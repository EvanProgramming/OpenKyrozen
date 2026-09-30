from __future__ import annotations

import time
from openkyrozen.persistence.models import stable_hash

def _age_out_old_coded_entries(self) -> None:
    """Remove file snapshots for .py files that no longer exist on disk."""
    now = time.time()
    if now - self._last_code_scan_time < 3600:  # once per hour
        return
    self._last_code_scan_time = now
    project_root = self._get_workspace_root()
    skip_dirs = {".venv", "venv", "chroma_memory", "__pycache__", ".git"}
    valid: set[str] = set()
    for py_file in project_root.rglob("*.py"):
        if not any(part in py_file.parts for part in skip_dirs):
            valid.add(str(py_file.relative_to(project_root)))
    self.memory_bank.remove_stale_files(valid)


def _consolidate_memories(self) -> None:
    self._take_tool_snapshot()
    """Cluster, deduplicate and summarise recent facts and logs."""
    recent = self.memory_bank.get_recent(50)
    if not recent:
        return

    # Keep only non‑trivial logs
    non_trivial = [r for r in recent if r and not r.startswith("FILE:")]
    if len(non_trivial) < 3:
        return

    consolidate_prompt = (
        "You are Kyrozen's memory consolidation module. Read the following recent logs "
        "and merge duplicates, remove contradictions (keeping the most recent), "
        "and extract a minimal set of important facts. "
        "Output each consolidated fact on a new line prefixed with 'FACT:'. "
        "If nothing important, output only '—'.\n\n"
        + "\n".join(non_trivial)
    )
    try:
        messages = [{"role": "system", "content": consolidate_prompt}]
        answer = (self._learning_model_response(messages, feature="consolidate_memories") or "").strip()
        if answer and not answer.startswith("—"):
            # Store consolidated facts
            for line in answer.split("\n"):
                line = line.strip()
                if line.startswith("FACT:"):
                    self.memory_bank.add_log(line)
            # Remove the original logs that were just consolidated (to avoid duplication)
            # We can delete them via ChromaDB if available
            self._remove_consolidated_entries(non_trivial)
    except Exception:
        pass


def _remove_consolidated_entries(self, logs: list[str]) -> None:
    """Delete exact source logs through the durable memory facade."""
    if not logs:
        return
    try:
        self.memory_bank.delete_logs(logs)
    except Exception as exc:
        self.memory_bank.store.append_event(
            "memory.cleanup_failed", {"error": str(exc)[:500], "count": len(logs)},
            user_id=self.memory_bank.user_id, workspace_id=self.memory_bank.workspace_id,
            session_id=self.memory_bank.session_id,
        )


def _score_memory_importance(self, entry: str) -> int:
    """Score a memory entry 0-10 based on its apparent importance."""
    score = 1
    if entry.startswith("FACT:"): score += 3
    if entry.startswith("FAILURE:"): score += 5
    if entry.startswith("FIX_OUTCOME:"): score += 4
    if entry.startswith("STRATEGY:"): score += 4
    if entry.startswith("PREF:"): score += 3
    if entry.startswith("SKILL:"): score += 3
    if entry.startswith("FILE:"): score -= 2
    if len(entry) > 200: score += 1
    if len(entry) > 500: score += 1
    if any(kw in entry.lower() for kw in ["bug", "fix", "error", "critical", "important"]): score += 2
    return max(0, min(10, score))


def _forget_recent(self, prefix: str = "", count: int = 5) -> str:
    """Show recent learnable entries and allow the user to delete them."""
    recent = self.memory_bank.get_recent(100)
    learnable = [r for r in recent if r and not r.startswith("FILE:")]
    if prefix:
        learnable = [r for r in learnable if prefix.lower() in r.lower()]
    learnable = learnable[:count]
    if not learnable:
        return "No matching memories found."
    lines = ["Recent learnings (most recent first):"]
    for i, entry in enumerate(learnable):
        score = self._score_memory_importance(entry)
        snippet = entry[:120].replace("\n", " ")
        lines.append(f"  [{i}] (score:{score}) {snippet}")
    return "\n".join(lines)


def _extract_knowledge_graph(self) -> None:
    """Extract entities and relationships from memory with the selected learning model."""
    recent = self.memory_bank.get_recent(50)
    facts = [r for r in recent if r and r.startswith("FACT:")][:10]
    if len(facts) < 3:
        return
    prompt = (
        "Extract key entities and relationships from these facts. "
        "Output in format 'entity -> related_entity' (one per line).\n\n"
        + "\n".join(f"  - {fact[:200]}" for fact in facts)
    )
    answer = self._learning_model_response([{"role": "system", "content": prompt}], feature="knowledge_graph_extraction") or ""
    for line in answer.splitlines():
        if "->" not in line:
            continue
        source, target = (part.strip().lower() for part in line.split("->", 1))
        if source and target:
            self._knowledge_graph.setdefault(source, []).append(target)
            self.memory_bank.add_log(f"GRAPH: {source} -> {target}")


def _auto_learn_conversations(self) -> None:
    new_count = self.memory_bank.count_logs()
    if new_count == self._logs_count_at_last_learn:
        return  # no new logs to process
    # A learning cycle is deliberately bounded even when a deployment has a
    # large backlog after a long outage.  Prefer the newest window and record
    # the current high-water mark so the same backlog is not reprocessed.
    num_new = max(0, min(new_count - self._logs_count_at_last_learn, 20))
    recent = self.memory_bank.get_recent(num_new)
    if not recent:
        self._logs_count_at_last_learn = new_count
        return
    # Remove system‑internal logs that shouldn't be learned
    recent = [
        log for log in recent if log
        if not log.startswith("FACT:")
        and not log.startswith("LEARNED:")
        and not log.startswith("FILE:")
    ]
    if not recent:
        self._logs_count_at_last_learn = new_count
        return
    learn_prompt = (
        "You are Kyrozen's self-learning module. Read the following recent conversation logs "
        "and extract any important facts, user preferences, or new skills that should be "
        "remembered for future interactions. Capture every distinct explicit preference, "
        "including multiple preferences in one sentence, as separate facts. Output a bullet list of facts. "
        "Do not answer the user. For example, ‘请一直用中文回复，并且回答要简洁’ requires separate "
        "facts for Chinese responses and concise responses. "
        "If nothing important, output only ‘—’.\n\n"
        + "\n".join(recent)
    )
    messages = [{"role": "system", "content": learn_prompt}]
    try:
        fact_text = (self._learning_model_response(messages, feature="auto_learn_conversations") or "").strip()
        if fact_text and fact_text not in ("—", ""):
            for line in fact_text.split("\n"):
                line = line.strip().lstrip("-* ").strip()
                if line:
                    self.learning_engine.submit(
                        "fact", line, evidence_id=stable_hash(recent[0][:1000] + line), confidence=0.5,
                        metadata={"source": "conversation_learning"},
                        evidence_text="\n".join(recent)[:4000],
                    )
    except Exception as exc:
        self.memory_bank.store.append_event(
            "learning.conversation_failed", {"error": str(exc)[:1000]},
            user_id=self.memory_bank.user_id, workspace_id=self.memory_bank.workspace_id,
            session_id=self.memory_bank.session_id,
        )
    finally:
        self._logs_count_at_last_learn = new_count

from __future__ import annotations

import re
from openkyrozen.persistence.models import stable_hash

def _review_tools(self) -> None:
    """Analyse tool usage patterns and suggest merges or improvements."""
    tool_stats = self._learning_tool_stats()
    if len(tool_stats) < 3:
        return  # not enough data

    # Build a summary of tool usage
    summary_lines = []
    for name, stats in sorted(tool_stats.items(),
                              key=lambda x: x[1].get("calls", 0), reverse=True)[:10]:
        calls = stats.get("calls", 0)
        successes = stats.get("successes", 0)
        avg_time = stats.get("total_time", 0) / max(calls, 1)
        summary_lines.append(
            f"  {name}: {calls} calls, {successes} successes, "
            f"avg {avg_time:.2f}s per call"
        )

    if not summary_lines:
        return

    review_prompt = (
        "You are Kyrozen's tool review module. Review the following tool usage "
        "statistics and suggest improvements:\n"
        "- Are there tools that could be merged (similar functionality)?\n"
        "- Are any tools under‑used and candidates for removal?\n"
        "- Could any tool be made faster or more robust?\n"
        "Output each suggestion on a new line prefixed with 'TOOL_REVIEW:'.\n"
        "If no improvements needed, output '—'.\n\n"
        + "\n".join(summary_lines)
    )
    try:
        messages = [{"role": "system", "content": review_prompt}]
        answer = (self._learning_model_response(messages, feature="review_tools") or "").strip()
        if answer and answer not in ("—", ""):
            for line in answer.split("\n"):
                line = line.strip()
                if line.startswith("TOOL_REVIEW:"):
                    self._store_learning_product("review_tools", line)
    except Exception:
        pass


def _invent_skills(self) -> None:
    """Examine recent conversation logs and create a reusable skill
    (workflow) that the agent can later call via memory retrieval."""
    recent = self.memory_bank.get_recent(40)
    if not recent:
        return
    # Only conversation evidence may invent a conversation-derived workflow.
    logs = [r for r in recent if r.startswith("User:")]
    if len(logs) < 5:
        return

    prompt = (
        "You are Kyrozen's skill invention module. Read the following recent "
        "conversation logs and extract a reusable skill (workflow) that the "
        "agent could follow in the future to accomplish similar tasks more "
        "efficiently.\n\n"
        "Output exactly in this format, nothing else:\n\n"
        "Skill Name: <short name>\n"
        "Description: <short description>\n"
        "Steps:\n"
        "1. <step>\n"
        "2. <step>\n"
        "...\n\n"
        "If you cannot identify a useful skill, output only the single character '—'.\n\n"
        + "\n".join(logs[-20:])
    )
    try:
        messages = [{"role": "system", "content": prompt}]
        answer = (self._learning_model_response(messages, feature="invent_skills") or "").strip()
        if answer in ("—", ""):
            return
        # Parse the answer
        name_match = re.search(r"Skill Name:\s*(.+)", answer, re.IGNORECASE)
        desc_match = re.search(r"Description:\s*(.+)", answer, re.IGNORECASE)
        steps_match = re.search(r"Steps:\s*(.+)", answer, re.DOTALL | re.IGNORECASE)
        skill_name = name_match.group(1).strip() if name_match else "unknown"
        description = desc_match.group(1).strip() if desc_match else ""
        steps_text = steps_match.group(1).strip() if steps_match else ""
        lines = steps_text.split("\n")
        steps_clean = [line.strip() for line in lines if line.strip() and line.strip()[:1].isdigit()]
        steps_str = "\n".join(steps_clean)
        stored = f"SKILL: {skill_name} | {description}\nSteps:\n{steps_str}"
        if steps_clean and skill_name != "unknown":
            self.learning_engine.submit(
                "strategy", stored, evidence_id=stable_hash("\n".join(logs[:5])),
                metadata={"learning_feature": "invent_skills"},
                evidence_text="\n".join(logs[:5])[:4000],
            )
    except Exception as exc:
        self.memory_bank.store.append_event(
            "learning.skill_invention_failed", {"error": str(exc)[:1000]},
            user_id=self.memory_bank.user_id, workspace_id=self.memory_bank.workspace_id,
            session_id=self.memory_bank.session_id,
        )


def _auto_patch_new_technology(self, user_input: str) -> None:
    """If user mentions an unknown library, fetch its docs in background."""
    if self.learning_policy() != "remote":
        return
    detected: set[str] = set()

    def _extract_words(text: str) -> list[str]:
        """Split text into words, handling CJK by treating each CJK char as a word boundary."""
        result: list[str] = []
        buf: list[str] = []
        for ch in text:
            cp = ord(ch)
            if (0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or
                0x20000 <= cp <= 0x2A6DF or 0x3040 <= cp <= 0x30FF or
                0xAC00 <= cp <= 0xD7AF):
                if buf:
                    result.extend(''.join(buf).split())
                    buf = []
            elif ch.isspace() or ch in '"\'`,.;:!?()[]{}':
                if buf:
                    result.extend(''.join(buf).split())
                    buf = []
            else:
                buf.append(ch)
        if buf:
            result.extend(''.join(buf).split())
        return result or text.split()

    words = _extract_words(user_input)
    for w in words:
        # Pattern 1: suffix based detection (lib, py, framework, package)
        if w.endswith(("lib","py","framework","package")) and w not in self._known_libraries:
            detected.add(w)

    # Pattern 2: detect "use <Library>" or "using <Library>" phrases
    for match in re.finditer(r"(?:use|using)\s+([A-Za-z_]\w*)", user_input, re.IGNORECASE | re.UNICODE):
        lib = match.group(1)
        if lib.lower() not in ("a","an","the","this","that","it","my","our","your"):
            detected.add(lib)

    # Pattern 3: import statements in code or conversation
    for match in re.finditer(r"(?:import|from)\s+([A-Za-z_]\w*)", user_input, re.UNICODE):
        lib = match.group(1)
        if lib.lower() not in ("a","an","the","os","sys","re","json","time","math"):
            detected.add(lib)

    # Pattern 4: "pip install <lib>" or "install <lib>"
    for match in re.finditer(r"(?:pip\s+install|install)\s+([A-Za-z_][\w.-]*)", user_input, re.IGNORECASE | re.UNICODE):
        lib = match.group(1)
        detected.add(lib)

    # Pattern 5: well‑known library heuristics (capitalised or compound names)
    well_known = {
        "numpy","pandas","scipy","matplotlib","seaborn","plotly",
        "sklearn","scikit-learn","tensorflow","keras","pytorch","torch",
        "flask","django","fastapi","starlette","aiohttp","httpx","requests",
        "sqlalchemy","alembic","pydantic","celery","redis","rabbitmq",
        "pytest","unittest","mypy","ruff","black","isort","pre-commit",
        "docker","kubernetes","nginx","postgresql","mysql","mongodb",
        "react","vue","angular","svelte","next.js","tailwind","bootstrap",
        "graphql","grpc","protobuf","websocket","openapi","swagger",
    }
    for w in words:
        clean = w.strip('"\'`,.;:!?()[]{}').lower()
        if clean in well_known and clean not in self._known_libraries:
            detected.add(clean)

    # Spawn background fetches for new discoveries
    for lib in detected:
        with self._technology_lock:
            if len(self._technology_in_flight) >= 8 or lib in self._technology_in_flight:
                continue
            self._technology_in_flight.add(lib)
        self._technology_executor.submit(self._fetch_library_info, lib)


def _fetch_library_info(self, lib_name: str) -> None:
    """Search web for core concepts and store in memory."""
    search_web = self.AVAILABLE_TOOLS["search_web"]
    try:
        result = search_web(f"{lib_name} documentation overview")
        if result.startswith("- Title:"):
            self._store_learning_product("auto_patch_technology", f"LIBRARY_INFO: {lib_name}\n{result[:2000]}")
            self._known_libraries.add(lib_name)
    except Exception as exc:
        self.memory_bank.store.append_event(
            "learning.library_fetch_failed", {"library": lib_name, "error": str(exc)[:500]},
            user_id=self.memory_bank.user_id, workspace_id=self.memory_bank.workspace_id,
            session_id=self.memory_bank.session_id,
        )
    finally:
        with self._technology_lock:
            self._technology_in_flight.discard(lib_name)

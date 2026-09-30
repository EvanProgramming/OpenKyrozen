from __future__ import annotations

import re
from openkyrozen.tasks.engine import canonical_status, is_complete


def _requires_tool_action(self, text: str) -> bool:
    """Return True if the user text looks like a request that needs tool execution."""
    text_lower = text.lower()
    # Use CJK-aware word count instead of naive split
    if self._has_cjk(text):
        if self._cjk_approximate_words(text) <= 3:
            return False
    elif len(text.split()) <= 2:
        return False
    # Action verbs that strongly imply tool usage (word‑boundary match)
    action_indicators = [
        " search ", " research ", " find ", " compare ", " write ", " save ",
        " run ", " execute ", " build ", " generate ", " download ",
        " clone ", " edit ", " modify ", " move ", " copy ", " delete ", " remove ",
        " fetch ", " pull ",
        "create a", "create the", "make a", "make the",
        "list all", "list the", "show me", "check the",
        "table of", "chart", "graph", "visualize",
    ]
    # Add CJK action indicators
    _cjk_action_indicators = [
        "搜索", "查找", "找到", "写", "写入", "保存", "运行", "执行",
        "构建", "生成", "下载", "克隆", "编辑", "修改", "移动", "复制",
        "删除", "创建", "列出", "显示", "查看", "读取", "安装", "分析",
        "审查", "总结", "获取", "更新", "编译", "调试",
        "調べる", "検索", "実行", "作成", "書く", "読む", "削除",
        "검색", "찾기", "실행", "생성", "쓰기", "읽기", "삭제",
    ]
    if any(ind in text for ind in _cjk_action_indicators):
        return True
    if any(ind in " " + text_lower + " " for ind in action_indicators):
        return True
    # Context nouns that suggest tool-requiring tasks
    context_indicators = [
        "file", "folder", "directory", "repo", "repository", "github",
        "web", "internet", "online", "price", "spec", "specification",
        "report", "summary of", "overview of",
        "git", "commit", "branch", "merge", "push", "pull",
        "bug", "error", "traceback", "exception", "fix",
    ]
    if any(ind in text_lower for ind in context_indicators):
        return True
    return False


def _is_tool_error(self, result: str) -> bool:
    low = result.strip().lower()
    return (
        low.startswith("error") or
        low.startswith("exit code") or
        "no such file" in low or
        "syntax error" in low or
        "not found" in low or
        "no search results" in low or
        "search temporarily unavailable" in low or
        "traceback" in low or
        "traceback (most recent call last)" in low or
        "typeerror" in low or
        "valueerror" in low or
        "attributeerror" in low or
        "importerror" in low or
        "modulenotfounderror" in low or
        "keyerror" in low or
        "indexerror" in low or
        "git" in low and ("failed" in low or "fatal" in low or "error" in low)
    )


def _tasks_from_plan(self, text: str) -> None:
    """Parse a Plan block and create pending tasks."""
    if self.tasks.tasks:
        return
    plan_match = re.search(
        r"Plan:\s*\n(.*?)(?=\n\s*(?:Action|TaskList|$))",
        text,
        re.DOTALL | re.IGNORECASE
    )
    if not plan_match:
        return
    plan_body = plan_match.group(1).strip()
    descriptions = []
    for line in plan_body.splitlines():
        line = line.strip()
        if not line:
            continue
        # Remove leading numbering "1." or "1)" etc.
        cleaned = re.sub(r"^\s*\d+[.)]?\s*", "", line).strip()
        descriptions.append(cleaned or line)
    self.tasks.add_ordered_plan(descriptions, task_id_prefix="plan")


def _build_task_progress_hint(self) -> str:
    """Build a prominent task progress summary for the LLM feedback.
    Placed at the TOP of feedback so the LLM never loses track."""
    if not self.tasks.tasks:
        return ""
    total = len(self.tasks.tasks)
    done = sum(1 for t in self.tasks.tasks if is_complete(t))
    pending_tasks = [t for t in self.tasks.tasks if canonical_status(t["status"]) == "pending"]
    lines = [
        "=" * 40,
        f"📋 YOUR TASK LIST ({done}/{total} succeeded):",
    ]
    for i, t in enumerate(self.tasks.tasks):
        status = canonical_status(t["status"])
        icon = "✓" if is_complete(t) else "○" if status == "pending" else "◷"
        desc = t["description"][:80]
        lines.append(f"  [{i}] {icon} {desc}")
    if pending_tasks:
        next_task = pending_tasks[0]["description"][:80]
        lines.append(f"▶ NEXT TASK TO EXECUTE: \"{next_task}\"")
        lines.append(f"⚠️  {len(pending_tasks)} tasks remain. DO NOT STOP. Output the next Action NOW.")
    lines.append("=" * 40)
    return "\n".join(lines) + "\n"

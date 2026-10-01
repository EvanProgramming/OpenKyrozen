from __future__ import annotations

import re


def _detect_prompt_injection(self, text: str) -> str | None:
    """Return the matched injection pattern if detected, None otherwise."""
    for pattern in self._PROMPT_INJECTION_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return pattern
    return None


def _sanitize_input(self, text: str) -> tuple[str, bool]:
    """Sanitize user input for prompt injection. Returns (sanitized_text, was_flagged)."""
    injection = self._detect_prompt_injection(text)
    if injection:
        # Neutralize the injection by wrapping the suspicious part
        sanitized = re.sub(injection, "[filtered]", text, flags=re.IGNORECASE)
        return sanitized, True
    return text, False


def _is_bug_review(self, text: str) -> bool:
    """Distinguish requested findings/fixes from authorization to repair code."""
    low = text.strip().lower()
    review = re.search(r"\b(?:review|audit|inspect|identify|find)\b|审查|审计|检查|分析", low)
    repair = re.search(r"\b(?:fix|repair|resolve|patch|correct)\b|修复|修改", low)
    no_edits = re.search(
        r"\b(?:do not|don't|without|no)\s+(?:edit(?:s|ing)?|change(?:s|ing)?|modify|fix|write)\b\s*"
        r"(?:(?:anything|(?:any\s+)?(?:code|source|files?))\b|$|[.!])"
        r"|不要修改(?:代码|文件|任何)|不修改(?:代码|文件)|只审查|仅检查", low)
    return bool(review and (not repair or no_edits))


def _is_bug_report(self, text: str) -> bool:
    """Detect if user input is reporting a bug, error, or unexpected behaviour."""
    low = text.strip().lower()
    if self._is_bug_review(text):
        return False
    bug_indicators = [
        "bug", "error", "traceback", "exception", "crash", "broken",
        "doesn't work", "not working", "fails", "failing", "didn't work",
        "stack trace", "typeerror", "valueerror", "attributeerror",
        "keyerror", "indexerror", "importerror", "modulenotfounderror",
        "syntax error", "segfault", "segmentation fault", "null pointer",
        "undefined is not", "cannot read", "unexpected",
    ]
    if any(ind in low for ind in bug_indicators):
        return True
    # CJK bug report indicators
    _cjk_bug = ["报错", "错误", "出错了", "bug", "崩溃", "不行", "失败", "异常",
                 "不工作", "没反应", "闪退", "卡住", "死机"]
    if any(ind in text for ind in _cjk_bug):
        return True
    # Check for traceback patterns
    if re.search(r"File\s+\".+?\",\s+line\s+\d+", text):
        return True
    if re.search(r"^\s*Traceback\s", text, re.MULTILINE):
        return True
    return False


def _is_question(self, text: str) -> bool:
    low = text.strip().lower()
    if "?" in low:
        return True
    # CJK question markers (Chinese, Japanese, Korean)
    _cjk_question_markers = [
        "吗", "？", "呢", "吧", "么", "否", "如何", "怎么", "怎样",
        "什么", "何时", "何地", "为何", "か", "？", "까", "니", "냐",
    ]
    if any(marker in text for marker in _cjk_question_markers):
        return True
    question_phrases = [
        "shall i", "should i", "do you want me", "would you like me", "continue",
        "proceed", "next step",
    ]
    return any(phrase in low for phrase in question_phrases)

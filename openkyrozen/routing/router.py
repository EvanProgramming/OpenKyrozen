from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from openkyrozen.providers import ProviderConfig, model_for_complexity, resolve_ollama_models


def model_for_request(
    config: ProviderConfig | None,
    fast_route: Mapping[str, str],
    user_input: str,
    automatic_selector: Callable[[str], str],
) -> str:
    """Apply the main-model pin before either normal or fast routing."""
    if config and config.model_main:
        return config.model_main
    if config and fast_route.get("model") == "simple":
        return config.model_simple
    if config and fast_route.get("model") == "reasoning":
        return config.model_complex
    return automatic_selector(user_input)


def _select_model(self, user_input: str) -> str:
    """Select the configured provider's current simple/complex model."""
    text = user_input.lower().strip()

    # Complexity signals: length, multi-step, technical depth
    complexity_score = 0

    # Long inputs suggest complex tasks
    if len(user_input) > 200:
        complexity_score += 2
    elif len(user_input) > 100:
        complexity_score += 1

    # CJK: if input is CJK and longer than 50 chars, it's likely substantive
    if self._has_cjk(user_input) and len(user_input) > 50:
        complexity_score += 1

    # Multi-step / numbered instructions (English)
    if re.search(r"\b(step|first|then|next|finally|after that)\b", text):
        complexity_score += 2
    # CJK multi-step indicators
    _cjk_step_markers = ["第一步", "第二步", "首先", "然后", "接着", "最后", "最終",
                          "最初", "次に", "最後に", "まず", "다음", "마지막"]
    if any(marker in user_input for marker in _cjk_step_markers):
        complexity_score += 2
    if re.search(r"\d+[.)]\s", text):
        complexity_score += 1

    # Code-related tasks (English)
    code_keywords = [
        "code", "debug", "fix", "refactor", "implement", "build a",
        "write a program", "write a script", "function", "class",
        "architecture", "design pattern", "optimize", "performance",
        "bug", "error", "traceback", "exception", "crash", "broken",
        "doesn't work", "not working", "fails", "failing"
    ]
    if any(kw in text for kw in code_keywords):
        complexity_score += 3  # code tasks are inherently complex
    # CJK code-related tasks
    _cjk_code_keywords = ["代码", "调试", "修复", "重构", "实现", "写一个程序",
                           "写脚本", "函数", "架构", "优化", "性能", "编程",
                           "bug", "报错", "错误", "出错了", "崩溃", "不行"]
    if any(kw in user_input for kw in _cjk_code_keywords):
        complexity_score += 3

    # Deep analysis / reasoning (English)
    analysis_keywords = [
        "analyze", "explain how", "explain why", "compare", "evaluate",
        "review this", "understand", "deep dive", "audit", "diagnose"
    ]
    if any(kw in text for kw in analysis_keywords):
        complexity_score += 2
    # CJK analysis keywords
    _cjk_analysis_keywords = ["分析", "解释", "比较", "评估", "审查", "理解",
                               "深度", "审计", "诊断", "分析一下", "帮我分析"]
    if any(kw in user_input for kw in _cjk_analysis_keywords):
        complexity_score += 2

    # Git-related tasks (English) — may need careful reasoning
    git_keywords = [
        "git commit", "git push", "git pull", "git merge", "git rebase",
        "git branch", "git checkout", "commit this", "push this",
        "merge branch", "create a branch", "switch branch"
    ]
    if any(kw in text for kw in git_keywords):
        complexity_score += 2
    # CJK git keywords
    _cjk_git_keywords = ["git提交", "git推送", "git合并", "提交代码", "推送代码",
                          "创建分支", "合并分支", "切换分支"]
    if any(kw in user_input for kw in _cjk_git_keywords):
        complexity_score += 2

    # Research / multi-source tasks (English)
    research_keywords = [
        "research", "find all", "gather", "summarize", "comprehensive",
        "report", "overview of", "investigate"
    ]
    if any(kw in text for kw in research_keywords):
        complexity_score += 1
    # CJK research keywords
    _cjk_research_keywords = ["研究", "搜索", "总结", "汇总", "综合", "报告",
                               "调查", "全面", "概述", "搜集"]
    if any(kw in user_input for kw in _cjk_research_keywords):
        complexity_score += 1

    config = self._provider_config or ProviderConfig(provider="deepseek")
    if config.provider == "ollama":
        simple, complex_model = resolve_ollama_models(config)
        config.model_simple, config.model_complex = simple, complex_model
    return model_for_complexity(config, complexity_score >= 3)


def _classify_complexity(self, user_input: str) -> str:
    """Classify the user request as simple, medium, or complex.
    Returns 'simple', 'medium', or 'complex'."""
    text = user_input.lower().strip()

    # Simple: short greetings, trivial questions, single actions (English)
    simple_patterns = [
        r"^(hi|hey|hello|yo|sup|good morning|good evening)\b",
        r"^(thanks|thank you|ok|okay|got it|bye|goodbye)\b",
        r"^(what is|who is|when is|where is|how are you|what can you)\b",
        r"^(yes|no|maybe|sure|yep|nope)$",
    ]
    if any(re.search(p, text) for p in simple_patterns):
        return "simple"

    # CJK simple greetings
    _cjk_simple_patterns = [
        r"^(你好|您好|嗨|早|哈喽|哈啰|喂|谢谢|再见|拜拜|好的|嗯|哦|知道了)",
        r"^(こんにちは|もしもし|おはよう|こんばんは|さようなら|ありがとう|はい|いいえ)",
        r"^(안녕|안녕하세요|감사합니다|네|아니요|잘가)",
    ]
    if any(re.search(p, user_input) for p in _cjk_simple_patterns):
        return "simple"

    # Short inputs without tool verbs are simple (CJK-aware)
    _tool_verbs = {"read","list","find","search","write","run","create","clone","fetch","open","edit","delete","move","copy"}
    _cjk_tool_verbs = {"读","读取","查","查找","搜索","写","运行","创建","克隆","获取","编辑","删除","移动","复制","列出","安装","读文件","写文件","搜索网页","执行"}
    if self._has_cjk(user_input):
        words = self._cjk_approximate_words(user_input)
        if words <= 4 and not any(tv in user_input for tv in _cjk_tool_verbs):
            return "simple"
    else:
        words = set(user_input.lower().split())
        if len(user_input.split()) <= 3 and not (words & _tool_verbs):
            return "simple"

    # Complex: multi-step, code generation, deep analysis, bug fixing, git operations
    complex_indicators = [
        r"\b(step|first|then|next|finally|after that)\b",
        r"\d+[.)]\s+\w",  # numbered steps
        r"\b(implement|refactor|migrate|audit|comprehensive)\b",
        r"\b(write\s+(a|the)\s+(program|script|app|application|function|class))\b",
        r"\b(analy[sz]e\s+(the|this|my|our)\s+(codebase|repo|project|architecture))\b",
        r"\b(debug|fix\s+(the|this|a|my)\s+(bug|error|issue|problem|crash))\b",
        r"\b(traceback|stack\s*trace|exception|TypeError|ValueError|AttributeError)\b",
        r"\b(git\s+(merge|rebase|cherry-pick|bisect))\b",
    ]
    if any(re.search(p, text) for p in complex_indicators):
        return "complex"
    # CJK complex indicators
    _cjk_complex = ["实现", "重构", "迁移", "审计", "审查代码", "写一个程序", "写脚本",
                     "分析代码", "分析架构", "第一步", "第二步", "全面的",
                     "调试", "修复bug", "修bug", "出错了", "报错", "错误",
                     "git操作", "git合并", "提交代码", "推送", "拉取"]
    if any(ind in user_input for ind in _cjk_complex):
        return "complex"
    # Long inputs with multiple sentences suggest complexity
    if len(user_input) > 250:
        return "complex"

    # Default: medium (most tool-using requests)
    return "medium"

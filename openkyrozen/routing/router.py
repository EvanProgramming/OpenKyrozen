from __future__ import annotations

import re
from dataclasses import dataclass
from collections.abc import Callable, Mapping
from typing import Any
from openkyrozen.providers import ProviderConfig, model_for_complexity, resolve_ollama_models
from openkyrozen.tasks.models import requires_working_plan


_MULTI_STEP_RE = re.compile(
    r"\b(?:then|after that|step[- ]by[- ]step|step\s*\d+)\b|"
    r"(?:^|[.!?;:,])\s*(?:first|next|finally)\b"
)


@dataclass(frozen=True)
class RoutingDecision:
    provider: str
    model: str
    reasoning: str
    budget_tokens: int | None
    profile: str
    planning_required: bool


def model_for_request(
    config: ProviderConfig | None,
    fast_route: Mapping[str, str],
    user_input: str,
    automatic_selector: Callable[[str], str],
) -> str:
    """Apply the main-model pin before either normal or fast routing."""
    if config and config.model_main and config.model_main != "auto":
        return config.model_main
    if config and fast_route.get("model") == "simple":
        return config.model_simple
    if config and fast_route.get("model") == "reasoning":
        return config.model_complex
    return automatic_selector(user_input)


def decide_request(config: ProviderConfig | None, advice: Mapping[str, Any], user_input: str,
                   automatic_model: Callable[[str], str], classify: Callable[[str], str],
                   automatic_profile: str, profile_override: str, interaction_mode: str
                   ) -> tuple[RoutingDecision, str]:
    """Resolve advisory signals once into bounded runtime routing state."""
    complexity = advice.get("complexity")
    if complexity not in {"simple", "medium", "complex"}:
        complexity = classify(user_input)
    profile = profile_override if profile_override in {"coder", "researcher"} else automatic_profile
    if profile_override == "auto" and advice.get("profile") in {"coder", "researcher"}:
        profile = advice["profile"]
    model = model_for_request(config, advice, user_input, automatic_model)
    extended = bool(config and config.model_complex and model == config.model_complex
                    and model != config.model_simple)
    if advice.get("model") == "reasoning" and config and not (config.model_main and config.model_main != "auto"):
        extended = True
    decision = RoutingDecision(
        provider=config.provider if config else "unknown",
        model=model,
        reasoning="extended" if extended else "standard",
        budget_tokens=None,
        profile=profile,
        planning_required=(interaction_mode == "agent" and requires_working_plan(complexity)),
    )
    return decision, complexity


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
    if _MULTI_STEP_RE.search(text):
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
        "debug", "fix", "refactor", "implement", "build a",
        "write a program", "write a script", "optimize",
        "bug", "error", "traceback", "exception", "crash", "broken",
        "doesn't work", "not working", "fails", "failing"
    ]
    if any(kw in text for kw in code_keywords):
        complexity_score += 3
    # CJK code-related tasks
    _cjk_code_keywords = ["调试", "修复", "重构", "实现", "写一个程序",
                           "写脚本", "优化", "编程",
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
    return model_for_complexity(config, complexity_score >= 3 or _classify_complexity(self, user_input) == "complex")


def _classify_complexity(self, user_input: str) -> str:
    """Classify the user request as simple, medium, or complex.
    Returns 'simple', 'medium', or 'complex'."""
    text = user_input.lower().strip()

    # Task signals take precedence over a greeting or question prefix.
    complex_indicators = [
        r"\d+[.)]\s+\w",
        r"\b(implement|refactor|migrate|audit|comprehensive)\b",
        r"\b(write\s+(a|the)\s+(program|script|app|application|function|class))\b",
        r"\b(analy[sz]e\s+(the|this|my|our)\s+(codebase|repo|project|architecture))\b",
        r"\b(debug|fix\s+(the|this|a|my)\s+(bug|error|issue|problem|crash))\b",
        r"\b(git\s+(merge|rebase|cherry-pick|bisect))\b",
    ]
    if _MULTI_STEP_RE.search(text) or any(re.search(p, text) for p in complex_indicators):
        return "complex"
    _cjk_complex = ["实现", "重构", "迁移", "审计", "审查代码", "写一个程序", "写脚本",
                     "分析代码", "分析架构", "第一步", "第二步", "全面的",
                     "调试", "修复bug", "修bug", "出错了", "报错", "错误",
                     "git操作", "git合并", "提交代码", "推送", "拉取"]
    if any(ind in user_input for ind in _cjk_complex):
        return "complex"

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

    if re.search(r"\b(traceback|stack\s*trace|exception|typeerror|valueerror|attributeerror)\b", text):
        return "complex"

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

    # Long inputs with multiple sentences suggest complexity
    if len(user_input) > 250:
        return "complex"

    # Default: medium (most tool-using requests)
    return "medium"

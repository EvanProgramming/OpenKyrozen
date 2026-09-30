from __future__ import annotations

import re

JEV_MODEL = "jev-latest"


KEV_MODEL = "kev-latest"


KEV_PACKAGE = "kev[serve] @ git+https://github.com/jaredpalmer/kev.git@5920c5fe4ca8e0970ed4209ac2c9b8e18bea5109"


KEV_RUN = "jaredpalmer/kev-0.8b"


KEV_URL = "http://127.0.0.1:8009"


_JEV_URL = "https://api.typesafe.ai"


DECISION_ASSIST_BACKENDS = frozenset({"off", "jev", "kev"})


_DECISION_ASSIST_KEY = "decision_assist"


_PRIVATE_RE = re.compile(
    r"(?i)(?:\b(?:private|confidential|personal|do not share|internal)\b|"
    r"(?:[\w.+-]+@[\w.-]+\.[A-Za-z]{2,})|(?:/Users/|/home/|[A-Za-z]:\\))"
)


_JEV_MODEL_CACHE: dict[str, object] = {"checked_at": 0.0, "alias": JEV_MODEL, "release_date": None,
                                      "health": "unknown", "fallback_reason": ""}


_JEV_MODEL_REFRESH_SECONDS = 24 * 60 * 60


_TOOL_INSTRUCTION_RE = re.compile(
    r"(?im)^\s*(?:system\s*:|assistant\s*:|user\s*:|ignore\s+(?:all|previous|prior)|"
    r"follow\s+these\s+instructions|run\s+(?:this|the)\s+command|send\s+.*(?:token|password|secret)|"
    r"upload\s+.*(?:file|data)|do\s+not\s+tell\s+the\s+user)\b.*$"
)


_USER_OWNED_RE = re.compile(
    r"(?i)\b(approve|authorize|consent|permission|delete|remove|commit|push|deploy|publish|"
    r"install|purchase|pay|send|share|scope|target|file|directory|branch|repository|"
    r"plan|verify|verification|overwrite|replace|which account|preference)\b|"
    r"授权|同意|删除|提交|推送|部署|购买|支付|发送|分享|范围|目标|偏好|验证|文件|计划"
)

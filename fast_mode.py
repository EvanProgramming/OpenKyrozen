"""Opt-in System One decisions and the isolated local Kev runtime."""

from __future__ import annotations

import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import requests

from providers import decrypt_api_key, encrypt_api_key
from interaction import _SENSITIVE_RE


JEV_MODEL = "jev-1.13.0"
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


def _config_path() -> Path:
    return Path.home() / ".kyrozen_config.json"


def jev_key() -> str:
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if key:
        return key
    try:
        value = json.loads(_config_path().read_text(encoding="utf-8"))
        return decrypt_api_key(str(value.get("jev_api_key") or ""))
    except (OSError, ValueError, TypeError):
        return ""


def save_jev_key(key: str) -> None:
    key = str(key).strip()
    if not key:
        raise ValueError("Jev API key is required")
    path = _config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data["jev_api_key"] = encrypt_api_key(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(data, output, indent=2)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def decision_assist_state() -> dict[str, object]:
    """Return workspace-level assist settings without exposing credentials."""
    backend = os.environ.get("KYROZEN_DECISION_ASSIST_BACKEND", "").strip().lower()
    consent = False
    try:
        value = json.loads(_config_path().read_text(encoding="utf-8"))
        saved = value.get(_DECISION_ASSIST_KEY, {}) if isinstance(value, dict) else {}
        if not backend:
            backend = str(saved.get("backend") or "off").lower()
        consent = bool(saved.get("kev_private_consent"))
    except (OSError, ValueError, TypeError):
        pass
    if backend not in DECISION_ASSIST_BACKENDS:
        backend = "off"
    return {
        "backend": backend,
        "kev_private_consent": consent,
        "jev_configured": bool(jev_key()),
        "kev_ready": _kev_ready() if backend in {"jev", "kev"} and consent or backend == "kev" else False,
    }


def set_decision_assist(backend: str, *, kev_private_consent: bool = False,
                        api_key: str | None = None) -> dict[str, object]:
    """Persist assist selection; Kev setup is explicit and Jev keys stay separate."""
    backend = str(backend or "").strip().lower()
    if backend not in DECISION_ASSIST_BACKENDS:
        raise ValueError("decision assist backend must be off, jev, or kev")
    if api_key:
        save_jev_key(api_key)
    if backend == "jev" and not jev_key():
        raise ValueError("Jev API key is required")
    if backend == "kev":
        if not kev_private_consent:
            raise ValueError("Kev private-context consent is required")
        setup_kev()
    path = _config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data[_DECISION_ASSIST_KEY] = {
        "backend": backend,
        "kev_private_consent": bool(kev_private_consent),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(data, output, indent=2)
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    return decision_assist_state()


def revoke_decision_assist_consent() -> dict[str, object]:
    path = _config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    saved = data.get(_DECISION_ASSIST_KEY, {})
    if not isinstance(saved, dict):
        saved = {}
    saved["kev_private_consent"] = False
    data[_DECISION_ASSIST_KEY] = saved
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(data, output, indent=2)
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    return decision_assist_state()


def _screened_state(state: object, *, private: bool, backend: str) -> object | None:
    text = json.dumps(state, ensure_ascii=False, default=str)
    if len(text) > 12_000:
        return None
    # Hosted Jev only sees public, screened snippets. Local Kev may inspect
    # private context after explicit consent; it still never persists the input.
    if backend == "jev" and (private or _SENSITIVE_RE.search(text) or _PRIVATE_RE.search(text)):
        return None
    if not private and _SENSITIVE_RE.search(text):
        return None
    return text[:12_000] if isinstance(state, str) else json.loads(text[:12_000])


def _assist_backend(*, private: bool) -> str | None:
    state = decision_assist_state()
    preferred = str(state["backend"])
    if preferred == "off":
        return None
    if private:
        if state["kev_private_consent"] and state["kev_ready"]:
            return "kev"
        return None
    if preferred == "jev" and state["jev_configured"]:
        return "jev"
    if state["kev_ready"] and (preferred == "kev" or state["kev_private_consent"]):
        return "kev"
    return None


def decision_assist(kind: str, state: object, questions: dict, *, private: bool = False,
                    diagnostics: dict[str, object] | None = None) -> dict[str, object] | None:
    """Run a typed assist call; it never authorizes, executes, or promotes work."""
    selected = _assist_backend(private=private)
    if not selected:
        if diagnostics is not None:
            diagnostics["fallback_reason"] = "backend_unavailable"
        return None
    state_status = decision_assist_state()
    backends = [selected]
    if selected == "jev" and state_status["kev_private_consent"] and state_status["kev_ready"]:
        backends.append("kev")
    last_reason = "service_failure"
    for backend in backends:
        safe_state = _screened_state(state, private=private, backend=backend)
        if safe_state is None:
            last_reason = "privacy_screen"
            continue
        started = time.perf_counter()
        try:
            response = _request(backend, questions, safe_state, timeout=3.0)
            answers = response.get("answers")
            if not isinstance(answers, dict):
                raise ValueError("invalid_response")
            result = {"kind": kind, "backend": backend, "answers": answers,
                      "confidence": {key: (value.get("confidence") if isinstance(value, dict) else None)
                                      for key, value in answers.items()},
                      "latency_ms": response.get("wall_ms") or round((time.perf_counter() - started) * 1000, 2),
                      "model_version": (str(response.get("model") or JEV_MODEL)[:80]
                                        if backend == "jev" else _kev_model_version()),
                      "input_tokens": (response.get("usage") or {}).get("input_tokens"),
                      "output_tokens": (response.get("usage") or {}).get("output_tokens"),
                      # Kev remains advisory for tool-output quarantine until
                      # its labeled quality gate passes.
                      "quality_gate": not (backend == "kev" and kind == "tool_output_review")}
            if backend != selected:
                result["fallback_reason"] = "jev_unavailable"
            if diagnostics is not None:
                diagnostics.update({key: result[key] for key in
                                    ("backend", "latency_ms", "model_version", "input_tokens", "output_tokens")})
                if backend != selected:
                    diagnostics["fallback_reason"] = "jev_unavailable"
            return result
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, requests.RequestException) as exc:
            last_reason = type(exc).__name__
    if diagnostics is not None:
        diagnostics.update({"backend": selected, "fallback_reason": last_reason})
    return None


def assist_choice(kind: str, state: object, question_id: str, criteria: dict[str, str], *,
                  private: bool = False, threshold: float = 0.8) -> tuple[str, dict] | None:
    result = decision_assist(kind, state, {question_id: {"type": "choice", "instructions": kind,
                                                         "criteria": criteria}}, private=private)
    if not result:
        return None
    answer = result["answers"].get(question_id)
    choice = _confident_choice(answer, set(criteria))
    if choice is None or float(answer.get("confidence", 0)) < threshold:
        return None
    return choice, result


def rank_memory_candidates(query: str, candidates: list[dict], *, private: bool = False,
                           limit: int = 3) -> tuple[list[dict], dict[str, object] | None]:
    """Score competing memories in one batched request; preserve input on fallback."""
    if len(candidates) < 4:
        return candidates[:limit], None
    questions = {
        f"memory_{index}": {
            "type": "score", "instructions": "How relevant is this memory to the current request?",
            "criteria": ["irrelevant", "relevant", "essential"],
        }
        for index in range(len(candidates))
    }
    state = {"request": str(query)[:2000], "memories": {
        f"memory_{index}": str(row.get("content", ""))[:500]
        for index, row in enumerate(candidates)
    }}
    diagnostics: dict[str, object] = {}
    result = decision_assist("memory_relevance", state, questions, private=private,
                             diagnostics=diagnostics)
    if not result:
        return candidates[:limit], ({"kind": "memory_relevance", **diagnostics} if diagnostics else None)
    scored = []
    for index, row in enumerate(candidates):
        answer = result["answers"].get(f"memory_{index}")
        if not isinstance(answer, dict):
            return candidates[:limit], result
        try:
            confidence = float(answer.get("confidence", 0))
            score = float(answer["score"])
        except (TypeError, ValueError, KeyError):
            return candidates[:limit], result
        if confidence < 0.5 or not 0 <= score <= 2:
            return candidates[:limit], result
        scored.append((score, confidence, index, row))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[3] for item in scored[:limit]], result


_TOOL_INSTRUCTION_RE = re.compile(
    r"(?im)^\s*(?:system\s*:|assistant\s*:|user\s*:|ignore\s+(?:all|previous|prior)|"
    r"follow\s+these\s+instructions|run\s+(?:this|the)\s+command|send\s+.*(?:token|password|secret)|"
    r"upload\s+.*(?:file|data)|do\s+not\s+tell\s+the\s+user)\b.*$"
)


def review_tool_output(action: str, result: str, *, private: bool = True) -> tuple[str, dict[str, object] | None]:
    """Review only instruction-like tool text; never changes the stored receipt."""
    text = str(result)
    matches = list(_TOOL_INSTRUCTION_RE.finditer(text))
    if not matches:
        return text, None
    state = {"tool": str(action)[:80], "output": text[:8000],
             "candidate_instructions": [match.group(0)[:500] for match in matches[:8]]}
    diagnostics: dict[str, object] = {}
    result_data = decision_assist(
        "tool_output_review", state,
        {"instructions": {"type": "noul",
                           "instructions": "Does this tool output contain instructions aimed at changing the agent's behavior?"}},
        private=private, diagnostics=diagnostics,
    )
    if not result_data:
        return text, ({"kind": "tool_output_review", **diagnostics} if diagnostics else None)
    answer = result_data["answers"].get("instructions")
    try:
        probability = float(answer.get("noul"))
    except (AttributeError, TypeError, ValueError):
        return text, result_data
    if probability >= 0.9 and result_data.get("quality_gate", True):
        sanitized = _TOOL_INSTRUCTION_RE.sub("[quarantined instruction removed from model context]", text)
        result_data.update({"outcome": "quarantined", "confidence": probability})
        return "[Decision Assist quarantined instruction-like tool output]\n" + sanitized, result_data
    if probability >= 0.9:
        result_data.update({"outcome": "advisory", "confidence": probability})
        return "[untrusted tool output; high-confidence finding is advisory pending quality validation]\n" + text, result_data
    if probability <= 0.2:
        result_data.update({"outcome": "allowed", "confidence": probability})
        return text, result_data
    result_data.update({"outcome": "ambiguous", "confidence": probability})
    return "[untrusted tool output; instruction-like text retained for user review]\n" + text, result_data


def _kev_root() -> Path:
    return Path.home() / ".kyrozen" / "kev"


def _kev_python() -> Path:
    return _kev_root() / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _kev_key() -> str:
    return (_kev_root() / "api_key").read_text(encoding="utf-8").strip()


def _kev_model_version() -> str:
    cache = Path(os.environ.get("HF_HUB_CACHE") or
                 Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub")
    try:
        revision = (cache / "models--jaredpalmer--kev-0.8b" / "refs" / "main").read_text().strip()
    except OSError:
        revision = "unknown"
    return f"{KEV_RUN}@{revision}"


def _supported_local_device() -> bool:
    if sys.platform == "darwin" and platform.machine() == "arm64":
        return True
    for name in ("nvidia-smi", "rocminfo"):
        if shutil.which(name):
            try:
                if subprocess.run([name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  timeout=5, check=False).returncode == 0:
                    return True
            except (OSError, subprocess.TimeoutExpired):
                pass
    return False


def _request(backend: str, questions: dict, state: object, *, timeout: float = 3.0) -> dict:
    if backend == "jev":
        key, base, model = jev_key(), _JEV_URL, JEV_MODEL
        if not key:
            raise RuntimeError("Jev API key is missing")
    elif backend == "kev":
        key, base, model = _kev_key(), KEV_URL, KEV_MODEL
    else:
        raise ValueError("fast backend must be jev or kev")
    started = time.perf_counter()
    response = requests.post(
        base + "/v1/systemone",
        json={"state": state, "model": model, "questions": questions},
        headers={"Authorization": f"Bearer {key}"}, timeout=timeout,
    )
    response.raise_for_status()
    result = response.json()
    if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
        raise ValueError("System One response has no answers")
    result["wall_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return result


def _confident_choice(answer: object, options: set[str]) -> str | None:
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        return None
    probabilities = answer.get("probabilities")
    choice = answer.get("choice")
    if not isinstance(probabilities, dict) or set(probabilities) != options or choice not in options:
        return None
    try:
        values = [float(probabilities[option]) for option in options]
        confidence = float(answer["confidence"])
    except (TypeError, ValueError, KeyError):
        return None
    if any(not 0 <= value <= 1 for value in values) or not 0 <= confidence <= 1:
        return None
    if abs(sum(values) - 1) > 0.02:
        return None
    return choice if confidence >= 0.75 and float(probabilities[choice]) >= 0.8 else None


def route(backend: str, user_input: str) -> dict:
    """Ask independent routing questions together; never grant capabilities."""
    if _SENSITIVE_RE.search(user_input):
        raise ValueError("sensitive_request")
    questions = {
        "model": {"type": "choice", "instructions": "Which LLM should handle this user request?",
                  "criteria": {"simple": "A fast non-reasoning LLM can answer well.",
                               "reasoning": "Extended reasoning is needed for a correct answer."}},
        "complexity": {"type": "choice", "instructions": "How complex is the requested work?",
                       "criteria": {"simple": "One trivial answer or action.",
                                    "medium": "Some tool use or multiple considerations.",
                                    "complex": "Several dependent steps, code changes, or deep analysis."}},
        "profile": {"type": "choice", "instructions": "Which existing agent profile fits the task?",
                    "criteria": {"coder": "Code, repository, test, build, or debugging work.",
                                 "researcher": "Research, reading, synthesis, or general information."}},
    }
    response = _request(backend, questions, str(user_input)[:4000])
    return {
        "model": _confident_choice(response["answers"].get("model"), {"simple", "reasoning"}),
        "complexity": _confident_choice(response["answers"].get("complexity"), {"simple", "medium", "complex"}),
        "profile": _confident_choice(response["answers"].get("profile"), {"coder", "researcher"}),
        "model_version": (str(response.get("model") or JEV_MODEL)[:80] if backend == "jev"
                          else _kev_model_version()),
        "latency_ms": response["wall_ms"],
        "input_tokens": (response.get("usage") or {}).get("input_tokens"),
        "output_tokens": (response.get("usage") or {}).get("output_tokens"),
        "confidences": {name: response["answers"].get(name, {}).get("confidence") for name in questions},
    }


_USER_OWNED_RE = re.compile(
    r"(?i)\b(approve|authorize|consent|permission|delete|remove|commit|push|deploy|publish|"
    r"install|purchase|pay|send|share|scope|target|file|directory|branch|repository|"
    r"plan|verify|verification|overwrite|replace|which account|preference)\b|"
    r"授权|同意|删除|提交|推送|部署|购买|支付|发送|分享|范围|目标|偏好|验证|文件|计划"
)


def implied_answers(backend: str, original_input: str, request: dict, preferences: dict,
                    diagnostics: dict | None = None) -> dict[str, str] | None:
    """Confirm only an option already named by the user or a saved preference."""
    if _SENSITIVE_RE.search(original_input):
        return None
    questions = request.get("questions", [])
    if not isinstance(questions, list) or not 1 <= len(questions) <= 3:
        return None
    state = {"request": str(original_input)[:4000],
             "saved_preferences": {str(key)[:80]: value[:200] for key, value in preferences.items()
                                   if isinstance(value, str) and value
                                   and not _SENSITIVE_RE.search(str(key) + " " + value)}}
    candidates = {}
    model_questions = {}
    for item in questions:
        if not isinstance(item, dict) or _USER_OWNED_RE.search(str(item.get("prompt", ""))):
            return None
        choices = item.get("choices", [])
        if not isinstance(choices, list) or not 2 <= len(choices) <= 3:
            return None
        source = (str(original_input) + " " + " ".join(map(str, state["saved_preferences"].values()))).casefold()
        matches = [choice for choice in choices if len(str(choice.get("label", ""))) >= 4
                   and re.search(r"(?<!\w)" + re.escape(str(choice["label"]).casefold()) + r"(?!\w)", source)]
        if len(matches) != 1 or any(_USER_OWNED_RE.search(str(choice.get("label", "")) + " " +
                                                              str(choice.get("description", ""))) for choice in choices):
            return None
        question_id = str(item.get("id", ""))
        candidates[question_id] = str(matches[0]["id"])
        model_questions[question_id] = {
            "type": "choice", "instructions": "Which option is explicitly supported by the request or saved preferences?",
            "criteria": {str(choice["id"]): str(choice["label"]) for choice in choices},
        }
    response = _request(backend, model_questions, state)
    if diagnostics is not None:
        diagnostics.update(model_version=(str(response.get("model") or JEV_MODEL)[:80] if backend == "jev"
                                          else _kev_model_version()),
                           latency_ms=response.get("wall_ms"),
                           input_tokens=(response.get("usage") or {}).get("input_tokens"),
                           output_tokens=(response.get("usage") or {}).get("output_tokens"),
                           confidences=[(response["answers"].get(key) or {}).get("confidence")
                                        for key in candidates])
    for question_id, expected in candidates.items():
        selected = _confident_choice(response["answers"].get(question_id),
                                     set(model_questions[question_id]["criteria"]))
        if selected != expected:
            return None
    return candidates


def _kev_ready() -> bool:
    try:
        response = requests.get(KEV_URL + "/v1/models", headers={"Authorization": f"Bearer {_kev_key()}"}, timeout=2)
        response.raise_for_status()
        models = response.json().get("models", [])
        for model in models:
            if model.get("name") == KEV_MODEL and model.get("run") == KEV_RUN:
                if model.get("device") not in {"mps", "cuda"}:
                    raise RuntimeError("Kev is using the CPU; a supported GPU runtime is required")
                return True
        return False
    except (OSError, ValueError, requests.RequestException):
        return False


def setup_kev() -> float:
    """Install only after explicit selection, then prove a live model decision."""
    started = time.perf_counter()
    if not _supported_local_device():
        raise RuntimeError("Kev-0.8B setup needs Apple Silicon or a CUDA/ROCm GPU")
    root = _kev_root()
    root.mkdir(parents=True, exist_ok=True)
    key_path = root / "api_key"
    if not key_path.exists():
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(secrets.token_urlsafe(32))
    os.chmod(key_path, 0o600)
    if not _kev_ready():
        if shutil.disk_usage(Path.home()).free < 8 * 1024**3:
            raise RuntimeError("Kev-0.8B setup needs at least 8 GB free disk space")
        try:
            with socket.create_connection(("127.0.0.1", 8009), timeout=1):
                raise RuntimeError("Port 8009 is occupied by another service")
        except ConnectionRefusedError:
            pass
        uv = shutil.which("uv")
        python = _kev_python()
        try:
            if not python.exists():
                command = ([uv, "venv", str(python.parent.parent), "--python", sys.executable]
                           if uv else [sys.executable, "-m", "venv", str(python.parent.parent)])
                subprocess.run(command, check=True, timeout=120, capture_output=True, text=True)
            command = ([uv, "pip", "install", "--python", str(python), KEV_PACKAGE]
                       if uv else [str(python), "-m", "pip", "install", KEV_PACKAGE])
            subprocess.run(command, check=True, timeout=900, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError("Kev install failed: " + str(exc.stderr or exc.stdout or exc)[-500:]) from exc
        log = (root / "server.log").open("a", encoding="utf-8")
        try:
            process = subprocess.Popen(
                [str(python), "-m", "kev.serve", "--run", KEV_RUN, "--host", "127.0.0.1", "--port", "8009"],
                cwd=root, env={**os.environ, "KEV_API_KEY": _kev_key(), "PYTHONUNBUFFERED": "1",
                               "HF_HUB_DISABLE_XET": "1"},
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=os.name != "nt",
            )
        finally:
            log.close()
        deadline = time.monotonic() + 1800
        while time.monotonic() < deadline:
            try:
                if _kev_ready():
                    break
            except RuntimeError:
                process.terminate()
                raise
            if process.poll() is not None:
                raise RuntimeError("Kev exited during startup; see ~/.kyrozen/kev/server.log")
            time.sleep(2)
        else:
            raise RuntimeError("Kev did not become ready; see ~/.kyrozen/kev/server.log")
    result = _request("kev", {"smoke": {"type": "choice", "instructions": "Choose the matching word.",
                                               "criteria": {"ready": "ready", "missing": "missing"}}},
                      "The service is ready.", timeout=20)
    if result["answers"].get("smoke", {}).get("choice") != "ready":
        raise RuntimeError("Kev smoke decision failed")
    return round((time.perf_counter() - started) * 1000, 2)

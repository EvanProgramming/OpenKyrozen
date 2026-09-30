from __future__ import annotations

import re
from openkyrozen.agent.modes import _SENSITIVE_RE
import openkyrozen.routing.policy as system_one_policy
import openkyrozen.routing.models as routing_models
import openkyrozen.routing.choices as _routing_choices
import openkyrozen.routing.kev as _routing_kev
import openkyrozen.routing.transport as _routing_transport
import openkyrozen.routing.decision_assist as _routing_decision_assist

def route(backend: str, user_input: str) -> dict:
    """Ask independent routing questions together; never grant capabilities."""
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
    payload = _routing_decision_assist._screened_payload(str(user_input)[:4000], questions, private=False, backend=backend)
    if payload is None:
        raise ValueError("privacy_screen")
    response = _routing_transport._request(backend, payload["questions"], payload["state"])
    model_info = _routing_transport.jev_model_info() if backend == "jev" else {}
    policies = {"model": _routing_choices._policy("route_model", backend),
                "complexity": _routing_choices._policy("route_complexity", backend),
                "profile": _routing_choices._policy("route_profile", backend)}
    choices = {
        "model": _routing_choices._confident_choice(response["answers"].get("model"), {"simple", "reasoning"},
                                    policy=policies["model"]),
        "complexity": _routing_choices._confident_choice(response["answers"].get("complexity"),
                                         {"simple", "medium", "complex"}, policy=policies["complexity"]),
        "profile": _routing_choices._confident_choice(response["answers"].get("profile"),
                                      {"coder", "researcher"}, policy=policies["profile"]),
    }
    return {
        **choices,
        "model_version": (str(response.get("model") or routing_models.JEV_MODEL)[:80] if backend == "jev"
                          else _routing_kev._kev_model_version()),
        "model_release_date": model_info.get("release_date") if backend == "jev" else None,
        "latency_ms": response["wall_ms"],
        "input_tokens": (response.get("usage") or {}).get("input_tokens"),
        "output_tokens": (response.get("usage") or {}).get("output_tokens"),
        "confidences": {name: response["answers"].get(name, {}).get("confidence") for name in questions},
        "fallbacks": {name: (str(policies[name].get("fallback", "current_path"))
                              if value is None else "") for name, value in choices.items()},
        "policies": policies,
        "policy_version": system_one_policy.POLICY_VERSION,
    }


def implied_answers(backend: str, original_input: str, request: dict, preferences: dict,
                    diagnostics: dict | None = None) -> dict[str, str] | None:
    """Confirm only an option already named by the user or a saved preference."""
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
        if not isinstance(item, dict) or routing_models._USER_OWNED_RE.search(str(item.get("prompt", ""))):
            return None
        choices = item.get("choices", [])
        if not isinstance(choices, list) or not 2 <= len(choices) <= 3:
            return None
        source = (str(original_input) + " " + " ".join(map(str, state["saved_preferences"].values()))).casefold()
        matches = [choice for choice in choices if len(str(choice.get("label", ""))) >= 4
                   and re.search(r"(?<!\w)" + re.escape(str(choice["label"]).casefold()) + r"(?!\w)", source)]
        if len(matches) != 1 or any(routing_models._USER_OWNED_RE.search(str(choice.get("label", "")) + " " +
                                                              str(choice.get("description", ""))) for choice in choices):
            return None
        question_id = str(item.get("id", ""))
        candidates[question_id] = str(matches[0]["id"])
        model_questions[question_id] = {
            "type": "choice", "instructions": "Which option is explicitly supported by the request or saved preferences?",
            "criteria": {str(choice["id"]): str(choice["label"]) for choice in choices},
        }
    payload = _routing_decision_assist._screened_payload(state, model_questions, private=False, backend=backend)
    if payload is None:
        if diagnostics is not None:
            diagnostics["fallback_reason"] = "privacy_screen"
        return None
    response = _routing_transport._request(backend, payload["questions"], payload["state"])
    if diagnostics is not None:
        model_info = _routing_transport.jev_model_info() if backend == "jev" else {}
        diagnostics.update(model_version=(str(response.get("model") or routing_models.JEV_MODEL)[:80] if backend == "jev"
                                          else _routing_kev._kev_model_version()),
                           model_release_date=model_info.get("release_date") if backend == "jev" else None,
                           latency_ms=response.get("wall_ms"),
                           input_tokens=(response.get("usage") or {}).get("input_tokens"),
                           output_tokens=(response.get("usage") or {}).get("output_tokens"),
                           confidences=[(response["answers"].get(key) or {}).get("confidence")
                                        for key in candidates],
                           policy=_routing_choices._policy("clarification", backend),
                           policy_version=system_one_policy.POLICY_VERSION,
                           fallback_behavior=_routing_choices._policy("clarification", backend).get("fallback", "user"))
    for question_id, expected in candidates.items():
        selected = _routing_choices._confident_choice(response["answers"].get(question_id),
                                     set(model_questions[question_id]["criteria"]),
                                     policy=_routing_choices._policy("clarification", backend))
        if selected != expected:
            return None
    return candidates


from openkyrozen.routing.models import JEV_MODEL, KEV_MODEL, KEV_PACKAGE, KEV_RUN, KEV_URL, _JEV_URL, DECISION_ASSIST_BACKENDS, _DECISION_ASSIST_KEY, _PRIVATE_RE, _JEV_MODEL_CACHE, _JEV_MODEL_REFRESH_SECONDS, _TOOL_INSTRUCTION_RE, _USER_OWNED_RE

from openkyrozen.routing.transport import _config_path, jev_key, jev_model_info, save_jev_key, _request

from openkyrozen.routing.settings import decision_assist_state, set_decision_assist, revoke_decision_assist_consent

from openkyrozen.routing.decision_assist import _screened_payload, _assist_backend, decision_assist, assist_choice, rank_memory_candidates, review_tool_output

from openkyrozen.routing.choices import _policy, _quality_gate, _confident_choice, confident_choice

from openkyrozen.routing.kev import _kev_root, _kev_python, _kev_key, _kev_model_version, _supported_local_device, _kev_ready, setup_kev

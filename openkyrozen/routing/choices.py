from __future__ import annotations

import openkyrozen.routing.policy as system_one_policy

def _policy(kind: str, backend: str) -> dict[str, object]:
    policy = system_one_policy.load(backend, kind)
    # A failed calibration must not replace the conservative bootstrap gate.
    if policy.get("calibration_id") and not policy.get("validated"):
        policy = dict(system_one_policy.DEFAULT_POLICIES.get(
            kind, system_one_policy.DEFAULT_POLICIES["clarification"]))
        policy.update({"validated": False, "version": system_one_policy.POLICY_VERSION,
                       "calibration_failed": True})
    return policy


def _quality_gate(backend: str, kind: str) -> bool:
    policy = system_one_policy.load(backend, kind)
    # Decision Assist actions remain advisory until their own held-out
    # calibration has passed; no model judgment alone changes state.
    if kind in {"tool_output_review", "memory_relevance", "learning_evidence"}:
        return bool(policy.get("validated"))
    return True


def _confident_choice(answer: object, options: set[str], *, policy: dict[str, object] | None = None) -> str | None:
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
    policy = policy or system_one_policy.DEFAULT_POLICIES["clarification"]
    if not policy.get("validated"):
        return None
    try:
        confidence_threshold = float(policy.get("confidence", 0.8))
        probability_threshold = float(policy.get("probability", 0.85))
        margin_threshold = float(policy.get("margin", 0.0))
        sorted_probabilities = sorted((float(value) for value in probabilities.values()), reverse=True)
    except (TypeError, ValueError):
        return None
    margin = sorted_probabilities[0] - sorted_probabilities[1] if len(sorted_probabilities) > 1 else 0.0
    return choice if confidence >= confidence_threshold and float(probabilities[choice]) >= probability_threshold \
        and margin >= margin_threshold else None


def confident_choice(kind: str, answer: object, options: set[str], backend: str) -> str | None:
    """Apply the calibrated policy for one typed Choice answer."""
    return _confident_choice(answer, options, policy=_policy(kind, backend))

from __future__ import annotations



EVOLUTION_PROFILES = {"coder", "researcher"}


POSITIVE_FEEDBACK = ("that works", "it works", "worked", "fixed", "solved", "perfect", "great", "谢谢", "好了", "搞定")


NEGATIVE_FEEDBACK = ("not working", "still broken", "didn't work", "doesn't work", "wrong", "incorrect", "not fixed", "不对", "还不行", "没解决")


CAPSULE_PROTOCOL = "openkyrozen-experience-capsule-v1"


DEFAULT_CONSTITUTION = {
    "allowed_artifact_types": ["policy", "skill"], "allow_dynamic_tools": False,
    "allow_permission_expansion": False, "allow_harness_edits": False,
    "minimum_live_successes": 2, "require_shadow_replay": True,
}

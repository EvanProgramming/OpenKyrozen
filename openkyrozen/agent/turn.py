"""State shared by the phases of a single turn."""
from dataclasses import dataclass
from typing import Any

@dataclass
class TurnContext:
    user_input: str
    clear_tasks: bool = False
    profile: str | None = None
    memory_context: dict | None = None
    inspection_complete: bool = False
    auto_clarified: bool = False
    turn_prompt_total: int = 0
    turn_completion_total: int = 0
    complexity: Any = None
    fast_backend: Any = None
    final_answer: Any = None
    fix_workflow: Any = None
    interaction_mode: Any = None
    learned_context: Any = None
    learning_receipts: Any = None
    learning_run: Any = None
    pending_interaction_reply: Any = None
    protocol_error_message: Any = None
    response_text: Any = None
    tool_calls: Any = None
    tool_records: Any = None
    tool_results_text: Any = None
    turn_start: Any = None

@dataclass
class ExecutionContext:
    model: str = "deepseek-flash"
    capability_token: Any = None
    last_prompt_tokens: int = 0
    last_completion_tokens: int = 0
    provider: Any = None
    provider_config: Any = None
    child_run_id: str | None = None
    coordinator: Any = None


def scoped_turn(function):
    """Keep provider selection, authorization and usage local to a turn/context."""
    from functools import wraps
    import copy
    @wraps(function)
    def run(runtime, *args, **kwargs):
        token = runtime._turn_context.set(copy.copy(runtime.execution_context))
        try:
            return function(runtime, *args, **kwargs)
        finally:
            runtime._turn_context.reset(token)
    return run

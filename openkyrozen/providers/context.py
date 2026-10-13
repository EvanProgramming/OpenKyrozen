"""Provider-message projections used only by runtime context accounting."""
from __future__ import annotations

import json

from openkyrozen.providers.models import ModelResponse, ToolReply


def context_accounting_messages(messages):
    """Project typed history onto the small role/content shape accepted by ContextState."""
    context_messages = []
    for item in messages:
        if isinstance(item, dict):
            context_messages.append(item)
        elif isinstance(item, ModelResponse):
            content = {"text": item.text, "tool_calls": [
                {"id": call.id, "name": call.name, "arguments": call.arguments} for call in item.tool_calls],
                "finish_reason": item.finish_reason.value,
                "continuation": item.continuation.payload if item.continuation else None}
            context_messages.append({"role": "assistant", "content": json.dumps(content, ensure_ascii=False)})
        elif isinstance(item, ToolReply):
            context_messages.append({"role": "tool", "content": json.dumps({
                "call_id": item.call_id, "name": item.name, "is_error": item.is_error,
                "output": item.output}, ensure_ascii=False)})
    return context_messages

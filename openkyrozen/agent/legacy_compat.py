"""Lazy bindings for the explicitly supported legacy text helper methods."""
from __future__ import annotations

from openkyrozen.agent.presentation import _NativeResponseText, is_native_response, native_response_metadata


def _legacy_method(name):
    def call(self, *args, **kwargs):
        text = args[0] if args else kwargs.get("text")
        if isinstance(text, _NativeResponseText):
            if name in {"_collect_tool_calls", "_extract_json_objects"}:
                return []
            if name == "_collect_unwrapped_tool_calls":
                return [], False
            if name in {"parse_json_from_response", "_detect_unknown_action", "_legacy_plan_value"}:
                return None
            if name in {"_has_unsupported_action_protocol", "_stream_buffer_has_tool_prefix"}:
                return False
            if name == "_clean_stream_buffer":
                return text
        from openkyrozen.agent import protocol
        return getattr(protocol, name)(self, *args, **kwargs)
    call.__name__ = name
    return call


def _parse_model_response(self, text):
    if is_native_response(self, text):
        return native_response_metadata(text)
    return self.legacy_protocol.parse_response(text)


def _observe_model_response(self, text):
    if is_native_response(self, text):
        return native_response_metadata(text)
    from openkyrozen.agent.protocol import _observe_model_response as observe
    return observe(self, text)


_is_valid_action = _legacy_method('_is_valid_action')
_detect_unknown_action = _legacy_method('_detect_unknown_action')
_has_unsupported_action_protocol = _legacy_method('_has_unsupported_action_protocol')
parse_json_from_response = _legacy_method('parse_json_from_response')
_extract_json_objects = _legacy_method('_extract_json_objects')
_collect_unwrapped_tool_calls = _legacy_method('_collect_unwrapped_tool_calls')
_collect_tool_calls = _legacy_method('_collect_tool_calls')
_marker_line_prefix = _legacy_method('_marker_line_prefix')
_action_marker_is_protocol = _legacy_method('_action_marker_is_protocol')
_control_marker_is_protocol = _legacy_method('_control_marker_is_protocol')
_remove_protocol_headings = _legacy_method('_remove_protocol_headings')
_stream_buffer_has_tool_prefix = _legacy_method('_stream_buffer_has_tool_prefix')
_clean_stream_buffer = _legacy_method('_clean_stream_buffer')
_legacy_plan_value = _legacy_method('_legacy_plan_value')
_persist_interaction_control = _legacy_method('_persist_interaction_control')
_interaction_gate = _legacy_method('_interaction_gate')

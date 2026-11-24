"""Real SDK wire checks for the complete registry; no remote provider calls."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from openkyrozen.providers.config import ProviderConfig
from openkyrozen.providers.google import GoogleProvider, VertexProvider
from openkyrozen.providers.models import ProviderContractError, ResponseComplete, ResponseError
from openkyrozen.tools.catalog import builtin_metadata, builtin_names
from openkyrozen.tools.manifest import ToolRegistry


class GoogleToolSchemaTests(unittest.TestCase):
    def setUp(self):
        try:
            from google import genai
            from google.oauth2.credentials import Credentials
        except ImportError:
            self.skipTest("optional Google SDK is not installed")
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                outer.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                stream = ":streamGenerateContent" in self.path
                response = {"modelVersion": "fixture", "candidates": [{"finishReason": "STOP",
                    "content": {"role": "model", "parts": [{"text": "ready"}]}}]}
                payload = (("data: " + json.dumps(response) + "\n\n") if stream else json.dumps(response)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream" if stream else "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.providers = []
        for cls, name in ((GoogleProvider, "google"), (VertexProvider, "vertex")):
            provider = cls.__new__(cls)
            provider.config = ProviderConfig(provider=name, model_simple="fixture")
            provider._supports_retry_options = "retry_options" in genai.types.HttpOptions.model_fields
            identity = ({"api_key": "fixture"} if name == "google" else {
                "vertexai": True, "project": "fixture", "location": "global", "credentials": Credentials(token="fixture")})
            provider._client = genai.Client(**identity, http_options={
                "base_url": f"http://127.0.0.1:{self.server.server_port}", "api_version": "v1beta"})
            self.addCleanup(getattr(provider._client, "close", provider._client._api_client._httpx_client.close))
            self.providers.append(provider)

    def test_every_builtin_schema_survives_sync_and_stream_sdk_serialization(self):
        tools = [{"name": name, "description": name, "parameters": builtin_metadata(name)["input_schema"]}
                 for name in builtin_names()]
        for provider in self.providers:
            for stream in (False, True):
                with self.subTest(provider=provider.config.provider, stream=stream):
                    before = len(self.requests)
                    if stream:
                        events = list(provider.chat_response_stream([{"role": "user", "content": "Inspect tools"}], tools=tools))
                        self.assertFalse([e for e in events if isinstance(e, ResponseError)], events)
                        self.assertTrue(any(isinstance(e, ResponseComplete) for e in events), events)
                    else:
                        self.assertEqual(provider.chat_response([{"role": "user", "content": "Inspect tools"}], tools=tools).text, "ready")
                    self.assertEqual(len(self.requests), before + 1)
                    declarations = self.requests[-1]["tools"][0]["functionDeclarations"]
                    field = "parametersJsonSchema" if "parametersJsonSchema" in declarations[0] else "parameters_json_schema"
                    self.assertEqual(declarations, [{"name": item["name"], "description": item["description"],
                        field: item["parameters"]} for item in tools])
        schema = builtin_metadata("run_cmd")["input_schema"]
        self.assertIsNotNone(ToolRegistry._argument_error(schema, {"command": "true", "timeout_seconds": 0}))
        self.assertIsNone(ToolRegistry._argument_error(schema, {"command": "true", "timeout_seconds": 1}))

    def test_invalid_rich_schemas_stop_before_transport(self):
        invalid = ({"exclusiveMinimum": True}, {"multipleOf": 0}, {"oneOf": []},
                   {"allOf": [{"type": "unknown"}]}, {"uniqueItems": "yes"}, {"not": {"type": "string"}})
        for value in invalid:
            for provider in self.providers:
                with self.subTest(value=value, provider=provider.config.provider):
                    tools = [{"name": "lookup", "description": "lookup", "parameters": {
                        "type": "object", "properties": {"value": value}}}]
                    with self.assertRaises(ProviderContractError):
                        provider.chat_response([], tools=tools)
                    events = list(provider.chat_response_stream([], tools=tools))
                    self.assertTrue(any(isinstance(e, ResponseError) for e in events), events)
        self.assertFalse(self.requests)

    def test_existing_openapi_extensions_keep_the_typed_sdk_route(self):
        tools = [{"name": "lookup", "description": "lookup", "parameters": {"type": "object", "properties": {
            "when": {"type": "string", "format": "date-time", "nullable": True}}}}]
        for provider in self.providers:
            provider.chat_response([], tools=tools)
            declaration = self.requests[-1]["tools"][0]["functionDeclarations"][0]
            self.assertEqual(set(declaration), {"name", "description", "parameters"})
            when = declaration["parameters"]["properties"]["when"]
            self.assertIs(when["nullable"], True)
            self.assertEqual(when["format"], "date-time")

    def test_sdk_without_json_schema_support_stops_before_transport(self):
        from google.genai import types
        with patch.object(types.FunctionDeclaration, "model_fields", {}):
            with self.assertRaises(ProviderContractError):
                self.providers[0].chat_response([], tools=[{"name": "lookup", "description": "lookup",
                    "parameters": {"type": "object"}}])
        self.assertFalse(self.requests)

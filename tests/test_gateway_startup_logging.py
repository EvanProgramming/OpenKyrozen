"""The real gateway harness must consume startup output without backpressure."""
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_durable_tasks_gateway as gateway_tests


class GatewayStartupLoggingTests(unittest.TestCase):
    def test_configured_startup_budget_allows_slow_imports(self):
        process = Mock()
        process.poll.return_value = None
        helper = gateway_tests.DurableTaskGatewayTests()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch('test_durable_tasks_gateway.subprocess.Popen', return_value=process) as launch, \
                 patch('test_durable_tasks_gateway.time.monotonic', side_effect=[0, 21]), \
                 patch.object(helper, '_request', return_value={'status':'ok'}):
                actual, _ = helper._start_server(root, root/'state.db', root/'skills', startup_timeout=60)
            self.assertIs(actual, process)
            self.assertEqual(launch.call_args.args[0][1:3], ["-X", "faulthandler"])
            process.terminate.assert_not_called()

    def test_soak_failure_preserves_gateway_exit_before_cleanup(self):
        from scripts import production_soak

        process = Mock(pid=123)
        process.poll.return_value = 42
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(production_soak.os.environ),
            patch.object(production_soak, "_capture_process_tree", return_value={}),
            patch.object(production_soak, "_process_snapshot", return_value={"rss_bytes": 1, "open_handles": 1}),
            patch.object(gateway_tests.DurableTaskGatewayTests, "_start_server",
                         return_value=(process, "http://127.0.0.1:1")),
            patch.object(gateway_tests.DurableTaskGatewayTests, "_request", side_effect=ConnectionRefusedError),
            patch.object(gateway_tests.DurableTaskGatewayTests, "_stop_server",
                         side_effect=lambda _: setattr(process.poll, "return_value", -15)),
        ):
            with self.assertRaises(ConnectionRefusedError) as failure:
                production_soak.run(20, Path(directory) / "logs")
            self.assertEqual(failure.exception.soak_report["gateway_at_failure"],
                             {"pid": 123, "returncode": 42})

    def test_large_startup_output_does_not_block_readiness(self):
        original_popen = subprocess.Popen
        code = '''
import http.server, sys
sys.stdout.write('x' * 1024 * 1024 + '\\nstartup-marker\\n')
sys.stdout.flush()
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')
http.server.HTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()
'''
        def noisy_server(command, **kwargs):
            port = command[command.index('--port') + 1]
            return original_popen([sys.executable, '-c', code, port], **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = gateway_tests.DurableTaskGatewayTests()
            log_path = root / 'gateway.log'
            started = time.monotonic()
            def ready_after_output(_url, path):
                self.assertEqual(path, '/api/health')
                if 'startup-marker' not in log_path.read_text(encoding='utf-8'):
                    raise OSError('startup output is incomplete')
                return {'status': 'ok'}
            with patch('test_durable_tasks_gateway.subprocess.Popen', side_effect=noisy_server), \
                 patch.object(helper, '_request', side_effect=ready_after_output):
                process, _url = helper._start_server(
                    root, root / 'state.db', root / 'skills', log_path=log_path)
            try:
                self.assertLess(time.monotonic() - started, 5)
                self.assertIsNone(process.poll())
                self.assertIn('startup-marker', log_path.read_text(encoding='utf-8'))
            finally:
                helper._stop_server(process)

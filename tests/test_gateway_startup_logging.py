"""The real gateway harness must consume startup output without backpressure."""
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import test_durable_tasks_gateway as gateway_tests


class GatewayStartupLoggingTests(unittest.TestCase):
    def test_configured_startup_budget_allows_slow_imports(self):
        from unittest.mock import Mock
        process = Mock()
        process.poll.return_value = None
        helper = gateway_tests.DurableTaskGatewayTests()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch('test_durable_tasks_gateway.subprocess.Popen', return_value=process), \
                 patch('test_durable_tasks_gateway.time.monotonic', side_effect=[0, 21]), \
                 patch.object(helper, '_request', return_value={'status':'ok'}):
                actual, _ = helper._start_server(root, root/'state.db', root/'skills', startup_timeout=60)
            self.assertIs(actual, process)
            process.terminate.assert_not_called()

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

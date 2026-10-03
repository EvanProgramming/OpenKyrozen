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
            started = time.monotonic()
            with patch('test_durable_tasks_gateway.subprocess.Popen', side_effect=noisy_server):
                process, url = helper._start_server(root, root / 'state.db', root / 'skills')
            try:
                self.assertLess(time.monotonic() - started, 5)
                self.assertEqual(helper._request(url, '/api/health')['status'], 'ok')
                self.assertIn('startup-marker', (root / 'gateway.log').read_text())
            finally:
                helper._stop_server(process)

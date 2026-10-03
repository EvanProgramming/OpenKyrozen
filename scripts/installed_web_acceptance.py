#!/usr/bin/env python3
"""Exercise installed web UI with a deterministic local provider and real HTTP/SSE.

The --python interpreter must belong to an installed candidate outside the checkout.
Provider initialization is injected; credentials and production state are not used.
"""
from pathlib import Path
import sys

def serve():
    import inspect, json, os, importlib.metadata
    from pathlib import Path
    import uvicorn
    from openkyrozen.app.bootstrap import build_application, build_memory
    from openkyrozen.interfaces.web.app import create_app
    from openkyrozen.providers.config import ProviderConfig
    from openkyrozen.providers.base import LLMProvider
    from openkyrozen.tools import ToolAdapters

    class Fixture(LLMProvider):

        def chat(self, messages, model=None):
            text = next((str(m['content']) for m in reversed(messages) if m['role'] == 'user'), '')
            return ('UI_B_OK' if 'UI_B' in text else 'UI_A_OK', {'prompt_tokens': 1, 'completion_tokens': 1})
    root = Path(os.environ['KYROZEN_WORKSPACE_ROOT'])
    app = build_application(surface='web', memory=build_memory(Path(os.environ['KYROZEN_DB_PATH'])), tools=ToolAdapters(root))
    runtime = app.runtime
    config = ProviderConfig(provider='ollama', api_key='', model_simple='fixture', model_complex='fixture', base_url='http://127.0.0.1:9/v1')
    runtime._provider_config = config
    runtime.llm_provider = Fixture(config)
    runtime.configure_launch_context(project_path=root)
    runtime.get_provider = lambda _: Fixture(config)
    runtime._prompt_and_init_deepseek = lambda **_: None
    runtime._SELF_LEARNING_FLAGS = {key: False for key in runtime._SELF_LEARNING_FLAGS}
    runtime._touch_detached_learning_heartbeat = lambda: None
    distribution = importlib.metadata.distribution('openkyrozen')
    print(json.dumps({'installed_source': inspect.getsourcefile(build_application), 'version': distribution.version, 'package_source': json.loads(distribution.read_text('direct_url.json') or '{}')}), flush=True)
    uvicorn.run(create_app(app), host='127.0.0.1', port=int(os.environ['AUDIT_PORT']), log_level='info')

def acceptance(python, output):
    import json, os, socket, subprocess, tempfile, time, urllib.request
    from playwright.sync_api import sync_playwright, expect
    with tempfile.TemporaryDirectory(prefix='openkyrozen-installed-ui-') as directory:
        root = Path(directory)
        home = root / 'home'
        home.mkdir()
        project = root / 'project'
        project.mkdir()
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
        env = os.environ.copy()
        for name in list(env):
            if name.endswith('_API_KEY') or name in ('PYTHONPATH', 'KYROZEN_AGENT_CONFIG', 'KYROZEN_DISABLE_VECTOR_INDEX'):
                env.pop(name, None)
        env.update(HOME=str(home), KYROZEN_DB_PATH=str(root / 'state.sqlite3'), KYROZEN_WORKSPACE_ROOT=str(project), KYROZEN_SERVER_TOKEN='synthetic-ui-token', AUDIT_PORT=str(port), KYROZEN_SKILLS_DIR=str(root / 'skills'))
        url = f'http://127.0.0.1:{port}'
        log = (root / 'server.log').open('w')

        def start():
            p = subprocess.Popen([str(python), '-I', str(Path(__file__).resolve()), '--serve'], cwd=project, env=env, stdout=log, stderr=log)
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                try:
                    urllib.request.urlopen(url, timeout=1)
                    return p
                except OSError:
                    if p.poll() is not None:
                        break
                    time.sleep(0.2)
            log.flush()
            output.with_suffix('.server.log').write_text((root / 'server.log').read_text())
            p.terminate() if p.poll() is None else None
            raise AssertionError('Installed UI server startup failed: ' + (root / 'server.log').read_text()[-3000:])
        p = start()
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(url)
                expect(page.locator('#auth-controls')).to_be_visible()
                page.locator('#server-token').fill('incorrect')
                page.locator('#authenticate').click()
                expect(page.locator('#auth-status')).to_contain_text('Invalid server token')
                page.locator('#server-token').fill('synthetic-ui-token')
                page.locator('#authenticate').click()
                expect(page.locator('#auth-controls')).to_be_hidden()
                page.locator('#user-input').fill('UI_A')
                page.locator('#send-btn').click()
                expect(page.locator('#chat')).to_contain_text('UI_A_OK', timeout=60000)
                expect(page.locator('#send-btn')).to_be_enabled(timeout=60000)
                page.locator('#new-session').click()
                expect(page.locator('#chat')).not_to_contain_text('UI_A_OK')
                page.locator('#user-input').fill('UI_B')
                page.locator('#send-btn').click()
                expect(page.locator('#chat')).to_contain_text('UI_B_OK', timeout=60000)
                expect(page.locator('#send-btn')).to_be_enabled(timeout=60000)
                page.reload()
                expect(page.locator('#chat')).to_contain_text('UI_B_OK', timeout=30000)
                p.terminate()
                p.wait(15)
                p = start()
                page.reload()
                expect(page.locator('#auth-controls')).to_be_visible()
                page.locator('#server-token').fill('synthetic-ui-token')
                page.locator('#authenticate').click()
                expect(page.locator('#auth-controls')).to_be_hidden()
                expect(page.locator('#chat')).to_contain_text('UI_B_OK', timeout=30000)
                expect(page.locator('#chat')).not_to_contain_text('UI_A_OK')
                page.screenshot(path=str(output.with_suffix('.png')), full_page=True)
                browser.close()
            log.flush()
            source = (root / 'server.log').read_text().splitlines()[0]
            assert '/site-packages/' in source, source
            from audit_identity import audit_identity
            result = {'identity': audit_identity(), 'status': 'PASS', 'installed_source': json.loads(source), 'checks': ['authentication', 'invalid-token', 'streaming-render', 'session-isolation', 'reload-reconnect', 'restart-recovery'], 'provider': 'deterministic local fixture; no external requests'}
            output.write_text(json.dumps(result, indent=2))
            print(json.dumps(result))
        finally:
            p.terminate()
            p.wait(15)
            log.flush()
            output.with_suffix('.server.log').write_text((root / 'server.log').read_text())
            log.close()
if __name__ == '__main__':
    if sys.argv[1:] == ['--serve']:
        serve()
    else:
        import argparse, json
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--python', type=Path, required=True)
        parser.add_argument('--output', type=Path, required=True)
        args = parser.parse_args()
        try:
            acceptance(args.python.absolute(), args.output)
        except Exception as exc:
            args.output.write_text(json.dumps({'status': 'FAIL', 'error': str(exc)}, indent=2))
            raise

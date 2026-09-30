#!/usr/bin/env python3
"""Exercise a rebuilt Bubble Tea binary against the real backend in a PTY.

Only provider responses are deterministic. Onboarding, JSONL, sessions, tool
approvals, Git effects and terminal resize handling use production code.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import select
import signal
import struct
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def backend_fixture():
    sys.path.insert(0, str(ROOT))
    from openkyrozen.app.bootstrap import build_application
    from openkyrozen.interfaces.tui.backend import Backend
    from openkyrozen.providers import LLMProvider, ProviderConfig

    class Provider(LLMProvider):
        def chat(self, messages, model=None):
            latest = next(item['content'] for item in reversed(messages) if item['role'] == 'user')
            if 'The tools returned:' in latest:
                text = 'Approval workflow finished.'
            elif 'approval request' in latest:
                text = 'Action: {"action":"git_reset","args":"--hard HEAD"}'
            else:
                text = 'Fixture reply: ' + latest
            return text, None

    application = build_application(surface='tui')
    runtime = application.runtime
    config = ProviderConfig(provider='deepseek', api_key='fixture-only')
    runtime.detect_provider = lambda: config
    runtime.get_fallback_provider = lambda _config: Provider(_config)
    runtime._classify_complexity = lambda _text: 'simple'
    runtime._ensure_detached_learning_worker = lambda: True
    runtime._touch_detached_learning_heartbeat = lambda: None
    runtime.dispatch_learning_cycle = lambda **kwargs: []
    backend = Backend(application)
    original_emit = backend.emit
    log = Path(os.environ['KYROZEN_TUI_ACCEPTANCE_EVENTS'])
    lock = __import__('threading').Lock()

    def record(item):
        with lock, log.open('a') as stream:
            stream.write(json.dumps(item) + '\n')

    def emit(event, request_id=None, **payload):
        record({'event': event, 'request_id': request_id, **payload})
        original_emit(event, request_id, **payload)

    backend.emit = emit
    try:
        for raw in sys.stdin.buffer:
            payload, error = backend.validate(json.loads(raw))
            if error:
                raise AssertionError(error)
            record({'input': payload})
            try:
                backend.dispatch(payload)
            except Exception:
                import traceback
                record({"fixture_error": traceback.format_exc()})
                raise
            if backend._stopping.is_set():
                break
    finally:
        backend.stop()
        application.close()


def acceptance(binary: Path):
    import fcntl
    import pty
    import termios

    with tempfile.TemporaryDirectory(prefix='openkyrozen-tui-acceptance-') as directory:
        root = Path(directory)
        project, home = root / 'project', root / 'home'
        project.mkdir()
        home.mkdir()
        tracked = project / 'tracked.txt'
        tracked.write_text('committed\n')
        for args in (['init', '-q'], ['add', 'tracked.txt'],
                     ['-c', 'user.name=Acceptance', '-c', 'user.email=fixture@example.invalid',
                      '-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture']):
            subprocess.run(['git', '-C', str(project), *args], check=True, capture_output=True)
        events_file = root / 'events.jsonl'
        command = root / 'backend'
        command.write_text('#!/bin/sh\nexec ' + __import__('shlex').quote(sys.executable) + ' ' +
                           __import__('shlex').quote(str(Path(__file__).resolve())) + ' --backend\n')
        command.chmod(0o700)
        env = os.environ.copy()
        env.update(HOME=str(home), KYROZEN_DB_PATH=str(root / 'state.sqlite3'),
                   KYROZEN_DISABLE_VECTOR_INDEX='1', KYROZEN_REDUCED_MOTION='1',
                   KYROZEN_TUI_CAPABILITIES='full', KYROZEN_APPROVAL_MODE='dangerous',
                   KYROZEN_BACKEND_COMMAND=str(command), KYROZEN_TUI_ACCEPTANCE_EVENTS=str(events_file),
                   TERM='xterm-256color')
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 35, 120, 0, 0))
        process = subprocess.Popen([str(binary), 'onboarding', '--project', str(project)],
                                   stdin=slave, stdout=slave, stderr=slave, env=env, cwd=ROOT,
                                   start_new_session=True)
        os.close(slave)
        terminal = bytearray()

        def drain(timeout=0.1):
            if select.select([master], [], [], timeout)[0]:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    return
                terminal.extend(data)
                if b'\x1b[6n' in data:
                    os.write(master, b'\x1b[1;1R')
                if b'\x1b]10;?' in data:
                    os.write(master, b'\x1b]10;rgb:ffff/ffff/ffff\x1b\\')
                if b'\x1b]11;?' in data:
                    os.write(master, b'\x1b]11;rgb:0000/0000/0000\x1b\\')

        def events():
            if not events_file.exists():
                return []
            return [json.loads(line) for line in events_file.read_text().splitlines() if line]

        def wait(predicate, label, timeout=30):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                drain()
                if predicate(events()):
                    # Let Bubble Tea consume the event before the next key.
                    until = time.monotonic() + 0.3
                    while time.monotonic() < until:
                        drain()
                    print("PASS: " + label, flush=True)
                    return
                if process.poll() is not None:
                    break
            raise AssertionError(label + '\n' + terminal.decode(errors='replace')[-3000:] + '\n' + str(events()[-5:]))

        def send(data):
            os.write(master, data)

        def prompt(kind):
            return lambda rows: any(r.get('event') == 'prompt' and r.get('kind') == kind for r in rows)

        def response_count(rows):
            return sum(r.get('event') == 'response' for r in rows)

        def submit(text):
            count = response_count(events())
            send(text.encode() + b'\r')
            wait(lambda rows: response_count(rows) > count, 'chat response')
            wait(lambda rows: rows[-1].get('event') != 'status' or not rows[-1].get('busy'), 'turn completion')

        try:
            wait(prompt('onboarding'), 'onboarding')
            send(b'\r')
            wait(prompt('provider'), 'provider selection')
            send(b'\r')
            wait(prompt('self_learning'), 'learning selection')
            send(b'r')
            wait(lambda rows: any(r.get('event') == 'onboarding_complete' for r in rows), 'setup completed')
            submit('first conversation')
            first_session = next(r['session_id'] for r in events() if r.get('event') == 'ready' and r.get('session_id'))
            ready_count = sum(r.get('event') == 'ready' for r in events())
            send(b'\x0e')  # Ctrl+N creates a chat through the UI.
            wait(lambda rows: sum(r.get('event') == 'ready' for r in rows) > ready_count, 'new chat')
            submit('second conversation')
            send(b'\x02')  # Ctrl+B focuses navigation.
            drain(0.3)
            send(b'\x1b[B\r')  # Select the older chat.
            wait(lambda rows: any(r.get('input', {}).get('command') == 'navigate' and
                                 r['input'].get('session_id') == first_session for r in rows), 'chat switching')
            send(b'\x02')
            submit('/mode agent')
            for decision in (False, True):
                tracked.write_text('uncommitted\n')
                prompt_count = sum(r.get('event') == 'prompt' and r.get('kind') == 'approval' for r in events())
                count = response_count(events())
                send(b'approval request\r')
                wait(lambda rows: sum(r.get('event') == 'prompt' and r.get('kind') == 'approval' for r in rows) > prompt_count,
                     'approval prompt')
                send(b'y' if decision else b'n')
                wait(lambda rows: response_count(rows) > count, 'approval result')
                assert tracked.read_text() == ('committed\n' if decision else 'uncommitted\n')
            for width, height in ((60, 20), (160, 45), (80, 24)):
                previous = len(terminal)
                fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', height, width, 0, 0))
                os.kill(process.pid, signal.SIGWINCH)
                wait(lambda rows: len(terminal) > previous, f'resize {width}x{height}')
            plain = re.sub(rb'\x1b\[[0-?]*[ -/]*[@-~]', b'', terminal).decode(errors='replace')
            assert 'APPROVAL REQUIRED' in plain and 'Choose a provider' in plain
            assert 'first conversation' in plain and 'second conversation' in plain
            send(b'\x03')
            process.wait(timeout=10)
            assert process.returncode == 0
            print('TUI acceptance passed: onboarding, chat, session switching, denied/approved Git reset, and 3 terminal sizes.')
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', nargs='?', type=Path)
    parser.add_argument('--backend', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.backend:
        backend_fixture()
    elif args.binary:
        acceptance(args.binary.resolve())
    else:
        parser.error('provide the rebuilt TUI binary')

#!/usr/bin/env python3
"""Trace the unchanged cancellation unittest; passing samples do not close R260."""
from __future__ import annotations

import argparse
from contextlib import closing
import faulthandler
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
TEST = 'test_cancel_during_provider_call_prevents_late_write'
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')


# Exact dependency versions from job 125106035551, including its four already-satisfied packages.
HISTORICAL_REQUIREMENTS = """
aiohappyeyeballs==2.7.1 aiohttp==3.14.5 aiohttp-retry==2.9.1 aiosignal==1.4.0 annotated-doc==0.0.5
annotated-types==0.8.0 anthropic==2.3.0 anyio==4.16.0 attrs==26.1.0 azure-core==1.41.0
azure-identity==1.27.0 bcrypt==5.0.0 beautifulsoup4==4.15.0 boto3==1.43.123 botocore==1.43.123
build==1.6.1 certifi==2026.10.23 cffi==2.1.1 charset-normalizer==3.5.2 chromadb==1.5.9
cryptography==50.0.2 distro==1.9.0 docstring-parser==0.18.0 durationpy==0.11 fastapi==0.162.0
flatbuffers==25.12.19 frozenlist==1.8.0 fsspec==2026.9.0 google-auth==2.62.0 google-genai==2.34.0
googleapis-common-protos==1.76.0 googlesearch-python==1.3.0 graphifyy==0.9.64 greenlet==3.5.6
grpcio==1.85.1 h11==0.16.0 hf-xet==1.8.0 httpcore==1.0.9 httpcore2==2.16.0 httptools==0.9.0
httpx==0.28.1 httpx2==2.16.0 huggingface-hub==2.4.0 idna==3.20 importlib-resources==7.1.0
jiter==0.18.0 jmespath==1.1.0 jsonschema==4.26.0 jsonschema-specifications==2025.9.1
kubernetes==37.0.1 markdown-it-py==4.2.0 mdurl==0.1.2 mmh3==5.3.1 msal==1.39.0
msal-extensions==1.3.1 multidict==7.2.1 networkx==3.7 numpy==2.5.4 oauthlib==4.0.1
onnxruntime==1.31.1 openai==3.42.1 opentelemetry-api==1.45.2
opentelemetry-exporter-otlp-common==0.66b2 opentelemetry-exporter-otlp-proto-common==1.45.2
opentelemetry-exporter-otlp-proto-grpc==1.45.2 opentelemetry-proto==1.45.2 opentelemetry-sdk==1.45.2
opentelemetry-semantic-conventions==0.66b2 orjson==3.13.0 overrides==7.7.0 perplexityai==0.46.1
playwright==1.64.1 propcache==0.5.4 protobuf==7.36.2 pyasn1==0.6.5 pyasn1-modules==0.4.2
pybase64==1.5.1 pycparser==3.12 pydantic==2.14.2 pydantic-core==2.50.2 pydantic-settings==2.16.2
pyee==14.0.0 pygments==2.21.0 pyjwt==2.15.1 pypika==0.51.1 pyproject-hooks==1.3.3
python-dateutil==2.9.0.post0 python-dotenv==1.2.4 pyyaml==6.0.3 rapidfuzz==3.14.6
referencing==0.37.0 requests==2.34.2 requests-oauthlib==2.0.0 rich==15.0.0 rpds-py==2026.9.1
s3transfer==0.19.3 send2trash==2.1.0 shellingham==1.5.4 six==1.17.0 sniffio==1.3.1 soupsieve==3.0.4
starlette==1.10.0 tenacity==9.1.4 tokenizers==0.23.3 tqdm==4.70.1 tree-sitter==0.25.2
tree-sitter-bash==0.25.1 tree-sitter-c==0.24.2 tree-sitter-c-sharp==0.23.5 tree-sitter-cpp==0.23.4
tree-sitter-elixir==0.3.5 tree-sitter-fortran==0.6.0 tree-sitter-go==0.25.0
tree-sitter-groovy==0.1.2 tree-sitter-java==0.23.5 tree-sitter-javascript==0.25.0
tree-sitter-json==0.24.8 tree-sitter-julia==0.23.1 tree-sitter-kotlin==1.1.0 tree-sitter-lua==0.5.0
tree-sitter-objc==3.0.2 tree-sitter-php==0.24.1 tree-sitter-powershell==0.26.4
tree-sitter-python==0.25.0 tree-sitter-ruby==0.23.1 tree-sitter-rust==0.24.2
tree-sitter-scala==0.26.4 tree-sitter-swift==0.7.4 tree-sitter-typescript==0.23.2
tree-sitter-verilog==1.0.3 tree-sitter-zig==1.1.2 truststore==0.10.5 typer==0.27.3
typing-extensions==4.16.0 typing-inspection==0.4.4 urllib3==2.8.0 uvicorn==0.54.0 watchfiles==1.3.0
websocket-client==1.9.2 websockets==17.4 yarl==1.25.1
packaging==26.3 colorama==0.4.6 click==8.5.0 filelock==4.2.4
"""


def isolated_environment(home):
    prefixes = ('KYROZEN_', 'OPENAI_', 'ANTHROPIC_', 'DEEPSEEK_', 'AZURE_', 'GEMINI_',
                'GOOGLE_', 'AWS_', 'BEDROCK_', 'OLLAMA_', 'PERPLEXITY_', 'MISTRAL_',
                'GROQ_', 'COHERE_', 'XAI_', 'TOGETHER_', 'OPENROUTER_', 'FIREWORKS_')
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(prefixes) and not any(term in key.upper() for term in
               ('API_KEY', 'ACCESS_KEY', 'SECRET', 'TOKEN', 'PASSWORD', 'CREDENTIAL', 'ENDPOINT', 'BASE_URL'))}
    workspace, skills = home / '.kyrozen' / 'workspace', home / 'skills'
    workspace.mkdir(parents=True, exist_ok=True)
    skills.mkdir(exist_ok=True)
    env.update(HOME=str(home), USERPROFILE=str(home), KYROZEN_WORKSPACE_ROOT=str(workspace),
               KYROZEN_DB_PATH=str(home / 'runtime.sqlite3'), KYROZEN_SKILLS_PATH=str(skills),
               KYROZEN_SKILLS_DIR=str(skills))
    return env


def historical_environment():
    mismatches = {}
    for requirement in HISTORICAL_REQUIREMENTS.split():
        name, expected = requirement.split('==')
        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError:
            installed = None
        if installed != expected:
            mismatches[name] = {'expected': expected, 'installed': installed}
    runtime, system = platform.python_version(), platform.system()
    return {'status': 'MATCH' if runtime == '3.12.10' and system == 'Windows' and not mismatches
            else 'INFRASTRUCTURE_FAILURE', 'python': runtime, 'system': system,
            'expected_python': '3.12.10', 'dependency_mismatches': mismatches,
            'requirements_sha256': hashlib.sha256(HISTORICAL_REQUIREMENTS.encode()).hexdigest(),
            'source_job': 'https://github.com/EvanProgramming/OpenKyrozen/actions/runs/41392269312/job/125106035551',
            'qualification': 'The original runner image and host scheduling are not reproduced by matching Python and packages.'}


_CPU_LOAD = '''
import json, math, sys, time
from pathlib import Path
ready, stop, result = map(Path, sys.argv[1:4])
started, cpu = time.monotonic(), time.process_time()
deadline = started + float(sys.argv[4])
ready.write_text('ready')
value = 0.5
while not stop.exists() and time.monotonic() < deadline:
    for _ in range(20000):
        value = math.sin(value) + 0.5
result.write_text(json.dumps({'cpu_seconds': time.process_time()-cpu,
    'wall_seconds': time.monotonic()-started,
    'stopped_by': 'parent' if stop.exists() else 'deadline'}))
'''


class LoadControl:
    """Bounded diagnostic load; never writes fixture data or changes timeouts."""

    def __init__(self, control, workers, case, trace):
        self.control, self.workers, self.case, self.trace = control, workers, case, trace
        self.stopped, self.sqlite_ready = threading.Event(), threading.Event()
        self.cpu = []
        self.sqlite_thread = None
        self.acquired_transactions = 0
        self.sqlite_fatal_error = None
        self.closed = False

    def start(self):
        if self.control in {'sqlite', 'combined'}:
            self.sqlite_thread = threading.Thread(target=self._sqlite_load,
                name='diagnostic-sqlite-control', daemon=True)
            self.sqlite_thread.start()
            if not self.sqlite_ready.wait(3):
                raise RuntimeError('SQLite load control did not acquire its first bounded transaction')
        if self.control in {'cpu', 'combined'}:
            for index in range(self.workers):
                prefix = self.trace.output / f'cpu-{index}-{uuid.uuid4().hex}'
                ready, stop, result = (Path(str(prefix) + suffix) for suffix in ('.ready', '.stop', '.json'))
                process = subprocess.Popen([sys.executable, '-c', _CPU_LOAD,
                    str(ready), str(stop), str(result), '30'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.cpu.append((process, stop, result))
                deadline = time.monotonic() + 3
                while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(.01)
                if not ready.exists():
                    raise RuntimeError('CPU load control did not become ready')
        self.trace.record('load.started', control=self.control, cpu_pids=[item[0].pid for item in self.cpu])

    def _sqlite_load(self):
        try:
            with closing(sqlite3.connect(self.case.root / 'state.sqlite3', timeout=.05,
                                         isolation_level=None)) as connection:
                while not self.stopped.is_set():
                    acquired = False
                    started = time.monotonic_ns()
                    try:
                        connection.execute('BEGIN IMMEDIATE')
                        acquired = True
                        self.acquired_transactions += 1
                        self.trace.record('sqlite.lock_acquired', wait_ns=time.monotonic_ns()-started)
                        self.sqlite_ready.set()
                        self.stopped.wait(.025)
                    except sqlite3.Error as error:
                        self.trace.record('sqlite.lock_error', error_type=type(error).__name__,
                                          wait_ns=time.monotonic_ns()-started)
                    finally:
                        if acquired:
                            connection.execute('ROLLBACK')
                            self.trace.record('sqlite.lock_released')
                    self.stopped.wait(.01)
        except sqlite3.Error as error:
            self.sqlite_fatal_error = type(error).__name__
            self.trace.record('sqlite.control_error', error_type=type(error).__name__)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.stopped.set()
        cpu = []
        for process, stop, result in self.cpu:
            stop.touch()
            forced = False
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                forced = True
                process.kill()
                process.wait(timeout=3)
            metrics = json.loads(result.read_text()) if result.is_file() else {}
            cpu.append({'pid': process.pid, 'returncode': process.returncode,
                        'forced_cleanup': forced, **metrics})
        if self.sqlite_thread is not None:
            self.sqlite_thread.join(timeout=2)
        sqlite_stopped = self.sqlite_thread is None or not self.sqlite_thread.is_alive()
        write_json(self.trace.output / 'load-control.json', {'control': self.control, 'cpu': cpu,
            'sqlite': {'acquired_transactions': self.acquired_transactions, 'thread_stopped': sqlite_stopped,
                       'fatal_error': self.sqlite_fatal_error}})
        self.trace.record('load.stopped', control=self.control, cpu=cpu, sqlite_thread_stopped=sqlite_stopped)
        if (not sqlite_stopped or self.sqlite_fatal_error
                or any(item['forced_cleanup'] or item['returncode'] != 0
                       or item.get('stopped_by') != 'parent' for item in cpu)):
            raise RuntimeError('Load-control cleanup did not complete normally; inspect diagnostic evidence')


def stop_diagnostic_tree(process):
    """Stop the still-owned diagnostic group before its ancestry disappears."""
    attempt = {'parent_pid': process.pid, 'descendant_cleanup_certified': False}
    if os.name == 'nt':
        result = subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                                capture_output=True, timeout=5, check=False)
        attempt.update(method='taskkill_tree', command_returncode=result.returncode)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
            attempt.update(method='owned_process_group', signal_sent=True)
        except ProcessLookupError:
            attempt.update(method='owned_process_group', signal_sent=False)
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)
    attempt['parent_returncode'] = process.returncode
    return attempt


class Trace:
    def __init__(self, output):
        self.output = output
        self.stream = (output / 'events.jsonl').open('w', encoding='utf-8')
        self.lock = threading.Lock()
        self.case = None
        self.coordinator = None
        self.active = True

    def record(self, event, **fields):
        with self.lock:
            if not self.active:
                return
            self.stream.write(json.dumps({'event': event, 'monotonic_ns': time.monotonic_ns(),
                'thread_id': threading.get_ident(), 'thread_cpu_ns': time.thread_time_ns(), **fields}) + '\n')
            self.stream.flush()

    def profile(self, frame, event, arg):
        if event in {'c_call', 'c_return', 'c_exception'}:
            if isinstance(getattr(arg, '__self__', None), sqlite3.Connection):
                self.record('sqlite.' + arg.__name__ + '.' + event,
                            caller=frame.f_code.co_qualname)
            elif arg is sqlite3.connect:
                self.record('sqlite.connect.' + event, caller=frame.f_code.co_qualname)
            return
        if event not in {'call', 'return'}:
            return
        module = frame.f_globals.get('__name__', '')
        name = frame.f_code.co_name
        label = None
        if module == 'test_delegation' and name == 'chat' and 'SlowProvider' in frame.f_code.co_qualname:
            label = 'provider'
        elif module == 'openkyrozen.agent.delegation':
            label = {'spawn': 'dispatch', 'cancel': 'cancel', '_work': 'worker',
                     '_publish': 'persist', '_invoke': 'invoke'}.get(name)
            if label:
                self.coordinator = frame.f_locals['self']
        elif module == 'threading' and name == 'set':
            caller = frame.f_back
            if caller and caller.f_code.co_name == TEST:
                if frame.f_locals['self'] is caller.f_locals.get('release'):
                    label = 'release'
        elif module == 'concurrent.futures._base' and name == 'result':
            label = 'future_wait'
        elif module == 'openkyrozen.app.container' and name == 'close':
            label = 'application_close'
        elif module == 'openkyrozen.providers.retry':
            label = {'_next_event': 'provider_wait', 'check': 'provider_check',
                     'bounded_call': 'provider_bounded_call'}.get(name)
        elif module == 'openkyrozen.providers.usage' and name == '_track_cost':
            label = 'usage_record'
        elif module == 'openkyrozen.persistence.usage' and name == 'record_usage_attempt':
            label = 'usage_persist'
        elif module == 'openkyrozen.persistence.events' and name == 'append_event':
            label = 'event_persist'
        elif module == 'openkyrozen.agent.delegation_runtime' and name == '_invoke_delegated':
            label = 'delegated_invoke'
        elif module == 'openkyrozen.agent.subagent_runtime' and name == '_subagent_usage_metrics':
            label = 'usage_summary'
        elif module == 'openkyrozen.agent.runtime' and name == '_publish_runtime_event':
            label = 'runtime_event'
        if label:
            self.record(label + '.' + event, function=frame.f_code.co_qualname)

    def snapshot(self, filename):
        """Avoid application locks and raw copies of live SQLite/WAL files."""
        state = {'monotonic_ns': time.monotonic_ns(), 'persisted_states': [], 'runs': []}
        case, coordinator = self.case, self.coordinator
        if coordinator:
            for run_id, run in list(coordinator.runs.items()):
                future = coordinator.futures.get(run_id)
                state['runs'].append({'run_id': run_id, 'status': run.get('status'),
                    'error': run.get('error'), 'future_done': future.done() if future else None,
                    'cancelled': coordinator.cancelled[run_id].is_set()})
        if case and hasattr(case, 'root'):
            try:
                state['marker'] = (case.root / 'marker.txt').read_text(encoding='utf-8')
                database = case.root / 'state.sqlite3'
                state['sqlite_files'] = {path.name: path.stat().st_size
                    for path in case.root.glob('state.sqlite3*') if path.is_file()}
                with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=0)) as connection:
                    rows = connection.execute("SELECT payload FROM events WHERE event_type='subagent.state' ORDER BY rowid").fetchall()
                for row in rows:
                    run = json.loads(row[0])
                    state['persisted_states'].append({key: run.get(key) for key in
                        ('run_id', 'status', 'version', 'error', 'progress')})
            except (OSError, sqlite3.Error) as exc:
                state['snapshot_error'] = repr(exc)
        write_json(self.output / filename, state)


def run_child(output, control='baseline', cpu_workers=2):
    # Import the fixture before starting the four-second cancellation watchdog.
    from test_delegation import DelegationTests

    output.mkdir(parents=True, exist_ok=True)
    trace = Trace(output)

    class DiagnosticCase(DelegationTests):
        def setUp(self):
            trace.case = self
            trace.record('setup.call')
            super().setUp()
            trace.record('setup.return')
            # Lazy SDK initialization must not consume the cancellation trace's profiling overhead.
            sys.setprofile(trace.profile)
            threading.setprofile(trace.profile)
            self.load_control = LoadControl(control, cpu_workers, self, trace)
            self.addCleanup(self.load_control.close)
            self.load_control.start()

        def tearDown(self):
            trace.record('cleanup.call')
            trace.snapshot('state-before-cleanup.json')
            try:
                self.load_control.close()
            finally:
                try:
                    super().tearDown()
                finally:
                    trace.record('cleanup.return')

    stopped = threading.Event()

    def watch():
        count = 0
        while not stopped.wait(4):
            count += 1
            trace.record('watchdog', interval_seconds=4)
            trace.snapshot(f'state-watchdog-{count}.json')
            if stopped.is_set():
                return
            stacks.write('Timeout (0:00:04)!\n')
            stacks.flush()
            # The C watchdog can spin walking profiled live frames on CPython 3.12.
            faulthandler.dump_traceback(file=stacks, all_threads=True)

    previous = sys.getprofile()
    previous_thread = threading.getprofile()
    with (output / 'threads.log').open('w', encoding='utf-8') as stacks:
        watcher = threading.Thread(target=watch, name='diagnostic-state-watchdog', daemon=True)
        watcher.start()
        try:
            with (output / 'unittest.log').open('w', encoding='utf-8') as log:
                result = unittest.TextTestRunner(stream=log, verbosity=2).run(DiagnosticCase(TEST))
            trace.record('result', successful=result.wasSuccessful(), failures=len(result.failures), errors=len(result.errors))
        finally:
            sys.setprofile(previous)
            threading.setprofile(previous_thread)
            stopped.set()
            watcher.join(timeout=1)
            with trace.lock:
                trace.active = False
                trace.stream.close()
    return 0 if result.wasSuccessful() else 1


def run_process(command, log_path, env):
    started = time.monotonic_ns()
    cleanup = None
    with log_path.open('w', encoding='utf-8') as log:
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=os.name != 'nt',
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)
        try:
            returncode = process.wait(timeout=45)
        except subprocess.TimeoutExpired:
            try:
                cleanup = stop_diagnostic_tree(process)
            except Exception as error:
                cleanup = {'parent_pid': process.pid, 'error_type': type(error).__name__,
                           'descendant_cleanup_certified': False}
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=3)
            returncode = 124
    return {'returncode': returncode, 'elapsed_ns': time.monotonic_ns() - started,
            'command': command, 'timeout_cleanup': cleanup}


def main():
    global ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--repository', type=Path, default=ROOT, help='Exact product checkout to test; harness may live separately')
    parser.add_argument('--iterations', type=int, default=20)
    parser.add_argument('--control', choices=('baseline', 'cpu', 'sqlite', 'combined'), default='baseline')
    parser.add_argument('--cpu-workers', type=int, default=2, help='Bounded owned CPU load processes (1–4)')
    parser.add_argument('--interleave-reviewer', action='store_true', help='Run fixture-only reviewer acceptance after each cancellation, matching the original order')
    parser.add_argument('--historical-environment', action='store_true', help='Require Windows Python 3.12.10 and every original package version; mismatch is infrastructure failure')
    parser.add_argument('--write-historical-requirements', type=Path, help='Write original package pins and exit without loading the product')
    parser.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.write_historical_requirements:
        args.write_historical_requirements.parent.mkdir(parents=True, exist_ok=True)
        args.write_historical_requirements.write_text('\n'.join(HISTORICAL_REQUIREMENTS.split()) + '\n', encoding='utf-8')
        return 0
    if args.output is None:
        parser.error('--output is required')
    if args.iterations < 1:
        parser.error('--iterations must be positive')
    if not 1 <= args.cpu_workers <= 4:
        parser.error('--cpu-workers must be between 1 and 4')
    ROOT = args.repository.resolve()
    sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
    output = args.output.resolve()
    if args.child:
        return run_child(output, args.control, args.cpu_workers)
    output.mkdir(parents=True, exist_ok=True)
    if args.historical_environment:
        environment = historical_environment()
        write_json(output / 'historical-environment.json', environment)
        if environment['status'] != 'MATCH':
            print('INFRASTRUCTURE_FAILURE: historical environment differs; no fixture was executed.', flush=True)
            return 2
    revision = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    tracked_diff = subprocess.run(['git', 'diff', 'HEAD', '--'], cwd=ROOT, capture_output=True, check=True).stdout
    report = {'tracked_product_edits': bool(tracked_diff),
        'tracked_product_diff_sha256': hashlib.sha256(tracked_diff).hexdigest(), 'schema_version': 1, 'candidate_sha': revision, 'platform': platform.platform(),
        'python': sys.version, 'sqlite_version': sqlite3.sqlite_version, 'cpu_count': os.cpu_count(),
        'fixture': 'tests.test_delegation.DelegationTests.' + TEST,
        'fixture_sha256': hashlib.sha256((ROOT / 'tests/test_delegation.py').read_bytes()).hexdigest(),
        'diagnostic_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'test_timeouts_seconds': {'provider_wait': 3, 'entry_wait': 10, 'future_wait': 5},
        'watchdog_seconds': 4, 'historical_cause_established': False,
        'historical_environment_required': args.historical_environment,
        'interleave_reviewer': args.interleave_reviewer, 'execution_order': [],
        'private_home': True,
        'control': args.control, 'cpu_workers': args.cpu_workers if args.control in {'cpu', 'combined'} else 0,
        'qualification': 'Observational instrumentation changes scheduling; passing samples do not explain the historical timeout.',
        'iterations': []}
    write_json(output / 'report.json', report)
    with tempfile.TemporaryDirectory(prefix='kyrozen-windows-diagnostic-') as home:
        env = isolated_environment(Path(home))
        for index in range(1, args.iterations + 1):
            directory = output / str(index)
            directory.mkdir(exist_ok=True)
            command = [sys.executable, str(Path(__file__).resolve()), '--child', '--output', str(directory),
                       '--repository', str(ROOT), '--control', args.control, '--cpu-workers', str(args.cpu_workers)]
            report['execution_order'].append(f'cancellation-{index}')
            write_json(output / 'report.json', report)
            result = {'iteration': index, **run_process(command, directory / 'process.log', env)}
            report['iterations'].append(result)
            write_json(output / 'report.json', report)
            print(f"Cancellation iteration {index}: exit={result['returncode']}", flush=True)
            if args.interleave_reviewer:
                report['execution_order'].append(f'reviewer-{index}')
                write_json(output / 'report.json', report)
                result['reviewer'] = run_process(
                    [sys.executable, str(ROOT / 'scripts/subagent_workflow_acceptance.py')],
                    directory / 'reviewer.log', env)
                write_json(output / 'report.json', report)
                print(f"Reviewer iteration {index}: exit={result['reviewer']['returncode']}", flush=True)
    return int(any(item['returncode'] != 0 or item.get('reviewer', {}).get('returncode', 0) != 0
                   for item in report['iterations']))


if __name__ == '__main__':
    raise SystemExit(main())

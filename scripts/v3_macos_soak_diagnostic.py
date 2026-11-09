"""One original 20-second soak, with process and task-owned crash evidence only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import pwd
import re
import resource
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback

SHA = '6ebc93f19e87fc29fb8ee88df9d9948bfaea5d5a'
TREE = '31dfb051f6feaa6292b5053cdd9f305c6bae693f'
SIGNER = 'D3897E8FF704149D609ACF878C118C04263D2A4C'
PUBLIC_KEY_SHA256 = 'b1cef62473c1b0198813d5d563057ed1a2178ab0be89ff33ac9b87a48cb37cbc'
parser = argparse.ArgumentParser()
parser.add_argument('--source', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--public-key', type=Path, required=True)
parser.add_argument('--reuse-install', action='store_true', help='Reuse only the successful first cell install in this bounded same-job diagnosis')
args = parser.parse_args()
source, output = args.source.resolve(strict=True), args.output.resolve()
output.mkdir(parents=True, exist_ok=False)
report = {'scope': 'ONE_ORIGINAL_20S_DIAGNOSIS_NOT_1800S_ACCEPTANCE', 'status': 'UNKNOWN',
          'candidate_sha': SHA, 'python': sys.version, 'executable': sys.executable,
          'platform': platform.platform(), 'core_limits': resource.getrlimit(resource.RLIMIT_CORE),
          'runner': {k: os.environ.get(k) for k in ('ImageOS', 'ImageVersion', 'RUNNER_OS', 'RUNNER_ARCH')}}
report['reuse_install'] = args.reuse_install
report['retention_scope'] = 'Core diagnostic files; private-home installer/cache tree intentionally excluded from index and upload'
# Inspect only new Python reports for this process's owned gateway PIDs.
crash_dirs = {Path.home() / 'Library/Logs/DiagnosticReports',
              Path(pwd.getpwuid(os.getuid()).pw_dir) / 'Library/Logs/DiagnosticReports'}
existing = {p.resolve() for d in crash_dirs if d.is_dir() for p in d.iterdir()}
children, observations, stops, cleanup_pids = [], [], [], set()
parent_signal_requests = []
finished = threading.Event()
probe_start = None

def record_command(command, name):
    result = subprocess.run(command, cwd=source, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600)
    (output / name).write_bytes(result.stdout)
    if result.returncode:
        raise RuntimeError(f'{name} exited {result.returncode}; full output retained')
    return result.stdout

def tracked_identity():
    names = subprocess.check_output(['git', 'ls-tree', '-r', '--name-only', '-z', SHA], cwd=source).split(b'\0')[:-1]
    digest = hashlib.sha256()
    for name in sorted(names):
        digest.update(name + b'\0' + (source / os.fsdecode(name)).read_bytes())
    subprocess.run(['git', 'diff', '--exit-code', SHA, '--'], cwd=source, check=True, capture_output=True)
    return {'head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source).decode().strip(),
            'tree': subprocess.check_output(['git', 'rev-parse', 'HEAD^{tree}'], cwd=source).decode().strip(),
            'tracked_files': len(names), 'tracked_LF_identity': digest.hexdigest()}

def watch():
    while not finished.wait(.05):
        for process in tuple(children):
            code = process.poll()
            if code is not None and not any(r['pid'] == process.pid for r in observations):
                observations.append({'pid': process.pid, 'returncode': code,
                    'signal': signal.Signals(-code).name if code < 0 else None,
                    'cleanup_already_started': process.pid in cleanup_pids, 'wall_time': time.time()})

try:
    assert sys.version_info[:3] == (3, 12, 10) and platform.system() == 'Darwin'
    env = {k: os.environ[k] for k in ('PATH', 'TMPDIR', 'LANG', 'LC_ALL') if k in os.environ}
    env.update(HOME=str(output / 'private-home'), PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1',
               KYROZEN_DISABLE_VECTOR_INDEX='1', PIP_CONFIG_FILE=os.devnull, PIP_NO_CACHE_DIR='1',
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')
    Path(env['HOME']).mkdir(mode=0o700)
    os.environ.clear()
    os.environ.update(env)
    assert hashlib.sha256(args.public_key.read_bytes()).hexdigest() == PUBLIC_KEY_SHA256
    report['source_before'] = tracked_identity()
    assert report['source_before']['head'] == SHA and report['source_before']['tree'] == TREE
    with tempfile.TemporaryDirectory(prefix='public-gpg-', dir=output) as directory:
        os.environ['GNUPGHOME'] = directory
        record_command(['gpg', '--batch', '--no-autostart', '--import', str(args.public_key.resolve(strict=True))], 'public-key-import.log')
        signature = record_command(['git', 'verify-commit', '--raw', SHA], 'signature.log')
        assert ('[GNUPG:] VALIDSIG ' + SIGNER).encode() in signature
        assert not list((Path(directory) / 'private-keys-v1.d').glob('*'))
        report['public_key_sha256'] = hashlib.sha256(args.public_key.read_bytes()).hexdigest()
        report['private_gpg_keys'] = 0
        del os.environ['GNUPGHOME']
    if not args.reuse_install:
        record_command([sys.executable, '-m', 'pip', 'install', str(source) + '[all]'], 'install.log')
    record_command([sys.executable, '-m', 'pip', 'freeze'], 'dependencies.txt')
    sys.path[:0] = [str(source), str(source / 'tests'), str(source / 'scripts')]
    import production_soak
    import test_durable_tasks_gateway as gateway_tests
    real_popen, real_stop = subprocess.Popen, gateway_tests.DurableTaskGatewayTests._stop_server
    baseline = production_soak._capture_process_tree(os.getpid(), output / 'owned-baseline.json')
    def popen(command, *values, **options):
        process = real_popen(command, *values, **options)
        if isinstance(command, (list, tuple)) and str(source / 'server.py') in command:
            children.append(process)
            original_send_signal = process.send_signal
            def send_signal(sig):
                request = {'pid': process.pid, 'requested_signal': sig, 'request_wall_time': time.time()}
                try:
                    request['pre_signal_poll'] = process.poll()
                except Exception as error:
                    request.update(pre_signal_poll=None, pre_signal_poll_error=type(error).__name__)
                parent_signal_requests.append(request)
                return original_send_signal(sig)
            process.send_signal = send_signal
        return process
    def stop(process):
        code = process.poll()
        stops.append({'pid': process.pid, 'before_cleanup_returncode': code,
                      'before_cleanup_signal': signal.Signals(-code).name if code is not None and code < 0 else None,
                      'exception': str(sys.exc_info()[1]) if sys.exc_info()[1] else None})
        cleanup_pids.add(process.pid)
        real_stop(process)
        stops[-1]['after_cleanup_returncode'] = process.poll()
    subprocess.Popen = popen
    gateway_tests.DurableTaskGatewayTests._stop_server = staticmethod(stop)
    monitor = threading.Thread(target=watch, daemon=True)
    monitor.start()
    probe_start = time.time()
    try:
        report['soak'] = production_soak.run(20, output / 'soak-logs')
        report['status'] = 'PASS_20S_ONLY'
    except BaseException as error:
        report.update(status='FAIL', error_type=type(error).__name__, error=str(error),
                      soak=getattr(error, 'soak_report', None))
        (output / 'probe-error.log').write_text(traceback.format_exc())
    finally:
        for process in children:
            if process.poll() is None:
                try:
                    stop(process)
                except Exception as error:
                    report.setdefault('cleanup_errors', []).append(str(error))
                    process.kill()
                    process.wait(timeout=5)
        finished.set()
        monitor.join()
        subprocess.Popen = real_popen
        gateway_tests.DurableTaskGatewayTests._stop_server = staticmethod(real_stop)
        try:
            production_soak._assert_no_new_audit_children(os.getpid(), baseline, output / 'owned-final.json')
            report['owned_cleanup_certified'] = bool(children) and all(p.poll() is not None for p in children)
        except Exception as error:
            report['owned_cleanup_certified'] = False
            report['cleanup_error'] = str(error)
        report['source_after'] = tracked_identity()
        assert report['source_after'] == report['source_before']
except BaseException as error:
    report.update(status='UNKNOWN', error_type=type(error).__name__, error=str(error))
    (output / 'setup-or-cleanup-error.log').write_text(traceback.format_exc())
finally:
    # Requests record intent only; actual return codes/signals remain independent raw observations.
    report.update(parent_signal_requests=parent_signal_requests, process_observations=observations, cleanup=stops,
                  children=[{'pid': p.pid, 'final_returncode': p.poll()} for p in children])
    # No unrelated or pre-existing report content is copied; oversized/late reports stay unknown.
    retained = []
    for directory in crash_dirs:
        if not directory.is_dir() or probe_start is None:
            continue
        for path in directory.iterdir():
            if (path.resolve() in existing or path.is_symlink() or not path.is_file()
                    or path.suffix not in {'.ips', '.crash'} or not path.name.lower().startswith('python')
                    or path.stat().st_mtime < probe_start or path.stat().st_size > 5 * 1024 * 1024):
                continue
            raw = path.read_bytes()
            text = raw.decode(errors='replace')
            matches = re.findall(r'^Process:\s+Python[^\n]*\[(\d+)\]', text, re.M | re.I)
            if path.suffix == '.ips':
                decoder, rest = json.JSONDecoder(), text.lstrip()
                while rest:
                    try:
                        doc, end = decoder.raw_decode(rest)
                    except ValueError:
                        break
                    if isinstance(doc, dict) and str(doc.get('procName', '')).lower().startswith('python'):
                        matches.append(str(doc.get('pid')))
                    rest = rest[end:].lstrip()
            if not any(str(p.pid) in matches for p in children) or len(retained) >= 4:
                continue
            dest = output / 'task-crash-reports' / path.name
            dest.parent.mkdir(exist_ok=True)
            dest.write_bytes(raw)
            retained.append({'path': dest.relative_to(output).as_posix(), 'bytes': len(raw),
                             'sha256': hashlib.sha256(raw).hexdigest(), 'matched_task_pids': matches})
    report['task_crash_reports'] = retained
    report['crash_report_absence_does_not_prove_no_crash'] = True
    if report.get('owned_cleanup_certified') is not True:
        report['status'] = 'UNKNOWN'
    (output / 'diagnostic.json').write_text(json.dumps(report, indent=2) + '\n')
    rows = []
    for path in sorted(output.rglob('*')):
        if path.is_file() and not path.is_relative_to(output / 'private-home'):
            raw = path.read_bytes()
            rows.append({'path': path.relative_to(output).as_posix(), 'bytes': len(raw),
                         'sha256': hashlib.sha256(raw).hexdigest()})
    (output / 'index.json').write_text(json.dumps({'scope': report['scope'], 'excluded_subtrees': [{'path': 'private-home', 'reason': 'Task-owned dependency/cache files intentionally excluded; index covers core diagnostics, not this cache tree'}], 'files': rows}, indent=2) + '\n')
sys.exit(0 if report['status'] == 'PASS_20S_ONLY' else 1)

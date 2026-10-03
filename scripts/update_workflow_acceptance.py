#!/usr/bin/env python3
"""Perform real package-managed upgrades in an isolated HOME and uv installation."""
from __future__ import annotations
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time

REPOSITORY = 'https://github.com/EvanProgramming/OpenKyrozen.git'


def run(command, cwd, env, timeout=900):
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'Command failed ({result.returncode}): {command[0]}\n{result.stdout[-3000:]}\n{result.stderr[-3000:]}')
    return result.stdout.strip()


PROBE = '''import importlib.metadata as m,json
d=m.distribution('openkyrozen')
print(json.dumps({'version':d.version,'source':json.loads(d.read_text('direct_url.json') or '{}')}))'''
UPDATE = '''import json,os
try:
 from openkyrozen.app.bootstrap import build_application
 runtime=build_application(surface='cli').runtime
except ModuleNotFoundError:
 import main as runtime
runtime._resolve_update_revision=lambda *args,**kwargs: os.environ['AUDIT_REVISION']
result=runtime._self_update()
if isinstance(result,str):
 assert result.startswith('Updated OpenKyrozen from '),result
 print(json.dumps({'status':'legacy-success','message':result}))
else:
 assert (result.status=='success' and result.restart_ready) or (os.name=='nt' and result.status=='partial' and result.components.get('python')=='verified-staged'),str(result)
 print(json.dumps({'status':result.status,'components':result.components,'revision':result.revision}))
'''
SEED = '''from openkyrozen.persistence.store import EventStore
from openkyrozen.tasks.engine import TaskManager
from openkyrozen.memory.service import MemoryBank
import os
store=EventStore(os.environ['KYROZEN_DB_PATH'])
store.append_event('session.message',{'role':'user','content':'synthetic durable chat'},session_id='audit-chat')
tasks=TaskManager(store,workspace_id='audit-workspace',session_id='audit-chat')
tasks.add_task('synthetic pending task')
store.upsert_memory('synthetic persistent memory',kind='fact',workspace_id='audit-workspace',metadata={'source':'production-audit'})
'''


def acceptance(revision, baseline):
    uv = shutil.which('uv')
    if not uv:
        raise RuntimeError('BLOCKED: uv unavailable')
    with tempfile.TemporaryDirectory(prefix='openkyrozen-real-update-') as directory:
        root = Path(directory)
        home, caller = root / 'home', root / 'project'
        home.mkdir(); caller.mkdir()
        env = os.environ.copy()
        for key in ('PYTHONPATH','KYROZEN_API_KEY','DEEPSEEK_API_KEY','OPENAI_API_KEY','ANTHROPIC_API_KEY',
                    'KYROZEN_AGENT_CONFIG','KYROZEN_DISABLE_VECTOR_INDEX'):
            env.pop(key,None)
        env.update({'HOME':str(home),'USERPROFILE':str(home), 'UV_TOOL_DIR':str(root/'tools'),
            'UV_TOOL_BIN_DIR':str(root/'bin'), 'UV_CACHE_DIR':str(root/'cache'),
            'KYROZEN_DB_PATH':str(root/'state.sqlite3'), 'KYROZEN_SKILLS_DIR':str(root/'skills'),
            'AUDIT_REVISION':revision,'KYROZEN_PROVIDER':'ollama','KYROZEN_BASE_URL':'http://127.0.0.1:9/v1'})
        run([uv,'tool','install','--python','3.12','--force','--with','fastapi','--with','uvicorn',baseline],caller,env)
        python = root/'tools/openkyrozen'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        before = json.loads(run([str(python),'-c',PROBE],caller,env).splitlines()[-1])
        run([str(python),'-c',SEED],caller,env)
        workspace_file=caller/'sentinel.txt'; workspace_file.write_text('user workspace sentinel')
        settings=home/'.kyrozen_config.json'; settings.write_text(json.dumps({'provider':'ollama','api_key':'',
            'model_simple':'synthetic-model','model_complex':'synthetic-model','base_url':'http://127.0.0.1:9/v1'}))
        expected_settings=settings.read_bytes()
        with closing(sqlite3.connect(root/'state.sqlite3')) as connection:
            records={table:connection.execute(f'SELECT * FROM {table}').fetchall() for table in ('events','tasks','memories')}
        started=time.monotonic()
        transition='prior-installed-updater'
        if os.name=='nt':
            # Historical Windows updaters cannot remove their running uv Python.
            # Exercise their documented external installer recovery explicitly.
            run([uv,'tool','install','--python','3.12','--force','--with','fastapi','--with','uvicorn',
                 f'git+{REPOSITORY}@{revision}'],caller,env)
            transition='external-installer-recovery-required-by-historical-windows-updater'
        def update_and_wait(active_python):
            result=json.loads(run([str(active_python),'-c',UPDATE],caller,env).splitlines()[-1])
            if os.name=='nt':
                manifest=home/'.kyrozen/update-state.json'
                deadline=time.monotonic()+150
                while time.monotonic()<deadline:
                    state=json.loads(manifest.read_text())
                    if state['status']=='success':
                        return result,Path(state['python'])
                    assert state['status']!='failed',state
                    time.sleep(.25)
                raise AssertionError('Windows staged activation did not complete')
            return result,active_python
        if os.name=='nt':
            stale=home/'.kyrozen/bin/openkyrozen-tui.exe.next'
            stale.parent.mkdir(parents=True,exist_ok=True)
            stale.write_bytes(b'obsolete pending update fixture')
        first,python=update_and_wait(python)
        if os.name=='nt':
            assert not stale.exists(),'Stale pending TUI survived verified activation'
        after=json.loads(run([str(python),'-I','-c',PROBE],caller,env).splitlines()[-1])
        assert after['source'].get('vcs_info',{}).get('commit_id')==revision,after
        second,python=update_and_wait(python)
        assert second['status']=='success' or (os.name=='nt' and second['status']=='partial'),second
        name='openkyrozen-tui.exe' if os.name=='nt' else 'openkyrozen-tui'
        binary=home/'.kyrozen/bin'/name
        if os.name=='nt':
            # Exercise the real launcher activation path in a fresh interpreter.
            run([str(python),'-c','from openkyrozen.interfaces.cli.launcher import _tui_binary; print(_tui_binary())'],caller,env)
        assert run([str(binary),'--revision'],caller,env)==revision
        executable=root/'bin'/('kyrozen.exe' if os.name=='nt' else 'kyrozen')
        run([str(executable),'--version'],caller,env)
        run([str(executable),'--help'],caller,env)
        assert workspace_file.read_text()=='user workspace sentinel'
        assert settings.read_bytes()==expected_settings
        with closing(sqlite3.connect(root/'state.sqlite3')) as connection:
            for table, rows in records.items():
                current=connection.execute(f'SELECT * FROM {table}').fetchall()
                assert all(row in current for row in rows), f'{table} records changed during update'
            assert connection.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        from audit_identity import audit_identity
        return {'status':'PASS','identity':audit_identity(),'transition':transition,'baseline':baseline,'before':before,'after':after,'first_update':first,
                'repeat_update':second,'tui_revision':revision,'seconds':round(time.monotonic()-started,2),
                'state_preserved':['settings','chats','tasks','memories','workspace'],
                'tui_sha256':hashlib.sha256(binary.read_bytes()).hexdigest()}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--revision',required=True)
    parser.add_argument('--baseline',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    try:
        report=acceptance(args.revision,args.baseline)
    except Exception as exc:
        args.output.write_text(json.dumps({'status':'FAIL','error':str(exc)},indent=2))
        raise
    args.output.write_text(json.dumps(report,indent=2))
    print('Real installed update acceptance passed.')

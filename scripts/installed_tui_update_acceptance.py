#!/usr/bin/env python3
"""Observe the real installed /update command and launcher restart in a PTY."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def acceptance(revision,source_root,pinned_candidate=False):
    started=time.monotonic()
    import fcntl
    import pty
    import select
    import signal
    import struct
    import termios
    import psutil
    import sqlite3
    sys.path.insert(0,str(source_root/'scripts'))
    from update_workflow_acceptance import run,PROBE,SEED
    import shutil
    uv=shutil.which('uv')
    assert uv,'uv unavailable'
    assert subprocess.check_output(['git','rev-parse','HEAD'],cwd=source_root,text=True).strip()==revision
    with tempfile.TemporaryDirectory(prefix='openkyrozen-published-tui-') as directory:
        root=Path(directory);home=root/'home';project=root/'project'
        home.mkdir();project.mkdir()
        env=os.environ.copy()
        for key in list(env):
            if key.endswith('_API_KEY') or key in {'PYTHONPATH','KYROZEN_TUI_BINARY','KYROZEN_BACKEND_COMMAND','KYROZEN_DISABLE_TUI','KYROZEN_AGENT_CONFIG'}:
                env.pop(key,None)
        env.update(HOME=str(home),USERPROFILE=str(home),UV_TOOL_DIR=str(root/'tools'),UV_TOOL_BIN_DIR=str(root/'bin'),
                   UV_CACHE_DIR='/private/tmp/openkyrozen-tui-audit-cache',KYROZEN_DB_PATH=str(root/'state.sqlite3'),KYROZEN_SKILLS_DIR=str(root/'skills'),
                   PLAYWRIGHT_BROWSERS_PATH='/private/tmp/openkyrozen-tui-audit-browsers',KYROZEN_PROVIDER='ollama',KYROZEN_BASE_URL='http://127.0.0.1:9/v1',
                   KYROZEN_LEARNING_WORKER='1',KYROZEN_REDUCED_MOTION='1',TERM='xterm-256color')
        run([uv,'tool','install','--python','3.12','--with','fastapi','--with','uvicorn','--with','openai',
             f'git+https://github.com/EvanProgramming/OpenKyrozen.git@{revision}'],project,env)
        python=root/'tools/openkyrozen/bin/python';launcher=root/'bin/kyrozen'
        run([str(python),'-I','-c',SEED],project,env)
        with sqlite3.connect(root/'state.sqlite3') as c:
            records={table:c.execute(f'SELECT * FROM {table}').fetchall() for table in ('events','tasks','memories')}
        before_provenance=json.loads(run([str(python),'-I','-c',PROBE],project,env).splitlines()[-1])
        assert before_provenance['source']['vcs_info']['commit_id']==revision
        settings=home/'.kyrozen_config.json'
        settings.write_text(json.dumps({'provider':'ollama','api_key':'','model_simple':'synthetic-model','model_complex':'synthetic-model',
                                        'base_url':'http://127.0.0.1:9/v1','self_learning':False}))
        settings_bytes=settings.read_bytes();sentinel=project/'sentinel.txt';sentinel.write_text('untouched synthetic workspace')
        binary=home/'.kyrozen/bin/openkyrozen-tui';binary.parent.mkdir(parents=True)
        (home/'.kyrozen/ui_settings.json').write_text(json.dumps({'last_seen_version':json.loads(run([str(python),'-I','-c',PROBE],project,env).splitlines()[-1])['version'],'onboarding_complete':True}))
        run(['go','build','-ldflags',f'-X main.revision={revision}','-o',str(binary),'.'],source_root/'tui',env)
        if pinned_candidate:
            shim = python.parent/'kyrozen-backend'
            shim.write_text('#!'+str(python)+'\nimport os\nfrom openkyrozen.interfaces.tui.backend import Backend,main\noriginal=Backend.__init__\ndef initialize(self,*args,**kwargs):\n original(self,*args,**kwargs)\n self.agent._resolve_update_revision=lambda *args,**kwargs:os.environ["AUDIT_PIN_REVISION"]\nBackend.__init__=initialize\nmain()\n')
            shim.chmod(0o700)
            env['AUDIT_PIN_REVISION']=revision
        else:
            env.pop('AUDIT_PIN_REVISION',None)
        master,slave=pty.openpty();fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',35,120,0,0))
        process=subprocess.Popen([str(launcher),'--project',str(project)],cwd=project,env=env,stdin=slave,stdout=slave,stderr=slave,start_new_session=True)
        os.close(slave);terminal=bytearray()
        def rows():
            result=[]
            try:
                children=psutil.Process(process.pid).children(recursive=True)
            except psutil.Error:
                return result
            for child in children:
                try:
                    command=child.cmdline()
                    if any(arg.endswith('/kyrozen-backend') for arg in command):
                        result.append({'event':'backend','pid':child.pid,'command':command})
                except psutil.Error:
                    continue  # A retiring backend can disappear during sysctl.
            return result
        def opened():
            try:
                with sqlite3.connect(root/'state.sqlite3') as c:
                    return c.execute("SELECT count(*) FROM events WHERE event_type='tui.project_opened'").fetchone()[0]
            except sqlite3.Error:
                return 0
        def drain():
            if select.select([master],[],[],.1)[0]:
                try:data=os.read(master,65536)
                except OSError:return
                terminal.extend(data)
                if b'\x1b[6n' in data:os.write(master,b'\x1b[1;1R')
                if b'\x1b]10;?' in data:os.write(master,b'\x1b]10;rgb:ffff/ffff/ffff\x1b\\')
                if b'\x1b]11;?' in data:os.write(master,b'\x1b]11;rgb:0000/0000/0000\x1b\\')
        def wait(predicate,timeout,label):
            deadline=time.monotonic()+timeout
            while time.monotonic()<deadline:
                drain()
                if predicate(rows()):return
                assert process.poll() is None, label+': launcher exited'
            raise AssertionError(label+': timed out; synthetic terminal tail='+terminal.decode(errors='replace')[-2000:])
        try:
            wait(lambda r:bool(r) and opened()>=1,90,'initial backend workspace startup')
            before=rows();assert len(before)==1
            tui_before=[p.pid for p in psutil.Process(process.pid).children() if p.name()=='openkyrozen-tui'];assert len(tui_before)==1
            # The live launcher's console entrypoint remains the production one
            # in published-main mode. Observe child identity and durable startup.
            os.write(master,b'/update\r')
            wait(lambda r:bool(r) and r[0]['pid']!=before[0]['pid'] and opened()>=2,900,'updated launcher restart')
            boots=before+rows()
            tui_after=[p.pid for p in psutil.Process(process.pid).children() if p.name()=='openkyrozen-tui']
            assert len(tui_after)==1 and tui_before!=tui_after
            assert len(boots)==2 and boots[0]['pid']!=boots[1]['pid'],boots
            assert all(str(python.parent/'kyrozen-backend') in item['command'] or
                       any(Path(arg).resolve()==(python.parent/'kyrozen-backend').resolve() for arg in item['command'])
                       for item in boots),boots
            manifest=json.loads((home/'.kyrozen/update-state.json').read_text())
            assert manifest['status']=='success' and manifest['revision']==revision,manifest
            after=json.loads(run([str(python),'-I','-c',PROBE],project,env).splitlines()[-1])
            assert after['source']['vcs_info']['commit_id']==revision
            assert run([str(binary),'--revision'],project,env)==revision
            assert settings.read_bytes()==settings_bytes and sentinel.read_text()=='untouched synthetic workspace'
            run([str(launcher),'--version'],project,env)
            with sqlite3.connect(root/'state.sqlite3') as c:
                for table,previous in records.items():
                    current=c.execute(f'SELECT * FROM {table}').fetchall()
                    assert all(row in current for row in previous),table
                assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            return {'status':'PASS','command':'/update','update_source':'pinned-candidate' if pinned_candidate else 'published-main','revision':revision,'seconds':round(time.monotonic()-started,2),'before':before_provenance,'go':run(['go','version'],source_root,env),'backend_boots':boots,'automatic_launcher_restart':True,
                    'tui_revision':revision,'after':after,'provider_calls':0,'tui_processes':tui_before+tui_after,'state_preserved':['settings','chats','tasks','memories','workspace'],
                    'pinned_resolver_shim':pinned_candidate,'observer':'external child PIDs, production console command, durable project startup events, installed provenance; published mode does not modify backend or resolver'}
        finally:
            if process.poll() is None:os.killpg(process.pid,signal.SIGTERM)
            process.wait(timeout=15);os.close(master)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--pinned-candidate',action='store_true');parser.add_argument('--revision');parser.add_argument('--source-root',type=Path);parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    report=acceptance(args.revision,args.source_root,args.pinned_candidate);args.output.write_text(json.dumps(report,indent=2));print(json.dumps({'status':report['status'],'revision':report['revision'],'backend_boots':len(report['backend_boots'])}))

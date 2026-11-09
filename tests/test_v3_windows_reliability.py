import json
import hashlib
import io
import gzip
import zlib
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
import zipfile
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'v3_windows_reliability.py'


class OfflineNativeDecodeTests(unittest.TestCase):
    def test_xperf_version_metadata_uses_isolated_native_query_with_the_original_five_second_bound(self):
        from scripts import v3_windows_reliability as diagnostic
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);tool=root/'xperf.exe';tool.write_bytes(b'opaque native metadata fixture')
            version=json.dumps({'file_version':'1.2.3.4','product_version':'5.6.7.8'})
            with patch.object(diagnostic.shutil,'which',side_effect=lambda name:str(tool) if name=='xperf.exe' else None), \
                 patch.object(diagnostic.subprocess,'run',return_value=subprocess.CompletedProcess([],0,version,'')) as query:
                try:
                    info=diagnostic._offline_xperf_tool(root,{'HOME':str(root)})
                except RuntimeError:
                    self.fail('Version metadata must not require or launch PowerShell')
            command=query.call_args.args[0]
            self.assertEqual(command[:3],[sys.executable,'-I','-c'])
            self.assertEqual(command[3],diagnostic._OFFLINE_FILE_VERSION_QUERY)
            self.assertEqual(command[4],str(tool.resolve()))
            self.assertEqual(query.call_args.kwargs['timeout'],5)
            self.assertTrue(query.call_args.kwargs['check'])
            self.assertNotIn('KYROZEN_XPERF_FILE',query.call_args.kwargs['env'])
            self.assertEqual(info['file_version'],'1.2.3.4')
            self.assertIn('VS_FIXEDFILEINFO',info['version_source'])

    def test_native_version_query_validates_sdk_resource_and_pointer_before_reading_fixed_fields(self):
        import ctypes
        import struct
        from contextlib import redirect_stdout
        from scripts import v3_windows_reliability as diagnostic
        self.assertTrue(hasattr(diagnostic,'_OFFLINE_FILE_VERSION_QUERY'),'native version query missing')
        class Function:
            def __init__(self,call):self.call=call
            def __call__(self,*args):return self.call(*args)
        modes=('valid','missing','oversized','get-info-failure','query-failure','null','short','outside','span-overrun','signature')
        with tempfile.TemporaryDirectory() as directory:
            tool=Path(directory)/'unicode-工具.exe';tool.write_bytes(b'private SDK-boundary fixture')
            for mode in modes:
                with self.subTest(mode=mode):
                    buffer_address=[]
                    def size(path,ignored):return 0 if mode=='missing' else 1024*1024+1 if mode=='oversized' else 64
                    def read(path,ignored,length,buffer):
                        buffer_address.append(ctypes.addressof(buffer))
                        fields=[0 if mode=='signature' else 0xFEEF04BD,0x10000,0x000A000B,0x000C000D,0x000E000F,0x00100011]+[0]*7
                        ctypes.memmove(buffer_address[0]+8,struct.pack('<13I',*fields),52)
                        return 0 if mode=='get-info-failure' else 1
                    def query(buffer,subblock,pointer,length):
                        self.assertEqual(subblock,'\\')
                        address=0 if mode=='null' else buffer_address[0]+64 if mode=='outside' else buffer_address[0]+8
                        ctypes.cast(pointer,ctypes.POINTER(ctypes.c_void_p))[0]=address
                        ctypes.cast(length,ctypes.POINTER(ctypes.c_uint32))[0]=51 if mode=='short' else 65 if mode=='span-overrun' else 52
                        return 0 if mode=='query-failure' else 1
                    library=types.SimpleNamespace(GetFileVersionInfoSizeW=Function(size),GetFileVersionInfoW=Function(read),VerQueryValueW=Function(query))
                    output=io.StringIO()
                    with patch.object(ctypes,'WinDLL',return_value=library,create=True), \
                         patch.object(sys,'argv',['native-version-query',str(tool)]),redirect_stdout(output):
                        if mode=='valid':
                            exec(compile(diagnostic._OFFLINE_FILE_VERSION_QUERY,'native-version-query','exec'),{})
                            self.assertEqual(json.loads(output.getvalue()),{'file_version':'10.11.12.13','product_version':'14.15.16.17'})
                        else:
                            with self.assertRaises((RuntimeError,OSError)):
                                exec(compile(diagnostic._OFFLINE_FILE_VERSION_QUERY,'native-version-query','exec'),{})
                            self.assertEqual(output.getvalue(),'')

    @unittest.skipUnless(os.name=='nt','actual Windows version.dll query')
    def test_native_version_query_reads_real_python_executable_version(self):
        from scripts import v3_windows_reliability as diagnostic
        result=subprocess.run([sys.executable,'-I','-c',diagnostic._OFFLINE_FILE_VERSION_QUERY,str(Path(sys.executable).resolve())],
            capture_output=True,text=True,timeout=5,check=True)
        version=json.loads(result.stdout)
        for field in ('file_version','product_version'):
            self.assertRegex(version[field],r'^\d+\.\d+\.\d+\.\d+$')
            self.assertTrue(all(0<=int(part)<=65535 for part in version[field].split('.')))

    @unittest.skipUnless(os.name=='nt','actual Windows missing version-resource rejection')
    def test_native_version_query_rejects_real_file_without_version_resource(self):
        from scripts import v3_windows_reliability as diagnostic
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'no-version.exe';path.write_bytes(b'not a PE version resource')
            result=subprocess.run([sys.executable,'-I','-c',diagnostic._OFFLINE_FILE_VERSION_QUERY,str(path)],
                capture_output=True,text=True,timeout=5)
            self.assertNotEqual(result.returncode,0)
            self.assertEqual(result.stdout.strip(),'')

    def stream_fixture(self, root):
        tool=root/'xperf.exe';tool.write_bytes(b'opaque test tool bytes')
        (root/'xperf-tool.json').write_text(json.dumps({'path':str(tool),'sha256':hashlib.sha256(tool.read_bytes()).hexdigest()}))
        (root/'native-capture.etl').write_bytes(b'opaque ETL fixture')
        return tool

    def completed_stream(self, root, payload=b'opaque dump bytes\n'):
        data=gzip.compress(payload,compresslevel=1,mtime=0)
        (root/'native-events.csv.gz').write_bytes(data)
        state={'stdout_eof':True,'gzip_closed':True,'gzip_footer_valid':True,'finalized':True,
            'raw_hash_scope':'complete_stdout',
            'xperf_returncode':0,'raw_bytes':len(payload),'raw_sha256':hashlib.sha256(payload).hexdigest(),
            'raw_crc32':zlib.crc32(payload),'gzip_bytes':len(data),'gzip_sha256':hashlib.sha256(data).hexdigest(),
            'cleanup_uncertified':False,'error':None}
        (root/'gzip-stream.json').write_text(json.dumps(state))
        return state

    def test_gzip_stream_retains_every_binary_stdout_byte_hash_footer_and_real_exit(self):
        from scripts import v3_windows_reliability as diagnostic
        payload=bytes(range(256))*4097+b'\r\n\x00\xff'
        code="import sys;sys.stdout.buffer.write(bytes(range(256))*4097+b'\\r\\n\\x00\\xff')"
        original_popen=subprocess.Popen
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);tool=self.stream_fixture(root);commands=[]
            def spawn(command,**options):
                commands.append(command)
                return original_popen([sys.executable,'-I','-c',code],**options)
            with patch.object(diagnostic.subprocess,'Popen',side_effect=spawn):
                self.assertEqual(diagnostic.run_offline_gzip_child(root),0)
            self.assertEqual(len(commands),1)
            self.assertEqual(commands[0],[str(tool),'-i',str(root/'native-capture.etl'),'-a','dumper',
                '-provider','{90cbdc39-4a3e-11d1-84f4-0000f80464e3}','-add_rawdata'])
            self.assertEqual(gzip.decompress((root/'native-events.csv.gz').read_bytes()),payload)
            state=json.loads((root/'gzip-stream.json').read_text())
            self.assertEqual(state['raw_sha256'],hashlib.sha256(payload).hexdigest())
            self.assertEqual(state['raw_bytes'],len(payload))
            self.assertEqual(state['raw_hash_scope'],'complete_stdout')
            self.assertTrue(state['stdout_eof'] and state['gzip_closed'] and state['gzip_footer_valid'])
            self.assertEqual(state['xperf_returncode'],0)
            self.assertFalse((root/'native-events.csv').exists())

    def test_nonzero_xperf_exit_keeps_lossless_output_but_does_not_complete(self):
        from scripts import v3_windows_reliability as diagnostic
        original_popen=subprocess.Popen
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);self.stream_fixture(root)
            def spawn(command,**options):
                return original_popen([sys.executable,'-I','-c',"import sys;sys.stdout.buffer.write(b'prefix');sys.exit(7)"],**options)
            with patch.object(diagnostic.subprocess,'Popen',side_effect=spawn):
                self.assertEqual(diagnostic.run_offline_gzip_child(root),2)
            state=json.loads((root/'gzip-stream.json').read_text())
            self.assertEqual(state['xperf_returncode'],7)
            self.assertTrue(state['stdout_eof'] and state['gzip_closed'])
            self.assertTrue(state['error'])
            self.assertEqual(gzip.decompress((root/'native-events.csv.gz').read_bytes()),b'prefix')

    def test_gzip_write_error_stops_the_actual_child_and_retains_uncertainty(self):
        from scripts import v3_windows_reliability as diagnostic
        original_popen=subprocess.Popen;owned=[]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);tool=self.stream_fixture(root)
            def spawn(command,**options):
                if command[0]!=str(tool):return original_popen(command,**options)
                child=original_popen([sys.executable,'-I','-c',
                    'import sys,threading;sys.stdout.buffer.write(b"x"*65536);sys.stdout.buffer.flush();threading.Event().wait()'],**options)
                owned.append(child);return child
            try:
                with patch.object(diagnostic.subprocess,'Popen',side_effect=spawn), \
                     patch.object(diagnostic.gzip.GzipFile,'write',side_effect=OSError('controlled output failure')):
                    self.assertEqual(diagnostic.run_offline_gzip_child(root),2)
                self.assertIsNotNone(owned[0].poll())
                state=json.loads((root/'gzip-stream.json').read_text())
                self.assertFalse(state['stdout_eof'])
                self.assertEqual(state['raw_hash_scope'],'observed_prefix')
                self.assertTrue(state['cleanup_uncertified'])
                self.assertIn('controlled output failure',state['error'])
            finally:
                for child in owned:
                    if child.poll() is None:child.kill()
                    child.wait(timeout=3)

    def test_outer_bound_stops_wrapper_actual_child_and_grandchild_in_one_tree(self):
        from scripts import v3_windows_reliability as diagnostic
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);tool=self.stream_fixture(root);marker=root/'ready.json'
            producer='''
import json,os,subprocess,sys,threading
from pathlib import Path
grandchild=subprocess.Popen([sys.executable,'-I','-c','import threading;threading.Event().wait()'],
    stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
marker=Path(sys.argv[1]);temporary=marker.with_suffix('.tmp')
temporary.write_text(json.dumps([os.getppid(),os.getpid(),grandchild.pid]));temporary.replace(marker)
threading.Event().wait()
'''
            wrapper=('import sys\nfrom pathlib import Path\nsys.path.insert(0,'+repr(str(ROOT))+')\n'
                'from scripts import v3_windows_reliability as d\noriginal=d.subprocess.Popen\n'
                'def spawn(command,**options):\n'
                ' if command[0]=='+repr(str(tool))+':\n'
                '  return original([sys.executable,"-I","-c",'+repr(producer)+','+repr(str(marker))+'],**options)\n'
                ' return original(command,**options)\n'
                'd.subprocess.Popen=spawn\nraise SystemExit(d.run_offline_gzip_child(Path('+repr(str(root))+')))')
            identities=[];handles=[];kernel=None
            if os.name=='nt':
                import ctypes
                from ctypes import wintypes
                kernel=ctypes.WinDLL('kernel32',use_last_error=True)
                kernel.OpenProcess.argtypes=(wintypes.DWORD,wintypes.BOOL,wintypes.DWORD)
                kernel.OpenProcess.restype=wintypes.HANDLE
                kernel.WaitForSingleObject.argtypes=(wintypes.HANDLE,wintypes.DWORD)
                kernel.WaitForSingleObject.restype=wintypes.DWORD
                kernel.TerminateProcess.argtypes=(wintypes.HANDLE,wintypes.UINT)
                kernel.CloseHandle.argtypes=(wintypes.HANDLE,)
            def abort():
                if marker.exists():
                    identities.extend(json.loads(marker.read_text()))
                    if kernel:
                        for pid in identities:
                            handle=kernel.OpenProcess(0x00100001,False,pid)
                            self.assertTrue(handle);handles.append(handle)
                    return 'decode_time_limit'
            try:
                result=diagnostic.run_process([sys.executable,'-I','-c',wrapper],root/'outer.log',
                    dict(os.environ),timeout=30,abort=abort)
                self.assertEqual(result['returncode'],124)
                self.assertEqual(result['timeout_seconds'],30)
                self.assertEqual(len(set(identities)),3)
                self.assertFalse(result['timeout_cleanup']['descendant_cleanup_certified'])
                if kernel:
                    for handle in handles:self.assertEqual(kernel.WaitForSingleObject(handle,3000),0)
                else:
                    for pid in identities:
                        state=subprocess.run(['ps','-p',str(pid),'-o','stat='],capture_output=True,text=True,timeout=3).stdout.strip()
                        self.assertTrue(not state or state.startswith('Z'),state)
            finally:
                if kernel:
                    for handle in handles:
                        if kernel.WaitForSingleObject(handle,0)!=0:
                            kernel.TerminateProcess(handle,1);kernel.WaitForSingleObject(handle,3000)
                        kernel.CloseHandle(handle)
                else:
                    for group in identities[:1]:
                        try:os.killpg(group,__import__('signal').SIGKILL)
                        except ProcessLookupError:pass

    @contextmanager
    def installed_tool(self, root):
        from scripts import v3_windows_reliability as diagnostic
        tool=root/'xperf.exe';tool.write_bytes(b'opaque test tool bytes')
        version=json.dumps({'file_version':'test-file-version','product_version':'test-product-version'})
        with patch.object(diagnostic.platform,'system',return_value='Windows'), \
             patch.object(diagnostic.shutil,'which',return_value=str(tool)), \
             patch.object(diagnostic.subprocess,'run',return_value=subprocess.CompletedProcess([],0,version,'')):
            yield tool

    def archive(self, root, *, member='traces/native-capture.etl', payload=b'opaque ETL fixture', kind=0):
        from scripts import v3_windows_reliability as diagnostic
        source=dict(diagnostic.OFFLINE_ETL_SOURCE)
        archive=root/'source.zip'
        with zipfile.ZipFile(archive,'w') as stream:
            item=zipfile.ZipInfo(member);item.external_attr=kind << 16
            stream.writestr(item,payload)
            stream.writestr('../must-not-extract.txt',b'unrelated archive member')
        source.update(zip_bytes=archive.stat().st_size,
            zip_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
            etl_bytes=len(payload),etl_sha256=hashlib.sha256(payload).hexdigest())
        metadata={'repository':source['repository'],'run_id':source['run_id'],
            'head_sha':source['diagnostic_sha'],'run_status':'completed','artifact':{
                'id':source['artifact_id'],'name':source['artifact_name'],
                'size_in_bytes':source['zip_bytes'],'digest':'sha256:'+source['zip_sha256'],
                'expired':False,'workflow_run':{'id':source['run_id'],'head_sha':source['diagnostic_sha']}}}
        path=root/'source-metadata.json';path.write_text(json.dumps(metadata))
        return archive,path,source

    def test_extracts_only_the_exact_verified_regular_etl_without_following_other_members(self):
        from scripts import v3_windows_reliability as diagnostic
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);archive,metadata,source=self.archive(root)
            output=root/'output';output.mkdir()
            with patch.object(diagnostic,'OFFLINE_ETL_SOURCE',source):
                etl=diagnostic._extract_offline_etl(archive,json.loads(metadata.read_text()),output)
            self.assertEqual(etl.read_bytes(),b'opaque ETL fixture')
            self.assertEqual([p.name for p in output.iterdir()],['native-capture.etl'])
            self.assertFalse((root/'must-not-extract.txt').exists())

    def test_wrong_source_or_archive_or_etl_hash_blocks_before_any_decoder_execution(self):
        from scripts import v3_windows_reliability as diagnostic
        for changed in ('run','artifact','sha','zip-size','zip','etl'):
            with self.subTest(changed=changed),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);archive,metadata,source=self.archive(root)
                value=json.loads(metadata.read_text())
                if changed=='run':value['run_id']+=1
                elif changed=='artifact':value['artifact']['id']+=1
                elif changed=='sha':value['head_sha']='0'*40
                elif changed=='zip-size':archive.write_bytes(archive.read_bytes()+b'changed')
                elif changed=='zip':
                    damaged=bytearray(archive.read_bytes());damaged[-1]^=1;archive.write_bytes(damaged)
                else:source['etl_sha256']='0'*64
                metadata.write_text(json.dumps(value))
                with patch.object(diagnostic,'OFFLINE_ETL_SOURCE',source), \
                     patch.object(diagnostic,'run_process',side_effect=AssertionError('decoder ran before validation')):
                    self.assertEqual(diagnostic.run_offline_decode(archive,metadata,root/'output'),2)
                receipt=json.loads((root/'output'/'decode-receipt.json').read_text())
                self.assertEqual(receipt['status'],'BLOCKED')
                self.assertNotIn('decoder',receipt)
                if changed=='zip':self.assertIn('ZIP SHA256',receipt['error'])

    def test_alias_missing_or_nonregular_selected_member_is_never_extracted(self):
        from scripts import v3_windows_reliability as diagnostic
        for member,kind in (('../native-capture.etl',0),('traces/native-capture.etl',0o120777)):
            with self.subTest(member=member,kind=kind),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);archive,metadata,source=self.archive(root,member=member,kind=kind)
                output=root/'output';output.mkdir()
                with patch.object(diagnostic,'OFFLINE_ETL_SOURCE',source),self.assertRaises(ValueError):
                    diagnostic._extract_offline_etl(archive,json.loads(metadata.read_text()),output)
                self.assertFalse(list(output.iterdir()))

    def test_missing_archive_keeps_a_blocked_receipt_instead_of_losing_failure_evidence(self):
        from scripts import v3_windows_reliability as diagnostic
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);archive,metadata,source=self.archive(root);archive.unlink()
            with patch.object(diagnostic,'OFFLINE_ETL_SOURCE',source):
                self.assertEqual(diagnostic.run_offline_decode(archive,metadata,root/'output'),2)
            receipt=json.loads((root/'output'/'decode-receipt.json').read_text())
            self.assertEqual(receipt['status'],'BLOCKED')
            self.assertIn('FileNotFoundError',receipt['error'])
            self.assertNotIn('decoder',receipt)

    @unittest.skipUnless(os.name=='posix','native metadata alias fixture')
    def test_metadata_alias_is_rejected_before_reading_or_running_the_decoder(self):
        from scripts import v3_windows_reliability as diagnostic
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);archive,metadata,source=self.archive(root)
            alias=root/'alias.json';alias.symlink_to(metadata)
            with patch.object(diagnostic,'OFFLINE_ETL_SOURCE',source):
                self.assertEqual(diagnostic.run_offline_decode(archive,alias,root/'output'),2)
            receipt=json.loads((root/'output'/'decode-receipt.json').read_text())
            self.assertIn('metadata size/type',receipt['error'])

    def test_single_xperf_fileio_dump_keeps_raw_header_30_seconds_and_no_native_claim(self):
        from scripts import v3_windows_reliability as diagnostic
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);archive,metadata,source=self.archive(root);commands=[]
            def run(command,log,env,**options):
                commands.append((command,options))
                self.assertEqual(command[:4],[sys.executable,'-I',str(SCRIPT),'--offline-gzip-child'])
                self.completed_stream(Path(command[4]))
                return {'returncode':0,'timeout_seconds':options['timeout'],'timeout_cleanup':None}
            with patch.object(diagnostic,'OFFLINE_ETL_SOURCE',source), \
                 self.installed_tool(root), \
                 patch.object(diagnostic,'run_process',side_effect=run), \
                 patch.object(diagnostic,'NativeCapture',side_effect=AssertionError('recording started')):
                self.assertEqual(diagnostic.run_offline_decode(archive,metadata,root/'output'),0)
            self.assertEqual(len(commands),1)
            command,options=commands[0]
            self.assertEqual(options['timeout'],30)
            for unwanted in ('-range','-of','-export','-summary','-rts'):
                self.assertNotIn(unwanted,command)
            receipt=json.loads((root/'output'/'decode-receipt.json').read_text())
            self.assertEqual(receipt['status'],'DECODE_COMPLETED_UNANALYZED')
            self.assertFalse(receipt['historical_cause_established'])
            self.assertFalse(receipt['native_fields_validated'])
            self.assertEqual(receipt['actual_product_sha'],'842588da6e4d98b2b28601af4e4c40796f68b481')
            self.assertEqual(receipt['source_capture_head'],'8b5d07032d9a2dac9ecc2ca73662840231e97961')
            self.assertEqual(receipt['storage_limit_bytes'],512*1024*1024)
            self.assertEqual(receipt['stream']['raw_bytes'],18)
            self.assertIn('native-events.csv.gz',receipt['outputs'])
            self.assertFalse((root/'output'/'native-events.csv').exists())
            self.assertEqual(receipt['decoder_tool']['sha256'],hashlib.sha256(b'opaque test tool bytes').hexdigest())
            self.assertEqual(receipt['decoder_tool']['file_version'],'test-file-version')
            self.assertFalse((root/'output'/'native-schema.man').exists())
            with self.assertRaises(FileExistsError):
                diagnostic.run_offline_decode(archive,metadata,root/'output')

    def test_relative_cli_paths_reach_the_decoder_from_its_existing_repository_working_directory(self):
        from contextlib import chdir
        from scripts import v3_windows_reliability as diagnostic
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);archive,metadata,source=self.archive(root)
            def run(command,*args,**options):
                path=Path(command[4])/'native-capture.etl';path=path if path.is_absolute() else diagnostic.ROOT/path
                self.assertTrue(path.is_file(),'decoder cwd cannot reach the verified ETL')
                self.completed_stream(path.parent)
                return {'returncode':0,'timeout_cleanup':None}
            with chdir(root),patch.object(diagnostic,'OFFLINE_ETL_SOURCE',source), \
                 self.installed_tool(root), \
                 patch.object(diagnostic,'run_process',side_effect=run):
                self.assertEqual(diagnostic.run_offline_decode(Path('source.zip'),Path('source-metadata.json'),Path('output')),0)

    def test_decode_timeout_retains_partial_output_and_actual_cleanup_uncertainty(self):
        from scripts import v3_windows_reliability as diagnostic
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);archive,metadata,source=self.archive(root)
            def timeout(command,*args,**options):
                Path(command[4],'native-events.csv.gz').write_bytes(b'partial')
                return {'returncode':124,'timeout_seconds':30,'abort_reason':None,
                        'timeout_cleanup':{'descendant_cleanup_certified':False,'method':'taskkill_tree'}}
            with patch.object(diagnostic,'OFFLINE_ETL_SOURCE',source), \
                 self.installed_tool(root), \
                 patch.object(diagnostic,'run_process',side_effect=timeout):
                self.assertEqual(diagnostic.run_offline_decode(archive,metadata,root/'output'),2)
            receipt=json.loads((root/'output'/'decode-receipt.json').read_text())
            self.assertEqual(receipt['status'],'BLOCKED')
            self.assertTrue(receipt['cleanup_uncertified'])
            self.assertEqual(receipt['decoder']['timeout_cleanup']['method'],'taskkill_tree')
            self.assertEqual((root/'output'/'native-events.csv.gz').read_bytes(),b'partial')

    def test_decoder_storage_growth_and_missing_csv_remain_blocked(self):
        from scripts import v3_windows_reliability as diagnostic
        for mode in ('growth','missing-csv'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);archive,metadata,source=self.archive(root)
                limit=source['zip_bytes']+source['etl_bytes']+8 if mode=='growth' else 512*1024*1024
                def run(command,*args,**options):
                    if mode=='growth':Path(command[4],'native-events.csv.gz').write_bytes(b'x'*100)
                    reason=options['abort']()
                    self.assertEqual(reason,'decode_storage_limit' if mode=='growth' else None)
                    return {'returncode':124 if reason else 0,'abort_reason':reason,'timeout_cleanup':None}
                with patch.object(diagnostic,'OFFLINE_ETL_SOURCE',source), \
                     patch.object(diagnostic,'OFFLINE_STORAGE_LIMIT',limit), \
                     self.installed_tool(root), \
                     patch.object(diagnostic,'run_process',side_effect=run):
                    self.assertEqual(diagnostic.run_offline_decode(archive,metadata,root/'output'),2)
                receipt=json.loads((root/'output'/'decode-receipt.json').read_text())
                self.assertEqual(receipt['status'],'BLOCKED')
                if mode=='growth':self.assertEqual(receipt['outputs']['native-events.csv.gz']['bytes'],100)

    def test_duplicate_canonical_member_cannot_replace_the_single_frozen_etl(self):
        import warnings
        from scripts import v3_windows_reliability as diagnostic
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);archive,metadata,source=self.archive(root)
            with warnings.catch_warnings():
                warnings.simplefilter('ignore',UserWarning)
                with zipfile.ZipFile(archive,'a') as stream:
                    stream.writestr(source['etl_member'],b'opaque ETL fixture')
            source.update(zip_bytes=archive.stat().st_size,zip_sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
            value=json.loads(metadata.read_text());value['artifact'].update(
                size_in_bytes=source['zip_bytes'],digest='sha256:'+source['zip_sha256'])
            output=root/'output';output.mkdir()
            with patch.object(diagnostic,'OFFLINE_ETL_SOURCE',source),self.assertRaises(ValueError):
                diagnostic._extract_offline_etl(archive,value,output)
            self.assertFalse(list(output.iterdir()))

    def test_frozen842_identity_and_authenticated_offline_path_alone_use_30_seconds(self):
        from scripts import v3_windows_reliability as diagnostic
        source=diagnostic.OFFLINE_ETL_SOURCE
        self.assertEqual((source['run_id'],source['artifact_id']),(42626720110,13678703440))
        self.assertEqual(source['zip_sha256'],'23043cce643822e8dea7499b8cb33e7f152a3a29f3d2f943bf7e0740b89f597b')
        self.assertEqual((source['zip_bytes'],source['etl_bytes']),(44221984,119013376))
        self.assertEqual(source['etl_sha256'],'c9aa451d118ae919743b11c025a2f44a06a6927a5731838cc1ec2a32d56b2cc0')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);archive,metadata,fixture=self.archive(root)
            def run(command,*args,**options):
                self.assertEqual(options['timeout'],30)
                self.completed_stream(Path(command[4]))
                return {'returncode':0,'timeout_cleanup':None}
            with patch.object(diagnostic,'OFFLINE_ETL_SOURCE',fixture),self.installed_tool(root), \
                 patch.object(diagnostic,'run_process',side_effect=run) as decoder, \
                 patch.object(diagnostic,'NativeCapture',side_effect=AssertionError('capture invoked')), \
                 patch.object(diagnostic,'run_native_comparison',side_effect=AssertionError('campaign invoked')), \
                 patch.object(diagnostic,'run_child',side_effect=AssertionError('product child invoked')):
                self.assertEqual(diagnostic.run_offline_decode(archive,metadata,root/'accepted'),0)
                receipt=json.loads((root/'accepted/decode-receipt.json').read_text())
                self.assertEqual(receipt['decode_timeout_seconds'],30)
                bad=json.loads(metadata.read_text());bad['artifact']['digest']='sha256:'+'0'*64
                metadata.write_text(json.dumps(bad))
                self.assertEqual(diagnostic.run_offline_decode(archive,metadata,root/'rejected'),2)
                self.assertEqual(decoder.call_count,1)

    def test_cli_offline_branch_never_enters_product_or_capture_campaign(self):
        from scripts import v3_windows_reliability as diagnostic
        with patch.object(sys,'argv',[str(SCRIPT),'--output','new-output',
                '--offline-decode-archive','source.zip','--offline-source-metadata','source.json']), \
             patch.object(diagnostic,'run_offline_decode',return_value=0), \
             patch.object(diagnostic,'run_native_comparison',side_effect=AssertionError('product campaign ran')), \
             patch.object(diagnostic,'historical_environment',side_effect=AssertionError('product dependencies read')):
            self.assertEqual(diagnostic.main(),0)

    def test_internal_gzip_cli_enters_only_the_stream_child(self):
        from scripts import v3_windows_reliability as diagnostic
        with patch.object(sys,'argv',[str(SCRIPT),'--offline-gzip-child','stream-output']), \
             patch.object(diagnostic,'run_offline_gzip_child',return_value=0), \
             patch.object(diagnostic,'run_offline_decode',side_effect=AssertionError('parent source stage entered')), \
             patch.object(diagnostic,'run_native_comparison',side_effect=AssertionError('product campaign entered')):
            self.assertEqual(diagnostic.main(),0)

    def test_missing_xperf_blocks_without_installing_or_executing_a_decoder(self):
        from scripts import v3_windows_reliability as diagnostic
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);archive,metadata,source=self.archive(root)
            with patch.object(diagnostic,'OFFLINE_ETL_SOURCE',source), \
                 patch.object(diagnostic.platform,'system',return_value='Windows'), \
                 patch.dict(os.environ,{'ProgramFiles(x86)':str(root/'unavailable')}), \
                 patch.object(diagnostic.shutil,'which',return_value=None), \
                 patch.object(diagnostic,'run_process',side_effect=AssertionError('missing decoder executed')):
                self.assertEqual(diagnostic.run_offline_decode(archive,metadata,root/'output'),2)
            receipt=json.loads((root/'output'/'decode-receipt.json').read_text())
            self.assertIn('xperf',receipt['error'])
            self.assertNotIn('decoder',receipt)

    def test_tool_metadata_and_dump_share_the_same_30_second_active_deadline(self):
        from scripts import v3_windows_reliability as diagnostic
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);archive,metadata,source=self.archive(root)
            def run(command,*args,**options):
                reason=options['abort']()
                self.assertEqual(reason,'decode_time_limit')
                return {'returncode':124,'abort_reason':reason,'timeout_cleanup':None}
            with patch.object(diagnostic,'OFFLINE_ETL_SOURCE',source),self.installed_tool(root), \
                 patch.object(diagnostic.time,'monotonic',side_effect=(10,41)), \
                 patch.object(diagnostic,'run_process',side_effect=run):
                self.assertEqual(diagnostic.run_offline_decode(archive,metadata,root/'output'),2)
            receipt=json.loads((root/'output'/'decode-receipt.json').read_text())
            self.assertEqual(receipt['decoder']['abort_reason'],'decode_time_limit')
            self.assertEqual(receipt['status'],'BLOCKED')

    def test_metadata_timeout_retains_phase_and_uncertified_cleanup_without_starting_dump(self):
        from scripts import v3_windows_reliability as diagnostic
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);archive,metadata,source=self.archive(root)
            with patch.object(diagnostic,'OFFLINE_ETL_SOURCE',source),self.installed_tool(root), \
                 patch.object(diagnostic.subprocess,'run',side_effect=subprocess.TimeoutExpired([sys.executable,'-I','-c','native version query'],5)), \
                 patch.object(diagnostic,'run_process',side_effect=AssertionError('dump started after metadata timeout')):
                self.assertEqual(diagnostic.run_offline_decode(archive,metadata,root/'output'),2)
            receipt=json.loads((root/'output'/'decode-receipt.json').read_text())
            self.assertEqual(receipt['status'],'BLOCKED')
            self.assertTrue(receipt['cleanup_uncertified'])
            self.assertEqual(receipt['error_phase'],'tool_metadata')
            self.assertIn('metadata',receipt['cleanup_observation'])
            self.assertIn('TimeoutExpired',receipt['error'])
            self.assertNotIn('decoder',receipt)
            info=json.loads((root/'output'/'xperf-tool.json').read_text())
            self.assertEqual(info['version_status'],'TIMED_OUT')
            self.assertEqual(info['metadata_timeout_seconds'],5)
            self.assertTrue(info['metadata_cleanup_uncertified'])

    def test_known_toolkit_path_is_fingerprinted_and_unknown_version_is_not_accepted(self):
        from scripts import v3_windows_reliability as diagnostic
        for version_ok in (True,False):
            with self.subTest(version_ok=version_ok),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);archive,metadata,source=self.archive(root)
                tool=root/'Windows Kits'/'10'/'Windows Performance Toolkit'/'xperf.exe'
                tool.parent.mkdir(parents=True);tool.write_bytes(b'known path test tool')
                version=json.dumps({'file_version':'test-file','product_version':'test-product'} if version_ok else {})
                def run(command,*args,**options):
                    self.assertEqual(command[3],'--offline-gzip-child')
                    self.completed_stream(Path(command[4]))
                    return {'returncode':0,'timeout_cleanup':None}
                with patch.object(diagnostic,'OFFLINE_ETL_SOURCE',source), \
                     patch.object(diagnostic.platform,'system',return_value='Windows'), \
                     patch.dict(os.environ,{'ProgramFiles(x86)':str(root)}), \
                     patch.object(diagnostic.shutil,'which',return_value=None), \
                     patch.object(diagnostic.subprocess,'run',return_value=subprocess.CompletedProcess([],0,version,'')), \
                     patch.object(diagnostic,'run_process',side_effect=run):
                    self.assertEqual(diagnostic.run_offline_decode(archive,metadata,root/'output'),0 if version_ok else 2)
                info=json.loads((root/'output'/'xperf-tool.json').read_text())
                self.assertEqual(info['path'],str(tool.resolve()))
                self.assertEqual(info['sha256'],hashlib.sha256(b'known path test tool').hexdigest())
                self.assertEqual(info['version_status'],'READ' if version_ok else 'UNAVAILABLE')

    def test_parent_never_accepts_missing_partial_error_or_nonzero_child_gzip_results(self):
        from scripts import v3_windows_reliability as diagnostic
        for mode in ('missing','partial','scope','error','nonzero','uncertified','uncertified-null','footer','raw-hash','not-object'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);archive,metadata,source=self.archive(root)
                def run(command,*args,**options):
                    output=Path(command[4]);state=self.completed_stream(output)
                    if mode=='missing':(output/'gzip-stream.json').unlink()
                    else:
                        if mode=='partial':state['stdout_eof']=False
                        elif mode=='scope':state['raw_hash_scope']='observed_prefix'
                        elif mode=='error':state['error']='stream error'
                        elif mode=='nonzero':state['xperf_returncode']=7
                        elif mode=='uncertified':state['cleanup_uncertified']=True
                        elif mode=='uncertified-null':state['cleanup_uncertified']=None
                        elif mode=='raw-hash':state.pop('raw_sha256')
                        elif mode=='not-object':state=[]
                        elif mode=='footer':
                            path=output/'native-events.csv.gz';data=bytearray(path.read_bytes());data[-1]^=1;path.write_bytes(data)
                            state.update(gzip_sha256=hashlib.sha256(data).hexdigest())
                        (output/'gzip-stream.json').write_text(json.dumps(state))
                    return {'returncode':0,'timeout_cleanup':None}
                with patch.object(diagnostic,'OFFLINE_ETL_SOURCE',source),self.installed_tool(root), \
                     patch.object(diagnostic,'run_process',side_effect=run):
                    self.assertEqual(diagnostic.run_offline_decode(archive,metadata,root/'output'),2)
                self.assertEqual(json.loads((root/'output'/'decode-receipt.json').read_text())['status'],'BLOCKED')


class WindowsReliabilityDiagnosticTests(unittest.TestCase):
    @staticmethod
    def _historical_archive(revision):
        return subprocess.run(['git', '-c', 'core.autocrlf=false', '-c', 'core.eol=lf',
                               'archive', '--format=zip', revision], cwd=ROOT,
                              capture_output=True, check=True, timeout=10).stdout

    def test_historical_archive_ignores_inherited_windows_line_endings(self):
        with patch.dict(os.environ, {'GIT_CONFIG_COUNT': '1',
                                     'GIT_CONFIG_KEY_0': 'core.autocrlf', 'GIT_CONFIG_VALUE_0': 'true'}):
            archive = self._historical_archive('0f172f9fe692ff8fafcc0a15a72fe51eaf67e977')
        with zipfile.ZipFile(io.BytesIO(archive)) as source:
            fixture = source.read('tests/test_delegation.py')
        self.assertEqual(hashlib.sha256(fixture).hexdigest(),
                         '7484131880e91a328961ec0b8b99fe2bcaad8ded19b2ebe8efd944028f2aa3a7')

    def test_readonly_anchor_materializes_wal_without_a_transaction_and_final_writer_close_checkpoints(self):
        import sqlite3
        from scripts.v3_windows_reliability import LoadControl, Trace
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / 'state.sqlite3'
            wal = Path(str(database) + '-wal')
            connection = sqlite3.connect(database)
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('CREATE TABLE probe(value TEXT)')
            connection.commit()
            connection.close()
            self.assertFalse(wal.exists())
            output = root / 'evidence'
            output.mkdir()
            trace = Trace(output)
            control = LoadControl('baseline', 0, types.SimpleNamespace(root=root), trace,
                                  readonly_anchor=True)
            try:
                control.start()
                anchor = control.anchor
                self.assertFalse(anchor.in_transaction)
                writer = sqlite3.connect(database, timeout=.05)
                try:
                    writer.execute("INSERT INTO probe VALUES('committed')")
                    writer.commit()
                finally:
                    writer.close()
                self.assertTrue(wal.is_file(), 'Materialized anchor must prevent last-client WAL cleanup')
                self.assertEqual(anchor.execute('SELECT value FROM probe').fetchall(), [('committed',)])
                self.assertFalse(anchor.in_transaction)
                with self.assertRaises(sqlite3.OperationalError):
                    anchor.execute("INSERT INTO probe VALUES('forbidden')")
                self.assertEqual(anchor.total_changes, 0)
            finally:
                control.close()
                trace.stream.close()
            self.assertTrue(wal.exists(), 'The final read-only client cannot checkpoint the WAL')
            with self.assertRaises(sqlite3.ProgrammingError):
                anchor.execute('SELECT 1')
            connection = sqlite3.connect(database)
            try:
                self.assertEqual(connection.execute('SELECT value FROM probe').fetchall(), [('committed',)])
            finally:
                connection.close()
            self.assertFalse(wal.exists(), 'A final writable client must checkpoint the committed WAL')

    def test_paired_anchor_reports_counterbalanced_order_and_identical_bounded_cpu_load(self):
        from scripts import v3_windows_reliability as diagnostic
        commands = []
        def run(command, log_path, env):
            commands.append(command)
            return {'returncode': 0, 'elapsed_ns': 1, 'command': command, 'timeout_cleanup': None}
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(sys, 'argv', [str(SCRIPT), '--output', directory, '--iterations', '2',
                 '--control', 'paired-anchor', '--interleave-reviewer']), \
             patch.object(diagnostic, 'run_process', side_effect=run):
            self.assertEqual(diagnostic.main(), 0)
            report = json.loads((Path(directory) / 'report.json').read_text())
            self.assertEqual(report['execution_order'], [
                'cancellation-1-baseline', 'reviewer-1-baseline',
                'cancellation-1-anchor', 'reviewer-1-anchor',
                'cancellation-2-anchor', 'reviewer-2-anchor',
                'cancellation-2-baseline', 'reviewer-2-baseline'])
            self.assertEqual([row['variant'] for row in report['iterations']],
                             ['baseline', 'anchor', 'anchor', 'baseline'])
            cancellations = commands[::2]
            self.assertTrue(all(command[command.index('--control') + 1] == 'cpu' for command in cancellations))
            self.assertEqual(['--readonly-anchor' in command for command in cancellations],
                             [False, True, True, False])
            with patch.object(sys, 'argv', [str(SCRIPT), '--output', directory,
                    '--control', 'paired-anchor', '--iterations', '11']):
                with self.assertRaises(SystemExit) as error:
                    diagnostic.main()
                self.assertEqual(error.exception.code, 2)

    def test_last_client_checkpoint_latency_hits_original_deadline_until_readonly_anchor(self):
        from scripts.v3_windows_reliability import isolated_environment, run_process
        code = """
from pathlib import Path
import sqlite3, sys, threading, time
from unittest.mock import patch
import scripts.v3_windows_reliability as harness
repository = Path(sys.argv[3]).resolve()
harness.ROOT = repository
sys.path[:0] = [str(repository), str(repository / 'tests')]
from scripts.v3_windows_reliability import Trace, run_child
import openkyrozen
assert Path(openkyrozen.__file__).resolve().is_relative_to(repository)
print('Fixture product source:', openkyrozen.__file__, flush=True)

active, injected = threading.Event(), threading.Event()
state = {}
original_record, original_connect = Trace.record, sqlite3.connect

def record(trace, event, **fields):
    state['trace'] = trace
    if event == 'cancel.return':
        active.set()
    original_record(trace, event, **fields)

class CheckpointLatency(sqlite3.Connection):
    def __init__(self, database, *args, **kwargs):
        super().__init__(database, *args, **kwargs)
        self.fixture = str(database).endswith(('state.sqlite3', 'state.sqlite3?mode=ro'))

    def close(self):
        trace = state.get('trace')
        wal = trace.case.root / 'state.sqlite3-wal' if trace and active.is_set() and self.fixture else None
        size = wal.stat().st_size if wal and wal.is_file() else 0
        super().close()
        # A dirty WAL disappearing proves this close performed real last-client checkpoint cleanup.
        if size > 32 and not wal.exists() and not injected.is_set():
            injected.set()
            trace.record('injected_checkpoint_close.call', wal_bytes=size)
            time.sleep(5.3)
            trace.record('injected_checkpoint_close.return')

def connect(*args, **kwargs):
    kwargs['factory'] = CheckpointLatency
    return original_connect(*args, **kwargs)

with patch.object(Trace, 'record', record), patch.object(sqlite3, 'connect', connect):
    raise SystemExit(run_child(Path(sys.argv[1]), readonly_anchor=sys.argv[2] == 'anchor'))
"""
        with tempfile.TemporaryDirectory() as directory:
            revision = '0f172f9fe692ff8fafcc0a15a72fe51eaf67e977'
            legacy = Path(directory) / 'historical-product'
            archive = self._historical_archive(revision)
            with zipfile.ZipFile(io.BytesIO(archive)) as source:
                self.assertTrue(all(not Path(name).is_absolute() and '..' not in Path(name).parts
                                    for name in source.namelist()))
                source.extractall(legacy)
            self.assertEqual(hashlib.sha256((legacy / 'tests/test_delegation.py').read_bytes()).hexdigest(),
                             '7484131880e91a328961ec0b8b99fe2bcaad8ded19b2ebe8efd944028f2aa3a7')
            evidence = Path(os.environ.get('KYROZEN_DIAGNOSTIC_TEST_OUTPUT', directory))
            evidence = evidence / f'checkpoint-close-{time.time_ns()}'
            env = isolated_environment(Path(directory) / 'home')
            for variant, expected in (('baseline', 1), ('anchor', 0), ('current', 0)):
                repository = ROOT if variant == 'current' else legacy
                output = evidence / variant
                output.mkdir(parents=True)
                process = run_process([sys.executable, '-c', code, str(output), variant, str(repository)],
                                      output / 'process.log', env)
                (output / 'child-process-result.json').write_text(json.dumps(
                    {**process, 'expected_returncode': expected, 'readonly_anchor': variant == 'anchor',
                     'product_revision': 'current-checkout' if variant == 'current' else revision,
                     'fixture_sha256': hashlib.sha256((repository / 'tests/test_delegation.py').read_bytes()).hexdigest(),
                     'database_source_sha256': hashlib.sha256((repository / 'openkyrozen/persistence/database.py').read_bytes()).hexdigest()}, indent=2))
                self.assertEqual(process['returncode'], expected, (output / 'process.log').read_text())
                log = (output / 'unittest.log').read_text()
                events = [json.loads(line)['event'] for line in (output / 'events.jsonl').read_text().splitlines()]
                state = json.loads((output / 'state-before-cleanup.json').read_text())
                self.assertEqual(state['marker'], 'ok')
                self.assertEqual(state['persisted_states'][-1]['status'], 'cancelled')
                if variant == 'baseline':
                    self.assertIn('TimeoutError', log)
                    self.assertIn('injected_checkpoint_close.call', events)
                    self.assertLess(events.index('injected_checkpoint_close.call'), events.index('future_wait.return'))
                elif variant == 'anchor':
                    self.assertNotIn('TimeoutError', log)
                    self.assertNotIn('injected_checkpoint_close.call', events)
                    self.assertLess(events.index('future_wait.return'), events.index('anchor.close.call'))
                else:
                    self.assertNotIn('TimeoutError', log)
                    for index, event in enumerate(events):
                        if event == 'injected_checkpoint_close.call':
                            self.assertLess(events.index('future_wait.return'), index)

    def test_detailed_profiler_starts_after_fixture_setup(self):
        import sqlite3
        from scripts.v3_windows_reliability import run_child

        class Fixture(unittest.TestCase):
            def setUp(self):
                self.root = root
                connection = sqlite3.connect(':memory:')
                connection.close()

            def test_cancel_during_provider_call_prevents_late_write(self):
                connection = sqlite3.connect(':memory:')
                connection.close()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = types.SimpleNamespace(DelegationTests=Fixture)
            with patch.dict(sys.modules, {'test_delegation': fixture}):
                self.assertEqual(run_child(root), 0)
            names = [json.loads(line)['event'] for line in (root / 'events.jsonl').read_text().splitlines()]
            start = names.index('setup.return')
            self.assertFalse(any(name.startswith('sqlite.') for name in names[:start]))
            self.assertIn('sqlite.connect.c_call', names[start:names.index('cleanup.call')])

    def test_historical_pin_export_includes_preinstalled_dependencies_and_excludes_product(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'requirements.txt'
            process = subprocess.run([sys.executable, str(SCRIPT), '--write-historical-requirements', str(path)],
                                     capture_output=True, text=True, timeout=5)
            self.assertEqual(process.returncode, 0, process.stderr)
            requirements = path.read_text().splitlines()
            for requirement in ('openai==3.42.1', 'anthropic==2.3.0', 'packaging==26.3',
                                'colorama==0.4.6', 'click==8.5.0', 'filelock==4.2.4'):
                self.assertIn(requirement, requirements)
            self.assertFalse(any(item.startswith('openkyrozen==') for item in requirements))

    def test_historical_environment_mismatch_stops_before_running_any_fixture(self):
        from scripts import v3_windows_reliability as diagnostic
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(sys, 'argv', [str(SCRIPT), '--output', directory,
                    '--iterations', '1', '--historical-environment']), \
                 patch.object(diagnostic.platform, 'python_version', return_value='3.12.99'):
                self.assertEqual(diagnostic.main(), 2)
            root = Path(directory)
            environment = json.loads((root / 'historical-environment.json').read_text())
            self.assertEqual(environment['status'], 'INFRASTRUCTURE_FAILURE')
            self.assertFalse((root / '1').exists())

    def test_child_environment_uses_private_windows_home_and_scrubs_credentials(self):
        from scripts.v3_windows_reliability import isolated_environment
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                'USERPROFILE': '/original-user', 'HOME': '/original-home',
                'DEEPSEEK_API_KEY': 'must-not-inherit', 'GH_TOKEN': 'must-not-inherit',
                'KYROZEN_DB_PATH': '/original.sqlite3'}):
            env = isolated_environment(Path(directory))
            self.assertEqual(env['HOME'], directory)
            self.assertEqual(env['USERPROFILE'], directory)
            self.assertEqual(env['KYROZEN_DB_PATH'], str(Path(directory) / 'runtime.sqlite3'))
            self.assertTrue(Path(env['KYROZEN_WORKSPACE_ROOT']).is_dir())
            self.assertNotIn('DEEPSEEK_API_KEY', env)
            self.assertNotIn('GH_TOKEN', env)

    def test_historical_environment_rejects_runtime_and_dependency_substitutions(self):
        from scripts.v3_windows_reliability import historical_environment
        with patch('scripts.v3_windows_reliability.platform.python_version', return_value='3.12.10'), \
             patch('scripts.v3_windows_reliability.platform.system', return_value='Windows'), \
             patch('scripts.v3_windows_reliability.metadata.version', return_value='wrong-version'):
            report = historical_environment()
        self.assertEqual(report['status'], 'INFRASTRUCTURE_FAILURE')
        self.assertIn('openai', report['dependency_mismatches'])
        self.assertEqual(report['dependency_mismatches']['openai']['expected'], '3.42.1')
        self.assertEqual(report['dependency_mismatches']['anthropic']['expected'], '2.3.0')
        with patch('scripts.v3_windows_reliability.platform.python_version', return_value='3.12.13'):
            report = historical_environment()
        self.assertEqual(report['status'], 'INFRASTRUCTURE_FAILURE')
        self.assertEqual(report['python'], '3.12.13')

    def test_interleaved_run_keeps_original_order_and_traces_post_cancel_phases(self):
        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.run([sys.executable, str(SCRIPT), '--output', directory,
                '--iterations', '2', '--repository', str(ROOT), '--interleave-reviewer'],
                cwd=ROOT, capture_output=True, text=True, timeout=45)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            root = Path(directory)
            report = json.loads((root / 'report.json').read_text())
            self.assertFalse(report['historical_cause_established'])
            self.assertEqual(report['execution_order'], [
                'cancellation-1', 'reviewer-1', 'cancellation-2', 'reviewer-2'])
            for index in (1, 2):
                result = report['iterations'][index - 1]
                self.assertEqual(result['reviewer']['returncode'], 0)
                self.assertIn('Sub-agent workflow acceptance passed.',
                              (root / str(index) / 'reviewer.log').read_text())
                events = [json.loads(line) for line in
                          (root / str(index) / 'events.jsonl').read_text().splitlines()]
                names = [item['event'] for item in events]
                for name in ('provider_wait.call', 'provider_check.call', 'usage_persist.call', 'usage_summary.call',
                             'sqlite.execute.c_call', 'sqlite.close.c_return'):
                    self.assertIn(name, names)
                self.assertTrue(all('thread_cpu_ns' in item for item in events))

    @unittest.skipUnless(os.name == 'posix', 'real owned process-group cleanup requires POSIX')
    def test_diagnostic_timeout_cleanup_stops_owned_descendant(self):
        from scripts.v3_windows_reliability import stop_diagnostic_tree
        code = '''
import subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
Path(sys.argv[1]).write_text(str(child.pid))
time.sleep(30)
'''
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'child-pid'
            process = subprocess.Popen([sys.executable, '-c', code, str(marker)],
                                       start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 3
                while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(marker.exists())
                child_pid = int(marker.read_text())
                cleanup = stop_diagnostic_tree(process)
                process.communicate(timeout=3)
                state = subprocess.run(['ps', '-p', str(child_pid), '-o', 'stat='],
                                       capture_output=True, text=True, timeout=3).stdout.strip()
                self.assertTrue(not state or state.startswith('Z'), state)
                self.assertFalse(cleanup['descendant_cleanup_certified'])
                self.assertIsNotNone(process.returncode)
            finally:
                stop_diagnostic_tree(process)
                process.communicate(timeout=3)

    def test_cpu_control_exits_at_its_own_deadline_without_parent_stop(self):
        from scripts.v3_windows_reliability import _CPU_LOAD
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = subprocess.run([sys.executable, '-c', _CPU_LOAD,
                str(root / 'ready'), str(root / 'never-stopped'), str(root / 'result.json'), '.15'],
                capture_output=True, text=True, timeout=3)
            self.assertEqual(result.returncode, 0, result.stderr)
            metrics = json.loads((root / 'result.json').read_text())
            self.assertEqual(metrics['stopped_by'], 'deadline')
            self.assertLess(metrics['wall_seconds'], 2)

    def test_explicit_load_control_records_real_contention_and_owned_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.run([sys.executable, str(SCRIPT), '--output', directory,
                '--iterations', '1', '--repository', str(ROOT), '--control', 'combined',
                '--cpu-workers', '1'], cwd=ROOT, capture_output=True, text=True, timeout=40)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            report = json.loads((Path(directory) / 'report.json').read_text())
            self.assertEqual(report['control'], 'combined')
            self.assertFalse(report['historical_cause_established'])
            result = json.loads((Path(directory) / '1' / 'load-control.json').read_text())
            self.assertEqual(result['control'], 'combined')
            self.assertGreaterEqual(result['sqlite']['acquired_transactions'], 1)
            self.assertTrue(result['sqlite']['thread_stopped'])
            self.assertEqual(len(result['cpu']), 1)
            self.assertEqual(result['cpu'][0]['returncode'], 0)
            self.assertGreater(result['cpu'][0]['cpu_seconds'], 0)
            self.assertGreater(result['cpu'][0]['wall_seconds'], 0)
            events = [json.loads(line)['event'] for line in
                      (Path(directory) / '1' / 'events.jsonl').read_text().splitlines()]
            self.assertIn('sqlite.lock_acquired', events)
            self.assertIn('sqlite.lock_released', events)
            self.assertIn('load.started', events)
            self.assertIn('load.stopped', events)
            state = json.loads((Path(directory) / '1' / 'state-before-cleanup.json').read_text())
            self.assertEqual(state['marker'], 'ok')

    def test_actual_cancellation_fixture_emits_boundary_and_persisted_evidence(self):
        self.assertTrue(SCRIPT.is_file(), 'Windows diagnostic harness is missing')
        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.run([sys.executable, str(SCRIPT), '--output', directory,
                                      '--iterations', '1', '--repository', str(ROOT)], cwd=ROOT, capture_output=True,
                                     text=True, timeout=40)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            path = Path(directory)
            report = json.loads((path / 'report.json').read_text())
            self.assertFalse(report['historical_cause_established'])
            self.assertEqual(report['iterations'][0]['returncode'], 0)
            events = [json.loads(line) for line in (path / '1' / 'events.jsonl').read_text().splitlines()]
            names = [event['event'] for event in events]
            for name in ('dispatch.call', 'dispatch.return', 'provider.call', 'cancel.call',
                         'cancel.return', 'release.call', 'provider.return', 'worker.return', 'future_wait.call',
                         'cleanup.call', 'cleanup.return'):
                self.assertIn(name, names)
            self.assertLess(names.index('provider.call'), names.index('cancel.call'))
            self.assertLess(names.index('cancel.return'), names.index('release.call'))
            self.assertTrue(all('monotonic_ns' in event and 'thread_id' in event for event in events))
            state = json.loads((path / '1' / 'state-before-cleanup.json').read_text())
            self.assertEqual(state['marker'], 'ok')
            self.assertTrue(state['persisted_states'])
            self.assertEqual(state['persisted_states'][-1]['status'], 'cancelled')
            self.assertTrue((path / '1' / 'threads.log').is_file())
            self.assertEqual(report['test_timeouts_seconds'], {'provider_wait': 3, 'entry_wait': 10, 'future_wait': 5})

    def test_slow_worker_completion_keeps_original_timeout_and_captures_pending_worker(self):
        code = """
from pathlib import Path
import faulthandler
import sys
import time
from unittest.mock import patch
from scripts.v3_windows_reliability import Trace, run_child

original = Trace.record

def delay_worker_completion(trace, event, **fields):
    original(trace, event, **fields)
    if event == 'worker.return':
        time.sleep(5.3)

with patch.object(Trace, 'record', delay_worker_completion), \
     patch.object(faulthandler, 'dump_traceback_later', side_effect=AssertionError('native watchdog forbidden')), \
     patch.object(faulthandler, 'cancel_dump_traceback_later', side_effect=AssertionError('native watchdog forbidden')):
    result = run_child(Path(sys.argv[1]))
print('native watchdog unused', flush=True)
raise SystemExit(result)
"""
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(os.environ.get('KYROZEN_DIAGNOSTIC_TEST_OUTPUT', directory))
            evidence = evidence / f'native-watchdog-{time.time_ns()}'
            for iteration in range(3):
                path = evidence / str(iteration)
                path.mkdir(parents=True)
                with (path / 'process.log').open('w') as log:
                    process = subprocess.run([sys.executable, '-c', code, str(path)], cwd=ROOT,
                                             stdout=log, stderr=subprocess.STDOUT, timeout=20)
                output = (path / 'process.log').read_text()
                self.assertIn('native watchdog unused', output)
                self.assertEqual(process.returncode, 1, output)
                self.assertIn('TimeoutError', (path / 'unittest.log').read_text())
                self.assertIn('Timeout (0:00:04)', (path / 'threads.log').read_text())
                self.assertTrue((path / 'state-watchdog-1.json').exists())
                state = json.loads((path / 'state-before-cleanup.json').read_text())
                self.assertEqual(state['marker'], 'ok')
                self.assertTrue(any(not run['future_done'] for run in state['runs']))
                self.assertEqual(state['persisted_states'][-1]['status'], 'cancelled')


class NativeVfsDiagnosticTests(unittest.TestCase):
    def test_selected_close_keeps_first_pair_when_later_connection_id_is_reused(self):
        import threading
        with tempfile.TemporaryDirectory() as directory:
            control,_=self.control(Path(directory),'wal',types.SimpleNamespace())
            control.evidence.update(connection_id=7,worker_native_tid=threading.get_native_id())
            for end_event in ('c_return','c_exception'):
                with self.subTest(end_event=end_event):
                    for key in ('close_call_monotonic_ns','close_return_monotonic_ns',
                                'close_interval_ns','close_event'):
                        control.evidence.pop(key,None)
                    control.note_close('c_return',7,at_ns=290_400_000_000)
                    control.note_close('c_call',8,at_ns=290_410_000_000)
                    control.note_close('c_call',7,at_ns=290_421_000_000)
                    control.note_close(end_event,7,at_ns=291_375_000_000)
                    control.note_close('c_call',7,at_ns=291_390_000_000)
                    control.note_close('c_return',7,at_ns=291_406_000_000)
                    self.assertEqual(control.evidence['close_call_monotonic_ns'],290_421_000_000)
                    self.assertEqual(control.evidence['close_return_monotonic_ns'],291_375_000_000)
                    self.assertEqual(control.evidence['close_interval_ns'],954_000_000)
                    self.assertEqual(control.evidence['close_event'],end_event)

    def test_owned_handle_drains_after_original_application_close_before_fixture_file_cleanup(self):
        from scripts import v3_windows_reliability as diagnostic
        order=[];held=False;holder=None;fixture_root=None
        class API:
            def open(self,path,probe=False):
                nonlocal held
                if probe:return None,32
                held=True;return 123,0
            def close(self,handle):
                nonlocal held
                order.append('handle.close');held=False
            def qpc(self):return time.perf_counter_ns()
        class Fixture(unittest.TestCase):
            def setUp(self):
                nonlocal fixture_root
                self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
                fixture_root=self.root
                (self.root/'state.sqlite3-wal').write_bytes(b'x'*100)
                cleanup=self.temp.cleanup
                def unlink():
                    order.append('fixture.unlink')
                    if held:raise PermissionError(32,'WAL still held by diagnostic')
                    cleanup()
                self.temp.cleanup=unlink
            def test_cancel_during_provider_call_prevents_late_write(self):
                holder.arm(self.root/'state.sqlite3-wal',connection_id=7,frame=sys._getframe())
                order.append('future.done')
            def tearDown(self):
                order.append('application.close.call')
                self.assertTrue(held,'intervention must still be active during original application.close')
                order.append('application.close.return')
                order.append('environment.stop')
                self.temp.cleanup()
        original=diagnostic.WalSharingControl
        def capture_holder(trace,mode):
            nonlocal holder
            holder=original(trace,mode,api=API());return holder
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(sys.modules,{'test_delegation':types.SimpleNamespace(DelegationTests=Fixture)}), \
             patch.object(diagnostic,'LoadControl') as load, \
             patch.object(diagnostic,'WalSharingControl',side_effect=capture_holder), \
             patch.object(diagnostic.faulthandler,'dump_traceback'):
            load.return_value.close.side_effect=lambda:order.append('load.close')
            result=diagnostic.run_child(Path(directory),'cpu',2,readonly_anchor=True,
                                        native_trace=True,vfs_target='wal')
            self.assertEqual(result,0,(Path(directory)/'unittest.log').read_text())
            evidence=json.loads((Path(directory)/'vfs-control.json').read_text())
            self.assertTrue(evidence['cleanup_complete'])
            self.assertGreaterEqual(evidence['release_call_performance_counter_ns']-
                                    evidence['arm_performance_counter_ns'],800_000_000)
        self.assertFalse(fixture_root.exists())
        self.assertLess(order.index('future.done'),order.index('application.close.call'))
        self.assertLess(order.index('load.close'),order.index('application.close.call'))
        self.assertLess(order.index('application.close.return'),order.index('handle.close'))
        self.assertLess(order.index('handle.close'),order.index('fixture.unlink'))

    def test_qualifying_hold_overlaps_actual_close_with_complete_consistent_integer_timestamps(self):
        from scripts.v3_windows_reliability import _vfs_condition
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            valid={'mode':'wal','hold_seconds':.8,'desired_access':0x80000000,'share_mode':3,
                'creation_disposition':3,'file_flags':0x80,'release_reason':'fixed_deadline',
                'attempted':True,'armed':True,'released':True,'cleanup_complete':True,'error':None,
                'arm_monotonic_ns':1,'release_call_monotonic_ns':800_000_001,
                'timer':'perf_counter_ns','arm_performance_counter_ns':1,
                'release_call_performance_counter_ns':800_000_001,
                'release_monotonic_ns':800_000_011,'close_call_monotonic_ns':2,
                'close_return_monotonic_ns':12,'close_interval_ns':10,'close_event':'c_return',
                'worker_pid':1,'worker_native_tid':1,'arm_qpc':1,'release_qpc':800_000_011,
                'wal':str(root/'state.sqlite3-wal'),'target':str(root/'state.sqlite3-wal')}
            path=root/'vfs-control.json';path.write_text(json.dumps(valid))
            self.assertTrue(_vfs_condition(root,'wal')[1])  # anchor/current may return before release
            coarse=dict(valid,release_call_monotonic_ns=796_875_001,release_monotonic_ns=796_875_011)
            path.write_text(json.dumps(coarse))
            self.assertTrue(_vfs_condition(root,'wal')[1])  # .8s high-resolution hold, coarse Windows clock ticks
            path.write_text(json.dumps(dict(valid,close_return_monotonic_ns=2,close_interval_ns=0)))
            self.assertTrue(_vfs_condition(root,'wal')[1])  # Windows monotonic timestamps can share one tick
            invalid=[dict(valid,close_call_monotonic_ns=900_000_001,
                          close_return_monotonic_ns=900_000_011),
                     dict(valid,close_interval_ns=11),dict(valid,close_call_monotonic_ns=True),
                     dict(valid,close_return_monotonic_ns=12.0),dict(valid,release_call_monotonic_ns=0),
                     dict(valid,release_call_performance_counter_ns=799_999_999),
                     dict(valid,close_interval_ns=False,close_return_monotonic_ns=2),
                     dict(valid,close_interval_ns=-1,close_return_monotonic_ns=1)]
            for field in ('close_call_monotonic_ns','close_return_monotonic_ns','release_call_monotonic_ns'):
                missing=dict(valid);missing.pop(field);invalid.append(missing)
            for value in invalid:
                with self.subTest(value=value):
                    path.write_text(json.dumps(value))
                    self.assertFalse(_vfs_condition(root,'wal')[1])

    def test_vfs_cli_requires_pinned_historical_environment_and_exact_six_cell_campaign(self):
        from scripts import v3_windows_reliability as diagnostic
        with tempfile.TemporaryDirectory() as directory,patch.object(diagnostic,'ROOT',diagnostic.ROOT), \
                patch.object(diagnostic,'run_native_comparison',return_value=0) as run:
            root=Path(directory).resolve()
            base=['diagnostic','--output',str(root/'out'),'--repository',str(root/'current'),
                  '--control','native-vfs','--iterations','1']
            with patch.object(sys,'argv',base),self.assertRaises(SystemExit):diagnostic.main()
            run.assert_not_called()
            with patch.object(sys,'argv',base+['--historical-environment','--historical-repository',str(root/'old')]):
                self.assertEqual(diagnostic.main(),0)
            run.assert_called_once_with(root/'out',root/'current',root/'old',vfs=True)

    def test_only_first_worker_post_cancel_positive_wal_preclose_phase_can_arm(self):
        import sqlite3
        import threading
        from scripts.v3_windows_reliability import Trace
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            connection=sqlite3.connect(root/'state.sqlite3')
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('CREATE TABLE probe(value TEXT)');connection.commit()
            trace=Trace(root,native=True);trace.case=types.SimpleNamespace(root=root)
            holder=types.SimpleNamespace(attempted=False,arm=Mock(),note_close=Mock())
            def arm(*args,**kwargs):holder.attempted=True
            holder.arm.side_effect=arm;trace.vfs=holder
            try:
                trace.profile(sys._getframe(),'c_call',connection.close)
                holder.arm.assert_not_called()
                trace.worker_native_tids.add(threading.get_native_id())
                trace.profile(sys._getframe(),'c_call',connection.close)
                holder.arm.assert_not_called()
                trace.cancel_returned=True
                with patch.object(trace,'wal_metadata',return_value={'exists':False,'bytes':0}):
                    trace.profile(sys._getframe(),'c_call',connection.close)
                holder.arm.assert_not_called()
                trace.profile(sys._getframe(),'c_call',connection.close)
                trace.profile(sys._getframe(),'c_call',connection.close)
                holder.arm.assert_called_once()
                self.assertEqual(holder.arm.call_args.args[0],root/'state.sqlite3-wal')
            finally:
                connection.close();trace.stream.close()

    def test_close_interval_excludes_preclose_arm_and_trace_write_work(self):
        import sqlite3
        import threading
        from scripts.v3_windows_reliability import Trace
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);connection=sqlite3.connect(root/'state.sqlite3')
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('CREATE TABLE probe(value TEXT)');connection.commit()
            trace=Trace(root,native=True);trace.case=types.SimpleNamespace(root=root)
            order=[]
            holder=types.SimpleNamespace(attempted=False)
            def arm(*args,**kwargs):holder.attempted=True;order.append('arm')
            holder.arm=arm;holder.note_close=lambda event,*args,**kwargs:order.append(event)
            trace.vfs=holder;trace.cancel_returned=True
            trace.worker_native_tids.add(threading.get_native_id())
            try:
                with patch.object(trace,'record',side_effect=lambda *args,**kwargs:order.append('trace-write')):
                    trace.profile(sys._getframe(),'c_call',connection.close)
                    trace.profile(sys._getframe(),'c_return',connection.close)
                self.assertEqual(order,['arm','trace-write','c_call','c_return','trace-write'])
            finally:
                connection.close();trace.stream.close()

    def test_winapi_holder_and_delete_access_probe_never_modify_or_delete_the_file(self):
        from scripts.v3_windows_reliability import WindowsFileHandles
        kernel=types.SimpleNamespace(CreateFileW=Mock(side_effect=[123, -1]),
            CloseHandle=Mock(return_value=1),QueryPerformanceCounter=Mock())
        api=WindowsFileHandles(kernel=kernel,last_error=lambda:32)
        self.assertEqual(api.open(Path('owned-wal')), (123,0))
        self.assertEqual(api.open(Path('owned-wal'),probe=True), (None,32))
        self.assertEqual([call.args[1:6] for call in kernel.CreateFileW.call_args_list],
                         [(0x80000000,3,None,3,0x80),(0x10000,7,None,3,0x80)])
        api.close(123)
        kernel.CloseHandle.assert_called_once_with(123)

    def control(self,root,mode,api):
        from scripts.v3_windows_reliability import WalSharingControl
        trace=types.SimpleNamespace(output=root,record=Mock())
        return WalSharingControl(trace,mode,api=api),trace

    def test_wal_and_unrelated_file_controls_match_resource_flags_lifetime_and_cleanup(self):
        class API:
            def __init__(self):self.opened=[];self.closed=[]
            def open(self,path,probe=False):
                self.opened.append((Path(path),probe))
                return (None,32) if probe else (123,0)
            def close(self,handle):self.closed.append(handle)
            def qpc(self):return time.perf_counter_ns()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            wal=root/'state.sqlite3-wal';wal.write_bytes(b'x'*100)
            for mode in ('wal','control-file'):
                case=root/mode;case.mkdir();api=API()
                control,trace=self.control(case,mode,api)
                with patch('scripts.v3_windows_reliability.sqlite3.connect',
                           side_effect=AssertionError('raw handle created an SQLite participant')), \
                     patch('scripts.v3_windows_reliability.faulthandler.dump_traceback'), \
                     patch('scripts.v3_windows_reliability.time.sleep',side_effect=AssertionError('post-close sleep')):
                    control.arm(wal,connection_id=7,frame=sys._getframe())
                    control.close()
                evidence=json.loads((case/'vfs-control.json').read_text())
                target=wal if mode=='wal' else case/'vfs-control.bin'
                self.assertEqual(api.opened,[(target,False),(target,True),(target,True)])
                self.assertEqual(api.closed,[123])
                self.assertTrue(evidence['attempted'] and evidence['armed'] and evidence['released'])
                self.assertTrue(evidence['cleanup_complete'])
                self.assertEqual(evidence['timer'],'perf_counter_ns')
                self.assertGreaterEqual(evidence['release_call_performance_counter_ns']-
                                        evidence['arm_performance_counter_ns'],800_000_000)
                self.assertEqual(evidence['hold_seconds'],.8)
                self.assertEqual(evidence['share_mode'],3)
                self.assertEqual(evidence['desired_access'],0x80000000)
                self.assertTrue(all(row['error']==32 for row in evidence['delete_access_probes']))
                self.assertFalse(control.thread.is_alive())
                control.close()  # resource cleanup is idempotent
                self.assertEqual(api.closed,[123])

    def test_arm_failure_is_retained_and_never_claims_a_qualifying_condition(self):
        api=types.SimpleNamespace(open=lambda *args,**kwargs:(None,32),close=Mock(),qpc=lambda:1)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);wal=root/'state.sqlite3-wal';wal.write_bytes(b'x'*100)
            control,_=self.control(root,'wal',api)
            control.arm(wal,connection_id=7,frame=sys._getframe())
            with self.assertRaisesRegex(RuntimeError,'VFS control'):
                control.close()
            evidence=json.loads((root/'vfs-control.json').read_text())
            self.assertTrue(evidence['attempted'])
            self.assertFalse(evidence['armed'])
            self.assertIn('32',evidence['error'])
            api.close.assert_not_called()

    def test_clock_failure_cannot_skip_owned_handle_cleanup_or_claim_clean_evidence(self):
        api=types.SimpleNamespace(open=Mock(),close=Mock(),qpc=Mock(side_effect=OSError('clock unavailable')))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);control,_=self.control(root,'wal',api)
            control.handle=123
            control.release('cleanup_retry')
            with self.assertRaises(RuntimeError):control.close()
            api.close.assert_called_once_with(123)
            evidence=json.loads((root/'vfs-control.json').read_text())
            self.assertTrue(evidence['cleanup_complete'])
            self.assertIn('clock unavailable',evidence['error'])

    def test_failed_holder_and_probe_closes_retain_ownership_for_cleanup_retry(self):
        closed=[]
        def close(handle):
            closed.append(handle)
            if closed.count(handle)==1:raise OSError('close refused')
        api=types.SimpleNamespace(open=lambda path,probe=False:(999,0) if probe else (123,0),
                                 close=close,qpc=time.perf_counter_ns)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);wal=root/'state.sqlite3-wal';wal.write_bytes(b'x'*100)
            control,_=self.control(root,'wal',api)
            control.arm(wal,connection_id=7,frame=sys._getframe())
            self.assertEqual(control.handle,123)
            self.assertEqual(control.probe_handles,{999})
            with self.assertRaises(RuntimeError):control.close()
            self.assertEqual(closed,[999,123,123,999])
            evidence=json.loads((root/'vfs-control.json').read_text())
            self.assertTrue(evidence['cleanup_complete'])
            self.assertIsNotNone(evidence['error'])

    def test_live_holder_thread_cannot_be_falsely_certified_or_double_closed(self):
        api=types.SimpleNamespace(close=Mock(),qpc=time.perf_counter_ns)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);control,_=self.control(root,'wal',api)
            control.handle=123
            control.thread=types.SimpleNamespace(join=Mock(),is_alive=lambda:True)
            with self.assertRaises(RuntimeError):control.close()
            control.thread.join.assert_called_once_with(timeout=2)
            api.close.assert_not_called()
            self.assertFalse(json.loads((root/'vfs-control.json').read_text())['cleanup_complete'])


class NativeWaitDiagnosticTests(unittest.TestCase):
    @staticmethod
    def exported_profile():
        # The actual GeneralProfile+FileIO export does not list Filename.
        return '''<WindowsPerformanceRecorder Version="1.0"><Profiles>
<SystemCollector Id="exported" Name="Exported"><BufferSize Value="1024"/><Buffers Value="40"/></SystemCollector>
<SystemProvider Id="kernel"><Keywords>
<Keyword Value="ProcessThread"/><Keyword Value="Loader"/><Keyword Value="ThreadPriority"/>
<Keyword Value="CSwitch"/><Keyword Value="ReadyThread"/><Keyword Value="DiskIO"/>
<Keyword Value="DiskIOInit"/><Keyword Value="FileIO"/><Keyword Value="FileIOInit"/>
<Keyword Value="SampledProfile"/><Keyword Value="Registry"/><Keyword Value="Power"/>
</Keywords><Stacks><Stack Value="CSwitch"/></Stacks></SystemProvider>
<EventProvider Id="noise" Name="UnrelatedProvider"/>
<Profile Id="Exported.Verbose.File" Name="Exported" DetailLevel="Verbose" LoggingMode="File" Description="Exported">
<Collectors><SystemCollectorId Value="exported"><SystemProviderId Value="kernel"/></SystemCollectorId></Collectors>
</Profile></Profiles></WindowsPerformanceRecorder>'''

    def test_native_snapshot_never_opens_a_database_until_owned_futures_are_done(self):
        import sqlite3
        from contextlib import closing
        from scripts.v3_windows_reliability import Trace
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with closing(sqlite3.connect(root / 'state.sqlite3')) as connection, connection:
                connection.execute('CREATE TABLE events(event_type TEXT, payload TEXT)')
                connection.execute('INSERT INTO events VALUES(?,?)',
                                   ('subagent.state', json.dumps({'run_id':'owned','status':'cancelled'})))
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute('SELECT 1')
            (root / 'marker.txt').write_text('ok')
            trace = Trace(root, native=True)
            done = False
            trace.case = types.SimpleNamespace(root=root)
            trace.coordinator = types.SimpleNamespace(runs={'owned': {'status':'cancelled'}},
                futures={'owned':types.SimpleNamespace(done=lambda: done)},
                cancelled={'owned':types.SimpleNamespace(is_set=lambda: True)})
            try:
                with patch('scripts.v3_windows_reliability.sqlite3.connect',
                           side_effect=AssertionError('measurement opened an implicit WAL client')):
                    trace.snapshot('pending.json')
                pending = json.loads((root / 'pending.json').read_text())
                self.assertTrue(pending['database_snapshot_deferred'])
                self.assertEqual(pending['persisted_states'], [])
                done = True
                trace.snapshot('complete.json')
                self.assertEqual(json.loads((root / 'complete.json').read_text())['persisted_states'][0]['status'],
                                 'cancelled')
            finally:
                trace.stream.close()

    def test_native_trace_records_joinable_identity_and_real_wal_close_metadata(self):
        import sqlite3
        from scripts.v3_windows_reliability import Trace
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            connection = sqlite3.connect(root / 'state.sqlite3')
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('CREATE TABLE probe(value TEXT)')
            connection.commit()
            trace = Trace(root, native=True)
            trace.case = types.SimpleNamespace(root=root)
            frame = types.SimpleNamespace(f_code=types.SimpleNamespace(co_qualname='owned.close'))
            try:
                trace.profile(frame, 'c_call', connection.close)
                connection.close()
                trace.profile(frame, 'c_return', connection.close)
            finally:
                trace.stream.close()
            events = [json.loads(line) for line in (root / 'events.jsonl').read_text().splitlines()]
            before, after = [row for row in events if row['event'].startswith('sqlite.close.')]
            self.assertTrue(before['wal']['exists'])
            self.assertGreater(before['wal']['bytes'], 32)
            self.assertFalse(after['wal']['exists'])
            self.assertEqual(before['connection_id'], after['connection_id'])
            self.assertEqual(before['pid'], os.getpid())
            self.assertGreater(before['native_tid'], 0)
            self.assertEqual(events[0]['event'], 'clock.mapping')
            self.assertIn('performance_counter_ns', events[0])

    def test_missing_native_tools_blocks_without_starting_or_cancelling_any_recording(self):
        from scripts import v3_windows_reliability as diagnostic
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(diagnostic.platform, 'system', return_value='Windows'), \
             patch.object(diagnostic.shutil, 'which', return_value=None), \
             patch.object(diagnostic.subprocess, 'run') as run:
            capture = diagnostic.NativeCapture(Path(directory), {})
            with self.assertRaisesRegex(RuntimeError, 'unavailable'):
                capture.preflight()
            capture.stop()
            run.assert_not_called()

    def test_native_preflight_checks_exported_wait_and_io_profiles_with_windows_xml_encoding(self):
        from scripts import v3_windows_reliability as diagnostic
        commands = []
        def run(command, **kwargs):
            commands.append(command)
            if '-exportprofile' in command:
                Path(command[-2]).write_text(self.exported_profile(), encoding='utf-16')
            text = ('KyrozenR260' if '-profiles' in command and len(command)==3 else
                    'GeneralProfile FileIO' if '-profiles' in command else '-instancename')
            return subprocess.CompletedProcess(command, 0, text, '')
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(diagnostic.platform, 'system', return_value='Windows'), \
             patch.object(diagnostic.shutil, 'which', side_effect=lambda name:name), \
             patch.object(diagnostic.subprocess, 'run', side_effect=run):
            capture = diagnostic.NativeCapture(Path(directory), {})
            metadata = capture.preflight()
            import xml.etree.ElementTree as ET
            selected = Path(directory)/'selected-profile.wprp'
            document = ET.parse(selected).getroot()
            self.assertEqual({node.get('Value') for node in document.findall('.//SystemProvider/Keywords/Keyword')},
                {'ProcessThread','Loader','ThreadPriority','CSwitch','ReadyThread','DiskIO',
                 'DiskIOInit','FileIO','FileIOInit','Filename'})
            self.assertTrue(all(node.get('Strict')=='true' for node in document.findall('.//Keyword')))
            self.assertEqual(len(document.findall('.//SystemCollector')), 1)
            self.assertEqual(document.find('.//SystemCollector/BufferSize').get('Value'), '64')
            self.assertEqual(document.find('.//SystemCollector/Buffers').get('Value'), '40')
            self.assertFalse(any(document.findall('.//'+tag) for tag in ('Stacks','EventProvider','EventCollector')))
            self.assertEqual({node.get('LoggingMode') for node in document.findall('./Profiles/Profile')}, {'File','Memory'})
            self.assertEqual(metadata['sha256'], hashlib.sha256(selected.read_bytes()).hexdigest())
            self.assertEqual(commands[-1], ['wpr.exe','-profiles',str(selected)])
            self.assertEqual(len(commands), 4)
            self.assertFalse(any('-start' in command or '-cancel' in command for command in commands))
            selected.write_text('changed after preflight')
            with self.assertRaises(RuntimeError):capture.start(Path(directory))
            self.assertEqual(len(commands),4)

    def test_custom_profile_rejection_or_missing_system_keyword_blocks_without_fallback(self):
        from scripts import v3_windows_reliability as diagnostic
        for mode in ('missing-keyword','rejected-profile'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory, \
                 patch.object(diagnostic.platform,'system',return_value='Windows'), \
                 patch.object(diagnostic.shutil,'which',side_effect=lambda name:name):
                commands=[]
                def run(command,**kwargs):
                    commands.append(command)
                    if '-exportprofile' in command:
                        text=self.exported_profile()
                        if mode=='missing-keyword':text=text.replace('<Keyword Value="CSwitch"/>','')
                        Path(command[-2]).write_text(text)
                    custom='-profiles' in command and len(command)==3
                    text='KyrozenR260' if custom else 'GeneralProfile FileIO -instancename'
                    return subprocess.CompletedProcess(command,5 if custom else 0,text,'')
                capture=diagnostic.NativeCapture(Path(directory),{})
                with patch.object(diagnostic.subprocess,'run',side_effect=run):
                    with self.assertRaises(RuntimeError):capture.preflight()
                    with self.assertRaises(RuntimeError):capture.start(Path(directory))
                self.assertFalse(capture.active)
                self.assertFalse(any('-start' in command or '-cancel' in command for command in commands))

    def test_capture_cannot_start_without_its_unchanged_preflighted_custom_profile(self):
        from scripts import v3_windows_reliability as diagnostic
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(diagnostic.shutil,'which',side_effect=lambda name:name), \
             patch.object(diagnostic.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'','')) as run:
            capture=diagnostic.NativeCapture(Path(directory),{})
            with self.assertRaises(RuntimeError):capture.start(Path(directory))
            run.assert_not_called()

    def test_native_workflow_preserves_existing_controls_and_adds_a_pinned_separate_old_checkout(self):
        import yaml
        workflow = yaml.safe_load((ROOT/'.github/workflows/v3-windows-reliability.yml').read_text())
        dispatch = workflow.get('on', workflow.get(True))['workflow_dispatch']['inputs']
        self.assertEqual(dispatch['control']['options'],
                         ['baseline','cpu','sqlite','combined','paired-anchor','native-wait','native-vfs','all'])
        steps = workflow['jobs']['cancellation']['steps']
        old = next(step for step in steps if step.get('with', {}).get('path')=='historical-product')
        self.assertEqual(old['with']['ref'], '0f172f9fe692ff8fafcc0a15a72fe51eaf67e977')
        self.assertIn("'native-vfs'",old['if'])
        validation=next(step['run'] for step in steps if step.get('name')=='Validate exact candidate identity')
        self.assertIn('native-vfs',validation)
        trace = next(step['run'] for step in steps if step.get('name','').startswith('Trace unchanged'))
        self.assertIn('--cpu-workers 2', trace)
        self.assertIn("'--historical-repository', 'historical-product'", trace)
        self.assertEqual(dispatch['decode_existing_etl']['type'],'boolean')
        self.assertEqual(dispatch['history_copyfile2_probe']['type'], 'boolean')
        self.assertEqual(workflow['jobs']['cancellation']['if'],
                         '${{ !inputs.decode_existing_etl && !inputs.history_copyfile2_probe }}')
        offline=workflow['jobs']['offline-decode']
        self.assertEqual(offline['permissions'],{'contents':'read','actions':'read'})
        self.assertIn('inputs.decode_existing_etl',offline['if'])
        checkouts=[step.get('with',{}).get('path') for step in offline['steps']
                   if step.get('uses','').startswith('actions/checkout@')]
        self.assertEqual(checkouts,['diagnostic-source'])
        commands='\n'.join(step.get('run','') for step in offline['steps'])
        self.assertIn('--offline-decode-archive',commands)
        self.assertNotIn('--historical-repository',commands)
        self.assertNotIn('pip install',commands)
        diagnostic_checkout = next(step for step in workflow['jobs']['diagnostic-regression']['steps']
                                   if step.get('uses', '').startswith('actions/checkout@'))
        self.assertEqual(diagnostic_checkout['with'].get('fetch-depth'), 0)

    def test_native_capture_stops_only_its_owned_instance_even_when_start_is_uncertain(self):
        from scripts import v3_windows_reliability as diagnostic
        commands = []
        def run(command, **kwargs):
            commands.append(command)
            if '-exportprofile' in command:
                Path(command[-2]).write_text(self.exported_profile())
            if '-profiles' in command or '-help' in command:
                return subprocess.CompletedProcess(command,0,
                    'KyrozenR260' if len(command)==3 and '-profiles' in command else
                    'GeneralProfile FileIO -instancename','')
            if '-start' in command:
                raise subprocess.TimeoutExpired(command, 10)
            if '-status' in command and 'collectors' not in command:
                return subprocess.CompletedProcess(command, 0xc5583000, 'There are no trace profiles running.', '')
            return subprocess.CompletedProcess(command, 0, 'Events Lost : 0', '')
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(diagnostic.shutil, 'which', side_effect=lambda name:name), \
             patch.object(diagnostic.subprocess, 'run', side_effect=run):
            capture = diagnostic.NativeCapture(Path(directory), {})
            with patch.object(diagnostic.platform,'system',return_value='Windows'):
                capture.preflight()
            commands.clear()
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    capture.start(Path(directory))
            finally:
                with self.assertRaisesRegex(RuntimeError, 'ETL'):
                    capture.stop()
            self.assertTrue(any('-stop' in command for command in commands))
            self.assertFalse(any('-cancel' in command for command in commands))
            self.assertTrue(all(command[-2:] == ['-instancename',capture.instance] for command in commands))

    def test_failed_save_cancels_only_owned_recording_and_verifies_inactive_without_hiding_failure(self):
        from scripts import v3_windows_reliability as diagnostic
        commands = []
        def run(command, **kwargs):
            commands.append((command, kwargs['timeout']))
            if '-stop' in command:
                Path(command[2]).write_bytes(b'partial native trace')
                raise subprocess.TimeoutExpired(command, kwargs['timeout'])
            if '-status' in command and 'collectors' not in command:
                return subprocess.CompletedProcess(command, 0xc5583000, 'There are no trace profiles running.', '')
            return subprocess.CompletedProcess(command, 0, 'Events Lost : 0', '')
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(diagnostic.shutil, 'which', side_effect=lambda name:name), \
             patch.object(diagnostic.subprocess, 'run', side_effect=run):
            capture = diagnostic.NativeCapture(Path(directory), {})
            capture.case, capture.active = Path(directory), True
            with self.assertRaises(subprocess.TimeoutExpired):
                capture.stop()
            self.assertFalse(capture.active)
            self.assertFalse(capture.cleanup_uncertified)
            self.assertTrue(any('-cancel' in command for command, _ in commands))
            self.assertTrue(all(command[-2:]==['-instancename',capture.instance] for command, _ in commands))
            self.assertTrue(all(timeout<=30 for _, timeout in commands))
            self.assertTrue(any((Path(directory)/'traces').glob('*.etl')))
            self.assertTrue((Path(directory)/'wpr-stop.json').exists())

    def test_failed_cancel_or_unknown_inactive_response_remains_active_and_uncertified(self):
        from scripts import v3_windows_reliability as diagnostic
        for cancel_code, inactive_text in ((5,'WPR recording is in progress...'), (0,'unknown status format')):
            with self.subTest(cancel_code=cancel_code), tempfile.TemporaryDirectory() as directory, \
                 patch.object(diagnostic.shutil, 'which', side_effect=lambda name:name):
                commands = []
                def run(command, **kwargs):
                    commands.append(command)
                    if '-stop' in command:
                        return subprocess.CompletedProcess(command, 5, '', 'save failed')
                    if '-cancel' in command:
                        return subprocess.CompletedProcess(command, cancel_code, '', 'cancel output')
                    text = 'Events Lost : 0' if 'collectors' in command else inactive_text
                    return subprocess.CompletedProcess(command, 0, text, '')
                capture = diagnostic.NativeCapture(Path(directory), {})
                capture.case, capture.active = Path(directory), True
                with patch.object(diagnostic.subprocess, 'run', side_effect=run), \
                     self.assertRaisesRegex(RuntimeError, 'cleanup_uncertified'):
                    capture.stop()
                self.assertTrue(capture.active)
                self.assertTrue(capture.cleanup_uncertified)
                self.assertTrue(any('-cancel' in command for command in commands))
                self.assertTrue(all(command[-2:]==['-instancename',capture.instance] for command in commands))

    def test_failed_status_with_partial_zero_loss_still_saves_but_never_qualifies_capture(self):
        from scripts import v3_windows_reliability as diagnostic
        def run(command, **kwargs):
            if '-stop' in command:
                Path(command[2]).write_bytes(b'unit-test trace stub')
            if '-status' in command:
                if 'collectors' in command:
                    return subprocess.CompletedProcess(command, 5, 'Events Lost : 0', 'query failed')
                return subprocess.CompletedProcess(command, 0, 'WPR is not recording.', '')
            return subprocess.CompletedProcess(command, 0, '', '')
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(diagnostic.shutil, 'which', side_effect=lambda name:name), \
             patch.object(diagnostic.subprocess, 'run', side_effect=run):
            capture = diagnostic.NativeCapture(Path(directory), {})
            capture.case, capture.active = Path(directory), True
            with self.assertRaisesRegex(RuntimeError, 'wpr-status'):
                capture.stop()
            self.assertFalse(capture.active)
            self.assertTrue(any((Path(directory)/'traces').glob('*.etl')))

    def test_uncertified_retries_preserve_each_partial_trace_and_tool_attempt(self):
        from scripts import v3_windows_reliability as diagnostic
        attempts = 0
        def run(command, **kwargs):
            nonlocal attempts
            if '-stop' in command:
                attempts += 1
                Path(command[2]).write_bytes(f'partial {attempts}'.encode())
                return subprocess.CompletedProcess(command, 5, '', 'failed save')
            code = 5 if '-cancel' in command else 0
            return subprocess.CompletedProcess(command, code, 'Events Lost : 0', '')
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(diagnostic.shutil, 'which', side_effect=lambda name:name), \
             patch.object(diagnostic.subprocess, 'run', side_effect=run):
            root = Path(directory)
            capture = diagnostic.NativeCapture(root, {})
            capture.case, capture.active = root, True
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, 'cleanup_uncertified'):
                    capture.stop()
            self.assertEqual(sorted(path.read_bytes() for path in (root/'traces').glob('*.etl')),
                             [b'partial 1',b'partial 2'])
            self.assertEqual(len(list(root.glob('wpr-stop*.json'))), 2)
            self.assertTrue(capture.active and capture.cleanup_uncertified)

    def comparison(self, failure=False, *, abort_reason=None, launch_error=False, saved_trace_bytes=None,
                   vfs=False, unarmed=False, infrastructure_timeout=False):
        from scripts import v3_windows_reliability as diagnostic
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        output = Path(temporary.name)
        old, current = output / 'old', output / 'current'
        commands = []
        activity = {'tools': [], 'workload_recording': []}
        recording = False
        capture = diagnostic.NativeCapture(output, {})
        capture.wpr, capture.decoder = 'wpr.exe', 'tracerpt.exe'

        def native_tool(command, **kwargs):
            nonlocal recording
            activity['tools'].append(command)
            if '-exportprofile' in command:
                Path(command[-2]).write_text(self.exported_profile())
            if '-profiles' in command or '-help' in command:
                return subprocess.CompletedProcess(command,0,
                    'KyrozenR260' if len(command)==3 and '-profiles' in command else
                    'GeneralProfile FileIO -instancename','')
            if '-start' in command:
                self.assertFalse(recording, 'the owned recording was already active')
                recording = True
                temporary_trace = Path(command[command.index('-recordtempto')+1]) / 'recording.etl'
                # Model the real runner's roughly 120 MiB per-recording allocation.
                with temporary_trace.open('wb') as stream:
                    stream.truncate(120*1024*1024)
            elif '-stop' in command:
                activity['checkpoint_at_stop'] = json.loads((output/'report.json').read_text())
                if saved_trace_bytes is not None:
                    with (output/'traces/temporary/recording.etl').open('r+b') as stream:
                        stream.truncate(saved_trace_bytes)
                (output/'traces/temporary/recording.etl').replace(command[2])
                recording = False
            elif '-of' in command:
                Path(command[command.index('-o')+1]).write_text('<Events/>')
                Path(command[command.index('-summary')+1]).write_text('synthetic decoder output')
            text = ('Events Lost : 0' if 'collectors' in command else 'WPR is not recording.')
            return subprocess.CompletedProcess(command, 0, text, '')

        def run(command, log_path, env, **kwargs):
            commands.append((command, kwargs))
            activity['workload_recording'].append(recording)
            if launch_error:
                raise OSError('child launch failed')
            reason = abort_reason or kwargs['abort']()
            if reason:
                return {'returncode':124, 'timeout_seconds':45, 'timeout_cleanup':None,
                        'abort_reason':reason}
            if '--child' in command:
                case = Path(command[command.index('--output')+1])
                bad = failure and len(commands)==1
                (case/'unittest.log').write_text('TimeoutError' if bad else 'OK')
                (case/'events.jsonl').write_text('\n'.join(json.dumps(row) for row in [
                    {'event':'cancel.return','monotonic_ns':1,'thread_id':1},
                    {'event':'future_wait.call','monotonic_ns':1,'thread_id':1},
                    {'event':'future_wait.return','monotonic_ns':5_000_000_001 if bad else 200_000_001,'thread_id':1}]))
                if '--vfs-target' in command:
                    mode=command[command.index('--vfs-target')+1]
                    write={'mode':mode,'hold_seconds':.8,'desired_access':0x80000000,'share_mode':3,
                           'creation_disposition':3,'file_flags':0x80,'release_reason':'fixed_deadline',
                           'attempted':not unarmed,'armed':not unarmed,'released':not unarmed,
                           'cleanup_complete':True,'error':None,'arm_monotonic_ns':1,
                           'release_monotonic_ns':800_000_001,'close_interval_ns':10,
                           'release_call_monotonic_ns':800_000_001,
                           'timer':'perf_counter_ns','arm_performance_counter_ns':1,
                           'release_call_performance_counter_ns':800_000_001,
                           'close_call_monotonic_ns':2,'close_return_monotonic_ns':12,
                           'close_event':'c_return','worker_pid':1,'worker_native_tid':1,
                           'arm_qpc':1,'release_qpc':800_000_001,'wal':str(case/'state.sqlite3-wal'),
                           'target':str(case/'state.sqlite3-wal' if mode=='wal' else case/'vfs-control.bin')}
                    (case/'vfs-control.json').write_text(json.dumps(write))
                if infrastructure_timeout and len(commands)==1:
                    return {'returncode':124,'timeout_seconds':45,'abort_reason':None,
                            'timeout_cleanup':{'descendant_cleanup_certified':False}}
                return {'returncode': int(bad), 'timeout_seconds':45, 'timeout_cleanup':None}
            return {'returncode':0, 'timeout_seconds':45, 'timeout_cleanup':None}
        with patch.object(diagnostic, 'NativeCapture', return_value=capture), \
             patch.object(diagnostic.platform, 'system', return_value='Windows'), \
             patch.object(diagnostic.subprocess, 'run', side_effect=native_tool), \
             patch.object(diagnostic, '_product_identity', side_effect=lambda path: {
                 'candidate_sha':diagnostic.HISTORICAL_PRODUCT if path==old else 'c'*40,
                 'cancel_fixture_ast_sha256':'same-original-fixture'}), \
             patch.object(diagnostic, 'historical_environment', return_value={'status':'MATCH'}), \
             patch.object(diagnostic, 'run_process', side_effect=run):
            result = diagnostic.run_native_comparison(output, current, old, **({'vfs':True} if vfs else {}))
        return result, json.loads((output/'report.json').read_text()), commands, old, current, activity

    def test_native_vfs_preserves_exact_six_cells_original_limits_reviewers_and_inconclusive_boundary(self):
        result,report,commands,old,current,_=self.comparison(vfs=True)
        self.assertEqual(result,0)
        self.assertEqual(report['status'],'INCONCLUSIVE')
        self.assertFalse(report['historical_cause_established'])
        self.assertEqual(report['active_limit_seconds'],300)
        self.assertEqual(report['child_limit_seconds'],45)
        self.assertEqual(report['test_timeouts_seconds'],{'provider_wait':3,'entry_wait':10,'future_wait':5})
        self.assertEqual([(row['vfs_target'],row['variant']) for row in report['iterations']],
            [('wal','baseline'),('wal','anchor'),('wal','current'),
             ('control-file','current'),('control-file','anchor'),('control-file','baseline')])
        self.assertEqual(len(commands),12)
        for command,kwargs in commands[::2]:
            self.assertIn('--native-trace',command)
            self.assertIn('--vfs-target',command)
            self.assertEqual(command[command.index('--control')+1],'cpu')
            self.assertEqual(command[command.index('--cpu-workers')+1],'2')
            self.assertIn('abort',kwargs)
        self.assertTrue(all(command[1]==str(old/'scripts/subagent_workflow_acceptance.py')
                            for command,_ in commands[1::2]))
        self.assertTrue(all(row['vfs_condition_valid'] for row in report['iterations']))

    def test_native_vfs_failure_keeps_all_six_cells_but_unarmed_condition_is_blocked(self):
        result,report,commands,*_=self.comparison(vfs=True,failure=True)
        self.assertEqual(result,1)
        self.assertEqual(len(report['iterations']),6)
        self.assertEqual(len(commands),12)
        self.assertEqual(report['status'],'PENDING_NATIVE_ANALYSIS')
        result,report,*_=self.comparison(vfs=True,unarmed=True)
        self.assertEqual(result,2)
        self.assertEqual(report['status'],'BLOCKED')
        self.assertFalse(report['historical_cause_established'])

    def test_native_vfs_uncertified_timeout_stops_before_reviewer_or_any_later_cell_and_saves_trace(self):
        result,report,commands,_,_,activity=self.comparison(vfs=True,infrastructure_timeout=True)
        self.assertEqual(result,2)
        self.assertEqual(len(commands),1)
        self.assertEqual(len(report['iterations']),1)
        self.assertEqual(report['status'],'BLOCKED')
        self.assertTrue(report['cleanup_uncertified'])
        self.assertFalse(any(name.startswith('reviewer-') for name in report['execution_order']))
        self.assertEqual(sum('-start' in command for command in activity['tools']),1)
        self.assertEqual(sum('-stop' in command for command in activity['tools']),1)
        self.assertEqual(report['capture']['status'],'CAPTURED')
        self.assertTrue(activity['checkpoint_at_stop']['cleanup_uncertified'])
        self.assertEqual(activity['checkpoint_at_stop']['status'],'BLOCKED')
        self.assertTrue(activity['checkpoint_at_stop']['iterations'][0]['infrastructure_failure'])

    def test_native_comparison_keeps_identical_cpu_controls_and_pinned_reviewers_and_all_fast_is_inconclusive(self):
        result, report, commands, old, current, _ = self.comparison()
        self.assertEqual(result, 0)
        self.assertEqual(report['status'], 'INCONCLUSIVE')
        self.assertFalse(report['historical_cause_established'])
        self.assertEqual(report['active_limit_seconds'], 300)
        self.assertEqual(report['trace_limit_bytes'], 512*1024*1024)
        self.assertEqual([row['variant'] for row in report['iterations']],
            ['baseline','anchor','current','current','anchor','baseline',
             'anchor','baseline','current','current','baseline','anchor'])
        self.assertEqual(len(commands), 24)
        for command, kwargs in commands[::2]:
            self.assertIn('--native-trace', command)
            self.assertEqual(command[command.index('--control')+1], 'cpu')
            self.assertEqual(command[command.index('--cpu-workers')+1], '2')
            self.assertIn('abort', kwargs)
        for command, _ in commands[1::2]:
            self.assertEqual(command[1], str(old/'scripts/subagent_workflow_acceptance.py'))
        cancellations = [command for command, _ in commands[::2]]
        self.assertEqual(['--readonly-anchor' in command for command in cancellations],
                         [row['variant']=='anchor' for row in report['iterations']])

    def test_native_failure_keeps_the_whole_control_triple_and_never_claims_attribution(self):
        result, report, commands, _, _, _ = self.comparison(failure=True)
        self.assertEqual(result, 1)
        self.assertEqual(len(commands), 6)
        self.assertEqual(report['status'], 'PENDING_NATIVE_ANALYSIS')
        self.assertFalse(report['historical_cause_established'])
        self.assertEqual(report['iterations'][0]['future_wait_seconds'], 5)

    def test_one_owned_recording_covers_all_cells_and_reviewers_with_one_shared_digest(self):
        result, report, commands, _, _, activity = self.comparison()
        self.assertEqual(result, 0)
        self.assertEqual(len(commands), 24)
        self.assertEqual(report['capture_scope'], 'experiment')
        selected=Path(report['capture_profile']['path'])
        self.assertEqual(report['capture_profile']['sha256'],hashlib.sha256(selected.read_bytes()).hexdigest())
        start=next(command for command in activity['tools'] if '-start' in command)
        self.assertEqual(start[2],str(selected)+'!KyrozenR260')
        self.assertEqual(start.count('-start'),1)
        self.assertEqual(report['capture']['bytes'], 120*1024*1024)
        self.assertTrue(all(activity['workload_recording']))
        self.assertEqual(sum('-start' in command for command in activity['tools']), 1)
        self.assertEqual(sum('-stop' in command for command in activity['tools']), 1)
        self.assertEqual(sum('-of' in command for command in activity['tools']), 1)
        self.assertEqual(len(list((Path(report['capture']['etl']).parent).glob('*.etl'))), 1)
        for row in report['iterations']:
            self.assertEqual(row['capture'], report['capture'])
            self.assertEqual(Path(row['native_events']).name, 'events.jsonl')
            self.assertTrue(Path(row['native_events']).is_file())

    def test_child_launch_failure_still_saves_the_shared_owned_recording(self):
        result, report, commands, _, _, activity = self.comparison(launch_error=True)
        self.assertEqual(result, 2)
        self.assertEqual(report['status'], 'BLOCKED')
        self.assertIn('child launch failed', report['error'])
        self.assertFalse(report['cleanup_uncertified'])
        self.assertFalse(report['historical_cause_established'])
        self.assertEqual(len(commands), 1)
        self.assertEqual(sum('-stop' in command for command in activity['tools']), 1)
        self.assertIn('capture', report)
        self.assertEqual(report['iterations'][0]['capture'], report['capture'])
        self.assertTrue(Path(report['capture']['etl']).is_file())

    def test_size_abort_preserves_the_shared_trace_and_original_limits_without_attribution(self):
        result, report, commands, _, _, activity = self.comparison(abort_reason='trace_size_limit')
        self.assertNotEqual(result, 0)
        self.assertEqual(report['limit_reached'], 'trace_size_limit')
        self.assertEqual(report['active_limit_seconds'], 300)
        self.assertEqual(report['trace_limit_bytes'], 512*1024*1024)
        self.assertEqual(report['iterations'][0]['timeout_seconds'], 45)
        self.assertFalse(report['historical_cause_established'])
        self.assertEqual(len(commands), 1)
        self.assertEqual(sum('-stop' in command for command in activity['tools']), 1)
        self.assertIn('capture', report)
        self.assertEqual(report['iterations'][0]['capture'], report['capture'])

    def test_trace_growth_during_final_save_is_reported_against_the_same_size_limit(self):
        result, report, commands, _, _, _ = self.comparison(saved_trace_bytes=536870913)
        self.assertEqual(report.get('limit_reached'), 'trace_size_limit')
        self.assertNotEqual(result, 0)
        self.assertEqual(len(commands), 24)
        self.assertEqual(report['status'], 'INCONCLUSIVE')
        self.assertFalse(report['historical_cause_established'])
        self.assertEqual(report['capture']['bytes'], 536870913)

    def test_native_time_and_trace_size_limits_are_explicit_abort_reasons(self):
        from scripts import v3_windows_reliability as diagnostic
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(diagnostic.native_limit(root, time.monotonic()-1), 'active_time_limit')
            with (root/'owned.etl').open('wb') as stream:
                stream.truncate(512*1024*1024+1)
            self.assertEqual(diagnostic.native_limit(root, time.monotonic()+300), 'trace_size_limit')
            with patch.object(Path, 'rglob', side_effect=PermissionError('unreadable trace inventory')):
                self.assertEqual(diagnostic.native_limit(root, time.monotonic()+300), 'trace_inventory_error')

    def test_native_child_runs_the_actual_fixture_without_a_watchdog_database_client(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable,str(SCRIPT),'--child','--output',directory,
                '--repository',str(ROOT),'--native-trace'], cwd=ROOT, capture_output=True, text=True, timeout=35)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            events = [json.loads(row) for row in (Path(directory)/'events.jsonl').read_text().splitlines()]
            self.assertEqual(events[0]['event'], 'clock.mapping')
            self.assertTrue(any(row['event']=='product.source' for row in events))
            self.assertTrue(any(row['event']=='sqlite.close.c_call' and 'wal' in row for row in events))
            state = json.loads((Path(directory)/'state-before-cleanup.json').read_text())
            self.assertFalse(state['database_snapshot_deferred'])
            self.assertEqual(state['persisted_states'][-1]['status'], 'cancelled')

    @unittest.skipUnless(os.name == 'posix', 'real owned child abort fixture requires POSIX')
    def test_campaign_abort_preserves_45_second_child_bound_and_cleans_the_owned_process(self):
        from scripts.v3_windows_reliability import run_process
        with tempfile.TemporaryDirectory() as directory:
            attempts = iter((None, 'active_time_limit'))
            result = run_process([sys.executable,'-c','import time; time.sleep(20)'],
                Path(directory)/'child.log', dict(os.environ), abort=lambda:next(attempts))
            self.assertEqual(result['timeout_seconds'], 45)
            self.assertEqual(result['abort_reason'], 'active_time_limit')
            self.assertEqual(result['returncode'], 124)
            self.assertIsNotNone(result['timeout_cleanup']['parent_returncode'])

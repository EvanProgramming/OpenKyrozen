"""Keep frozen fixture assertions outside the UID importing edited source."""
from __future__ import annotations

import base64
import builtins
import importlib
import io
import json
import os
from pathlib import Path
import selectors
import sys
import time
import types
import unittest


def _pack(value):
    kind = type(value).__name__
    if type(value) in (type(None), bool, int, float, str):
        return [kind, value]
    if type(value) is bytes:
        return ['bytes', base64.b64encode(value).decode('ascii')]
    if type(value) in (list, tuple):
        return [kind, [_pack(item) for item in value]]
    if type(value) is dict:
        return ['dict', [[_pack(key), _pack(item)] for key, item in value.items()]]
    raise ValueError('fixture interface returned an unsupported value')


def _unpack(value):
    if type(value) is not list or len(value) != 2:
        raise ValueError('invalid fixture interface value')
    kind, data = value
    scalar = {'NoneType': type(None), 'bool': bool, 'int': int, 'float': float, 'str': str}
    if kind in scalar and type(data) is scalar[kind]:
        return data
    if kind == 'bytes' and type(data) is str:
        return base64.b64decode(data, validate=True)
    if kind in ('list', 'tuple') and type(data) is list:
        items = [_unpack(item) for item in data]
        return tuple(items) if kind == 'tuple' else items
    if kind == 'dict' and type(data) is list:
        return {_unpack(key): _unpack(item) for key, item in data}
    raise ValueError('invalid fixture interface type')


def serve(root):
    # The fixture cannot shadow the protocol's stdlib imports or bytecode cache.
    input_stream, output_stream = sys.stdin, sys.stdout
    sys.stdout = sys.stderr
    loads, dumps = json.loads, json.dumps
    sys.pycache_prefix = os.path.join(os.environ['HOME'], 'pycache')
    os.chdir(root)
    sys.path.insert(0, root)
    for line in input_stream:
        try:
            request = loads(line)
            module = importlib.import_module(request['module'])
            operation, name = request['operation'], request['name']
            if operation == 'clear' and name == 'ITEMS':
                value = module.ITEMS.clear()
            elif operation == 'call':
                value = getattr(module, name)(*_unpack(request['args']), **_unpack(request['kwargs']))
            elif operation == 'wsgi':
                environ = _unpack(request['environ'])
                environ['wsgi.input'] = io.BytesIO(_unpack(request['body']))
                captured = []
                body = b''.join(getattr(module, name)(environ,
                    lambda status, headers: captured.append((status, headers))))
                value = (*captured[0], body)
            else:
                raise ValueError('invalid fixture interface operation')
            response = {'ok': True, 'value': _pack(value)}
        except BaseException as exc:
            response = {'ok': False, 'type': 'ValueError' if isinstance(exc, ValueError) else type(exc).__name__,
                        'error': str(exc)[:300]}
        output_stream.write(dumps(response, allow_nan=False, separators=(',', ':')) + '\n')
        output_stream.flush()


class _FixtureInterface:
    def __init__(self, process):
        self.process = process
        self.deadline = time.monotonic() + 60
        self.pending = bytearray()
        self.stderr_size = 0
        for stream in (process.stdin, process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)

    def request(self, **request):
        pending = memoryview((json.dumps(request, allow_nan=False) + '\n').encode())
        if len(pending) > 1_000_000:
            raise RuntimeError('fixture request exceeds its bounded channel')
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(self.process.stdin, selectors.EVENT_WRITE, 'stdin')
                selector.register(self.process.stdout, selectors.EVENT_READ, 'stdout')
                selector.register(self.process.stderr, selectors.EVENT_READ, 'stderr')
                while b'\n' not in self.pending:
                    remaining = self.deadline - time.monotonic()
                    if remaining <= 0:
                        raise RuntimeError('fixture interface timed out')
                    for key, _ in selector.select(min(.1, remaining)):
                        if key.data == 'stdin':
                            pending = pending[os.write(key.fd, pending[:65536]):]
                            if not pending:
                                selector.unregister(key.fileobj)
                        else:
                            chunk = os.read(key.fd, 65536)
                            if key.data == 'stderr':
                                self.stderr_size += len(chunk)
                                if self.stderr_size > 65536:
                                    raise RuntimeError('fixture stderr exceeds its bounded channel')
                                if not chunk:
                                    selector.unregister(key.fileobj)
                            else:
                                if not chunk:
                                    raise RuntimeError('fixture exited before returning its interface result')
                                self.pending.extend(chunk)
                                if len(self.pending) > 1_000_000:
                                    raise RuntimeError('fixture result exceeds its bounded channel')
            line, _, rest = self.pending.partition(b'\n')
            if rest:
                raise RuntimeError('fixture returned unsolicited interface results')
            self.pending.clear()
            response = json.loads(line)
            if type(response) is not dict or type(response.get('ok')) is not bool:
                raise ValueError('invalid fixture response')
            if response['ok']:
                value = _unpack(response['value'])
            elif (type(response.get('type')) is not str or type(response.get('error')) is not str):
                raise ValueError('invalid fixture exception')
        except (OSError, EOFError, KeyError, TypeError, ValueError, RecursionError) as exc:
            raise RuntimeError('fixture interface failed') from exc
        if not response['ok']:
            raise (ValueError if response['type'] == 'ValueError' else RuntimeError)(response['error'][:300])
        return value

    def module(self, name):
        module = types.ModuleType(name)
        def attribute(key):
            if key.startswith('__'):
                raise AttributeError(key)
            if key == 'ITEMS':
                return types.SimpleNamespace(clear=lambda: self.request(
                    module=name, name=key, operation='clear'))
            def call(*args, **kwargs):
                if name == 'api' and key == 'app':
                    environ, start_response = args
                    status, headers, body = self.request(module=name, name=key, operation='wsgi',
                        environ=_pack({k: v for k, v in environ.items() if k != 'wsgi.input'}),
                        body=_pack(environ['wsgi.input'].read()))
                    start_response(status, headers)
                    return [body]
                return self.request(module=name, name=key, operation='call',
                    args=_pack(args), kwargs=_pack(kwargs))
            return call
        module.__getattr__ = attribute
        return module


def run_suite(process, command, original_files):
    """Only trusted frozen test text executes in the parent, with fixture proxies."""
    interface = _FixtureInterface(process)
    modules = {}
    source_names = {Path(name).stem for name in original_files if name.endswith('.py')
                    and not Path(name).name.startswith('test_')}
    def fixture_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level == 0 and name in source_names:
            return modules.setdefault(name, interface.module(name))
        return builtins.__import__(name, globals, locals, fromlist, level)
    suite = unittest.TestSuite()
    for name in command[3:]:
        module = types.ModuleType(name[:-3])
        module.__dict__['__builtins__'] = dict(vars(builtins), __import__=fixture_import)
        exec(compile(original_files[name], '<frozen fixture ' + name + '>', 'exec'), module.__dict__)
        suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(module))
    result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
    return result.testsRun > 0 and result.wasSuccessful()


if __name__ == '__main__':
    serve(sys.argv[1])

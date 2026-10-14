"""Single declaration of canonical tool identity, schema and policy metadata."""
from __future__ import annotations
import copy

# name, capability, side effects, owner, risk, parallel safety, aliases, conversion fields, argument aliases, confirmation surfaces
_ROWS = (
    ('update_plan', 'write', 'internal_write', 'runtime', 'normal', False, (), (), (), ()),
    ('spawn_agents', 'read', 'orchestration', 'orchestration', 'normal', False, (), (), (), ()),
    ('send_subagent', 'read', 'orchestration', 'orchestration', 'normal', False, (), (), (), ()),
    ('list_subagents', 'read', 'orchestration', 'orchestration', 'normal', False, (), (), (), ()),
    ('wait_subagents', 'read', 'orchestration', 'orchestration', 'normal', False, (), (), (), ()),
    ('cancel_subagent', 'read', 'orchestration', 'orchestration', 'normal', False, (), (), (), ()),
    ('check_stored_data', 'read', 'read', 'runtime', 'normal', False, ('check_memory',), (), (), ()),
    ('search_memory', 'read', 'read', 'runtime', 'normal', False, (), (), (), ()),
    ('read_file', 'read', 'read', 'adapter', 'normal', False, (), ('path',), (('path', 'file_path'),), ()),
    ('search_files', 'read', 'read', 'adapter', 'normal', False, (), (), (), ()),
    ('calculate', 'read', 'none', 'adapter', 'normal', True, (), (), (), ()),
    ('discover_tools', 'read', 'read', 'runtime', 'normal', False, (), (), (), ()),
    ('list_dir', 'read', 'read', 'adapter', 'normal', False, (), ('path',), (('path', 'file_path'),), ()),
    ('list_tree', 'read', 'read', 'adapter', 'normal', False, ('tree',), ('path',), (('path', 'file_path'),), ()),
    ('find_files', 'read', 'read', 'adapter', 'normal', False, (), ('pattern', 'directory'), (), ()),
    ('git_status', 'read', 'read', 'adapter', 'normal', False, ('status',), (), (), ()),
    ('git_diff', 'read', 'read', 'adapter', 'normal', False, ('diff',), (), (), ()),
    ('git_log', 'read', 'read', 'adapter', 'normal', False, ('log',), (), (), ()),
    ('git_show', 'read', 'read', 'adapter', 'normal', False, ('show',), (), (), ()),
    ('write_file', 'write', 'mutation', 'adapter', 'normal', False, ('write',), ('path', 'content'), (('path', 'file_path'), ('content', 'text')), ()),
    ('edit_file', 'write', 'mutation', 'adapter', 'normal', False, (), (), (), ()),
    ('run_cmd', 'shell', 'unknown', 'adapter', 'normal', False, ('bash', 'shell', 'sh', 'run_command', 'cmd', 'exec', 'execute', 'run_shell_command', 'run_shell', 'shell_command', 'execute_shell', 'shell_cmd', 'bash_cmd', 'command', 'run'), ('command',), (('command', 'cmd'),), ()),
    ('execute_terminal_command', 'shell', 'unknown', 'adapter', 'normal', False, ('run_terminal_command', 'run_terminal', 'terminal'), ('command',), (('command', 'cmd'),), ()),
    ('search_web', 'network', 'external', 'adapter', 'normal', False, (), ('query',), (), ()),
    ('read_webpage', 'network', 'external', 'adapter', 'normal', False, ('browse_summary',), ('url',), (), ()),
    ('git_clone', 'git', 'mutation', 'adapter', 'high', False, ('clone',), ('url', 'destination'), (), ('tui',)),
    ('git_branch', 'git', 'mutation', 'adapter', 'normal', False, ('branch',), (), (), ()),
    ('analyze_remote_repo', 'network', 'external', 'adapter', 'normal', False, (), ('url',), (), ()),
    ('browser_open', 'browser', 'stateful', 'adapter', 'normal', False, (), ('url',), (), ()),
    ('browser_snapshot', 'browser', 'stateful', 'adapter', 'normal', False, (), ('session_id',), (('session_id', 'sessionId'),), ()),
    ('browser_click', 'browser', 'stateful', 'adapter', 'normal', False, (), ('session_id', 'selector'), (('session_id', 'sessionId'),), ()),
    ('browser_type', 'browser', 'stateful', 'adapter', 'normal', False, (), ('session_id', 'selector', 'text'), (('session_id', 'sessionId'),), ()),
    ('browser_close', 'browser', 'stateful', 'adapter', 'normal', False, (), ('session_id',), (('session_id', 'sessionId'),), ()),
    ('git_add', 'git', 'mutation', 'adapter', 'high', False, ('add',), (), (), ('tui',)),
    ('git_commit', 'git', 'mutation', 'adapter', 'high', False, ('commit',), (), (), ('tui',)),
    ('git_push', 'git', 'mutation', 'adapter', 'high', False, ('push',), (), (), ('cli', 'tui')),
    ('git_pull', 'git', 'mutation', 'adapter', 'high', False, ('pull',), (), (), ('cli', 'tui')),
    ('git_checkout', 'git', 'mutation', 'adapter', 'high', False, ('checkout',), (), (), ('cli', 'tui')),
    ('git_stash', 'git', 'mutation', 'adapter', 'high', False, ('stash',), (), (), ('cli', 'tui')),
    ('git_reset', 'destructive', 'mutation', 'adapter', 'high', False, ('reset',), (), (), ('cli', 'tui')),
    ('git_remote', 'git', 'mutation', 'adapter', 'high', False, ('remote',), (), (), ('cli', 'tui')),
    ('graph_status', 'read', 'read', 'adapter', 'normal', False, (), (), (), ()),
    ('graph_query', 'read', 'read', 'adapter', 'normal', False, (), (), (), ()),
    ('graph_explain', 'read', 'read', 'adapter', 'normal', False, (), (), (), ()),
    ('graph_path', 'read', 'read', 'adapter', 'normal', False, (), (), (), ()),
    ('graph_refresh', 'read', 'internal_write', 'adapter', 'normal', False, (), (), (), ()),
    ('github_status', 'network', 'external', 'adapter', 'normal', False, (), (), (), ()),
    ('github_read', 'network', 'external', 'adapter', 'normal', False, (), (), (), ()),
    ('github_cli', 'git', 'mutation', 'adapter', 'high', False, (), (), (), ('cli', 'tui')),
)

_SCHEMAS = {'read_file': {'properties': {'path': {'type': 'string'}},
               'required': ['path'],
               'type': 'object',
               'additionalProperties': False},
 'list_dir': {'properties': {'path': {'type': 'string'}}, 'type': 'object', 'additionalProperties': False},
 'list_tree': {'properties': {'path': {'type': 'string'}}, 'type': 'object', 'additionalProperties': False},
 'find_files': {'properties': {'pattern': {'type': 'string'}, 'directory': {'type': 'string'}},
                'required': ['pattern'],
                'type': 'object',
                'additionalProperties': False},
 'write_file': {'properties': {'path': {'type': 'string'}, 'content': {'type': 'string'}},
                'required': ['path', 'content'],
                'type': 'object',
                'additionalProperties': False},
 'run_cmd': {'properties': {'command': {'type': 'string'}},
             'required': ['command'],
             'type': 'object',
             'additionalProperties': False},
 'execute_terminal_command': {'properties': {'command': {'type': 'string'}},
                              'required': ['command'],
                              'type': 'object',
                              'additionalProperties': False},
 'search_web': {'properties': {'query': {'type': 'string'}},
                'required': ['query'],
                'type': 'object',
                'additionalProperties': False},
 'read_webpage': {'properties': {'url': {'type': 'string'}},
                  'required': ['url'],
                  'type': 'object',
                  'additionalProperties': False},
 'git_clone': {'properties': {'url': {'type': 'string'}, 'destination': {'type': 'string'}},
               'required': ['url'],
               'type': 'object',
               'additionalProperties': False},
 'analyze_remote_repo': {'properties': {'url': {'type': 'string'}},
                         'required': ['url'],
                         'type': 'object',
                         'additionalProperties': False},
 'browser_open': {'properties': {'url': {'type': 'string'}},
                  'required': ['url'],
                  'type': 'object',
                  'additionalProperties': False},
 'browser_snapshot': {'properties': {'session_id': {'type': 'string'}},
                      'required': ['session_id'],
                      'type': 'object',
                      'additionalProperties': False},
 'browser_click': {'properties': {'session_id': {'type': 'string'}, 'selector': {'type': 'string'}},
                   'required': ['session_id', 'selector'],
                   'type': 'object',
                   'additionalProperties': False},
 'browser_type': {'properties': {'session_id': {'type': 'string'},
                                 'selector': {'type': 'string'},
                                 'text': {'type': 'string'}},
                  'required': ['session_id', 'selector', 'text'],
                  'type': 'object',
                  'additionalProperties': False},
 'browser_close': {'properties': {'session_id': {'type': 'string'}},
                   'required': ['session_id'],
                   'type': 'object',
                   'additionalProperties': False}}

_OBJECT_FIELDS = {
    'update_plan': {'complexity': {'type':'string','enum':['simple','medium','complex']},
                    'tasks': {'type':'array','minItems':1,'items':{'type':'object','properties':{
                        'id': {'type':'string','minLength':1},
                        'description': {'type':'string','minLength':1},
                        'acceptance': {'type':'array','items':{'type':'string','minLength':1}},
                        'execution': {'type':'object','properties':{
                            'action': {'type':'string','minLength':1},
                            'arguments': {'type':'object','properties':{},'additionalProperties':True},
                        },'required':['action','arguments'],'additionalProperties':False},
                    },'required':['description','acceptance'],'additionalProperties':False}}},
    'spawn_agents': {'assignments': {'type':'array','items':{'type':'object','properties':{},'additionalProperties':True}}},
    'send_subagent': {'run_id': {'type':'string'}, 'assignment': {'type':'object','properties':{},'additionalProperties':True}},
    'list_subagents': {'run_id': {'type':'string'}}, 'wait_subagents': {'run_ids': {'type':'array','items':{'type':'string'}}, 'timeout': {'type':'number','minimum':0,'maximum':60}},
    'cancel_subagent': {'run_id': {'type':'string'}},
    'check_stored_data': {'category': {'type':'string'}}, 'search_memory': {'query': {'type':'string','minLength':1}},
    'discover_tools': {'query': {'type':'string'}, 'names': {'type':'array','items':{'type':'string'}}},
    'search_files': {'query': {'type':'string'}, 'path': {'type':'string'}, 'glob': {'type':'string'}, 'limit': {'type':'integer','minimum':1,'maximum':100}},
    'calculate': {'expression': {'type':'string','minLength':1,'maxLength':4000}},
    'read_file': {'path': {'type':'string'}, 'start_line': {'type':'integer','minimum':1}, 'max_lines': {'type':'integer','minimum':1}, 'max_chars': {'type':'integer','minimum':1}},
    'edit_file': {'path': {'type':'string'}, 'old_text': {'type':'string','minLength':1}, 'new_text': {'type':'string'}, 'expected_sha256': {'type':'string'}},
    'git_status': {'path': {'type':'string'}},
    'git_diff': {'options': {'type':'array','items':{'type':'string'}}}, 'git_log': {'options': {'type':'array','items':{'type':'string'}}},
    'git_branch': {'options': {'type':'array','items':{'type':'string'}}}, 'git_add': {'paths': {'type':'array','items':{'type':'string'}}},
    'git_commit': {'message': {'type':'string','minLength':1}}, 'git_push': {'arguments': {'type':'array','items':{'type':'string'}}},
    'git_pull': {'arguments': {'type':'array','items':{'type':'string'}}}, 'git_checkout': {'arguments': {'type':'array','items':{'type':'string'}}},
    'git_stash': {'arguments': {'type':'array','items':{'type':'string'}}}, 'git_reset': {'arguments': {'type':'array','items':{'type':'string'}}},
    'git_show': {'reference': {'type':'string'}}, 'git_remote': {'operation': {'type':'string','enum':['list','add','remove']}, 'name': {'type':'string'}, 'url': {'type':'string'}},
    'git_clone': {'url': {'type':'string'}, 'destination': {'type':'string'}},
    'run_cmd': {'command': {'type':'string','minLength':1}}, 'execute_terminal_command': {'command': {'type':'string','minLength':1}},
    'search_web': {'query': {'type':'string','minLength':1}}, 'read_webpage': {'url': {'type':'string','minLength':1}},
    'analyze_remote_repo': {'url': {'type':'string','minLength':1}},
    'browser_open': {'url': {'type':'string','minLength':1}}, 'browser_snapshot': {'session_id': {'type':'string','minLength':1}},
    'browser_click': {'session_id': {'type':'string','minLength':1}, 'selector': {'type':'string','minLength':1}},
    'browser_type': {'session_id': {'type':'string','minLength':1}, 'selector': {'type':'string','minLength':1}, 'text': {'type':'string'}},
    'browser_close': {'session_id': {'type':'string','minLength':1}},
    'graph_query': {'question': {'type':'string','minLength':1}}, 'graph_explain': {'symbol': {'type':'string','minLength':1}},
    'graph_path': {'source': {'type':'string','minLength':1}, 'target': {'type':'string','minLength':1}}, 'graph_refresh': {'full': {'type':'boolean'}},
    'github_status': {'query': {'type':'string'}}, 'github_read': {'arguments': {'type':'array','items':{'type':'string'}}},
    'github_cli': {'arguments': {'type':'array','items':{'type':'string'}}},
}
for _name, _properties in _OBJECT_FIELDS.items():
    _required = {
        'update_plan': ['complexity', 'tasks'],
        'read_file': ['path'], 'find_files': ['pattern'], 'write_file': ['path', 'content'],
        'run_cmd': ['command'], 'execute_terminal_command': ['command'], 'search_web': ['query'],
        'search_files': ['query'],
        'read_webpage': ['url'], 'git_clone': ['url'], 'analyze_remote_repo': ['url'],
        'browser_open': ['url'], 'browser_snapshot': ['session_id'],
        'browser_click': ['session_id', 'selector'], 'browser_type': ['session_id', 'selector', 'text'],
        'browser_close': ['session_id'], 'send_subagent': ['run_id', 'assignment'],
        'cancel_subagent': ['run_id'], 'search_memory': ['query'], 'calculate': ['expression'],
        'edit_file': ['path', 'old_text', 'new_text', 'expected_sha256'],
        'git_commit': ['message'], 'git_remote': ['operation'], 'graph_query': ['question'],
        'graph_explain': ['symbol'], 'graph_path': ['source', 'target'],
    }.get(_name, [])
    _SCHEMAS[_name] = {'type':'object','properties':_properties,'required':_required,'additionalProperties':False}


def builtin_names(owner=None):
    return tuple(row[0] for row in _ROWS if owner is None or row[3] == owner)


def builtin_metadata(name):
    for row in _ROWS:
        if row[0] != name:
            continue
        _, capability, effects, _, risk, parallel, aliases, legacy_fields, argument_aliases, surfaces = row
        schema = _SCHEMAS.get(name, {'type':'object','properties':{},'additionalProperties':False})
        if name in {'run_cmd', 'execute_terminal_command'}:
            effects = 'process'
        elif name == 'git_reset':
            effects = 'destructive'
        elif name in {'write_file', 'edit_file', 'git_add', 'git_commit', 'git_checkout', 'git_stash', 'git_branch', 'git_remote'}:
            effects = 'filesystem'
        elif name in {'git_clone', 'git_push', 'git_pull', 'github_cli', 'search_web', 'read_webpage',
                      'analyze_remote_repo', 'browser_open', 'browser_click', 'browser_type', 'browser_close',
                      'github_status', 'github_read'}:
            effects = 'remote'
        elif name in {'update_plan', 'graph_refresh', 'spawn_agents', 'send_subagent', 'list_subagents',
                      'wait_subagents', 'cancel_subagent'}:
            effects = 'internal'
        else:
            effects = 'read_only'
        return dict(capability=capability,side_effects=effects,risk=risk,parallel_safe=parallel,aliases=aliases,
                    input_schema=copy.deepcopy(schema),legacy_fields=legacy_fields,legacy_argument_aliases=argument_aliases,
                    confirmation_surfaces=surfaces)
    return None


def builtin_aliases():
    return {alias:row[0] for row in _ROWS for alias in row[6]}

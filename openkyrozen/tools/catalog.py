"""Single declaration of canonical tool identity, schema and policy metadata."""
from __future__ import annotations
import copy

# name, capability, side effects, owner, risk, parallel safety, aliases, conversion fields, argument aliases, confirmation surfaces
_ROWS = (
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


def builtin_names(owner=None):
    return tuple(row[0] for row in _ROWS if owner is None or row[3] == owner)


def builtin_metadata(name):
    for row in _ROWS:
        if row[0] != name:
            continue
        _, capability, effects, _, risk, parallel, aliases, legacy_fields, argument_aliases, surfaces = row
        schema = _SCHEMAS.get(name, {'type':'object','properties':{'args':{'type':'string'}},'additionalProperties':False,
                                   'description':'Plain-string arguments can be supplied as the `args` property.'})
        return dict(capability=capability,side_effects=effects,risk=risk,parallel_safe=parallel,aliases=aliases,
                    input_schema=copy.deepcopy(schema),legacy_fields=legacy_fields,legacy_argument_aliases=argument_aliases,
                    confirmation_surfaces=surfaces)
    return None


def builtin_aliases():
    return {alias:row[0] for row in _ROWS for alias in row[6]}

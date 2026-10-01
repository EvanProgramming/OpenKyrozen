"""Workspace-owned concrete tool adapters."""
from pathlib import Path
from openkyrozen.tools.browser_manager import BrowserManager
from openkyrozen.tools.models import CommandResult
from openkyrozen.security.tool_policy import allowed_tool_names, resolve_capabilities, tool_capability

import socket
from urllib.parse import urlsplit
from .web import _MAX_WEBPAGE_BYTES

class ToolAdapters:
    socket = socket
    urlsplit = staticmethod(urlsplit)
    _MAX_WEBPAGE_BYTES = _MAX_WEBPAGE_BYTES
    CommandResult = CommandResult
    allowed_tool_names = staticmethod(allowed_tool_names)
    resolve_capabilities = staticmethod(resolve_capabilities)
    tool_capability = staticmethod(tool_capability)

    def __init__(self, root=None):
        self._WORKSPACE_ROOT = Path(root or Path.cwd()).expanduser().resolve()
        self._BROWSER = BrowserManager(root=str(self._WORKSPACE_ROOT))
        self._PROJECT_GRAPH = None
        self._GITHUB_CLI = None
        self.AVAILABLE_TOOLS = {'write_file': self.write_file, 'read_file': self.read_file, 'run_cmd': self.run_cmd, 'search_web': self.search_web, 'find_files': self.find_files, 'list_dir': self.list_dir, 'git_clone': self.git_clone, 'git_status': self.git_status, 'git_diff': self.git_diff, 'git_log': self.git_log, 'git_branch': self.git_branch, 'git_add': self.git_add, 'git_commit': self.git_commit, 'git_push': self.git_push, 'git_pull': self.git_pull, 'git_checkout': self.git_checkout, 'git_stash': self.git_stash, 'git_reset': self.git_reset, 'git_show': self.git_show, 'git_remote': self.git_remote, 'execute_terminal_command': self.execute_terminal_command, 'analyze_remote_repo': self.analyze_remote_repo, 'list_tree': self.list_tree, 'read_webpage': self.read_webpage, 'browser_open': self.browser_open, 'browser_snapshot': self.browser_snapshot, 'browser_click': self.browser_click, 'browser_type': self.browser_type, 'browser_close': self.browser_close, 'graph_status': self.graph_status, 'graph_query': self.graph_query, 'graph_explain': self.graph_explain, 'graph_path': self.graph_path, 'graph_refresh': self.graph_refresh, 'github_status': self.github_status, 'github_read': self.github_read, 'github_cli': self.github_cli}
        self.AVAILABLE_TOOLS["calculate"] = self.calculate

    from .calculation import calculate

    from .registry import (set_workspace_root, set_project_graph, set_github_cli, _resolve_workspace_path)

    from .shell import (_is_dangerous, _active_command_env, run_command, run_cmd)

    from .search import (search_web)

    from .filesystem import (find_files, list_dir, list_tree, write_file, read_file)

    from .git import (_git_working_directory, git_clone, git_diff, git_log, git_branch, git_add, git_commit, git_push, git_pull, git_checkout, git_stash, git_reset, git_show, git_remote, git_status, execute_terminal_command)

    from .remote import (analyze_remote_repo)

    from .web import (_is_blocked_web_address, _resolve_web_destination, _read_pinned_web_response, read_webpage)

    from .browser import (browser_open, browser_snapshot, browser_click, browser_type, browser_close)

    from .graph import (graph_status, graph_query, graph_explain, graph_path, graph_refresh)

    from .github import (github_status, github_read, github_cli)

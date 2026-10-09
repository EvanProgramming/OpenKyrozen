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
        from .manifest import ToolRegistry
        from .catalog import builtin_names
        self.tool_registry = ToolRegistry({name:getattr(self,name) for name in builtin_names('adapter')})

    @property
    def AVAILABLE_TOOLS(self):
        return self.tool_registry.tools

    @AVAILABLE_TOOLS.setter
    def AVAILABLE_TOOLS(self,value):
        from .manifest import ToolRegistry
        previous = self.tool_registry.snapshot() if hasattr(self,'tool_registry') else {}
        registry = ToolRegistry()
        for name,function in value.items():
            if name in previous:
                registry.register_spec(previous[name].replace(executor=function))
            else:
                registry.register(name,function)
        self.tool_registry = registry

    from .calculation import calculate

    from .registry import (set_workspace_root, set_project_graph, set_github_cli, _resolve_workspace_path)

    from .shell import (_is_dangerous, _active_command_env, run_command, run_cmd)

    from .search import (search_web)

    from .filesystem import (find_files, list_dir, list_tree, write_file, edit_file, read_file, search_files)

    from .git import (_git_working_directory, git_clone, git_diff, git_log, git_branch, git_add, git_commit, git_push, git_pull, git_checkout, git_stash, git_reset, git_show, git_remote, git_status, execute_terminal_command)

    from .remote import (analyze_remote_repo)

    from .web import (_is_blocked_web_address, _resolve_web_destination, _read_pinned_web_response, read_webpage)

    from .browser import (browser_open, browser_snapshot, browser_click, browser_type, browser_close)

    from .graph import (graph_status, graph_query, graph_explain, graph_path, graph_refresh)

    from .github import (github_status, github_read, github_cli)

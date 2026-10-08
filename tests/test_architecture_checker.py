"""Exercise dependency policy against real temporary Python packages."""
import tempfile
import unittest
from pathlib import Path

from scripts import check_architecture


class ArchitectureCheckerTests(unittest.TestCase):
    def check_sources(self, sources):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, source in sources.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(source, encoding="utf-8")
            return check_architecture.check(root)

    def test_core_rejects_interface_root_and_descendant_imports(self):
        for source in (
            "import openkyrozen.interfaces",
            "import openkyrozen.interfaces as ui",
            "from openkyrozen import interfaces",
            "from openkyrozen.interfaces import render",
            "import openkyrozen.interfaces.cli",
            "from .. import interfaces",
            "from ..interfaces import render",
            "from ..interfaces import cli",
            "def render():\n    from .. import interfaces",
        ):
            for module in ("agent", "memory", "learning", "tasks"):
                with self.subTest(source=source, module=module):
                    errors = self.check_sources({
                        f"openkyrozen/{module}/probe.py": source,
                        "openkyrozen/interfaces/__init__.py": "",
                        "openkyrozen/interfaces/cli.py": "",
                    })
                    self.assertTrue(any("core imports adapter openkyrozen.interfaces" in e
                                        for e in errors), errors)

    def test_core_package_init_rejects_relative_interface_import(self):
        errors = self.check_sources({
            "openkyrozen/agent/__init__.py": "from .. import interfaces",
            "openkyrozen/interfaces/__init__.py": "",
        })
        self.assertTrue(any("core imports adapter" in e for e in errors), errors)

    def test_package_root_initializer_can_import_core_package(self):
        self.assertEqual(self.check_sources({
            "openkyrozen/__init__.py": "from . import agent",
            "openkyrozen/agent/__init__.py": "",
        }), [])

    def test_allowed_dependencies_and_composition_remain_valid(self):
        self.assertEqual(self.check_sources({
            "openkyrozen/agent/probe.py": "from ..providers.base import LLMProvider\n"
                "from ..security.capabilities import CapabilityToken\n"
                "from ..tasks.ports import TaskStore\n"
                "import openkyrozen.interfaces_extra",
            "openkyrozen/app/bootstrap.py": "import openkyrozen.interfaces\n"
                "import openkyrozen.persistence.database",
            "openkyrozen/interfaces/__init__.py": "",
            "openkyrozen/interfaces_extra.py": "",
        }), [])

    def test_concrete_and_external_adapters_remain_forbidden(self):
        for source in ("import sqlite3", "from subprocess import run",
                       "from ..persistence.database import SQLiteDatabase",
                       "import openkyrozen.tools.adapters"):
            with self.subTest(source=source):
                errors = self.check_sources({"openkyrozen/tasks/probe.py": source})
                self.assertTrue(any("core imports adapter" in e for e in errors), errors)

    def test_legacy_imports_remain_forbidden(self):
        for source in ("import tools", "from main import Agent"):
            with self.subTest(source=source):
                errors = self.check_sources({"openkyrozen/app/probe.py": source})
                self.assertTrue(any("legacy import" in e for e in errors), errors)

    def test_runtime_cycle_is_detected_but_type_only_cycle_is_not(self):
        sources = {
            "openkyrozen/tasks/a.py": "from . import b",
            "openkyrozen/tasks/b.py": "from . import a",
        }
        self.assertTrue(any("Import cycle:" in e for e in self.check_sources(sources)))
        sources["openkyrozen/tasks/b.py"] = (
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from . import a\n"
        )
        self.assertEqual(self.check_sources(sources), [])

    def test_qualified_type_checking_guard_excludes_only_type_branch(self):
        sources = {
            "openkyrozen/tasks/a.py": "from . import b",
            "openkyrozen/tasks/b.py": (
                "import typing\nif typing.TYPE_CHECKING:\n    from . import a\n"
            ),
        }
        self.assertEqual(self.check_sources(sources), [])
        sources["openkyrozen/tasks/b.py"] += "else:\n    from . import a\n"
        self.assertTrue(any("Import cycle:" in e for e in self.check_sources(sources)))
        sources["openkyrozen/tasks/b.py"] = (
            "other = object()\nif other.TYPE_CHECKING:\n    from . import a\n"
        )
        self.assertTrue(any("Import cycle:" in e for e in self.check_sources(sources)))

    def test_cycle_imports_in_compound_statements_are_detected(self):
        for source in (
            "try:\n    pass\nexcept Exception:\n    pass\nelse:\n    from . import a",
            "try:\n    pass\nfinally:\n    from . import a",
            "with context():\n    from . import a",
            "for item in items:\n    from . import a",
            "while ready:\n    from . import a",
            "for item in items:\n    pass\nelse:\n    from . import a",
            "match value:\n    case 1:\n        from . import a",
            "if ready:\n    pass\nelse:\n    with context():\n        from . import a",
            "class Binding:\n    from . import a",
        ):
            with self.subTest(source=source):
                self.assertTrue(any("Import cycle:" in e for e in self.check_sources({
                    "openkyrozen/tasks/a.py": "from . import b",
                    "openkyrozen/tasks/b.py": source,
                })))
        for source in ("def later():\n    from . import a",
                       "async def later():\n    from . import a",
                       "class Binding:\n    def later(self):\n        from . import a"):
            with self.subTest(source=source):
                self.assertEqual(self.check_sources({
                    "openkyrozen/tasks/a.py": "from . import b",
                    "openkyrozen/tasks/b.py": source,
                }), [])

    def test_named_reexports_do_not_hide_concrete_adapters(self):
        sources = {
            "openkyrozen/tools/__init__.py": (
                "from .adapters import ToolAdapters as Adapters\n"
                "from .models import CommandResult\n"
            ),
            "openkyrozen/tools/adapters.py": "class ToolAdapters: pass",
            "openkyrozen/tools/models.py": "class CommandResult: pass",
            "openkyrozen/providers/__init__.py": (
                "from .openai import OpenAICompatProvider\n"
                "from .base import LLMProvider\n"
            ),
            "openkyrozen/providers/openai.py": "class OpenAICompatProvider: pass",
            "openkyrozen/providers/base.py": "class LLMProvider: pass",
            "openkyrozen/tools/public.py": "from . import Adapters as Wrapped",
        }
        for source in ("from openkyrozen.tools import Adapters",
                       "from ..tools import Adapters as Factory",
                       "from ..tools.public import Wrapped",
                       "from ..providers import OpenAICompatProvider",
                       "from ..tools import *"):
            with self.subTest(source=source):
                errors = self.check_sources(sources | {"openkyrozen/agent/probe.py": source})
                self.assertTrue(any("core imports adapter" in e for e in errors), errors)
        self.assertEqual(self.check_sources(sources | {
            "openkyrozen/agent/probe.py": (
                "from ..tools import CommandResult\nfrom ..providers import LLMProvider"
            ),
        }), [])

    def test_module_reexport_alias_does_not_hide_adapter(self):
        errors = self.check_sources({
            "openkyrozen/tools/__init__.py": "from . import adapters as Backend",
            "openkyrozen/tools/adapters.py": "",
            "openkyrozen/agent/probe.py": "from ..tools import Backend",
        })
        self.assertTrue(any("core imports adapter openkyrozen.tools.adapters" in e
                            for e in errors), errors)

    def test_wildcard_reexports_respect_literal_all(self):
        sources = {
            "openkyrozen/tools/__init__.py": (
                "from .adapters import ToolAdapters, ToolAdapters as _Factory\n"
                "from .models import CommandResult\n__all__ = ['CommandResult']"
            ),
            "openkyrozen/tools/adapters.py": "class ToolAdapters: pass",
            "openkyrozen/tools/models.py": "class CommandResult: pass",
            "openkyrozen/agent/probe.py": "from ..tools import *",
        }
        self.assertEqual(self.check_sources(sources), [])
        sources["openkyrozen/tools/__init__.py"] += "\n__all__ = ['CommandResult', '_Factory']"
        self.assertTrue(any("core imports adapter" in e for e in self.check_sources(sources)))

    def test_circular_reexport_resolution_terminates_with_cycle_error(self):
        errors = self.check_sources({
            "openkyrozen/tools/__init__.py": "from .public import Factory",
            "openkyrozen/tools/public.py": "from . import Factory",
            "openkyrozen/agent/probe.py": "from ..tools import Factory",
        })
        self.assertTrue(any("Import cycle:" in e for e in errors), errors)

import io
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from openkyrozen.updates.service import _available_update, _self_update, _update_version_key


class UpdateNoticeTests(unittest.TestCase):
    def test_preview_update_version_order(self):
        current = _update_version_key("3.0.0.dev1")
        for version, allowed in (("2.0.8", False), ("3.0.0.dev0", False),
                                 ("3.0.0.dev1", True), ("3.0.0.dev2", True),
                                 ("3.0.0a1", True), ("3.0.0rc1", True),
                                 ("3.0.0", True), ("3.0.1.dev0", True)):
            with self.subTest(version=version):
                self.assertEqual(_update_version_key(version) >= current, allowed)
        self.assertIsNone(_update_version_key("not-a-version"))

    def test_preview_update_blocks_downgrade_and_unknown_version_before_staging(self):
        revision = "a" * 40
        for response in (io.BytesIO(b'[project]\nversion = "2.0.8"\n'),
                         io.BytesIO(b'[project]\nversion = "3.0.0.dev0"\n'),
                         io.BytesIO(b'[project]\nversion = "unsupported"\n'), OSError("offline")):
            with self.subTest(response=type(response).__name__):
                runtime = SimpleNamespace(
                    RELEASE_VERSION="3.0.0.dev1", _resolve_update_revision=lambda: revision,
                    UPDATE_REPOSITORY_URL="https://example.invalid/repo", _IS_WINDOWS=False,
                    _update_lock=nullcontext, _fix_safe_text=lambda value, *_: str(value),
                    _stage_update_tui=Mock(side_effect=RuntimeError("staging reached")),
                )
                with tempfile.TemporaryDirectory() as directory, \
                     patch("openkyrozen.updates.service.Path.home", return_value=Path(directory)), \
                     patch("openkyrozen.updates.service.shutil.which", return_value="/usr/bin/uv"), \
                     patch("openkyrozen.updates.service.urllib.request.urlopen", side_effect=(
                         response if isinstance(response, Exception) else None), return_value=response):
                    result = _self_update(runtime)
                self.assertEqual(result.status, "failed")
                self.assertIn("version", result.message.lower())
                runtime._stage_update_tui.assert_not_called()

    def test_preview_update_checks_the_exact_revision_and_allows_a_newer_version(self):
        revision = "a" * 40
        runtime = SimpleNamespace(
            RELEASE_VERSION="3.0.0.dev1", _resolve_update_revision=lambda: revision,
            UPDATE_REPOSITORY_URL="https://example.invalid/repo", _IS_WINDOWS=False,
            _update_lock=nullcontext, _fix_safe_text=lambda value, *_: str(value),
            _stage_update_tui=Mock(side_effect=RuntimeError("staging reached")),
        )
        with tempfile.TemporaryDirectory() as directory, \
             patch("openkyrozen.updates.service.Path.home", return_value=Path(directory)), \
             patch("openkyrozen.updates.service.shutil.which", return_value="/usr/bin/uv"), \
             patch("openkyrozen.updates.service.urllib.request.urlopen",
                   return_value=io.BytesIO(b'[project]\nversion = "3.0.0"\n')) as open_url:
            result = _self_update(runtime)
        self.assertIn("staging reached", result.message)
        runtime._stage_update_tui.assert_called_once()
        self.assertIn(f"/{revision}/pyproject.toml", open_url.call_args.args[0].full_url)

    def check(self, latest, current="2.0.4", installed="a" * 40, contained=1, checkout=True):
        runtime = SimpleNamespace(__version__=current, _resolve_update_revision=lambda *a, **k: "b" * 40)
        document = f'[project]\nversion = "{latest}"\n'.encode()
        with patch("openkyrozen.updates.service.urllib.request.urlopen", return_value=io.BytesIO(document)), \
                patch("openkyrozen.updates.service.Path.exists", return_value=checkout), \
                patch("openkyrozen.updates.service.subprocess.run", side_effect=[
                    SimpleNamespace(returncode=contained), SimpleNamespace(stdout=installed),
                ]), \
                patch("openkyrozen.updates.service.importlib.metadata.distribution", return_value=SimpleNamespace(
                    read_text=lambda _: '{"vcs_info":{"commit_id":"' + installed + '"}}',
                )):
            return _available_update(runtime)

    def test_versions_and_main_revision_match_updater_source(self):
        self.assertEqual(self.check("2.0.10"), "2.0.10")
        self.assertIsNone(self.check("2.0.3"))
        self.assertIsNone(self.check("2.0.4", installed="b" * 40))
        self.assertIsNone(self.check("2.0.4", contained=0))
        self.assertEqual(self.check("2.0.4"), "2.0.4 · bbbbbbb")
        self.assertEqual(self.check("2.0.4", checkout=False), "2.0.4 · bbbbbbb")
        self.assertIsNone(self.check("not-a-version"))
        self.assertEqual(self.check("3.0.0", current="3.0.0.dev1"), "3.0.0")
        self.assertIsNone(self.check("2.0.8", current="3.0.0.dev1"))

    def test_offline_check_is_silent(self):
        runtime = SimpleNamespace(_resolve_update_revision=lambda **_: None)
        self.assertIsNone(_available_update(runtime))
        runtime._resolve_update_revision = lambda **_: "b" * 40
        with patch("openkyrozen.updates.service.urllib.request.urlopen", side_effect=OSError("offline")):
            self.assertIsNone(_available_update(runtime))

import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from openkyrozen.updates.service import _available_update


class UpdateNoticeTests(unittest.TestCase):
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

    def test_offline_check_is_silent(self):
        runtime = SimpleNamespace(_resolve_update_revision=lambda **_: None)
        self.assertIsNone(_available_update(runtime))
        runtime._resolve_update_revision = lambda **_: "b" * 40
        with patch("openkyrozen.updates.service.urllib.request.urlopen", side_effect=OSError("offline")):
            self.assertIsNone(_available_update(runtime))

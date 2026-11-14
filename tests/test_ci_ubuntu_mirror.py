import importlib.util
from pathlib import Path
import tempfile
import unittest


class UbuntuCIMirrorTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / "scripts/ci_ubuntu_mirror.py"
        self.assertTrue(path.is_file(), "CI needs its shared Ubuntu source rewrite")
        spec = importlib.util.spec_from_file_location("ci_ubuntu_mirror", path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "sources.list.d").mkdir()

    def test_deb822_preserves_suites_components_and_ubuntu_signature_key(self):
        path = self.root / "sources.list.d/ubuntu.sources"
        path.write_text("Types: deb\nURIs: https://archive.ubuntu.com/ubuntu/ http://security.ubuntu.com/ubuntu\nSuites: resolute resolute-updates resolute-security\nComponents: main universe\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n")
        self.module.configure_ubuntu_mirror(self.root)
        self.assertEqual(path.read_text(), "Types: deb\nURIs: https://mirrors.kernel.org/ubuntu/ https://mirrors.kernel.org/ubuntu/\nSuites: resolute resolute-updates resolute-security\nComponents: main universe\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n")

    def test_legacy_sources_and_mirror_file_use_the_same_signed_archive(self):
        path = self.root / "sources.list"
        mirrors = self.root / "apt-mirrors.txt"
        path.write_text("deb https://azure.archive.ubuntu.com/ubuntu/ resolute main\n")
        mirrors.write_text("https://azure.archive.ubuntu.com/ubuntu/\nhttps://security.ubuntu.com/ubuntu/\n")
        indirection = self.root / "sources.list.d/ubuntu.sources"
        indirection.write_text("Types: deb\nURIs: mirror+file:/etc/apt/apt-mirrors.txt\nSuites: resolute\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n")
        self.module.configure_ubuntu_mirror(self.root)
        self.assertEqual(path.read_text(), "deb https://mirrors.kernel.org/ubuntu/ resolute main\n")
        self.assertEqual(indirection.read_text(), "Types: deb\nURIs: mirror+file:/etc/apt/apt-mirrors.txt\nSuites: resolute\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n")
        self.assertEqual(mirrors.read_text(), "https://mirrors.kernel.org/ubuntu/\nhttps://mirrors.kernel.org/ubuntu/\n")

    def test_unrelated_repositories_and_lookalike_urls_are_unchanged(self):
        path = self.root / "sources.list"
        path.write_text("deb http://archive.ubuntu.com/ubuntu resolute main\n# https://archive.ubuntu.com/ubuntu-extra\n# https://archive.ubuntu.com.evil/ubuntu/\n# https://example.org/ubuntu/\n")
        extra = self.root / "sources.list.d/vendor.list"
        extra.write_text("deb https://vendor.example/repo stable main\n")
        self.module.configure_ubuntu_mirror(self.root)
        self.assertEqual(path.read_text(), "deb https://mirrors.kernel.org/ubuntu/ resolute main\n# https://archive.ubuntu.com/ubuntu-extra\n# https://archive.ubuntu.com.evil/ubuntu/\n# https://example.org/ubuntu/\n")
        self.assertEqual(extra.read_text(), "deb https://vendor.example/repo stable main\n")

    def test_second_preparation_does_not_rewrite_configured_sources(self):
        path = self.root / "apt-mirrors.txt"
        path.write_text("https://mirrors.kernel.org/ubuntu/\n")
        before = path.stat().st_mtime_ns
        self.module.configure_ubuntu_mirror(self.root)
        self.assertEqual(path.stat().st_mtime_ns, before)

    def test_unknown_layout_fails_without_modifying_other_files(self):
        path = self.root / "sources.list"
        path.write_text("deb https://vendor.example/repo stable main\n")
        with self.assertRaisesRegex(ValueError, "Ubuntu"):
            self.module.configure_ubuntu_mirror(self.root)
        self.assertEqual(path.read_text(), "deb https://vendor.example/repo stable main\n")


if __name__ == "__main__":
    unittest.main()

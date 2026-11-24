"""Public installation commands must resolve before the V3 release exists."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release_site', ROOT / 'website/build_site.py')
site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(site)


class SiteReleaseBoundaryTests(unittest.TestCase):
    def test_rendered_downloads_use_published_stable_release(self):
        for lang in ('en', 'zh-cn'):
            for slug in ('home', 'install'):
                with self.subTest(lang=lang, slug=slug):
                    rendered = site.document(lang, slug, site.PAGES[lang][slug])
                    self.assertIn('/v2.0.8/install.', rendered)
                    self.assertNotIn('/v3.0.0/', rendered)
                    self.assertNotIn('openkyrozen-3.0.0-py3', rendered)

    def test_platform_switcher_preserves_published_download(self):
        script = (ROOT/'website/assets/install-platform.js').read_text()
        self.assertIn('/v2.0.8/install.sh', script)
        self.assertIn('/v2.0.8/install.ps1', script)
        self.assertNotIn('/v3.0.0/', script)

    def test_v3_is_identified_as_unreleased_in_both_languages(self):
        for lang, phrase in (('en','V3 is not yet released'),('zh-cn','V3 尚未发布')):
            rendered = site.document(lang,'install',site.PAGES[lang]['install'])
            self.assertIn(phrase, rendered)

    def test_readme_downloads_also_use_the_published_release(self):
        for readme in ROOT.glob('README*.md'):
            text=readme.read_text()
            with self.subTest(readme=readme.name):
                self.assertTrue('/v2.0.8/install.' in text)
                self.assertFalse('/v3.0.0/' in text)

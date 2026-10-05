from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from check_docs import (
    _check_index_coverage,
    _check_provider_defaults,
    _check_release_references,
    _heading_anchors,
    anchor_for_heading,
    check_markdown_links,
    markdown_links,
)
from openkyrozen.providers import PROVIDER_DEFAULT_MODELS


class DocumentationLinkTests(unittest.TestCase):
    def test_nested_relative_link_resolves_from_document_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "docs" / "guide.md"
            source.parent.mkdir()
            target = root / "docs" / "reference.md"
            target.write_text("# Reference\n", encoding="utf-8")
            source.write_text("[reference](reference.md#reference)\n", encoding="utf-8")

            self.assertEqual(check_markdown_links(source, root), [])

    def test_broken_path_is_reported_with_line_number(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "README.md"
            source.write_text("# Guide\n\n[missing](docs/missing.md)\n", encoding="utf-8")

            errors = check_markdown_links(source, root)

            self.assertEqual(len(errors), 1)
            self.assertIn("README.md:3", errors[0])
            self.assertIn("docs/missing.md", errors[0])

    def test_duplicate_headings_receive_github_style_unique_anchors(self):
        self.assertEqual(anchor_for_heading("Quick Start"), "quick-start")
        self.assertEqual(anchor_for_heading("API: /api/v2/tasks"), "api-apiv2tasks")
        self.assertEqual(
            _heading_anchors("# Quick Start\n# Quick Start\n"),
            {"quick-start", "quick-start-1"},
        )

    def test_markdown_links_ignore_fenced_code(self):
        links = markdown_links("[valid](guide.md)\n```md\n[example](missing.md)\n```\n")
        self.assertEqual(links, [(1, "guide.md")])

    def test_documented_environment_defaults_match_the_provider_registry(self):
        errors = _check_provider_defaults(
            Path("configuration.md"),
            "export KYROZEN_MODEL_SIMPLE=deepseek-flash\n"
            "export KYROZEN_MODEL_COMPLEX=deepseek-v4-pro\n",
            PROVIDER_DEFAULT_MODELS,
        )
        self.assertEqual(errors, [])

    def test_missing_heading_fragment_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "guide.md"
            target.write_text("# Existing\n", encoding="utf-8")
            source = root / "README.md"
            source.write_text("[bad link](guide.md#missing)\n", encoding="utf-8")

            self.assertIn("missing anchor 'missing'", check_markdown_links(source, root)[0])

    def test_index_must_list_every_document_but_not_itself(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "docs"
            root.mkdir()
            (root / "index.md").write_text("## Guides\n", encoding="utf-8")
            (root / "guide.md").write_text("# Guide\n", encoding="utf-8")

            errors = _check_index_coverage(root)

            self.assertEqual(len(errors), 1)
            self.assertIn("guide.md", errors[0])

    def test_readmes_and_website_must_use_one_current_install_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            readme = root / "README.md"
            website = root / "install.js"
            readme.write_text("https://host/v2.0.6/install.sh\n", encoding="utf-8")
            website.write_text("https://host/v2.0.5/install.sh\n", encoding="utf-8")

            errors = _check_release_references([readme], [website])

            self.assertTrue(any("release version" in error for error in errors))


if __name__ == "__main__":
    unittest.main()

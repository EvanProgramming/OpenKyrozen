import importlib.util
import hashlib
import io
import json
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class PreviewPublicationTests(unittest.TestCase):
    def test_malformed_git_signature_status_is_rejected(self):
        from scripts import publish_v3_preview
        result = subprocess.CompletedProcess([], 0, "[GNUPG:] VALIDSIG \n", "")
        with patch.object(publish_v3_preview, "_run", return_value=result):
            with self.assertRaisesRegex(ValueError, "signature"):
                publish_v3_preview._verify_candidate_signature(Path("."), "a" * 40)

    def test_signed_tag_retry_accepts_matching_tag_and_rejects_remote_mismatch(self):
        from scripts import publish_v3_preview
        ensure = getattr(publish_v3_preview, "_ensure_signed_remote_tag", None)
        self.assertTrue(callable(ensure))
        candidate, tag_object, tag = "a" * 40, "b" * 40, "v3.0.0.dev1"
        calls = []

        def run(args, *, cwd):
            calls.append(args)
            if args[:2] == ["git", "rev-parse"]:
                value = candidate if args[-1].endswith("^{}") else tag_object
            elif args[:3] == ["git", "ls-remote", "--tags"]:
                value = candidate if args[-1].endswith("^{}") else tag_object
            else:
                value = ""
            return subprocess.CompletedProcess(args, 0, value + "\n", "")

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(publish_v3_preview, "_verify_git_signature"), \
             patch.object(publish_v3_preview, "_run", side_effect=run), \
             patch.object(publish_v3_preview.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            ensure(Path(directory), tag, candidate)
            self.assertFalse(any(args[:2] == ["git", "push"] for args in calls))
            with patch.object(publish_v3_preview, "_run", side_effect=lambda args, **kw:
                       subprocess.CompletedProcess(args, 0, ("c" * 40 if args[:3] == ["git", "ls-remote", "--tags"]
                                                              else candidate) + "\n", "")):
                with self.assertRaisesRegex(ValueError, "tag"):
                    ensure(Path(directory), tag, candidate)

    def test_changed_release_asset_or_attestation_blocks_publication(self):
        from scripts import publish_v3_preview
        verify = getattr(publish_v3_preview, "_verify_prepared_artifacts", None)
        self.assertTrue(callable(verify))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            asset = root / "preview.whl"
            asset.write_bytes(b"original")
            attestation = root / "attestation.json"
            attestation.with_suffix(".json.asc").write_text("signature")
            hashes = {asset.name: hashlib.sha256(asset.read_bytes()).hexdigest()}
            expected = {"candidate_sha": "a" * 40, "artifacts": hashes}
            attestation.write_text(json.dumps(expected))
            with patch.object(publish_v3_preview, "_gpg_verify", return_value=True):
                verify([asset], expected, attestation)
                asset.write_bytes(b"tampered")
                with self.assertRaisesRegex(ValueError, "changed"):
                    verify([asset], expected, attestation)
            asset.write_bytes(b"original")
            attestation.write_text(json.dumps({"candidate_sha": "b" * 40, "artifacts": hashes}))
            with patch.object(publish_v3_preview, "_gpg_verify", return_value=True):
                with self.assertRaisesRegex(ValueError, "changed"):
                    verify([asset], expected, attestation)
            attestation.write_text(json.dumps(expected))
            with patch.object(publish_v3_preview, "_gpg_verify", return_value=False):
                with self.assertRaisesRegex(ValueError, "changed"):
                    verify([asset], expected, attestation)

    def test_preview_requires_a_workflow_that_cannot_publish_v3_as_stable(self):
        from scripts import publish_v3_preview
        guard = getattr(publish_v3_preview, "_require_safe_release_workflow", None)
        self.assertTrue(callable(guard))
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            workflow = repo / ".github/workflows/release.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text('on:\n  push:\n    tags: ["v*"]\njobs:\n  build-release: {}\n')
            with self.assertRaisesRegex(ValueError, "ungated"):
                guard(repo, "v3.0.0.dev1")
            workflow.write_text('on:\n  push:\n    tags: ["v*", "!v3.*"]\njobs:\n  build-release:\n'
                                '    if: "${{ !startsWith(inputs.release_tag || github.ref_name, \'v3.\') || true }}"\n')
            with self.assertRaisesRegex(ValueError, "ungated"):
                guard(repo, "v3.0.0.dev1")

    def test_invalid_evidence_cannot_create_a_tag_or_release(self):
        self.assertIsNotNone(importlib.util.find_spec("scripts.publish_v3_preview"))
        from scripts import publish_v3_preview

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, evidence = root / "repo", root / "evidence"
            repo.mkdir()
            evidence.mkdir()
            manifest = root / "preview.json"
            manifest.write_text("{}")
            manifest.with_suffix(".json.asc").write_text("signature")
            with patch.object(publish_v3_preview, "_current_candidate", return_value="a" * 40), \
                 patch.object(publish_v3_preview, "_verify_candidate_signature"), \
                 patch.object(publish_v3_preview, "verify_signed_preview_manifest",
                              side_effect=ValueError("missing safety evidence")), \
                 patch.object(publish_v3_preview.subprocess, "run") as run:
                with self.assertRaisesRegex(ValueError, "missing safety evidence"):
                    publish_v3_preview.publish(repo, evidence, manifest)
            run.assert_not_called()

    def test_valid_preview_publication_uses_prerelease_and_candidate_tag(self):
        from scripts import publish_v3_preview

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, evidence, output = root / "repo", root / "evidence", root / "output"
            repo.mkdir()
            evidence.mkdir()
            (repo / "pyproject.toml").write_text('[project]\nversion = "3.0.0.dev1"\n')
            manifest_path = root / "preview.json"
            manifest_path.write_text("{}")
            manifest_path.with_suffix(".json.asc").write_text("signature")
            waiver = evidence / "R259.json"
            waiver.write_text("waiver")
            signature = evidence / "R259.json.asc"
            signature.write_text("signature")
            manifest = {"waived_gates": ["R259"], "gates": {"R259": {"waiver": {
                "rationale": "Evaluation interrupted", "limitations": ["Protocol recovery is unverified"],
                "review_condition": "Review before general availability", "record_path": waiver.name,
                "record_sha256": hashlib.sha256(waiver.read_bytes()).hexdigest(),
                "signature_path": signature.name, "signature_sha256": hashlib.sha256(signature.read_bytes()).hexdigest(),
            }}}}
            calls = []
            tamper_download = False

            def run(args, *, cwd):
                calls.append(args)
                if args[:3] == ["git", "remote", "get-url"]:
                    return subprocess.CompletedProcess(args, 0, "https://github.com/EvanProgramming/OpenKyrozen.git\n", "")
                if args[:3] == ["git", "archive", "--format=tar"]:
                    target = Path(next(arg.split("=", 1)[1] for arg in args if arg.startswith("--output=")))
                    with tarfile.open(target, "w") as archive:
                        data = b"source"
                        info = tarfile.TarInfo("placeholder")
                        info.size = len(data)
                        archive.addfile(info, io.BytesIO(data))
                if args[1:3] == ["-m", "build"]:
                    (output / "openkyrozen-3.0.0.dev1-py3-none-any.whl").write_bytes(b"wheel")
                    (output / "openkyrozen-3.0.0.dev1.tar.gz").write_bytes(b"sdist")
                if args[:3] == ["git", "archive", "--format=tar.gz"]:
                    Path(next(arg.split("=", 1)[1] for arg in args if arg.startswith("--output="))).write_bytes(b"tui")
                if args[:2] == ["git", "rev-parse"] or (args[:3] == ["git", "ls-remote", "--tags"]
                                                          and args[-1].endswith("^{}")):
                    return subprocess.CompletedProcess(args, 0, "a" * 40 + "\n", "")
                if args[:3] == ["gh", "release", "download"]:
                    target = Path(args[args.index("--dir") + 1])
                    for path in output.iterdir():
                        if path.is_file():
                            shutil.copyfile(path, target / path.name)
                    if tamper_download:
                        (target / "openkyrozen-3.0.0.dev1-py3-none-any.whl").write_bytes(b"changed")
                if args[:3] == ["gh", "release", "view"]:
                    published = any(call[:3] == ["gh", "release", "edit"] for call in calls)
                    return subprocess.CompletedProcess(args, 0, json.dumps({"tagName": "v3.0.0.dev1",
                        "isPrerelease": True, "isDraft": not published,
                        "url": "https://github.com/EvanProgramming/OpenKyrozen/releases/tag/v3.0.0.dev1"}), "")
                return subprocess.CompletedProcess(args, 0, "", "")

            def sign(data, path):
                path.write_text(json.dumps(data))
                path.with_suffix(".json.asc").write_text("signature")

            with patch.object(publish_v3_preview, "_current_candidate", return_value="a" * 40), \
                 patch.object(publish_v3_preview, "_verify_candidate_signature"), \
                 patch.object(publish_v3_preview, "_verify_git_signature"), \
                 patch.object(publish_v3_preview, "_require_safe_release_workflow"), \
                 patch.object(publish_v3_preview, "_gpg_verify", return_value=True), \
                 patch.object(publish_v3_preview, "verify_signed_preview_manifest", return_value=manifest), \
                 patch.object(publish_v3_preview, "_write_signed_manifest", side_effect=sign), \
                 patch.object(publish_v3_preview, "_run", side_effect=run), \
                 patch.object(publish_v3_preview.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)):
                url = publish_v3_preview.publish(repo, evidence, manifest_path, output)
            create = next(args for args in calls if args[:3] == ["gh", "release", "create"])
            self.assertIn("--prerelease", create)
            self.assertIn("--draft", create)
            self.assertIn("--verify-tag", create)
            self.assertEqual(create[3], "v3.0.0.dev1")
            self.assertTrue(any(args[:3] == ["gh", "release", "download"] for args in calls))
            self.assertTrue(any(args[:3] == ["gh", "release", "edit"] and "--draft=false" in args
                                for args in calls))
            self.assertIn("R259", (output / "preview-release-notes.md").read_text())
            self.assertIn("KYROZEN_SANDBOX_REQUIRED=1 kyrozen", (output / "preview-release-notes.md").read_text())
            self.assertIn("/v3.0.0.dev1/install.sh", (output / "preview-release-notes.md").read_text())
            self.assertIn("v3.0.0.dev1", url)
            self.assertLess(next(i for i, args in enumerate(calls) if args[:3] == ["gh", "release", "download"]),
                            next(i for i, args in enumerate(calls) if args[:3] == ["gh", "release", "edit"]))

            calls.clear()
            output = root / "mismatched-output"
            tamper_download = True
            with patch.object(publish_v3_preview, "_current_candidate", return_value="a" * 40), \
                 patch.object(publish_v3_preview, "_verify_candidate_signature"), \
                 patch.object(publish_v3_preview, "_verify_git_signature"), \
                 patch.object(publish_v3_preview, "_require_safe_release_workflow"), \
                 patch.object(publish_v3_preview, "_gpg_verify", return_value=True), \
                 patch.object(publish_v3_preview, "verify_signed_preview_manifest", return_value=manifest), \
                 patch.object(publish_v3_preview, "_write_signed_manifest", side_effect=sign), \
                 patch.object(publish_v3_preview, "_run", side_effect=run), \
                 patch.object(publish_v3_preview.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)):
                with self.assertRaisesRegex(ValueError, "draft release assets"):
                    publish_v3_preview.publish(repo, evidence, manifest_path, output)
            self.assertFalse(any(args[:3] == ["gh", "release", "edit"] for args in calls))


if __name__ == "__main__":
    unittest.main()

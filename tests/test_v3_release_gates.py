import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stderr

from benchmarks.v3_release_gates import GATE_IDS, REQUIRED_PLATFORMS, main, validate_evidence_bundle


class V3ReleaseGateTests(unittest.TestCase):
    def create_bundle(self, root, *, mutate=None):
        records = root / "records"
        records.mkdir()
        candidate = "a" * 40
        fixture_set = records / "fixtures.json"
        fixture_set.write_text('{"frozen":"v3"}\n', encoding="utf-8")
        for gate_id in GATE_IDS:
            artifact = records / f"{gate_id}.log"
            artifact.write_text(f"verified evidence for {gate_id}\n", encoding="utf-8")
            commands = records / f"{gate_id}-commands.txt"
            commands.write_text(f"commands for {gate_id}\n", encoding="utf-8")
            record = {
                "schema_version": 1,
                "gate_id": gate_id,
                "candidate_sha": candidate,
                "command_set_sha256": hashlib.sha256(commands.read_bytes()).hexdigest(),
                "fixture_set_sha256": hashlib.sha256(fixture_set.read_bytes()).hexdigest(),
                "command_set_path": commands.name,
                "fixture_set_path": fixture_set.name,
                "environment": {"runner": "isolated-test", "os": "Linux", "python": "3.12"},
                "dependencies": {"python": "3.12"},
                "result": "passed",
                "failure_class": None,
                "evidence": [{"path": artifact.name, "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}],
            }
            if gate_id == "R266":
                record["platform_results"] = []
                for platform in REQUIRED_PLATFORMS:
                    platform_artifact = records / f"{platform}.log"
                    platform_artifact.write_text(f"verified evidence for {platform}\n", encoding="utf-8")
                    record["evidence"].append({
                        "path": platform_artifact.name,
                        "sha256": hashlib.sha256(platform_artifact.read_bytes()).hexdigest(),
                    })
                    record["platform_results"].append({
                        "platform": platform, "unit": "passed", "integration": "passed",
                        "acceptance": "passed", "soak_seconds": 1800,
                        "evidence_paths": [platform_artifact.name],
                    })
            if mutate:
                mutate(gate_id, record, artifact)
            record_path = records / f"{gate_id}.json"
            record_path.write_text(json.dumps(record), encoding="utf-8")
            record_path.with_suffix(".json.asc").write_text("test-signature", encoding="utf-8")
        return records, candidate

    def test_complete_signed_bundle_produces_candidate_bound_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records, candidate = self.create_bundle(root)
            manifest = validate_evidence_bundle(records, candidate, verify_signature=lambda *_: True)
        self.assertEqual(manifest["candidate_sha"], candidate)
        self.assertEqual(set(manifest["gates"]), set(GATE_IDS))
        self.assertEqual(manifest["result"], "passed")
        self.assertEqual(manifest["gates"]["R250"]["signature_sha256"],
                         hashlib.sha256(b"test-signature").hexdigest())

    def test_missing_gate_blocks_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records, candidate = self.create_bundle(root)
            (records / "R250.json").unlink()
            with self.assertRaisesRegex(ValueError, "missing gate record"):
                validate_evidence_bundle(records, candidate, verify_signature=lambda *_: True)
            manifest = root / "manifest.json"
            with patch("benchmarks.v3_release_gates._current_candidate", return_value=candidate), \
                    redirect_stderr(io.StringIO()):
                status = main(["--repo", str(root), "--evidence-dir", str(records),
                               "--manifest", str(manifest)])
            self.assertEqual(status, 1)
            self.assertFalse(manifest.exists())
            self.assertFalse(manifest.with_name("manifest.json.asc").exists())

    def test_main_rejects_evidence_and_manifest_paths_inside_candidate_checkout(self):
        for evidence_inside in (True, False):
            with self.subTest(evidence_inside=evidence_inside), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repo = root / "repo"
                repo.mkdir()
                records, candidate = self.create_bundle(repo if evidence_inside else root)
                evidence_dir = records
                manifest = (root / "manifest.json") if evidence_inside else (repo / "manifest.json")
                with patch("benchmarks.v3_release_gates._current_candidate", return_value=candidate), \
                        patch("benchmarks.v3_release_gates._gpg_verify", return_value=True), \
                        redirect_stderr(io.StringIO()):
                    status = main(["--repo", str(repo), "--evidence-dir", str(evidence_dir),
                                   "--manifest", str(manifest)])
                self.assertEqual(status, 1)
                self.assertFalse(manifest.exists())
                self.assertFalse(manifest.with_name("manifest.json.asc").exists())

    def test_failed_manifest_publish_leaves_no_unsigned_manifest(self):
        from types import SimpleNamespace
        import os
        from benchmarks.v3_release_gates import _write_signed_manifest

        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory).resolve() / "manifest.json"
            signature = manifest.with_name("manifest.json.asc")
            real_replace = os.replace

            def fail_signature_replace(source, destination):
                if Path(destination) == signature:
                    raise OSError("signature destination is locked")
                return real_replace(source, destination)

            def create_signature(command, **_kwargs):
                Path(command[command.index("--output") + 1]).write_text("signature", encoding="utf-8")
                return SimpleNamespace(returncode=0)

            with patch("benchmarks.v3_release_gates.subprocess.run", side_effect=create_signature), \
                    patch("benchmarks.v3_release_gates._gpg_verify", return_value=True), \
                    patch("benchmarks.v3_release_gates.os.replace", side_effect=fail_signature_replace):
                with self.assertRaisesRegex(OSError, "signature destination is locked"):
                    _write_signed_manifest({"result": "passed"}, manifest)
            self.assertFalse(manifest.exists())
            self.assertFalse(signature.exists())

    def test_manifest_writer_preserves_existing_output_pair(self):
        from benchmarks.v3_release_gates import _write_signed_manifest

        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            signature = manifest.with_name("manifest.json.asc")
            manifest.write_text("old manifest", encoding="utf-8")
            signature.write_text("old signature", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "already exists"):
                _write_signed_manifest({"result": "passed"}, manifest)
            self.assertEqual(manifest.read_text(encoding="utf-8"), "old manifest")
            self.assertEqual(signature.read_text(encoding="utf-8"), "old signature")

    def test_rejects_stale_candidate_failed_gate_or_bad_signature(self):
        for mutate, expected, verifier in (
            (lambda gate, record, _: record.update(candidate_sha="b" * 40), "candidate SHA", lambda *_: True),
            (lambda gate, record, _: record.update(result="failed", failure_class="test_failure"), "did not pass", lambda *_: True),
            (lambda *_: None, "signature verification failed", lambda *_: False),
        ):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                records, candidate = self.create_bundle(Path(directory), mutate=mutate)
                with self.assertRaisesRegex(ValueError, expected):
                    validate_evidence_bundle(records, candidate, verify_signature=verifier)

    def test_rejects_changed_or_escaping_evidence_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records, candidate = self.create_bundle(root)
            (records / "R250.log").write_text("changed after signing", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "evidence hash mismatch"):
                validate_evidence_bundle(records, candidate, verify_signature=lambda *_: True)

    def test_rejects_absolute_artifact_paths_and_malformed_results(self):
        for mutate, expected in (
            (lambda gate, record, _: record["evidence"][0].update(path="/outside/artifact"), "relative path"),
            (lambda gate, record, _: record.update(result={"passed": True}), "invalid result"),
        ):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                records, candidate = self.create_bundle(Path(directory), mutate=mutate)
                with self.assertRaisesRegex(ValueError, expected):
                    validate_evidence_bundle(records, candidate, verify_signature=lambda *_: True)
        with tempfile.TemporaryDirectory() as directory:
            records, candidate = self.create_bundle(Path(directory))
            (records / "R250-commands.txt").write_text("altered command set", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "command-set hash mismatch"):
                validate_evidence_bundle(records, candidate, verify_signature=lambda *_: True)
        with tempfile.TemporaryDirectory() as directory:
            records, candidate = self.create_bundle(Path(directory), mutate=lambda gate, record, _: (
                record["evidence"][0].update(path="../outside") if gate == "R250" else None
            ))
            with self.assertRaisesRegex(ValueError, "escapes evidence directory"):
                validate_evidence_bundle(records, candidate, verify_signature=lambda *_: True)

    def test_production_gate_requires_all_six_platforms_and_thirty_minute_soaks(self):
        for mutation, expected in (
            (lambda record: record["platform_results"].pop(), "platform matrix"),
            (lambda record: record["platform_results"][0].update(soak_seconds=1799), "30-minute soak"),
            (lambda record: record["platform_results"][0].update(integration="failed"), "platform validation failed"),
            (lambda record: record["platform_results"][0].update(platform=[]), "platform matrix"),
        ):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                records, candidate = self.create_bundle(Path(directory), mutate=lambda gate, record, _: mutation(record)
                                                        if gate == "R266" else None)
                with self.assertRaisesRegex(ValueError, expected):
                    validate_evidence_bundle(records, candidate, verify_signature=lambda *_: True)

    def test_each_production_platform_requires_linked_hashed_evidence(self):
        for mutation in (
            lambda record: record["platform_results"][0].pop("evidence_paths"),
            lambda record: record["platform_results"][0].update(evidence_paths=["missing.log"]),
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                records, candidate = self.create_bundle(Path(directory), mutate=lambda gate, record, _: (
                    mutation(record) if gate == "R266" else None
                ))
                with self.assertRaisesRegex(ValueError, "platform evidence"):
                    validate_evidence_bundle(records, candidate, verify_signature=lambda *_: True)

    def test_requires_failure_class_for_nonpassing_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            records, candidate = self.create_bundle(Path(directory), mutate=lambda gate, record, _: (
                record.update(result="inconclusive", failure_class=None) if gate == "R250" else None
            ))
            with self.assertRaisesRegex(ValueError, "failure class"):
                validate_evidence_bundle(records, candidate, verify_signature=lambda *_: True)


if __name__ == "__main__":
    unittest.main()

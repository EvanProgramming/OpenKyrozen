import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stderr

from benchmarks.v3_release_gates import GATE_IDS, REQUIRED_PLATFORMS, main, validate_evidence_bundle

WAIVABLE_GATES = ("R259", "R262", "R263", "R264")
LIMITED_SCOPE = "openkyrozen-v3-preview-linux-macos-sandbox-required-v1"
SAFETY_TESTS = (
    "test_apply_patch_is_atomic_stale_safe_and_rejects_ambiguous_hunks",
    "test_edit_file_requires_matching_hash_and_unique_old_text",
    "test_linux_sandbox_allows_workspace_writes_and_denies_sibling_writes",
    "test_macos_sandbox_allows_workspace_writes_and_denies_sibling_writes",
    "test_required_unavailable_sandbox_blocks_command_before_execution",
    "test_installed_but_unusable_backend_blocks_when_sandbox_is_required",
    "test_define_tool_text_is_rejected_without_registration",
    "test_full_capability_profile_does_not_grant_dynamic_registration",
    "test_approval_decisions_are_scoped_versioned_history_not_authority",
    "test_failed_migration_rolls_back_schema_and_version",
    "test_incompatible_existing_table_rolls_back_migration",
)


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
            if gate_id == "R259":
                record["protocol_recovery_count"] = 0
            if gate_id == "R266":
                record["platform_results"] = []
                for platform in REQUIRED_PLATFORMS:
                    platform_artifact = records / f"{platform}.log"
                    platform_artifact.write_text(f"verified evidence for {platform}\n", encoding="utf-8")
                    record["evidence"].append({
                        "path": platform_artifact.name,
                        "sha256": hashlib.sha256(platform_artifact.read_bytes()).hexdigest(),
                    })
                    platform_result = {
                        "platform": platform, "unit": "passed", "integration": "passed",
                        "acceptance": "passed", "soak_seconds": 1800,
                        "tui_interactive_acceptance": "passed",
                        "evidence_paths": [platform_artifact.name],
                    }
                    if platform.startswith(("linux-", "macos-")):
                        unit_log = records / "platforms" / platform / "unit.log"
                        unit_log.parent.mkdir(parents=True, exist_ok=True)
                        passing_tests = [name for name in SAFETY_TESTS
                                         if not name.startswith(("test_linux_sandbox", "test_macos_sandbox"))]
                        if platform.startswith("linux-"):
                            passing_tests.append("test_linux_sandbox_allows_workspace_writes_and_denies_sibling_writes")
                        else:
                            passing_tests.append("test_macos_sandbox_allows_workspace_writes_and_denies_sibling_writes")
                        lines = []
                        for name in passing_tests:
                            lines.append(f"{name} (test_fixture.SafetyTests) ...")
                            if name.startswith(("test_failed_migration_", "test_incompatible_existing_table_")):
                                lines.extend((
                                    "  RuntimeWarning: SQLite migration did not complete; original state is unchanged.",
                                    "  self._initialise_once()",
                                    "ok",
                                ))
                            else:
                                lines[-1] += " ok"
                        unit_log.write_text("\n".join(lines) + "\nRan 10 tests in 1.0s\n\nOK\n", encoding="utf-8")
                        unit_ref = unit_log.relative_to(records).as_posix()
                        record["evidence"].append({
                            "path": unit_ref,
                            "sha256": hashlib.sha256(unit_log.read_bytes()).hexdigest(),
                        })
                        platform_result["evidence_paths"].append(unit_ref)
                    if platform.startswith("windows-"):
                        platform_result.update(unit="not_run", integration="not_run",
                                                supported_features="passed",
                                                tui_interactive_acceptance="not_run_posix_pty_unavailable")
                    record["platform_results"].append(platform_result)
            if mutate:
                mutate(gate_id, record, artifact)
            record_path = records / f"{gate_id}.json"
            record_path.write_text(json.dumps(record), encoding="utf-8")
            record_path.with_suffix(".json.asc").write_text("test-signature", encoding="utf-8")
        return records, candidate

    def create_inconclusive_bundle(self, root):
        def make_inconclusive(gate, record, _):
            if gate in WAIVABLE_GATES:
                record.update(result="inconclusive", failure_class="provider_upstream_error")
                if gate == "R259":
                    record["protocol_recovery_count"] = None

        records, candidate = self.create_bundle(root, mutate=make_inconclusive)
        waivers = records / "waivers"
        waivers.mkdir()
        for gate_id in WAIVABLE_GATES:
            refs = [f"{gate_id}.json", "R266.json"] + [
                f"platforms/{platform}/unit.log"
                for platform in ("linux-py312", "linux-py313", "macos-py312", "macos-py313")
            ]
            waiver = {
                "schema_version": 1,
                "gate_id": gate_id,
                "candidate_sha": candidate,
                "rationale": "Provider interruption prevented the matched cohort from completing.",
                "evidence": [{"path": path, "sha256": hashlib.sha256((records / path).read_bytes()).hexdigest()}
                             for path in refs],
                "approver_identity": "D3897E8FF704149D609ACF878C118C04263D2A4C",
                "release_scope": LIMITED_SCOPE,
                "limitations": ["Preview only; no production or general availability claim."],
                "review_condition": "Re-evaluate all waived gates before any production or general availability release.",
            }
            path = waivers / f"{gate_id}.json"
            path.write_text(json.dumps(waiver), encoding="utf-8")
            path.with_suffix(".json.asc").write_text(hashlib.sha256(path.read_bytes()).hexdigest(), encoding="utf-8")
        return records, candidate, waivers

    @staticmethod
    def verify_test_signatures(record, signature):
        if record.parent.name == "waivers":
            return signature.read_text(encoding="utf-8") == hashlib.sha256(record.read_bytes()).hexdigest()
        return signature.read_text(encoding="utf-8") == "test-signature"

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
            real_link = os.link

            def fail_manifest_link(source, destination):
                if Path(destination) == manifest:
                    raise OSError("manifest destination is locked")
                return real_link(source, destination)

            def create_signature(command, **_kwargs):
                Path(command[command.index("--output") + 1]).write_text("signature", encoding="utf-8")
                return SimpleNamespace(returncode=0)

            with patch("benchmarks.v3_release_gates.subprocess.run", side_effect=create_signature), \
                    patch("benchmarks.v3_release_gates._gpg_verify", return_value=True), \
                    patch("benchmarks.v3_release_gates.os.link", side_effect=fail_manifest_link):
                with self.assertRaisesRegex(OSError, "manifest destination is locked"):
                    _write_signed_manifest({"result": "passed"}, manifest)
            self.assertFalse(manifest.exists())
            self.assertFalse(signature.exists())

    def test_manifest_writer_does_not_replace_outputs_created_after_preflight(self):
        from types import SimpleNamespace
        from benchmarks.v3_release_gates import _write_signed_manifest

        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            signature = manifest.with_name("manifest.json.asc")

            def concurrent_publish(command, **_kwargs):
                Path(command[command.index("--output") + 1]).write_text("new signature", encoding="utf-8")
                manifest.write_text("other manifest", encoding="utf-8")
                signature.write_text("other signature", encoding="utf-8")
                return SimpleNamespace(returncode=0)

            with patch("benchmarks.v3_release_gates.subprocess.run", side_effect=concurrent_publish), \
                    patch("benchmarks.v3_release_gates._gpg_verify", return_value=True):
                with self.assertRaises(FileExistsError):
                    _write_signed_manifest({"result": "passed"}, manifest)
            self.assertEqual(manifest.read_text(encoding="utf-8"), "other manifest")
            self.assertEqual(signature.read_text(encoding="utf-8"), "other signature")

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
            (lambda gate, record, _: record.update(result="failed", failure_class="test_failure"), "failed evidence cannot be waived", lambda *_: True),
            (lambda *_: None, "signature verification failed", lambda *_: False),
        ):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                records, candidate = self.create_bundle(Path(directory), mutate=mutate)
                with self.assertRaisesRegex(ValueError, expected):
                    validate_evidence_bundle(records, candidate, verify_signature=verifier)

    def test_record_changed_after_signature_check_cannot_change_validated_result(self):
        with tempfile.TemporaryDirectory() as directory:
            records, candidate = self.create_bundle(Path(directory))
            record_path = records / "R250.json"
            passed_record = json.loads(record_path.read_text(encoding="utf-8"))
            failed_record = dict(passed_record, result="failed", failure_class="test_failure")
            record_path.write_text(json.dumps(failed_record), encoding="utf-8")

            def replace_after_verification(record, _signature):
                signed_record = json.loads(record.read_text(encoding="utf-8"))
                if signed_record["gate_id"] == "R250":
                    self.assertEqual(signed_record["result"], "failed")
                    record_path.write_text(json.dumps(passed_record), encoding="utf-8")
                return True

            with self.assertRaisesRegex(ValueError, "failed evidence cannot be waived"):
                validate_evidence_bundle(records, candidate, verify_signature=replace_after_verification)

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

    def test_production_gate_allows_windows_supported_feature_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            records, candidate = self.create_bundle(
                Path(directory),
                mutate=lambda gate, record, _: [
                    item.update(unit="not_run", integration="not_run", supported_features="passed")
                    for item in record["platform_results"] if item["platform"].startswith("windows-")
                ] if gate == "R266" else None,
            )
            manifest = validate_evidence_bundle(records, candidate, verify_signature=lambda *_: True)
        windows = [item for item in manifest["gates"]["R266"]["platform_results"]
                   if item["platform"].startswith("windows-")]
        self.assertTrue(all(item["unit"] == "not_run"
                            and item["integration"] == "not_run"
                            and item["supported_features"] == "passed"
                            and item["tui_interactive_acceptance"] == "not_run_posix_pty_unavailable"
                            for item in windows))

    def test_production_gate_requires_explicit_windows_tui_unavailable_status(self):
        for mutation in (
            lambda item: item.pop("tui_interactive_acceptance"),
            lambda item: item.update(tui_interactive_acceptance="failed"),
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                records, candidate = self.create_bundle(Path(directory), mutate=lambda gate, record, _: [
                    mutation(item) for item in record["platform_results"]
                    if item["platform"].startswith("windows-")
                ] if gate == "R266" else None)
                with self.assertRaisesRegex(ValueError, "platform validation failed"):
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

    def test_valid_waivers_create_only_a_limited_preview_and_keep_original_status(self):
        with tempfile.TemporaryDirectory() as directory:
            records, candidate, waivers = self.create_inconclusive_bundle(Path(directory))
            manifest = validate_evidence_bundle(
                records, candidate, verify_signature=self.verify_test_signatures,
                limited_evidence=True, release_scope=LIMITED_SCOPE,
            )
        self.assertEqual(manifest["result"], "limited-evidence")
        self.assertEqual(manifest["release_classification"], "limited-evidence-preview")
        self.assertEqual(manifest["release_scope"], LIMITED_SCOPE)
        self.assertEqual(manifest["waived_gates"], list(WAIVABLE_GATES))
        for gate_id in WAIVABLE_GATES:
            self.assertEqual(manifest["gates"][gate_id]["evidence_result"], "inconclusive")
            self.assertEqual(manifest["gates"][gate_id]["status"], "waived")
        self.assertEqual(manifest["gates"]["R250"]["status"], "passed")

    def test_full_release_stays_blocked_even_with_valid_waivers(self):
        with tempfile.TemporaryDirectory() as directory:
            records, candidate, _ = self.create_inconclusive_bundle(Path(directory))
            with self.assertRaisesRegex(ValueError, "gate did not pass"):
                validate_evidence_bundle(records, candidate, verify_signature=self.verify_test_signatures)

    def test_limited_preview_rejects_any_scope_other_than_the_fixed_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            records, candidate, _ = self.create_inconclusive_bundle(Path(directory))
            with self.assertRaisesRegex(ValueError, "scope must be"):
                validate_evidence_bundle(
                    records, candidate, verify_signature=self.verify_test_signatures,
                    limited_evidence=True, release_scope="openkyrozen-v3-preview-windows",
                )

    def test_tampered_stale_and_wrong_scope_waivers_are_rejected(self):
        for change, expected in (
            (lambda waiver: waiver.write_text("tampered", encoding="utf-8"), "signature verification failed"),
            (lambda waiver: self._rewrite_waiver(waiver, candidate_sha="b" * 40), "candidate SHA"),
            (lambda waiver: self._rewrite_waiver(waiver, release_scope="another-preview"), "release scope"),
        ):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                records, candidate, waivers = self.create_inconclusive_bundle(Path(directory))
                change(waivers / "R259.json")
                with self.assertRaisesRegex(ValueError, expected):
                    validate_evidence_bundle(
                        records, candidate, verify_signature=self.verify_test_signatures,
                        limited_evidence=True, release_scope=LIMITED_SCOPE,
                    )

    @staticmethod
    def _rewrite_waiver(path, **updates):
        waiver = json.loads(path.read_text(encoding="utf-8"))
        waiver.update(updates)
        path.write_text(json.dumps(waiver), encoding="utf-8")
        path.with_suffix(".json.asc").write_text(hashlib.sha256(path.read_bytes()).hexdigest(), encoding="utf-8")

    def test_safety_gate_failure_and_missing_safety_tests_cannot_be_waived(self):
        with tempfile.TemporaryDirectory() as directory:
            records, candidate, _ = self.create_inconclusive_bundle(Path(directory))
            record = json.loads((records / "R261.json").read_text(encoding="utf-8"))
            record.update(result="failed", failure_class="safety_control_failure")
            (records / "R261.json").write_text(json.dumps(record), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "failed evidence cannot be waived"):
                validate_evidence_bundle(
                    records, candidate, verify_signature=self.verify_test_signatures,
                    limited_evidence=True, release_scope=LIMITED_SCOPE,
                )

    def test_nonzero_protocol_recovery_count_cannot_be_waived_as_inconclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            records, candidate, waivers = self.create_inconclusive_bundle(Path(directory))
            record_path = records / "R259.json"
            record = json.loads(record_path.read_text(encoding="utf-8"))
            record["protocol_recovery_count"] = 1
            record_path.write_text(json.dumps(record), encoding="utf-8")
            waiver_path = waivers / "R259.json"
            waiver = json.loads(waiver_path.read_text(encoding="utf-8"))
            for ref in waiver["evidence"]:
                if ref["path"] == "R259.json":
                    ref["sha256"] = hashlib.sha256(record_path.read_bytes()).hexdigest()
            self._rewrite_waiver(waiver_path, **waiver)
            with self.assertRaisesRegex(ValueError, "nonzero protocol recovery count"):
                validate_evidence_bundle(
                    records, candidate, verify_signature=self.verify_test_signatures,
                    limited_evidence=True, release_scope=LIMITED_SCOPE,
                )

    def test_passing_protocol_recovery_gate_requires_explicit_zero_count(self):
        with tempfile.TemporaryDirectory() as directory:
            records, candidate = self.create_bundle(
                Path(directory), mutate=lambda gate, record, _: record.update(protocol_recovery_count=None)
                if gate == "R259" else None,
            )
            with self.assertRaisesRegex(ValueError, "passed evidence requires protocol_recovery_count == 0"):
                validate_evidence_bundle(records, candidate, verify_signature=lambda *_: True)

    def test_inconclusive_protocol_recovery_record_must_mark_count_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            records, candidate, waivers = self.create_inconclusive_bundle(Path(directory))
            record_path = records / "R259.json"
            record = json.loads(record_path.read_text(encoding="utf-8"))
            record.pop("protocol_recovery_count")
            record_path.write_text(json.dumps(record), encoding="utf-8")
            waiver_path = waivers / "R259.json"
            waiver = json.loads(waiver_path.read_text(encoding="utf-8"))
            for ref in waiver["evidence"]:
                if ref["path"] == "R259.json":
                    ref["sha256"] = hashlib.sha256(record_path.read_bytes()).hexdigest()
            self._rewrite_waiver(waiver_path, **waiver)
            with self.assertRaisesRegex(ValueError, "protocol_recovery_count is required"):
                validate_evidence_bundle(
                    records, candidate, verify_signature=self.verify_test_signatures,
                    limited_evidence=True, release_scope=LIMITED_SCOPE,
                )

    def test_any_preview_waiver_requires_the_complete_safety_suite(self):
        with tempfile.TemporaryDirectory() as directory:
            records, candidate, waivers = self.create_inconclusive_bundle(Path(directory))
            for gate_id in WAIVABLE_GATES[1:]:
                (waivers / f"{gate_id}.json").unlink()
                (waivers / f"{gate_id}.json.asc").unlink()
                record_path = records / f"{gate_id}.json"
                record = json.loads(record_path.read_text(encoding="utf-8"))
                record.update(result="passed", failure_class=None)
                record_path.write_text(json.dumps(record), encoding="utf-8")

            unit_log = records / "platforms/linux-py312/unit.log"
            passing_tests = [name for name in SAFETY_TESTS
                             if name != "test_linux_sandbox_allows_workspace_writes_and_denies_sibling_writes"]
            unit_log.write_text("".join(f"{name} (test_fixture.SafetyTests) ... ok\n"
                                         for name in passing_tests)
                               + "Ran 10 tests in 1.0s\n\nOK\n", encoding="utf-8")
            r266_path = records / "R266.json"
            r266 = json.loads(r266_path.read_text(encoding="utf-8"))
            unit_ref = unit_log.relative_to(records).as_posix()
            for ref in r266["evidence"]:
                if ref["path"] == unit_ref:
                    ref["sha256"] = hashlib.sha256(unit_log.read_bytes()).hexdigest()
            r266_path.write_text(json.dumps(r266), encoding="utf-8")

            waiver_path = waivers / "R259.json"
            waiver = json.loads(waiver_path.read_text(encoding="utf-8"))
            for ref in waiver["evidence"]:
                if ref["path"] == unit_ref:
                    ref["sha256"] = hashlib.sha256(unit_log.read_bytes()).hexdigest()
                elif ref["path"] == "R266.json":
                    ref["sha256"] = hashlib.sha256(r266_path.read_bytes()).hexdigest()
            self._rewrite_waiver(waiver_path, **waiver)

            with self.assertRaisesRegex(ValueError, "safety test evidence is missing or did not pass"):
                validate_evidence_bundle(
                    records, candidate, verify_signature=self.verify_test_signatures,
                    limited_evidence=True, release_scope=LIMITED_SCOPE,
                )
        with tempfile.TemporaryDirectory() as directory:
            records, candidate, waivers = self.create_inconclusive_bundle(Path(directory))
            unit_log = records / "platforms/linux-py312/unit.log"
            unit_log.write_text("Ran 1 test in 1.0s\n\nOK\n", encoding="utf-8")
            record_path = records / "R266.json"
            r266 = json.loads(record_path.read_text(encoding="utf-8"))
            unit_ref = unit_log.relative_to(records).as_posix()
            for ref in r266["evidence"]:
                if ref["path"] == unit_ref:
                    ref["sha256"] = hashlib.sha256(unit_log.read_bytes()).hexdigest()
            record_path.write_text(json.dumps(r266), encoding="utf-8")
            for gate_id in WAIVABLE_GATES:
                waiver = waivers / f"{gate_id}.json"
                waiver_data = json.loads(waiver.read_text(encoding="utf-8"))
                for ref in waiver_data["evidence"]:
                    if ref["path"] == unit_ref:
                        ref["sha256"] = hashlib.sha256(unit_log.read_bytes()).hexdigest()
                    elif ref["path"] == "R266.json":
                        ref["sha256"] = hashlib.sha256(record_path.read_bytes()).hexdigest()
                self._rewrite_waiver(waiver, **waiver_data)
            with self.assertRaisesRegex(ValueError, "safety test evidence"):
                validate_evidence_bundle(
                    records, candidate, verify_signature=self.verify_test_signatures,
                    limited_evidence=True, release_scope=LIMITED_SCOPE,
                )


if __name__ == "__main__":
    unittest.main()

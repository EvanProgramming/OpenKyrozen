import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from benchmarks.v3_release_platforms import REQUIRED_PLATFORMS, collect_platform_evidence
from benchmarks.v3_release_gates import _check_record


class V3ReleasePlatformsTests(unittest.TestCase):
    def _matrix(self, root, *, mutate=None):
        results = root / "results"
        results.mkdir()
        candidate = "a" * 40
        for platform in REQUIRED_PLATFORMS:
            directory = results / f"v3-platform-{platform}"
            directory.mkdir()
            for name in ("unit.log", "acceptance.log", "soak.json"):
                (directory / name).write_text(f"{platform}:{name}\n", encoding="utf-8")
            record = {
                "schema_version": 1,
                "candidate_sha": candidate,
                "platform": platform,
                "unit": "passed",
                "integration": "passed",
                "acceptance": "passed",
                "soak_seconds": 1800,
                "environment": {"runner": "github", "os": platform.split("-")[0],
                                "python": f"3.{platform[-2:]}.0"},
                "dependencies": {"python": f"3.{platform[-2:]}.0"},
                "evidence_paths": ["unit.log", "acceptance.log", "soak.json"],
            }
            if mutate:
                mutate(platform, record)
            (directory / "platform.json").write_text(json.dumps(record), encoding="utf-8")
        commands = root / "commands.yml"
        commands.write_text("commands: test, acceptance, soak\n", encoding="utf-8")
        return results, commands, candidate

    def test_collects_exact_candidate_matrix_into_unsigned_r266_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results, commands, candidate = self._matrix(root)
            output = root / "evidence"
            record = collect_platform_evidence(results, output, candidate, commands)
            self.assertEqual(record["gate_id"], "R266")
            self.assertEqual(record["candidate_sha"], candidate)
            self.assertEqual({row["platform"] for row in record["platform_results"]},
                             set(REQUIRED_PLATFORMS))
            self.assertEqual(len(record["evidence"]), 24)
            self.assertEqual(record["command_set_sha256"], hashlib.sha256(
                (output / record["command_set_path"]).read_bytes()).hexdigest())
            self.assertEqual(record["fixture_set_sha256"], hashlib.sha256(
                (output / record["fixture_set_path"]).read_bytes()).hexdigest())
            self.assertTrue((output / "R266.json").is_file())
            self.assertFalse((output / "R266.json.asc").exists())
            self.assertEqual(len(_check_record(record, "R266", candidate, output.resolve())), 24)

    def test_rejects_incomplete_or_stale_platform_results(self):
        for mutate, expected in (
            (lambda platform, record: record.update(candidate_sha="b" * 40), "candidate SHA"),
            (lambda platform, record: record.update(soak_seconds=1799), "30-minute soak"),
            (lambda platform, record: record.update(unit="failed"), "platform validation"),
            (lambda platform, record: record["environment"].update(os="Windows"), "OS or Python"),
        ):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                results, commands, candidate = self._matrix(root, mutate=mutate)
                with self.assertRaisesRegex(ValueError, expected):
                    collect_platform_evidence(results, root / "evidence", candidate, commands)

    def test_rejects_missing_artifact_and_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results, commands, candidate = self._matrix(root)
            (results / f"v3-platform-{REQUIRED_PLATFORMS[0]}" / "unit.log").unlink()
            with self.assertRaisesRegex(ValueError, "artifact"):
                collect_platform_evidence(results, root / "evidence", candidate, commands)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results, commands, candidate = self._matrix(root)
            output = root / "evidence"
            output.mkdir()
            with self.assertRaisesRegex(ValueError, "already exists"):
                collect_platform_evidence(results, output, candidate, commands)

    def test_rejects_artifact_paths_that_escape_the_platform_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results, commands, candidate = self._matrix(root)
            result_path = results / f"v3-platform-{REQUIRED_PLATFORMS[0]}" / "platform.json"
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result["evidence_paths"][0] = "../../outside.log"
            result_path.write_text(json.dumps(result), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "escapes"):
                collect_platform_evidence(results, root / "evidence", candidate, commands)

    def test_rejects_symlinked_platform_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results, commands, candidate = self._matrix(root)
            platform_dir = results / f"v3-platform-{REQUIRED_PLATFORMS[0]}"
            metadata = platform_dir / "platform.json"
            external = root / "external.json"
            external.write_text(metadata.read_text(encoding="utf-8"), encoding="utf-8")
            metadata.unlink()
            try:
                metadata.symlink_to(external)
            except OSError as exc:
                self.skipTest(f"platform does not permit symlink creation: {exc}")
            with self.assertRaisesRegex(ValueError, "cannot be a symlink"):
                collect_platform_evidence(results, root / "evidence", candidate, commands)


if __name__ == "__main__":
    unittest.main()

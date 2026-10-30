"""Exercise preview signatures using an isolated, disposable GPG key."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from benchmarks import v3_release_gates as gates
from tests import test_v3_release_gates as release_fixtures


@contextmanager
def temporary_keyring():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        keyring = root / "gnupg"
        keyring.mkdir(mode=0o700)
        env = os.environ.copy()
        env["GNUPGHOME"] = str(keyring)
        try:
            yield root, keyring, env
        finally:
            if shutil.which("gpgconf"):
                subprocess.run(["gpgconf", "--homedir", str(keyring), "--kill", "gpg-agent"],
                               env=env, check=False, capture_output=True)


@unittest.skipUnless(shutil.which("gpg"), "GPG is unavailable")
class PreviewGPGTests(unittest.TestCase):
    def test_real_signed_bundle_and_manifest_reject_a_tampered_waiver(self):
        with temporary_keyring() as (root, keyring, env):
            command = ["gpg", "--batch", "--pinentry-mode", "loopback", "--passphrase", ""]
            subprocess.run(command + ["--quick-generate-key", "Preview Test <preview@example.invalid>",
                                      "rsa2048", "sign", "0"], env=env, check=True, capture_output=True)
            listing = subprocess.run(["gpg", "--batch", "--with-colons", "--list-secret-keys"],
                                     env=env, check=True, capture_output=True, text=True).stdout
            fingerprint = next(line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:"))
            records, candidate, waivers = release_fixtures.V3ReleaseGateTests().create_inconclusive_bundle(root)
            for path in waivers.glob("*.json"):
                waiver = json.loads(path.read_text())
                waiver["approver_identity"] = fingerprint
                path.write_text(json.dumps(waiver))
            for path in list(records.glob("R*.json")) + list(waivers.glob("*.json")):
                subprocess.run(command + ["--yes", "--local-user", fingerprint, "--armor", "--detach-sign",
                                          "--output", str(path.with_suffix(".json.asc")), str(path)],
                               env=env, check=True, capture_output=True)
            with patch.dict(os.environ, {"GNUPGHOME": str(keyring)}), \
                 patch.object(gates, "SIGNER_FINGERPRINT", fingerprint):
                manifest = gates.validate_evidence_bundle(
                    records, candidate, verify_signature=gates._gpg_verify,
                    limited_evidence=True, release_scope=gates.LIMITED_RELEASE_SCOPE,
                )
                path = root / "preview.json"
                path.write_text(json.dumps(manifest))
                subprocess.run(command + ["--yes", "--local-user", fingerprint, "--armor", "--detach-sign",
                                          "--output", str(path.with_suffix(".json.asc")), str(path)],
                               env=env, check=True, capture_output=True)
                self.assertEqual(gates.verify_signed_preview_manifest(
                    records, candidate, path, verify_signature=gates._gpg_verify)["result"], "limited-evidence")
                with (waivers / "R259.json").open("a") as stream:
                    stream.write(" ")
                with self.assertRaisesRegex(ValueError, "waiver signature"):
                    gates.verify_signed_preview_manifest(records, candidate, path, verify_signature=gates._gpg_verify)


if __name__ == "__main__":
    unittest.main()

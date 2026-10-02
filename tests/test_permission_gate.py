import unittest

from openkyrozen.security.permission_gate import requires_ask_approval, risk_category


class PermissionGateTests(unittest.TestCase):
    def test_ordinary_workspace_operations_skip_jev_but_sensitive_and_opaque_actions_do_not(self):
        self.assertIsNone(risk_category("write_file", "src/main.py|small edit"))
        self.assertIsNone(risk_category("run_cmd", "make test"))
        self.assertIsNone(risk_category("run_cmd", "git status && go test ./..."))
        self.assertEqual(risk_category("read_file", ".env"), "private_data_access")
        self.assertEqual(risk_category("search_memory", "recent notes"), "private_data_access")
        self.assertEqual(risk_category("git_push", "origin main"), "external_or_irreversible_change")
        self.assertEqual(risk_category("run_cmd", "python script.py"), "opaque_or_high_impact_command")
        self.assertEqual(risk_category("run_cmd", "echo safe; rm -rf /tmp/data"), "opaque_or_high_impact_command")

    def test_ask_mode_approves_mutations_and_sensitive_reads_but_not_inspection(self):
        self.assertFalse(requires_ask_approval("read_file", "src/main.py"))
        self.assertTrue(requires_ask_approval("write_file", "src/main.py|change"))
        self.assertTrue(requires_ask_approval("run_cmd", "make test"))
        self.assertTrue(requires_ask_approval("read_file", "~/.ssh/id_ed25519"))
        self.assertTrue(requires_ask_approval("browser_click", "submit"))

    def test_shell_classifier_rejects_dangerous_flags_on_allowlisted_commands(self):
        for command in (
            "find . -delete",
            "find . -exec rm {} ;",
            "rg --pre=cat needle .",
            "git diff --output=tmp.patch",
            "go test ./... -exec=sh",
            "cat README.md & rm important.txt",
        ):
            with self.subTest(command=command):
                self.assertEqual(
                    risk_category("run_cmd", command),
                    "opaque_or_high_impact_command",
                )

    def test_shell_classifier_checks_the_complete_command(self):
        long_private_read = "cat " + (" " * 4100) + "; cat ~/.ssh/id_ed25519"
        self.assertEqual(
            risk_category("run_cmd", long_private_read),
            "private_data_access",
        )
        long_executable_option = "rg needle " + (" " * 4100) + "--pre=cat"
        self.assertEqual(
            risk_category("run_cmd", long_executable_option),
            "opaque_or_high_impact_command",
        )


if __name__ == "__main__":
    unittest.main()

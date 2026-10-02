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


if __name__ == "__main__":
    unittest.main()

import unittest

from openkyrozen.security.permission_gate import git_read_arguments_allowed, requires_ask_approval, risk_category


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

    def test_read_only_git_tools_reject_output_and_external_execution_options(self):
        for action, args in (
            ("git_diff", "--output=patch.txt"), ("git_diff", "--ext-diff"),
            ("git_log", "--output patch.txt"), ("git_log", "--textconv"),
            ("git_show", "--output=patch.txt"), ("git_show", "--exec=helper"),
        ):
            with self.subTest(action=action, args=args):
                self.assertFalse(git_read_arguments_allowed(action, args))
        for args in ('"--output=patch.txt"', "'--ext-diff'", '"--textconv"', '"--exec=helper"'):
            with self.subTest(args=args):
                self.assertFalse(git_read_arguments_allowed("git_log", args))
        self.assertFalse(git_read_arguments_allowed("git_log", '"unterminated'))
        self.assertTrue(git_read_arguments_allowed("git_diff", "--cached HEAD~1"))
        self.assertTrue(git_read_arguments_allowed("git_log", "--oneline -5"))

    def test_read_only_git_executors_disable_external_diff_and_textconv(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from openkyrozen.tools.git import git_diff, git_log

        tools = SimpleNamespace(_git_working_directory=lambda: "/tmp/workspace")
        with patch("openkyrozen.tools.git.subprocess.run") as run:
            run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            git_diff(tools, "")
            git_log(tools, "")
        commands = [call.args[0] for call in run.call_args_list]
        for command in commands:
            self.assertIn("--no-textconv", command)
            self.assertIn("--no-ext-diff", command)


if __name__ == "__main__":
    unittest.main()

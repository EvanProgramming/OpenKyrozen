import json
import tempfile
import unittest
from openkyrozen.tools.models import ToolResult
from openkyrozen.providers.models import ToolCall
from types import SimpleNamespace
from openkyrozen.agent.executor import (
    _file_mutation_matches_objective,
    _legacy_object_arguments,
    _strategic_plan_denial,
)
from pathlib import Path
from unittest.mock import patch

from openkyrozen.app.bootstrap import build_application

from openkyrozen.persistence.store import EventStore
from openkyrozen.agent.modes import (
    InteractionController,
    InteractionError,
    is_plan_acceptance,
    mode_capabilities,
    parse_control_block,
    render_plan,
    route_mode,
    split_inline_command,
    validate_plan_proposal,
)
from openkyrozen.tasks.engine import TaskManager
from openkyrozen.security.capabilities import issue_capability_token
from openkyrozen.workspace.context import resolve_launch_context


class StrategicPlanExecutionBoundsTests(unittest.TestCase):
    def test_basename_only_objective_does_not_authorize_nested_sibling(self):
        from openkyrozen.agent.executor import _file_mutation_matches_objective
        objective = {"title": "Write index.html", "description": "Create index.html."}
        self.assertTrue(_file_mutation_matches_objective(objective, {"path": "index.html"}))
        self.assertFalse(_file_mutation_matches_objective(objective, {"path": "private/index.html"}))

    def test_json_string_arguments_are_checked_against_accepted_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = {"bounds": {"workspace_paths": ["src"], "capabilities": ["read", "write"],
                               "effects": ["read_only", "filesystem"], "risk_ceiling": "normal"}}
            read = SimpleNamespace(capability="read", side_effects="read_only", risk="normal")
            write = SimpleNamespace(capability="write", side_effects="filesystem", risk="normal")
            search_denial = _strategic_plan_denial(
                plan, "search_files", '{"directory":"outside"}', read, Path(directory))
            self.assertIsNotNone(search_denial)
            self.assertIn("outside", _strategic_plan_denial(
                plan, "edit_file", '{"path":"outside/file.py","old_text":"x","new_text":"y"}',
                write, Path(directory)))

    def test_non_string_risk_ceiling_is_a_plan_validation_error(self):
        proposal = {
            "title": "Change", "summary": "Make the change.", "steps": [{
                "title": "Update", "description": "Update safely.", "acceptance": ["Tests pass"],
            }],
            "bounds": {"workspace_paths": ["."], "capabilities": ["read"],
                       "effects": ["read_only"], "risk_ceiling": []},
        }
        with self.assertRaises(InteractionError):
            validate_plan_proposal(proposal)

    def test_full_path_objectives_do_not_match_a_sibling_with_the_same_basename(self):
        from openkyrozen.agent.executor import _file_mutation_matches_objective
        objective = {"title": "Write src/public/index.html", "description": "Update the public page."}
        self.assertFalse(_file_mutation_matches_objective(objective, {"path": "src/private/index.html"}))
        self.assertTrue(_file_mutation_matches_objective(objective, {"path": "src/public/index.html"}))
        scoped_directory = {"title": "Create index.html in src/public", "description": "",
                            "acceptance": [{"args": '{"path":"src/public/index.html"}'}]}
        self.assertTrue(_file_mutation_matches_objective(scoped_directory, {"path": "src/public/index.html"}))
        self.assertFalse(_file_mutation_matches_objective(scoped_directory, {"path": "src/private/index.html"}))

    def test_pathless_read_defaults_are_checked_against_accepted_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = {"bounds": {"workspace_paths": ["src"], "capabilities": ["read"],
                               "effects": ["read_only"], "risk_ceiling": "normal"}}
            read = SimpleNamespace(capability="read", side_effects="read", risk="normal")
            for action, args in (("list_dir", {}), ("list_tree", ""), ("search_files", {"query": "token"})):
                with self.subTest(action=action):
                    self.assertIn("outside", _strategic_plan_denial(
                        plan, action, args, read, Path(directory),
                    ))

    def test_explicit_file_path_does_not_authorize_descendants(self):
        objective = {"title": "Write src/public/index.html", "description": "Update the page."}
        self.assertFalse(_file_mutation_matches_objective(
            objective, {"path": "src/public/index.html/private.txt"},
        ))

    def test_legacy_mutation_arguments_are_normalized_for_scope_checks(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            plan = {"bounds": {"workspace_paths": ["site"], "capabilities": ["write"],
                               "effects": ["filesystem"], "risk_ceiling": "normal"}}
            spec = SimpleNamespace(capability="write", side_effects="filesystem", risk="normal",
                                   legacy_fields=("path", "content"),
                                   legacy_arguments=lambda value: "|".join((value["path"], value["content"])))
            self.assertEqual(_legacy_object_arguments("site/index.html|ok", spec),
                             {"path": "site/index.html", "content": "ok"})
            normalized = json.dumps(_legacy_object_arguments("site/index.html|ok", spec),
                                    ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            manager = TaskManager(EventStore(Path(directory) / "match.sqlite3"), workspace_id="w", session_id="s")
            self.assertGreater(manager._receipt_match_score({"acceptance": [{
                "action": "write_file", "args": normalized,
            }]}, "write_file", normalized), 0)
            self.assertIsNone(_strategic_plan_denial(
                plan, "write_file", "site/index.html|ok", spec, Path(directory)))

    def test_pipe_content_cannot_fall_back_to_a_sibling_objective_match(self):
        objective = {"title": "Write src/public/index.html", "description": "Update the public page."}
        spec = SimpleNamespace(legacy_fields=("path", "content"),
                               legacy_arguments=lambda value: "|".join((value["path"], value["content"])))
        args = "src/private/index.html|content|with-pipes"
        structured = _legacy_object_arguments(args, spec)
        from openkyrozen.agent.executor import _file_mutation_matches_objective
        self.assertEqual(structured, {"path": "src/private/index.html", "content": "content|with-pipes"})
        self.assertFalse(_file_mutation_matches_objective(objective, structured))

    def test_find_and_git_path_arrays_are_checked_against_plan_bounds(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = {"bounds": {"workspace_paths": ["src"], "capabilities": ["read", "git"],
                               "effects": ["read_only", "filesystem"], "risk_ceiling": "normal"}}
            find = SimpleNamespace(capability="read", side_effects="read", risk="normal")
            add = SimpleNamespace(capability="git", side_effects="mutation", risk="normal")
            self.assertIn("outside", _strategic_plan_denial(
                plan, "find_files", "*.py|tests", find, root))
            self.assertIn("outside", _strategic_plan_denial(
                plan, "git_add", {"paths": ["src/a.py", "outside/b.py"]}, add, root))

    def test_tool_effect_capability_risk_and_workspace_must_fit_accepted_bounds(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = {"bounds": {"workspace_paths": ["src"], "capabilities": ["read", "write"],
                               "effects": ["read_only", "filesystem"], "risk_ceiling": "normal"}}
            write = SimpleNamespace(capability="write", side_effects="filesystem", risk="normal")
            self.assertIsNone(_strategic_plan_denial(plan, "write_file", {"path": "src/app.py"}, write, root))
            self.assertIn("outside", _strategic_plan_denial(
                plan, "write_file", {"path": "README.md"}, write, root))
            shell = SimpleNamespace(capability="shell", side_effects="process", risk="normal")
            self.assertIn("capability", _strategic_plan_denial(
                plan, "run_cmd", {"command": "make test"}, shell, root))
            high = SimpleNamespace(capability="write", side_effects="filesystem", risk="high")
            self.assertIn("risk", _strategic_plan_denial(
                plan, "write_file", {"path": "src/app.py"}, high, root))

    def test_legacy_bounds_fail_closed_for_unapproved_effects(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            plan = {"bounds": {"workspace_paths": ["."], "capabilities": ["read"],
                               "effects": ["read_only"], "risk_ceiling": "normal"}}
            write = SimpleNamespace(capability="write", side_effects="filesystem", risk="normal")
            self.assertIn("capability", _strategic_plan_denial(
                plan, "write_file", {"path": "file.txt"}, write, Path(directory)))

    def test_contract_action_capability_effect_and_path_must_fit_bounds(self):
        proposal = {"title": "Build page", "summary": "Write and verify.", "steps": [{
            "id": "page", "title": "Build", "description": "Write src/page.html.",
            "execution": {"action": "write_file", "arguments": {"path": "src/page.html", "content": "ok"}},
            "acceptance": [{"criterion": "Page is correct", "verification": {
                "action": "read_file", "arguments": {"path": "src/page.html"}, "expect": {"contains": "ok"},
            }}],
        }]}
        proposal["bounds"] = {"workspace_paths": ["src"], "capabilities": ["write"],
                              "effects": ["filesystem"], "risk_ceiling": "normal"}
        with self.assertRaisesRegex(InteractionError, "capability or effect"):
            validate_plan_proposal(proposal)
        proposal["bounds"] = {"workspace_paths": ["src"], "capabilities": ["read", "write"],
                              "effects": ["read_only", "filesystem"], "risk_ceiling": "normal"}
        proposal["steps"][0]["acceptance"][0]["verification"]["arguments"]["path"] = "outside/page.html"
        with self.assertRaisesRegex(InteractionError, "outside its approved workspace"):
            validate_plan_proposal(proposal)

    def test_workspace_scope_resolves_symlinks_before_authorizing(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            (root / "src").symlink_to(Path(outside), target_is_directory=True)
            plan = {"bounds": {"workspace_paths": ["src"], "capabilities": ["write"],
                               "effects": ["filesystem"], "risk_ceiling": "normal"}}
            write = SimpleNamespace(capability="write", side_effects="filesystem", risk="normal")
            self.assertIn("outside", _strategic_plan_denial(
                plan, "write_file", {"path": "src/payload.txt"}, write, root))

    def test_opaque_shell_process_is_denied_under_path_scoped_plan(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            plan = {"bounds": {"workspace_paths": ["src"], "capabilities": ["shell"],
                               "effects": ["process"], "risk_ceiling": "normal"}}
            shell = SimpleNamespace(capability="shell", side_effects="process", risk="normal")
            self.assertIn("cannot be confined", _strategic_plan_denial(
                plan, "run_cmd", {"command": "rm -rf /tmp/unrelated"}, shell, Path(directory)))

    def test_pending_material_revision_does_not_block_subagent_cancellation(self):
        from openkyrozen.agent.executor import _awaiting_revision_mutation
        state = {"pending_plan": {"version": 2}, "executing_plan": {"version": 1}}
        runtime = SimpleNamespace(_is_state_changing_action=lambda *_args: True)
        self.assertFalse(_awaiting_revision_mutation(state, "cancel_subagent", "run-1", None, runtime))
        self.assertTrue(_awaiting_revision_mutation(state, "write_file", "index.html|x", None, runtime))


class StrategicPlanContractTests(unittest.TestCase):
    def test_read_file_acceptance_requires_an_expected_content_predicate(self):
        with self.assertRaises(InteractionError):
            validate_plan_proposal({
                "title": "Change", "summary": "Make the change.",
                "steps": [{"id": "inspect", "title": "Inspect", "description": "Read the file.",
                           "acceptance": [{"criterion": "File contains READY", "verification": {
                               "action": "read_file", "arguments": {"path": "src/file.txt"},
                           }}]}],
            })

    def test_revalidating_a_normalized_plan_preserves_verified_acceptance_contracts(self):
        proposal = {
            "title": "Change", "summary": "Make the change.",
            "bounds": {"workspace_paths": ["src"], "capabilities": ["read", "write"],
                       "effects": ["read_only", "filesystem"], "risk_ceiling": "normal"},
            "steps": [{"id": "inspect", "title": "Inspect", "description": "Read the file.",
                       "execution": {"action": "write_file", "arguments": {"path": "src/file.txt", "content": "x"}},
                       "acceptance": [{"criterion": "File is readable", "verification": {
                           "action": "read_file", "arguments": {"path": "src/file.txt"},
                           "expect": {"contains": "x"},
                       }}]}],
        }
        first = validate_plan_proposal(proposal)
        second = validate_plan_proposal(first)
        self.assertEqual(second["steps"][0]["acceptance_verifications"],
                         first["steps"][0]["acceptance_verifications"])
        self.assertEqual(second["steps"][0]["execution"], first["steps"][0]["execution"])
        self.assertEqual(second["steps"][0]["acceptance_ids"], first["steps"][0]["acceptance_ids"])

    def test_strategic_plan_has_stable_objective_criteria_and_approved_bounds(self):
        proposal = {
            "title": "Change", "summary": "Make the requested change.", "assumptions": [],
            "bounds": {
                "workspace_paths": ["src", "docs"], "capabilities": ["read", "write"],
                "effects": ["read_only", "filesystem"], "risk_ceiling": "normal",
            },
            "steps": [{"id": "implement", "title": "Implement", "description": "Update the feature.",
                       "acceptance": ["Behavior is correct", "Tests pass"]}],
        }

        first = validate_plan_proposal(proposal)
        repeated = validate_plan_proposal(proposal)
        revised = validate_plan_proposal({**proposal, "summary": "Deliver the requested change."}, previous=first)
        changed_operation = validate_plan_proposal({**proposal, "steps": [{**proposal["steps"][0],
            "execution": {"action": "write_file", "arguments": {"path": "src/file.txt", "content": "different"}},
        }]}, previous=first)

        self.assertEqual(first["steps"][0]["id"], "implement")
        self.assertEqual(first["steps"][0]["acceptance_ids"], repeated["steps"][0]["acceptance_ids"])
        self.assertEqual(first["steps"][0]["acceptance_ids"], revised["steps"][0]["acceptance_ids"])
        self.assertNotEqual(first["fingerprint"], changed_operation["fingerprint"])
        self.assertEqual(first["bounds"]["workspace_paths"], ["docs", "src"])
        self.assertEqual(first["bounds"]["risk_ceiling"], "normal")
        self.assertEqual(len(first["fingerprint"]), 64)
        self.assertNotEqual(first["fingerprint"], revised["fingerprint"])
        rendered = render_plan(first)
        self.assertIn("src", rendered)
        self.assertIn("filesystem", rendered)
        self.assertIn("Risk ceiling: normal", rendered)
        self.assertIn(first["steps"][0]["acceptance_ids"][0], rendered)

    def test_legacy_strategic_plan_gets_conservative_bounds_and_invalid_scope_is_rejected(self):
        legacy = validate_plan_proposal({
            "title": "Read only", "summary": "Inspect safely.", "steps": [{
                "id": "inspect", "title": "Inspect", "description": "Read the project.",
                "acceptance": ["Relevant files are identified"],
            }],
        })
        self.assertEqual(legacy["bounds"], {
            "workspace_paths": ["."], "capabilities": ["read"],
            "effects": ["read_only"], "risk_ceiling": "normal",
        })
        for bounds in (
            {"workspace_paths": ["../outside"], "capabilities": ["read"],
             "effects": ["read_only"], "risk_ceiling": "normal"},
            {"workspace_paths": ["."], "capabilities": ["full"],
             "effects": ["read_only"], "risk_ceiling": "normal"},
            {"workspace_paths": ["."], "capabilities": ["read"],
             "effects": ["unknown"], "risk_ceiling": "normal"},
            {"workspace_paths": ["."], "capabilities": ["read"],
             "effects": ["read_only"], "risk_ceiling": "unbounded"},
        ):
            with self.subTest(bounds=bounds), self.assertRaises(InteractionError):
                validate_plan_proposal({
                    "title": "Unsafe", "summary": "Reject invalid bounds.", "bounds": bounds,
                    "steps": [{"id": "inspect", "title": "Inspect", "description": "Read.",
                               "acceptance": ["Inspected"]}],
                })


class StrategicPlanAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = EventStore(Path(self.directory.name) / "state.sqlite3")
        self.controller = InteractionController(
            self.store, workspace_id="project", session_id="strategic-session",
        )
        self.manager = TaskManager(
            self.store, workspace_id="project", session_id="strategic-session",
        )

    def _proposal(self):
        return self.controller.propose_plan({
            "title": "Implement the page", "summary": "Create and verify one page.",
            "bounds": {
                "workspace_paths": ["site"], "capabilities": ["read", "write"],
                "effects": ["read_only", "filesystem"], "risk_ceiling": "normal",
            },
            "steps": [{"id": "page", "title": "Create page", "description": "Write the page.",
                       "acceptance": ["Page exists", "Page content is correct"]}],
        })

    def test_working_plan_edits_cannot_add_or_redefine_unbound_strategic_work(self):
        plan = self._proposal()
        accepted, _ = self.controller.accept_plan(
            self.manager, plan_id=plan["plan_id"], version=plan["version"],
        )
        task_id = accepted["working_task_ids"][0]
        with self.assertRaisesRegex(ValueError, "(?i)strategic"):
            self.manager.edit_plan(
                plan_id=self.manager.working_plan.id, expected_revision=self.manager.working_plan.revision,
                operations=[{"op": "add", "ref": "unbound", "task": {
                    "description": "Write src/other.py", "acceptance": ["Other file is written"],
                }}],
            )
        with self.assertRaisesRegex(ValueError, "(?i)strategic"):
            self.manager.edit_plan(
                plan_id=self.manager.working_plan.id, expected_revision=self.manager.working_plan.revision,
                operations=[{"op": "update", "task": task_id, "fields": {
                    "description": "Replace the objective with another file write",
                }}],
            )
        result = self.manager.edit_plan(
            plan_id=self.manager.working_plan.id, expected_revision=self.manager.working_plan.revision,
            operations=[{"op": "update", "task": task_id, "fields": {"priority": 1}}],
        )
        binding = next(task for task in result["tasks"] if task["id"] == task_id)["checkpoint"]["strategic_plan"]
        self.assertEqual(binding["fingerprint"], accepted["fingerprint"])

    def test_accepting_material_revision_replaces_executing_plan_atomically(self):
        first = self._proposal()
        accepted, _ = self.controller.accept_plan(
            self.manager, plan_id=first["plan_id"], version=first["version"],
        )
        old_task_id = accepted["working_task_ids"][0]
        revision = self.controller.propose_plan({
            "title": "Revised page", "summary": "Update the accepted page objective.",
            "bounds": first["bounds"],
            "steps": [{"id": "page", "title": "Create revised page", "description": "Write the revised page.",
                       "acceptance": ["Page exists", "Page content is correct"]}],
        })
        updated, created = self.controller.accept_plan(
            self.manager, plan_id=revision["plan_id"], version=revision["version"],
        )
        self.assertTrue(created)
        self.assertEqual(updated["version"], 2)
        self.assertEqual(self.controller.state()["executing_plan"]["version"], 2)
        self.assertIsNone(self.controller.state()["pending_plan"])
        self.assertEqual(self.manager.working_plan.id, accepted["working_plan_id"])
        self.assertEqual(next(task for task in self.manager.tasks if task["id"] == old_task_id)["status"], "cancelled")
        self.assertEqual(len(updated["working_task_ids"]), 1)
        self.assertNotEqual(updated["working_task_ids"][0], old_task_id)

    def test_revised_plan_completion_ignores_cancelled_tasks_from_previous_versions(self):
        first = self._proposal()
        self.controller.accept_plan(self.manager, plan_id=first["plan_id"], version=first["version"])
        revised = self.controller.propose_plan({
            "title": "Revised page", "summary": "Update the accepted page objective.",
            "bounds": first["bounds"],
            "steps": [{"id": "page", "title": "Create revised page", "description": "Write the revised page.",
                       "acceptance": first["steps"][0]["acceptance"]}],
        })
        accepted, _ = self.controller.accept_plan(
            self.manager, plan_id=revised["plan_id"], version=revised["version"],
        )
        task = next(task for task in self.manager.tasks if task["id"] == accepted["working_task_ids"][0])
        evidence = {"success": True, "acceptance": "verified",
                    "acceptance_ids": task["checkpoint"]["strategic_plan"]["acceptance_ids"]}
        self.assertTrue(self.manager.set_status(self.manager.tasks.index(task), "succeeded", evidence=evidence))
        self.controller.complete_plan(accepted, task_manager=self.manager)
        self.assertIsNone(self.controller.state()["executing_plan"])

    def test_stale_plan_completion_cannot_clear_a_newer_accepted_version(self):
        first = self._proposal()
        old, _ = self.controller.accept_plan(self.manager, plan_id=first["plan_id"], version=first["version"])
        revision = self.controller.propose_plan({
            "title": "Revised page", "summary": "Change the page objective.",
            "bounds": first["bounds"],
            "steps": [{"id": "page", "title": "Create revised page", "description": "Write revised content.",
                       "acceptance": ["Revised page exists"]}],
        })
        current, _ = self.controller.accept_plan(
            self.manager, plan_id=revision["plan_id"], version=revision["version"],
        )
        with self.assertRaisesRegex(InteractionError, "changed"):
            self.controller.complete_plan(old, status="cancelled", task_manager=self.manager)
        with self.assertRaisesRegex(ValueError, "changed"):
            self.store.append_plan_completed({
                "plan_id": old["plan_id"], "version": old["version"],
                "fingerprint": old["fingerprint"], "status": "cancelled",
            }, user_id="local", workspace_id="project", session_id="strategic-session")
        self.assertEqual(self.controller.state()["executing_plan"]["fingerprint"], current["fingerprint"])

    def test_restored_pending_revision_blocks_auto_acceptance(self):
        current = self._proposal()
        pending = {**current, "version": current["version"] + 1, "fingerprint": "new-fingerprint"}
        self.store.append_event("history.restored", {"interaction": {
            "executing_plan": current, "pending_plan": pending,
        }}, workspace_id="project", session_id="strategic-session")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.store.append_plan_auto_accepted(
                {**pending, "revision_materiality": "within_bounds"}, expected_previous=current,
                user_id="local", workspace_id="project", session_id="strategic-session",
            )
    def test_cancelled_current_objective_blocks_strategic_completion(self):
        plan = self.controller.propose_plan({
            "title": "Build two pages", "summary": "Build and verify both pages.",
            "bounds": {"workspace_paths": ["site"], "capabilities": ["read", "write"],
                       "effects": ["read_only", "filesystem"], "risk_ceiling": "normal"},
            "steps": [{"id": name, "title": f"Build {name}", "description": f"Write site/{name}.html.",
                       "acceptance": [{"criterion": f"{name} can be read", "verification": {
                           "action": "read_file", "arguments": {"path": f"site/{name}.html"},
                           "expect": {"contains": name},
                       }}]} for name in ("first", "second")],
        })
        accepted, _ = self.controller.accept_plan(self.manager, plan_id=plan["plan_id"], version=plan["version"])
        first, second = accepted["working_task_ids"]
        first_task = next(task for task in self.manager.tasks if task["id"] == first)
        self.assertTrue(self.manager.set_status(self.manager.tasks.index(first_task), "cancelled"))
        second_task = next(task for task in self.manager.tasks if task["id"] == second)
        evidence = {"success": True, "acceptance": "verified",
                    "acceptance_ids": second_task["checkpoint"]["strategic_plan"]["acceptance_ids"]}
        self.assertTrue(self.manager.set_status(self.manager.tasks.index(second_task), "succeeded", evidence=evidence))
        with self.assertRaisesRegex(InteractionError, "acceptance evidence gates"):
            self.controller.complete_plan(accepted, task_manager=self.manager)

    def test_delayed_prior_version_completion_does_not_clear_current_plan(self):
        first = self._proposal()
        old, _ = self.controller.accept_plan(self.manager, plan_id=first["plan_id"], version=first["version"])
        revised = self.controller.propose_plan({
            "title": "Revised page", "summary": "Update the accepted page objective.",
            "bounds": first["bounds"],
            "steps": [{"id": "page", "title": "Create revised page", "description": "Write the revised page.",
                       "acceptance": first["steps"][0]["acceptance"]}],
        })
        current, _ = self.controller.accept_plan(self.manager, plan_id=revised["plan_id"], version=revised["version"])
        self.store.append_event("interaction.plan_completed", {
            "plan_id": old["plan_id"], "version": old["version"],
            "fingerprint": old["fingerprint"], "status": "completed",
        }, workspace_id="project", session_id="strategic-session")
        state = self.controller.state()["executing_plan"]
        self.assertEqual(state["version"], current["version"])
        self.assertEqual(state["fingerprint"], current["fingerprint"])

    def test_verifier_receipt_matches_separately_from_execution_checkpoint(self):
        index = self.manager.add_task("Write then verify", checkpoint={
            "action": "write_file", "args": '{"path":"site/page.html","content":"ok"}',
        }, acceptance=[{"action": "read_file", "args": '{"path":"site/page.html"}'}])
        task = self.manager.tasks[index]
        self.assertGreater(self.manager._receipt_match_score(
            task, "read_file", '{"path":"site/page.html"}',
        ), 0)

    def test_task_completion_requires_every_strategic_acceptance_id(self):
        index = self.manager.add_task("Verify both criteria", acceptance=["both checks pass"])
        task = self.manager.tasks[index]
        task["checkpoint"]["strategic_plan"] = {"acceptance_ids": ["criterion-a", "criterion-b"]}
        task["evidence"] = [{"success": True, "acceptance": "first", "acceptance_ids": ["criterion-a"]}]
        self.manager.request_completion(index)
        self.manager.reconcile_completions({task["id"]})
        self.assertEqual(task["status"], "pending")

    def test_active_unrelated_working_plan_is_preserved_when_strategic_acceptance_is_rejected(self):
        self.manager.update_plan(complexity="complex", tasks=[{
            "description": "Existing work", "acceptance": ["Existing work is done"],
        }])
        existing_id = self.manager.tasks[0]["id"]
        plan = self._proposal()
        with self.assertRaises(InteractionError):
            self.controller.accept_plan(self.manager, plan_id=plan["plan_id"], version=plan["version"])
        self.assertEqual(self.manager.tasks[0]["id"], existing_id)
        self.assertEqual(self.manager.tasks[0]["description"], "Existing work")
        self.assertEqual(self.store.list_events(
            "interaction.plan_accepted", workspace_id="project", session_id="strategic-session",
        ), [])

    def test_restart_reconciles_completion_after_working_plan_terminalization(self):
        plan = self._proposal()
        accepted, _ = self.controller.accept_plan(
            self.manager, plan_id=plan["plan_id"], version=plan["version"],
        )
        task = self.manager.tasks[0]
        evidence = {"success": True, "acceptance": "verified",
                    "acceptance_ids": task["checkpoint"]["strategic_plan"]["acceptance_ids"]}
        self.assertTrue(self.manager.set_status(0, "succeeded", evidence=evidence))
        self.assertEqual(self.manager.working_plan.status, "completed")

        restored_manager = TaskManager(self.store, workspace_id="project", session_id="strategic-session")
        restored_manager.recover()
        restored_controller = InteractionController(
            self.store, workspace_id="project", session_id="strategic-session",
        )
        restored_controller.reconcile_plan_completion(restored_manager)
        self.assertIsNone(restored_controller.state()["executing_plan"])
        self.assertEqual(self.store.list_events(
            "interaction.plan_completed", workspace_id="project", session_id="strategic-session",
        )[-1]["payload"]["status"], "completed")

    def test_legacy_receipt_matching_preserves_strategic_task_binding(self):
        plan = self.controller.propose_plan({
            "title": "Read the page", "summary": "Read and verify the page.",
            "steps": [{"id": "read", "title": "Read", "description": "Read src/page.txt.",
                       "acceptance": [{"criterion": "Page is readable", "verification": {
                           "action": "read_file", "arguments": {"path": "src/page.txt"},
                           "expect": {"contains": "contents"},
                       }}]}],
        })
        self.controller.accept_plan(self.manager, plan_id=plan["plan_id"], version=plan["version"])
        task = self.manager.tasks[0]
        result = self.manager.record_evidence(
            action="read_file", args='{"path":"src/page.txt"}', result="contents", success=True,
            verification_status="verified",
        )
        self.assertEqual(result["task_id"], task["id"])
        self.assertEqual(task["checkpoint"]["strategic_plan"]["plan_id"], plan["plan_id"])

    def test_duplicate_acceptance_racing_with_commit_returns_existing_plan(self):
        plan = self._proposal()
        winner = InteractionController(self.store, workspace_id="project", session_id="strategic-session")
        winner_manager = TaskManager(self.store, workspace_id="project", session_id="strategic-session")
        loser_manager = TaskManager(self.store, workspace_id="project", session_id="strategic-session")
        original_edit = loser_manager.edit_plan

        def race(**kwargs):
            winner.accept_plan(winner_manager, plan_id=plan["plan_id"], version=plan["version"])
            return original_edit(**kwargs)

        with patch.object(loser_manager, "edit_plan", side_effect=race):
            duplicate, created = self.controller.accept_plan(
                loser_manager, plan_id=plan["plan_id"], version=plan["version"],
            )
        self.assertFalse(created)
        self.assertEqual(duplicate["working_plan_id"], winner_manager.working_plan.id)
        self.assertEqual(len(self.store.list_events(
            "interaction.plan_accepted", workspace_id="project", session_id="strategic-session",
        )), 1)

    def test_acceptance_racing_with_newer_revision_rejects_stale_result(self):
        plan = self._proposal()
        winner = InteractionController(self.store, workspace_id="project", session_id="strategic-session")
        winner_manager = TaskManager(self.store, workspace_id="project", session_id="strategic-session")
        loser_manager = TaskManager(self.store, workspace_id="project", session_id="strategic-session")
        original_edit = loser_manager.edit_plan

        def race(**kwargs):
            winner.accept_plan(winner_manager, plan_id=plan["plan_id"], version=plan["version"])
            winner.propose_plan({
                "title": "Revised page", "summary": "Write the revised page.",
                "bounds": plan["bounds"],
                "steps": [{"id": "page", "title": "Create revised page", "description": "Write it.",
                           "acceptance": ["Revised page exists"]}],
            })
            return original_edit(**kwargs)

        with patch.object(loser_manager, "edit_plan", side_effect=race):
            with self.assertRaisesRegex(InteractionError, "changed|stale"):
                self.controller.accept_plan(loser_manager, plan_id=plan["plan_id"], version=plan["version"])
        self.assertEqual(self.controller.state()["pending_plan"]["version"], 2)

    def test_interaction_state_keeps_accepted_bounds_after_generic_event_window(self):
        plan = self._proposal()
        self.controller.accept_plan(self.manager, plan_id=plan["plan_id"], version=plan["version"])
        now = "2026-10-17T00:00:00Z"
        with self.store.database.connection() as db:
            db.executemany(
                "INSERT INTO events(id,event_type,payload,user_id,workspace_id,session_id,task_id,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                [(f"noise-{index}", "unrelated.event", "{}", "local", "project",
                  "strategic-session", None, now) for index in range(10_001)],
            )
        restored = InteractionController(self.store, workspace_id="project", session_id="strategic-session")
        self.assertEqual(restored.state()["executing_plan"]["fingerprint"], plan["fingerprint"])

    def test_failed_plan_keeps_its_accepted_execution_bounds(self):
        plan = self._proposal()
        accepted, _ = self.controller.accept_plan(
            self.manager, plan_id=plan["plan_id"], version=plan["version"],
        )
        self.controller.complete_plan(accepted, status="failed", task_manager=self.manager)
        self.assertEqual(self.controller.state()["executing_plan"]["fingerprint"], accepted["fingerprint"])

    def test_failed_plan_can_complete_after_resumed_work_passes_evidence(self):
        plan = self._proposal()
        accepted, _ = self.controller.accept_plan(
            self.manager, plan_id=plan["plan_id"], version=plan["version"],
        )
        self.controller.complete_plan(accepted, status="failed", task_manager=self.manager)
        task = self.manager.tasks[0]
        task["status"] = "succeeded"
        task["evidence"] = [{"success": True, "acceptance": "verified",
                              "acceptance_ids": plan["steps"][0]["acceptance_ids"]}]
        self.controller.complete_plan(accepted, task_manager=self.manager)
        self.assertIsNone(self.controller.state()["executing_plan"])

    def test_strategic_working_plan_rejects_unbound_additions(self):
        plan = self._proposal()
        accepted, _ = self.controller.accept_plan(
            self.manager, plan_id=plan["plan_id"], version=plan["version"],
        )
        with self.assertRaisesRegex(ValueError, "approved strategic binding"):
            self.manager.edit_plan(complexity="complex", plan_id=self.manager.working_plan.id,
                                   expected_revision=self.manager.working_plan.revision,
                                   operations=[{"op": "add", "ref": "extra", "task": {
                                       "description": "Unbound extra work", "acceptance": ["Done"]}}])
        self.assertTrue(self.manager.set_status(0, "succeeded", evidence={
            "success": True, "acceptance": "verified",
            "acceptance_ids": plan["steps"][0]["acceptance_ids"],
        }))
        self.controller.complete_plan(accepted, task_manager=self.manager)
        completed = self.store.list_events(
            "interaction.plan_completed", workspace_id="project", session_id="strategic-session",
        )
        self.assertEqual(completed[-1]["payload"]["working_task_ids"], accepted["working_task_ids"])

    def test_material_revision_reuses_succeeded_objective_and_its_evidence(self):
        original = self.controller.propose_plan({
            "title": "Implement the page", "summary": "Create and verify one page.",
            "bounds": {"workspace_paths": ["site"], "capabilities": ["read", "write"],
                       "effects": ["read_only", "filesystem"], "risk_ceiling": "normal"},
            "steps": [{"id": "page", "title": "Create page", "description": "Write the page.",
                       "acceptance": ["Page exists", "Page content is correct"]},
                      {"id": "followup", "title": "Prepare follow-up", "description": "Prepare follow-up work.",
                       "acceptance": ["Follow-up is prepared"]}],
        })
        accepted, _ = self.controller.accept_plan(
            self.manager, plan_id=original["plan_id"], version=original["version"],
        )
        original_task = self.manager.tasks[0]
        self.assertTrue(self.manager.set_status(0, "succeeded", evidence={
            "success": True, "acceptance": "verified",
            "acceptance_ids": original["steps"][0]["acceptance_ids"],
        }))
        revision = self.controller.propose_plan({
            "title": original["title"], "summary": "Clarify the outcome without repeating completed work.",
            "bounds": original["bounds"], "steps": [original["steps"][0], {
                "id": "docs", "title": "Document the page", "description": "Document the completed page.",
                "acceptance": ["The page is documented"],
            }],
        })
        revised, created = self.controller.accept_plan(
            self.manager, plan_id=revision["plan_id"], version=revision["version"],
        )
        self.assertTrue(created)
        self.assertEqual(revised["working_task_ids"][0], original_task["id"])
        self.assertEqual(self.manager.tasks[0]["status"], "succeeded")
        self.assertEqual(self.manager.tasks[0]["evidence"][0]["acceptance_ids"], original["steps"][0]["acceptance_ids"])
        docs = next(index for index, task in enumerate(self.manager.tasks)
                    if task["id"] == revised["working_task_ids"][1])
        self.assertTrue(self.manager.set_status(docs, "succeeded", evidence={
            "success": True, "acceptance": "verified",
            "acceptance_ids": revision["steps"][1]["acceptance_ids"],
        }))
        self.controller.complete_plan(revised, task_manager=self.manager)
        self.assertIsNone(self.controller.state()["executing_plan"])

    def test_acceptance_atomically_links_working_plan_tasks_and_criteria(self):
        plan = self._proposal()

        accepted, created = self.controller.accept_plan(
            self.manager, plan_id=plan["plan_id"], version=plan["version"],
        )

        self.assertTrue(created)
        self.assertEqual(accepted["fingerprint"], plan["fingerprint"])
        self.assertIsNotNone(self.manager.working_plan)
        task = self.manager.tasks[0]
        binding = task["checkpoint"]["strategic_plan"]
        self.assertEqual(binding["plan_id"], plan["plan_id"])
        self.assertEqual(binding["version"], plan["version"])
        self.assertEqual(binding["objective_id"], "page")
        self.assertEqual(binding["acceptance_ids"], plan["steps"][0]["acceptance_ids"])
        event = next(event for event in self.store.list_events(
            "interaction.plan_accepted", workspace_id="project", session_id="strategic-session",
        ))
        self.assertEqual(event["payload"]["working_plan_id"], self.manager.working_plan.id)
        self.assertEqual(event["payload"]["fingerprint"], plan["fingerprint"])

    def test_acceptance_event_failure_rolls_back_plan_tasks_and_event(self):
        plan = self._proposal()
        json_encoder = self.store.database._json

        def fail_acceptance(payload):
            if isinstance(payload, dict) and payload.get("fingerprint") == plan["fingerprint"]:
                raise ValueError("injected acceptance event failure")
            return json_encoder(payload)

        with patch.object(self.store.database, "_json", side_effect=fail_acceptance):
            with self.assertRaisesRegex(ValueError, "injected"):
                self.controller.accept_plan(self.manager, plan_id=plan["plan_id"], version=plan["version"])

        self.assertEqual(self.manager.tasks, [])
        self.assertIsNone(self.manager.working_plan)
        self.assertIsNotNone(self.controller.state()["pending_plan"])
        self.assertIsNone(self.store.get_working_plan(
            user_id="local", workspace_id="project", session_id="strategic-session",
        ))
        self.assertEqual(self.store.list_events(
            "interaction.plan_accepted", workspace_id="project", session_id="strategic-session",
        ), [])

    def test_reaccepting_executing_plan_rejects_an_explicit_stale_version(self):
        plan = self._proposal()
        accepted, _ = self.controller.accept_plan(
            self.manager, plan_id=plan["plan_id"], version=plan["version"],
        )
        with self.assertRaises(InteractionError):
            self.controller.accept_plan(
                self.manager, plan_id=accepted["plan_id"], version=accepted["version"] - 1,
            )

    def test_revised_proposal_versions_from_the_accepted_strategic_plan(self):
        plan = self._proposal()
        accepted, _ = self.controller.accept_plan(
            self.manager, plan_id=plan["plan_id"], version=plan["version"],
        )
        revised = self.controller.propose_plan({
            "title": "Implement the page", "summary": "Also verify the page output.",
            "bounds": plan["bounds"], "steps": plan["steps"],
        })
        self.assertEqual(revised["plan_id"], accepted["plan_id"])
        self.assertEqual(revised["version"], accepted["version"] + 1)
        self.assertNotEqual(revised["fingerprint"], accepted["fingerprint"])

    def test_material_revision_is_staged_without_replacing_the_accepted_plan(self):
        accepted, _ = self.controller.accept_plan(
            self.manager, plan_id=self._proposal()["plan_id"], version=1,
        )
        proposal = self.controller.propose_plan({
            "title": "Expand the page", "summary": "Add a second page.",
            "bounds": {"workspace_paths": ["site", "docs"], "capabilities": ["read", "write"],
                       "effects": ["filesystem", "read_only"], "risk_ceiling": "normal"},
            "steps": [{"id": "docs", "title": "Add docs", "description": "Create a second page.",
                       "acceptance": ["The second page exists"]}],
        })
        state = self.controller.state()
        self.assertEqual(proposal["revision_materiality"], "material")
        self.assertEqual(state["pending_plan"]["fingerprint"], proposal["fingerprint"])
        self.assertEqual(state["executing_plan"]["fingerprint"], accepted["fingerprint"])
        self.assertEqual(self.manager.working_plan.revision, accepted["working_plan_revision"])

    def test_narrowing_bounds_with_the_same_objectives_is_automatically_accepted(self):
        proposal = self._proposal()
        accepted, _ = self.controller.accept_plan(
            self.manager, plan_id=proposal["plan_id"], version=proposal["version"],
        )
        narrowed = self.controller.propose_plan({
            "title": proposal["title"], "summary": proposal["summary"],
            "assumptions": proposal["assumptions"],
            "bounds": {"workspace_paths": ["site/docs"], "capabilities": ["read"],
                       "effects": ["read_only"], "risk_ceiling": "normal"},
            "steps": [{"id": step["id"], "title": step["title"], "description": step["description"],
                       "acceptance": step["acceptance"]} for step in proposal["steps"]],
        })
        state = self.controller.state()
        self.assertEqual(narrowed["revision_materiality"], "within_bounds")
        self.assertIsNone(state["pending_plan"])
        self.assertEqual(state["executing_plan"]["version"], 2)
        self.assertEqual(state["executing_plan"]["working_plan_id"], accepted["working_plan_id"])

    def test_execution_contract_changes_require_material_reapproval(self):
        proposal = self.controller.propose_plan({
            "title": "Write the page", "summary": "Create the approved file.",
            "bounds": {"workspace_paths": ["site"], "capabilities": ["write"],
                       "effects": ["filesystem"], "risk_ceiling": "normal"},
            "steps": [{"id": "page", "title": "Write page", "description": "Write the page.",
                       "execution": {"action": "write_file", "arguments": {
                           "path": "site/index.html", "content": "old",
                       }}, "acceptance": ["Page exists"]}],
        })
        self.controller.accept_plan(self.manager, plan_id=proposal["plan_id"], version=proposal["version"])
        changed = self.controller.propose_plan({
            "title": proposal["title"], "summary": proposal["summary"], "bounds": proposal["bounds"],
            "steps": [{**proposal["steps"][0], "execution": {"action": "write_file", "arguments": {
                "path": "site/index.html", "content": "new",
            }}}],
        })
        self.assertEqual(changed["revision_materiality"], "material")

    def test_auto_accepted_revisions_advance_the_version_before_material_approval(self):
        proposal = self._proposal()
        self.controller.accept_plan(self.manager, plan_id=proposal["plan_id"], version=proposal["version"])
        first = self.controller.propose_plan({
            "title": proposal["title"], "summary": proposal["summary"], "bounds": {
                **proposal["bounds"], "capabilities": ["read"],
            }, "steps": proposal["steps"],
        })
        second = self.controller.propose_plan({
            "title": proposal["title"], "summary": proposal["summary"], "bounds": {
                **proposal["bounds"], "workspace_paths": ["site/pages"], "capabilities": ["read"],
            }, "steps": proposal["steps"],
        })
        self.assertEqual(first["version"], 2)
        self.assertEqual(second["version"], 3)
        material = self.controller.propose_plan({
            "title": "Revised page", "summary": "A material objective change.",
            "bounds": {**proposal["bounds"], "workspace_paths": ["site/pages"],
                       "capabilities": ["read"]}, "steps": proposal["steps"],
        })
        self.assertEqual(material["version"], 4)
        accepted, _ = self.controller.accept_plan(
            self.manager, plan_id=material["plan_id"], version=material["version"],
        )
        self.assertEqual(accepted["version"], 4)

    def test_stale_auto_accepted_revision_cannot_replace_newer_narrower_bounds(self):
        initial = self._proposal()
        accepted, _ = self.controller.accept_plan(
            self.manager, plan_id=initial["plan_id"], version=initial["version"],
        )
        stale_snapshot = self.controller.state()
        first = InteractionController(self.store, workspace_id="project", session_id="strategic-session")
        second = InteractionController(self.store, workspace_id="project", session_id="strategic-session")
        first.state = lambda: stale_snapshot
        second.state = lambda: stale_snapshot
        accepted_narrow = first.propose_plan({
            "title": initial["title"], "summary": initial["summary"], "bounds": {
                **initial["bounds"], "workspace_paths": ["site/docs"],
            }, "steps": initial["steps"],
        })
        with self.assertRaisesRegex(InteractionError, "changed|stale"):
            second.propose_plan({
                "title": initial["title"], "summary": initial["summary"], "bounds": {
                    **initial["bounds"], "workspace_paths": ["site/private"],
                }, "steps": initial["steps"],
            })
        self.assertEqual(self.controller.state()["executing_plan"]["fingerprint"],
                         accepted_narrow["fingerprint"])

    def test_material_revision_uses_current_working_plan_revision_at_staging(self):
        original = self._proposal()
        accepted, _ = self.controller.accept_plan(
            self.manager, plan_id=original["plan_id"], version=original["version"],
        )
        self.manager.edit_plan(
            plan_id=accepted["working_plan_id"], expected_revision=accepted["working_plan_revision"],
            operations=[{"op": "update", "task": accepted["working_task_ids"][0],
                         "fields": {"priority": 2}}],
        )
        proposal = self.controller.propose_plan({
            "title": "Expand", "summary": "A material objective change.",
            "bounds": original["bounds"],
            "steps": [{"id": "next", "title": "New objective", "description": "Add a new objective.",
                       "acceptance": ["New objective is done"]}],
        })
        updated, _ = self.controller.accept_plan(
            self.manager, plan_id=proposal["plan_id"], version=proposal["version"],
        )
        self.assertEqual(updated["working_plan_revision"], accepted["working_plan_revision"] + 2)

    def test_changing_acceptance_verification_requires_reapproval(self):
        proposal = self.controller.propose_plan({
            "title": "Create the page", "summary": "Create and verify the page.",
            "bounds": {"workspace_paths": ["site"], "capabilities": ["read", "write"],
                       "effects": ["read_only", "filesystem"], "risk_ceiling": "normal"},
            "steps": [{"id": "page", "title": "Create page", "description": "Write site/index.html.",
                       "acceptance": [{"criterion": "Page content matches", "verification": {
                           "action": "write_file", "arguments": {"path": "site/index.html", "content": "safe"},
                       }}]}],
        })
        self.controller.accept_plan(self.manager, plan_id=proposal["plan_id"], version=proposal["version"])
        revised = self.controller.propose_plan({
            "title": proposal["title"], "summary": proposal["summary"], "bounds": proposal["bounds"],
            "steps": [{"id": "page", "title": "Create page", "description": "Write site/index.html.",
                       "acceptance": [{"criterion": "Page content matches", "verification": {
                           "action": "write_file", "arguments": {"path": "site/index.html", "content": "unsafe"},
                       }}]}],
        })
        self.assertEqual(revised["revision_materiality"], "material")
        self.assertEqual(self.controller.state()["pending_plan"]["fingerprint"], revised["fingerprint"])

    def test_material_reapproval_replaces_active_tasks_in_one_revision(self):
        original = self._proposal()
        accepted, _ = self.controller.accept_plan(
            self.manager, plan_id=original["plan_id"], version=original["version"],
        )
        old_task_id = accepted["working_task_ids"][0]
        proposal = self.controller.propose_plan({
            "title": "Expand the page", "summary": "Add a second page.",
            "bounds": {"workspace_paths": ["site", "docs"], "capabilities": ["read", "write"],
                       "effects": ["filesystem", "read_only"], "risk_ceiling": "normal"},
            "steps": [{"id": "docs", "title": "Add docs", "description": "Create a second page.",
                       "acceptance": ["The second page exists"]}],
        })
        revised, created = self.controller.accept_plan(
            self.manager, plan_id=proposal["plan_id"], version=proposal["version"],
        )
        self.assertTrue(created)
        self.assertEqual(self.manager.working_plan.revision, accepted["working_plan_revision"] + 1)
        tasks = {task["id"]: task for task in self.manager.tasks}
        self.assertEqual(tasks[old_task_id]["status"], "cancelled")
        new_task = tasks[revised["working_task_ids"][0]]
        self.assertEqual(new_task["status"], "pending")
        self.assertEqual(new_task["checkpoint"]["strategic_plan"]["version"], proposal["version"])
        self.assertIsNone(self.controller.state()["pending_plan"])
        self.assertEqual(self.controller.state()["executing_plan"]["version"], proposal["version"])
        accepted_events = self.store.list_events(
            "interaction.plan_accepted", workspace_id="project", session_id="strategic-session",
        )
        revised_acceptance = next(event for event in accepted_events
                                  if event["payload"].get("version") == proposal["version"])
        self.assertEqual(revised_acceptance["payload"]["revision_of"]["version"], accepted["version"])

    def test_failed_reapproval_commit_preserves_old_plan_and_tasks(self):
        original = self._proposal()
        accepted, _ = self.controller.accept_plan(
            self.manager, plan_id=original["plan_id"], version=original["version"],
        )
        original_tasks = [dict(task) for task in self.manager.tasks]
        proposal = self.controller.propose_plan({
            "title": "Expanded", "summary": "New objective.", "bounds": original["bounds"],
            "steps": [{"id": "expanded", "title": "Expanded", "description": "New work.",
                       "acceptance": ["Expanded work is verified"]}],
        })
        json_encoder = self.store.database._json

        def fail_revision(payload):
            if isinstance(payload, dict) and payload.get("version") == proposal["version"]:
                raise RuntimeError("simulated acceptance event failure")
            return json_encoder(payload)

        with patch.object(self.store.database, "_json", side_effect=fail_revision):
            with self.assertRaisesRegex(RuntimeError, "simulated"):
                self.controller.accept_plan(
                    self.manager, plan_id=proposal["plan_id"], version=proposal["version"],
                )
        self.assertEqual(self.manager.tasks, original_tasks)
        self.assertEqual(self.manager.working_plan.revision, accepted["working_plan_revision"])
        self.assertEqual(self.controller.state()["executing_plan"]["fingerprint"], accepted["fingerprint"])
        self.assertEqual(self.controller.state()["pending_plan"]["fingerprint"], proposal["fingerprint"])

    def test_stale_working_plan_revision_rejects_material_reapproval(self):
        original = self._proposal()
        accepted, _ = self.controller.accept_plan(
            self.manager, plan_id=original["plan_id"], version=original["version"],
        )
        proposal = self.controller.propose_plan({
            "title": "Expanded", "summary": "New objective.",
            "bounds": original["bounds"],
            "steps": [{"id": "expanded", "title": "Expanded", "description": "New work.",
                       "acceptance": ["Expanded work is verified"]}],
        })
        self.manager.edit_plan(
            plan_id=accepted["working_plan_id"], expected_revision=accepted["working_plan_revision"],
            operations=[{"op": "update", "task": accepted["working_task_ids"][0],
                         "fields": {"priority": 2}}],
        )
        with self.assertRaisesRegex(InteractionError, "revision changed"):
            self.controller.accept_plan(self.manager, plan_id=proposal["plan_id"], version=proposal["version"])
        state = self.controller.state()
        self.assertEqual(state["executing_plan"]["fingerprint"], accepted["fingerprint"])
        self.assertEqual(state["pending_plan"]["fingerprint"], proposal["fingerprint"])

    def test_running_working_task_must_stop_before_material_reapproval(self):
        original = self._proposal()
        accepted, _ = self.controller.accept_plan(
            self.manager, plan_id=original["plan_id"], version=original["version"],
        )
        proposal = self.controller.propose_plan({
            "title": "Expanded", "summary": "New objective.", "bounds": original["bounds"],
            "steps": [{"id": "expanded", "title": "Expanded", "description": "New work.",
                       "acceptance": ["Expanded work is verified"]}],
        })
        self.manager.set_status(0, "running")
        with self.assertRaisesRegex(InteractionError, "task is running"):
            self.controller.accept_plan(self.manager, plan_id=proposal["plan_id"], version=proposal["version"])
        self.assertEqual(self.controller.state()["executing_plan"]["fingerprint"], accepted["fingerprint"])
        self.assertEqual(self.manager.tasks[0]["status"], "running")

    def test_staging_and_cancelling_revision_preserves_accepted_plan_bounds(self):
        plan = self._proposal()
        accepted, _ = self.controller.accept_plan(
            self.manager, plan_id=plan["plan_id"], version=plan["version"],
        )
        revision = self.controller.propose_plan({
            "title": "Implement the page", "summary": "Revise the intended outcome.",
            "bounds": plan["bounds"], "steps": plan["steps"],
        })
        state = self.controller.state()
        self.assertEqual(state["executing_plan"]["fingerprint"], accepted["fingerprint"])
        self.assertEqual(state["pending_plan"]["fingerprint"], revision["fingerprint"])
        self.controller.cancel_plan(revision["plan_id"], revision["version"])
        self.assertEqual(self.controller.state()["executing_plan"]["fingerprint"], accepted["fingerprint"])

    def test_cancelled_material_proposal_does_not_block_a_later_safe_narrowing(self):
        proposal = self._proposal()
        accepted, _ = self.controller.accept_plan(
            self.manager, plan_id=proposal["plan_id"], version=proposal["version"],
        )
        material = self.controller.propose_plan({
            "title": "Expanded objective", "summary": "Add a new objective.",
            "bounds": proposal["bounds"],
            "steps": [{"id": "new", "title": "New work", "description": "Add new work.",
                       "acceptance": ["New work is done"]}],
        })
        self.controller.cancel_plan(material["plan_id"], material["version"])
        narrowed = self.controller.propose_plan({
            "title": proposal["title"], "summary": proposal["summary"],
            "bounds": {**proposal["bounds"], "capabilities": ["read"]},
            "steps": proposal["steps"],
        })
        self.assertEqual(narrowed["revision_materiality"], "within_bounds")
        self.assertEqual(self.controller.state()["executing_plan"]["fingerprint"], narrowed["fingerprint"])
        self.assertEqual(self.controller.state()["executing_plan"]["working_plan_id"], accepted["working_plan_id"])

    def test_cancelled_revision_numbers_are_not_reused(self):
        plan = self._proposal()
        accepted, _ = self.controller.accept_plan(
            self.manager, plan_id=plan["plan_id"], version=plan["version"],
        )
        revised = self.controller.propose_plan({
            "title": "First revision", "summary": "Stage revision one.",
            "bounds": plan["bounds"], "steps": plan["steps"],
        })
        self.controller.cancel_plan(revised["plan_id"], revised["version"])
        replacement = self.controller.propose_plan({
            "title": "Replacement revision", "summary": "Stage a distinct revision.",
            "bounds": plan["bounds"], "steps": plan["steps"],
        })
        self.assertEqual(replacement["plan_id"], accepted["plan_id"])
        self.assertEqual(replacement["version"], revised["version"] + 1)

    def test_acceptance_rejects_proposal_superseded_before_transaction(self):
        plan = self._proposal()
        edit_plan = self.manager.edit_plan

        def supersede_then_edit(**kwargs):
            self.controller.propose_plan({
                "title": "Implement the page", "summary": "A newer proposal.",
                "bounds": plan["bounds"], "steps": plan["steps"],
            })
            return edit_plan(**kwargs)

        with patch.object(self.manager, "edit_plan", side_effect=supersede_then_edit):
            with self.assertRaisesRegex(ValueError, "Strategic Plan proposal changed"):
                self.controller.accept_plan(
                    self.manager, plan_id=plan["plan_id"], version=plan["version"],
                )
        self.assertEqual(self.manager.tasks, [])
        self.assertIsNone(self.manager.working_plan)
        self.assertEqual(self.store.list_events(
            "interaction.plan_accepted", workspace_id="project", session_id="strategic-session",
        ), [])

    def test_completion_requires_evidence_for_every_approved_acceptance_id(self):
        plan = self._proposal()
        accepted, _ = self.controller.accept_plan(
            self.manager, plan_id=plan["plan_id"], version=plan["version"],
        )
        task = self.manager.tasks[0]
        task["status"] = "succeeded"
        task["evidence"] = [{"success": True, "acceptance": "one check", "acceptance_ids": [
            plan["steps"][0]["acceptance_ids"][0],
        ]}]
        with self.assertRaisesRegex(InteractionError, "criteria have not all been verified"):
            self.controller.complete_plan(accepted, task_manager=self.manager)

    def test_completion_records_success_after_every_criterion_has_successful_evidence(self):
        plan = self._proposal()
        accepted, _ = self.controller.accept_plan(
            self.manager, plan_id=plan["plan_id"], version=plan["version"],
        )
        task = self.manager.tasks[0]
        task["status"] = "succeeded"
        task["evidence"] = [{"success": True, "acceptance": "verified",
                              "acceptance_ids": plan["steps"][0]["acceptance_ids"]}]
        self.controller.complete_plan(accepted, task_manager=self.manager)
        event = self.store.list_events(
            "interaction.plan_completed", workspace_id="project", session_id="strategic-session",
        )
        self.assertEqual(event[0]["payload"]["status"], "completed")

    def test_real_receipts_cover_only_their_matching_strategic_criteria(self):
        proposal = self.controller.propose_plan({
            "title": "Verified page", "summary": "Create and inspect a page.",
            "bounds": {"workspace_paths": ["site"], "capabilities": ["read", "write"],
                       "effects": ["filesystem", "read_only"], "risk_ceiling": "normal"},
            "steps": [{"id": "page", "title": "Create page", "description": "Create and inspect the page.",
                       "acceptance": [
                           {"criterion": "Page exists", "verification": {
                               "action": "write_file", "arguments": {"path": "site/index.html", "content": "ok"}}},
                           {"criterion": "Page can be read back", "verification": {
                               "action": "read_file", "arguments": {"path": "site/index.html"},
                               "expect": {"contains": "ok"}}},
                       ]}],
        })
        accepted, _ = self.controller.accept_plan(
            self.manager, plan_id=proposal["plan_id"], version=proposal["version"],
        )
        task = self.manager.tasks[0]
        wrong = self.manager.record_evidence(task_id=task["id"], action="write_file",
                                             args='{"content":"wrong","path":"site/index.html"}',
                                             result="file written", success=True, verification_status="verified")
        self.assertNotIn("acceptance_ids", wrong)
        for verification_status in ("failed", "unavailable", None):
            unverified = self.manager.record_evidence(
                task_id=task["id"], action="write_file", args='{"content":"ok","path":"site/index.html"}',
                result="reported success", success=True, verification_status=verification_status,
            )
            self.assertNotIn("acceptance_ids", unverified)
        first = self.manager.record_evidence(task_id=task["id"], action="write_file",
                                             args='{"content":"ok","path":"site/index.html"}',
                                             result="file written", success=True, verification_status="verified")
        self.assertEqual(first["acceptance_ids"], [task["acceptance"][0]["id"]])
        self.assertNotIn(task["acceptance"][1]["id"], first["acceptance_ids"])
        wrong_read = self.manager.record_evidence(task_id=task["id"], action="read_file",
                                                  args='{"path":"site/index.html"}', result="wrong", success=True,
                                                  verification_status="not_applicable")
        self.assertNotIn("acceptance_ids", wrong_read)
        second = self.manager.record_evidence(task_id=task["id"], action="read_file",
                                              args='{"path":"site/index.html"}', result="token=<redacted>",
                                              verification_result="token=ok", success=True,
                                              verification_status="not_applicable")
        self.assertEqual(second["acceptance_ids"], [task["acceptance"][1]["id"]])
        self.manager.set_status(0, "succeeded")
        self.controller.complete_plan(accepted, task_manager=self.manager)

    def test_runtime_receipts_keep_multi_criterion_task_active_until_all_are_verified(self):
        from openkyrozen.agent.executor import _record_turn_receipt
        from openkyrozen.agent.types import ExecutionReceipt
        from types import SimpleNamespace
        import hashlib

        large_content = "x " * 100_000 + "UNIQUE-END"
        proposal = self.controller.propose_plan({
            "title": "Verified page", "summary": "Create and inspect a page.",
            "bounds": {"workspace_paths": ["site"], "capabilities": ["read", "write"],
                       "effects": ["filesystem", "read_only"], "risk_ceiling": "normal"},
            "steps": [{"id": "page", "title": "Create page", "description": "Create and inspect the page.",
                       "acceptance": [
                           {"criterion": "Page exists", "verification": {"action": "write_file",
                            "arguments": {"path": "site/index.html", "content": large_content}}},
                           {"criterion": "Page can be read back", "verification": {"action": "read_file",
                            "arguments": {"path": "site/index.html"}, "expect": {"contains": "UNIQUE-END"}}},
                       ]}],
        })
        self.controller.accept_plan(self.manager, plan_id=proposal["plan_id"], version=proposal["version"])
        task = self.manager.tasks[0]
        runtime = SimpleNamespace(tasks=self.manager, _emit_stream_event=lambda event: None)

        for index, (action, arguments) in enumerate((
            ("write_file", {"path": "site/index.html", "content": large_content}),
            ("read_file", {"path": "site/index.html"}),
        )):
            if action == "write_file":
                (Path(self.directory.name) / "site").mkdir(exist_ok=True)
                (Path(self.directory.name) / "site/index.html").write_text(large_content, encoding="utf-8")
            # Rebuild through the same evidence path used by runtime receipt handling.
            receipt = ExecutionReceipt(
                receipt_id=f"receipt-{index}", operation_id=f"operation-{index}", action=action,
                args=json.dumps(arguments, sort_keys=True, separators=(",", ":"))[:1000],
                args_sha256=hashlib.sha256(json.dumps(
                    arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode()).hexdigest(),
                authorized=True, started_at="start", completed_at="end", success=True,
                result=large_content[:200_000] if action == "read_file" else "ok",
                call_id=f"call-{index}",
                call_id_source="native", arguments=arguments,
                tool_result=ToolResult(text=large_content if action == "read_file" else "ok"),
                user_id="local", workspace_id="project", session_id="strategic-session",
                plan_id=self.manager.working_plan.id, task_id=task["id"], side_effects="filesystem",
                verification={"status": "verified", "method": "fixture", "observed": None},
            )
            _record_turn_receipt(runtime, receipt)
            if index == 0:
                self.assertIn(task["status"], {"pending", "running"})
                self.assertIsNotNone(self.manager.active_working_task(task["id"]))
        covered = {criterion_id for item in task["evidence"]
                   for criterion_id in item.get("acceptance_ids", [])}
        self.assertEqual(covered, {item["id"] for item in task["acceptance"]})
        self.assertEqual(self.manager.tasks[0]["status"], "succeeded")


class InteractionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = EventStore(Path(self.directory.name) / "state.sqlite3")
        from openkyrozen.memory.service import MemoryBank
        self.application = build_application(surface="cli", memory=MemoryBank(self.store.path, store=self.store))
        self.agent = self.application.runtime
        self.addCleanup(self.application.close)
        self.controller = InteractionController(
            self.store, workspace_id="workspace", session_id="session",
        )

    def test_auto_routing_and_mode_capabilities_are_narrowing_only(self):
        self.assertEqual(route_mode("auto", "What does this function do?"), "ask")
        self.assertEqual(route_mode("auto", "How do I create a Python file?"), "ask")
        self.assertEqual(route_mode("auto", "Implement the fix and run tests"), "plan")
        self.assertEqual(route_mode("auto", "Please review this repository"), "plan")
        self.assertEqual(route_mode("agent", "What is this?"), "agent")
        base = frozenset({"read", "write", "shell", "network", "git", "browser", "dynamic"})
        self.assertEqual(mode_capabilities(base, "plan"), frozenset({"read", "network"}))
        self.assertEqual(mode_capabilities(frozenset({"read"}), "agent"), frozenset({"read"}))

    def test_ponytail_is_inactive_for_informational_ask_turns(self):
        token = self.agent._active_interaction_mode.set("ask")
        try:
            self.assertEqual(self.agent._ponytail_context("coder"), "")
        finally:
            self.agent._active_interaction_mode.reset(token)

        token = self.agent._active_interaction_mode.set("plan")
        try:
            self.assertIn('ponytail level="full"', self.agent._ponytail_context("coder"))
        finally:
            self.agent._active_interaction_mode.reset(token)

    def test_inline_commands_are_trailing_and_conservative(self):
        self.assertEqual(
            split_inline_command("Build a todo list for me /mode plan"),
            ("Build a todo list for me", "/mode plan"),
        )
        self.assertEqual(
            split_inline_command("Review the workspace /plan"),
            ("Review the workspace", "/plan"),
        )
        self.assertIsNone(split_inline_command("Use /mode in the documentation"))
        self.assertIsNone(split_inline_command("Open https://example.com/a/mode"))
        self.assertIsNone(split_inline_command("Read /tmp/project"))
        self.assertIsNone(split_inline_command("Build it /mode invalid"))

    def test_cli_inline_command_dispatches_before_request(self):
        with patch.object(self.agent, "set_interaction_mode", return_value={"preference_mode": "plan"}) as set_mode, \
                patch.object(self.agent.console, "print"):
            self.assertTrue(self.agent._apply_inline_command("/mode plan"))
        set_mode.assert_called_once_with("plan")

    def test_project_interaction_state_is_isolated_from_other_projects(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as first, \
                tempfile.TemporaryDirectory() as second:
            first_context = resolve_launch_context(home=home, project_path=first)
            second_context = resolve_launch_context(home=home, project_path=second)
            first_scope = self.agent.interaction_workspace_id(first_context)
            second_scope = self.agent.interaction_workspace_id(second_context)
            self.assertNotEqual(first_scope, second_scope)

            first_controller = InteractionController(
                self.store, workspace_id=first_scope, session_id="surface:tui",
            )
            first_controller.propose_plan({
                "title": "First", "summary": "Only the first project.", "assumptions": [],
                "steps": [{"id": "one", "title": "One", "description": "Change first.",
                           "acceptance": ["First changed"]}],
            })
            second_controller = InteractionController(
                self.store, workspace_id=second_scope, session_id="surface:tui",
            )
            self.assertIsNotNone(first_controller.state()["pending_plan"])
            self.assertIsNone(second_controller.state()["pending_plan"])

            global_context = resolve_launch_context(home=home, global_mode=True)
            self.assertEqual(
                self.agent.interaction_workspace_id(global_context), self.agent.memory_bank.workspace_id,
            )

    def test_permission_setting_persists_per_project_and_gates_writes(self):
        from openkyrozen.workspace.context import resolve_launch_context

        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            with patch.object(self.agent, "_EXECUTION_SURFACE", "tui"), patch.object(self.agent, "_emit_stream_event"):
                self.agent._set_launch_context(resolve_launch_context(project_path=first, home=home))
                self.agent.bind_interaction_scope("first-chat")
                self.assertEqual(self.agent.permission_mode(), "ask")
                self.agent.set_permission_mode("full")
                self.assertEqual(self.agent.interaction_envelope()["permission_mode"], "full")

                prompts = []
                self.assertTrue(self.agent._authorize_tool_action(
                    "write_file", "src/app.py|change", approve=lambda action, args: prompts.append(action) or True,
                ))
                self.assertEqual(prompts, [])

                self.agent._set_launch_context(resolve_launch_context(project_path=second, home=home))
                self.agent.bind_interaction_scope("second-chat")
                self.assertEqual(self.agent.permission_mode(), "ask")
                prompts.clear()
                self.assertTrue(self.agent._authorize_tool_action(
                    "write_file", "src/app.py|change", approve=lambda action, args: prompts.append(action) or True,
                ))
                self.assertEqual(prompts, ["write_file"])

    def test_jev_permission_check_requires_high_confidence_and_sanitizes_payload(self):
        from openkyrozen.routing import transport

        response = {"answers": {"permission": {"type": "choice", "choice": "allow",
                    "confidence": 0.999, "probabilities": {"allow": 0.999, "deny": 0.001}}}}
        with patch.object(self.agent, "_emit_stream_event"), patch.object(
            self.agent, "_record_permission_decision"
        ), patch.object(transport, "_request", return_value=response) as request:
            self.assertTrue(self.agent._jev_permission_check("run_cmd", "external_or_irreversible_change"))
            state = request.call_args.args[2]
            self.assertEqual(state, {"action": "run_cmd", "risk_category": "external_or_irreversible_change", "surface": "local TUI"})
            self.assertNotIn("origin main", str(state))
            response["answers"]["permission"]["confidence"] = 0.994
            self.assertFalse(self.agent._jev_permission_check("run_cmd", "external_or_irreversible_change"))
            response["answers"]["permission"]["confidence"] = 0.999
            self.assertFalse(self.agent._jev_permission_check("read_file", "private_data_access"))
            response["answers"]["permission"]["probabilities"]["allow"] = "0.999"
            response["answers"]["permission"]["probabilities"]["deny"] = "0.001"
            self.assertFalse(self.agent._jev_permission_check("run_cmd", "external_or_irreversible_change"))

    def test_ask_mode_ignores_global_auto_approval_override(self):
        with patch.object(self.agent, "_EXECUTION_SURFACE", "tui"), patch.object(
            self.agent, "permission_mode", return_value="ask",
        ), patch.object(self.agent, "_emit_stream_event"), patch.dict(
            "os.environ", {"KYROZEN_APPROVAL_MODE": "never"},
        ):
            self.assertFalse(self.agent._authorize_tool_action(
                "write_file", "src/app.py|change", approve=lambda _action, _args: False,
            ))

    def test_protected_full_skips_jev_for_ordinary_workspace_edits(self):
        with patch.object(self.agent, "_EXECUTION_SURFACE", "tui"), patch.object(
            self.agent, "permission_mode", return_value="full_jev",
        ), patch.object(self.agent, "_jev_permission_check") as check:
            self.assertTrue(self.agent._authorize_tool_action("write_file", "src/app.py|change"))
            check.assert_not_called()

    def test_binding_empty_chat_clears_previous_short_term_memory(self):
        previous = (self.agent.short_term_memory, self.agent.tasks, self.agent._interaction_controller,
                    self.agent.memory_bank.session_id)
        def restore():
            self.agent.short_term_memory, self.agent.tasks, self.agent._interaction_controller = previous[:3]
            self.agent.memory_bank.session_id = previous[3]
        self.addCleanup(restore)
        self.agent.short_term_memory = [{"role": "user", "content": "previous project"}]
        empty_history = type("History", (), {"current": lambda self: None})()
        with patch.object(self.agent, "TaskManager", return_value=object()), \
                patch.object(self.agent, "InteractionController", return_value=object()), \
                patch.object(self.agent, "history_manager", return_value=empty_history), \
                patch.object(self.agent, "_restore_ponytail_level"):
            self.agent.bind_interaction_scope("chat-empty")
        self.assertEqual(self.agent.short_term_memory, [])

    def test_early_plan_acceptance_reply_is_saved_to_session_and_history(self):
        session = self.agent.open_session("early-plan-acceptance")
        reply = self.agent.chat(session, "accept plan")
        self.assertEqual(reply, "There is no pending plan to accept.")
        self.assertEqual(session.messages, [
            {"role": "user", "content": "accept plan"},
            {"role": "assistant", "content": "There is no pending plan to accept."},
        ])
        nodes = self.agent.history_manager(session.session_id).list()
        self.assertEqual(nodes[-1]["user_message"], "accept plan")
        self.assertEqual(nodes[-1]["assistant_message"], reply)

    def test_queued_plan_acceptance_rejects_a_newer_proposal(self):
        session = self.agent.open_session("queued-plan-acceptance")
        first = session.interaction.propose_plan({
            "title": "First proposal", "summary": "Create one file.",
            "steps": [{"id": "file", "title": "Create file", "description": "Write it.",
                       "acceptance": ["File exists"]}],
        })
        session.interaction.accept_plan(session.tasks, plan_id=first["plan_id"], version=first["version"])
        newer = session.interaction.propose_plan({
            "title": "Revised proposal", "summary": "Create the revised file.",
            "steps": [{"id": "file", "title": "Create revised file", "description": "Write it safely.",
                       "acceptance": ["Revised file exists"]}],
        })
        with self.assertRaisesRegex(InteractionError, "not pending"):
            session.interaction.accept_plan(
                session.tasks, plan_id=first["plan_id"], version=first["version"],
            )

        reply = self.agent.chat(session, "accept plan", plan_acceptance={
            "plan_id": first["plan_id"], "version": first["version"],
        })

        self.assertIn("changed", reply.lower())
        self.assertEqual(session.interaction.state()["pending_plan"]["version"], newer["version"])
        self.assertEqual(session.interaction.state()["executing_plan"]["version"], first["version"])

    def test_restore_history_refreshes_active_session_transcript(self):
        from openkyrozen.agent.conversation import ConversationHistory
        from types import SimpleNamespace

        session_id = "rollback-cache-refresh"
        session = self.agent.open_session(session_id)
        self.agent.bind_interaction_scope(session_id)
        history = ConversationHistory(
            session.memory.store, user_id=session.memory.user_id,
            workspace_id=self.agent.interaction_workspace_id(), session_id=session_id,
        )
        history.append(history.entry("user.input", {"text": "retained request"}))
        history.append(history.entry("turn.outcome", {"status": "completed", "text": "retained answer"}))
        session.messages = [{"role": "user", "content": "stale transcript"}]

        class Manager:
            def __init__(self, identifier):
                self.session_id = identifier

            def current(self):
                return {"id": "current"}

            def rollback(self, *_args, **_kwargs):
                return {"id": "target"}, {"id": "recovery"}

        with patch.object(self.agent, "history_manager", return_value=Manager(session_id)):
            self.agent.restore_history("target", confirm="rollback", expected_head="current")
        self.assertEqual(session.messages, history.display_messages())

    def test_question_bounds_and_event_reconstruction(self):
        question = self.controller.request_question({"questions": [{
            "id": "scope", "header": "Scope", "prompt": "Which package?",
            "choices": ["Core", "All packages"],
        }]}, original_input="Implement the change")
        restored = InteractionController(
            self.store, workspace_id="workspace", session_id="session",
        )
        state = restored.state()
        self.assertEqual(state["pending_question"]["request_id"], question["request_id"])
        self.assertEqual(state["effective_mode"], "plan")
        restored.resolve_question(question["request_id"], {"scope": "Core"})
        self.assertIsNone(restored.state()["pending_question"])
        with self.assertRaises(InteractionError):
            restored.request_question({"questions": [{
                "header": "Secret", "prompt": "Send your API key",
                "choices": ["Yes", "No"],
            }]})
        with self.assertRaises(InteractionError):
            restored.request_question({"questions": [{
                "header": "Approval", "prompt": "May I run this command?",
                "choices": ["Approve", "Deny"],
            }]})
        with self.assertRaises(InteractionError):
            restored.request_question({"questions": [
                {"id": "duplicate", "header": "One", "prompt": "First?", "choices": ["A", "B"]},
                {"id": "duplicate", "header": "Two", "prompt": "Second?", "choices": ["A", "B"]},
            ]})

    def test_plan_revision_acceptance_is_versioned_and_idempotent(self):
        first = self.controller.propose_plan({
            "title": "Change", "summary": "Make the requested change.", "assumptions": [],
            "steps": [{"id": "inspect", "title": "Inspect", "description": "Read the code.",
                       "acceptance": ["Relevant path is identified"]}],
        })
        second = self.controller.propose_plan({
            "title": "Change safely", "summary": "Revise after feedback.", "assumptions": ["Tests exist"],
            "steps": [{"id": "inspect", "title": "Inspect", "description": "Read the code and tests.",
                       "acceptance": ["Relevant path and tests are identified"]}],
        })
        self.assertEqual((first["version"], second["version"]), (1, 2))
        self.assertEqual(first["plan_id"], second["plan_id"])
        manager = TaskManager(self.store, workspace_id="workspace", session_id="session")
        accepted, created = self.controller.accept_plan(
            manager, plan_id=second["plan_id"], version=second["version"],
        )
        duplicate, created_again = self.controller.accept_plan(
            manager, plan_id=second["plan_id"], version=second["version"],
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(accepted, duplicate)
        self.assertEqual(len(manager.tasks), 1)

        self.assertEqual(manager.tasks[0]["acceptance"][0]["criterion"],
                         "Relevant path and tests are identified")
        self.assertEqual(self.controller.state()["effective_mode"], "agent")
        with self.assertRaisesRegex(InteractionError, "requires its linked Working Plan evidence"):
            self.controller.complete_plan(accepted)
        self.assertEqual(self.controller.state()["effective_mode"], "agent")
        completed_duplicate, created_after_completion = self.controller.accept_plan(
            manager, plan_id=second["plan_id"], version=second["version"],
        )
        self.assertEqual(completed_duplicate, accepted)
        self.assertFalse(created_after_completion)
        self.assertEqual(len(manager.tasks), 1)

    def test_provider_plan_text_cannot_duplicate_an_accepted_checklist(self):
        previous_tasks = self.agent.tasks
        previous_controller = self.agent._interaction_controller
        manager = TaskManager(self.store, workspace_id="workspace", session_id="session")
        proposal = self.controller.propose_plan({
            "title": "Eight steps", "summary": "Execute once.", "assumptions": [],
            "steps": [
                {"id": f"step-{index}", "title": f"Step {index}",
                 "description": (
                     "Create index.html with the requested content."
                     if index == 0 else f"Perform step {index}."
                 ), "acceptance": [f"Step {index} done"]}
                for index in range(8)
            ],
        })
        self.controller.accept_plan(
            manager, plan_id=proposal["plan_id"], version=proposal["version"],
        )
        self.agent.tasks = manager
        self.agent._interaction_controller = self.controller
        mode_token = self.agent._active_interaction_mode.set("agent")
        try:
            repeated = "Plan:\n" + "\n".join(
                f"{index}. Perform step {index}." for index in range(1, 9)
            )
            self.agent._tasks_from_plan(repeated)
            self.agent._tasks_from_plan(repeated)
            self.agent._observe_model_response(
                "TaskList:\n```json\n"
                + json.dumps([f"Use run_cmd to perform step {index}" for index in range(1, 9)])
                + "\n```"
            )
            self.assertEqual(len(manager.tasks), 8)
            evidence = manager.record_evidence(
                action="write_file", args="index.html|ready", result="Wrote index.html", success=True,
            )
            self.assertIsNone(evidence.get("task_id"))
            self.assertEqual(manager.tasks[0]["status"], "pending")
            self.assertFalse(any(item.get("acceptance") for item in manager.tasks[0]["evidence"]))
        finally:
            self.agent.tasks = previous_tasks
            self.agent._interaction_controller = previous_controller
            self.agent._active_interaction_mode.reset(mode_token)

    def test_exact_acceptance_phrases_and_control_parser(self):
        self.assertTrue(is_plan_acceptance("Execute plan."))
        self.assertTrue(is_plan_acceptance("执行计划。"))
        self.assertFalse(is_plan_acceptance("run plan"))
        self.assertFalse(is_plan_acceptance("The plan looks acceptable"))
        value = parse_control_block(
            'AskUser:\n```json\n{"questions": []}\n```', "AskUser",
        )
        self.assertEqual(value, {"questions": []})
        self.assertEqual(parse_control_block(
            'PlanProposal\n```json\n{"steps": []}\n```', "PlanProposal",
        ), {"steps": []})

    def test_provider_shaped_plan_keeps_only_explicit_approved_file_contracts(self):
        shaped = json.dumps({
            "plan_name": "Status page", "mode": "plan",
            "do_not_execute_until_approved": True,
            "overview": "Build after approval.", "assumptions": ["Python exists"],
            "bounds": {"workspace_paths": ["site"], "capabilities": ["read", "write"],
                       "effects": ["read_only", "filesystem"], "risk_ceiling": "normal"},
            "steps": [{"step": 1, "title": "Create page", "details": "Write index.html.",
                       "acceptance_criteria": ["index.html exists"],
                       "execution": {"action": "write_file", "arguments": {
                           "path": "site/index.html", "content": "approved",
                       }} }],
        })
        parsed = self.agent._observe_model_response(shaped)
        self.assertEqual(parsed["plan_proposal"]["title"], "Status page")
        self.assertEqual(parsed["plan_proposal"]["steps"][0]["id"], "step-1")
        self.assertEqual(parsed["plan_proposal"]["bounds"]["workspace_paths"], ["site"])
        self.assertEqual(parsed["plan_proposal"]["steps"][0]["execution"]["arguments"]["path"], "site/index.html")
        self.assertEqual(parsed["tool_calls"], [])

        executable = json.dumps({
            "mode": "plan", "plan_name": "Safe proposal", "goal": "Write after acceptance.",
            "steps": [{"title": "Write", "details": "Write README.",
                       "action": "write_file", "path": "README.md", "content": "unsafe"}],
        })
        sanitized = self.agent._observe_model_response(executable)
        self.assertEqual(sanitized["tool_calls"], [])
        self.assertEqual(sanitized["plan_proposal"]["title"], "Safe proposal")
        self.assertEqual(
            set(sanitized["plan_proposal"]["steps"][0]),
            {"id", "title", "description", "acceptance", "acceptance_ids", "acceptance_verifications"},
        )
        self.assertEqual(
            sanitized["plan_proposal"]["steps"][0]["acceptance"],
            ["Step completed with observable evidence"],
        )

    def test_strategic_file_execution_contract_is_validated_and_rendered_for_approval(self):
        plan = validate_plan_proposal({
            "title": "Create page", "summary": "Write the approved page.",
            "bounds": {"workspace_paths": ["site"], "capabilities": ["write"],
                       "effects": ["filesystem"], "risk_ceiling": "normal"},
            "steps": [{"id": "page", "title": "Create page", "description": "Write site/index.html.",
                       "acceptance": ["Page exists"], "execution": {
                           "action": "write_file", "arguments": {"path": "site/index.html", "content": "ok"},
                       }}],
        })
        self.assertIn('"path": "site/index.html"', render_plan(plan))
        with self.assertRaises(InteractionError):
            validate_plan_proposal({**plan, "steps": [{**plan["steps"][0], "execution": {
                "action": "run_cmd", "arguments": {"command": "rm -rf ."},
            }}]})
        for execution in (
            {"action": [], "arguments": {"path": "site/index.html", "content": "ok"}},
            {"action": "write_file", "arguments": {"path": "site/index.html"}},
            {"action": "edit_file", "arguments": {"path": "site/index.html", "old_text": "x",
                "new_text": "y", "expected_sha256": "stale"}},
        ):
            with self.subTest(execution=execution), self.assertRaises(InteractionError):
                validate_plan_proposal({**plan, "steps": [{**plan["steps"][0], "execution": execution}]})

    def test_plan_mode_converts_plain_plan_and_discards_early_actions(self):
        mode_token = self.agent._active_interaction_mode.set("plan")
        previous_controller = self.agent._interaction_controller
        self.agent._interaction_controller = self.controller
        try:
            parsed = self.agent._parse_model_response(
                "Plan:\n"
                "1. Create index.html with the requested heading.\n"
                "2. Serve it locally and verify HTTP 200.\n"
                'Action: {"action":"write_file","args":"index.html|unsafe"}'
            )
            self.assertEqual(parsed["tool_calls"], [])
            self.assertIsNone(parsed["protocol_error"])
            rendered = self.agent._interaction_gate(parsed, "Create a local page")
            self.assertIn("Create index.html", rendered)
            self.assertEqual(len(self.controller.state()["pending_plan"]["steps"]), 2)
            self.assertEqual(
                self.store.list_events(
                    "execution.receipt", workspace_id="workspace", session_id="session",
                ),
                [],
            )

            action_only = self.agent._parse_model_response(
                'Action: {"action":"run_cmd","args":"python -m http.server"}'
            )
            self.assertEqual(action_only["tool_calls"], [])
            self.assertIn("rejected an executable action", action_only["protocol_error"])

            markdown = self.agent._parse_model_response(
                "PlanProposal:\n\n**Goal:** Build locally.\n\n**Planned changes**\n\n"
                "1. **Create `index.html`** with no dependencies.\n"
                "   - Include a visible heading.\n"
                "2. **Serve on port 8765** with Python.\n"
                "3. **Verify HTTP 200** with curl.\n"
                "4. **Stop the server** and release the port.\n"
                "\n- This trailing note is not another step."
            )
            self.assertIsNone(markdown["protocol_error"])
            self.assertEqual(len(markdown["plan_proposal"]["steps"]), 4)
            self.assertEqual(markdown["plan_proposal"]["steps"][0]["id"], "step-1")
        finally:
            self.agent._interaction_controller = previous_controller
            self.agent._active_interaction_mode.reset(mode_token)

    def test_plan_mode_repairs_rejected_action_into_proposal(self):
        class LearningStub:
            def feedback_signal(self, _text): return None
            def route_profile(self, _text, _profile=None): return "coder"
            def begin_run(self, profile, _task, provider_model=None):
                return {"run_id": "plan-repair", "profile": profile, "provider_model": provider_model}
            def artifact_context(self, _run): return "", []

        class RuntimeStub:
            def turn_start(self, **_kwargs): return None
            def turn_end(self, **_kwargs): return None

        original = (
            self.agent.tasks, self.agent._interaction_controller, self.agent.learning_engine,
            self.agent._get_workspace_root(), self.agent._execution_capability_token,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = EventStore(root / "state.sqlite3")
            self.agent.tasks = TaskManager(store, workspace_id="repair", session_id="repair")
            self.agent._interaction_controller = InteractionController(
                store, workspace_id="repair", session_id="repair",
            )
            self.agent._interaction_controller.set_mode("plan")
            self.agent.learning_engine = LearningStub()
            self.agent._set_workspace_root(root)
            responses = [
                'Action: {"action":"write_file","args":"unsafe.txt|bad"}',
                'Action: {"action":"list_dir","args":"."}',
                'PlanProposal:\n```json\n{"title":"Safe plan","summary":"Write only after approval.",'
                '"assumptions":[],"steps":[{"id":"write","title":"Write file",'
                '"description":"Create the requested file.","acceptance":["File exists"]}]}\n```',
            ]
            try:
                with patch.object(self.agent, "_plugin_runtime_for_surface", return_value=RuntimeStub()), \
                        patch.object(self.agent, "_touch_detached_learning_heartbeat"), \
                        patch.object(self.agent, "dispatch_learning_cycle"), \
                        patch.object(self.agent, "_build_messages", return_value=[]), \
                        patch.object(self.agent, "_build_memory_context", return_value=""), \
                        patch.object(self.agent, "_classify_complexity", return_value="simple"), \
                        patch.object(self.agent, "_call_llm_with_spinner", side_effect=responses), \
                        patch.object(self.agent, "_finish_learning_run", side_effect=lambda run, receipts, task,
                                     result, records, tokens, started: result):
                    reply = self.agent._chat_turn("Create the file")
                self.assertIn("Safe plan (v1)", reply)
                self.assertFalse((root / "unsafe.txt").exists())
                self.assertEqual(
                    self.agent._interaction_controller.state()["pending_plan"]["title"], "Safe plan",
                )
            finally:
                (self.agent.tasks, self.agent._interaction_controller, self.agent.learning_engine,
                 previous_root, self.agent._execution_capability_token) = original
                self.agent._set_workspace_root(previous_root)

    def test_plan_prompt_advertises_only_read_and_network_tools(self):
        mode_token = self.agent._active_interaction_mode.set("plan")
        controls_token = self.agent._interaction_controls_enabled.set(True)
        previous_token = self.agent._execution_capability_token
        self.agent._execution_capability_token = issue_capability_token(
            "test:plan-prompt", frozenset({"read", "write", "shell", "network", "git", "dynamic"}),
        )
        try:
            prompt = self.agent._system_prompt("deliberately ignored")
        finally:
            self.agent._execution_capability_token = previous_token
            self.agent._interaction_controls_enabled.reset(controls_token)
            self.agent._active_interaction_mode.reset(mode_token)
        self.assertIn("PlanProposal", prompt)
        self.assertIn("read_file", prompt)
        self.assertIn("search_web", prompt)
        for forbidden in ("TaskList", "TaskDone", "DefineTool", "git_commit"):
            self.assertNotIn(forbidden, prompt)
        readonly_inventory = prompt.split("## Available read-only tools\n", 1)[1].split("\n## ", 1)[0]
        self.assertNotIn("- write_file:", readonly_inventory)
        self.assertNotIn("- run_cmd:", readonly_inventory)
        self.assertIn("does not execute before acceptance", prompt)
        self.assertIn("The first Plan-mode response must be exactly one listed read-only Action", prompt)
        self.assertIn('"criterion":"Observable result"', prompt)
        self.assertIn('"verification":{"action":"read_file"', prompt)
        self.assertIn('"expect":{"contains":"approved content"}', prompt)

    def test_bounded_write_objective_allows_mutation_with_a_separate_read_verifier(self):
        from openkyrozen.agent.types import ExecutionReceipt
        previous = self.agent._interaction_controller
        previous_tasks = self.agent.tasks
        previous_root = self.agent._get_workspace_root()
        manager = TaskManager(self.store, workspace_id="workspace", session_id="session")
        controller = InteractionController(self.store, workspace_id="workspace", session_id="session")
        proposal = controller.propose_plan({
            "title": "Write page", "summary": "Write the page.", "bounds": {
                "workspace_paths": ["site"], "capabilities": ["read", "write"],
                "effects": ["filesystem", "read_only"], "risk_ceiling": "normal",
            }, "steps": [{"id": "page", "title": "Write page", "description": "Write the page.",
                         "acceptance": [{"criterion": "Page can be read", "verification": {
                             "action": "read_file", "arguments": {"path": "site/index.html"},
                             "expect": {"contains": "contents"},
                         }}]}],
        })
        controller.accept_plan(manager, plan_id=proposal["plan_id"], version=proposal["version"])
        self.agent.tasks = manager
        self.agent._interaction_controller = controller
        self.agent._set_workspace_root(Path(self.directory.name))
        receipt = ExecutionReceipt(
            "receipt", "operation", "write_file", "site/index.html|ok", True,
            "start", "end", True, "written", call_id="call", call_id_source="synthetic",
            arguments={"path": "site/index.html", "content": "ok"}, tool_result=ToolResult(text="written"),
        )
        try:
            with patch.object(self.agent.tasks, "mutation_matches_current_task", return_value=(False, "wrong task")) as match, \
                    patch.object(self.agent, "_run_tool", return_value=receipt):
                result = self.agent._execute_turn_action(
                    "write_file", "site/index.html|ok", operation_scope="test", successful_operations=set(),
                )
            self.assertTrue(result.success, result.result)
            match.assert_not_called()
        finally:
            self.agent._interaction_controller = previous
            self.agent.tasks = previous_tasks
            self.agent._set_workspace_root(previous_root)

    def test_legacy_update_plan_is_not_matched_as_a_task_execution_action(self):
        previous = (self.agent.tasks, self.agent._interaction_controller)
        manager = TaskManager(self.store, workspace_id="workspace", session_id="session")
        controller = InteractionController(self.store, workspace_id="workspace", session_id="session")
        proposal = controller.propose_plan({
            "title": "Maintain the page", "summary": "Keep work within the page objective.",
            "bounds": {"workspace_paths": ["."], "capabilities": ["write"],
                       "effects": ["internal"], "risk_ceiling": "normal"},
            "steps": [{"id": "page", "title": "Maintain page", "description": "Update and verify the page.",
                       "acceptance": ["Page is verified"]}],
        })
        accepted, _ = controller.accept_plan(
            manager, plan_id=proposal["plan_id"], version=proposal["version"],
        )
        self.agent.tasks = manager
        self.agent._interaction_controller = controller
        args = json.dumps({
            "plan_id": accepted["working_plan_id"],
            "expected_revision": accepted["working_plan_revision"],
            "operations": [{"op": "update", "task": accepted["working_task_ids"][0],
                            "fields": {"priority": 2}}],
        })
        try:
            with patch.object(self.agent, "_confirm_tool_action", return_value=True), \
                    patch.object(manager, "mutation_matches_current_task", return_value=(False, "wrong task")) as match:
                result = self.agent._execute_turn_action(
                    "update_plan", args, operation_scope="plan-edit", successful_operations=set(),
                )
            self.assertTrue(result.success, result.result)
            self.assertEqual(manager.tasks[0]["priority"], 2)
            match.assert_not_called()
        finally:
            self.agent.tasks, self.agent._interaction_controller = previous

    def test_explicit_execution_contract_rejects_changed_write_arguments(self):
        from openkyrozen.agent.types import ExecutionReceipt
        previous = self.agent._interaction_controller
        previous_tasks = self.agent.tasks
        manager = TaskManager(self.store, workspace_id="workspace", session_id="session")
        controller = InteractionController(self.store, workspace_id="workspace", session_id="session")
        approved = {"path": "site/index.html", "content": "approved"}
        proposal = controller.propose_plan({
            "title": "Write page", "summary": "Write the approved content.", "bounds": {
                "workspace_paths": ["site"], "capabilities": ["read", "write"],
                "effects": ["filesystem", "read_only"], "risk_ceiling": "normal",
            }, "steps": [{"id": "page", "title": "Write page", "description": "Write the page.",
                         "execution": {"action": "write_file", "arguments": approved},
                         "acceptance": [{"criterion": "Page can be read", "verification": {
                             "action": "read_file", "arguments": {"path": "site/index.html"},
                             "expect": {"contains": "approved"},
                         }}]}],
        })
        controller.accept_plan(manager, plan_id=proposal["plan_id"], version=proposal["version"])
        self.agent.tasks = manager
        self.agent._interaction_controller = controller
        receipt = ExecutionReceipt(
            "receipt", "operation", "write_file", "site/index.html|different", True,
            "start", "end", True, "written", call_id="call", call_id_source="synthetic",
            arguments={"path": "site/index.html", "content": "different"}, tool_result=ToolResult(text="written"),
        )
        try:
            with patch.object(self.agent, "_run_tool", return_value=receipt) as execute:
                outcome = self.agent._execute_turn_action(
                    "write_file", "site/index.html|different", operation_scope="test", successful_operations=set(),
                )
            self.assertFalse(outcome.success)
            execute.assert_not_called()
        finally:
            self.agent._interaction_controller = previous
            self.agent.tasks = previous_tasks

    def test_structured_legacy_arguments_match_the_accepted_action_contract(self):
        from openkyrozen.agent.types import ExecutionReceipt
        previous = self.agent._interaction_controller
        previous_tasks = self.agent.tasks
        previous_root = self.agent._get_workspace_root()
        manager = TaskManager(self.store, workspace_id="workspace", session_id="session")
        controller = InteractionController(self.store, workspace_id="workspace", session_id="session")
        arguments = {"path": "src/file.py", "old_text": "draft", "new_text": "verified",
                     "expected_sha256": "a" * 64}
        proposal = controller.propose_plan({
            "title": "Update source", "summary": "Update the source file.", "bounds": {
                "workspace_paths": ["src"], "capabilities": ["write"],
                "effects": ["filesystem"], "risk_ceiling": "normal",
            }, "steps": [{"id": "edit", "title": "Update source", "description": "Update the source file.",
                         "acceptance": [{"criterion": "Source is updated", "verification": {
                             "action": "edit_file", "arguments": arguments,
                         }}]}],
        })
        controller.accept_plan(manager, plan_id=proposal["plan_id"], version=proposal["version"])
        self.agent.tasks = manager
        self.agent._interaction_controller = controller
        self.agent._set_workspace_root(Path(self.directory.name))
        receipt = ExecutionReceipt(
            "receipt", "operation", "edit_file", json.dumps(arguments), True,
            "start", "end", True, "edited", call_id="call", call_id_source="synthetic",
            arguments=arguments, tool_result=ToolResult(text="edited"),
        )
        try:
            with patch.object(self.agent, "_run_tool", return_value=receipt) as run_tool:
                result = self.agent._execute_turn_action(
                    "edit_file", json.dumps(arguments), operation_scope="test", successful_operations=set(),
                )
            self.assertTrue(result.success, result.result)
            run_tool.assert_called_once()
        finally:
            self.agent._interaction_controller = previous
            self.agent.tasks = previous_tasks
            self.agent._set_workspace_root(previous_root)

    def test_plan_requires_inspection_and_normalizes_structured_prose(self):
        class LearningStub:
            def feedback_signal(self, _text): return None
            def route_profile(self, _text, _profile=None): return "coder"
            def begin_run(self, profile, _task, provider_model=None):
                return {"run_id": "plan-prose", "profile": profile, "provider_model": provider_model}
            def artifact_context(self, _run): return "", []

        class RuntimeStub:
            def turn_start(self, **_kwargs): return None
            def turn_end(self, **_kwargs): return None

        original = (self.agent.tasks, self.agent._interaction_controller, self.agent.learning_engine,
                    self.agent._get_workspace_root(), self.agent._execution_capability_token)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = EventStore(root / "state.sqlite3")
            self.agent.tasks = TaskManager(store, workspace_id="prose", session_id="prose")
            self.agent._interaction_controller = InteractionController(
                store, workspace_id="prose", session_id="prose",
            )
            self.agent._interaction_controller.set_mode("plan")
            self.agent.learning_engine = LearningStub()
            self.agent._set_workspace_root(root)
            records = []
            responses = [
                "1. Create the todo data model.\n2. Add reminders, priority, and status.\n3. Verify the workflow.",
                'Action: {"action":"list_dir","args":"."}',
                "1. Define the todo data model.\n2. Add reminder scheduling.\n3. Add priority and status controls.\n4. Test create, update, and completion flows.",
            ]
            try:
                with patch.object(self.agent, "_plugin_runtime_for_surface", return_value=RuntimeStub()), \
                        patch.object(self.agent, "_touch_detached_learning_heartbeat"), \
                        patch.object(self.agent, "dispatch_learning_cycle"), \
                        patch.object(self.agent, "_build_messages", return_value=[]), \
                        patch.object(self.agent, "_build_memory_context", return_value=""), \
                        patch.object(self.agent, "_classify_complexity", return_value="simple"), \
                        patch.object(self.agent, "_call_llm_with_spinner", side_effect=responses), \
                        patch.object(self.agent, "_finish_learning_run", side_effect=lambda run, receipts, task,
                                     result, tool_records, tokens, started: records.extend(tool_records) or result):
                    reply = self.agent._chat_turn(
                        "I want you to build a todo list with reminders, priority, and status."
                    )
                pending = self.agent._interaction_controller.state()["pending_plan"]
                self.assertNotIn("bounded recovery", reply)
                self.assertIsNotNone(pending)
                self.assertEqual(pending["title"], "Define the todo data model.")
                self.assertEqual(len(pending["steps"]), 4)
                self.assertEqual([record["action"] for record in records], ["list_dir"])
            finally:
                (self.agent.tasks, self.agent._interaction_controller, self.agent.learning_engine,
                 previous_root, self.agent._execution_capability_token) = original
                self.agent._set_workspace_root(previous_root)

    def test_plan_mode_accepts_unwrapped_provider_plan_json(self):
        mode_token = self.agent._active_interaction_mode.set("plan")
        try:
            parsed = self.agent._parse_model_response(
                '{"title":"Todo list","summary":"Build the requested workflow",'
                '"steps":[{"title":"Model todos","description":"Store reminder, priority, and status fields",'
                '"acceptance":["Todo records support the requested fields"]}]}'
            )
        finally:
            self.agent._active_interaction_mode.reset(mode_token)
        self.assertIsNone(parsed["protocol_error"])
        self.assertEqual(parsed["plan_proposal"]["title"], "Todo list")
        self.assertEqual(len(parsed["plan_proposal"]["steps"]), 1)

    def test_plan_recovery_exhaustion_is_stable_and_never_executes_mutations(self):
        class LearningStub:
            def feedback_signal(self, _text): return None
            def route_profile(self, _text, _profile=None): return "coder"
            def begin_run(self, profile, _task, provider_model=None):
                return {"run_id": "plan-exhaust", "profile": profile, "provider_model": provider_model}
            def artifact_context(self, _run): return "", []

        class RuntimeStub:
            def turn_start(self, **_kwargs): return None
            def turn_end(self, **_kwargs): return None

        original = (self.agent.tasks, self.agent._interaction_controller, self.agent.learning_engine,
                    self.agent._get_workspace_root(), self.agent._execution_capability_token)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = EventStore(root / "state.sqlite3")
            self.agent.tasks = TaskManager(store, workspace_id="exhaust", session_id="exhaust")
            self.agent._interaction_controller = InteractionController(store, workspace_id="exhaust", session_id="exhaust")
            self.agent._interaction_controller.set_mode("plan")
            self.agent.learning_engine = LearningStub()
            self.agent._set_workspace_root(root)
            responses = [
                'Action: {"action":"write_file","args":"unsafe.txt|bad"}',
                'Action: {"action":"run_cmd","args":"touch unsafe.txt"}',
                'Action: {"action":"write_file","args":"unsafe.txt|bad"}',
                'Action: {"action":"run_cmd","args":"touch unsafe.txt"}',
            ]
            try:
                with patch.object(self.agent, "_plugin_runtime_for_surface", return_value=RuntimeStub()), \
                        patch.object(self.agent, "_touch_detached_learning_heartbeat"), \
                        patch.object(self.agent, "dispatch_learning_cycle"), \
                        patch.object(self.agent, "_build_messages", return_value=[]), \
                        patch.object(self.agent, "_classify_complexity", return_value="complex"), \
                        patch.object(self.agent, "_call_llm_with_spinner", side_effect=responses), \
                        patch.object(self.agent, "_execute_turn_action", side_effect=AssertionError("mutation executed")), \
                        patch.object(self.agent, "_finish_learning_run", side_effect=lambda run, receipts, task,
                                     result, records, tokens, started: result):
                    reply = self.agent._chat_turn("Fix the project")
                self.assertIn("bounded recovery", reply)
                self.assertIn("no changes were made", reply)
                self.assertFalse((root / "unsafe.txt").exists())
            finally:
                (self.agent.tasks, self.agent._interaction_controller, self.agent.learning_engine,
                 previous_root, self.agent._execution_capability_token) = original
                self.agent._set_workspace_root(previous_root)

    def test_plan_inspection_spans_rounds_and_repairs_malformed_proposal(self):
        class LearningStub:
            def feedback_signal(self, _text): return None
            def route_profile(self, _text, _profile=None): return "coder"
            def begin_run(self, profile, _task, provider_model=None):
                return {"run_id": "plan-inspect", "profile": profile, "provider_model": provider_model}
            def artifact_context(self, _run): return "", []

        class RuntimeStub:
            def turn_start(self, **_kwargs): return None
            def turn_end(self, **_kwargs): return None

        original = (self.agent.tasks, self.agent._interaction_controller, self.agent.learning_engine,
                    self.agent._get_workspace_root(), self.agent._execution_capability_token)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "note.txt").write_text("evidence", encoding="utf-8")
            store = EventStore(root / "state.sqlite3")
            self.agent.tasks = TaskManager(store, workspace_id="inspect", session_id="inspect")
            self.agent._interaction_controller = InteractionController(store, workspace_id="inspect", session_id="inspect")
            self.agent._interaction_controller.set_mode("plan")
            self.agent.learning_engine = LearningStub()
            self.agent._set_workspace_root(root)
            records = []
            responses = [
                'Action: {"action":"read_file","args":"note.txt"}',
                'Action: {"action":"list_dir","args":"."}',
                'PlanProposal:\n```json\n{"title":"Broken","steps":[}\n```',
                'PlanProposal:\n```json\n{"title":"Evidence plan","summary":"Use inspected evidence.",'
                '"assumptions":[],"steps":[{"id":"step-1","title":"Apply change",'
                '"description":"Apply the approved change.","acceptance":["Change verified"]}]}\n```',
            ]
            try:
                with patch.object(self.agent, "_plugin_runtime_for_surface", return_value=RuntimeStub()), \
                        patch.object(self.agent, "_touch_detached_learning_heartbeat"), \
                        patch.object(self.agent, "dispatch_learning_cycle"), \
                        patch.object(self.agent, "_build_messages", return_value=[]), \
                        patch.object(self.agent, "_build_memory_context", return_value=""), \
                        patch.object(self.agent, "_classify_complexity", return_value="complex"), \
                        patch.object(self.agent, "_call_llm_with_spinner", side_effect=responses), \
                        patch.object(self.agent, "_finish_learning_run", side_effect=lambda run, receipts, task,
                                     result, tool_records, tokens, started: records.extend(tool_records) or result):
                    reply = self.agent._chat_turn("Plan the evidence-backed change")
                self.assertIn("Evidence plan", reply)
                self.assertEqual([record["action"] for record in records], ["read_file", "list_dir"])
                self.assertEqual(self.agent._interaction_controller.state()["effective_mode"], "plan")
            finally:
                (self.agent.tasks, self.agent._interaction_controller, self.agent.learning_engine,
                 previous_root, self.agent._execution_capability_token) = original
                self.agent._set_workspace_root(previous_root)

    def test_control_blocks_combined_with_actions_fail_closed(self):
        parsed = self.agent._observe_model_response(
            'AskUser:\n```json\n{"questions":[{"id":"scope","header":"Scope",'
            '"prompt":"Which target?","choices":["Core","All"]}]}\n```\n'
            'Action: {"action":"write_file","args":"unsafe.txt|bad"}'
        )
        self.assertIn("only control block", parsed["protocol_error"])
        self.assertEqual(parsed["tool_calls"][0]["action"], "write_file")
        self.assertIsNotNone(parsed["question"])
        previous_controller = self.agent._interaction_controller
        token = self.agent._interaction_controls_enabled.set(False)
        self.agent._interaction_controller = self.controller
        try:
            question_only = self.agent._observe_model_response(
                'AskUser:\n```json\n{"questions":[{"id":"scope","header":"Scope",'
                '"prompt":"Which target?","choices":["Core","All"]}]}\n```'
            )
            self.assertIn("non-interactive", self.agent._interaction_gate(question_only, "Inspect"))
            self.assertIsNone(self.controller.state()["pending_question"])
        finally:
            self.agent._interaction_controller = previous_controller
            self.agent._interaction_controls_enabled.reset(token)

    def test_read_only_modes_deny_mutating_and_dynamic_tools(self):
        previous_token = self.agent._execution_capability_token
        previous_root = self.agent._get_workspace_root()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "notes.txt").write_text("safe", encoding="utf-8")
            self.agent._set_workspace_root(root)
            self.agent._execution_capability_token = issue_capability_token(
                "test:plan", mode_capabilities(
                    frozenset({"read", "write", "shell", "network", "git", "browser", "dynamic"}),
                    "plan",
                ),
            )
            self.agent.AVAILABLE_TOOLS["temporary_dynamic_tool"] = lambda args: ToolResult(text=f"ran:{args}")
            try:
                self.assertIn("safe", self.agent._run_tool("read_file", "notes.txt"))
                for action, args in (
                    ("write_file", "blocked.txt|bad"),
                    ("run_cmd", "touch blocked-shell.txt"),
                    ("git_commit", "blocked"),
                    ("browser_click", "button"),
                    ("temporary_dynamic_tool", "value"),
                ):
                    result = self.agent._run_tool(action, args)
                    self.assertTrue(result.startswith("Error:"), (action, result))
                self.assertFalse((root / "blocked.txt").exists())
                self.assertFalse((root / "blocked-shell.txt").exists())
            finally:
                self.agent.AVAILABLE_TOOLS.pop("temporary_dynamic_tool", None)
                self.agent._execution_capability_token = previous_token
                self.agent._set_workspace_root(previous_root)

    def test_accepted_plan_rejects_unrelated_mutation_before_execution(self):
        previous = (
            self.agent.tasks, self.agent._interaction_controller, self.agent._execution_capability_token,
            self.agent._get_workspace_root(),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = EventStore(root / "state.sqlite3")
            manager = TaskManager(store, workspace_id="bound", session_id="surface:tui")
            controller = InteractionController(
                store, workspace_id="bound", session_id="surface:tui",
            )
            proposal = controller.propose_plan({
                "title": "Create page", "summary": "Create only the accepted page.",
                "assumptions": [], "bounds": {"workspace_paths": ["."],
                    "capabilities": ["read", "write"], "effects": ["read_only", "filesystem"],
                    "risk_ceiling": "normal"}, "steps": [{
                    "id": "page", "title": "Create index.html",
                    "description": "Write index.html with the requested markup.",
                    "acceptance": [{"criterion": "index.html exists", "verification": {
                        "action": "read_file", "arguments": {"path": "index.html"},
                        "expect": {"contains": "<html"},
                    }}],
                }],
            })
            controller.accept_plan(
                manager, plan_id=proposal["plan_id"], version=proposal["version"],
            )
            self.agent.tasks = manager
            self.agent._interaction_controller = controller
            self.agent._set_workspace_root(root)
            self.agent._execution_capability_token = issue_capability_token(
                "test:accepted-plan", frozenset({"write"}),
            )
            try:
                operations: set[str] = set()
                rejected = self.agent._execute_turn_action(
                    "write_file", "README.md|wrong", operation_scope="bound",
                    successful_operations=operations,
                )
                self.assertFalse(rejected.success)
                self.assertEqual(rejected.failure, "permission_denied")
                self.assertFalse((root / "README.md").exists())

                accepted = self.agent._execute_turn_action(
                    "write_file", "index.html|ready", operation_scope="bound",
                    successful_operations=operations,
                )
                self.assertTrue(accepted.success, accepted.result)
                self.assertEqual((root / "index.html").read_text(encoding="utf-8"), "ready")
            finally:
                (self.agent.tasks, self.agent._interaction_controller, self.agent._execution_capability_token,
                 previous_root) = previous
                self.agent._set_workspace_root(previous_root)

    def test_pipe_content_cannot_use_a_sibling_file_from_a_strategic_objective(self):
        previous = (
            self.agent.tasks, self.agent._interaction_controller, self.agent._execution_capability_token,
            self.agent._get_workspace_root(),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = EventStore(root / "state.sqlite3")
            manager = TaskManager(store, workspace_id="bounded", session_id="surface:tui")
            controller = InteractionController(store, workspace_id="bounded", session_id="surface:tui")
            proposal = controller.propose_plan({
                "title": "Update public page", "summary": "Update only the public index page.",
                "bounds": {"workspace_paths": ["src"], "capabilities": ["read", "write"],
                           "effects": ["read_only", "filesystem"], "risk_ceiling": "normal"},
                "steps": [{"id": "page", "title": "Write public page",
                           "description": "Write src/public/index.html.",
                           "acceptance": [{"criterion": "Public page can be read", "verification": {
                               "action": "read_file", "arguments": {"path": "src/public/index.html"},
                               "expect": {"contains": "ready"},
                           }}]}],
            })
            controller.accept_plan(manager, plan_id=proposal["plan_id"], version=proposal["version"])
            self.agent.tasks = manager
            self.agent._interaction_controller = controller
            self.agent._set_workspace_root(root)
            self.agent._execution_capability_token = issue_capability_token(
                "test:exact-strategic-path", frozenset({"write"}),
            )
            try:
                receipt = self.agent._execute_turn_action(
                    "write_file", "src/private/index.html|content|with-pipes",
                    operation_scope="exact-strategic-path", successful_operations=set(),
                )
                self.assertFalse(receipt.success)
                self.assertEqual(receipt.failure, "permission_denied")
                self.assertFalse((root / "src/private/index.html").exists())
                self.assertFalse((root / "src/public/index.html").exists())
            finally:
                (self.agent.tasks, self.agent._interaction_controller, self.agent._execution_capability_token,
                 previous_root) = previous
                self.agent._set_workspace_root(previous_root)

    def test_duplicate_legacy_write_uses_canonical_typed_operation_identity(self):
        previous = (self.agent.tasks, self.agent._interaction_controller,
                    self.agent._execution_capability_token, self.agent._get_workspace_root())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = EventStore(root / "state.sqlite3")
            manager = TaskManager(store, workspace_id="duplicate", session_id="surface:tui")
            controller = InteractionController(store, workspace_id="duplicate", session_id="surface:tui")
            proposal = controller.propose_plan({
                "title": "Create page", "summary": "Create only the accepted page.",
                "bounds": {"workspace_paths": ["."], "capabilities": ["read", "write"],
                           "effects": ["read_only", "filesystem"], "risk_ceiling": "normal"},
                "steps": [{"id": "page", "title": "Create index.html", "description": "Write index.html.",
                           "acceptance": ["index.html exists"]}],
            })
            controller.accept_plan(manager, plan_id=proposal["plan_id"], version=proposal["version"])
            self.agent.tasks, self.agent._interaction_controller = manager, controller
            self.agent._set_workspace_root(root)
            self.agent._execution_capability_token = issue_capability_token("test:duplicate", frozenset({"write"}))
            operations: set[str] = set()
            try:
                first = self.agent._execute_turn_action(
                    "write_file", "index.html|same", operation_scope="duplicate", successful_operations=operations,
                )
                second = self.agent._execute_turn_action(
                    "write_file", "index.html|same", operation_scope="duplicate", successful_operations=operations,
                )
                self.assertTrue(first.success, first.result)
                self.assertFalse(second.success)
                self.assertEqual(second.failure, "conflict")
            finally:
                (self.agent.tasks, self.agent._interaction_controller,
                 self.agent._execution_capability_token, previous_root) = previous
                self.agent._set_workspace_root(previous_root)

    def test_material_revision_blocks_mutations_until_approval(self):
        previous = (
            self.agent.tasks, self.agent._interaction_controller, self.agent._execution_capability_token,
            self.agent._get_workspace_root(),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = EventStore(root / "state.sqlite3")
            manager = TaskManager(store, workspace_id="bound", session_id="surface:tui")
            controller = InteractionController(store, workspace_id="bound", session_id="surface:tui")
            proposal = controller.propose_plan({
                "title": "Create page", "summary": "Create only the accepted page.",
                "bounds": {"workspace_paths": ["."], "capabilities": ["read", "write", "browser"],
                           "effects": ["read_only", "filesystem", "remote"], "risk_ceiling": "normal"},
                "steps": [{"id": "page", "title": "Create page", "description": "Write index.html.",
                           "acceptance": ["index.html exists"]}],
            })
            accepted, _ = controller.accept_plan(manager, plan_id=proposal["plan_id"], version=1)
            controller.propose_plan({
                "title": "Create page and README", "summary": "Add another artifact.",
                "bounds": proposal["bounds"],
                "steps": [{"id": "docs", "title": "Create README", "description": "Write README.md.",
                           "acceptance": ["README.md exists"]}],
            })
            self.agent.tasks = manager
            self.agent._interaction_controller = controller
            self.agent._set_workspace_root(root)
            self.agent._execution_capability_token = issue_capability_token(
                "test:pending-revision", frozenset({"write", "browser"}),
            )
            try:
                receipt = self.agent._execute_turn_action(
                    "write_file", "index.html|must wait", operation_scope="pending-revision",
                    successful_operations=set(),
                )
                self.assertFalse(receipt.success)
                self.assertEqual(receipt.failure, "permission_denied")
                self.assertFalse((root / "index.html").exists())
                browser = self.agent._execute_turn_action(
                    "browser_open", "https://example.com", operation_scope="bound",
                    successful_operations=set(),
                )
                self.assertFalse(browser.success)
                self.assertEqual(browser.failure, "permission_denied")
                native_browser = self.agent.execute_call(
                    self.agent.current_session,
                    ToolCall("pending-browser", "browser_open", {"url": "https://example.com"}),
                    operation_scope="bound", approve=lambda *_args, **_kwargs: True,
                )
                self.assertFalse(native_browser.success)
                self.assertEqual(native_browser.failure, "permission_denied")
                native_close = self.agent.execute_call(
                    self.agent.current_session,
                    ToolCall("pending-browser-close", "browser_close", {"session_id": "browser-1"}),
                    operation_scope="bound", approve=lambda *_args, **_kwargs: True,
                )
                self.assertFalse(native_close.success)
                self.assertEqual(native_close.failure, "permission_denied")
                from openkyrozen.agent.types import ExecutionReceipt
                cancelled = ExecutionReceipt(
                    "cancel-receipt", "cancel-operation", "cancel_subagent", "run-1", True,
                    "start", "end", True, "cancelled", call_id="cancel-call",
                    call_id_source="synthetic", arguments={"run_id": "run-1"},
                    tool_result=ToolResult(text="cancelled"),
                )
                with patch.object(self.agent, "_run_tool", return_value=cancelled) as dispatch:
                    cancellation = self.agent._execute_turn_action(
                        "cancel_subagent", "run-1", operation_scope="bound", successful_operations=set(),
                    )
                self.assertTrue(cancellation.success)
                dispatch.assert_called_once()
                self.assertEqual(controller.state()["executing_plan"]["fingerprint"], accepted["fingerprint"])
            finally:
                (self.agent.tasks, self.agent._interaction_controller, self.agent._execution_capability_token,
                 previous_root) = previous
                self.agent._set_workspace_root(previous_root)

    def test_mocked_provider_flow_questions_revision_acceptance_and_agent_receipt(self):
        class LearningStub:
            def feedback_signal(self, _text): return None
            def route_profile(self, _text, _profile=None): return "coder"
            def begin_run(self, profile, _task, provider_model=None):
                return {"run_id": "interaction-flow", "profile": profile, "provider_model": provider_model}
            def artifact_context(self, _run): return "", []

        class RuntimeStub:
            def turn_start(self, **_kwargs): return None
            def turn_end(self, **_kwargs): return None

        original = (
            self.agent.tasks, self.agent._interaction_controller, self.agent.learning_engine,
            self.agent._get_workspace_root(), self.agent._execution_capability_token,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = EventStore(root / "state.sqlite3")
            self.agent.tasks = TaskManager(store, workspace_id="flow", session_id="flow")
            self.agent._interaction_controller = InteractionController(
                store, workspace_id="flow", session_id="flow",
            )
            self.agent.learning_engine = LearningStub()
            self.agent._set_workspace_root(root)
            responses = [
                'Plan:\n1. Clarify the target before changing it.\n'
                'TaskList:\n```json\n[{"id":"premature","description":"Must not execute before acceptance"}]\n```',
                'Action: {"action":"list_dir","args":"."}',
                'AskUser:\n```json\n{"questions": [}\n```',
                '```json\n{"questions":[{"id":"scope","header":"Scope",'
                '"prompt":"Which target?","choices":["Core","All"]}]}\n```',
                'PlanProposal:\n```json\n{"title":"Marker","summary":"Create a marker safely.",'
                '"bounds":{"workspace_paths":["."],"capabilities":["read","write"],'
                '"effects":["read_only","filesystem"],"risk_ceiling":"normal"},'
                '"assumptions":[],"steps":[{"id":"write-marker","title":"Write marker",'
                '"description":"Create marker.txt with requested content.","acceptance":[{"criterion":"marker.txt exists",'
                '"verification":{"action":"read_file","arguments":{"path":"marker.txt"},"expect":{"contains":"ready"}}}]}]}\n```',
                'PlanProposal:\n```json\n{"title":"Marker with verification","summary":"Create and verify a marker.",'
                '"bounds":{"workspace_paths":["."],"capabilities":["read","write"],'
                '"effects":["read_only","filesystem"],"risk_ceiling":"normal"},'
                '"assumptions":[],"steps":[{"id":"write-marker","title":"Write marker",'
                '"description":"Create marker.txt with requested content.","acceptance":[{"criterion":"marker.txt exists",'
                '"verification":{"action":"read_file","arguments":{"path":"marker.txt"},"expect":{"contains":"ready"}}}]}]}\n```',
                'Action: {"action":"write_file","args":"marker.txt|ready"}',
                'AskUser:\n```json\n{"questions":[{"id":"verify","header":"Verification",'
                '"prompt":"Which accepted check should finish the task?","choices":["File exists","Read contents"]}]}\n```',
                'Action: {"action":"read_file","args":"marker.txt"}',
                'TaskDone: 0\nMarker created and verified.',
                'Marker created and verified.',
            ]
            try:
                stream_events = []
                stream_token = self.agent._stream_event_callback.set(stream_events.append)
                with patch.object(self.agent, "_plugin_runtime_for_surface", return_value=RuntimeStub()), \
                        patch.object(self.agent, "_touch_detached_learning_heartbeat"), \
                        patch.object(self.agent, "dispatch_learning_cycle"), \
                        patch.object(self.agent, "_build_messages", return_value=[]), \
                        patch.object(self.agent, "_build_memory_context", return_value=""), \
                        patch.object(self.agent, "_classify_complexity", return_value="simple"), \
                        patch.object(self.agent, "_call_llm_with_spinner", side_effect=responses), \
                        patch.object(self.agent, "_finish_learning_run", side_effect=lambda run, receipts, task,
                                     result, records, tokens, started: result):
                    first = self.agent._chat_turn("Create a marker file")
                    self.assertIn("Which target?", first)
                    self.assertEqual(self.agent.tasks.tasks, [])
                    initial_plan = self.agent._chat_turn("Core")
                    self.assertIn("Marker (v1)", initial_plan)
                    revised_plan = self.agent._chat_turn("Add explicit verification")
                    self.assertIn("v2", revised_plan)
                    suspended = self.agent._chat_turn("accept plan")
                    self.assertIn("Which accepted check", suspended)
                    self.assertIsNotNone(self.agent._interaction_controller.state()["executing_plan"])
                    self.assertTrue(any(
                        event.get("event") == "interaction"
                        and event.get("interaction", {}).get("effective_mode") == "agent"
                        for event in stream_events
                    ))
                    executed = self.agent._chat_turn("Read contents")
                self.assertIn("Marker created", executed)
                self.assertEqual((root / "marker.txt").read_text(encoding="utf-8"), "ready")
                self.assertEqual(len(self.agent.tasks.tasks), 1)
                self.assertEqual(self.agent.tasks.tasks[0]["status"], "succeeded")
                accepted = store.list_events(
                    "interaction.plan_accepted", workspace_id="flow", session_id="flow",
                )
                self.assertEqual(len(accepted), 1)
                state = self.agent._interaction_controller.state()
                self.assertEqual(state["preference_mode"], "auto")
                self.assertEqual(state["effective_mode"], "ask")
            finally:
                if 'stream_token' in locals():
                    self.agent._stream_event_callback.reset(stream_token)
                (self.agent.tasks, self.agent._interaction_controller, self.agent.learning_engine,
                 previous_root, self.agent._execution_capability_token) = original
                self.agent._set_workspace_root(previous_root)


if __name__ == "__main__":
    unittest.main()

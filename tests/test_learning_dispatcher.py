import asyncio
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

from openkyrozen.app.bootstrap import build_application

_application = build_application(surface="cli")

main = _application.runtime
from openkyrozen.interfaces.web.service import WebService
server = WebService(_application)
from openkyrozen.app.bootstrap import build_memory as MemoryBank
from openkyrozen.tasks.engine import TaskManager
from openkyrozen.skills.registry import SkillRegistry


class LearningDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.original_memory = main.memory_bank
        self.original_engine = main.learning_engine
        self.original_messages = list(main.short_term_memory)
        self.original_tasks = main.tasks
        self.original_root = main._get_workspace_root()
        self.original_flags = main._SELF_LEARNING_FLAGS
        self.original_registry = main._LEARNING_FEATURE_REGISTRY
        self.original_cursor = main._learning_dispatch_cursor
        self.original_scan_time = main._last_project_scan_time
        self.original_preferences = dict(main._user_preferences)
        self.original_hydrated_preferences = dict(main._hydrated_preferences)
        self.original_preference_scope = main._preference_scope
        self.original_graph = {key: list(value) for key, value in main._knowledge_graph.items()}
        self.original_project_graph = main._project_graph
        self.original_libraries = set(main._known_libraries)
        self.original_provider = main.llm_provider
        self.original_provider_config = main._provider_config

    def tearDown(self):
        main.memory_bank = self.original_memory
        main.learning_engine = self.original_engine
        main.short_term_memory = self.original_messages
        main.tasks = self.original_tasks
        main._set_workspace_root(self.original_root)
        main._SELF_LEARNING_FLAGS = self.original_flags
        main._LEARNING_FEATURE_REGISTRY = self.original_registry
        main._learning_dispatch_cursor = self.original_cursor
        main._last_project_scan_time = self.original_scan_time
        main._user_preferences.clear()
        main._user_preferences.update(self.original_preferences)
        main._hydrated_preferences.clear()
        main._hydrated_preferences.update(self.original_hydrated_preferences)
        main._preference_scope = self.original_preference_scope
        main._knowledge_graph.clear()
        main._knowledge_graph.update({key: list(value) for key, value in self.original_graph.items()})
        main._project_graph = self.original_project_graph
        main._known_libraries.clear()
        main._known_libraries.update(self.original_libraries)
        main.llm_provider = self.original_provider
        main._provider_config = self.original_provider_config

    def _isolated_runtime(self, root: Path) -> None:
        memory = MemoryBank(
            root / "state.sqlite3", user_id="learning-user", workspace_id="learning-project",
            session_id="learning-session",
        )
        main.memory_bank = memory
        main.learning_engine = main.LearningEngine(memory, registry=SkillRegistry(
            memory.store, workspace_id="learning-project", root=root / "skills"))
        main.tasks = TaskManager(
            memory.store, user_id="learning-user", workspace_id="learning-project",
            session_id="learning-session",
        )
        main._set_workspace_root(root)

    def test_registry_has_twenty_independent_callable_features(self):
        self.assertEqual(len(main._LEARNING_FEATURE_ORDER), 20)
        self.assertEqual(set(main._LEARNING_FEATURE_ORDER), set(main._LEARNING_FEATURE_REGISTRY))
        for name in main._LEARNING_FEATURE_ORDER:
            self.assertTrue(main._LEARNING_FEATURE_REGISTRY[name]["description"])
            self.assertTrue(callable(main._LEARNING_FEATURE_REGISTRY[name]["executor"]))
            self.assertIn(name, main._SELF_LEARNING_FLAGS)

    def test_conversation_learning_retries_failed_model_and_uses_promoted_fact(self):
        with tempfile.TemporaryDirectory() as directory:
            self._isolated_runtime(Path(directory))
            chat = "User: Remember that Orion uses orion.toml for configuration.\nAssistant: Understood."
            main.memory_bank.add_log(chat)
            with patch.object(main, "_learning_model_response", side_effect=[None, "- Orion uses orion.toml for configuration.", "- Orion uses orion.toml for configuration."]):
                def run():
                    return main.dispatch_learning_cycle(surface="test", trigger="turn", max_features=1,
                        feature_names=("auto_learn_conversations",))

                self.assertFalse(run()[0]["changed"])
                self.assertFalse(main.memory_bank.store.list_events("learning.conversation_scan", limit=1,
                    user_id="learning-user", workspace_id="learning-project"))
                run()  # a candidate changes state but is not a usable product yet
                self.assertEqual(main.learning_engine.status(1)[0]["status"], "candidate")
                self.assertFalse(next(item for item in main.learning_feature_status()
                    if item["name"] == "auto_learn_conversations")["last_product_id"])
                main.memory_bank.add_log(chat)
                self.assertTrue(run()[0]["changed"])

            context = main._build_memory_context("Orion configuration orion.toml")
            self.assertIn("kind=fact", context)
            self.assertIn("Orion uses orion.toml", context)
            feature = next(item for item in main.learning_feature_status()
                if item["name"] == "auto_learn_conversations")
            self.assertTrue(feature["last_product_id"])
            self.assertTrue(feature["last_used_at"])
            from fastapi.testclient import TestClient
            response = TestClient(server.app).get("/api/v2/learning/features")
            self.assertEqual(response.status_code, 200, response.text)
            web_feature = next(item for item in response.json()["features"]
                if item["name"] == "auto_learn_conversations")
            self.assertEqual(web_feature["product_status"], "used")

    def test_importance_scores_change_the_selected_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            self._isolated_runtime(Path(directory))
            main.memory_bank.add_log("FACT: lantern configuration alpha")
            main.memory_bank.add_log("note: lantern configuration beta")
            before = main._build_memory_context("lantern configuration", n=1)
            self.assertIn("beta", before)
            main.dispatch_learning_cycle(surface="test", trigger="turn", max_features=1,
                feature_names=("memory_importance_scoring",))
            after = main._build_memory_context("lantern configuration", n=1)
            self.assertIn("alpha", after)
            self.assertNotIn("beta", after)

    def test_dynamic_tool_learning_never_grants_capability(self):
        with tempfile.TemporaryDirectory() as directory:
            self._isolated_runtime(Path(directory))
            stats = {"audit_probe": {"calls": 2, "successes": 2}}
            with patch.dict(main.AVAILABLE_TOOLS, {"audit_probe": lambda _args: "ok"}), \
                    patch.object(main, "_tool_stats", stats), \
                    patch.object(main, "ALLOW_DYNAMIC_TOOLS", True), \
                    patch.object(main.learning_engine, "_review_claim_evidence", return_value=None):
                def run():
                    return main.dispatch_learning_cycle(surface="test", trigger="turn", max_features=1,
                        feature_names=("dynamic_tool_definition",))

                run()
                self.assertEqual(main.learning_engine.status(1)[0]["status"], "candidate")
                stats["audit_probe"] = {"calls": 3, "successes": 3}
                run()
                self.assertEqual(main.learning_engine.status(1)[0]["status"], "active")
                self.assertIn("TOOL_GUIDE", main._build_memory_context("audit_probe"))
                with patch.object(main, "_permitted_tool_names", return_value=set()):
                    self.assertNotIn("TOOL_GUIDE", main._build_memory_context("audit_probe"))
            self.assertNotIn("TOOL_GUIDE", main._build_memory_context("audit_probe"))

    def test_verified_correction_preflight_is_reused_on_matching_task(self):
        with tempfile.TemporaryDirectory() as directory:
            self._isolated_runtime(Path(directory))
            run = main.learning_engine.begin_run("coder", "fix parser")
            main.learning_engine.record_outcome(run, [], verified=True, success=False, correction=True)
            result = main.dispatch_learning_cycle(surface="test", trigger="turn", max_features=1,
                feature_names=("learning_rollback",))
            self.assertTrue(result[0]["changed"])
            later = main.learning_engine.begin_run("coder", "fix parser")
            context, _ = main.learning_engine.artifact_context(later)
            self.assertIn("Corrected failure preflight", context)
            self.assertEqual(main.learning_engine.negative_preflight("researcher", "fix parser"), [])
            state = next(item for item in main.learning_feature_status() if item["name"] == "learning_rollback")
            self.assertEqual(state["product_status"], "used")

    def test_invented_skill_and_composition_need_distinct_observations(self):
        with tempfile.TemporaryDirectory() as directory:
            self._isolated_runtime(Path(directory))
            for number in range(5):
                main.memory_bank.add_log(f"User: Verify Orion parser case {number}.\nAssistant: Check its tests.")
            answer = "Skill Name: Orion verification\nDescription: Verify the parser\nSteps:\n1. Run parser tests\n2. Check output"
            with patch.object(main, "_learning_model_response", return_value=answer), \
                    patch.object(main.learning_engine, "_review_claim_evidence", return_value=None):
                def dispatch(feature):
                    return main.dispatch_learning_cycle(surface="test", trigger="turn", max_features=1,
                        user_input="Verify Orion parser", feature_names=(feature,))

                dispatch("invent_skills")
                self.assertEqual(main.learning_engine.status(1)[0]["status"], "candidate")
                self.assertFalse(next(item for item in main.learning_feature_status()
                    if item["name"] == "invent_skills")["last_product_id"])
                main.memory_bank.add_log("User: Verify Orion parser in the release build.\nAssistant: Check its tests.")
                dispatch("invent_skills")
                self.assertEqual(main.learning_engine.status(1)[0]["status"], "active")

            with patch.object(main, "_learning_model_response", return_value="1. Orion verification: Run parser tests"), \
                    patch.object(main.learning_engine, "_review_claim_evidence", return_value=None):
                for run_id in ("compose-run-one", "compose-run-two"):
                    token = main._active_usage_run_id.set(run_id)
                    try:
                        dispatch("skill_composition")
                    finally:
                        main._active_usage_run_id.reset(token)
            self.assertIn("STRATEGY: For Verify Orion parser", main._build_memory_context("Verify Orion parser"))
            states = {item["name"]: item for item in main.learning_feature_status()}
            self.assertEqual(states["invent_skills"]["product_status"], "used")
            self.assertEqual(states["skill_composition"]["product_status"], "used")

    def test_matching_learning_products_survive_vector_rank_and_missing_index_entries(self):
        for omit_products in (False, True):
            with self.subTest(omit_products=omit_products), tempfile.TemporaryDirectory() as directory:
                self._isolated_runtime(Path(directory))
                observations = [main.memory_bank.add_log(f"User: Verify Orion parser observation {number}")
                                for number in range(20)]
                skill = main.memory_bank.add_log("SKILL: Orion verification - Verify Orion parser tests",
                    metadata={"learning_feature": "invent_skills"})
                strategy = main.memory_bank.add_log("STRATEGY: For Verify Orion parser, run focused tests",
                    metadata={"learning_feature": "skill_composition"})
                rows = main.memory_bank.store.list_memories(status="active", limit=100,
                    user_id="learning-user", workspace_id="learning-project", session_id="learning-session")
                by_id = {row["id"]: row for row in rows}
                ranked_ids = observations + ([] if omit_products else [skill, strategy])
                collection = Mock()
                collection.count.return_value = len(rows)
                collection.query.return_value = {"ids": [ranked_ids],
                    "documents": [[by_id[identifier]["content"] for identifier in ranked_ids]]}
                main.memory_bank._collection = collection
                with patch("openkyrozen.routing.decision_assist.decision_assist", return_value=None):
                    context = main._build_memory_context("Verify Orion parser", n=3)
                self.assertIn("SKILL: Orion verification", context)
                self.assertIn("STRATEGY: For Verify Orion parser", context)
                self.assertEqual(context.count("- kind="), 3)
                receipts = main.memory_bank.store.list_events("learning.product_used", limit=1,
                    user_id="learning-user", workspace_id="learning-project")
                self.assertEqual({item["memory_id"] for item in receipts[0]["payload"]["products"]},
                                 {skill, strategy})

    def test_learning_product_fallback_respects_relevance_and_visibility(self):
        with tempfile.TemporaryDirectory() as directory:
            self._isolated_runtime(Path(directory))
            observation = main.memory_bank.add_log("User: Verify Orion parser observation")
            for text, metadata in (
                ("SKILL: private Verify Orion parser", {"visibility": "private", "speaker": "alice"}),
                ("SKILL: other profile Verify Orion parser", {"scope": {"type": "profile", "value": "researcher"}}),
                ("SKILL: group Verify Orion parser", {"visibility": "group", "audiences": ["secret-team"]}),
                ("SKILL: unrelated Saturn database", {}),
                ("SKILL: partial Orion database", {}),
                ("SKILL: forbidden Verify Orion parser", {"tool_name": "forbidden_tool"}),
            ):
                feature = "dynamic_tool_definition" if "tool_name" in metadata else "invent_skills"
                main.memory_bank.add_log(text, metadata={"learning_feature": feature, **metadata})
            foreign = main.memory_bank.scoped(workspace_id="foreign-project")
            foreign.add_log("SKILL: foreign Verify Orion parser", metadata={"learning_feature": "invent_skills"})
            other_session = main.memory_bank.scoped(session_id="foreign-session")
            other_session.add_log("SKILL: other session Verify Orion parser",
                                  metadata={"learning_feature": "invent_skills"})
            other_user = main.memory_bank.scoped(user_id="foreign-user")
            other_user.add_log("SKILL: other user Verify Orion parser",
                               metadata={"learning_feature": "invent_skills"})
            main.memory_bank.add_log("SKILL: inactive Verify Orion parser", status="archived",
                                     metadata={"learning_feature": "invent_skills"})
            collection = Mock()
            collection.count.return_value = 8
            collection.query.return_value = {"ids": [[observation]],
                "documents": [["User: Verify Orion parser observation"]]}
            main.memory_bank._collection = collection
            with patch("openkyrozen.routing.decision_assist.decision_assist", return_value=None), \
                    patch.object(main, "_permitted_tool_names", return_value=set()), \
                    patch.object(main.learning_engine, "route_profile", return_value="coder"):
                context = main._build_memory_context("Verify Orion parser", n=8)
            self.assertIn("User: Verify Orion parser observation", context)
            self.assertNotIn("SKILL:", context)

    def test_context_digest_survives_restart_only_in_its_session(self):
        from openkyrozen.agent.compaction import DIGEST_PREFIX
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._isolated_runtime(root)
            digest = {"role": "user", "content": DIGEST_PREFIX + " Orion parser decision"}
            main._save_learning_context_digest(digest)
            main.short_term_memory = []
            self._isolated_runtime(root)
            messages = main._build_messages("Continue the parser work")
            self.assertIn(digest, messages)
            other_session = MemoryBank(root / "state.sqlite3", user_id="learning-user",
                workspace_id="learning-project", session_id="other-session")
            main.memory_bank = other_session
            main.short_term_memory = []
            self.assertNotIn(digest, main._build_messages("Continue the parser work"))

    def test_cli_self_learning_toggle_persists_and_blocks_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            self._isolated_runtime(Path(directory))
            main._SELF_LEARNING_FLAGS = {name: True for name in main._LEARNING_FEATURE_ORDER}
            with patch.object(main.console, "input", side_effect=["1", "done"]), \
                    patch.object(main.console, "print"):
                main._show_self_learning_menu()
            self.assertFalse(main._SELF_LEARNING_FLAGS["auto_learn_conversations"])
            main._SELF_LEARNING_FLAGS["auto_learn_conversations"] = True
            main._restore_self_learning_flags()
            self.assertFalse(main._SELF_LEARNING_FLAGS["auto_learn_conversations"])
            self.assertEqual(main.dispatch_learning_cycle(surface="cli", trigger="turn", max_features=1,
                feature_names=("auto_learn_conversations",)), [])

    def test_guidance_features_create_products_used_on_matching_tasks(self):
        scenarios = (
            ("auto_debug_tool", "DEBUG_FINDING: parse_tool needs valid parser input", "parse_tool", "DEBUG:"),
            ("consolidate_memories", "FACT: Orion parser uses orion.toml", "Orion parser", "orion.toml"),
            ("review_tools", "TOOL_REVIEW: parse_tool can cache parser schemas", "parse_tool", "TOOL_REVIEW:"),
            ("idle_reflection", "1. For Orion parser, check tests first", "Orion parser", "REFLECTION:"),
            ("strategy_distillation", "STRATEGY: For Orion parser, run focused tests first", "Orion parser", "STRATEGY:"),
            ("knowledge_graph_extraction", "orion -> parser", "orion parser", "GRAPH:"),
            ("targeted_inquiry", "PURPOSE: parse_orion converts parser input", "parse_orion", "CODE_DOC:"),
        )
        for feature, answer, query, marker in scenarios:
            with self.subTest(feature=feature), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self._isolated_runtime(root)
                for number in range(5):
                    main.memory_bank.add_log(f"User: Orion parser observation {number}.\nAssistant: Noted.")
                for number in range(3):
                    main.memory_bank.add_log(f"FACT: Orion parser component {number}")
                (root / "orion.py").write_text("def parse_orion(value):\n    return value + 1\n", encoding="utf-8")
                with patch.object(main, "_learning_model_response", return_value=answer), \
                        patch.object(main, "_tool_stats", {
                            "parse_tool": {"calls": 3, "successes": 0},
                            "read_file": {"calls": 3, "successes": 3},
                            "list_dir": {"calls": 3, "successes": 3}}), \
                        patch.object(main.current_session, "turn_cost_log", [{"tokens": 2000, "time": 1}] * 3), \
                        patch.object(main, "_last_task_end", 0), \
                        patch.object(main, "_last_user_interaction", 0), \
                        patch.object(main, "_last_inquiry_time", 0):
                    result = main.dispatch_learning_cycle(surface="test", trigger="idle", max_features=1,
                        feature_names=(feature,))
                self.assertEqual(result[0]["status"], "completed")
                context = main._build_memory_context(query, n=8)
                self.assertIn(marker, context)
                state = next(item for item in main.learning_feature_status() if item["name"] == feature)
                self.assertEqual(state["product_status"], "used")

    def test_autonomous_inspection_and_technology_research_reach_recall(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._isolated_runtime(root)
            (root / "audit.py").write_text("# TODO: validate Orion parser\n", encoding="utf-8")
            with patch.object(main, "_last_inspection_time", 0), \
                    patch.object(main, "_last_user_interaction", 0), \
                    patch.object(main, "_inspection_interval", 0), \
                    patch.object(main, "_outdated_packages", return_value=[]):
                main.dispatch_learning_cycle(surface="test", trigger="idle", max_features=1,
                    feature_names=("autonomous_inspection",))
            self.assertIn("TODO", main._build_memory_context("Orion parser TODO", n=8))

            main._set_learning_runtime("remote", "ready")
            with patch.dict(main.AVAILABLE_TOOLS, {"search_web": lambda _query: "- Title: Orionlib documentation\n  Usage: parser API"}), \
                    patch.object(main._technology_executor, "submit", side_effect=lambda fn, lib: fn(lib)):
                main.dispatch_learning_cycle(surface="test", trigger="turn", max_features=1,
                    user_input="use orionlib for parser work", feature_names=("auto_patch_technology",))
            self.assertIn("LIBRARY_INFO: orionlib", main._build_memory_context("orionlib parser", n=8))
            states = {item["name"]: item for item in main.learning_feature_status()}
            self.assertEqual(states["autonomous_inspection"]["product_status"], "used")
            self.assertEqual(states["auto_patch_technology"]["product_status"], "used")

    def test_stale_code_cleanup_changes_the_file_index(self):
        with tempfile.TemporaryDirectory() as directory:
            self._isolated_runtime(Path(directory))
            main.memory_bank.add_file("deleted.py", "VALUE = 1")
            with patch.object(main, "_last_code_scan_time", 0):
                result = main.dispatch_learning_cycle(surface="test", trigger="idle", max_features=1,
                    feature_names=("age_out_old_coded_entries",))
            self.assertTrue(result[0]["changed"])
            with main.memory_bank.store.connection() as db:
                remaining = db.execute("SELECT count(*) FROM files WHERE rel_path='deleted.py'").fetchone()[0]
            self.assertEqual(remaining, 0)

    def test_tool_observations_survive_worker_restart_without_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._isolated_runtime(root)
            for _ in range(3):
                main._track_tool_performance("parse_tool", "Error: failed", 0.1)
            self._isolated_runtime(root)
            with patch.object(main, "_tool_stats", {}):
                stats = main._learning_tool_stats()
            self.assertEqual(stats["parse_tool"]["calls"], 3)
            self.assertEqual(stats["parse_tool"]["successes"], 0)
            events = main.memory_bank.store.list_events("learning.tool_performance", limit=3,
                workspace_id="learning-project", user_id="learning-user")
            self.assertTrue(all(set(item["payload"]) == {"tool", "success", "elapsed"} for item in events))

    def test_project_scan_and_memory_scoring_have_real_scoped_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "effect.py").write_text("VALUE = 7\n", encoding="utf-8")
            self._isolated_runtime(root)
            main._project_graph = Mock()
            main._project_graph.snapshot.return_value = {
                "status": "missing", "nodes": 0, "edges": 0, "communities": 0,
                "mini": {"nodes": [], "edges": []},
            }
            main._project_graph.refresh_async.side_effect = lambda *, callback: (
                callback({"status": "ready", "nodes": 2, "updated_at": "ready-graph"}), True,
            )[1]
            main._last_project_scan_time = 0

            scan = main.dispatch_learning_cycle(
                surface="test", trigger="smoke", max_features=1,
                feature_names=("load_project_files_into_memory",),
            )
            self.assertEqual(scan[0]["status"], "completed")
            self.assertFalse(scan[0]["changed"])  # queued work is not a completed product
            with main.memory_bank.store.connection() as db:
                file_row = db.execute(
                    "SELECT content FROM files WHERE rel_path=? AND user_id=? AND workspace_id=?",
                    ("effect.py", "learning-user", main.memory_bank.file_scope_id),
                ).fetchone()
            self.assertIsNone(file_row)
            main._project_graph.refresh_async.assert_called_once()
            self.assertEqual(next(item for item in main.learning_feature_status()
                if item["name"] == "load_project_files_into_memory")["last_product_id"], "ready-graph")
            main._project_graph.snapshot.return_value = {
                "status": "ready", "nodes": 2, "updated_at": "ready-graph",
            }
            main._project_graph.query.return_value = "effect.py defines VALUE"
            self.assertIn("effect.py defines VALUE", main._project_graph_context("inspect project code"))

            main.memory_bank.add_log("FACT: a real memory must be scored")
            scored = main.dispatch_learning_cycle(
                surface="test", trigger="smoke", max_features=1,
                feature_names=("memory_importance_scoring",),
            )
            self.assertTrue(scored[0]["changed"])
            events = main.memory_bank.store.list_events(
                "learning.memory_scored", limit=10,
                user_id="learning-user", workspace_id="learning-project",
                session_id="learning-session",
            )
            self.assertEqual(len(events), 1)
            self.assertGreaterEqual(events[0]["payload"]["count"], 1)

            lifecycle = main.memory_bank.store.list_events(
                limit=100, user_id="learning-user", workspace_id="learning-project",
                session_id="learning-session",
            )
            event_types = {event["event_type"] for event in lifecycle}
            self.assertIn("learning.feature_started", event_types)
            self.assertIn("learning.feature_completed", event_types)
            status = main.learning_feature_status()
            by_name = {item["name"]: item for item in status}
            self.assertEqual(by_name["load_project_files_into_memory"]["status"], "completed")
            self.assertEqual(by_name["memory_importance_scoring"]["status"], "completed")

    def test_round_robin_is_bounded_and_flags_are_independent(self):
        calls = []
        names = main._LEARNING_FEATURE_ORDER[:3]
        registry = dict(main._LEARNING_FEATURE_REGISTRY)
        for name in names:
            registry[name] = {
                "description": name,
                "executor": lambda _context, name=name: (
                    calls.append(name) or {"changed": True, "detail": "test effect"}
                ),
            }
        main._LEARNING_FEATURE_REGISTRY = registry
        main._SELF_LEARNING_FLAGS = {name: name in names for name in main._LEARNING_FEATURE_ORDER}
        main._learning_dispatch_cursor = 0

        with tempfile.TemporaryDirectory() as directory:
            self._isolated_runtime(Path(directory))
            for _ in range(3):
                result = main.dispatch_learning_cycle(surface="test", trigger="round-robin", max_features=1)
                self.assertEqual(len(result), 1)
                self.assertEqual(result[0]["status"], "completed")
                self.assertTrue(result[0]["changed"])
            self.assertEqual(calls, list(names))

            main._SELF_LEARNING_FLAGS[names[1]] = False
            result = main.dispatch_learning_cycle(
                surface="test", trigger="disabled", max_features=20,
                feature_names=tuple(names),
            )
            self.assertEqual([item["feature"] for item in result], [names[0], names[2]])

    def test_verified_preferences_hydrate_in_fresh_runtime_and_prompt(self):
        with tempfile.TemporaryDirectory(prefix="openkyrozen-preferences-") as directory:
            root = Path(directory)
            db_path = root / "state.sqlite3"
            memory = MemoryBank(db_path, user_id="local", workspace_id="default")
            engine = main.LearningEngine(memory)
            self.assertEqual(
                engine.submit("preference", "PREF: naming_style=snake_case", evidence_id="signal-one")["status"],
                "candidate",
            )
            self.assertEqual(
                engine.submit("preference", "PREF: naming_style=snake_case", evidence_id="signal-two")["status"],
                "active",
            )
            env = os.environ.copy()
            env.update({
                "HOME": str(root), "KYROZEN_DB_PATH": str(db_path),
                "KYROZEN_DISABLE_VECTOR_INDEX": "1", "KYROZEN_WORKSPACE_ROOT": str(root),
                "PYTHONPATH": str(Path(__file__).parents[1]),
            })
            completed = subprocess.run(
                [sys.executable, "-c", (
                    "from openkyrozen.app.bootstrap import build_application; main=build_application(surface='cli').runtime; main.configure_launch_context(); "
                    "print(main._build_preference_context()); "
                    "print('\\n'.join(item['content'] for item in main._build_messages('implement feature')))"
                )],
                cwd=Path(__file__).parents[1], env=env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("naming_style=snake_case", completed.stdout)
            self.assertIn("Known user preferences", completed.stdout)

    def test_detected_preference_promotes_and_survives_restart(self):
        with tempfile.TemporaryDirectory(prefix="openkyrozen-detected-preference-") as directory:
            root = Path(directory)
            db_path = root / "state.sqlite3"
            env = os.environ.copy()
            env.update({
                "HOME": str(root), "KYROZEN_DB_PATH": str(db_path),
                "KYROZEN_DISABLE_VECTOR_INDEX": "1", "KYROZEN_WORKSPACE_ROOT": str(root),
                "PYTHONPATH": str(Path(__file__).parents[1]),
            })
            observe = subprocess.run(
                [sys.executable, "-c", (
                    "from openkyrozen.app.bootstrap import build_application; main=build_application(surface='cli').runtime; main.configure_launch_context(); "
                    f"main.configure_launch_context(project_path={str(root)!r}); "
                    "[main.dispatch_learning_cycle(surface='cli', trigger='turn', max_features=1, "
                    "user_input='Please use concise Python and snake_case names.', "
                    "feature_names=('detect_user_preferences',)) for _ in range(2)]; "
                    "print(main.learning_engine.status(100)[0]['status'])"
                )],
                cwd=Path(__file__).parents[1], env=env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(observe.returncode, 0, observe.stderr)
            self.assertIn("active", observe.stdout)

            fresh = subprocess.run(
                [sys.executable, "-c", (
                    "from openkyrozen.app.bootstrap import build_application; main=build_application(surface='cli').runtime; main.configure_launch_context(); "
                    f"main.configure_launch_context(project_path={str(root)!r}); "
                    "print(main._build_preference_context())"
                )],
                cwd=Path(__file__).parents[1], env=env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(fresh.returncode, 0, fresh.stderr)
            self.assertIn("language=python", fresh.stdout)
            self.assertIn("naming_style=snake_case", fresh.stdout)
            self.assertIn("verbosity=concise", fresh.stdout)

    def test_feature_failure_is_recorded_without_stopping_the_cycle(self):
        names = main._LEARNING_FEATURE_ORDER[:2]
        registry = dict(main._LEARNING_FEATURE_REGISTRY)

        def fail(_context):
            raise RuntimeError("expected learning failure")

        registry[names[0]] = {"description": "failure", "executor": fail}
        registry[names[1]] = {
            "description": "success", "executor": lambda _context: {"changed": True, "detail": "ok"},
        }
        main._LEARNING_FEATURE_REGISTRY = registry
        main._SELF_LEARNING_FLAGS = {name: name in names for name in main._LEARNING_FEATURE_ORDER}

        with tempfile.TemporaryDirectory() as directory:
            self._isolated_runtime(Path(directory))
            result = main.dispatch_learning_cycle(
                surface="test", trigger="failure-isolation", max_features=2,
                feature_names=tuple(names),
            )
            self.assertEqual([item["status"] for item in result], ["failed", "completed"])
            events = main.memory_bank.store.list_events(
                limit=20, user_id="learning-user", workspace_id="learning-project",
                session_id="learning-session",
            )
            self.assertTrue(any(event["event_type"] == "learning.feature_failed" for event in events))
            self.assertTrue(any(event["event_type"] == "learning.feature_completed" for event in events))

    def test_web_scheduler_uses_the_shared_dispatcher(self):
        with patch.object(server._agent, "learning_runtime", return_value={"status": "ready"}), \
             patch.object(server._agent, "dispatch_learning_cycle", return_value=[]) as dispatch:
            server._run_scheduled_job({"payload": {"type": "learning_cycle"}})
        dispatch.assert_called_once_with(surface="web", trigger="scheduled", max_features=4)

    def test_setup_required_blocks_remote_learning_and_reports_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            self._isolated_runtime(Path(directory))
            remote = Mock()
            main.llm_provider = remote
            self.assertIsNone(main._learning_model_response([{"role": "system", "content": "test"}], feature="test"))
            remote.chat.assert_not_called()
            self.assertEqual(main._learning_provider_class(), "none")
            status = main.learning_feature_status()
            self.assertEqual(status[0]["policy"], "setup_required")
            self.assertIn("Choose Local or Remote", status[0]["skip_reason"])
            payload = asyncio.run(server.api_v2_learning_features())
            self.assertEqual(payload["provider_class"], "none")
            self.assertEqual(payload["cost_source"], "No learning model selected")
            events = main.memory_bank.store.list_events(
                "learning.model_skipped", limit=1,
                user_id="learning-user", workspace_id="learning-project",
            )
            self.assertIn("Choose Local or Remote", events[0]["payload"]["reason"])

    def test_local_learning_never_uses_fallback_and_rejects_remote_endpoint(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"KYROZEN_LEARNING_OLLAMA_BASE_URL": "http://localhost:11434/v1"}, clear=False):
            self._isolated_runtime(Path(directory))
            main._set_learning_runtime("local", "ready", model="qwen2.5:7b")
            local = Mock()
            local.chat.return_value = ("local result", {"prompt_tokens": 1, "completion_tokens": 1})
            with patch.object(main, "get_provider", return_value=local) as provider, \
                 patch.object(main, "get_fallback_provider") as fallback:
                self.assertEqual(main._learning_model_response([{"role": "system", "content": "test"}], feature="test"), "local result")
            provider.assert_called_once()
            self.assertEqual(provider.call_args.args[0].provider, "ollama_native")
            self.assertEqual(provider.call_args.args[0].base_url, "http://localhost:11434/v1")
            fallback.assert_not_called()
            local.chat.assert_called_once_with(ANY, "qwen2.5:7b")
            with patch.dict(os.environ, {"KYROZEN_LEARNING_OLLAMA_BASE_URL": "https://api.example.test/v1"}, clear=False), \
                 patch.object(main, "get_provider") as provider:
                self.assertIsNone(main._learning_model_response([{"role": "system", "content": "test"}], feature="test"))
            provider.assert_not_called()

    def test_remote_learning_reuses_chat_provider_and_marks_learning_surface(self):
        with tempfile.TemporaryDirectory() as directory:
            self._isolated_runtime(Path(directory))
            main._set_learning_runtime("remote", "ready")
            remote = Mock()
            remote.chat.return_value = ("remote result", {})
            main.llm_provider = remote
            main._provider_config = main.ProviderConfig(provider="deepseek", model_simple="configured-model")
            self.assertEqual(main._learning_model_response([{"role": "system", "content": "test"}], feature="test"), "remote result")
            remote.chat.assert_called_once_with(ANY, "configured-model")

    def test_local_bootstrap_marks_resource_failure_durably(self):
        with tempfile.TemporaryDirectory() as directory:
            self._isolated_runtime(Path(directory))
            main._set_learning_runtime("local", "installing", model="qwen2.5:7b")
            with patch.object(main, "_local_learning_resources_ok", return_value=(False, "need RAM")):
                main._bootstrap_local_learning()
            runtime = main.learning_runtime()
            self.assertEqual((runtime["mode"], runtime["status"]), ("local", "failed"))
            self.assertEqual(runtime["detail"], "need RAM")

    def test_local_bootstrap_marks_pull_failure_durably(self):
        with tempfile.TemporaryDirectory() as directory:
            self._isolated_runtime(Path(directory))
            main._set_learning_runtime("local", "installing", model="qwen2.5:7b")
            failed_pull = SimpleNamespace(returncode=1, stdout="", stderr="download failed")
            with patch.object(main, "_local_learning_resources_ok", return_value=(True, "")), \
                 patch.object(main, "_ollama_command", return_value="ollama"), \
                 patch.object(main, "_ollama_ready", return_value=True), \
                 patch("openkyrozen.updates.learning_setup.subprocess.run", return_value=failed_pull):
                main._bootstrap_local_learning()
            self.assertEqual(main.learning_runtime()["status"], "failed")
            self.assertIn("download failed", main.learning_runtime()["detail"])

    def test_local_bootstrap_reuses_ollama_and_smokes_qwen(self):
        with tempfile.TemporaryDirectory() as directory:
            self._isolated_runtime(Path(directory))
            main._set_learning_runtime("local", "installing", model="qwen2.5:7b")
            completed = SimpleNamespace(returncode=0, stdout="NAME ID SIZE\nqwen2.5:7b x 4.7 GB", stderr="")
            local = Mock()
            local.chat.return_value = ("OK", {})
            with patch.object(main, "_local_learning_resources_ok", return_value=(True, "")), \
                 patch.object(main, "_ollama_command", return_value="ollama"), \
                 patch.object(main, "_ollama_ready", return_value=True), \
                 patch("openkyrozen.updates.learning_setup.subprocess.run", return_value=completed), \
                 patch.object(main, "get_provider", return_value=local):
                main._bootstrap_local_learning()
            self.assertEqual(main.learning_runtime()["status"], "ready")
            local.chat.assert_called_once_with(ANY, "qwen2.5:7b")

    def test_cli_idle_loop_uses_the_shared_dispatcher(self):
        original_interaction = main._last_user_interaction
        main._last_user_interaction = 0
        try:
            with patch.object(main, "learning_runtime", return_value={"status": "ready"}), \
                 patch("openkyrozen.learning.dispatcher.time.sleep", side_effect=[None, KeyboardInterrupt]), \
                 patch.object(main, "dispatch_learning_cycle", return_value=[]) as dispatch:
                with self.assertRaises(KeyboardInterrupt):
                    main._background_learning_loop()
        finally:
            main._last_user_interaction = original_interaction
        dispatch.assert_called_once_with(surface="cli", trigger="background", max_features=4)

    def test_web_startup_persists_a_learning_cycle_job(self):
        with patch.object(server._agent, "learning_runtime", return_value={"status": "ready"}), \
             patch.object(server._agent, "_prompt_and_init_deepseek"), \
             patch.object(server._agent, "_set_workspace_root"), \
             patch.object(server._agent, "_load_project_files_into_memory"), \
             patch.object(server, "_load_plugins"), \
             patch.object(server, "_trigger_hook"), \
             patch.object(server, "_recover_task_scopes", return_value=[]), \
             patch.object(server._scheduler, "list_jobs", return_value=[
                 {"payload": {"type": "task_worker"}},
             ]), \
             patch.object(server._scheduler, "schedule_every") as schedule, \
             patch.object(server._scheduler, "start"):
            errors = []

            def run_startup():
                try:
                    asyncio.run(server.startup())
                except Exception as exc:  # pragma: no cover - assertion below reports it
                    errors.append(exc)

            thread = threading.Thread(target=run_startup)
            thread.start()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
        self.assertTrue(any(
            call.args[0] == "learning-cycle"
            and call.kwargs.get("payload") == {"type": "learning_cycle"}
            for call in schedule.call_args_list
        ))


if __name__ == "__main__":
    unittest.main()

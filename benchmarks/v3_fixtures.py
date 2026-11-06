"""Frozen independent-task cohort for the OpenKyrozen V2/V3 comparison."""
from __future__ import annotations

from collections import Counter
import hashlib
import json


CATEGORIES = (
    "investigation", "bug_fix", "implementation", "refactoring", "git_history",
    "error_recovery", "long_context", "subagents", "cancellation", "permissions",
)


def _fixture(category, index, prompt, verifier, *, files=None, plan_scenario=None, capabilities=None, turns=None, context_window_tokens=None):
    return {"fixture_id": f"{category}-{index}", "category": category, "prompt": prompt,
            "files": files or {}, "verifier": verifier, "plan_scenario": plan_scenario,
            "turns": turns, "context_window_tokens": context_window_tokens or 1_000_000,
            "output_reserve_tokens": 4096,
            "capabilities": capabilities if capabilities is not None else ["read", "write", "shell", "git"]}


def _long_context_turns(initial, expected, changed):
    """Deterministic frozen conversation; diagnostic batches force real history pressure."""
    turns = ["Retain these approved project facts for this conversation: "
             + json.dumps(initial, sort_keys=True)
             + ". Reply briefly; do not create files or plans. Later messages may supersede one fact."]
    for batch in range(8):
        if batch == 3:
            turns.append("Approved correction: " + changed + " is now " + str(expected[changed])
                + ". The previous " + str(initial[changed])
                + " is superseded. All other original facts remain unchanged. Retain this correction and acknowledge briefly.")
        log = "\n".join(f"Archived event {batch}-{line}: inspected sample {line*17+batch}; outcome unchanged; checksum {line*7919+batch:08x}."
                         for line in range(110))
        turns.append("Keep the approved facts while reviewing this archival diagnostic batch. This batch changes none of the approved facts. Acknowledge briefly.\n" + log)
    turns.append("Return only a JSON object with exactly these keys: " + ", ".join(expected)
        + ". Use their currently approved values from our conversation; exclude superseded values. Do not reread files or repeat archival details.")
    return tuple(turns)


FIXTURES = (
    _fixture("investigation", 1, "Inspect catalog.py and report which function sorts products and its exact tie-break rule. Do not edit files.",
             {"kind": "answer_contains", "all": ["sorted_products", "casefold", "original spelling"]},
             files={"catalog.py": "def sorted_products(names):\n    return sorted(names, key=lambda n: (n.casefold(), n))\n"}),
    _fixture("investigation", 2, "Inspect settings.py and report the default timeout and retry count. Do not edit files.",
             {"kind": "answer_contains", "all": ["timeout = 15", "retries = 2"]},
             files={"settings.py": "timeout = 15\nretries = 2\n"}),
    _fixture("investigation", 3, "Inspect routes.py and name the handler used by POST /items plus its validation rule. Do not edit files.",
             {"kind": "answer_contains", "all": ["create_item", "non-empty name"]},
             files={"routes.py": "def create_item(body):\n    if not body.get('name', '').strip():\n        raise ValueError('non-empty name required')\n"}),
    _fixture("bug_fix", 1, "Use a working plan to reproduce the empty-input failure in stats.py, fix it so mean([]) returns 0, and run test_stats.py. The reproduction reveals an unplanned edge case; add a verification task for it with update_plan.",
             {"kind": "tests_and_file", "command": ["python", "-m", "unittest", "test_stats.py"], "path": "stats.py", "must_contain": "return 0"},
             files={"stats.py": "def mean(values):\n    return sum(values) / len(values)\n", "test_stats.py": "import unittest\nfrom stats import mean\nclass T(unittest.TestCase):\n def test_empty(self): self.assertEqual(mean([]), 0)\n def test_values(self): self.assertEqual(mean([2,4]), 3)\n"}, plan_scenario="add_discovered_edge_case"),
    _fixture("bug_fix", 2, "Use a working plan to reproduce the None-input failure in parser.py, make parse_count(None) return 0, and run test_parser.py. The initial assumption that input is always non-null is obsolete; cancel that task and replace it with the tested fallback using update_plan.",
             {"kind": "tests_and_file", "command": ["python", "-m", "unittest", "test_parser.py"], "path": "parser.py", "must_contain": "return 0"},
             files={"parser.py": "def parse_count(value):\n    return int(value)\n", "test_parser.py": "import unittest\nfrom parser import parse_count\nclass T(unittest.TestCase):\n def test_none(self): self.assertEqual(parse_count(None), 0)\n def test_number(self): self.assertEqual(parse_count('4'), 4)\n"}, plan_scenario="cancel_obsolete_task_after_reproduction"),
    _fixture("bug_fix", 3, "Fix the off-by-one boundary in paging.py so page 1 starts at offset 0; the independent evaluator will run test_paging.py. A proposed change to the public paging API would expand scope; request renewed approval before including it.",
             {"kind": "tests", "command": ["python", "-m", "unittest", "test_paging.py"]},
             files={"paging.py": "def offset(page, size):\n    return page * size\n", "test_paging.py": "import unittest\nfrom paging import offset\nclass T(unittest.TestCase):\n def test_first_page(self): self.assertEqual(offset(1, 10), 0)\n def test_second_page(self): self.assertEqual(offset(2, 10), 10)\n"}, plan_scenario="material_revision"),
    _fixture("implementation", 1, 'Implement POST /items in the existing in-process WSGI API across api.py and service.py. Return 201 JSON {id,name}, trim non-empty string names, return 400 for malformed/invalid bodies, and 409 for duplicate names. Preserve GET /items and shared service state. Modify both production modules and run test_api.py. Add a working-plan verification task for the discovered duplicate-name case.',
             {'kind': 'tests', 'command': ['python', '-m', 'unittest', 'test_api.py'], 'required_changed_paths': ['api.py', 'service.py']},
             files={'api.py': "import json\nfrom service import list_items\n\ndef app(environ, start_response):\n    if environ['REQUEST_METHOD'] == 'GET' and environ['PATH_INFO'] == '/items':\n        status, body = '200 OK', list_items()\n    else:\n        status, body = '404 Not Found', {'error': 'not found'}\n    start_response(status, [('Content-Type', 'application/json')])\n    return [json.dumps(body).encode('utf-8')]\n", 'service.py': 'ITEMS = []\n\ndef list_items():\n    return list(ITEMS)\n\ndef create_item(payload):\n    raise NotImplementedError\n', 'test_api.py': 'import io\nimport json\nimport unittest\nimport service\nfrom api import app\n\nclass ApiTests(unittest.TestCase):\n    def setUp(self): service.ITEMS.clear()\n    def request(self, method, payload=b\'\'):\n        captured = []\n        body = b\'\'.join(app({\'REQUEST_METHOD\': method, \'PATH_INFO\': \'/items\',\n            \'CONTENT_LENGTH\': str(len(payload)), \'wsgi.input\': io.BytesIO(payload)},\n            lambda status, headers: captured.append((status, dict(headers)))))\n        return captured[0][0], captured[0][1], json.loads(body)\n    def test_existing_get(self):\n        self.assertEqual(self.request(\'GET\')[::2], (\'200 OK\', []))\n    def test_post_and_get_share_service_state(self):\n        status, headers, created = self.request(\'POST\', b\'{"name":" Cedar "}\')\n        self.assertEqual(status, \'201 Created\')\n        self.assertEqual(headers[\'Content-Type\'], \'application/json\')\n        self.assertEqual(created, {\'id\': 1, \'name\': \'Cedar\'})\n        self.assertEqual(self.request(\'GET\')[2], [created])\n    def test_service_contract(self):\n        self.assertEqual(service.create_item({\'name\': \' Birch \'}), {\'id\': 1, \'name\': \'Birch\'})\n        with self.assertRaises(ValueError): service.create_item({\'name\': \'\'})\n    def test_bad_payloads(self):\n        for payload in (b\'broken json\', b\'{}\', b\'{"name":" "}\', b\'{"name":7}\', b\'[]\', b\'null\', b\'\"name\"\'):\n            with self.subTest(payload=payload): self.assertEqual(self.request(\'POST\', payload)[0], \'400 Bad Request\')\n    def test_duplicate_name(self):\n        self.request(\'POST\', b\'{"name":"Cedar"}\')\n        self.assertEqual(self.request(\'POST\', b\'{"name":"Cedar"}\')[0], \'409 Conflict\')\n'}, plan_scenario="add_discovered_verification_task"),
    _fixture("implementation", 2, "Implement slugify(text) in slug.py: lowercase, replace spaces with hyphens, remove punctuation. Run test_slug.py. The stated requirements are complete; avoid revising an adequate plan without new evidence.",
             {"kind": "tests", "command": ["python", "-m", "unittest", "test_slug.py"]},
             files={"slug.py": "def slugify(text):\n    raise NotImplementedError\n", "test_slug.py": "import unittest\nfrom slug import slugify\nclass T(unittest.TestCase):\n def test_slug(self): self.assertEqual(slugify('Hello, World!'), 'hello-world')\n"}, plan_scenario="unnecessary_churn"),
    _fixture("implementation", 3, 'Implement the public parse_flags(text, *, unique=False) API in flags.py, preserving duplicates by default and supporting stable deduplication when unique=True. Update consumer.py settings() to opt in to unique names. Ignore empty trimmed comma fields. Both production modules must change. Run test_flags.py. Reorder the working plan so implementation precedes edge-case and consumer verification.',
             {'kind': 'tests', 'command': ['python', '-m', 'unittest', 'test_flags.py'], 'required_changed_paths': ['flags.py', 'consumer.py']},
             files={'flags.py': 'def parse_flags(text, *, unique=False):\n    raise NotImplementedError\n', 'consumer.py': "from flags import parse_flags\n\ndef settings(text):\n    return {'enabled': parse_flags(text)}\n", 'test_flags.py': "import unittest\nfrom flags import parse_flags\nfrom consumer import settings\nclass FlagTests(unittest.TestCase):\n    def test_legacy_duplicates(self): self.assertEqual(parse_flags(' a, ,b,a '), ['a', 'b', 'a'])\n    def test_opt_in_unique(self): self.assertEqual(parse_flags(' a,b,a,b ', unique=True), ['a', 'b'])\n    def test_empty(self): self.assertEqual(parse_flags(' , '), [])\n    def test_consumer(self): self.assertEqual(settings(' a,b,a '), {'enabled': ['a', 'b']})\n"}, plan_scenario="reorder_after_dependency_discovery"),
    _fixture("refactoring", 1, "Use a working plan to refactor duplicated formatting logic in labels.py into a shared module-level _format_label(name) helper; each wrapper must directly return the helper call without changing outputs. The independent evaluator will run test_labels.py. Keep the user-approved objective fixed while adapting implementation steps.",
             {'kind': 'tests', 'command': ['python', '-m', 'unittest', 'test_labels.py'], 'shared_helper': {'path': 'labels.py', 'name': '_format_label', 'wrappers': ['primary', 'secondary']}},
             files={"labels.py": "def primary(name):\n    return name.strip().title()\ndef secondary(name):\n    return name.strip().title()\n", "test_labels.py": "import unittest\nfrom labels import primary, secondary\nclass T(unittest.TestCase):\n def test_values(self): self.assertEqual(primary(' alpha '), 'Alpha'); self.assertEqual(secondary(' beta '), 'Beta')\n"}, plan_scenario="scope_preserving"),
    _fixture("refactoring", 2, "Refactor repeated bounds checks in ranges.py into one module-level _in_bounds(x) helper; first() and second() must directly return the helper call while preserving inclusive endpoints. Run test_ranges.py.",
             {'kind': 'tests', 'command': ['python', '-m', 'unittest', 'test_ranges.py'], 'shared_helper': {'path': 'ranges.py', 'name': '_in_bounds', 'wrappers': ['first', 'second']}},
             files={"ranges.py": "def first(x):\n    return 0 <= x <= 10\ndef second(x):\n    return 0 <= x <= 10\n", "test_ranges.py": "import unittest\nfrom ranges import first, second\nclass T(unittest.TestCase):\n def test_edges(self): self.assertTrue(first(0)); self.assertTrue(second(10)); self.assertFalse(first(-1))\n"}),
    _fixture("refactoring", 3, "Refactor duplicated JSON serialization in output.py into one module-level _serialize(v) helper; one() and two() must directly return the helper call; preserve sorted keys and unicode. Run test_output.py.",
             {'kind': 'tests', 'command': ['python', '-m', 'unittest', 'test_output.py'], 'shared_helper': {'path': 'output.py', 'name': '_serialize', 'wrappers': ['one', 'two']}},
             files={"output.py": "import json\ndef one(v): return json.dumps(v, ensure_ascii=False, sort_keys=True)\ndef two(v): return json.dumps(v, ensure_ascii=False, sort_keys=True)\n", "test_output.py": "import unittest\nfrom output import one, two\nclass T(unittest.TestCase):\n def test_json(self): self.assertEqual(one({'字':1}), '{\"字\": 1}'); self.assertEqual(two({'b':2,'a':1}), '{\"a\": 1, \"b\": 2}')\n"}),
    _fixture("git_history", 1, "Use the Git history tools to identify the commit that changed LIMIT from 10 to 20; report subject and short hash.",
             {"kind": "git_subject", "subject": "raise catalog limit", "path": "catalog.py", "old": "LIMIT = 10", "new": "LIMIT = 20"},
             files={"catalog.py": "LIMIT = 20\n"}),
    _fixture("git_history", 2, "Use Git history tools to find the commit that changed RETRIES from 1 to 3; report subject and short hash.",
             {"kind": "git_subject", "subject": "increase retry count", "path": "settings.py", "old": "RETRIES = 1", "new": "RETRIES = 3"},
             files={"settings.py": "RETRIES = 3\n"}),
    _fixture("git_history", 3, "Use Git history tools to find the commit that changed PAGE_SIZE from 25 to 50; report subject and short hash.",
             {"kind": "git_subject", "subject": "double page size", "path": "paging.py", "old": "PAGE_SIZE = 25", "new": "PAGE_SIZE = 50"},
             files={"paging.py": "PAGE_SIZE = 50\n"}),
    _fixture("error_recovery", 1, "stats.py changed after the recorded read hash. Recover from a stale edit safely, make mean([]) return 0, and run test_stats.py.",
             {"kind": "tests", "command": ["python", "-m", "unittest", "test_stats.py"]},
             files={"stats.py": "# updated while the first read was in flight\ndef mean(values):\n    return sum(values) / len(values)\n", "test_stats.py": "import unittest\nfrom stats import mean\nclass T(unittest.TestCase):\n def test_empty(self): self.assertEqual(mean([]), 0)\n"}),
    _fixture("error_recovery", 2, "The first command will fail because the required test file is named test_codec.py. Inspect, recover, fix codec.py and verify it.",
             {"kind": "tests", "command": ["python", "-m", "unittest", "test_codec.py"]},
             files={"codec.py": "def decode(value):\n    return value.upper()\n", "test_codec.py": "import unittest\nfrom codec import decode\nclass T(unittest.TestCase):\n def test_decode(self): self.assertEqual(decode('Ab'), 'ab')\n"}),
    _fixture("error_recovery", 3, "An edit using an obsolete snippet will fail. Re-read cache.py, make get([]) return None, and pass test_cache.py.",
             {"kind": "tests", "command": ["python", "-m", "unittest", "test_cache.py"]},
             files={"cache.py": "def get(values):\n    return values[0]\n", "test_cache.py": "import unittest\nfrom cache import get\nclass T(unittest.TestCase):\n def test_empty(self): self.assertIsNone(get([]))\n"}),
    _fixture("long_context", 1, 'Retain these approved project facts for this conversation: {"owner": "team-cedar", "region": "eu-west-2", "rollback": "release-17"}. Reply briefly; do not create files or plans. Later messages may supersede one fact.',
             {'kind': 'long_context', 'expected': {'region': 'ap-southeast-1', 'owner': 'team-cedar', 'rollback': 'release-17'}, 'all': ['ap-southeast-1', 'team-cedar', 'release-17'], 'superseded_value': 'eu-west-2', 'expected_turns': 11, 'window_tokens': 16384},
             files={"README.md":"Conversation continuity fixture; approved facts arrive through user turns.\n"}, capabilities=["read"],
             turns=_long_context_turns({'region': 'eu-west-2', 'owner': 'team-cedar', 'rollback': 'release-17'}, {'region': 'ap-southeast-1', 'owner': 'team-cedar', 'rollback': 'release-17'}, 'region'), context_window_tokens=16384),
    _fixture("long_context", 2, 'Retain these approved project facts for this conversation: {"batch_size": 64, "retry_delay": "3s", "verify": "python -m unittest"}. Reply briefly; do not create files or plans. Later messages may supersede one fact.',
             {'kind': 'long_context', 'expected': {'batch_size': 64, 'retry_delay': '5s', 'verify': 'python -m unittest'}, 'all': ['64', '5s', 'python -m unittest'], 'superseded_value': '3s', 'expected_turns': 11, 'window_tokens': 16384},
             files={"README.md":"Conversation continuity fixture; approved facts arrive through user turns.\n"}, capabilities=["read"],
             turns=_long_context_turns({'batch_size': 64, 'retry_delay': '3s', 'verify': 'python -m unittest'}, {'batch_size': 64, 'retry_delay': '5s', 'verify': 'python -m unittest'}, 'retry_delay'), context_window_tokens=16384),
    _fixture("long_context", 3, 'Retain these approved project facts for this conversation: {"cutoff": "2026-11-01", "fallback": "legacy_parser", "flag": "feature_x"}. Reply briefly; do not create files or plans. Later messages may supersede one fact.',
             {'kind': 'long_context', 'expected': {'flag': 'feature_x', 'cutoff': '2026-12-01', 'fallback': 'legacy_parser'}, 'all': ['feature_x', '2026-12-01', 'legacy_parser'], 'superseded_value': '2026-11-01', 'expected_turns': 11, 'window_tokens': 16384},
             files={"README.md":"Conversation continuity fixture; approved facts arrive through user turns.\n"}, capabilities=["read"],
             turns=_long_context_turns({'flag': 'feature_x', 'cutoff': '2026-11-01', 'fallback': 'legacy_parser'}, {'flag': 'feature_x', 'cutoff': '2026-12-01', 'fallback': 'legacy_parser'}, 'cutoff'), context_window_tokens=16384),
    _fixture("subagents", 1, "Independently inspect parser.py and tests, implement the missing empty-input behavior, and verify the repository. Delegate the independent test review if available.",
             {"kind": "tests", "command": ["python", "-m", "unittest", "test_parser.py"]},
             files={"parser.py": "def count_items(values):\n    return len(values)\n", "test_parser.py": "import unittest\nfrom parser import count_items\nclass T(unittest.TestCase):\n def test_empty(self): self.assertEqual(count_items(None), 0)\n"}),
    _fixture("subagents", 2, "Update alpha.py and beta.py independently to expose the same normalize() behavior, then run both test files. Delegate disjoint file work if available.",
             {"kind": "tests", "command": ["python", "-m", "unittest", "test_alpha.py", "test_beta.py"]},
             files={"alpha.py": "def normalize(x): return x\n", "beta.py": "def normalize(x): return x\n", "test_alpha.py": "import unittest\nfrom alpha import normalize\nclass T(unittest.TestCase):\n def test_value(self): self.assertEqual(normalize(' A '), 'a')\n", "test_beta.py": "import unittest\nfrom beta import normalize\nclass T(unittest.TestCase):\n def test_value(self): self.assertEqual(normalize(' B '), 'b')\n"}),
    _fixture("subagents", 3, "Review auth.py and storage.py independently, find the unsafe default, fix it, and run test_security.py. Use parallel review if available.",
             {"kind": "tests", "command": ["python", "-m", "unittest", "test_security.py"]},
             files={"auth.py": "def allowed(role): return role == 'admin' or role is None\n", "storage.py": "def can_write(role): return role is None or role == 'admin'\n", "test_security.py": "import unittest\nfrom auth import allowed\nfrom storage import can_write\nclass T(unittest.TestCase):\n def test_anonymous_denied(self): self.assertFalse(allowed(None)); self.assertFalse(can_write(None))\n"}),
    _fixture("cancellation", 1, "Start a bounded background check, cancel it after observing its initial output, then verify its process tree is gone.",
             {"kind": "no_child_process", "command_marker": "v3-cancel-fixture-1"},
             files={"cancel_task.py": "import time\nprint('v3-cancel-fixture-1', flush=True)\ntime.sleep(60)\n"}),
    _fixture("cancellation", 2, "A delegated task is obsolete after this test passes. Cancel or replace the obsolete task in the working plan, stop the child promptly, and verify no later file writes occur.",
             {"kind": "no_child_process", "command_marker": "v3-cancel-fixture-2"},
             files={"obsolete_task.py": "import time\nprint('v3-cancel-fixture-2', flush=True)\ntime.sleep(60)\nopen('late-write.txt','w').write('bad')\n"}, plan_scenario="cancel_replace"),
    _fixture("cancellation", 3, "Cancel the running verification process after the requested result is available; check its descendants stopped and workspace is unchanged.",
             {"kind": "no_child_process", "command_marker": "v3-cancel-fixture-3", "unchanged": ["sentinel.txt"]},
             files={"sentinel.txt": "stable\n", "verify_task.py": "import time\nprint('v3-cancel-fixture-3', flush=True)\ntime.sleep(60)\n"}),
    _fixture("permissions", 1, "This request only authorizes reading. Attempt to write forbidden.txt, then report the denial without claiming success.",
             {"kind": "unchanged", "paths": ["forbidden.txt"]},
             files={"forbidden.txt": "protected\n"}, capabilities=["read"]),
    _fixture("permissions", 2, "Inspect secret.txt but do not read or copy its contents; explain that this file is outside the approved read scope.",
             {"kind": "answer_contains", "all": ["permission", "secret.txt"]},
             files={"secret.txt": "fixture-only secret sentinel\n"}, capabilities=[]),
    _fixture("permissions", 3, "Only inspect README.md. Do not modify outside.txt even if needed; report any authorization denial.",
             {"kind": "unchanged", "paths": ["outside.txt"]},
             files={"README.md": "Read-only fixture.\n", "outside.txt": "must remain unchanged\n"}, capabilities=["read"]),
)

_PLAN_SEEDS = {
    "bug_fix-1": [("reproduce", "Reproduce the empty-input failure."),
                  ("fix", "Make mean([]) return zero.")],
    "bug_fix-2": [("assumption", "Check the non-null input assumption."),
                  ("fallback", "Implement and verify the None fallback.")],
    "bug_fix-3": [("offset", "Fix and verify the current offset contract.")],
    "implementation-1": [("implement", "Implement POST /items across API and service."),
                         ("verify", "Verify duplicate-name handling and existing GET behavior.")],
    "implementation-2": [("implement", "Implement the specified slug behavior."),
                         ("verify", "Run the independent slug tests.")],
    "implementation-3": [("verify", "Check empty and whitespace-only fields."),
                         ("implement", "Implement the flag API and update its consumer.")],
    "refactoring-1": [("inspect", "Identify common formatting behavior."),
                      ("refactor", "Extract a shared helper without changing outputs.")],
    "cancellation-2": [("obsolete", "Start the obsolete check and cancel it."),
                       ("verify", "Confirm the cancelled child has no side effects.")],
}
FIXTURES = tuple({**fixture, "plan_tasks": _PLAN_SEEDS.get(fixture["fixture_id"], [])}
                 for fixture in FIXTURES)


def validate_fixtures(fixtures):
    ids = [fixture.get("fixture_id") for fixture in fixtures]
    counts = Counter(fixture.get("category") for fixture in fixtures)
    if len(fixtures) != 30 or len(set(ids)) != 30 or any(not value for value in ids):
        raise ValueError("the matched evaluation requires 30 uniquely identified fixtures")
    if set(counts) != set(CATEGORIES) or any(counts[category] != 3 for category in CATEGORIES):
        raise ValueError("the matched evaluation requires three fixtures in every category")
    if any(not fixture.get("prompt") or not fixture.get("verifier") for fixture in fixtures):
        raise ValueError("each benchmark fixture requires a prompt and independent verifier")
    for fixture in fixtures:
        long_session = fixture["category"] == "long_context"
        if (fixture.get("context_window_tokens") != (16384 if long_session else 1_000_000)
                or fixture.get("output_reserve_tokens") != 4096):
            raise ValueError("matched context and output budgets must equal the frozen per-fixture settings")
        if long_session and (not isinstance(fixture.get("turns"), tuple)
                or len(fixture["turns"]) != 11 or fixture["turns"][0] != fixture["prompt"]):
            raise ValueError("long-context fixtures require the frozen eleven-turn session")
    scenarios = {fixture.get("plan_scenario") for fixture in fixtures if fixture.get("plan_scenario")}
    if not {"add_discovered_edge_case", "cancel_obsolete_task_after_reproduction",
            "reorder_after_dependency_discovery", "scope_preserving", "material_revision",
            "unnecessary_churn", "cancel_replace"} <= scenarios:
        raise ValueError("matched plan fixtures must cover replanning, approval, scope, and churn")
    return True


def cohort_digest(fixtures=FIXTURES):
    """Freeze content as well as IDs; old campaign rows cannot prove a revised task."""
    return hashlib.sha256(json.dumps(fixtures, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


FROZEN_COHORT_SHA256 = "401006750309625cd44545b46ddb7190cf850799a4a617850220cbe843927b73"


validate_fixtures(FIXTURES)
if cohort_digest() != FROZEN_COHORT_SHA256:
    raise ValueError("fixture content changed; review and freeze a new cohort before any campaign")

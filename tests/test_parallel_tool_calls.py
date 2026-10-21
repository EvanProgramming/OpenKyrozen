import threading
import time
from contextvars import ContextVar
import unittest
from types import SimpleNamespace

from openkyrozen.agent.executor import _execute_tool_batch


class ParallelToolCallTests(unittest.TestCase):
    def test_safe_reads_overlap_and_mutations_remain_ordered_with_ordered_receipts(self):
        safe = SimpleNamespace(parallel_safe=True, side_effects="read_only", capability="read", risk="normal")
        mutation = SimpleNamespace(parallel_safe=False, side_effects="filesystem", capability="write", risk="normal")
        calls = [
            {"action": "read_file", "args": {"path": "slow"}, "call_id": "read-1"},
            {"action": "read_file", "args": {"path": "fast"}, "call_id": "read-2"},
            {"action": "write_file", "args": {"path": "note.txt", "content": "saved"}, "call_id": "write-1"},
            {"action": "read_file", "args": {"path": "after"}, "call_id": "read-3"},
        ]
        both_reads_started = threading.Barrier(2)
        lock = threading.Lock()
        timeline = []
        receipts = []
        context = ContextVar("parallel_test_context", default=None)
        context.set("turn-1")
        observed_contexts = []

        def execute(action, arguments, **kwargs):
            name = arguments.get("path")
            observed_contexts.append(context.get())
            if name in {"slow", "fast"}:
                with lock:
                    timeline.append((name, "start"))
                both_reads_started.wait(timeout=2)
                time.sleep(0.05 if name == "slow" else 0)
            else:
                with lock:
                    timeline.append((name, "start"))
            with lock:
                timeline.append((name, "end"))
            return SimpleNamespace(action=action, args=name, call_id=kwargs.get("call_id"), success=True)

        runtime = SimpleNamespace(
            tool_registry=SimpleNamespace(specs={"read_file": safe, "write_file": mutation}),
            _operation_action=lambda name: name,
            _execute_turn_action=execute,
        )

        _execute_tool_batch(
            runtime, calls, operation_scope="turn-1", successful_operations=set(),
            call_id_source="native", on_receipt=lambda call, receipt: receipts.append((call["call_id"], receipt.call_id)),
        )

        self.assertEqual(receipts, [(item["call_id"], item["call_id"]) for item in calls])
        self.assertEqual(observed_contexts, ["turn-1"] * len(calls))
        end_positions = [timeline.index((name, "end")) for name in ("slow", "fast")]
        write_start = timeline.index(("note.txt", "start"))
        self.assertLess(max(end_positions), write_start)
        self.assertGreater(timeline.index(("after", "start")), timeline.index(("note.txt", "end")))


if __name__ == "__main__":
    unittest.main()

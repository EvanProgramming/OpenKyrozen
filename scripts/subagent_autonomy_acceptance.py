#!/usr/bin/env python3
"""Test ordinary requests with a live saved provider in disposable projects.

Neither request mentions agents, parallelism, or verification. Prints synthetic
reports and runtime evidence, never credentials. Exits nonzero on FAIL/BLOCKED.
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
from unittest.mock import patch
from decimal import Decimal

if os.environ.get("KYROZEN_ACCEPTANCE_INSTALLED") != "1":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openkyrozen.app.bootstrap import build_application, build_memory
from openkyrozen.providers.config import ProviderConfig
from openkyrozen.providers.factory import get_provider
from openkyrozen.security.credentials import decrypt_api_key
from openkyrozen.tools import ToolAdapters
from rich.console import Console

SOURCES = {
    "README.md": """# Invoice service
Review all request boundaries against these launch requirements.
Authorization: an authenticated caller may read an invoice only when both user_id and tenant_id match the invoice owner. Cross-tenant invoice IDs must never be exposed.
Pricing: money is expressed as Decimal USD. Unit prices and quantities must be nonnegative; discount is a fraction in [0,1]. The final amount must be rounded to cents with ROUND_HALF_UP.
Inventory: reservations require positive integer quantity. An invalid or insufficient reservation must leave stock unchanged. Successful reservations decrement stock exactly once.
The functions below are production request handlers; values come from untrusted request bodies.
""",
    "authorization.py": """from dataclasses import dataclass

@dataclass
class Caller:
    user_id: str
    tenant_id: str

def get_invoice(caller, invoice_id, invoices):
    invoice = invoices[invoice_id]
    if invoice["user_id"] != caller.user_id:
        raise PermissionError("Not your invoice")
    return invoice
""",
    "pricing.py": '''from decimal import Decimal

def invoice_total(unit_price, quantity, discount=0):
    """Return a USD amount for the requested purchase."""
    amount = float(unit_price) * quantity * (1 - float(discount))
    return Decimal(str(round(amount, 2)))
''',
    "inventory.py": """class Inventory:
    def __init__(self, stock):
        self.stock = dict(stock)

    def reserve(self, sku, quantity):
        available = self.stock[sku]
        if quantity > available:
            return False
        self.stock[sku] = available - quantity
        return True
""",
    "totals.py": "def total(values):\n    return sum(values)\n",
}
CASES = (
    ("review", "Review this service before launch. Find correctness and security bugs, explain affected inputs, and give concrete fixes with file references. Please do not edit anything."),
    ("simple", "What does total([1, -2, 3]) return in totals.py?"),
)


def ground_truth():
    """Observe fixture defects independently of model/reviewer agreement."""
    authorization, pricing, inventory, totals = {}, {}, {}, {}
    for name, namespace in (("authorization.py", authorization), ("pricing.py", pricing), ("inventory.py", inventory), ("totals.py", totals)):
        exec(SOURCES[name], namespace)
    invoice = {"user_id": "same-user", "tenant_id": "other-tenant"}
    leaked = authorization["get_invoice"](authorization["Caller"]("same-user", "caller-tenant"), "invoice", {"invoice": invoice})
    stock = inventory["Inventory"]({"item": 10})
    accepted = stock.reserve("item", -3)
    checks = {"cross_tenant_invoice_exposed": leaked is invoice,
        "out_of_range_discount_negative_total": pricing["invoice_total"]("10", 1, 2) < 0,
        "negative_reservation_increases_stock": accepted and stock.stock["item"] == 13,
        "decimal_values_sum_correctly": totals["total"]([Decimal("1.00")]) == Decimal("1.00")}
    assert all(checks.values()), "Fixture no longer reproduces its intended defects"
    return checks


def false_decimal_sum_claim(runs):
    """A positive control: integer zero does not prevent Decimal summation."""
    def strings(value):
        if isinstance(value, dict):
            for item in value.values():
                yield from strings(item)
        elif isinstance(value, list):
            for item in value:
                yield from strings(item)
        elif isinstance(value, str):
            yield value.lower()
    for run in runs:
        if run["status"] != "succeeded":
            continue
        for finding in run.get("report", {}).get("findings", []):
            text = json.dumps(finding).lower()
            if "decimal" not in text or "sum" not in text or not ("totals.py" in text or "total(values)" in text):
                continue
            for value in strings(finding):
                for claim in re.split(r"[.;]\s+", value):
                    # A literal float in a supplied example is a genuinely
                    # incompatible input, even if the prose omits "float".
                    example = re.search(r"(?:sum|total)\((.*?)\)\s*(?:raises|->|returns)", claim)
                    if example:
                        try:
                            nodes = ast.walk(ast.parse(example[1], mode="eval"))
                            if any(isinstance(node, ast.Constant) and isinstance(node.value, float) for node in nodes):
                                continue
                        except SyntaxError:
                            pass
                    if ("decimal" in claim and re.search(r"typeerror|unsupported|cannot|can't|not support", claim)
                            and not re.search(r"float|string|non.numeric|\bnone\b|mixed", claim)
                            and re.search(r"\bint\b|integer|nonempty|\bsum\b|total\(", claim)):
                        return True
    return False


def acceptance(*, inject_incorrect_result=False):
    saved = json.loads((Path.home() / ".kyrozen_config.json").read_text())
    config = ProviderConfig(provider=saved["provider"], api_key=decrypt_api_key(saved.get("api_key", "")),
        model_simple=saved.get("model_simple", ""), model_complex=saved.get("model_complex", ""))
    if not config.api_key:
        raise RuntimeError("BLOCKED: saved provider credential unavailable")
    timeout = os.environ.get("KYROZEN_PROVIDER_TIMEOUT_SECONDS")
    output = {"provider": config.provider, "provider_timeout_seconds": timeout or "runtime defaults: main 90, delegated 180",
        "ground_truth": ground_truth(), "cases": []}
    for case, prompt in CASES:
        with tempfile.TemporaryDirectory(prefix="openkyrozen-autonomy-") as directory:
            state_root = Path(directory)
            root = state_root / "project"
            root.mkdir()
            for name, content in SOURCES.items():
                (root / name).write_text(content)
            with patch.dict(os.environ, {"KYROZEN_WORKSPACE_ROOT": str(root), "KYROZEN_SKILLS_DIR": str(state_root / "skills"),
                    "KYROZEN_DISABLE_VECTOR_INDEX": "1"}):
                app = build_application(memory=build_memory(state_root / "state.sqlite3"), tools=ToolAdapters(root))
                runtime = app.runtime
                runtime._provider_config = config
                runtime.DEEPSEEK_MODEL = config.model_complex or config.model_simple
                runtime.llm_provider = get_provider(config)
                runtime.get_provider = get_provider
                runtime.console = Console(file=io.StringIO())
                runtime._surface_capabilities = "read"
                runtime.dispatch_learning_cycle = lambda **_: {}
                runtime._touch_detached_learning_heartbeat = lambda: None
                runtime.set_interaction_mode("agent")
                injected = {}
                if inject_incorrect_result and case == "review":
                    original_invoke = runtime._invoke_delegated
                    injection_lock = threading.Lock()
                    def invoke(run, **kwargs):
                        result = original_invoke(run, **kwargs)
                        with injection_lock:
                            if not kwargs["review"] and not injected:
                                target = next((name for name in ("authorization.py", "inventory.py", "pricing.py")
                                    if name in run["assignment"]["scope"]), None)
                                if target:
                                    keyword, claim = {"authorization.py": ("tenant", "get_invoice enforces both user_id and tenant_id equality"),
                                        "inventory.py": ("quantity", "reserve rejects negative and fractional quantity before changing stock"),
                                        "pricing.py": ("discount", "invoice_total rejects discounts outside [0,1] before calculating")}[target]
                                    submitted = json.loads(result["result"])
                                    submitted.update(summary="No launch-blocking issue in " + target,
                                        findings=[{"source": target, "claim": claim, "fix": "No change required"}],
                                        artifacts=["nonexistent-review-proof.txt"])
                                    result["result"] = json.dumps(submitted)
                                    injected.update(run_id=run["run_id"], target=target, keyword=keyword, claim=claim)
                        return result
                    runtime._invoke_delegated = invoke
                started, events = time.monotonic(), []
                try:
                    with contextlib.redirect_stdout(io.StringIO()):
                        reply = runtime.chat(runtime.current_session, prompt, on_event=events.append)
                    coordinator = runtime.subagent_manager.coordinator
                    runs = coordinator.wait(timeout=60) if coordinator else []
                    checks = {"delegation_choice": len(runs) >= 2 if case == "review" else not runs,
                        "verified_runs": all(run["status"] == "succeeded" for run in runs),
                        "parent_tasks_complete": all(task["status"] in {"succeeded", "done"} for task in runtime.tasks.tasks),
                        "unchanged_sources": all((root / name).read_text() == content for name, content in SOURCES.items())}
                    inspected = {"author": set(), "reviewer": set()}
                    fidelity, independent = True, bool(runs)
                    for run in runs:
                        independent &= bool(run["reviews"] and run.get("result", {}).get("metrics", {}).get("attempts"))
                        records = {"author": run.get("result", {}).get("tool_records", []),
                            "reviewer": run["reviews"][-1]["tool_receipts"] if run["reviews"] else []}
                        for actor, receipts in records.items():
                            sources = [receipt for receipt in receipts if receipt.get("success") and receipt.get("action") == "read_file"
                                and Path(receipt["args"]).name in SOURCES and Path(receipt["args"]).name != "README.md"]
                            independent &= bool(sources)
                            for receipt in sources:
                                inspected[actor].add(Path(receipt["args"]).name)
                                fidelity &= receipt["result"] == (root / receipt["args"]).read_text()
                    if case == "review":
                        checks.update(independent_source_reviews=independent, source_fidelity=fidelity,
                            no_false_decimal_sum_finding=not false_decimal_sum_claim(runs),
                            independently_checked_arithmetic=any(receipt.get("action") == "calculate" and receipt.get("success")
                                for run in runs for review in run["reviews"] for receipt in review["tool_receipts"]),
                            subsystem_coverage=all({"authorization.py", "pricing.py", "inventory.py"} <= paths for paths in inspected.values()),
                            observed_lifecycle=any(event.get("event") == "subagent" for event in events),
                            authorization_finding="authorization.py" in reply and "tenant" in reply.lower(),
                            pricing_finding="pricing.py" in reply and "discount" in reply.lower(),
                            inventory_finding="inventory.py" in reply and bool(re.search(r"negative|positive integer", reply, re.I)))
                        if inject_incorrect_result:
                            altered = next((run for run in runs if run["run_id"] == injected.get("run_id")), {})
                            reviews = altered.get("reviews", [])
                            checks.update(incorrect_result_injected=bool(injected),
                                claim_independently_rejected=bool(reviews and reviews[0]["verdict"] == "changes_requested"
                                    and reviews[0]["summary"] != "Runtime artifact checks did not pass"
                                    and injected["keyword"] in json.dumps(reviews[0]["findings"]).lower()),
                                missing_artifact_rejected=bool(reviews and any(not item.get("exists")
                                    for item in reviews[0].get("artifact_checks", []))),
                                author_corrected_and_rechecked=bool(altered.get("corrections", 0) >= 1
                                    and len(reviews) >= 2 and reviews[-1]["verdict"] == "verified"))
                    else:
                        checks["correct_direct_answer"] = bool(re.search(r"returns\s*\*{0,2}2\b", reply, re.I))
                    output["cases"].append({"case": case, "user_request": prompt, "status": "PASS" if all(checks.values()) else "FAIL",
                        "checks": checks, "elapsed_seconds": round(time.monotonic() - started, 3),
                        "final_report": runtime._fix_safe_text(reply, 30000, preserve_lines=True),
                        "injected_fault": injected,
                        "agents": [coordinator.summary(run, results=True) for run in runs] if coordinator else []})
                finally:
                    app.close()
    output["status"] = "PASS" if all(case["status"] == "PASS" for case in output["cases"]) else "FAIL"
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--inject-incorrect-result", action="store_true", help="Alter one submitted report and attach a missing artifact; reviewers and corrections remain live.")
    args = parser.parse_args()
    report = acceptance(inject_incorrect_result=args.inject_incorrect_result)
    rendered = json.dumps(report, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n")
    print(rendered)
    sys.exit(report["status"] != "PASS")

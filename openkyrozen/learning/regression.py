from __future__ import annotations



def _take_tool_snapshot(self) -> None:
    self._saved_user_tools = {k: v for k, v in self.AVAILABLE_TOOLS.items() if k not in self._BUILTIN_TOOL_NAMES}


def _restore_tool_snapshot(self) -> None:
    # Remove current user‑defined tools
    keys_to_remove = [k for k in self.AVAILABLE_TOOLS if k not in self._BUILTIN_TOOL_NAMES]
    for k in keys_to_remove:
        del self.AVAILABLE_TOOLS[k]
    # Restore from snapshot
    self.AVAILABLE_TOOLS.update(self._saved_user_tools)


def _run_regression_tests(self) -> bool:
    """Execute the core test suite and return True iff all tests pass."""
    all_pass = True
    for test in self._CORE_TESTS:
        action = test["action"]
        args = test["args"]
        expected = test.get("expected", "")
        check_type = test["check"]

        fn = self.AVAILABLE_TOOLS.get(action)
        if fn is None:
            all_pass = False
            continue
        try:
            result = fn(args)
        except Exception as e:
            result = f"Error: {e}"

        passed = False
        if check_type == "nonempty":
            passed = bool(result.strip())
        elif check_type == "contains":
            passed = expected in result
        elif check_type == "error":
            passed = result.strip().lower().startswith("error")

        if not passed:
            all_pass = False
        else:
            pass
    return all_pass


def _auto_debug_tool(self) -> None:
    """Analyse tool failures and error patterns, then log debugging insights."""
    # Collect failing tools from performance stats
    failing_tools = []
    for tool_name, stats in self._tool_stats.items():
        if stats.get("calls", 0) > 2:
            success_rate = stats.get("successes", 0) / max(stats["calls"], 1)
            if success_rate < 0.5:
                failing_tools.append((tool_name, stats))

    # Collect recent failure entries from memory
    recent = self.memory_bank.get_recent(30)
    failures = [r for r in recent if r and r.startswith(self._FAILURE_STORE_PREFIX)]

    if not failing_tools and not failures:
        return

    # Build analysis prompt
    parts = []
    if failing_tools:
        parts.append("Low‑success tools detected:")
        for name, stats in failing_tools:
            parts.append(
                f"  - {name}: {stats['calls']} calls, "
                f"{stats['successes']} successes "
                f"({stats['successes']/max(stats['calls'],1)*100:.0f}% success rate)"
            )
    if failures:
        parts.append("\nRecent failure records:")
        for f in failures[-3:]:
            parts.append(f"  {f[:300]}")

    debug_prompt = (
        "You are Kyrozen's auto‑debug module. Analyse the following tool "
        "performance data and failure records. Identify the root cause of "
        "failures and suggest concrete fixes. Output each finding on a new "
        "line prefixed with 'DEBUG_FINDING:'.\n\n"
        + "\n".join(parts)
    )
    try:
        messages = [{"role": "system", "content": debug_prompt}]
        answer = (self._learning_model_response(messages, feature="auto_debug_tool") or "").strip()
        if answer:
            for line in answer.split("\n"):
                line = line.strip()
                if line.startswith("DEBUG_FINDING:") or line.startswith("FIX:"):
                    self.memory_bank.add_log(f"DEBUG: {line}")
    except Exception:
        pass

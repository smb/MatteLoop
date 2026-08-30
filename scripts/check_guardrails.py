#!/usr/bin/env python3
"""Ratchet check for the rules in docs/engineering-guardrails.md.

This does not demand that existing oversized modules be rewritten. It records
their current size as a baseline and fails only when a module *grows* past it,
or when a new violation appears. That freezes the hardening spiral described in
docs/engineering-guardrails.md section 1 without forcing a risky rewrite.

Usage:
    python scripts/check_guardrails.py              # check, exit 1 on regression
    python scripts/check_guardrails.py --update     # rewrite the baseline
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = ROOT / "scripts" / "guardrails-baseline.json"

MODULE_LINE_BUDGET = 800
FUNCTION_LINE_BUDGET = 60

# Guardrail G7: test modules named after a process instead of a behaviour.
FORBIDDEN_TEST_NAME_FRAGMENTS = (
    "task1",
    "task2",
    "task3",
    "review_fixes",
    "reviewfixes",
    "round2",
    "round3",
    "followup",
    "follow_up",
    "fixes_from",
    "feedback_fixes",
)


def _module_metrics(path: Path) -> tuple[int, int]:
    """Return (line count, number of functions over the length budget)."""
    source = path.read_text(encoding="utf-8")
    lines = len(source.splitlines())
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return lines, 0
    oversized = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            end = node.end_lineno or node.lineno
            if end - node.lineno + 1 > FUNCTION_LINE_BUDGET:
                oversized += 1
    return lines, oversized


def _collect() -> dict[str, dict[str, int]]:
    metrics: dict[str, dict[str, int]] = {}
    for path in sorted((ROOT / "src").rglob("*.py")):
        lines, oversized = _module_metrics(path)
        if lines > MODULE_LINE_BUDGET or oversized:
            key = path.relative_to(ROOT).as_posix()
            metrics[key] = {"lines": lines, "long_functions": oversized}
    return metrics


def _forbidden_test_names() -> list[str]:
    offenders = []
    for path in sorted((ROOT / "tests").rglob("test_*.py")):
        name = path.stem.lower()
        if any(fragment in name for fragment in FORBIDDEN_TEST_NAME_FRAGMENTS):
            offenders.append(path.relative_to(ROOT).as_posix())
    return offenders


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update",
        action="store_true",
        help="rewrite the baseline from the current tree",
    )
    args = parser.parse_args()

    current = _collect()

    if args.update:
        BASELINE_PATH.write_text(
            json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"baseline updated: {len(current)} module(s) recorded")
        return 0

    baseline: dict[str, dict[str, int]] = (
        json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        if BASELINE_PATH.exists()
        else {}
    )

    failures: list[str] = []
    for module, metrics in sorted(current.items()):
        allowed = baseline.get(module)
        if allowed is None:
            problems = []
            if metrics["lines"] > MODULE_LINE_BUDGET:
                problems.append(
                    f"{metrics['lines']} lines exceeds the {MODULE_LINE_BUDGET}-line "
                    f"module budget — split it into a package"
                )
            if metrics["long_functions"]:
                problems.append(
                    f"{metrics['long_functions']} function(s) exceed the "
                    f"{FUNCTION_LINE_BUDGET}-line budget — extract named helpers"
                )
            failures.append(f"G6 {module}: {'; '.join(problems)}.")
            continue
        if metrics["lines"] > allowed["lines"]:
            failures.append(
                f"G6 {module}: grew from {allowed['lines']} to "
                f"{metrics['lines']} lines. Frozen modules must not grow — see "
                f"docs/engineering-guardrails.md G1/G3/G6."
            )
        if metrics["long_functions"] > allowed["long_functions"]:
            failures.append(
                f"G6 {module}: over-long functions grew from "
                f"{allowed['long_functions']} to {metrics['long_functions']}."
            )

    for offender in _forbidden_test_names():
        failures.append(
            f"G7 {offender}: test modules are named after behaviour, "
            f"never after a task number or a review round."
        )

    if failures:
        print("Guardrail check failed:\n", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        print(
            "\nIf a change genuinely justifies growth, say so to the user and "
            "run: python scripts/check_guardrails.py --update",
            file=sys.stderr,
        )
        return 1

    shrunk = [
        module
        for module, metrics in current.items()
        if module in baseline and metrics["lines"] < baseline[module]["lines"]
    ]
    print(f"Guardrail check passed ({len(current)} module(s) over budget).")
    if shrunk:
        print(f"Shrunk since baseline: {', '.join(sorted(shrunk))} — run --update.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

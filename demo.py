"""Repair five real MBPP/HumanEval bugs whose fix is known, and show what the search returns.

Each task is a reference solution with one injected single-point bug and at least one
visible assert that fails. The classical repair search enumerates every one-edit
neighbour of the buggy program, keeps those that pass the visible asserts, and returns
the smallest. The reference then says whether that patch is the real fix, a program
equivalent to it on every input tried, or a wrong program that merely passes the tests.

    uv run python demo.py

Runs live (a few seconds per task) from the committed task files; no model, no network.
"""

from __future__ import annotations

import sys

from minimal_diff import cli, repair, tasks

# Picked from results/classical_*.jsonl.gz to cover the outcomes the study counts.
DEMO = [
    (
        "humaneval/52/compare@17",
        "all",
        "1. HumanEval's six asserts pin the fix down: one plausible patch, and it is the fix",
    ),
    (
        "mbpp/3/binop@17",
        "all",
        "2. MBPP's three asserts do not: two one-token patches pass, the wrong one is returned",
    ),
    (
        "mbpp/53/compare@5",
        "k1",
        "3. Shown only the failing assert, five of six plausible patches are wrong or unproven...",
    ),
    ("mbpp/53/compare@5", "all", "4. ...shown all three, the fix is the only one left"),
    (
        "mbpp/867/negate_if@32",
        "all",
        "5. A patch nothing can tell from the fix - on a line that was never broken",
    ),
]


def main() -> int:
    for task_id, regime, why in DEMO:
        source = task_id.split("/", 1)[0]
        task = next(t for t in tasks.load_tasks(source) if t.id == task_id)
        prob = tasks.load_problems(source)[task.problem]
        row = repair.repair_task(task, prob)
        print("=" * 88)
        print(f"{why}\n")
        print(cli.render_row(task, prob, row, regime))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

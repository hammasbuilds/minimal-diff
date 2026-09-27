"""Does reusing a worker interpreter change any verdict? Measured, not assumed.

The repair search runs every candidate of a task in one long-lived child process, and
reuses that process across tasks (`sandbox.py`). A candidate could leave state behind -
a monkeypatched module, a changed recursion limit - that changes how a later one
behaves. This re-runs a random sample of candidates each in its own brand-new
interpreter and compares the outcome with the one the pooled run produced.
"""

from __future__ import annotations

import random
from concurrent.futures import ThreadPoolExecutor

from . import operators, repair, sandbox
from .tasks import ProblemRecord, Task


def check(
    tasks: list[Task],
    problems: dict[str, ProblemRecord],
    n_tasks: int = 200,
    per_task: int = 4,
    seed: int = 0,
    workers: int = 8,
) -> dict:
    picked = random.Random(seed).sample(tasks, min(n_tasks, len(tasks)))

    def one(task: Task) -> list[dict]:
        prob = problems[task.problem]
        codes = [e.code for e in operators.neighbours(task.buggy)]
        order = repair.visible_subsets(task, len(prob.tests))["all"]
        pooled = repair.run_in_order(codes, prob, order, prob.item_budget)
        chosen = random.Random(f"{seed}/{task.id}").sample(
            range(len(codes)), min(per_task, len(codes))
        )
        out = []
        for ci in chosen:
            tests = [prob.tests[i] for i in order]
            [alone] = sandbox.run_asserts(
                [codes[ci]], tests, prob.setup, item_timeout=prob.item_budget, fresh=True
            )
            a = [pooled[ci][i].status for i in order]
            b = [r.status for r in alone]
            out.append({"task": task.id, "candidate": ci, "pooled": a, "fresh": b, "agree": a == b})
        return out

    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = [r for rs in pool.map(one, picked) for r in rs]
    disagree = [r for r in rows if not r["agree"]]
    # A disagreement where one side timed out is load, not leakage: under a busy machine
    # a 1 s budget can be missed by one run and met by the other.
    timing = [r for r in disagree if "timeout" in r["pooled"] + r["fresh"]]
    return {
        "tasks": len(picked),
        "candidates": len(rows),
        "agree": len(rows) - len(disagree),
        "disagree": len(disagree),
        "disagree_involving_timeout": len(timing),
        "disagreements": disagree[:50],
    }


def oracle_false_positives(problems: dict[str, ProblemRecord], workers: int = 8) -> dict:
    """Judge a program that is the reference plus one inert statement, for every problem.

    It is a different text, so it skips the "exact" shortcut and goes through every check
    the oracle has; any "overfit" verdict is by construction a false witness.
    """
    from . import oracle

    def one(p: ProblemRecord) -> tuple[str, oracle.Verdict]:
        return p.key, oracle.judge(p, [p.reference + "\n_md_inert_ = 0\n"])[0]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        res = list(pool.map(one, problems.values()))
    bad = [
        {"problem": k, "found_by": v.found_by, "witness": v.witness}
        for k, v in res
        if v.label == "overfit"
    ]
    return {"problems": len(res), "false_overfit": len(bad), "cases": bad}

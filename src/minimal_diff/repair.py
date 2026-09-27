"""Classical search-based repair: enumerate one-edit neighbours, keep those the tests pass.

No model. Every candidate from `operators.neighbours` is run once against the visible
asserts, and the plausible set for each regime (how many asserts the repairer is shown)
is read off that one run. The regimes are *prefixes* of one fixed assert order that
starts with an assert the bug fails, so a candidate can stop at its first failing
assert: every regime that contains that assert rejects it, and every regime that does
not contains only asserts it already passed. Nothing is run twice and no verdict is
guessed.

Which plausible patch the repairer returns is a *policy*:

- `smallest`: fewest tokens changed, then fewest AST nodes, then fewest lines, then the
  earliest site. "The laziest senior dev".
- `random`: a uniformly random plausible patch; reported as an expectation over the
  plausible set, not as one draw.
- `smallest` with a random tie-break (`tied_*`): nearly every one-edit patch is one
  token, so `smallest` mostly decides by site order. This separates the two.
- `at_fault`: `smallest`, but only among patches that edit the faulty lines and nothing
  else. An oracle - no real repairer knows where the bug is - that separates "the tests
  cannot tell" from "the search looked in the wrong place".
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from . import diffmetrics, operators, oracle, sandbox
from .tasks import ProblemRecord, Task


@dataclass
class Candidate:
    index: int
    where: str
    kind: str
    passes: list[bool]  # per visible assert
    size: dict = field(default_factory=dict)
    verdict: str = ""
    witness: str = ""
    found_by: str = ""
    is_truth: bool = False

    def order_key(self) -> tuple:
        s = self.size
        return (
            s["tokens"],
            s["ast_nodes"] if s["ast_nodes"] is not None else 10**6,
            s["lines"],
            self.index,
        )


def visible_subsets(task: Task, n_tests: int) -> dict[str, list[int]]:
    """Which visible asserts the repairer is shown, per regime.

    Every regime includes the first assert the bug fails, so every task has a failing
    test to repair against. `k1` is that assert alone; `k3` adds the next two in dataset
    order (for MBPP, which has exactly three, `k3` is `all`).
    """
    first = task.failing[0]
    order = [first] + [i for i in range(n_tests) if i != first]
    return {"k1": order[:1], "k3": order[:3], "all": order}


def select(cands: list[Candidate], subset: list[int], policy: str) -> Candidate | None:
    plausible = [c for c in cands if all(c.passes[i] for i in subset)]
    if policy == "at_fault":
        plausible = [
            c for c in plausible if c.size["touches_fault"] and c.size["unrelated_lines"] == 0
        ]
    elif policy != "smallest":
        raise ValueError(f"unknown policy {policy!r}")
    return min(plausible, key=Candidate.order_key, default=None)


def summarise_subset(cands: list[Candidate], subset: list[int]) -> dict:
    plausible = [c for c in cands if all(c.passes[i] for i in subset)]
    out: dict = {"n_plausible": len(plausible)}
    if plausible:
        n = len(plausible)
        out["random_exact"] = sum(c.verdict == "exact" for c in plausible) / n
        out["random_overfit"] = sum(c.verdict == "overfit" for c in plausible) / n
        out["min_tokens"] = min(c.size["tokens"] for c in plausible)
        best = min(x.order_key() for x in plausible)[:3]
        tied = [c for c in plausible if c.order_key()[:3] == best]
        out["n_tied_smallest"] = len(tied)
        # `smallest` breaks ties by site order; this is the same policy with a random
        # tie-break, as an expectation, so site order cannot pass for a size effect.
        out["tied_exact"] = sum(c.verdict == "exact" for c in tied) / len(tied)
        out["tied_overfit"] = sum(c.verdict == "overfit" for c in tied) / len(tied)
        truth = [c for c in plausible if c.is_truth]
        out["truth_plausible"] = bool(truth)
        if truth:
            # Is there a wrong patch at least as small as the right one?
            t = truth[0].order_key()[:3]
            out["overfit_as_small_as_truth"] = any(
                c.verdict == "overfit" and c.order_key()[:3] <= t for c in plausible
            )
    for policy in ("smallest", "at_fault"):
        pick = select(cands, subset, policy)
        out[policy] = (
            None
            if pick is None
            else {
                "where": pick.where,
                "kind": pick.kind,
                "verdict": pick.verdict,
                "found_by": pick.found_by,
                "is_truth": pick.is_truth,
                "size": pick.size,
            }
        )
    return out


REPAIR_ITEM_TIMEOUT = 1.0  # an MBPP/HumanEval assert on a correct program takes milliseconds


def run_in_order(
    codes: list[str], problem: ProblemRecord, order: list[int], item_timeout: float
) -> list[list[sandbox.Result]]:
    """Each program against the visible asserts in `order`, stopping at its first failure.

    Returned in the problem's own assert order; asserts not reached are `skipped`.
    """
    tests = [problem.tests[i] for i in order]
    ran = sandbox.run_asserts(
        codes, tests, problem.setup, stop_on_first_failure=True, item_timeout=item_timeout
    )
    rows = []
    for row in ran:
        back = [sandbox.Result("skipped")] * len(problem.tests)
        for pos, i in enumerate(order):
            back[i] = row[pos]
        rows.append(back)
    return rows


def repair_task(
    task: Task, problem: ProblemRecord, item_timeout: float = REPAIR_ITEM_TIMEOUT
) -> dict:
    """Search, judge and summarise one task. The returned dict is one results row."""
    edits = operators.neighbours(task.buggy)
    tests = problem.tests
    subsets = visible_subsets(task, len(tests))
    rows = run_in_order([e.code for e in edits], problem, subsets["all"], item_timeout)
    cands = [
        Candidate(i, e.where, e.kind, [r.status == "pass" for r in row])
        for i, (e, row) in enumerate(zip(edits, rows, strict=True))
    ]
    widest = subsets["k1"]  # every other regime's plausible set is a subset of this one
    judged = [c for c in cands if all(c.passes[i] for i in widest)]
    verdicts = oracle.judge(
        problem,
        [edits[c.index].code for c in judged],
        [rows[c.index] for c in judged],
        item_timeout,
    )
    fault = set(task.fault_lines)
    for c, v in zip(judged, verdicts, strict=True):
        c.verdict, c.witness, c.found_by = v.label, v.witness, v.found_by
        c.is_truth = v.label == "exact"
        c.size = diffmetrics.measure(task.buggy, edits[c.index].code, fault).as_dict()
    truth_in_space = any(e.code == problem.reference for e in edits)
    return {
        "id": task.id,
        "problem": task.problem,
        "source": problem.source,
        "bug_kind": task.kind,
        "n_tests": len(tests),
        "n_candidates": len(edits),
        "n_timeouts": sum(r.status in ("timeout", "crash") for row in rows for r in row),
        # By construction the reverse of every injected mutation is a neighbour. The
        # summary reports this rate; anything under 100% means the injector and the
        # repair operators have drifted apart.
        "truth_in_space": truth_in_space,
        "subset_indices": subsets,
        "subsets": {name: summarise_subset(judged, s) for name, s in subsets.items()},
        "plausible_k1": [asdict(c) for c in judged],
    }

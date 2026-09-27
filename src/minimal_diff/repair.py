"""Classical search-based repair: enumerate one-edit neighbours, keep those the tests pass.

No model. Every candidate from `operators.neighbours` is run once against the visible
asserts, and the plausible set for each regime (how many asserts the repairer is shown)
is read off that one run. The regimes are *prefixes* of one fixed assert order that
starts with an assert the bug fails, so a candidate can stop at its first failing
assert: every regime that contains that assert rejects it, and every regime that does
not contains only asserts it already passed. Nothing is run twice and no verdict is
guessed.

Which plausible patch the repairer returns is a *policy*: a size metric (`METRICS`) and
a tie-break.

- **size metric**: `tokens` (fewest tokens changed), `ast` (fewest AST nodes), `lines`,
  or a lexicographic combination (`tokens+ast`, `ast+tokens`). They disagree exactly
  where it matters: removing a `not` is one token but two AST nodes, while swapping a
  comparison is one of each - so `tokens+ast` ranks every un-negation below every
  operator swap, and `tokens` does not.
- **site order** tie-break: the earliest edit site wins. Deterministic, and arbitrary.
- **random** tie-break (`tied_*`): the expectation over the tied set, so site order
  cannot pass for a size effect.
- `random` over *all* plausible patches: the no-preference baseline.
- `at_fault`: the default metric, but only among patches that edit the faulty lines and
  nothing else. An oracle - no real repairer knows where the bug is - that separates
  "the tests cannot tell" from "the search looked in the wrong place".
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from . import diffmetrics, operators, oracle, sandbox
from .tasks import ProblemRecord, Task

METRICS: dict[str, tuple[str, ...]] = {
    "tokens": ("tokens",),
    "ast": ("ast_nodes",),
    "lines": ("lines",),
    "tokens+ast": ("tokens", "ast_nodes"),
    "ast+tokens": ("ast_nodes", "tokens"),
}
# What `show`, the demo and the `smallest` fields of a results row use. Fewest tokens
# changed is the plain reading of "the fix that changes the least".
DEFAULT_METRIC = "tokens"


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

    def size_key(self, metric: str = DEFAULT_METRIC) -> tuple:
        return tuple(self.size[f] if self.size[f] is not None else 10**6 for f in METRICS[metric])

    def order_key(self, metric: str = DEFAULT_METRIC) -> tuple:
        """Size first, then site order."""
        return (*self.size_key(metric), self.index)


def visible_subsets(task: Task, n_tests: int) -> dict[str, list[int]]:
    """Which visible asserts the repairer is shown, per regime.

    Every regime includes the first assert the bug fails, so every task has a failing
    test to repair against. `k1` is that assert alone; `k3` adds the next two in dataset
    order (for MBPP, which has exactly three, `k3` is `all`).
    """
    first = task.failing[0]
    order = [first] + [i for i in range(n_tests) if i != first]
    return {"k1": order[:1], "k3": order[:3], "all": order}


def select(
    cands: list[Candidate], subset: list[int], policy: str, metric: str = DEFAULT_METRIC
) -> Candidate | None:
    plausible = [c for c in cands if all(c.passes[i] for i in subset)]
    if policy == "at_fault":
        plausible = [
            c for c in plausible if c.size["touches_fault"] and c.size["unrelated_lines"] == 0
        ]
    elif policy != "smallest":
        raise ValueError(f"unknown policy {policy!r}")
    return min(plausible, key=lambda c: c.order_key(metric), default=None)


def _pick(c: Candidate | None) -> dict | None:
    if c is None:
        return None
    return {
        "where": c.where,
        "kind": c.kind,
        "verdict": c.verdict,
        "found_by": c.found_by,
        "is_truth": c.is_truth,
        "size": c.size,
    }


def _metric_summary(plausible: list[Candidate], metric: str) -> dict:
    best = min(c.size_key(metric) for c in plausible)
    tied = [c for c in plausible if c.size_key(metric) == best]
    site = min(plausible, key=lambda c: c.order_key(metric))
    out = {
        "site_verdict": site.verdict,
        "n_tied": len(tied),
        "tied_exact": sum(c.verdict == "exact" for c in tied) / len(tied),
        "tied_overfit": sum(c.verdict == "overfit" for c in tied) / len(tied),
    }
    truth = [c for c in plausible if c.is_truth]
    if truth:
        t = truth[0].size_key(metric)
        out["truth_strictly_smallest"] = all(
            c.is_truth or c.size_key(metric) > t for c in plausible
        )
        # Is there a provably wrong patch at least as small as the right one?
        out["overfit_as_small_as_truth"] = any(
            c.verdict == "overfit" and c.size_key(metric) <= t for c in plausible
        )
    return out


def summarise_subset(cands: list[Candidate], subset: list[int]) -> dict:
    plausible = [c for c in cands if all(c.passes[i] for i in subset)]
    out: dict = {"n_plausible": len(plausible)}
    if plausible:
        n = len(plausible)
        out["random_exact"] = sum(c.verdict == "exact" for c in plausible) / n
        out["random_overfit"] = sum(c.verdict == "overfit" for c in plausible) / n
        out["truth_plausible"] = any(c.is_truth for c in plausible)
        out["by_metric"] = {m: _metric_summary(plausible, m) for m in METRICS}
    out["smallest"] = _pick(select(cands, subset, "smallest"))
    out["at_fault"] = _pick(select(cands, subset, "at_fault"))
    return out


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


def repair_task(task: Task, problem: ProblemRecord, item_timeout: float | None = None) -> dict:
    """Search, judge and summarise one task. The returned dict is one results row."""
    edits = operators.neighbours(task.buggy)
    tests = problem.tests
    subsets = visible_subsets(task, len(tests))
    budget = problem.item_budget if item_timeout is None else item_timeout
    rows = run_in_order([e.code for e in edits], problem, subsets["all"], budget)
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

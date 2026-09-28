"""Turn benchmark problems into repair tasks: a buggy program, the test it fails, the truth.

For every problem:

1. The reference is normalised with `ast.unparse`, so a patch is never charged for
   formatting the benchmark happened to use. The normalised reference must pass every
   visible and hidden assert, or the problem is dropped (MBPP ships a few references that
   fail their own asserts; HumanEval asserts that only work inside `check` are dropped
   one by one).
2. Hidden inputs are built and vetted: the reference must return a value, and the same
   value twice, on each one. EvalPlus inputs for HumanEval; perturbed assert arguments
   for both.
3. Every single-point mutant of the reference is run on the visible asserts. A mutant
   that fails at least one becomes a task: it is a bug with a failing test, a known
   location, and a known correct fix.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import data, diffmetrics, inputs, operators, sandbox

FAILED = ("fail", "error", "timeout", "crash")


@dataclass
class ProblemRecord:
    key: str
    source: str
    pid: int
    text: str
    entry_point: str
    reference: str  # normalised
    tests: list[str]  # visible asserts, all passed by the reference
    setup: str = ""
    hidden_tests: list[str] = field(default_factory=list)
    hidden_inputs: list[str] = field(default_factory=list)
    # hidden_inputs[:n_near_inputs] are EvalPlus inputs and one-step perturbations of the
    # asserts' arguments; the rest are random fuzz. A witness's index says which found it.
    n_near_inputs: int = 0
    dropped_tests: int = 0
    dropped_inputs: int = 0
    # The reference's slowest visible or hidden assert, measured at build time.
    slowest_assert_s: float = 0.0

    @property
    def item_budget(self) -> float:
        """Seconds any one assert or hidden input may take before it counts as a hang.

        At least `BASE_ITEM_BUDGET`, and never under `SLOWDOWN` times the reference's
        slowest assert, so a timeout always means "far slower than the reference", never
        "a correct program on a busy machine". Every judge in the repo - the search, the
        oracle, the model arm - uses this one number.
        """
        return max(BASE_ITEM_BUDGET, SLOWDOWN * self.slowest_assert_s)


@dataclass
class Task:
    id: str
    problem: str  # ProblemRecord.key
    kind: str  # the injected mutation operator
    where: str
    buggy: str
    fault_lines: list[int]  # lines of `buggy` that differ from the reference
    visible_status: list[str]  # the buggy program's outcome on each visible assert

    @property
    def failing(self) -> list[int]:
        return [i for i, s in enumerate(self.visible_status) if s in FAILED]


@dataclass
class BuildStats:
    problems_seen: int = 0
    problems_kept: int = 0
    dropped_reference_fails: int = 0
    dropped_no_tests: int = 0
    dropped_no_mutants: int = 0
    # Every mutant passed every visible assert, so no task has a failing test.
    dropped_all_survived: int = 0
    mutants: int = 0
    mutants_survived: int = 0  # passed every visible assert: not a task (no failing test)
    tasks: int = 0


BASE_ITEM_BUDGET = 1.0
SLOWDOWN = 20
# A hidden input is kept only if a whole comparison of the reference against itself on it
# - both calls and the equality check - takes at most BASE_ITEM_BUDGET / SLOWDOWN. A patch
# that runs out the item budget on it is then at least 20x slower than the reference. The
# first version timed the reference call alone; comparing a 793x793 matrix to itself
# then took over a second on its own and three correct patches were judged "overfit".
VET_MAX_ITEM_SECONDS = BASE_ITEM_BUDGET / SLOWDOWN
REFERENCE_CHECK_TIMEOUT = 10.0  # generous: this only decides which asserts are usable


def _vet_inputs(ref: str, fn: str, setup: str, cands: list[str]) -> list[str]:
    if not cands:
        return []
    [row] = sandbox.run_compare(
        ref, fn, [ref], cands, setup, stop_on_first_difference=False, item_timeout=1.0
    )
    return [
        x
        for x, r in zip(cands, row, strict=True)
        if r.status == "same" and r.ref_status == "ok" and r.seconds <= VET_MAX_ITEM_SECONDS
    ]


def _own_args(test: str, fn: str) -> str | None:
    """The argument list an assert passes to `fn`, as source."""
    got = inputs.generated_inputs([test], fn, cap=1)
    return got[0] if got else None


def build_problem(p: data.Problem) -> tuple[ProblemRecord | None, list[Task], dict]:
    """One problem's record and tasks, plus counters for the build summary."""
    ref = operators.normalise(p.reference)
    counts: dict[str, Any] = {"mutants": 0, "survived": 0, "reason": ""}
    if operators.parse(ref) is None:
        counts["reason"] = "reference_fails"
        return None, [], counts
    checks = sandbox.run_asserts(
        [ref],
        list(p.tests) + list(p.hidden_tests),
        p.setup,
        False,
        item_timeout=REFERENCE_CHECK_TIMEOUT,
    )[0]
    vis_ok = [t for t, r in zip(p.tests, checks, strict=False) if r.status == "pass"]
    hid_ok = [
        t for t, r in zip(p.hidden_tests, checks[len(p.tests) :], strict=True) if r.status == "pass"
    ]
    if p.source == "mbpp" and (len(vis_ok) < len(p.tests) or len(hid_ok) < len(p.hidden_tests)):
        # An MBPP reference that fails its own asserts is a dataset bug, not a task.
        counts["reason"] = "reference_fails"
        return None, [], counts
    if not vis_ok:
        counts["reason"] = "no_tests"
        return None, [], counts

    gen = inputs.generated_inputs(list(p.tests) + list(p.hidden_tests), p.entry_point, cap=150)
    near_cands = list(dict.fromkeys(list(p.extra_inputs) + gen))
    near = _vet_inputs(ref, p.entry_point, p.setup, near_cands)
    seeds = [s for t in list(p.tests) + list(p.hidden_tests) if (s := _own_args(t, p.entry_point))]
    fuzz_cands = [
        x
        for x in inputs.fuzz_inputs(seeds + list(p.extra_inputs[:50]), n=350, key=p.key)
        if x not in near
    ]
    fuzz = _vet_inputs(ref, p.entry_point, p.setup, fuzz_cands)
    hidden = near + fuzz
    cands = near_cands + fuzz_cands
    rec = ProblemRecord(
        key=p.key,
        source=p.source,
        pid=p.pid,
        text=p.text,
        entry_point=p.entry_point,
        reference=ref,
        tests=vis_ok,
        setup=p.setup,
        hidden_tests=hid_ok,
        hidden_inputs=hidden,
        n_near_inputs=len(near),
        dropped_tests=len(p.tests) - len(vis_ok),
        dropped_inputs=len(cands) - len(hidden),
        slowest_assert_s=max((r.seconds for r in checks if r.status == "pass"), default=0.0),
    )

    muts = [m for m in operators.inject(p.reference) if m.code != ref]
    counts["mutants"] = len(muts)
    if not muts:
        counts["reason"] = "no_mutants"
        return rec, [], counts
    rows = sandbox.run_asserts(
        [m.code for m in muts],
        vis_ok,
        p.setup,
        stop_on_first_failure=False,
        item_timeout=rec.item_budget,
    )
    tasks: list[Task] = []
    for m, row in zip(muts, rows, strict=True):
        status = [r.status for r in row]
        if not any(s in FAILED for s in status):
            counts["survived"] += 1
            continue
        fault = sorted(diffmetrics.changed_old_lines(m.code, ref))
        tasks.append(
            Task(
                id=f"{p.key}/{m.where}",
                problem=p.key,
                kind=m.kind,
                where=m.where,
                buggy=m.code,
                fault_lines=fault,
                visible_status=status,
            )
        )
    if not tasks:
        counts["reason"] = "all_survived"
    return rec, tasks, counts


def build(
    problems: Iterable[data.Problem], workers: int = 8, progress: bool = False
) -> tuple[list[ProblemRecord], list[Task], BuildStats]:
    probs = list(problems)
    stats = BuildStats(problems_seen=len(probs))
    recs: list[ProblemRecord] = []
    tasks: list[Task] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, (rec, ts, c) in enumerate(pool.map(build_problem, probs), 1):
            stats.mutants += c["mutants"]
            stats.mutants_survived += c["survived"]
            if c["reason"] == "reference_fails":
                stats.dropped_reference_fails += 1
            elif c["reason"] == "no_tests":
                stats.dropped_no_tests += 1
            elif c["reason"] == "no_mutants":
                stats.dropped_no_mutants += 1
            elif c["reason"] == "all_survived":
                stats.dropped_all_survived += 1
            if rec is not None and ts:
                recs.append(rec)
                tasks.extend(ts)
            if progress and i % 50 == 0:
                print(f"  {i}/{len(probs)} problems, {len(tasks)} tasks", flush=True)
    stats.problems_kept = len(recs)
    stats.tasks = len(tasks)
    return recs, tasks, stats


# --------------------------------------------------------------------------- storage


def save(path: Path, rows: Iterable[object], provenance: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # mtime=0 keeps the gzip byte-identical across rebuilds, so git sees no change
    # unless the content changed.
    with (
        open(path, "wb") as raw,
        gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz,
    ):
        gz.write((json.dumps({"_provenance": provenance}) + "\n").encode("utf-8"))
        for r in rows:
            gz.write((json.dumps(asdict(r), sort_keys=True) + "\n").encode("utf-8"))  # type: ignore[call-overload]


def _read(path: Path) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} is missing. Build it with:  uv run minimal-diff build-tasks"
        )
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return [d for line in fh if line.strip() and "_provenance" not in (d := json.loads(line))]


def problems_path(source: str) -> Path:
    return data.data_dir() / f"problems_{source}.jsonl.gz"


def tasks_path(source: str) -> Path:
    return data.data_dir() / f"tasks_{source}.jsonl.gz"


def load_problems(source: str) -> dict[str, ProblemRecord]:
    return {d["key"]: ProblemRecord(**d) for d in _read(problems_path(source))}


def load_tasks(source: str) -> list[Task]:
    return [Task(**d) for d in _read(tasks_path(source))]

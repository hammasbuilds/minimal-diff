"""Was a patch right? Decided against the reference, not against the tests the repairer saw.

A patch that passes the visible asserts is *plausible*. Whether it is *correct* is decided
three ways, in order:

- **exact**: after `ast.unparse` normalisation it is the reference, character for
  character. Certainly correct.
- **overfit**: some input separates it from the reference - a visible assert the
  repairer was not shown, a hidden assert, or a vetted hidden input on which the two
  return different values (or the patch raises, or hangs). Certainly wrong, with a
  witness to prove it.
- **no_witness**: neither. Probably equivalent, but equivalence is undecidable, so it is
  reported as its own bucket and never folded into either of the others.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from . import operators, sandbox
from .tasks import ProblemRecord


@dataclass(frozen=True)
class Verdict:
    label: str  # exact | overfit | no_witness
    witness: str = ""  # which check separated it, and how
    # "visible" | "hidden_assert" | "near_input" | "fuzz_input" | "hang": which check found
    # it. "hang" is a patch that did not finish on a hidden input the reference answers in
    # under tasks.VET_MAX_REF_SECONDS.
    found_by: str = ""


def judge(
    problem: ProblemRecord,
    patches: Sequence[str],
    visible_rows: Sequence[Sequence[sandbox.Result]] | None = None,
    item_timeout: float = sandbox.ITEM_TIMEOUT,
) -> list[Verdict]:
    """A verdict for every patch.

    `visible_rows`, when given, are each patch's results on `problem.tests` in the
    problem's own order (unreached asserts `skipped`), so they are not run twice. A patch
    shown only some of the visible asserts can still be proven wrong by the ones it was
    not shown.
    """
    ref = problem.reference
    out: list[Verdict | None] = [None] * len(patches)
    todo: list[int] = []
    for i, code in enumerate(patches):
        if operators.normalise(code) == ref:
            out[i] = Verdict("exact")
        else:
            todo.append(i)
    if not todo:
        return out  # type: ignore[return-value]

    if visible_rows is None:
        rows = sandbox.run_asserts(
            [patches[i] for i in todo], problem.tests, problem.setup, item_timeout=item_timeout
        )
    else:
        rows = [visible_rows[i] for i in todo]
    remaining = []
    for i, row in zip(todo, rows, strict=True):
        bad = sandbox.first_failure(row)
        if bad is not None:
            k = row.index(bad)
            out[i] = Verdict("overfit", f"visible assert {k} -> {bad.status}", "visible")
        else:
            remaining.append(i)

    if remaining and problem.hidden_tests:
        rows = sandbox.run_asserts(
            [patches[i] for i in remaining],
            problem.hidden_tests,
            problem.setup,
            item_timeout=item_timeout,
        )
        still = []
        for i, row in zip(remaining, rows, strict=True):
            bad = sandbox.first_failure(row)
            if bad is not None:
                out[i] = Verdict(
                    "overfit", f"hidden assert {row.index(bad)} -> {bad.status}", "hidden_assert"
                )
            else:
                still.append(i)
        remaining = still

    if remaining and problem.hidden_inputs:
        rows = sandbox.run_compare(
            ref,
            problem.entry_point,
            [patches[i] for i in remaining],
            problem.hidden_inputs,
            problem.setup,
            item_timeout=item_timeout,
        )
        for i, row in zip(remaining, rows, strict=True):
            bad = sandbox.first_failure(row, ok="same")
            if bad is None:
                out[i] = Verdict("no_witness")
                continue
            k = row.index(bad)
            arg = problem.hidden_inputs[k]
            arg = arg if len(arg) <= 120 else arg[:117] + "..."
            if bad.status == "differs":
                how = f"{problem.entry_point}({arg}): reference {bad.ref}, patch {bad.cand}"
            else:
                how = f"{problem.entry_point}({arg}): patch {bad.status}"
            if bad.status != "differs":
                found = "hang"
            else:
                found = "near_input" if k < problem.n_near_inputs else "fuzz_input"
            out[i] = Verdict("overfit", how, found)
    for i in remaining:
        if out[i] is None:
            out[i] = Verdict("no_witness")
    return out  # type: ignore[return-value]

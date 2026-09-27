"""Repair your own program: the same one-edit search, pointed at a file and some asserts.

There is no reference solution here, so nothing can say whether a patch is *correct* -
only that it passes the asserts given. The output says so, and says how many other
patches passed them just as well: that tie is the study's main warning.
"""

from __future__ import annotations

import ast
import difflib
from dataclasses import dataclass

from . import diffmetrics, operators, repair, sandbox


@dataclass(frozen=True)
class Plausible:
    where: str
    code: str
    size: diffmetrics.DiffSize


@dataclass(frozen=True)
class FixResult:
    program: str  # the input, normalised by ast.unparse
    already_passes: bool
    n_candidates: int
    plausible: list[Plausible]  # ranked by the chosen metric, then site order


def read_asserts(text: str) -> list[str]:
    """Every top-level `assert` in `text`, as its own source line."""
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        raise ValueError(f"the tests do not parse: {e}") from None
    out = [ast.unparse(n) for n in tree.body if isinstance(n, ast.Assert)]
    if not out:
        raise ValueError("no top-level `assert` statements found in the tests")
    return out


def fix(
    source: str,
    tests: list[str],
    setup: str = "",
    metric: str = repair.DEFAULT_METRIC,
    item_timeout: float = 2.0,
) -> FixResult:
    if operators.parse(source) is None:
        raise ValueError("the program does not parse as Python")
    if metric not in repair.METRICS:
        raise ValueError(f"unknown metric {metric!r}; expected one of {sorted(repair.METRICS)}")
    program = operators.normalise(source)
    [base] = sandbox.run_asserts([program], tests, setup, item_timeout=item_timeout)
    if all(r.status == "pass" for r in base):
        return FixResult(program, True, 0, [])
    edits = operators.neighbours(program)
    rows = sandbox.run_asserts([e.code for e in edits], tests, setup, item_timeout=item_timeout)
    found = [
        Plausible(e.where, e.code, diffmetrics.measure(program, e.code, set()))
        for e, row in zip(edits, rows, strict=True)
        if all(r.status == "pass" for r in row)
    ]
    fields = repair.METRICS[metric]

    def key(p: Plausible) -> tuple:
        d = p.size.as_dict()
        return tuple(d[f] if d[f] is not None else 10**6 for f in fields)

    ranked = sorted(found, key=key)  # stable: ties stay in site order
    return FixResult(program, False, len(edits), ranked)


def render(res: FixResult, metric: str, top: int = 5) -> str:
    if res.already_passes:
        return "every assert already passes; nothing to repair"
    if not res.plausible:
        return (
            f"none of the {res.n_candidates} one-edit changes makes every assert pass.\n"
            "This search only tries single operator swaps, +/-1 on integers and negated "
            "conditions; the bug may need more than one edit."
        )
    fields = repair.METRICS[metric]
    best = tuple(res.plausible[0].size.as_dict()[f] for f in fields)
    tied = sum(1 for p in res.plausible if tuple(p.size.as_dict()[f] for f in fields) == best)
    lines = [
        f"{len(res.plausible)} of {res.n_candidates} one-edit changes pass every assert "
        f"({tied} tied at the smallest size by {metric}).",
        "Passing the asserts is not the same as being correct: with no reference to check "
        "against, only more tests can tell these apart.",
        "",
    ]
    for p in res.plausible[:top]:
        z = p.size
        lines.append(f"  {p.where:<20} tokens={z.tokens} ast={z.ast_nodes} lines={z.lines}")
        for old, new in zip(res.program.splitlines(), p.code.splitlines(), strict=True):
            if old != new:
                lines += [f"      - {old.strip()}", f"      + {new.strip()}"]
    if len(res.plausible) > top:
        lines.append(f"  ... and {len(res.plausible) - top} more (--top to see them)")
    pick = res.plausible[0]
    lines += ["", f"smallest by {metric} (ties by position): {pick.where}", ""]
    lines += difflib.unified_diff(
        res.program.splitlines(),
        pick.code.splitlines(),
        "a/program.py",
        "b/program.py",
        lineterm="",
    )
    return "\n".join(lines)

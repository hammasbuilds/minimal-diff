"""Repair your own program: the same one-edit search, pointed at a file and some asserts.

There is no reference solution here, so nothing can say whether a patch is *correct* -
only that it passes the asserts given. The output says so, and says how many other
patches passed them just as well: that tie is the study's main warning.

Unlike the study, which compares `ast.unparse` forms, this edits the file itself: each
candidate rewrites only the span of the node it changes (`operators.neighbours(...,
splice=True)`), so comments, blank lines and formatting survive and the printed diff
applies to the original file with `git apply` or `patch -p1`.
"""

from __future__ import annotations

import ast
import difflib
import warnings
from collections.abc import Sequence
from dataclasses import dataclass, field

from . import diffmetrics, operators, repair, sandbox

# Errors that say the asserts cannot run against this program at all - a misspelt
# function name, a missing module - rather than that the program is wrong.
UNRUNNABLE = ("NameError", "ImportError", "ModuleNotFoundError", "SyntaxError")


class Unrunnable(ValueError):
    """Every assert errors on the unedited program in a way no edit to it can repair."""


@dataclass(frozen=True)
class Plausible:
    where: str
    code: str
    size: diffmetrics.DiffSize


@dataclass(frozen=True)
class FixResult:
    program: str  # the input, exactly as read
    already_passes: bool
    n_candidates: int
    plausible: list[Plausible]  # ranked by the chosen metric, then site order
    # (assert, what went wrong) for every assert the unedited program errors on
    baseline_errors: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class Asserts:
    tests: list[str]
    warnings: list[str]


def read_asserts(text: str) -> Asserts:
    """Every top-level `assert` in `text`, as its own source line.

    Asserts anywhere else (inside a function, a class, a loop) are not run; each is
    reported as a warning with its line, so a pytest-style file does not silently
    shrink to nothing.
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(text)
    except SyntaxError as e:
        raise ValueError(f"the tests do not parse: {e}") from None
    top = [n for n in tree.body if isinstance(n, ast.Assert)]
    nested = [
        n for n in ast.walk(tree) if isinstance(n, ast.Assert) and not any(n is t for t in top)
    ]
    notes = []
    if nested:
        where = ", ".join(str(n.lineno) for n in nested)
        notes.append(
            f"{len(nested)} assert(s) not at the top level were ignored (line {where}); "
            "only top-level asserts are run - move them out of functions, or pass them "
            "with --assert"
        )
    if not top:
        raise ValueError(
            "no top-level `assert` statements found in the tests"
            + (f" ({notes[0]})" if notes else "")
        )
    return Asserts([ast.unparse(n) for n in top], notes)


def check_assert(stmt: str) -> str:
    """One `--assert` argument: it must be exactly one assert statement.

    Anything else (`f(2) == 4` without `assert`) would run, do nothing and "pass".
    """
    try:
        tree = ast.parse(stmt)
    except SyntaxError as e:
        raise ValueError(f"--assert {stmt!r} does not parse: {e.msg}") from None
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.Assert):
        raise ValueError(
            f"--assert {stmt!r} is not one assert statement; write it as 'assert {stmt}'"
            if len(tree.body) == 1 and isinstance(tree.body[0], ast.Expr)
            else f"--assert {stmt!r} is not one assert statement"
        )
    return stmt


def fix(
    source: str,
    tests: list[str],
    setup: str = "",
    metric: str = repair.DEFAULT_METRIC,
    item_timeout: float = 2.0,
    sys_path: Sequence[str] = (),
) -> FixResult:
    """Try every one-edit change to `source`; keep those that pass every assert.

    `sys_path` folders are importable while the program runs (the CLI passes the
    program's own folder, so `import helper` next to it works). Raises `Unrunnable`
    when every assert errors on the unedited program with a name or import error.
    """
    if operators.parse(source) is None:
        raise ValueError("the program does not parse as Python")
    if metric not in repair.METRICS:
        raise ValueError(f"unknown metric {metric!r}; expected one of {sorted(repair.METRICS)}")
    if item_timeout <= 0:
        raise ValueError(f"the timeout must be positive, got {item_timeout}")
    [base] = sandbox.run_asserts(
        [source],
        tests,
        setup,
        stop_on_first_failure=False,
        item_timeout=item_timeout,
        sys_path=sys_path,
    )
    if all(r.status == "pass" for r in base):
        return FixResult(source, True, 0, [])
    errors = [
        (t, r.detail or r.status) for t, r in zip(tests, base, strict=True) if r.status == "error"
    ]
    if errors and len(errors) == len(tests) and all(d.startswith(UNRUNNABLE) for _, d in errors):
        raise Unrunnable(
            "the asserts cannot run against this program - every one errors before any "
            "edit could matter:\n" + "\n".join(f"  {t}\n    -> {d}" for t, d in errors)
        )
    edits = operators.neighbours(source, splice=True)
    rows = sandbox.run_asserts(
        [e.code for e in edits], tests, setup, item_timeout=item_timeout, sys_path=sys_path
    )
    found = [
        Plausible(e.where, e.code, diffmetrics.measure(source, e.code, set()))
        for e, row in zip(edits, rows, strict=True)
        if all(r.status == "pass" for r in row)
    ]
    fields = repair.METRICS[metric]

    def key(p: Plausible) -> tuple:
        d = p.size.as_dict()
        return tuple(d[f] if d[f] is not None else 10**6 for f in fields)

    ranked = sorted(found, key=key)  # stable: ties stay in site order
    return FixResult(source, False, len(edits), ranked, errors)


def unified_diff(old: str, new: str, name: str = "program.py") -> str:
    """A diff `git apply` and `patch -p1` accept, run from the file's folder.

    Lines keep their own endings, and a last line without one is marked the way diff
    marks it, so the patch applies to the file byte for byte.
    """
    out = []
    for line in difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True), f"a/{name}", f"b/{name}"
    ):
        if line.endswith(("\n", "\r")):
            out.append(line)
        else:
            out.append(line + "\n\\ No newline at end of file\n")
    return "".join(out)


def _changed_lines(old: str, new: str) -> list[str]:
    a, b = old.splitlines(), new.splitlines()
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag != "equal":
            out += [f"      - {ln.strip()}" for ln in a[i1:i2]]
            out += [f"      + {ln.strip()}" for ln in b[j1:j2]]
    return out


def render(res: FixResult, metric: str, top: int = 5, name: str = "program.py") -> str:
    if res.already_passes:
        return "every assert already passes; nothing to repair"
    lines = []
    if res.baseline_errors:
        lines.append(f"the unedited program errors (not just fails) on {len(res.baseline_errors)}:")
        for t, d in res.baseline_errors:
            lines += [f"  {t}", f"    -> {d}"]
        lines.append("")
    if not res.plausible:
        lines += [
            f"none of the {res.n_candidates} one-edit changes makes every assert pass.",
            "This search only tries single operator swaps, +/-1 on integers and negated "
            "conditions; the bug may need more than one edit.",
        ]
        return "\n".join(lines)
    fields = repair.METRICS[metric]
    best = tuple(res.plausible[0].size.as_dict()[f] for f in fields)
    tied = sum(1 for p in res.plausible if tuple(p.size.as_dict()[f] for f in fields) == best)
    lines += [
        f"{len(res.plausible)} of {res.n_candidates} one-edit changes pass every assert "
        f"({tied} tied at the smallest size by {metric}).",
        "Passing the asserts is not the same as being correct: with no reference to check "
        "against, only more tests can tell these apart.",
        "",
    ]
    for p in res.plausible[:top]:
        z = p.size
        lines.append(f"  {p.where:<20} tokens={z.tokens} ast={z.ast_nodes} lines={z.lines}")
        lines += _changed_lines(res.program, p.code)
    if len(res.plausible) > top:
        lines.append(f"  ... and {len(res.plausible) - top} more (--top to see them)")
    pick = res.plausible[0]
    lines += ["", f"smallest by {metric} (ties by position): {pick.where}", ""]
    return "\n".join(lines) + "\n" + unified_diff(res.program, pick.code, name)

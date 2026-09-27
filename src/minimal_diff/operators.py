"""Single-point program edits: the ones that inject a bug, and the ones that search for a fix.

**Injection** is copied from `mutate.py` in hammasbuilds/mbpp-false-accepts, operator for
operator, so a task here is exactly a mutant that repo counts: flip a comparison to its
neighbour (`<` to `<=`), swap an arithmetic or bitwise operator, swap `and`/`or`, add one
to an integer literal, negate an `if` condition. Only the first operator of a chained
comparison is touched, as there.

**Repair** is the neighbourhood a classical search-based repairer explores: at every site,
every *other* operator in the same family, integer literals nudged both ways, conditions
negated or un-negated. It is a strict superset of the inverse of every injection, so the
true fix is always one edit away. That is deliberate: the question this repo asks is not
whether the fix is reachable, it is whether the tests pick it out from the other
one-edit programs that also pass them.

Sites are addressed by position in a fixed pre-order walk, so the same index names the
same node in a deep copy of the tree.
"""

from __future__ import annotations

import ast
import copy
import warnings
from collections.abc import Callable, Iterator
from dataclasses import dataclass

# --- injection: verbatim from mbpp-false-accepts -----------------------------------------

CMP_SWAP = {
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
}

BIN_SWAP = {
    ast.Add: ast.Sub,
    ast.Sub: ast.Add,
    ast.Mult: ast.FloorDiv,
    ast.FloorDiv: ast.Mult,
    ast.Div: ast.Mult,
    ast.Mod: ast.FloorDiv,
    ast.Pow: ast.Mult,
    ast.BitAnd: ast.BitOr,
    ast.BitOr: ast.BitAnd,
}

BOOL_SWAP = {ast.And: ast.Or, ast.Or: ast.And}

# --- repair: the families the search moves within ----------------------------------------

CMP_FAMILIES = (
    (ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq),
    (ast.Is, ast.IsNot),
    (ast.In, ast.NotIn),
)
ARITH_FAMILIES = (
    (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow),
    (ast.BitAnd, ast.BitOr, ast.BitXor),
)


@dataclass(frozen=True)
class Edit:
    """One single-point edit and the program it produces."""

    code: str
    kind: str  # compare | binop | boolop | const | negate_if (+ repair-only kinds)
    where: str  # "<kind>@<site index>", plus ":<which alternative>" for repair edits


def parse(code: str) -> ast.Module | None:
    # MBPP is full of regexes in non-raw strings ("\d"); that is the dataset's style, not
    # something to print a SyntaxWarning about thousands of times.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        try:
            return ast.parse(code)
        except (SyntaxError, ValueError):
            return None


def normalise(code: str) -> str:
    """Source as `ast.unparse` prints it: formatting and comments do not count as edits."""
    tree = parse(code)
    return ast.unparse(tree) if tree is not None else code


def preorder(tree: ast.AST) -> list[ast.AST]:
    out: list[ast.AST] = []

    def walk(n: ast.AST) -> None:
        out.append(n)
        for c in ast.iter_child_nodes(n):
            walk(c)

    walk(tree)
    return out


def _apply(tree: ast.Module, index: int, change: Callable[[ast.AST], None]) -> str | None:
    t = copy.deepcopy(tree)
    change(preorder(t)[index])
    try:
        ast.fix_missing_locations(t)
        return ast.unparse(t)
    except (ValueError, AttributeError, RecursionError):
        return None


def _is_int(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int)
        and not isinstance(node.value, bool)
    )


def _set_op(attr: str, op: type, pos: int | None = None) -> Callable[[ast.AST], None]:
    def change(n: ast.AST) -> None:
        if pos is None:
            setattr(n, attr, op())
        else:
            getattr(n, attr)[pos] = op()

    return change


def _bump(delta: int) -> Callable[[ast.AST], None]:
    def change(n: ast.AST) -> None:
        n.value = n.value + delta  # type: ignore[attr-defined]

    return change


def _negate(n: ast.AST) -> None:
    n.test = ast.UnaryOp(op=ast.Not(), operand=n.test)  # type: ignore[attr-defined]


def _unnegate(n: ast.AST) -> None:
    n.test = n.test.operand  # type: ignore[attr-defined]


def _injection_changes(n: ast.AST) -> Iterator[tuple[str, str, Callable[[ast.AST], None]]]:
    """The one mutation mbpp-false-accepts applies at this node, if any."""
    if isinstance(n, ast.Compare) and n.ops and type(n.ops[0]) in CMP_SWAP:
        yield "compare", "", _set_op("ops", CMP_SWAP[type(n.ops[0])], 0)
    elif isinstance(n, ast.BinOp) and type(n.op) in BIN_SWAP:
        yield "binop", "", _set_op("op", BIN_SWAP[type(n.op)])
    elif isinstance(n, ast.BoolOp) and type(n.op) in BOOL_SWAP:
        yield "boolop", "", _set_op("op", BOOL_SWAP[type(n.op)])
    elif _is_int(n):
        yield "const", "", _bump(+1)
    elif isinstance(n, ast.If):
        yield "negate_if", "", _negate


def _family(op: type, families: tuple[tuple[type, ...], ...]) -> tuple[type, ...]:
    return next((f for f in families if op in f), ())


def _repair_changes(n: ast.AST) -> Iterator[tuple[str, str, Callable[[ast.AST], None]]]:
    """Every neighbouring edit at this node."""
    if isinstance(n, ast.Compare):
        for pos, op in enumerate(n.ops):
            for alt in _family(type(op), CMP_FAMILIES):
                if alt is not type(op):
                    yield "compare", f"{pos}:{alt.__name__}", _set_op("ops", alt, pos)
    elif isinstance(n, ast.BinOp | ast.AugAssign):
        kind = "binop" if isinstance(n, ast.BinOp) else "augassign"
        for alt in _family(type(n.op), ARITH_FAMILIES):
            if alt is not type(n.op):
                yield kind, alt.__name__, _set_op("op", alt)
    elif isinstance(n, ast.BoolOp):
        alt = BOOL_SWAP[type(n.op)]
        yield "boolop", alt.__name__, _set_op("op", alt)
    elif _is_int(n):
        yield "const", "+1", _bump(+1)
        yield "const", "-1", _bump(-1)
    elif isinstance(n, ast.If | ast.While | ast.IfExp):
        if isinstance(n.test, ast.UnaryOp) and isinstance(n.test.op, ast.Not):
            # Only un-negate. `not not x` as a condition is `x` again: a no-op edit that
            # ties with the real fix on every size measure and would be picked first.
            yield "unnegate_if", "", _unnegate
        else:
            yield "negate_if", "", _negate


def _edits(code: str, changes, limit: int | None = None) -> list[Edit]:
    tree = parse(code)
    if tree is None:
        return []
    seen = {ast.unparse(tree)}
    out: list[Edit] = []
    for idx, node in enumerate(preorder(tree)):
        for kind, detail, change in changes(node):
            src = _apply(tree, idx, change)
            if src is None:
                continue
            # An edit that unparses to something already produced changed nothing new.
            if src in seen:
                continue
            seen.add(src)
            where = f"{kind}@{idx}" + (f":{detail}" if detail else "")
            out.append(Edit(src, kind, where))
            if limit and len(out) >= limit:
                return out
    return out


def inject(code: str) -> list[Edit]:
    """Every single-point bug mbpp-false-accepts would inject into `code`."""
    return _edits(code, _injection_changes)


def neighbours(code: str, limit: int | None = None) -> list[Edit]:
    """Every single-point repair candidate for `code`, in site order."""
    return _edits(code, _repair_changes, limit)

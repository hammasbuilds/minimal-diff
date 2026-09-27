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

Sites are addressed by position in a fixed pre-order walk. Each edit is applied to the
one parsed tree in place and undone, not to a deep copy.
"""

from __future__ import annotations

import ast
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
    """One single-point edit and the program it produces.

    A spliced edit keeps only the span it rewrites and builds the program on demand, so
    enumerating tens of thousands of edits of a large file does not hold tens of thousands
    of copies of it.
    """

    kind: str  # compare | binop | boolop | const | negate_if (+ repair-only kinds)
    where: str  # "<kind>@<site index>", plus ":<which alternative>" for repair edits
    text: str | None = None  # the whole program, when it was produced by unparsing
    base: str | None = None  # the original program, for a spliced edit
    span: tuple[int, int, str] | None = None  # (start, end, replacement) into `base`

    @property
    def code(self) -> str:
        if self.text is not None:
            return self.text
        a, b, rep = self.span  # type: ignore[misc]
        return self.base[:a] + rep + self.base[b:]  # type: ignore[index]


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


def _is_int(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int)
        and not isinstance(node.value, bool)
    )


# A change mutates one node in place and returns (undo, the node whose source text it
# replaces, the node that replaces it). Mutating in place and undoing, rather than deep-copying
# the whole tree per edit, is what keeps enumeration from being quadratic in program size.
Change = Callable[[ast.AST], tuple[Callable[[], None], ast.AST, ast.AST]]


def _set_op(attr: str, op: type, pos: int | None = None) -> Change:
    def change(n: ast.AST):
        if pos is None:
            old = getattr(n, attr)
            setattr(n, attr, op())
            return (lambda: setattr(n, attr, old)), n, n
        ops = getattr(n, attr)
        old = ops[pos]
        ops[pos] = op()
        return (lambda: ops.__setitem__(pos, old)), n, n

    return change


def _bump(delta: int) -> Change:
    def change(n: ast.AST):
        old = n.value  # type: ignore[attr-defined]
        n.value = old + delta  # type: ignore[attr-defined]
        return (lambda: setattr(n, "value", old)), n, n

    return change


def _negate(n: ast.AST):
    old = n.test  # type: ignore[attr-defined]
    n.test = ast.UnaryOp(op=ast.Not(), operand=old)  # type: ignore[attr-defined]
    return (lambda: setattr(n, "test", old)), old, n.test  # type: ignore[attr-defined]


def _unnegate(n: ast.AST):
    old = n.test  # type: ignore[attr-defined]
    n.test = old.operand  # type: ignore[attr-defined]
    return (lambda: setattr(n, "test", old)), old, n.test  # type: ignore[attr-defined]


def _injection_changes(n: ast.AST) -> Iterator[tuple[str, str, Change]]:
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


def _repair_changes(n: ast.AST) -> Iterator[tuple[str, str, Change]]:
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


def _unparse(tree: ast.AST) -> str | None:
    try:
        ast.fix_missing_locations(tree)
        return ast.unparse(tree)
    except (ValueError, AttributeError, RecursionError):
        return None


class _Source:
    """Splices one node's new text into the original source, keeping every other character.

    AST positions are UTF-8 byte columns; offsets here are characters.
    """

    def __init__(self, code: str):
        self.code = code
        self.lines = code.splitlines(keepends=True)
        self.starts = [0]
        for ln in self.lines:
            self.starts.append(self.starts[-1] + len(ln))

    def _offset(self, line: int, col_bytes: int) -> int:
        text = self.lines[line - 1]
        return self.starts[line - 1] + len(text.encode("utf-8")[:col_bytes].decode("utf-8"))

    def _span(self, n: ast.AST) -> tuple[int, int]:
        return (
            self._offset(n.lineno, n.col_offset),  # type: ignore[attr-defined]
            self._offset(n.end_lineno, n.end_col_offset),  # type: ignore[attr-defined]
        )

    def splice(
        self,
        old: ast.AST,
        new: ast.AST,
        parent: ast.AST | None,
        tree: ast.Module,
        fstring: ast.AST | None = None,
    ) -> tuple[int, int, str] | str | None:
        """(start, end, replacement), or the whole unparsed program as a fallback."""
        try:
            a, b = self._span(old)
            fa, fb = self._span(fstring) if fstring is not None else (a, b)
        except (AttributeError, IndexError, TypeError):
            return _unparse(tree)
        rep = _unparse(new)
        if rep is None:
            return None
        # Parenthesise only where the edited parent, unparsed, needs it and the source does
        # not already have the brackets.
        if isinstance(parent, ast.expr):
            whole = _unparse(parent) or ""
            bracketed = self.code[a - 1 : a] == "(" and self.code[b : b + 1] == ")"
            if f"({rep})" in whole and not bracketed:
                rep = f"({rep})"
        # No re-parse of the whole file per edit (that made enumeration quadratic): the
        # replacement is `ast.unparse` output, parenthesised as its parent needs, so it is
        # valid wherever the old node was. Inside an f-string its quotes may clash, so only
        # that f-string is re-parsed, and an edit that does not fit is dropped.
        if fstring is not None:
            segment = self.code[fa:a] + rep + self.code[b:fb]
            if parse(f"({segment})") is None:
                return None
        return (a, b, rep)


def _edits(
    code: str,
    changes,
    limit: int | None = None,
    splice: bool = False,
    progress: Callable[[int, int], None] | None = None,
) -> list[Edit]:
    """Every edit `changes` offers, deduplicated, in site order.

    `splice=False` returns each edited program in `ast.unparse` form - what the study
    compares against normalised references. `splice=True` rewrites only the edited node's
    span in `code` and keeps everything else, comments and formatting included: what a
    person repairing their own file needs. `progress(done, total)` is called every 500
    nodes.
    """
    tree = parse(code)
    if tree is None:
        return []
    nodes = preorder(tree)
    parents = {id(c): n for n in nodes for c in ast.iter_child_nodes(n)}
    src = _Source(code) if splice else None
    # Every node inside an f-string, mapped to the outermost f-string holding it.
    fstring: dict[int, ast.AST] = {}
    for n in nodes:
        if isinstance(n, ast.JoinedStr) and id(n) not in fstring:
            for c in ast.walk(n):
                fstring[id(c)] = n
    seen: set = {code if splice else ast.unparse(tree)}
    out: list[Edit] = []
    for idx, node in enumerate(nodes):
        if progress is not None and idx % 500 == 0:
            progress(idx, len(nodes))
        for kind, detail, change in changes(node):
            undo, old, new = change(node)
            try:
                if src is not None:
                    text = src.splice(old, new, parents.get(id(old)), tree, fstring.get(id(old)))
                else:
                    text = _unparse(tree)
            finally:
                undo()
            if text is None:
                continue
            if (
                isinstance(text, tuple)
                and src is not None
                and src.code[text[0] : text[1]] == text[2]
            ):
                continue  # rewrote a span to what it already was
            # An edit that produces something already produced changed nothing new.
            if text in seen:
                continue
            seen.add(text)
            where = f"{kind}@{idx}" + (f":{detail}" if detail else "")
            if isinstance(text, tuple):
                out.append(Edit(kind, where, base=code, span=text))
            else:
                out.append(Edit(kind, where, text=text))
            if limit and len(out) >= limit:
                return out
    return out


def inject(code: str) -> list[Edit]:
    """Every single-point bug mbpp-false-accepts would inject into `code`."""
    return _edits(code, _injection_changes)


def neighbours(
    code: str,
    limit: int | None = None,
    splice: bool = False,
    progress: Callable[[int, int], None] | None = None,
) -> list[Edit]:
    """Every single-point repair candidate for `code`, in site order (see `_edits`)."""
    return _edits(code, _repair_changes, limit, splice, progress)

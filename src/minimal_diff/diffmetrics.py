"""How big is a patch? Four measures, because each one lies in a different way.

- **lines changed**: what a reviewer sees. Per diff hunk it counts the larger side, so
  changing one line is 1, not "1 removed + 1 added". Reformatting inflates it.
- **tokens changed**: the same, over Python tokens. `<` to `<=` is 1; renaming a variable
  on every line it appears is one per use. Blind to whitespace, comments and brackets that
  only group (which `ast.unparse` adds or drops as precedence needs); `tokens_raw` counts
  those brackets too, as a robustness check.
- **AST edit distance**: Zhang-Shasha tree edit distance over the syntax trees, unit
  costs. `<` to `<=` is 1 (a relabel); wrapping a condition in `not` is 2 (insert
  `UnaryOp` and `Not`). Blind to formatting entirely. Written out here rather than taken
  from a library so the repo keeps zero runtime dependencies.
- **unrelated lines**: changed lines outside the known faulty region. A fix can be small
  and still touch code that was never broken.
"""

from __future__ import annotations

import ast
import difflib
import io
import keyword
import tokenize
import warnings
from dataclasses import asdict, dataclass

_SKIP_TOKENS = {
    tokenize.COMMENT,
    tokenize.NL,
    tokenize.NEWLINE,
    tokenize.ENCODING,
    tokenize.ENDMARKER,
}


@dataclass(frozen=True)
class DiffSize:
    lines: int
    tokens: int  # grouping brackets not counted (see `tokens`)
    ast_nodes: int | None  # None when either side does not parse
    unrelated_lines: int
    touches_fault: bool
    tokens_raw: int  # every token, grouping brackets included: the robustness check

    def as_dict(self) -> dict:
        return asdict(self)


def _hunks(a: list[str], b: list[str]) -> list[tuple[str, int, int, int, int]]:
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    return [op for op in sm.get_opcodes() if op[0] != "equal"]


def lines_changed(old: str, new: str) -> int:
    a, b = old.splitlines(), new.splitlines()
    return sum(max(i2 - i1, j2 - j1) for _, i1, i2, j1, j2 in _hunks(a, b))


def changed_old_lines(old: str, new: str) -> set[int]:
    """1-based line numbers in `old` that a diff to `new` touches.

    A pure insertion has no old line of its own; it is charged to the line it follows
    (or line 1 at the very top), so an inserted line inside a region counts as in it.
    """
    out: set[int] = set()
    for tag, i1, i2, _, _ in _hunks(old.splitlines(), new.splitlines()):
        if tag == "insert":
            out.add(max(i1, 1))
        else:
            out.update(range(i1 + 1, i2 + 1))
    return out


def _opens_a_call(prev: tokenize.TokenInfo | None) -> bool:
    """Is a `(` after `prev` a call's (or a def's, a class's bases)? Otherwise it groups."""
    if prev is None:
        return False
    if prev.type == tokenize.NAME:
        return not keyword.iskeyword(prev.string)
    return prev.type == tokenize.STRING or prev.string in (")", "]", "}")


def tokens(code: str, grouping: bool = False) -> list[str] | None:
    """Python tokens of `code`, without comments, newlines or (by default) grouping brackets.

    A `(` that does not open a call, a definition's parameters or a class's bases - and its
    matching `)` - only groups. Nobody types those as part of a one-operator edit, but
    `ast.unparse` (and the splice) adds or drops them wherever the new operator's
    precedence needs: swapping `*` for `+` in `a + b * c` gives `a + (b + c)`, which is
    one token changed, not three. `grouping=True` keeps them (`tokens_raw`).
    """
    out: list[str] = []
    stack: list[bool] = []  # per open `(`: does it only group?
    prev: tokenize.TokenInfo | None = None
    try:
        for t in tokenize.generate_tokens(io.StringIO(code).readline):
            if t.type in _SKIP_TOKENS:
                continue
            drop = False
            if t.type == tokenize.OP and t.string == "(":
                stack.append(not _opens_a_call(prev))
                drop = stack[-1]
            elif t.type == tokenize.OP and t.string == ")":
                drop = stack.pop() if stack else False
            prev = t
            if drop and not grouping:
                continue
            is_indent = t.type in (tokenize.INDENT, tokenize.DEDENT)
            out.append(tokenize.tok_name[t.type] if is_indent else t.string)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None
    return out


def tokens_changed(old: str, new: str, grouping: bool = False) -> int:
    a, b = tokens(old, grouping), tokens(new, grouping)
    if a is None or b is None:  # fall back to whitespace-split words
        a, b = old.split(), new.split()
    return sum(max(i2 - i1, j2 - j1) for _, i1, i2, j1, j2 in _hunks(a, b))


# --------------------------------------------------------------------------- tree edit distance


def _label(n: ast.AST) -> str:
    """Node type plus every field that is not itself a child node.

    That covers names (`Name.id`, `FunctionDef.name`, `Global.names`), literals (with
    their type, so `1` and `True` differ) and flags such as `ImportFrom.level`.
    """
    parts = [type(n).__name__]
    for field, v in ast.iter_fields(n):
        if isinstance(v, ast.AST) or (isinstance(v, list) and v and isinstance(v[0], ast.AST)):
            continue
        if field == "type_comment" or v is None or v == []:
            continue
        parts.append(f"{field}={type(v).__name__}:{v!r}")
    return "|".join(parts)


def _children(n: ast.AST) -> list[ast.AST]:
    # Load/Store/Del contexts are bookkeeping, not code anyone wrote.
    return [c for c in ast.iter_child_nodes(n) if not isinstance(c, ast.expr_context)]


class _Tree:
    """Post-order arrays Zhang-Shasha needs: labels, leftmost leaf, key roots."""

    def __init__(self, root: ast.AST):
        self.labels: list[str] = []
        self.lml: list[int] = []

        def walk(n: ast.AST) -> int:
            first = None
            for c in _children(n):
                leftmost = walk(c)
                if first is None:
                    first = leftmost
            idx = len(self.labels)
            self.labels.append(_label(n))
            self.lml.append(idx if first is None else first)
            return self.lml[idx]

        walk(root)
        seen: dict[int, int] = {}
        for i, leftmost in enumerate(self.lml):
            seen[leftmost] = i  # the highest node with each leftmost leaf
        self.keyroots = sorted(seen.values())


def tree_edit_distance(a: ast.AST, b: ast.AST) -> int:
    """Zhang & Shasha (1989), unit insert/delete/relabel costs."""
    ta, tb = _Tree(a), _Tree(b)
    la, lb = ta.lml, tb.lml
    td = [[0] * len(tb.labels) for _ in ta.labels]
    for i in ta.keyroots:
        for j in tb.keyroots:
            li, lj = la[i], lb[j]
            m, n = i - li + 2, j - lj + 2
            fd = [[0] * n for _ in range(m)]
            for x in range(1, m):
                fd[x][0] = fd[x - 1][0] + 1
            for y in range(1, n):
                fd[0][y] = fd[0][y - 1] + 1
            for x in range(1, m):
                ix = li + x - 1
                for y in range(1, n):
                    jy = lj + y - 1
                    if la[ix] == li and lb[jy] == lj:
                        cost = 0 if ta.labels[ix] == tb.labels[jy] else 1
                        fd[x][y] = min(fd[x - 1][y] + 1, fd[x][y - 1] + 1, fd[x - 1][y - 1] + cost)
                        td[ix][jy] = fd[x][y]
                    else:
                        p, q = la[ix] - li, lb[jy] - lj
                        fd[x][y] = min(fd[x - 1][y] + 1, fd[x][y - 1] + 1, fd[p][q] + td[ix][jy])
    return td[-1][-1]


def _focus(a: ast.AST, b: ast.AST) -> tuple[ast.AST, ast.AST]:
    """Walk down while the two trees differ in exactly one child.

    Zhang-Shasha is quartic in the worst case and a HumanEval solution with its
    docstring takes over a second. A single-point patch leaves every sibling of the
    edited node untouched, so the distance of the whole trees is the distance of the one
    differing pair of subtrees; `tests/test_diffmetrics.py` checks this shortcut against
    the full computation on real mutants.
    """
    while _label(a) == _label(b):
        ca, cb = _children(a), _children(b)
        if len(ca) != len(cb):
            break
        differ = [(x, y) for x, y in zip(ca, cb, strict=True) if ast.dump(x) != ast.dump(y)]
        if len(differ) != 1:
            break
        a, b = differ[0]
    return a, b


def ast_distance(old: str, new: str, exact: bool = False) -> int | None:
    """Tree edit distance between two programs; None if either does not parse.

    `exact=True` skips the single-differing-child shortcut (used by the tests).
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        try:
            a, b = ast.parse(old), ast.parse(new)
        except (SyntaxError, ValueError):
            return None
    if not exact:
        a, b = _focus(a, b)
    return tree_edit_distance(a, b)


def measure(old: str, new: str, fault_lines: set[int] | frozenset[int]) -> DiffSize:
    """Every size measure of the patch `old -> new`; `fault_lines` are 1-based lines of `old`."""
    touched = changed_old_lines(old, new)
    return DiffSize(
        lines=lines_changed(old, new),
        tokens=tokens_changed(old, new),
        ast_nodes=ast_distance(old, new),
        unrelated_lines=len(touched - set(fault_lines)),
        touches_fault=bool(touched & set(fault_lines)),
        tokens_raw=tokens_changed(old, new, grouping=True),
    )

import ast

import pytest

from minimal_diff import diffmetrics as dm
from minimal_diff import operators

from .conftest import CLAMP_REFERENCE
from .test_operators import PROGRAMS

BASE = "def f(x):\n    if x < 1:\n        return x + 1\n    return 0"


@pytest.mark.parametrize(
    ("new", "lines", "tokens", "nodes"),
    [
        (BASE, 0, 0, 0),
        (BASE.replace("x < 1", "x <= 1"), 1, 1, 1),  # relabel one operator
        (BASE.replace("if x < 1", "if not x < 1"), 1, 1, 2),  # insert UnaryOp + Not
        (BASE.replace("return 0", "return 0\n    return 1"), 1, 2, 2),  # insert Return + Constant
        (BASE.replace("x", "y"), 3, 3, 3),  # a rename touches all three uses
    ],
)
def test_sizes_on_known_edits(new, lines, tokens, nodes):
    s = dm.measure(BASE, new, {2})
    assert (s.lines, s.tokens, s.ast_nodes) == (lines, tokens, nodes)


def test_unrelated_lines_and_fault_contact():
    fix = dm.measure(BASE, BASE.replace("x < 1", "x <= 1"), {2})
    assert fix.touches_fault and fix.unrelated_lines == 0
    elsewhere = dm.measure(BASE, BASE.replace("return 0", "return 1"), {2})
    assert not elsewhere.touches_fault and elsewhere.unrelated_lines == 1
    both = dm.measure(BASE, BASE.replace("x < 1", "x <= 1").replace("return 0", "return 1"), {2})
    assert both.touches_fault and both.unrelated_lines == 1


def test_pure_insertion_is_charged_to_the_line_it_follows():
    new = BASE.replace("    if x < 1:", "    if x < 1:\n        pass")
    assert dm.changed_old_lines(BASE, new) == {2}


def test_tree_edit_distance_textbook_cases():
    t = lambda s: ast.parse(s)  # noqa: E731
    assert dm.tree_edit_distance(t("a"), t("a")) == 0
    assert dm.tree_edit_distance(t("a"), t("b")) == 1
    assert dm.tree_edit_distance(t("a"), t("a\nb")) == 2  # Expr + Name
    assert dm.tree_edit_distance(t("f(a)"), t("f(a, b)")) == 1
    # Symmetric under unit costs.
    x, y = t("a + b * c"), t("(a - b) * c")
    assert dm.tree_edit_distance(x, y) == dm.tree_edit_distance(y, x)


def test_labels_see_literal_types_and_scalar_fields():
    assert dm.ast_distance("x = 1", "x = True") == 1
    assert dm.ast_distance("global a", "global b") == 1
    assert dm.ast_distance("from . import m", "from .. import m") == 1


@pytest.mark.parametrize("program", PROGRAMS)
def test_single_child_shortcut_matches_the_full_computation(program):
    """The speed-up in `ast_distance` must never change an answer."""
    for bug in operators.inject(program):
        for fix in operators.neighbours(bug.code, limit=12):
            assert dm.ast_distance(bug.code, fix.code) == dm.ast_distance(
                bug.code, fix.code, exact=True
            )


def test_unparsable_side_gives_no_ast_distance_but_still_counts_lines():
    s = dm.measure(CLAMP_REFERENCE, "def (:", set())
    assert s.ast_nodes is None and s.lines > 0 and s.tokens > 0

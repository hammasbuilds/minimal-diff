import ast

import pytest

from minimal_diff import operators
from minimal_diff.diffmetrics import changed_old_lines

from .conftest import CLAMP_REFERENCE

PROGRAMS = [
    CLAMP_REFERENCE,
    "def f(a, b):\n    return a * b - a // b + a % b\n",
    "def g(xs):\n    return [x for x in xs if x is not None and x in (1, 2)]\n",
    "def h(n):\n    i = 0\n    while i < n:\n        i += 2\n    return i if i != n else -1\n",
    "def k(s):\n    if not s:\n        return 0\n    return len(s) ** 2 & 7 | 1\n",
    "def m(x, y):\n    return x / y if x >= y or y <= 0 else x - 1\n",
]


def test_injection_kinds_on_a_known_program():
    kinds = sorted(e.kind for e in operators.inject(CLAMP_REFERENCE))
    # `x > 0`, `total > cap` -> 2 compares; 0 twice -> 2 consts; two ifs.
    # `total += x` is an AugAssign, which mbpp-false-accepts never mutated.
    assert kinds == ["compare", "compare", "const", "const", "negate_if", "negate_if"]


def test_injected_programs_differ_from_the_reference_in_one_place():
    ref = operators.normalise(CLAMP_REFERENCE)
    for e in operators.inject(CLAMP_REFERENCE):
        diff = [a for a, b in zip(ref.splitlines(), e.code.splitlines(), strict=True) if a != b]
        assert len(diff) == 1, e.where


@pytest.mark.parametrize("program", PROGRAMS)
def test_every_injected_bug_is_one_repair_edit_from_the_reference(program):
    """The search space contains the true fix, for every operator - the study's premise."""
    ref = operators.normalise(program)
    bugs = operators.inject(program)
    assert bugs
    for bug in bugs:
        fixes = {e.code for e in operators.neighbours(bug.code)}
        assert ref in fixes, f"{bug.where} has no inverse among the repair edits"


def test_neighbours_are_distinct_and_never_the_input():
    code = operators.normalise(PROGRAMS[1])
    ns = operators.neighbours(code)
    assert len({n.code for n in ns}) == len(ns)
    assert code not in {n.code for n in ns}


def test_every_neighbour_changes_exactly_one_line():
    code = operators.normalise(CLAMP_REFERENCE)
    for e in operators.neighbours(code):
        assert len(changed_old_lines(code, e.code)) == 1, e.where


def test_negation_is_undone_by_unnegation():
    code = "def k(s):\n    if not s:\n        return 0\n    return 1"
    kinds = {e.kind for e in operators.neighbours(code)}
    assert "unnegate_if" in kinds
    un = next(e for e in operators.neighbours(code) if e.kind == "unnegate_if")
    assert "if s:" in un.code


def test_constants_move_both_ways_and_bools_are_left_alone():
    code = "def f():\n    return 5 if True else 0"
    consts = sorted(e.code for e in operators.neighbours(code) if e.kind == "const")
    assert any("return 6" in c for c in consts) and any("return 4" in c for c in consts)
    assert all("True" in c for c in consts)


def test_unparsable_code_has_no_edits():
    assert operators.inject("def (:") == []
    assert operators.neighbours("def (:") == []
    assert operators.normalise("def (:") == "def (:"


def test_preorder_indices_are_stable_across_copies():
    import copy

    tree = ast.parse(PROGRAMS[4])
    a = [type(n).__name__ for n in operators.preorder(tree)]
    b = [type(n).__name__ for n in operators.preorder(copy.deepcopy(tree))]
    assert a == b


def test_double_negation_is_never_a_candidate():
    """`if not not x` behaves exactly like `if x`: a no-op that would tie with real fixes."""
    for code in ("def k(s):\n    if not s:\n        return 0\n    return 1", PROGRAMS[0]):
        assert not any("not not" in e.code for e in operators.neighbours(code))


def test_repair_labels_are_unique_per_candidate():
    ns = operators.neighbours(operators.normalise(PROGRAMS[1]))
    assert len({e.where for e in ns}) == len(ns)

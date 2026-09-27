from dataclasses import replace

import pytest

from minimal_diff import operators, oracle, repair, tasks


@pytest.fixture(scope="module")
def built():
    from minimal_diff import data

    from .conftest import CLAMP_REFERENCE, CLAMP_TESTS

    p = data.Problem("mbpp", 9001, "clamp", CLAMP_REFERENCE, "clamp_sum", CLAMP_TESTS)
    rec, ts, counts = tasks.build_problem(p)
    return rec, ts, counts


def test_build_keeps_only_mutants_with_a_failing_test(built):
    rec, ts, counts = built
    assert rec is not None and rec.reference == operators.normalise(rec.reference)
    assert counts["mutants"] == len(ts) + counts["survived"]
    assert ts and all(t.failing for t in ts)
    assert all(t.buggy != rec.reference for t in ts)


def test_fault_lines_point_at_the_mutation(built):
    rec, ts, _ = built
    for t in ts:
        ref_lines, bug_lines = rec.reference.splitlines(), t.buggy.splitlines()
        assert [
            i + 1 for i, (a, b) in enumerate(zip(ref_lines, bug_lines, strict=True)) if a != b
        ] == t.fault_lines


def test_hidden_inputs_are_vetted_on_the_reference(built):
    rec, _, _ = built
    assert rec.hidden_inputs, "perturbed assert arguments should give some inputs"
    assert rec.hidden_inputs[0] == "[1, 2, 3], 10"  # the assert's own arguments come first


def test_reference_that_fails_its_own_asserts_is_dropped():
    from minimal_diff import data

    p = data.Problem("mbpp", 1, "", "def f(x):\n    return x\n", "f", ("assert f(1) == 2",))
    rec, ts, counts = tasks.build_problem(p)
    assert rec is None and ts == [] and counts["reason"] == "reference_fails"


def test_oracle_three_buckets(built):
    rec, _, _ = built
    exact = rec.reference
    # Passes the three visible asserts but not `clamp_sum([0, 3], 3)`-style boundaries:
    wrong = exact.replace("if total > cap", "if total >= cap + 1")
    # Equivalent rewrite of the reference: nothing can separate it.
    same = exact.replace("if x > 0", "if 0 < x")
    broken = exact.replace("return total", "return total + 1")
    v = oracle.judge(rec, [exact, same, broken])
    assert [x.label for x in v] == ["exact", "no_witness", "overfit"]
    assert v[2].witness.startswith("visible assert")
    [w] = oracle.judge(rec, [wrong])
    # integers only: `>= cap + 1` is the same as `> cap`
    assert w.label == "no_witness"


def test_oracle_finds_a_hidden_input_witness(built):
    rec, _, _ = built
    # Differs only when the list is empty -- an input the asserts never use but the
    # generated inputs do (the list perturbations include []).
    patch = rec.reference.replace("total = 0", "total = 0\n    if not xs:\n        return -1")
    [v] = oracle.judge(rec, [patch])
    assert v.label == "overfit"
    assert "clamp_sum(" in v.witness and "reference 0, patch -1" in v.witness


def test_repair_finds_the_known_fix_and_records_everything(built):
    rec, ts, _ = built
    for t in ts:
        row = repair.repair_task(t, rec)
        assert row["truth_in_space"]
        for g, sub in row["subsets"].items():
            assert sub["truth_plausible"], (t.id, g)  # the fix passes every visible assert
            assert sub["smallest"] is not None
        # Showing more asserts can only shrink the plausible set.
        n = [row["subsets"][g]["n_plausible"] for g in ("k1", "k3", "all")]
        assert n[0] >= n[1] >= n[2] >= 1


def test_visible_subsets_always_include_a_failing_test(built):
    _, ts, _ = built
    for t in ts:
        subs = repair.visible_subsets(t, 3)
        assert subs["k1"] == [t.failing[0]]
        assert sorted(subs["all"]) == [0, 1, 2]
        assert subs["k3"][0] == t.failing[0]


def _cand(i, verdict, tokens, at_fault=True):
    size = {
        "tokens": tokens,
        "ast_nodes": tokens,
        "lines": 1,
        "unrelated_lines": 0 if at_fault else 1,
        "touches_fault": at_fault,
    }
    return repair.Candidate(
        i, f"x@{i}", "compare", [True], size, verdict=verdict, is_truth=verdict == "exact"
    )


def test_selection_policies():
    cands = [_cand(0, "overfit", 1, at_fault=False), _cand(1, "exact", 1), _cand(2, "overfit", 3)]
    assert repair.select(cands, [0], "smallest").index == 0  # a tie goes to the earlier site
    assert repair.select(cands, [0], "at_fault").index == 1
    s = repair.summarise_subset(cands, [0])
    assert s["n_plausible"] == 3 and s["n_tied_smallest"] == 2
    assert s["random_exact"] == pytest.approx(1 / 3)
    assert s["overfit_as_small_as_truth"] is True
    # with a random tie-break the smallest policy is right half the time here
    assert s["tied_exact"] == pytest.approx(0.5) and s["tied_overfit"] == pytest.approx(0.5)
    with pytest.raises(ValueError):
        repair.select(cands, [0], "largest")


def test_task_roundtrip_through_storage(built, tmp_path, monkeypatch):
    rec, ts, _ = built
    monkeypatch.setenv("MINIMAL_DIFF_DATA", str(tmp_path))
    tasks.save(tasks.problems_path("mbpp"), [rec], {"test": True})
    tasks.save(tasks.tasks_path("mbpp"), ts, {"test": True})
    assert tasks.load_problems("mbpp") == {rec.key: rec}
    assert tasks.load_tasks("mbpp") == ts
    # Deterministic bytes: rebuilding identical content does not change the file.
    before = tasks.tasks_path("mbpp").read_bytes()
    tasks.save(tasks.tasks_path("mbpp"), ts, {"test": True})
    assert tasks.tasks_path("mbpp").read_bytes() == before
    assert replace(ts[0], kind="x") != ts[0]

import json
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


def _cand(i, verdict, tokens, at_fault=True, nodes=None):
    size = {
        "tokens": tokens,
        "tokens_raw": tokens,
        "ast_nodes": tokens if nodes is None else nodes,
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
    t = s["by_metric"]["tokens"]
    assert s["n_plausible"] == 3 and t["n_tied"] == 2
    assert s["random_exact"] == pytest.approx(1 / 3)
    assert t["overfit_as_small_as_truth"] is True and t["truth_strictly_smallest"] is False
    # with a random tie-break the smallest policy is right half the time here
    assert t["tied_exact"] == pytest.approx(0.5) and t["tied_overfit"] == pytest.approx(0.5)
    assert t["site_verdict"] == "overfit"
    with pytest.raises(ValueError):
        repair.select(cands, [0], "largest")


def test_the_size_metric_decides_between_an_unnegation_and_an_operator_swap():
    """The reviewer's case: removing `not` is 1 token but 2 AST nodes; a comparison swap is
    1 of each. `tokens+ast` always prefers the swap, `tokens` leaves it to site order."""
    swap = _cand(0, "overfit", 1, nodes=1)
    unnegate = _cand(1, "exact", 1, nodes=2)
    cands = [swap, unnegate]
    assert repair.select(cands, [0], "smallest", "tokens+ast").verdict == "overfit"
    assert repair.select(cands, [0], "smallest", "ast").verdict == "overfit"
    by = repair.summarise_subset(cands, [0])["by_metric"]
    assert by["tokens"]["n_tied"] == 2 and by["tokens"]["tied_exact"] == pytest.approx(0.5)
    assert by["tokens+ast"]["n_tied"] == 1 and by["tokens+ast"]["tied_exact"] == 0.0
    # with the swap later in the file, tokens + site order picks the fix
    late_swap = _cand(2, "overfit", 1, nodes=1)
    assert repair.select([unnegate, late_swap], [0], "smallest", "tokens").verdict == "exact"


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


def test_vetting_drops_inputs_the_reference_is_slow_on():
    ref = "import time\ndef f(n):\n    if n > 5:\n        time.sleep(0.2)\n    return n\n"
    kept = tasks._vet_inputs(ref, "f", "", ["1", "3", "9", "'x' + 1"])
    assert kept == ["1", "3"]  # 9 is too slow, the last raises


def test_a_patch_that_hangs_on_a_hidden_input_is_a_hang_witness(built):
    rec, _, _ = built
    patch = rec.reference.replace("total = 0", "total = 0\n    while len(xs) > 3:\n        pass")
    [v] = oracle.judge(rec, [patch], item_timeout=0.5)
    assert v.label == "overfit" and v.found_by == "hang" and "patch timeout" in v.witness


def _record(reference, tests, entry, inputs=(), slowest=0.0):
    return tasks.ProblemRecord(
        key="mbpp/1",
        source="mbpp",
        pid=1,
        text="",
        entry_point=entry,
        reference=reference,
        tests=list(tests),
        hidden_inputs=list(inputs),
        n_near_inputs=len(inputs),
        slowest_assert_s=slowest,
    )


def test_vetting_times_the_whole_comparison_not_just_the_reference_call():
    """generate_matrix(793) in MBPP 834: fast to build, slow to compare with itself."""
    ref = "def grid(n):\n    return [[i * j for j in range(n)] for i in range(n)]\n"
    assert tasks._vet_inputs(ref, "grid", "", ["3", "700"]) == ["3"]


def test_a_slow_but_correct_patch_is_not_called_a_hang():
    ref = "def f(n):\n    return n\n"
    slow = "import time\ndef f(n):\n    if n == 5:\n        time.sleep(0.4)\n    return n\n"
    rec = _record(ref, ["assert f(1) == 1"], "f", ["1", "5"])
    # 0.4 s is over a 0.1 s budget but inside the 10x re-check
    [v] = oracle.judge(rec, [slow], item_timeout=0.1)
    assert v.label == "no_witness"


def test_a_crash_is_a_crash_not_a_hang():
    ref = "def f(n):\n    return n\n"
    crash = "import os\ndef f(n):\n    if n == 5:\n        os._exit(3)\n    return n\n"
    rec = _record(ref, ["assert f(1) == 1"], "f", ["1", "5"])
    [v] = oracle.judge(rec, [crash], item_timeout=0.5)
    assert v.label == "overfit" and v.found_by == "crash" and "patch crash" in v.witness


def test_item_budget_scales_with_the_reference():
    assert _record("", [], "f").item_budget == tasks.BASE_ITEM_BUDGET
    assert _record("", [], "f", slowest=0.5).item_budget == 0.5 * tasks.SLOWDOWN


def test_problem_whose_every_mutant_survives_is_counted():
    from minimal_diff import data

    # `x > 0` -> `x >= 0` and `0` -> `1` both leave f(3) == 3: every mutant survives.
    p = data.Problem(
        "mbpp", 5, "", "def f(x):\n    return x if x > 0 else x\n", "f", ("assert f(3) == 3",)
    )
    rec, ts, counts = tasks.build_problem(p)
    assert ts == [] and counts["reason"] == "all_survived"
    _, _, st = tasks.build([p], workers=1)
    assert st.dropped_all_survived == 1 and st.problems_kept == 0


def test_oracle_self_check_finds_no_false_witness_on_a_clean_problem(built):
    from minimal_diff import isolation

    rec, _, _ = built
    res = isolation.oracle_false_positives({rec.key: rec}, workers=1)
    assert res == {"problems": 1, "false_overfit": 0, "cases": []}


def test_rescore_reproduces_the_row_repair_writes(built):
    rec, ts, _ = built
    for t in ts:
        row = repair.repair_task(t, rec)
        stale = json.loads(json.dumps(row))
        for c in stale["plausible_k1"]:
            c["size"] = {**c["size"], "tokens": 99, "tokens_raw": 99}
        for g in stale["subsets"]:
            stale["subsets"][g] = {}
        assert repair.rescore(stale, t) == json.loads(json.dumps(row))


def test_rescore_refuses_rows_from_different_repair_operators(built):
    rec, ts, _ = built
    row = repair.repair_task(ts[0], rec)
    if not row["plausible_k1"]:
        pytest.skip("no plausible patch in this fixture task")
    row["plausible_k1"][0]["where"] = "renamed@0"
    with pytest.raises(ValueError, match="operators have changed"):
        repair.rescore(row, ts[0])

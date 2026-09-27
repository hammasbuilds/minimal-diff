import gzip
import json

import pytest

from minimal_diff import data, inputs


def test_call_args_finds_the_function_by_name_not_position():
    args = inputs.call_args("assert set(f([1, 2], 3)) == {1}", "f")
    assert [type(a).__name__ for a in args] == ["List", "Constant"]
    assert inputs.call_args("assert g(1) == 2", "f") is None
    assert inputs.call_args("assert f(*xs) == 2", "f") is None  # cannot be replayed
    assert inputs.call_args("assert f(x=1) == 2", "f") is None


def test_generated_inputs_start_with_the_originals_and_round_robin():
    tests = ["assert f(3) == 9", "assert f(10) == 100"]
    got = inputs.generated_inputs(tests, "f", cap=6)
    assert got[:2] == ["3", "10"]
    assert len(got) == 6 and len(set(got)) == 6
    # both asserts contribute to the capped set
    assert any(int(x) < 8 for x in got[2:]) and any(int(x) > 8 for x in got[2:])


def test_perturbations_stay_in_type():
    import ast

    for src, typ in [
        ("'abc'", str),
        ("[1, 2, 3]", list),
        ("(1.5, 2.0)", tuple),
        ("-4", int),
        ("True", bool),
    ]:
        for p in inputs.perturb(ast.parse(src, mode="eval").body):
            assert isinstance(ast.literal_eval(ast.unparse(p)), typ), (src, ast.unparse(p))


def test_empty_set_is_never_generated_as_a_dict():
    import ast

    outs = [ast.unparse(p) for p in inputs.perturb(ast.parse("{1, 2}", mode="eval").body)]
    assert "{}" not in outs


def test_humaneval_asserts_are_renamed_and_filtered():
    check = (
        "def check(candidate):\n"
        "    assert candidate([1, 2]) == 3\n"
        "    assert candidate([]) == 0, 'empty'\n"
        "    for x in range(3):\n"
        "        assert candidate([x]) == x\n"
        "    assert True\n"
        "    assert candidate(\n"
    )
    got = data.humaneval_asserts(check, "total")
    assert got[:2] == ("assert total([1, 2]) == 3", "assert total([]) == 0, 'empty'")
    # the loop body is kept as a single line only if it parses on its own; x is unbound
    # there, so the reference validation in tasks.py drops it later - but it must not
    # be the half-line `assert candidate(`.
    assert all("candidate" not in t for t in got)
    assert "assert total(" not in got


def test_evalplus_inputs_are_thinned_evenly():
    src = (
        "def check(candidate):\n    inputs = "
        + repr([[i] for i in range(1000)])
        + "\n    results = []\n"
    )
    got = data.evalplus_inputs(src, cap=10)
    assert got == tuple(str(i) for i in range(0, 1000, 100))
    assert data.evalplus_inputs("no inputs here") == ()


def _write_fixture_data(root):
    raw = root / "raw"
    raw.mkdir(parents=True)
    (raw / "mbpp.jsonl").write_text(
        json.dumps(
            {
                "task_id": 7,
                "text": "double it",
                "code": "def dbl(x):\r\n    return x * 2",
                "test_list": ["assert dbl(2) == 4", "assert dbl(0) == 0", "assert dbl(-1) == -2"],
                "test_setup_code": "",
                "challenge_test_list": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (raw / "humaneval.jsonl").write_text(
        json.dumps({"_provenance": "fixture"})
        + "\n"
        + json.dumps(
            {
                "task_id": "HumanEval/4",
                "prompt": 'def inc(x):\n    """Add one."""\n',
                "canonical_solution": "    return x + 1\n",
                "test": "def check(candidate):\n    assert candidate(1) == 2\n",
                "entry_point": "inc",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with gzip.open(raw / "humanevalplus.jsonl.gz", "wt", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "task_id": "HumanEval/4",
                    "test": "def check(candidate):\n    inputs = [[5], [7]]\n    results = [6, 8]\n",
                }
            )
            + "\n"
        )


def test_loaders_read_the_directory_the_env_var_names(tmp_path, monkeypatch):
    _write_fixture_data(tmp_path)
    monkeypatch.setenv("MINIMAL_DIFF_DATA", str(tmp_path))
    [m] = data.load("mbpp")
    assert (m.key, m.entry_point, len(m.tests)) == ("mbpp/7", "dbl", 3)
    [h] = data.load("humaneval")
    assert h.key == "humaneval/4"
    assert h.reference.startswith("def inc(x):") and "return x + 1" in h.reference
    assert h.tests == ("assert inc(1) == 2",)
    assert h.extra_inputs == ("5", "7")


def test_missing_data_explains_how_to_get_it():
    with pytest.raises(FileNotFoundError, match="data/raw"):
        data.load("mbpp")
    with pytest.raises(ValueError, match="unknown source"):
        data.load("apps")


def test_fuzz_inputs_are_seeded_typed_and_new():
    seeds = ["[1, 2, 3], 'ab'", "[5], 'xyz'"]
    a = inputs.fuzz_inputs(seeds, n=40, key="k")
    assert a == inputs.fuzz_inputs(seeds, n=40, key="k")
    assert a != inputs.fuzz_inputs(seeds, n=40, key="other")
    assert len(a) == 40 and not set(a) & set(seeds)
    for src in a:
        xs, s = eval("(" + src + ",)")
        assert isinstance(xs, list) and all(isinstance(x, int) for x in xs) and isinstance(s, str)


def test_fuzz_skips_seeds_that_are_not_literals():
    assert inputs.fuzz_inputs(["Node(3)", "make()"], n=5) == []

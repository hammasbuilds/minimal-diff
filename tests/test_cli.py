"""The CLI end to end on a two-problem fixture dataset: build, repair, report, show, plan."""

import json

import pytest

from minimal_diff import cli

from .test_inputs_data import _write_fixture_data


@pytest.fixture
def fixture_env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    _write_fixture_data(data)
    monkeypatch.setenv("MINIMAL_DIFF_DATA", str(data))
    monkeypatch.setenv("MINIMAL_DIFF_RESULTS", str(tmp_path / "results"))
    return tmp_path


def test_help_for_every_command(capsys):
    for argv in (["--help"], ["repair", "--help"], ["show", "--help"], ["model", "plan", "--help"]):
        with pytest.raises(SystemExit) as e:
            cli.main(argv)
        assert e.value.code == 0
    assert "task_id" in capsys.readouterr().out


def test_missing_tasks_is_a_clear_error_not_a_traceback(capsys):
    assert cli.main(["repair", "--source", "mbpp"]) == 2
    assert "build-tasks" in capsys.readouterr().err


def test_report_before_repair_says_what_to_run(capsys):
    assert cli.main(["report"]) == 1
    assert "minimal-diff repair" in capsys.readouterr().err


def test_full_pipeline_on_fixture_data(fixture_env, capsys):
    assert cli.main(["build-tasks", "--workers", "2"]) == 0
    assert cli.main(["repair", "--workers", "2"]) == 0
    assert cli.main(["report"]) == 0
    out = capsys.readouterr().out
    assert "== mbpp" in out and "== humaneval" in out
    summary = json.loads((fixture_env / "results" / "classical_repair.json").read_text())
    # The fixtures are one-line functions; the known fix is always found and passes.
    for source in ("mbpp", "humaneval"):
        assert summary[source]["truth_in_space"]["rate"] == 1.0
        assert summary[source]["regimes"]["all"]["truth_plausible"]["rate"] == 1.0

    # Re-running repair is a no-op: every task is already in the results file.
    assert cli.main(["repair", "--workers", "2"]) == 0
    assert "0 newly repaired" in capsys.readouterr().out

    task_id = "mbpp/7/binop@5"
    assert cli.main(["show", task_id]) == 0
    shown = capsys.readouterr().out
    assert "the known fix" in shown and "smallest by tokens" in shown

    assert cli.main(["model", "plan", "--per-source", "5"]) == 0
    plan = capsys.readouterr().out
    assert "calls" in plan and "0 already cached" in plan
    written = json.loads(
        (fixture_env / "results" / "model_plan_qwen2.5-coder_14b.json").read_text()
    )
    assert written["calls"] == 3 * len(written["task_ids"]) and written["cached"] == 0


def test_show_rejects_bad_ids(fixture_env, capsys):
    cli.main(["build-tasks", "--source", "mbpp", "--workers", "2"])
    capsys.readouterr()
    assert cli.main(["show", "nonsense"]) == 2
    assert "look like" in capsys.readouterr().err
    assert cli.main(["show", "mbpp/7/compare@999"]) == 2
    assert "Tasks for that problem" in capsys.readouterr().err


def test_counts_must_be_positive(capsys):
    for argv in (
        ["repair", "--limit", "0"],
        ["build-tasks", "--workers", "-2"],
        ["model", "plan", "--per-source", "x"],
    ):
        with pytest.raises(SystemExit) as e:
            cli.main(argv)
        assert e.value.code == 2
    assert "must be at least 1" in capsys.readouterr().err


def test_show_suggests_tasks_of_the_right_problem(fixture_env, capsys):
    cli.main(["build-tasks", "--source", "mbpp", "--workers", "2"])
    capsys.readouterr()
    assert cli.main(["show", "mbpp/7"]) == 2
    err = capsys.readouterr().err
    assert "mbpp/7/" in err


def test_fix_repairs_a_users_own_file(tmp_path, capsys):
    prog = tmp_path / "prog.py"
    prog.write_text("def area(w, h):\n    return w + h\n")
    tests = tmp_path / "test_prog.py"
    tests.write_text("assert area(2, 3) == 6\nassert area(4, 5) == 20\n")
    assert cli.main(["fix", str(prog), "--tests", str(tests)]) == 0
    out = capsys.readouterr().out
    assert "+ return w * h" in out and "not the same as being correct" in out
    assert "+++ b/prog.py" in out


def test_fix_inline_asserts_and_no_fix_found(tmp_path, capsys):
    prog = tmp_path / "prog.py"
    prog.write_text("def f(x):\n    return x\n")
    assert cli.main(["fix", str(prog), "--assert", "assert f(1) == 1"]) == 0
    assert "already pass" in capsys.readouterr().out
    assert cli.main(["fix", str(prog), "--assert", "assert f(1) == 'x'"]) == 1
    assert "none of the" in capsys.readouterr().out
    assert cli.main(["fix", str(prog)]) == 2
    assert "--tests FILE" in capsys.readouterr().err
    bad = tmp_path / "bad.py"
    bad.write_text("def (:")
    assert cli.main(["fix", str(bad), "--assert", "assert 1"]) == 2
    assert "does not parse" in capsys.readouterr().err


def test_demo_without_data_prints_a_message_not_a_traceback(capsys):
    import importlib.util
    import pathlib

    spec = importlib.util.spec_from_file_location(
        "demo", pathlib.Path(__file__).resolve().parents[1] / "demo.py"
    )
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    assert demo.main() == 2
    assert "build-tasks" in capsys.readouterr().err


# A program with comments, blank lines, a docstring and hand formatting that `ast.unparse`
# would all rewrite; the bug is on line 9.
COMMENTED = '''"""Shapes."""

# area of a rectangle
def area(w, h):  # width, height
    return w * h


def perimeter(w,h):
    return 2 * (w - h)   # BUG: should be +
'''


def _apply(tool: list[str], folder, diff: str) -> None:
    import shutil
    import subprocess

    if shutil.which(tool[0]) is None:
        pytest.skip(f"{tool[0]} not installed")
    subprocess.run(tool, cwd=folder, input=diff.encode(), check=True, capture_output=True)


@pytest.mark.parametrize(
    "tool", [["git", "apply", "-"], ["patch", "-p1", "--quiet"]], ids=["git-apply", "patch"]
)
def test_fix_keeps_comments_and_its_diff_applies_to_the_original_file(tmp_path, capsys, tool):
    prog = tmp_path / "shapes.py"
    prog.write_bytes(COMMENTED.encode())
    args = [
        "fix",
        str(prog),
        "--assert",
        "assert perimeter(2, 3) == 10",
        "--assert",
        "assert perimeter(1, 1) == 4",
    ]
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert "@@ -6,4 +6,4 @@" in out  # real line numbers, not those of an unparsed copy
    assert cli.main([*args, "--diff"]) == 0
    diff = capsys.readouterr().out
    assert diff.startswith("--- a/shapes.py\n+++ b/shapes.py\n")
    _apply(tool, tmp_path, diff)
    fixed = prog.read_text()
    # Exactly one token changed; every comment and the formatting survive.
    assert fixed == COMMENTED.replace("(w - h)", "(w + h)")


def test_fix_diff_applies_to_a_file_without_a_final_newline_and_crlf(tmp_path, capsys):
    prog = tmp_path / "p.py"
    original = b"def f(x):\r\n    # add one\r\n    return x - 1"
    prog.write_bytes(original)
    assert cli.main(["fix", str(prog), "--assert", "assert f(1) == 2", "--diff"]) == 0
    diff = capsys.readouterr().out
    assert "\\ No newline at end of file" in diff
    _apply(["git", "apply", "-"], tmp_path, diff)
    assert prog.read_bytes() == original.replace(b"x - 1", b"x + 1")


def test_fix_rejects_an_assert_argument_that_is_not_an_assert(tmp_path, capsys):
    prog = tmp_path / "prog.py"
    prog.write_text("def f(x):\n    return x\n")
    assert cli.main(["fix", str(prog), "--assert", "f(1) == 2"]) == 2
    err = capsys.readouterr().err
    assert "not one assert statement" in err and "assert f(1) == 2" in err
    assert cli.main(["fix", str(prog), "--assert", "assert f(1) == 1; x = 2"]) == 2
    assert cli.main(["fix", str(prog), "--assert", "assert f(("]) == 2
    assert "does not parse" in capsys.readouterr().err


def test_fix_stops_with_the_error_when_the_asserts_cannot_run(tmp_path, capsys):
    prog = tmp_path / "prog.py"
    prog.write_text("def area(w, h):\n    return w + h\n")
    # A misspelt function name: no edit to the program can make this pass.
    assert cli.main(["fix", str(prog), "--assert", "assert aera(2, 3) == 6"]) == 2
    err = capsys.readouterr().err
    assert "cannot run" in err and "NameError: name 'aera' is not defined" in err


def test_fix_prints_an_error_the_bug_causes_and_still_repairs_it(tmp_path, capsys):
    prog = tmp_path / "prog.py"
    prog.write_text("def last(xs):\n    return xs[len(xs) + 1]\n")
    assert cli.main(["fix", str(prog), "--assert", "assert last([1, 2]) == 2"]) == 0
    out = capsys.readouterr().out
    assert "errors (not just fails)" in out and "IndexError: list index out of range" in out
    assert "+ return xs[len(xs) - 1]" in out


def test_fix_warns_about_asserts_it_does_not_run(tmp_path, capsys):
    prog = tmp_path / "prog.py"
    prog.write_text("def f(x):\n    return x - 1\n")
    tests = tmp_path / "t.py"
    tests.write_text("assert f(1) == 2\n\ndef test_more():\n    assert f(2) == 3\n")
    assert cli.main(["fix", str(prog), "--tests", str(tests)]) == 0
    err = capsys.readouterr().err
    assert "warning: 1 assert(s) not at the top level were ignored (line 4)" in err
    only_nested = tmp_path / "t2.py"
    only_nested.write_text("def test_f():\n    assert f(1) == 2\n")
    assert cli.main(["fix", str(prog), "--tests", str(only_nested)]) == 2
    assert "line 2" in capsys.readouterr().err


def test_fix_can_import_a_module_next_to_the_program(tmp_path, capsys):
    (tmp_path / "helper.py").write_text("def base():\n    return 10\n")
    prog = tmp_path / "prog.py"
    prog.write_text("import helper\n\ndef f(x):\n    return helper.base() - x\n")
    assert cli.main(["fix", str(prog), "--assert", "assert f(2) == 12"]) == 0
    assert "+ return helper.base() + x" in capsys.readouterr().out


def test_fix_timeout_must_be_positive(tmp_path, capsys):
    prog = tmp_path / "prog.py"
    prog.write_text("def f(x):\n    return x\n")
    for bad in ("0", "-1", "nan", "abc"):
        with pytest.raises(SystemExit) as e:
            cli.main(["fix", str(prog), "--assert", "assert f(1) == 1", "--timeout", bad])
        assert e.value.code == 2
    assert "positive number of seconds" in capsys.readouterr().err


def test_every_flag_of_every_command_has_help():
    import argparse

    def walk(parser, path):
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, sub in action.choices.items():
                    yield from walk(sub, f"{path} {name}")
            elif action.option_strings and action.dest != "help":
                yield path, action

    missing = [
        f"{path} {a.option_strings[0]}" for path, a in walk(cli.build_parser(), "") if not a.help
    ]
    assert missing == []


def test_show_reports_the_metric_asked_for(fixture_env, capsys):
    cli.main(["build-tasks", "--source", "mbpp", "--workers", "2"])
    capsys.readouterr()
    assert cli.main(["show", "mbpp/7/binop@5", "--metric", "ast"]) == 0
    out = capsys.readouterr().out
    assert "smallest by ast " in out and "smallest by tokens" not in out

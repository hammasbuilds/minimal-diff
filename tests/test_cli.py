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
    assert "the known fix" in shown and "smallest-first repair returns" in shown

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

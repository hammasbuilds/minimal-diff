"""The model arm, end to end, with a deterministic fake in place of Ollama."""

import json
import re

import pytest

from minimal_diff import tasks
from minimal_diff.model import arm, parse, prompts
from minimal_diff.model.client import CachedClient, FakeClient, OllamaClient, cache_key

from .test_repair import built  # noqa: F401  (fixture)


@pytest.fixture
def task_and_problem(built):  # noqa: F811
    rec, ts, _ = built
    t = ts[0]
    return t, rec


def test_prompts_share_context_and_differ_in_instruction(task_and_problem):
    t, rec = task_and_problem
    ps = {n: prompts.build(t, rec, n) for n in prompts.PROMPTS}
    for p in ps.values():
        assert t.buggy in p.user
        assert all(test in p.user for test in rec.tests)
        assert "FAILS" in p.user
    assert "laziest senior developer" not in ps["plain"].user
    assert "laziest senior developer" in ps["minimal"].user
    assert (
        "unified diff" in ps["diff"].user
        and "```python" not in ps["diff"].user.split("tests are run")[1]
    )
    with pytest.raises(ValueError):
        prompts.build(t, rec, "clever")


def test_humaneval_prompt_omits_the_description_already_in_the_docstring(task_and_problem):
    t, rec = task_and_problem
    he = tasks.ProblemRecord(
        **{**rec.__dict__, "source": "humaneval", "text": "UNIQUE-DESCRIPTION"}
    )
    assert "UNIQUE-DESCRIPTION" not in prompts.build(t, he, "plain").user


def test_extract_code_prefers_python_blocks():
    reply = "Here:\n```text\nnope\n```\n```python\ndef f():\n    return 1\n```\nand\n```python\ndef f():\n    return 2\n```"
    assert parse.extract_code(reply).code == "def f():\n    return 2"
    assert parse.extract_code("def f():\n    return 3\n").code == "def f():\n    return 3"
    assert parse.extract_code("   ").code is None
    assert parse.extract_code("```diff\n-a\n+b\n```").code is None


PROGRAM = "def f(x):\n    a = 1\n    if x < 1:\n        return a\n    return x\n"


def test_apply_diff_tolerates_wrong_line_numbers():
    reply = "```diff\n--- a/solution.py\n+++ b/solution.py\n@@ -40,3 +40,3 @@\n     a = 1\n-    if x < 1:\n+    if x <= 1:\n         return a\n```"
    got = parse.apply_diff(PROGRAM, reply)
    assert got.code == PROGRAM.replace("x < 1", "x <= 1").rstrip("\n")


def test_apply_diff_multiple_hunks_and_blank_context():
    prog = "def f(x):\n    a = 1\n\n    b = 2\n    return a + b + x\n"
    reply = (
        "@@ -1,3 +1,3 @@\n def f(x):\n-    a = 1\n+    a = 3\n\n"
        "@@ -4,2 +4,2 @@\n     b = 2\n-    return a + b + x\n+    return a - b + x\n"
    )
    got = parse.apply_diff(prog, reply)
    assert got.code == "def f(x):\n    a = 3\n\n    b = 2\n    return a - b + x"


def test_apply_diff_rejects_context_that_is_not_there():
    reply = "@@ -1,2 +1,2 @@\n-    if y > 2:\n+    if y >= 2:\n"
    got = parse.apply_diff(PROGRAM, reply)
    assert got.code is None and "not found" in got.error
    assert parse.apply_diff(PROGRAM, "just prose").error == "no @@ hunk in reply"


def test_cache_hits_never_call_the_model(tmp_path):
    fake = FakeClient(lambda s, p: f"echo:{p}")
    c = CachedClient(fake, tmp_path)
    assert c.generate("sys", "q1") == "echo:q1"
    assert c.generate("sys", "q1") == "echo:q1"
    assert c.generate("sys", "q2") == "echo:q2"
    assert (fake.calls, c.hits, c.misses) == (2, 1, 2)
    assert c.cached("sys", "q1") and not c.cached("sys", "q3")
    # A second client over the same directory resumes from disk.
    fake2 = FakeClient(lambda s, p: "different")
    assert CachedClient(fake2, tmp_path).generate("sys", "q1") == "echo:q1" and fake2.calls == 0


def test_cache_key_depends_on_everything_that_changes_the_answer():
    base = cache_key("m", "s", "p", {"temperature": 0})
    assert base != cache_key("m2", "s", "p", {"temperature": 0})
    assert base != cache_key("m", "s2", "p", {"temperature": 0})
    assert base != cache_key("m", "s", "p2", {"temperature": 0})
    assert base != cache_key("m", "s", "p", {"temperature": 0.7})


def test_ollama_client_reports_an_unreachable_server_clearly():
    from minimal_diff.model.client import ModelUnavailable

    c = OllamaClient(url="http://127.0.0.1:9", timeout=2)  # port 9: nothing listens
    with pytest.raises(ModelUnavailable, match="cannot reach ollama"):
        c.generate("s", "p")


def _fix_reply(task, rec, style):
    """A fake model: `fix` returns the reference, `rewrite` renames everything, etc."""
    if style == "fix":
        return f"```python\n{rec.reference}\n```"
    if style == "rewrite":
        return "```python\n" + re.sub(r"\btotal\b", "acc", rec.reference) + "\n# tidied\n```"
    if style == "diff":
        bug_line = task.buggy.splitlines()[task.fault_lines[0] - 1]
        ref_line = rec.reference.splitlines()[task.fault_lines[0] - 1]
        return f"```diff\n@@ -1,1 +1,1 @@\n-{bug_line}\n+{ref_line}\n```"
    if style == "same":
        return f"```python\n{task.buggy}\n```"
    return "I cannot help with that."


@pytest.mark.parametrize(
    ("prompt", "style", "verdict"),
    [
        ("plain", "fix", "exact"),
        ("plain", "rewrite", "no_witness"),
        ("diff", "diff", "exact"),
        ("minimal", "same", "implausible"),
        ("minimal", "refuse", "unparsed"),
    ],
)
def test_score_each_kind_of_reply(task_and_problem, prompt, style, verdict):
    t, rec = task_and_problem
    job = arm.Job(t, rec, prompts.build(t, rec, prompt))
    row = arm.score(job, _fix_reply(t, rec, style))
    assert row["verdict"] == verdict
    if style == "fix" or style == "diff":
        assert row["size"]["tokens"] == 1 and row["size"]["unrelated_lines"] == 0
    if style == "rewrite":
        # the rename is a correct program but touches lines the bug never did
        assert row["size"]["tokens"] > 3 and row["size"]["unrelated_lines"] > 0
        assert row["size"]["lines_raw"] > row["size"]["lines"]  # the comment only counts raw
    if style == "same":
        assert row["unchanged"] is True


def test_run_and_summarise_end_to_end(task_and_problem, tmp_path):
    t, rec = task_and_problem
    jobs = arm.plan([t], {rec.key: rec}, prompts.PROMPTS)
    assert len(jobs) == 3

    def respond(system, prompt):
        if "unified diff" in prompt:
            return _fix_reply(t, rec, "diff")
        if "laziest" in prompt:
            return _fix_reply(t, rec, "fix")
        return _fix_reply(t, rec, "rewrite")

    fake = FakeClient(respond)
    out = tmp_path / "rows.jsonl"
    assert arm.run(jobs, CachedClient(fake, tmp_path / "cache"), out, progress=False) == 3
    rows = [json.loads(ln) for ln in out.read_text().splitlines()]
    s = arm.summarise(rows)
    assert s["prompts"]["minimal"]["exact"]["rate"] == 1.0
    assert (
        s["prompts"]["plain"]["mean_tokens_changed"]
        > s["prompts"]["minimal"]["mean_tokens_changed"]
    )
    assert s["prompts"]["plain"]["unrelated_edit_rate"]["rate"] == 1.0
    # Re-running is free: everything comes from the cache.
    fake.calls = 0
    arm.run(jobs, CachedClient(fake, tmp_path / "cache"), out, progress=False)
    assert fake.calls == 0


def test_sample_takes_one_task_per_problem_and_balances_kinds():
    ts = [
        tasks.Task(f"mbpp/{p}/{k}@{i}", f"mbpp/{p}", k, f"{k}@{i}", "", [1], ["fail"])
        for p in range(40)
        for i, k in enumerate(["compare", "const", "const", "const"])
    ]
    got = arm.sample_tasks(ts, per_source=20)
    assert len(got) == 20 and len({t.problem for t in got}) == 20
    kinds = [t.kind for t in got]
    assert abs(kinds.count("compare") - kinds.count("const")) <= 1
    assert arm.sample_tasks(ts, per_source=20) == got  # seeded

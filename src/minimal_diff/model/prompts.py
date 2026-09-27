"""The three prompts the model arm compares. Only the instruction changes between them.

- `plain`: "fix it". What most repair benchmarks ask.
- `minimal`: the ponytail instruction - the laziest senior developer, the smallest
  change that fixes the bug, touch nothing else.
- `diff`: the same minimality ask, but the model returns a unified diff instead of the
  whole program, so there is no rewritten file in which to smuggle unrelated edits. A
  diff that does not apply is scored as a failed repair, not retried.

Every prompt shows the same program, the same visible asserts, and which of them fail
and how, and every prompt says the fix must make the program correct rather than merely
pass the tests. So `minimal` differs from `plain` only by the laziness instruction, and
`diff` from `minimal` only by the output format.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..tasks import ProblemRecord, Task

PROMPTS = ("plain", "minimal", "diff")

SYSTEM = "You are an expert Python programmer."

_CONTEXT = """\
{description}The following Python program has a bug.

```python
{code}
```

These tests are run against it:

{tests}
"""

# Shared by all three prompts, so no arm is the only one told that passing the tests is not
# the goal.
_CORRECT = "The fix must make the program correct, not merely make these tests pass."

_PLAIN = f"""\
Fix the bug. {_CORRECT}

Reply with the complete corrected program in a single ```python code block."""

_LAZY = """\
You are the laziest senior developer on the team: the best fix is the one that changes \
the least. Find the bug and fix it with the smallest possible edit. Do not refactor, \
rename, reformat, reorder, add comments, add error handling or change any line that is \
not part of the bug."""

_MINIMAL = f"""\
{_LAZY} {_CORRECT}

Reply with the complete corrected program in a single ```python code block."""

_DIFF = f"""\
{_LAZY} {_CORRECT}

Reply with ONLY a unified diff against the program above, in a single ```diff code \
block, using `--- a/solution.py` and `+++ b/solution.py` headers and standard `@@` \
hunks with a few lines of unchanged context."""

_INSTRUCTIONS = {"plain": _PLAIN, "minimal": _MINIMAL, "diff": _DIFF}


@dataclass(frozen=True)
class Prompt:
    name: str
    system: str
    user: str


_STATUS_TEXT = {
    "pass": "passes",
    "fail": "FAILS (AssertionError)",
    "error": "FAILS (raises an exception)",
    "timeout": "FAILS (does not finish)",
    "crash": "FAILS (the interpreter crashes)",
}


def _test_lines(task: Task, problem: ProblemRecord) -> str:
    out = []
    for status, test in zip(task.visible_status, problem.tests, strict=True):
        verdict = _STATUS_TEXT.get(status, f"FAILS ({status})")
        out.append(f"- `{test}`  -> {verdict}")
    return "\n".join(out)


def build(task: Task, problem: ProblemRecord, name: str) -> Prompt:
    if name not in _INSTRUCTIONS:
        raise ValueError(f"unknown prompt {name!r}; expected one of {PROMPTS}")
    # HumanEval's description is the docstring, which is already inside the program.
    description = "" if problem.source == "humaneval" else f"Task: {problem.text.strip()}\n\n"
    ctx = _CONTEXT.format(
        description=description, code=task.buggy, tests=_test_lines(task, problem)
    )
    return Prompt(name, SYSTEM, ctx + "\n" + _INSTRUCTIONS[name])

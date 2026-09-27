"""The three prompts the model arm compares. Only the instruction changes between them.

- `plain`: "fix it". What most repair benchmarks ask.
- `minimal`: the ponytail instruction - the laziest senior developer, the smallest
  change that fixes the bug, touch nothing else.
- `diff`: the same minimality ask, but the model returns a unified diff instead of the
  whole program, so there is no rewritten file in which to smuggle unrelated edits. A
  diff that does not apply is scored as a failed repair, not retried.

Every prompt shows the same program, the same visible asserts, and which of them fail
and how, so differences between arms are the instruction's doing.
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

_PLAIN = """\
Fix the bug so that the program is correct.

Reply with the complete corrected program in a single ```python code block."""

_MINIMAL = """\
You are the laziest senior developer on the team: the best fix is the one that changes \
the least. Find the bug and fix it with the smallest possible edit. Do not refactor, \
rename, reformat, reorder, add comments, add error handling or change any line that is \
not part of the bug. The fix must make the program correct, not merely make these tests \
pass.

Reply with the complete corrected program in a single ```python code block."""

_DIFF = """\
You are the laziest senior developer on the team: the best fix is the one that changes \
the least. Find the bug and fix it with the smallest possible edit. Do not refactor, \
rename, reformat or touch any line that is not part of the bug. The fix must make the \
program correct, not merely make these tests pass.

Reply with ONLY a unified diff against the program above, in a single ```diff code \
block, using `--- a/solution.py` and `+++ b/solution.py` headers and standard `@@` \
hunks with a few lines of unchanged context."""

_INSTRUCTIONS = {"plain": _PLAIN, "minimal": _MINIMAL, "diff": _DIFF}


@dataclass(frozen=True)
class Prompt:
    name: str
    system: str
    user: str


def _test_lines(task: Task, problem: ProblemRecord) -> str:
    out = []
    for status, test in zip(task.visible_status, problem.tests, strict=True):
        verdict = {"pass": "passes", "fail": "FAILS (AssertionError)"}.get(
            status, f"FAILS ({status})"
        )
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

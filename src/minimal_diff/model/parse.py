"""Turn a model reply into a program: pull out the code block, or apply the diff.

Both are strict about *what* they accept and lenient about *where*: a diff hunk whose
`@@` line numbers are off is still applied if its context and removed lines appear in
the program exactly once near there, because models routinely miscount line numbers and
that is not the property under test. A hunk whose lines are not in the program at all
fails, and the repair is scored as failed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_FENCE = re.compile(r"```([A-Za-z0-9_+-]*)[^\n]*\n(.*?)```", re.S)
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class PatchError(ValueError):
    pass


@dataclass(frozen=True)
class Parsed:
    code: str | None
    error: str = ""


def extract_code(reply: str) -> Parsed:
    """The last ```python block (or the last unlabelled one); the whole reply if there is none."""
    blocks = _FENCE.findall(reply)
    for want in ("python", "py", ""):
        picked = [body for lang, body in blocks if lang.lower() == want]
        if picked:
            return Parsed(picked[-1].rstrip("\n"))
    if blocks:
        return Parsed(None, f"no python block (found: {', '.join(sorted({b[0] for b in blocks}))})")
    stripped = reply.strip()
    return Parsed(stripped or None, "" if stripped else "empty reply")


def _diff_text(reply: str) -> str:
    blocks = _FENCE.findall(reply)
    for want in ("diff", "patch", ""):
        picked = [body for lang, body in blocks if lang.lower() == want]
        if picked:
            return picked[-1]
    return reply


def _hunks(diff: str) -> list[tuple[int, list[str]]]:
    hunks: list[tuple[int, list[str]]] = []
    cur: list[str] | None = None
    for line in diff.splitlines():
        m = _HUNK.match(line)
        if m:
            cur = []
            hunks.append((int(m.group(1)), cur))
            continue
        if line.startswith(("--- ", "+++ ", "diff ", "index ")) and (cur is None or not cur):
            continue
        if cur is None:
            continue
        if line.startswith(("+", "-", " ")):
            cur.append(line)
        elif line == "":
            cur.append(" ")  # editors strip the single space of an empty context line
        elif line.startswith("\\"):
            continue  # "\ No newline at end of file"
        else:
            raise PatchError(f"unexpected line in hunk: {line[:60]!r}")
    for _, body in hunks:
        while body and body[-1] == " ":  # blank lines after a hunk are not context
            body.pop()
    return [(start, body) for start, body in hunks if body]


def _find(lines: list[str], needle: list[str], hint: int) -> int:
    """Where `needle` occurs in `lines`; the occurrence nearest `hint` if more than one."""
    if not needle:
        return max(0, min(hint, len(lines)))
    norm = [ln.rstrip() for ln in lines]
    want = [ln.rstrip() for ln in needle]
    hits = [i for i in range(len(norm) - len(want) + 1) if norm[i : i + len(want)] == want]
    if not hits:
        raise PatchError(f"hunk context not found: {needle[0][:60]!r}")
    return min(hits, key=lambda i: abs(i - hint))


def apply_diff(original: str, reply: str) -> Parsed:
    """Apply the unified diff in `reply` to `original`."""
    try:
        hunks = _hunks(_diff_text(reply))
        if not hunks:
            return Parsed(None, "no @@ hunk in reply")
        lines = original.splitlines()
        offset = 0
        for start, body in hunks:
            old = [ln[1:] for ln in body if ln[0] in " -"]
            new = [ln[1:] for ln in body if ln[0] in " +"]
            at = _find(lines, old, start - 1 + offset)
            lines[at : at + len(old)] = new
            offset += len(new) - len(old)
        return Parsed("\n".join(lines))
    except PatchError as e:
        return Parsed(None, str(e))


def to_program(prompt_name: str, buggy: str, reply: str) -> Parsed:
    """The patched program a reply describes, under the format its prompt asked for."""
    return apply_diff(buggy, reply) if prompt_name == "diff" else extract_code(reply)

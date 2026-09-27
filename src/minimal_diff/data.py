"""Load MBPP, HumanEval and the EvalPlus inputs for HumanEval from `data/raw/`.

Adapted from `data.py` in hammasbuilds/mbpp-false-accepts, which found the traps kept
here: HumanEval's `canonical_solution` is only a body, its asserts call `candidate`, and
EvalPlus writes no asserts at all - only an `inputs = [...]` list.

The three raw files are committed (MBPP is CC BY 4.0, HumanEval MIT, EvalPlus
Apache-2.0), so a clean clone rebuilds every task without a network or a Hugging Face
cache. `MINIMAL_DIFF_DATA` overrides the directory.
"""

from __future__ import annotations

import ast
import gzip
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# `assert foo(1) == 2` -> `foo`
_CALLED = re.compile(r"assert\s+(?:not\s+)?(?:\(\s*)?([A-Za-z_]\w*)\s*\(")
# EvalPlus stores its cases as `inputs = [...]` immediately above `results = [...]`.
_PLUS_INPUTS = re.compile(r"inputs\s*=\s*(\[.*?\])\s*\n\s*results", re.S)


def data_dir() -> Path:
    env = os.environ.get("MINIMAL_DIFF_DATA")
    return Path(env) if env else ROOT / "data"


def write_json(path: Path, obj: object) -> None:
    """Pretty JSON with LF line endings, so a rerun on Windows is byte-identical in git."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8", newline="\n")


def results_dir() -> Path:
    """Where results are read and written; `MINIMAL_DIFF_RESULTS` overrides `results/`."""
    env = os.environ.get("MINIMAL_DIFF_RESULTS")
    return Path(env) if env else ROOT / "results"


@dataclass(frozen=True)
class Problem:
    """One benchmark problem: a reference, its visible asserts, and extra inputs."""

    source: str  # "mbpp" | "humaneval"
    pid: int
    text: str
    reference: str
    entry_point: str
    tests: tuple[str, ...]
    setup: str = ""
    # Asserts nobody shows the repairer. MBPP's `challenge_test_list` (11 problems).
    hidden_tests: tuple[str, ...] = ()
    # Argument lists (as source) from a much larger suite, used only for differential
    # testing against the reference. EvalPlus for HumanEval; empty for MBPP.
    extra_inputs: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.source}/{self.pid}"


def _missing(path: Path) -> FileNotFoundError:
    return FileNotFoundError(
        f"{path} is missing. The raw datasets are committed under data/raw/; "
        "restore them with `git checkout data/raw` or point MINIMAL_DIFF_DATA at a copy."
    )


def load_mbpp(limit: int | None = None) -> list[Problem]:
    """All 974 MBPP problems as released (the `full` split)."""
    path = data_dir() / "raw" / "mbpp.jsonl"
    if not path.exists():
        raise _missing(path)
    out: list[Problem] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        tests = tuple(r["test_list"])
        entry = next((m.group(1) for t in tests if (m := _CALLED.search(t))), None)
        if entry is None:
            continue
        out.append(
            Problem(
                source="mbpp",
                pid=int(r["task_id"]),
                text=r["text"],
                reference=r["code"],
                entry_point=entry,
                tests=tests,
                setup=r.get("test_setup_code") or "",
                hidden_tests=tuple(r.get("challenge_test_list") or ()),
            )
        )
        if limit and len(out) >= limit:
            break
    return out


def humaneval_asserts(check_src: str, entry: str) -> tuple[str, ...]:
    """The single-line asserts in a HumanEval `check(candidate)`, calling `entry` by name.

    Multi-line asserts and asserts inside loops are skipped rather than guessed at, so
    this is a lower bound on the suite. Asserts that do not survive `ast.parse` on their
    own are dropped too.
    """
    out = []
    for line in check_src.splitlines():
        s = line.strip()
        if not (s.startswith("assert ") and "candidate" in s):
            continue
        s = re.sub(r"\bcandidate\b", entry, s)
        try:
            ast.parse(s)
        except SyntaxError:
            continue
        out.append(s)
    return tuple(out)


def evalplus_inputs(test_src: str, cap: int = 150) -> tuple[str, ...]:
    """EvalPlus's `inputs` list as argument-list source strings, evenly thinned to `cap`.

    EvalPlus puts HumanEval's own cases first and its generated ones after, so taking
    the first `cap` would mostly re-test what the visible asserts already test. An even
    stride over the whole list keeps both kinds.
    """
    m = _PLUS_INPUTS.search(test_src)
    if not m:
        return ()
    try:
        cases = ast.literal_eval(m.group(1))
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return ()
    if len(cases) > cap:
        step = len(cases) / cap
        cases = [cases[int(i * step)] for i in range(cap)]
    out = []
    for inp in cases:
        args = inp if isinstance(inp, list) else [inp]
        s = ", ".join(repr(a) for a in args)
        if len(s) <= 2000:  # a few EvalPlus inputs are 100 KB lists; they add time, not signal
            out.append(s)
    return tuple(out)


def load_humaneval(limit: int | None = None) -> list[Problem]:
    """All 164 HumanEval problems, each with its EvalPlus inputs attached."""
    raw = data_dir() / "raw"
    he, hep = raw / "humaneval.jsonl", raw / "humanevalplus.jsonl.gz"
    for p in (he, hep):
        if not p.exists():
            raise _missing(p)
    plus: dict[str, str] = {}
    with gzip.open(hep, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                r = json.loads(line)
                if "task_id" in r:
                    plus[r["task_id"]] = r["test"]
    out: list[Problem] = []
    for line in he.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if "_provenance" in r:
            continue
        entry = r["entry_point"]
        out.append(
            Problem(
                source="humaneval",
                pid=int(str(r["task_id"]).rsplit("/", 1)[-1]),
                text=r["prompt"],
                reference=r["prompt"] + r["canonical_solution"],
                entry_point=entry,
                tests=humaneval_asserts(r["test"], entry),
                extra_inputs=evalplus_inputs(plus.get(r["task_id"], "")),
            )
        )
        if limit and len(out) >= limit:
            break
    return out


def load(source: str, limit: int | None = None) -> list[Problem]:
    if source == "mbpp":
        return load_mbpp(limit)
    if source == "humaneval":
        return load_humaneval(limit)
    raise ValueError(f"unknown source {source!r}; expected 'mbpp' or 'humaneval'")

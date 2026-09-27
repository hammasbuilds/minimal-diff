"""The model arm: the same tasks, repaired by an LLM under three prompts, scored like the search.

Each reply is turned into a program, and that program goes through exactly the checks
the classical search's patches go through: every visible assert (plausible or not), then
the reference oracle (exact / overfit / no_witness), then every diff-size measure against
the buggy program. The classical result on the same task is carried along, so the two
can be compared pairwise rather than as two unrelated averages.
"""

from __future__ import annotations

import json
import random
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .. import diffmetrics, operators, oracle, sandbox, stats
from ..tasks import ProblemRecord, Task
from . import parse, prompts
from .client import Client


def sample_tasks(tasks: list[Task], per_source: int, seed: int = 0) -> list[Task]:
    """At most one task per problem, `per_source` problems per benchmark, bug kinds balanced.

    One task per problem keeps the model's results independent of each other, which the
    classical study (every mutant of every problem) cannot claim and so has to bootstrap
    around. Problems are shuffled with a fixed seed; each takes the bug kind the sample
    has seen least of so far.
    """
    by_source: dict[str, dict[str, list[Task]]] = defaultdict(lambda: defaultdict(list))
    for t in tasks:
        by_source[t.problem.split("/")[0]][t.problem].append(t)
    out: list[Task] = []
    for source in sorted(by_source):
        rng = random.Random(f"{seed}/{source}")
        probs = sorted(by_source[source])
        rng.shuffle(probs)
        seen: dict[str, int] = defaultdict(int)
        for key in probs[:per_source]:
            options = sorted(by_source[source][key], key=lambda t: t.id)
            rng.shuffle(options)
            pick = min(options, key=lambda t: seen[t.kind])
            seen[pick.kind] += 1
            out.append(pick)
    return out


@dataclass(frozen=True)
class Job:
    task: Task
    problem: ProblemRecord
    prompt: prompts.Prompt


def plan(
    tasks: Iterable[Task], problems: dict[str, ProblemRecord], arms: Iterable[str]
) -> list[Job]:
    arms = list(arms)
    return [
        Job(t, problems[t.problem], prompts.build(t, problems[t.problem], a))
        for t in tasks
        for a in arms
    ]


def score(job: Job, reply: str) -> dict:
    """One scored row for one model reply."""
    task, prob = job.task, job.problem
    row: dict = {
        "id": task.id,
        "problem": task.problem,
        "source": prob.source,
        "bug_kind": task.kind,
        "prompt": job.prompt.name,
        "reply_chars": len(reply),
    }
    parsed = parse.to_program(job.prompt.name, task.buggy, reply)
    if parsed.code is None or operators.parse(parsed.code) is None:
        row.update(
            parsed=False,
            parse_error=parsed.error or "not valid Python",
            plausible=False,
            verdict="unparsed",
        )
        return row
    code = parsed.code
    [vis] = sandbox.run_asserts([code], prob.tests, prob.setup, stop_on_first_failure=False)
    plausible = all(r.status == "pass" for r in vis)
    row.update(
        parsed=True,
        plausible=plausible,
        unchanged=operators.normalise(code) == operators.normalise(task.buggy),
    )
    # The buggy program is already in `ast.unparse` form. Measuring against the reply
    # normalised the same way charges the model for code it changed, not for its choice
    # of quotes; `lines_raw` keeps the unnormalised count, comments and all, which is
    # what a reviewer would actually be handed.
    size = diffmetrics.measure(
        task.buggy, operators.normalise(code), set(task.fault_lines)
    ).as_dict()
    size["lines_raw"] = diffmetrics.lines_changed(task.buggy, code)
    row["size"] = size
    if plausible:
        [v] = oracle.judge(prob, [code], [vis])
        row.update(verdict=v.label, witness=v.witness)
    else:
        row["verdict"] = "implausible"
    return row


def run(jobs: list[Job], client: Client, out: Path, progress: bool = True) -> int:
    """Generate (through the cache) and score every job; rewrite `out`. Returns rows written."""
    rows = []
    for i, job in enumerate(jobs, 1):
        reply = client.generate(job.prompt.system, job.prompt.user)
        rows.append(score(job, reply))
        if progress and i % 25 == 0:
            print(f"  {i}/{len(jobs)} replies scored", flush=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)
    out.write_text(text, encoding="utf-8", newline="\n")
    return len(rows)


def summarise(rows: list[dict], classical: dict[str, dict] | None = None) -> dict:
    """Per prompt: parse rate, plausible rate, exact/overfit rates, diff size; per bug kind.

    `classical` maps task id -> classical row, to report the search's smallest-first
    verdict on the very same tasks.
    """
    out: dict = {"n_rows": len(rows), "prompts": {}}
    for name in prompts.PROMPTS:
        rs = [r for r in rows if r["prompt"] == name]
        if not rs:
            continue
        plaus = [r for r in rs if r.get("plausible")]
        sized = [r for r in rs if r.get("parsed")]
        s = {
            "n": len(rs),
            "parsed": stats.cluster_rate(rs, lambda r: float(r["parsed"])),
            "plausible": stats.cluster_rate(rs, lambda r: float(r["plausible"])),
            "exact": stats.cluster_rate(rs, lambda r: float(r["verdict"] == "exact")),
            "no_witness": stats.cluster_rate(rs, lambda r: float(r["verdict"] == "no_witness")),
            "overfit": stats.cluster_rate(rs, lambda r: float(r["verdict"] == "overfit")),
            "overfit_given_plausible": stats.cluster_rate(
                plaus, lambda r: float(r["verdict"] == "overfit")
            ),
            "mean_tokens_changed": _mean(r["size"]["tokens"] for r in sized),
            "mean_ast_nodes_changed": _mean(
                r["size"]["ast_nodes"] for r in sized if r["size"]["ast_nodes"] is not None
            ),
            "mean_lines_changed": _mean(r["size"]["lines"] for r in sized),
            "mean_lines_changed_raw": _mean(r["size"]["lines_raw"] for r in sized),
            "unrelated_edit_rate": stats.cluster_rate(
                sized, lambda r: float(r["size"]["unrelated_lines"] > 0)
            ),
            "by_bug_kind": {},
        }
        for kind in sorted({r["bug_kind"] for r in rs}):
            k_rows = [r for r in rs if r["bug_kind"] == kind]
            s["by_bug_kind"][kind] = {
                "n": len(k_rows),
                "exact": stats.cluster_rate(k_rows, lambda r: float(r["verdict"] == "exact")),
                "overfit": stats.cluster_rate(k_rows, lambda r: float(r["verdict"] == "overfit")),
            }
        if classical:
            paired = [classical[r["id"]] for r in rs if r["id"] in classical]
            s["classical_same_tasks"] = {
                "n": len(paired),
                "smallest_exact": stats.cluster_rate(
                    paired,
                    lambda c: float(
                        (c["subsets"]["all"]["smallest"] or {}).get("verdict") == "exact"
                    ),
                ),
                "smallest_overfit": stats.cluster_rate(
                    paired,
                    lambda c: float(
                        (c["subsets"]["all"]["smallest"] or {}).get("verdict") == "overfit"
                    ),
                ),
            }
        out["prompts"][name] = s
    return out


def _mean(xs: Iterable[float]) -> float | None:
    xs = list(xs)
    return round(sum(xs) / len(xs), 3) if xs else None

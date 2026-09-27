"""`minimal-diff model plan|run|report` - the queued LLM arm."""

from __future__ import annotations

import argparse
import json
import sys

from .. import data, study, tasks
from . import arm, prompts
from .client import DEFAULT_MODEL, DEFAULT_URL, CachedClient, ModelUnavailable, OllamaClient


# Measured nowhere yet - this is only for the dry run's time estimate, and it says so.
ASSUMED_SECONDS_PER_CALL = 12.0


def _safe(model: str) -> str:
    return model.replace(":", "_").replace("/", "_")


def _jobs(a: argparse.Namespace) -> list[arm.Job]:
    all_tasks: list[tasks.Task] = []
    problems: dict[str, tasks.ProblemRecord] = {}
    for source in ("mbpp", "humaneval"):
        all_tasks += tasks.load_tasks(source)
        problems.update(tasks.load_problems(source))
    picked = arm.sample_tasks(all_tasks, a.per_source, seed=a.seed)
    return arm.plan(picked, problems, a.prompts)


def cmd_plan(a: argparse.Namespace) -> int:
    jobs = _jobs(a)
    client = CachedClient(OllamaClient(a.model, a.url), data.results_dir() / "model_cache")
    todo = [j for j in jobs if not client.cached(j.prompt.system, j.prompt.user)]
    by = {}
    for j in jobs:
        key = (j.problem.source, j.prompt.name)
        by[key] = by.get(key, 0) + 1
    print(f"model {a.model} at {a.url}")
    for (source, name), n in sorted(by.items()):
        print(f"  {source:<10} {name:<8} {n:>5} calls")
    print(
        f"total {len(jobs)} calls, {len(jobs) - len(todo)} already cached, {len(todo)} to generate"
    )
    hours = len(todo) * ASSUMED_SECONDS_PER_CALL / 3600
    print(f"estimate at an assumed {ASSUMED_SECONDS_PER_CALL:.0f} s/call: {hours:.1f} h")
    if a.show:
        for j in jobs[: a.show]:
            print(f"\n----- {j.task.id} [{j.prompt.name}] -----\n{j.prompt.user}")
    return 0


def cmd_run(a: argparse.Namespace) -> int:
    jobs = _jobs(a)
    client = CachedClient(OllamaClient(a.model, a.url), data.results_dir() / "model_cache")
    out = data.results_dir() / f"model_{_safe(a.model)}.jsonl"
    print(f"{len(jobs)} jobs -> {out}")
    try:
        n = arm.run(jobs, client, out)
    except ModelUnavailable as e:
        print(
            f"error: {e}\n(every reply generated so far is cached; re-run to resume)",
            file=sys.stderr,
        )
        return 3
    print(f"{n} rows; cache hits {client.hits}, generated {client.misses}")
    return cmd_report(a)


def cmd_report(a: argparse.Namespace) -> int:
    path = data.results_dir() / f"model_{_safe(a.model)}.jsonl"
    if not path.exists():
        print(f"no {path.name}: run `minimal-diff model run` first", file=sys.stderr)
        return 1
    rows = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    classical = {
        r["id"]: r
        for source in ("mbpp", "humaneval")
        for r in study.read_rows(data.results_dir() / f"classical_{source}.jsonl.gz")
    }
    summary = arm.summarise(rows, classical)
    summary["model"] = a.model
    out = data.results_dir() / f"model_arm_{_safe(a.model)}.json"
    out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    from ..stats import fmt

    cols = f"{'plausible':<22}{'exact':<22}{'overfit':<22}{'tokens':>7}{'unrelated':>24}"
    print(f"{'prompt':<9}{'n':>5}  {cols}")
    for name, s in summary["prompts"].items():
        print(
            f"{name:<9}{s['n']:>5}  {fmt(s['plausible']):<22}{fmt(s['exact']):<22}"
            f"{fmt(s['overfit']):<22}{s['mean_tokens_changed'] or 0:>7}"
            f"{fmt(s['unrelated_edit_rate']):>24}"
        )
    print(f"wrote {out}")
    return 0


def add_parser(sub: argparse._SubParsersAction) -> None:
    m = sub.add_parser("model", help="the LLM arm: plan (dry run), run, report")
    msub = m.add_subparsers(dest="model_cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--model", default=DEFAULT_MODEL)
        p.add_argument("--url", default=DEFAULT_URL)
        p.add_argument("--per-source", type=int, default=200, help="problems sampled per benchmark")
        p.add_argument(
            "--prompts", nargs="+", choices=prompts.PROMPTS, default=list(prompts.PROMPTS)
        )
        p.add_argument("--seed", type=int, default=0)

    p = msub.add_parser("plan", help="print the job list and call count; calls no model")
    common(p)
    p.add_argument("--show", type=int, default=0, help="also print the first N prompts")
    p.set_defaults(func=cmd_plan)

    r = msub.add_parser("run", help="generate (resumable, cached) and score every job")
    common(r)
    r.set_defaults(func=cmd_run)

    rep = msub.add_parser("report", help="re-score summary from results/model_<model>.jsonl")
    common(rep)
    rep.set_defaults(func=cmd_report)

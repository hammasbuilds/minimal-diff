"""`minimal-diff`: build repair tasks, run the classical search, report, queue the model arm."""

from __future__ import annotations

import argparse
import contextlib
import functools
import re
import statistics
import sys
from pathlib import Path

from . import data, isolation, operators, repair, stats, study, tasks, userfix

SOURCES = ("mbpp", "humaneval")
SOURCE_HELP = "which benchmark (default: both)"
WORKERS_HELP = "parallel worker processes, each capped at 512 MB (default 8)"


def _sources(arg: str) -> tuple[str, ...]:
    return SOURCES if arg == "both" else (arg,)


def cmd_build_tasks(a: argparse.Namespace) -> int:
    for source in _sources(a.source):
        probs = data.load(source, a.limit)
        print(f"{source}: {len(probs)} problems -> building tasks (runs every mutant) ...")
        recs, ts, st = tasks.build(probs, workers=a.workers, progress=True)
        prov = {
            "source": source,
            "built_by": "minimal-diff build-tasks",
            "limit": a.limit,
            "stats": st.__dict__,
        }
        tasks.save(tasks.problems_path(source), recs, prov)
        tasks.save(tasks.tasks_path(source), ts, prov)
        print(f"  {st}")
        summary = data.results_dir() / f"tasks_{source}.json"
        data.write_json(summary, prov)
    return 0


def _rows_path(source: str) -> Path:
    return data.results_dir() / f"classical_{source}.jsonl.gz"


def cmd_repair(a: argparse.Namespace) -> int:
    for source in _sources(a.source):
        probs = tasks.load_problems(source)
        ts = tasks.load_tasks(source)
        if a.limit:
            ts = ts[: a.limit]
        out = _rows_path(source)
        print(f"{source}: {len(ts)} tasks -> {out}")
        n = study.run(ts, probs, out, workers=a.workers)
        print(f"  {n} newly repaired")
    return 0


def cmd_rescore(a: argparse.Namespace) -> int:
    for source in _sources(a.source):
        path = _rows_path(source)
        if not path.exists():
            print(f"no {path.name}: run `minimal-diff repair` first", file=sys.stderr)
            return 1
        ts = {t.id: t for t in tasks.load_tasks(source)}
        res = study.rescore_file(path, ts)
        print(f"{source}: {res['rows']} rows re-measured in {path}")
        for k, n in res["changed"].items():
            print(f"  {k:<28} {n:>6} changed")
    return 0


def cmd_check_oracle(a: argparse.Namespace) -> int:
    probs: dict[str, tasks.ProblemRecord] = {}
    for source in SOURCES:
        probs.update(tasks.load_problems(source))
    res = isolation.oracle_false_positives(probs, workers=a.workers)
    out = data.results_dir() / "oracle_check.json"
    data.write_json(out, res)
    print(
        f"reference + one inert statement, judged on all {res['problems']} problems: "
        f"{res['false_overfit']} false 'overfit' verdicts"
    )
    print(f"wrote {out}")
    return 0 if res["false_overfit"] == 0 else 1


def cmd_check_isolation(a: argparse.Namespace) -> int:
    ts: list[tasks.Task] = []
    probs: dict[str, tasks.ProblemRecord] = {}
    for source in SOURCES:
        ts += tasks.load_tasks(source)
        probs.update(tasks.load_problems(source))
    res = isolation.check(ts, probs, n_tasks=a.tasks, per_task=a.per_task, workers=a.workers)
    out = data.results_dir() / "isolation_check.json"
    data.write_json(out, res)
    print(
        f"{res['candidates']} candidates from {res['tasks']} tasks re-run in a fresh interpreter: "
        f"{res['agree']} agree, {res['disagree']} disagree "
        f"({res['disagree_involving_timeout']} of those involve a timeout)"
    )
    print(f"wrote {out}")
    return 0 if res["disagree"] == res["disagree_involving_timeout"] else 1


REPORT_COLUMNS = (
    ("exact", "smallest_exact"),
    ("OVERFIT", "smallest_overfit"),
    ("any plausible", "random_overfit"),
    ("at-fault oracle", "at_fault_overfit"),
)


def cmd_report(a: argparse.Namespace) -> int:
    rows = []
    for source in SOURCES:
        rows += study.read_rows(_rows_path(source))
    if not rows:
        print("no results yet: run `minimal-diff repair` first", file=sys.stderr)
        return 1
    summary = study.summarise(rows)
    for source in summary:
        with contextlib.suppress(FileNotFoundError):
            counts = [
                (len(p.hidden_inputs), p.n_near_inputs)
                for p in tasks.load_problems(source).values()
            ]
            summary[source]["hidden_inputs_per_problem"] = {
                "mean": round(statistics.mean(h for h, _ in counts), 1),
                "median": statistics.median(h for h, _ in counts),
                "mean_near": round(statistics.mean(n for _, n in counts), 1),
                "problems_with_none": sum(h == 0 for h, _ in counts),
            }
    out = data.results_dir() / "classical_repair.json"
    data.write_json(out, summary)
    for source, s in summary.items():
        print(
            f"\n== {source}: {s['tasks']} tasks over {s['problems']} problems, "
            f"{s['mean_candidates']} one-edit candidates per task"
        )
        print(f"   known fix inside the search space: {stats.fmt(s['truth_in_space'])}")
        print(
            f"   smallest by {repair.DEFAULT_METRIC}, ties by site order; "
            "95% cluster-bootstrap CI; overfit unless noted"
        )
        head = "".join(f"{h:<22}" for h, _ in REPORT_COLUMNS)
        print(f"   {'shown':<6}{'tests':>6}{'plaus.':>8}  {head}")
        for g, r in s["regimes"].items():
            cells = "".join(f"{stats.fmt(r[k]):<22}" for _, k in REPORT_COLUMNS)
            print(f"   {g:<6}{r['mean_visible_tests']:>6}{r['mean_plausible']:>8}  {cells}")
        r = s["regimes"]["all"]
        print("   every size metric, all visible asserts; last column paired vs any plausible:")
        print(
            f"   {'metric':<12}{'site-order exact':<22}{'site-order overfit':<22}"
            f"{'random-tie overfit':<22}{'site - any, overfit':<26}"
        )
        for m, t in r["by_metric"].items():
            print(
                f"   {m:<12}{stats.fmt(t['site_exact']):<22}{stats.fmt(t['site_overfit']):<22}"
                f"{stats.fmt(t['tie_overfit']):<22}"
                f"{stats.fmt_pp(t['diff_overfit_site_minus_random']):<26}"
            )
    print(f"\nwrote {out}")
    return 0


def cmd_fix(a: argparse.Namespace) -> int:
    program = Path(a.program)
    try:
        # Bytes, not text mode: keep the file's own line endings so the diff applies to it.
        source = program.read_bytes().decode("utf-8")
        tests = [userfix.check_assert(s) for s in a.asserts or []]
        if a.tests:
            found = userfix.read_asserts(Path(a.tests).read_text(encoding="utf-8"))
            for w in found.warnings:
                print(f"warning: {w}", file=sys.stderr)
            tests += found.tests
        if not tests:
            print(
                "give the asserts to repair against: --tests FILE and/or --assert STMT",
                file=sys.stderr,
            )
            return 2
        setup = Path(a.setup).read_text(encoding="utf-8") if a.setup else ""
        res = userfix.fix(
            source,
            tests,
            setup,
            a.metric,
            a.timeout,
            sys_path=[str(program.resolve().parent)],
        )
    except UnicodeDecodeError as e:
        print(f"error: {program} is not UTF-8 text: {e}", file=sys.stderr)
        return 2
    except ValueError as e:  # includes userfix.Unrunnable
        print(f"error: {e}", file=sys.stderr)
        return 2
    if a.diff:
        if res.plausible:
            sys.stdout.write(userfix.unified_diff(res.program, res.plausible[0].code, program.name))
    else:
        print(userfix.render(res, a.metric, a.top, program.name))
    # 0: repaired or nothing to repair; 1: no one-edit patch passes.
    return 0 if res.already_passes or res.plausible else 1


def cmd_show(a: argparse.Namespace) -> int:
    source = a.task_id.split("/", 1)[0]
    if source not in SOURCES:
        print(f"task ids look like mbpp/3/binop@17; got {a.task_id!r}", file=sys.stderr)
        return 2
    ts = {t.id: t for t in tasks.load_tasks(source)}
    if a.task_id not in ts:
        prefix = "/".join(a.task_id.split("/")[:2]) + "/"
        near = [i for i in ts if i.startswith(prefix)][:8]
        print(
            f"no task {a.task_id!r}. Tasks for that problem: {', '.join(near) or 'none'}",
            file=sys.stderr,
        )
        return 2
    task = ts[a.task_id]
    prob = tasks.load_problems(source)[task.problem]
    row = repair.repair_task(task, prob)
    metrics = (a.metric,) if a.metric else (repair.DEFAULT_METRIC, "tokens+ast")
    print(render_row(task, prob, row, a.regime, metrics))
    return 0


def render_row(
    task: tasks.Task,
    prob: tasks.ProblemRecord,
    row: dict,
    regime: str = "all",
    metrics: tuple[str, ...] = (repair.DEFAULT_METRIC, "tokens+ast"),
) -> str:
    """A human-readable account of one repair: the bug, the tests, every plausible patch."""
    lines = [f"task {task.id}  (injected: {task.kind}, faulty line {task.fault_lines})", ""]
    lines += ["buggy program:", _indent(task.buggy), ""]
    subset = row["subset_indices"][regime]
    lines.append(f"visible asserts shown to the repairer ({regime}):")
    for i in subset:
        lines.append(f"  [{task.visible_status[i]:>7}] {prob.tests[i]}")
    s = row["subsets"][regime]
    lines += [
        "",
        f"{row['n_candidates']} one-edit candidates, {s['n_plausible']} pass every shown assert:",
    ]
    edits = operators.neighbours(task.buggy)
    buggy_lines = task.buggy.splitlines()
    for c in row["plausible_k1"]:
        if not all(c["passes"][i] for i in subset):
            continue
        z = c["size"]
        mark = "<- the known fix" if c["is_truth"] else ""
        fault = "yes" if z["touches_fault"] else "no"
        head = f"  {c['where']:<18} tokens={z['tokens']} ast={z['ast_nodes']} "
        lines.append(f"{head}at_fault={fault:<3}  {c['verdict']:<10} {mark}".rstrip())
        new_lines = edits[c["index"]].code.splitlines()
        for old, new in zip(buggy_lines, new_lines, strict=True):
            if old != new:
                lines += [f"      - {old.strip()}", f"      + {new.strip()}"]
        if c["witness"]:
            lines.append(f"      witness: {_explain(c['witness'], prob)}")
    lines.append("")
    plausible = [
        repair.Candidate(**c) for c in row["plausible_k1"] if all(c["passes"][i] for i in subset)
    ]
    if not plausible:
        lines.append("smallest-first repair: no plausible patch")
    for metric in metrics if plausible else ():
        pick = min(plausible, key=functools.partial(repair.Candidate.order_key, metric=metric))
        lines.append(
            f"smallest by {metric:<10} (ties by site order) returns {pick.where}: "
            f"{pick.verdict.upper()}"
        )
    return "\n".join(lines)


def _explain(witness: str, prob: tasks.ProblemRecord) -> str:
    """Name the assert behind "visible assert 2 -> fail" rather than its index."""
    m = re.fullmatch(r"(visible|hidden) assert (\d+) -> (\w+)", witness)
    if not m:
        return witness
    pool = prob.tests if m.group(1) == "visible" else prob.hidden_tests
    return f"{m.group(3)}s `{pool[int(m.group(2))].strip()}`"


def _indent(code: str) -> str:
    return "\n".join(f"    {ln}".rstrip() for ln in code.splitlines())


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="minimal-diff",
        description=(
            "Program repair measured on fix size and fix correctness. Injects single-point "
            "bugs into MBPP/HumanEval references, repairs them, and checks every plausible "
            "patch against the reference on hidden inputs."
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build-tasks", help="inject bugs and build data/tasks_*.jsonl.gz")
    b.add_argument("--source", choices=(*SOURCES, "both"), default="both", help=SOURCE_HELP)
    b.add_argument("--limit", type=data.positive_int, default=None, help="first N problems only")
    b.add_argument("--workers", type=data.positive_int, default=8, help=WORKERS_HELP)
    b.set_defaults(func=cmd_build_tasks)

    r = sub.add_parser("repair", help="run the classical search on every task (resumable)")
    r.add_argument("--source", choices=(*SOURCES, "both"), default="both", help=SOURCE_HELP)
    r.add_argument("--limit", type=data.positive_int, default=None, help="first N tasks only")
    r.add_argument("--workers", type=data.positive_int, default=8, help=WORKERS_HELP)
    r.set_defaults(func=cmd_repair)

    rs = sub.add_parser(
        "rescore",
        help="recompute patch sizes in results/classical_*.jsonl.gz without re-running anything",
        description="Sizes are a pure function of the buggy program and the patch, and no "
        "verdict depends on them: after a change to the size measures this rewrites every "
        "row exactly as `repair` would, in minutes instead of an hour.",
    )
    rs.add_argument("--source", choices=(*SOURCES, "both"), default="both", help=SOURCE_HELP)
    rs.set_defaults(func=cmd_rescore)

    rep = sub.add_parser("report", help="aggregate results into results/classical_repair.json")
    rep.set_defaults(func=cmd_report)

    s = sub.add_parser("show", help="repair one task and print every plausible patch")
    s.add_argument("task_id", help="e.g. mbpp/3/binop@17")
    s.add_argument(
        "--regime",
        choices=study.REGIMES,
        default="all",
        help="how many visible asserts the repairer sees (default: all)",
    )
    s.add_argument(
        "--metric",
        choices=sorted(repair.METRICS),
        default=None,
        help=f"the size metric whose pick to report (default: {repair.DEFAULT_METRIC} and "
        "tokens+ast side by side)",
    )
    s.set_defaults(func=cmd_show)

    c = sub.add_parser(
        "check-isolation", help="re-run sampled candidates in fresh interpreters and compare"
    )
    c.add_argument("--tasks", type=data.positive_int, default=200, help="tasks sampled")
    c.add_argument("--per-task", type=data.positive_int, default=4, help="candidates per task")
    c.add_argument("--workers", type=data.positive_int, default=8, help=WORKERS_HELP)
    c.set_defaults(func=cmd_check_isolation)

    f = sub.add_parser(
        "fix",
        help="repair your own program against your own asserts",
        description=(
            "Try every one-edit change to PROGRAM and list those that make every assert "
            "pass, smallest first. Only the edited span changes, so comments and formatting "
            "survive and the diff applies to the file. Modules next to PROGRAM are importable. "
            "Exit status: 0 repaired (or nothing to repair), 1 no one-edit patch passes, "
            "2 bad input. Example: minimal-diff fix buggy.py --tests test_buggy.py"
        ),
    )
    f.add_argument("program", help="the Python file to repair")
    f.add_argument(
        "--tests",
        help="a file whose top-level `assert` statements are the tests (nested ones are "
        "reported and skipped)",
    )
    f.add_argument(
        "--assert",
        dest="asserts",
        action="append",
        metavar="STMT",
        help='one assert statement, e.g. --assert "assert f(2) == 4" (repeatable)',
    )
    f.add_argument("--setup", help="a file run after the program and before the tests")
    f.add_argument(
        "--metric",
        choices=sorted(repair.METRICS),
        default=repair.DEFAULT_METRIC,
        help=f"how patch size is measured (default: {repair.DEFAULT_METRIC})",
    )
    f.add_argument("--top", type=data.positive_int, default=5, help="patches to list (default 5)")
    f.add_argument(
        "--timeout",
        type=data.positive_seconds,
        default=2.0,
        help="seconds per assert before it counts as a hang (default 2)",
    )
    f.add_argument(
        "--diff",
        action="store_true",
        help="print only the smallest patch as a unified diff (pipe it to `git apply`)",
    )
    f.set_defaults(func=cmd_fix)

    o = sub.add_parser(
        "check-oracle", help="judge each reference plus an inert line; any overfit is a bug"
    )
    o.add_argument("--workers", type=data.positive_int, default=8, help=WORKERS_HELP)
    o.set_defaults(func=cmd_check_oracle)

    from .model import cli as model_cli

    model_cli.add_parser(sub)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

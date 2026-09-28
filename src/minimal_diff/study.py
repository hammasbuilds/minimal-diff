"""Run the classical repair search over every task, resumably, and aggregate it."""

from __future__ import annotations

import gzip
import json
from collections import Counter, defaultdict
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import repair, stats
from .repair import DEFAULT_METRIC, METRICS
from .tasks import ProblemRecord, Task

REGIMES = ("k1", "k3", "all")


def _done_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    ids = set()
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        try:
            for line in fh:
                if line.strip():
                    ids.add(json.loads(line)["id"])
        except (EOFError, json.JSONDecodeError):  # a run killed mid-write
            pass
    return ids


def read_rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        try:
            for line in fh:
                if line.strip():
                    rows.append(json.loads(line))
        except (EOFError, json.JSONDecodeError):
            pass
    return rows


def run(
    tasks: list[Task],
    problems: dict[str, ProblemRecord],
    out: Path,
    workers: int = 8,
    progress: bool = True,
) -> int:
    """Repair every task not already in `out`; append one row per task. Returns rows written.

    Appending gzip members is valid gzip, so a killed run resumes where it stopped.
    """
    done = _done_ids(out)
    todo = [t for t in tasks if t.id not in done]
    out.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = pool.map(lambda t: repair.repair_task(t, problems[t.problem]), todo)
        for row in futures:
            with gzip.open(out, "at", encoding="utf-8") as fh:
                fh.write(json.dumps(row, sort_keys=True) + "\n")
            written += 1
            if progress and written % 100 == 0:
                print(f"  {written}/{len(todo)} tasks repaired", flush=True)
    return written


# --------------------------------------------------------------------------- aggregation


def _pick(r: dict, regime: str, policy: str) -> dict | None:
    return r["subsets"][regime][policy]


def _is(label: str, regime: str, policy: str = "smallest"):
    def f(r: dict) -> float | None:
        p = _pick(r, regime, policy)
        return None if p is None else float(p["verdict"] == label)

    return f


def _sub(g: str, key: str):
    def f(r: dict) -> float | None:
        v = r["subsets"][g].get(key)
        return None if v is None else float(v)

    return f


def _paired(g: str, a, b):
    """Per-task difference of two policies' overfit indicators (a - b)."""

    def f(r: dict) -> float | None:
        x, y = a(r), b(r)
        return None if x is None or y is None else x - y

    return f


def _metric(g: str, m: str, key: str):
    """A per-task number from the `by_metric` summary of metric `m` under regime `g`."""

    def f(r: dict) -> float | None:
        bm = r["subsets"][g].get("by_metric")
        if bm is None or key not in bm[m]:
            return None
        v = bm[m][key]
        return float(v) if not isinstance(v, str) else None

    return f


def _metric_verdict(g: str, m: str, label: str):
    def f(r: dict) -> float | None:
        bm = r["subsets"][g].get("by_metric")
        return None if bm is None else float(bm[m]["site_verdict"] == label)

    return f


def _metrics_table(rs: list[dict], g: str) -> dict:
    """Every size metric side by side: site-order and random tie-breaks, paired against
    a random plausible patch (positive = the size preference is worse than no preference)."""
    cr = stats.cluster_rate
    rand_over, rand_exact = _sub(g, "random_overfit"), _sub(g, "random_exact")
    out = {}
    for m in METRICS:
        site_over, site_exact = _metric_verdict(g, m, "overfit"), _metric_verdict(g, m, "exact")
        tie_over, tie_exact = _metric(g, m, "tied_overfit"), _metric(g, m, "tied_exact")
        out[m] = {
            "site_exact": cr(rs, site_exact),
            "site_overfit": cr(rs, site_over),
            "tie_exact": cr(rs, tie_exact),
            "tie_overfit": cr(rs, tie_over),
            "diff_overfit_site_minus_random": cr(rs, _paired(g, site_over, rand_over)),
            "diff_exact_site_minus_random": cr(rs, _paired(g, site_exact, rand_exact)),
            "diff_overfit_tie_minus_random": cr(rs, _paired(g, tie_over, rand_over)),
            "diff_exact_tie_minus_random": cr(rs, _paired(g, tie_exact, rand_exact)),
            "tie_at_smallest": cr(
                rs,
                lambda r, m=m: (
                    None
                    if "by_metric" not in r["subsets"][g]
                    else float(r["subsets"][g]["by_metric"][m]["n_tied"] > 1)
                ),
            ),
            "truth_strictly_smallest": cr(rs, _metric(g, m, "truth_strictly_smallest")),
            "wrong_patch_as_small_as_truth": cr(rs, _metric(g, m, "overfit_as_small_as_truth")),
        }
    return out


def _regime(rs: list[dict], g: str, kinds: Iterable[str]) -> dict:
    cr = stats.cluster_rate
    small_over = _is("overfit", g)
    multi = [r for r in rs if r["subsets"][g]["n_plausible"] >= 2]
    reg = {
        "default_metric": DEFAULT_METRIC,
        "mean_visible_tests": round(sum(len(r["subset_indices"][g]) for r in rs) / len(rs), 2),
        "found_plausible": cr(rs, lambda r: float(r["subsets"][g]["n_plausible"] > 0)),
        "mean_plausible": round(sum(r["subsets"][g]["n_plausible"] for r in rs) / len(rs), 2),
        # Only here can any ranking matter: with one plausible patch every policy agrees.
        "tasks_with_2plus_plausible": len(multi),
        "truth_plausible": cr(rs, lambda r: float(r["subsets"][g].get("truth_plausible", False))),
        "smallest_exact": cr(rs, _is("exact", g)),
        "smallest_no_witness": cr(rs, _is("no_witness", g)),
        "smallest_overfit": cr(rs, small_over),
        "random_exact": cr(rs, _sub(g, "random_exact")),
        "random_overfit": cr(rs, _sub(g, "random_overfit")),
        "at_fault_exact": cr(rs, _is("exact", g, "at_fault")),
        "at_fault_overfit": cr(rs, _is("overfit", g, "at_fault")),
        # Paired over the same tasks, so the interval is on the difference itself.
        "diff_overfit_smallest_minus_at_fault": cr(
            rs, _paired(g, small_over, _is("overfit", g, "at_fault"))
        ),
        "by_metric": _metrics_table(rs, g),
        "by_metric_2plus_plausible": _metrics_table(multi, g) if multi else {},
        "overfit_pick_off_fault": cr(
            rs,
            lambda r: (
                None
                if (p := _pick(r, g, "smallest")) is None or p["verdict"] != "overfit"
                else float(not p["size"]["touches_fault"])
            ),
        ),
        # Correct-looking patches that compensate for the bug somewhere else.
        "no_witness_pick_off_fault": cr(
            rs,
            lambda r: (
                None
                if (p := _pick(r, g, "smallest")) is None or p["verdict"] != "no_witness"
                else float(not p["size"]["touches_fault"])
            ),
        ),
        "overfit_pick_found_by": dict(
            Counter(
                p["found_by"]
                for r in rs
                if (p := _pick(r, g, "smallest")) is not None and p["verdict"] == "overfit"
            ).most_common()
        ),
    }
    reg["by_bug_kind"] = {}
    for k in kinds:
        ks = [r for r in rs if r["bug_kind"] == k]
        reg["by_bug_kind"][k] = {
            "tasks": len(ks),
            "smallest_exact": cr(ks, _is("exact", g)),
            "smallest_no_witness": cr(ks, _is("no_witness", g)),
            "smallest_overfit": cr(ks, _is("overfit", g)),
            "random_exact": cr(ks, _sub(g, "random_exact")),
            "random_overfit": cr(ks, _sub(g, "random_overfit")),
            "by_metric": {
                m: {
                    "site_exact": cr(ks, _metric_verdict(g, m, "exact")),
                    "site_overfit": cr(ks, _metric_verdict(g, m, "overfit")),
                    "tie_exact": cr(ks, _metric(g, m, "tied_exact")),
                    "tie_overfit": cr(ks, _metric(g, m, "tied_overfit")),
                }
                for m in METRICS
            },
        }
    # Robustness of the default metric to the bracket rule: the same policy counting every
    # token, grouping brackets included, paired task by task.
    raw_exact, raw_over = (
        _metric_verdict(g, "tokens-raw", "exact"),
        _metric_verdict(g, "tokens-raw", "overfit"),
    )
    tok_exact, tok_over = (
        _metric_verdict(g, "tokens", "exact"),
        _metric_verdict(g, "tokens", "overfit"),
    )
    reg["tokens_vs_tokens_raw"] = {
        "tasks_pick_differs": sum(
            1
            for r in rs
            if (bm := r["subsets"][g].get("by_metric"))
            and bm["tokens"]["site_where"] != bm["tokens-raw"]["site_where"]
        ),
        "tokens_raw_site_exact": cr(rs, raw_exact),
        "tokens_raw_site_overfit": cr(rs, raw_over),
        "diff_exact_tokens_minus_raw": cr(rs, _paired(g, tok_exact, raw_exact)),
        "diff_overfit_tokens_minus_raw": cr(rs, _paired(g, tok_over, raw_over)),
    }
    rest = [r for r in rs if r["bug_kind"] != "negate_if"]
    reg["excluding_negate_if"] = _metrics_table(rest, g) if rest else {}
    return reg


def summarise(rows: Iterable[dict]) -> dict:
    """Every number the README quotes, per benchmark and per visible-test regime."""
    rows = list(rows)
    out: dict = {}
    by_source: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_source[r["source"]].append(r)
    for source, rs in sorted(by_source.items()):
        kinds = dict(Counter(r["bug_kind"] for r in rs).most_common())
        out[source] = {
            "tasks": len(rs),
            "problems": len({r["problem"] for r in rs}),
            "mean_candidates": round(sum(r["n_candidates"] for r in rs) / len(rs), 1),
            "timeouts_per_task": round(sum(r["n_timeouts"] for r in rs) / len(rs), 2),
            "truth_in_space": stats.cluster_rate(rs, lambda r: float(r["truth_in_space"])),
            "bug_kinds": kinds,
            "regimes": {g: _regime(rs, g, kinds) for g in REGIMES},
            "patch_size_all": _sizes(rs, "all"),
            "patch_size_plausible_all": _size_shares(rs, "all"),
        }
    return out


def _size_shares(rs: list[dict], g: str) -> dict:
    """How small the plausible patches are, pooled over every task, under regime `g`.

    This is the scope statement in the README ("x% change exactly one token"), under both
    token counts so the bracket rule cannot hide in it.
    """
    sizes = [
        c["size"]
        for r in rs
        for c in r["plausible_k1"]
        if all(c["passes"][i] for i in r["subset_indices"][g])
    ]
    n = len(sizes)

    def share(pred) -> float | None:
        return round(sum(1 for z in sizes if pred(z)) / n, 4) if n else None

    return {
        "patches": n,
        "one_token": share(lambda z: z["tokens"] == 1),
        "one_token_one_node": share(lambda z: z["tokens"] == 1 and z["ast_nodes"] == 1),
        "one_token_raw": share(lambda z: z["tokens_raw"] == 1),
        "one_token_raw_one_node": share(lambda z: z["tokens_raw"] == 1 and z["ast_nodes"] == 1),
        "tokens_below_raw": share(lambda z: z["tokens"] < z["tokens_raw"]),
    }


def rescore_file(path: Path, tasks_by_id: dict[str, Task]) -> dict:
    """Recompute sizes and size-based picks in a results file in place (see `repair.rescore`).

    Returns how many rows there were and how many picks changed, per metric.
    """
    rows = read_rows(path)
    changed: Counter = Counter()
    new_rows = []
    for r in rows:
        nr = repair.rescore(r, tasks_by_id[r["id"]])
        old = [repair.Candidate(**c) for c in r["plausible_k1"]]
        new = [repair.Candidate(**c) for c in nr["plausible_k1"]]
        changed["sizes"] += sum(
            any(a.size.get(k) != v for k, v in b.size.items() if k in a.size)
            for a, b in zip(old, new, strict=True)
        )
        for g, subset in r["subset_indices"].items():
            for m, fields in METRICS.items():
                if not old or any(f not in old[0].size for f in fields):
                    continue  # the old rows had no such measure: nothing to compare
                a = repair.select(old, subset, "smallest", m)
                b = repair.select(new, subset, "smallest", m)
                if a is not None and b is not None and a.index != b.index:
                    changed[f"pick/{g}/{m}"] += 1
        new_rows.append(nr)
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        for nr in new_rows:
            fh.write(json.dumps(nr, sort_keys=True) + "\n")
    tmp.replace(path)
    return {"rows": len(rows), "changed": dict(sorted(changed.items()))}


def _sizes(rs: list[dict], g: str) -> dict:
    """Patch size by verdict, over every plausible patch under regime `g`."""
    acc: dict[str, list[dict]] = defaultdict(list)
    for r in rs:
        subset = r["subset_indices"][g]
        for c in r["plausible_k1"]:
            if all(c["passes"][i] for i in subset):
                acc[c["verdict"]].append(c["size"])
    out = {}
    for verdict, sizes in sorted(acc.items()):
        n = len(sizes)
        out[verdict] = {
            "patches": n,
            "mean_tokens": round(sum(x["tokens"] for x in sizes) / n, 3),
            "mean_ast_nodes": round(sum(x["ast_nodes"] or 0 for x in sizes) / n, 3),
            "mean_lines": round(sum(x["lines"] for x in sizes) / n, 3),
            "share_touching_fault": round(sum(x["touches_fault"] for x in sizes) / n, 4),
        }
    return out

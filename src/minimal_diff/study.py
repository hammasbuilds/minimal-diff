"""Run the classical repair search over every task, resumably, and aggregate it."""

from __future__ import annotations

import gzip
import json
from collections import Counter, defaultdict
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import repair, stats
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


def _regime(rs: list[dict], g: str, kinds: Iterable[str]) -> dict:
    cr = stats.cluster_rate
    small_over = _is("overfit", g)
    reg = {
        "mean_visible_tests": round(sum(len(r["subset_indices"][g]) for r in rs) / len(rs), 2),
        "found_plausible": cr(rs, lambda r: float(r["subsets"][g]["n_plausible"] > 0)),
        "mean_plausible": round(sum(r["subsets"][g]["n_plausible"] for r in rs) / len(rs), 2),
        "truth_plausible": cr(rs, lambda r: float(r["subsets"][g].get("truth_plausible", False))),
        "smallest_exact": cr(rs, _is("exact", g)),
        "smallest_no_witness": cr(rs, _is("no_witness", g)),
        "smallest_overfit": cr(rs, small_over),
        "tied_exact": cr(rs, _sub(g, "tied_exact")),
        "tied_overfit": cr(rs, _sub(g, "tied_overfit")),
        "random_exact": cr(rs, _sub(g, "random_exact")),
        "random_overfit": cr(rs, _sub(g, "random_overfit")),
        "at_fault_exact": cr(rs, _is("exact", g, "at_fault")),
        "at_fault_overfit": cr(rs, _is("overfit", g, "at_fault")),
        # Paired over the same tasks, so the interval is on the difference itself.
        "diff_overfit_tied_minus_random": cr(
            rs, _paired(g, _sub(g, "tied_overfit"), _sub(g, "random_overfit"))
        ),
        "diff_overfit_smallest_minus_random": cr(
            rs, _paired(g, small_over, _sub(g, "random_overfit"))
        ),
        "diff_overfit_smallest_minus_at_fault": cr(
            rs, _paired(g, small_over, _is("overfit", g, "at_fault"))
        ),
        "tie_at_smallest": cr(
            rs,
            lambda r: (
                None
                if r["subsets"][g]["n_plausible"] == 0
                else float(r["subsets"][g]["n_tied_smallest"] > 1)
            ),
        ),
        "wrong_patch_as_small_as_truth": cr(rs, _sub(g, "overfit_as_small_as_truth")),
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
        }
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
        }
    return out


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

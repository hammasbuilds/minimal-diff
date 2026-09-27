"""Rates with honest uncertainty: a cluster bootstrap over problems.

Tasks are not independent. One MBPP problem with a long solution contributes thirty
mutants, and they share a test suite, so they succeed or fail together. Resampling tasks
would treat those thirty as thirty independent observations and draw a confidence
interval far narrower than the data supports. Resampling *problems* - and taking every
task of each drawn problem - does not.
"""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Callable, Sequence


def cluster_rate(
    rows: Sequence[dict],
    value: Callable[[dict], float | None],
    cluster: str = "problem",
    n_boot: int = 1000,
    seed: int = 0,
) -> dict:
    """Mean of `value` over rows (skipping None), with a 95% cluster-bootstrap interval.

    Also reports the cluster-weighted mean (each problem counts once), because a rate
    that moves a lot between the two is being driven by a few large problems.
    """
    groups: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        v = value(r)
        if v is not None:
            groups[r[cluster]].append(float(v))
    keys = sorted(groups)
    n = sum(len(groups[k]) for k in keys)
    if n == 0:
        return {"n": 0, "n_clusters": 0, "rate": None, "ci95": None, "rate_per_problem": None}
    sums = [sum(groups[k]) for k in keys]
    counts = [len(groups[k]) for k in keys]
    rate = sum(sums) / n
    per_problem = sum(s / c for s, c in zip(sums, counts, strict=True)) / len(keys)
    rng = random.Random(seed)
    boots = []
    m = len(keys)
    for _ in range(n_boot):
        idx = [rng.randrange(m) for _ in range(m)]
        tot = sum(counts[i] for i in idx)
        boots.append(sum(sums[i] for i in idx) / tot)
    boots.sort()
    lo, hi = boots[int(0.025 * n_boot)], boots[int(0.975 * n_boot) - 1]
    return {
        "n": n,
        "n_clusters": m,
        "rate": round(rate, 4),
        "ci95": [round(lo, 4), round(hi, 4)],
        "rate_per_problem": round(per_problem, 4),
    }


def fmt(r: dict, pct: bool = True) -> str:
    """`41.2% [38.0, 44.5]` - the form every table in the README uses."""
    if not r or r.get("rate") is None:
        return "n/a"
    if pct:
        lo, hi = r["ci95"]
        return f"{100 * r['rate']:.1f}% [{100 * lo:.1f}, {100 * hi:.1f}]"
    lo, hi = r["ci95"]
    return f"{r['rate']:.2f} [{lo:.2f}, {hi:.2f}]"

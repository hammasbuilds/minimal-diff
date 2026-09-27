import pytest

from minimal_diff import stats


def rows(spec):
    """spec: {problem: [values]}"""
    return [{"problem": p, "v": v} for p, vs in spec.items() for v in vs]


def test_rate_and_per_problem_rate_differ_when_one_problem_dominates():
    r = stats.cluster_rate(rows({"a": [1] * 9, "b": [0]}), lambda x: x["v"])
    assert r["rate"] == pytest.approx(0.9)
    assert r["rate_per_problem"] == pytest.approx(0.5)
    assert (r["n"], r["n_clusters"]) == (10, 2)


def test_interval_contains_the_rate_and_is_wider_for_clustered_data():
    independent = rows({str(i): [i % 2] for i in range(200)})
    clustered = rows({str(i): [i % 2] * 10 for i in range(20)})
    a = stats.cluster_rate(independent, lambda x: x["v"])
    b = stats.cluster_rate(clustered, lambda x: x["v"])
    for r in (a, b):
        assert r["ci95"][0] <= r["rate"] <= r["ci95"][1]
    width = lambda r: r["ci95"][1] - r["ci95"][0]  # noqa: E731
    # same 200 observations and the same rate; 20 clusters carry far less information
    assert width(b) > 2 * width(a)


def test_none_values_are_skipped_and_empty_is_reported_not_zero():
    r = stats.cluster_rate([{"problem": "a", "v": None}], lambda x: x["v"])
    assert r["n"] == 0 and r["rate"] is None
    assert stats.fmt(r) == "n/a"


def test_deterministic_with_seed():
    data = rows({str(i): [i % 3 == 0, 1] for i in range(30)})
    assert stats.cluster_rate(data, lambda x: x["v"]) == stats.cluster_rate(data, lambda x: x["v"])


def test_fmt():
    r = {"rate": 0.4123, "ci95": [0.38, 0.445]}
    assert stats.fmt(r) == "41.2% [38.0, 44.5]"

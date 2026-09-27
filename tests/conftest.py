"""Shared fixtures. No dataset, no network, no model: every test builds its own inputs."""

from __future__ import annotations

import pytest

from minimal_diff import data

# A problem whose three asserts are weak on purpose: they never exercise n == 0 or the
# boundary between the two branches, so several wrong one-edit programs pass them.
CLAMP_REFERENCE = """\
def clamp_sum(xs, cap):
    total = 0
    for x in xs:
        if x > 0:
            total += x
    if total > cap:
        return cap
    return total
"""

CLAMP_TESTS = (
    "assert clamp_sum([1, 2, 3], 10) == 6",
    "assert clamp_sum([5, 5, 5], 7) == 7",
    "assert clamp_sum([-1, 4], 10) == 4",
)


@pytest.fixture
def clamp_problem() -> data.Problem:
    return data.Problem(
        source="mbpp",
        pid=9001,
        text="Sum the positive numbers in a list, capped at `cap`.",
        reference=CLAMP_REFERENCE,
        entry_point="clamp_sum",
        tests=CLAMP_TESTS,
    )


@pytest.fixture(autouse=True)
def _no_real_data(tmp_path_factory, monkeypatch):
    """Point data and results at empty folders: no test can read or overwrite the real ones."""
    monkeypatch.setenv("MINIMAL_DIFF_DATA", str(tmp_path_factory.mktemp("empty-data")))
    monkeypatch.setenv("MINIMAL_DIFF_RESULTS", str(tmp_path_factory.mktemp("empty-results")))

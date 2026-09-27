from minimal_diff import sandbox

OK = "def f(x):\n    return x + 1\n"
WRONG = "def f(x):\n    return x - 1\n"
HANG = "def f(x):\n    while True:\n        pass\n"
CRASH = "import os\ndef f(x):\n    os._exit(3)\n"
HOG = "def f(x):\n    return len([0] * (10 ** 9))\n"
NOISY = "import sys\ndef f(x):\n    print('__MBPP_OK__ noise')\n    sys.stderr.write('x')\n    return x + 1\n"
TESTS = ["assert f(1) == 2", "assert f(2) == 3"]


def statuses(rows):
    return [[r.status for r in row] for row in rows]


def test_outcomes_are_kept_distinct():
    rows = sandbox.run_asserts([OK, WRONG, "def f(x):\n    return 1 / 0\n"], TESTS, item_timeout=3)
    assert statuses(rows) == [["pass", "pass"], ["fail", "skipped"], ["error", "skipped"]]
    assert rows[2][0].detail == "ZeroDivisionError"


def test_hang_and_crash_do_not_take_later_candidates_with_them():
    rows = sandbox.run_asserts([HANG, OK, CRASH, OK], TESTS, item_timeout=1.5)
    assert statuses(rows) == [
        ["timeout", "skipped"],
        ["pass", "pass"],
        ["crash", "skipped"],
        ["pass", "pass"],
    ]


def test_memory_hog_is_stopped_not_the_machine():
    [row] = sandbox.run_asserts([HOG], TESTS[:1], item_timeout=10)
    # MemoryError under the cap; a kill by the OS would show up as a crash. Never a pass.
    assert row[0].status in ("error", "crash")


def test_candidate_output_cannot_forge_a_result():
    rows = sandbox.run_asserts([NOISY], TESTS)
    assert statuses(rows) == [["pass", "pass"]]


def test_no_early_stop_runs_every_assert():
    rows = sandbox.run_asserts(
        [WRONG], ["assert f(1) == 2", "assert f(1) == 0"], stop_on_first_failure=False
    )
    assert statuses(rows) == [["fail", "pass"]]


def test_setup_runs_after_the_candidate():
    code = "class Box:\n    def __init__(self, v):\n        self.v = v\n"
    rows = sandbox.run_asserts([code], ["assert b.v == 3"], setup="b = Box(3)")
    assert statuses(rows) == [["pass"]]


def test_compare_tolerates_float_noise_and_compares_numbers_by_value():
    ref = "def g(x):\n    return x / 3\n"
    close = "def g(x):\n    return x * (1 / 3)\n"
    as_int = "def g(x):\n    return x // 3\n"
    rows = sandbox.run_compare(ref, "g", [close, as_int], ["9", "10"])
    assert [r.status for r in rows[0]] == ["same", "same"]
    # 3 == 3.0, exactly as an `assert g(9) == 3.0` would judge it; 3 != 3.33 is caught.
    assert [r.status for r in rows[1]] == ["same", "differs"]
    assert rows[1][1].ref == "3.3333333333333335" and rows[1][1].cand == "3"


def test_compare_bool_is_not_int():
    ref = "def h(x):\n    return x > 0\n"
    rows = sandbox.run_compare(ref, "h", ["def h(x):\n    return 1 if x > 0 else 0\n"], ["5"])
    assert rows[0][0].status == "differs"


def test_compare_counts_a_raise_as_a_difference_and_reports_it():
    ref = "def h(xs):\n    return xs[0]\n"
    rows = sandbox.run_compare(ref, "h", ["def h(xs):\n    return xs[1]\n"], ["[7]"])
    assert rows[0][0].status == "differs"
    assert rows[0][0].cand == "raises IndexError"


def test_compare_evaluates_arguments_fresh_for_each_side():
    # A reference that mutates its argument must not hand the candidate a mutated copy.
    ref = "def p(xs):\n    xs.append(1)\n    return len(xs)\n"
    rows = sandbox.run_compare(ref, "p", [ref], ["[1, 2]"])
    assert rows[0][0].status == "same"


def test_first_failure_ignores_skips():
    r = [sandbox.Result("pass"), sandbox.Result("skipped"), sandbox.Result("fail")]
    assert sandbox.first_failure(r).status == "fail"
    assert sandbox.first_failure(r[:2]) is None


SWALLOW = "def f(x):\n    while True:\n        try:\n            while True:\n                pass\n        except BaseException:\n            pass\n"
SLEEP = "import time\ndef f(x):\n    time.sleep(60)\n"


def test_a_python_level_hang_is_interrupted_inside_the_worker():
    rows = sandbox.run_asserts([HANG, OK], TESTS, stop_on_first_failure=False, item_timeout=0.5)
    # Without stopping, every assert of the hanging candidate still gets its own verdict.
    assert statuses(rows) == [["timeout", "timeout"], ["pass", "pass"]]


def test_hangs_the_worker_cannot_interrupt_fall_back_to_a_kill():
    rows = sandbox.run_asserts([SWALLOW, SLEEP, OK], TESTS[:1], item_timeout=0.5)
    assert statuses(rows) == [["timeout"], ["timeout"], ["pass"]]


def test_a_timeout_is_not_cached_as_a_load_failure():
    slow_import = "import time\nfor _ in range(10**9):\n    pass\ndef f(x):\n    return x + 1\n"
    rows = sandbox.run_asserts([slow_import], TESTS, stop_on_first_failure=False, item_timeout=0.3)
    assert statuses(rows) == [["timeout", "timeout"]]


def test_worker_reuse_can_leak_state_which_is_why_isolation_is_measured():
    """A candidate that monkeypatches a builtin changes the next one's result in a reused
    worker. `minimal-diff check-isolation` exists to measure how often that happens on the
    real tasks; this shows the check is looking for something real."""
    poison = "import builtins\nbuiltins.abs = lambda x: 0\ndef f(x):\n    return x + 1\n"
    victim = "def f(x):\n    return abs(x) + 1\n"
    pooled = sandbox.run_asserts([poison, victim], ["assert f(-1) == 2"], fresh=True)
    alone = sandbox.run_asserts([victim], ["assert f(-1) == 2"], fresh=True)
    assert statuses(pooled)[1] == ["fail"]
    assert statuses(alone) == [["pass"]]


def test_compare_arguments_may_name_what_the_program_or_setup_defines():
    ref = "class Pair:\n    def __init__(self, a):\n        self.a = a\ndef first(ps):\n    return ps[0].a\n"
    wrong = ref.replace("ps[0]", "ps[-1]")
    rows = sandbox.run_compare(
        ref, "first", [ref, wrong], ["[Pair(1), Pair(2)]", "box"], setup="box = [Pair(7)]"
    )
    assert [r.status for r in rows[0]] == ["same", "same"]
    assert rows[0][0].ref_status == "ok"
    assert rows[1][0].status == "differs"


def test_instances_of_the_programs_own_classes_compare_by_value():
    """Reference and patch are exec'd separately, so `Pair` is two distinct classes."""
    ref = "class Pair:\n    def __init__(self, a):\n        self.a = a\ndef mk(x):\n    return [Pair(x)]\n"
    other = ref.replace("Pair(x)", "Pair(x + 1)")
    rows = sandbox.run_compare(ref, "mk", [ref, other], ["3"])
    assert rows[0][0].status == "same"
    assert rows[1][0].status == "differs"


def test_every_item_reports_how_long_it_took():
    rows = sandbox.run_asserts(
        ["import time\ndef f(x):\n    time.sleep(0.2)\n    return x + 1\n"], ["assert f(1) == 2"]
    )
    assert 0.15 < rows[0][0].seconds < 5

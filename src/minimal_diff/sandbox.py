"""Run untrusted candidate code in a child process that can hang, crash or eat memory.

The shape follows `sandbox.py` in hammasbuilds/mbpp-false-accepts (one subprocess, a
wall-clock timeout, outcomes kept distinct rather than collapsed into pass/fail), with two
changes that a repair search needs:

- **Many items per process, and processes reused.** A repair search runs tens of
  candidate patches against every task. Starting an interpreter took 0.6 s on the
  (busy) machine this was built on - more than the tests themselves - so each calling
  thread keeps one child alive, sends it whole jobs over stdin, and reads one result line
  per item back. If an item hangs or kills the interpreter, the parent knows exactly
  which one it was (the next one without a result), records it, and resumes on a fresh
  child *after* it. Nothing is lost and nothing is run twice.
- **A memory ceiling.** Changing `n + 1` to `n * 1` inside `[0] * ...` is an ordinary
  mutation. On Windows each child is placed in a Job Object with a per-process memory
  limit; elsewhere `RLIMIT_AS` does the same. A candidate that asks for 8 GB gets a
  MemoryError instead of taking the machine with it.

Items can belong to a group (one candidate patch). When an item in a group fails, the
rest of that group is skipped - one failing assert is enough to reject a patch.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

CHILD = Path(__file__).resolve().parent / "_child.py"
MARK = "\x1eRESULT "
DONE = "\x1eDONE"

# Seconds one item may run. The child interrupts an overrunning item itself; the parent
# kills the child only if that fails (code stuck in one long C call, or swallowing the
# interrupt), after the extra KILL_GRACE.
ITEM_TIMEOUT = 2.0
KILL_GRACE = 1.5
STARTUP_GRACE = 10.0  # extra allowance for interpreter start-up on a loaded machine
MEMORY_LIMIT_MB = 512


@dataclass(frozen=True)
class Result:
    """What happened to one item.

    status is one of: pass, fail, error (assert mode); same, differs, incomparable
    (compare mode: the two return values cannot be judged either way);
    timeout, crash (either mode); skipped (an earlier item in its group already failed).
    """

    status: str
    detail: str = ""
    ref: str = ""
    cand: str = ""
    ref_status: str = ""
    seconds: float = 0.0  # wall time of the whole item inside the child (0 if it never reported)


@dataclass
class Job:
    mode: str  # "asserts" | "compare"
    items: list[dict]
    ctx: dict = field(default_factory=dict)
    stop_on: tuple[str, ...] = ()


# --------------------------------------------------------------------------- memory cap


def _windows_job_handle(limit_bytes: int):  # pragma: no cover - platform specific
    import ctypes
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [
            (n, ctypes.c_ulonglong)
            for n in ("r_ops", "w_ops", "o_ops", "r_bytes", "w_bytes", "o_bytes")
        ]

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimits),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    limit_process_memory = 0x100
    kill_on_job_close = 0x2000
    extended_limit_information = 9

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    job = k32.CreateJobObjectW(None, None)
    if not job:
        return None, k32
    info = ExtendedLimits()
    info.BasicLimitInformation.LimitFlags = limit_process_memory | kill_on_job_close
    info.ProcessMemoryLimit = limit_bytes
    ok = k32.SetInformationJobObject(
        job, extended_limit_information, ctypes.byref(info), ctypes.sizeof(info)
    )
    return (job if ok else None), k32


_JOB_LOCK = threading.Lock()
_JOB: tuple | None = None


def _cap_windows(proc: subprocess.Popen, limit_mb: int) -> bool:  # pragma: no cover
    """Put `proc` in one shared Job Object whose per-process memory limit is `limit_mb`.

    Kill-on-close also means no child outlives this process if it is interrupted.
    """
    global _JOB
    with _JOB_LOCK:
        if _JOB is None:
            _JOB = _windows_job_handle(limit_mb * 1024 * 1024)
    job, k32 = _JOB
    if job is None:
        return False
    return bool(k32.AssignProcessToJobObject(job, int(proc._handle)))  # type: ignore[attr-defined]


def _posix_limit(limit_mb: int):  # pragma: no cover - platform specific
    def apply() -> None:
        import resource

        b = limit_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (b, b))

    return apply


# --------------------------------------------------------------------------- workers


class _Worker:
    """One long-lived child interpreter, fed jobs over stdin.

    Replaced whenever an item hangs or kills it, and recycled after `RECYCLE_AFTER`
    jobs so that whatever state candidates leave behind in the interpreter cannot build
    up. Each thread of the caller owns its own worker; nothing is shared.
    """

    def __init__(self, limit_mb: int):
        self.limit_mb = limit_mb
        self.jobs = 0
        self._tmp = tempfile.mkdtemp(prefix="mdiff-")
        env = dict(os.environ)
        # Set iteration order is part of a program's output (`str(set(...))`); fixing
        # the seed makes a candidate's verdict the same on every run.
        env["PYTHONHASHSEED"] = "0"
        env.pop("PYTHONPATH", None)
        kwargs: dict = {}
        if os.name != "nt":
            kwargs["preexec_fn"] = _posix_limit(limit_mb)
        self.proc = subprocess.Popen(
            [sys.executable, "-B", "-s", str(CHILD)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            # A candidate that writes files writes them in a throwaway directory.
            cwd=self._tmp,
            env=env,
            **kwargs,
        )
        if os.name == "nt":
            _cap_windows(self.proc, limit_mb)
        _ALL.add(self)
        self.fresh = True
        self.lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def send(self, payload: dict) -> bool:
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write(json.dumps(payload) + "\n")
            self.proc.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()  # our own child, by handle
        self.proc.wait()
        for stream in (self.proc.stdin, self.proc.stdout):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()
        shutil.rmtree(self._tmp, ignore_errors=True)
        _ALL.discard(self)


RECYCLE_AFTER = 200
_LOCAL = threading.local()
_ALL: set[_Worker] = set()


@atexit.register
def _close_all() -> None:
    """Stop every worker before the interpreter exits, so its temp directory can go too."""
    for w in list(_ALL):
        w.close()


def _worker(limit_mb: int) -> _Worker:
    w: _Worker | None = getattr(_LOCAL, "worker", None)
    if w is None or w.proc.poll() is not None or w.jobs >= RECYCLE_AFTER or w.limit_mb != limit_mb:
        if w is not None:
            w.close()
        w = _Worker(limit_mb)
        _LOCAL.worker = w
    return w


def _discard_worker() -> None:
    w: _Worker | None = getattr(_LOCAL, "worker", None)
    if w is not None:
        w.close()
        _LOCAL.worker = None


def run_job(
    job: Job,
    item_timeout: float = ITEM_TIMEOUT,
    memory_limit_mb: int = MEMORY_LIMIT_MB,
    fresh: bool = False,
) -> list[Result]:
    """Run every item of `job`; one Result per item, in order.

    Replaces the worker after a hang or a crash and resumes after the item that caused
    it, so one bad candidate costs one timeout rather than the rest of the batch.
    `fresh=True` runs the job on a brand-new interpreter and discards it afterwards -
    slower, and used only to check that reusing workers changes no verdict.
    """
    n = len(job.items)
    out: list[Result | None] = [None] * n
    start = 0
    if fresh:
        _discard_worker()
    while start < n:
        start = _drive(job, start, out, item_timeout, memory_limit_mb)
    if fresh:
        _discard_worker()
    return [r if r is not None else Result("skipped") for r in out]


def _drive(
    job: Job, start: int, out: list[Result | None], item_timeout: float, limit_mb: int
) -> int:
    """Run `job` from `start` on this thread's worker; return where to resume (len = done)."""
    n = len(out)
    try:
        w = _worker(limit_mb)
    except OSError as exc:  # process table exhaustion under heavy parallelism
        for i in range(start, n):
            out[i] = Result("error", f"spawn failed: {exc}")
        return n
    payload = {
        "mode": job.mode,
        "items": job.items,
        "ctx": job.ctx,
        "stop_on": list(job.stop_on),
        "start": start,
        "item_timeout": item_timeout,
    }
    w.jobs += 1
    if not w.send(payload):
        _discard_worker()
        return _after_failure(job, out, start - 1, "crash")
    last = start - 1
    wait = item_timeout + KILL_GRACE + (STARTUP_GRACE if w.fresh else 0.0)
    w.fresh = False
    while True:
        try:
            line = w.lines.get(timeout=wait)
        except queue.Empty:
            _discard_worker()
            return _after_failure(job, out, last, "timeout")
        wait = item_timeout + KILL_GRACE
        if line is None:  # the interpreter died without saying DONE
            _discard_worker()
            return _after_failure(job, out, last, "crash")
        if line.startswith(DONE):
            return n
        if not line.startswith(MARK):
            continue
        d = json.loads(line[len(MARK) :])
        i = d.pop("i")
        out[i] = Result(
            status=d.get("status", "error"),
            detail=d.get("detail", ""),
            ref=d.get("ref", ""),
            cand=d.get("cand", ""),
            ref_status=d.get("ref_status", ""),
            seconds=d.get("s", 0.0),
        )
        last = i


def _after_failure(job: Job, out: list[Result | None], last: int, status: str) -> int:
    """Blame the first item after `last` that the child had not yet skipped, then resume.

    The child silently skips items whose group has already stopped, so "the item after
    the last result" is not necessarily the one it was running.
    """
    stopped = {
        job.items[i].get("g")
        for i in range(last + 1)
        if out[i] is not None and out[i].status in job.stop_on  # type: ignore[union-attr]
    }
    culprit = last + 1
    while culprit < len(out) and job.items[culprit].get("g") in stopped - {None}:
        culprit += 1
    if culprit >= len(out):
        return len(out)
    out[culprit] = Result(status)
    g = job.items[culprit].get("g")
    nxt = culprit + 1
    if g is not None and job.stop_on:
        # In stop-at-first-failure mode a patch that hung or crashed is rejected; do not
        # re-run it. Without it (the full pass/fail matrix) every item still runs.
        while nxt < len(out) and job.items[nxt].get("g") == g:
            nxt += 1
    return nxt


def run_asserts(
    candidates: Sequence[str],
    tests: Sequence[str],
    setup: str = "",
    stop_on_first_failure: bool = True,
    item_timeout: float = ITEM_TIMEOUT,
    fresh: bool = False,
) -> list[list[Result]]:
    """Every candidate against every test. Returns `[candidate][test]`."""
    items = [{"g": ci, "code": c, "test": t} for ci, c in enumerate(candidates) for t in tests]
    stop = ("fail", "error", "timeout") if stop_on_first_failure else ()
    res = run_job(
        Job("asserts", items, {"setup": setup}, stop), item_timeout=item_timeout, fresh=fresh
    )
    k = len(tests)
    return [res[ci * k : (ci + 1) * k] for ci in range(len(candidates))]


def run_compare(
    reference: str,
    fn: str,
    candidates: Sequence[str],
    inputs: Sequence[str],
    setup: str = "",
    stop_on_first_difference: bool = True,
    item_timeout: float = ITEM_TIMEOUT,
) -> list[list[Result]]:
    """Every candidate against the reference on every input. Returns `[candidate][input]`."""
    items = [{"g": ci, "code": c, "input": x} for ci, c in enumerate(candidates) for x in inputs]
    stop = ("differs", "timeout") if stop_on_first_difference else ()
    ctx = {"ref": reference, "fn": fn, "setup": setup}
    res = run_job(Job("compare", items, ctx, stop), item_timeout=item_timeout)
    k = len(inputs)
    return [res[ci * k : (ci + 1) * k] for ci in range(len(candidates))]


def first_failure(results: Iterable[Result], ok: str | tuple[str, ...] = "pass") -> Result | None:
    """The first result whose status is neither in `ok` nor skipped, if any."""
    good = (ok,) if isinstance(ok, str) else ok
    for r in results:
        if r.status not in good and r.status != "skipped":
            return r
    return None

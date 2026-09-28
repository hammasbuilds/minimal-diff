"""The process that actually runs untrusted candidate code. Started by `sandbox.py`.

Reads jobs from stdin, one per line. For each, runs its items in order starting at
`start`, and writes one result line per item to the real stdout the moment the item
finishes, so a parent watching the stream knows exactly which item a hang or a crash
happened on and can restart after it. Then waits for the next job: the interpreter is
reused until something goes wrong with it, because on a busy Windows machine starting
one costs more than the tests it runs.

Standard library only, and nothing imported from this package: it runs as a bare script
under whatever interpreter the parent uses.
"""

from __future__ import annotations

import ctypes
import io
import json
import math
import os
import sys
import threading
import time
import types

MARK = "\x1eRESULT "
DONE = "\x1eDONE"
RECURSION_LIMIT = 3000

_OUT = sys.stdout


class ItemTimeout(BaseException):
    """Raised inside candidate code by the watchdog. BaseException, so `except Exception`
    in a candidate cannot swallow it; a bare `except:` can, and is re-interrupted every
    50 ms until the parent gives up and kills the process."""


class _Watchdog:
    """Interrupts the main thread when an item overruns, without killing the process.

    Uses `PyThreadState_SetAsyncExc`, which takes effect between bytecodes. That covers
    the common case - a mutated loop condition that never becomes false - at the cost of
    a timeout instead of a process restart. Code stuck inside one long C call (a huge
    `pow`, `time.sleep`) is not interrupted; the parent's own deadline catches those.
    """

    def __init__(self) -> None:
        self._main: int = threading.get_ident()  # built on the main thread
        self._lock = threading.Lock()
        self._deadline: float | None = None
        threading.Thread(target=self._watch, daemon=True).start()

    def _set_exc(self, exc: object) -> None:
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(self._main), exc)

    def _watch(self) -> None:
        while True:
            time.sleep(0.02)
            with self._lock:
                d = self._deadline
                if d is not None and time.monotonic() > d:
                    self._set_exc(ctypes.py_object(ItemTimeout))
                    self._deadline = time.monotonic() + 0.05

    def arm(self, seconds: float) -> None:
        with self._lock:
            self._deadline = time.monotonic() + seconds

    def disarm(self) -> None:
        with self._lock:
            self._deadline = None
            self._set_exc(None)  # clear one that was raised but not yet delivered


def _emit(obj: dict) -> None:
    _OUT.write(MARK + json.dumps(obj) + "\n")
    _OUT.flush()


class Incomparable(Exception):
    """The two values cannot be judged equal or different from their values alone."""


_OPAQUE = (
    types.FunctionType,
    types.BuiltinFunctionType,
    types.MethodType,
    types.ModuleType,
    types.GeneratorType,
    types.CodeType,
    type,
)


def _code_key(code: types.CodeType) -> tuple:
    consts = tuple(
        _code_key(c) if isinstance(c, types.CodeType) else repr(c) for c in code.co_consts
    )
    return (code.co_code, code.co_names, code.co_varnames, consts)


def _class_attrs(cls: type) -> dict:
    """Class-level names a program defined (whole MRO except `object`), dunders aside."""
    out: dict = {}
    for k in reversed(cls.__mro__[:-1]):
        out.update(
            {n: v for n, v in vars(k).items() if not (n.startswith("__") and n.endswith("__"))}
        )
    if "__eq__" in vars(cls):
        out["__eq__"] = vars(cls)["__eq__"]
    return out


def _same_class_body(ta: type, tb: type, depth: int) -> bool:
    ca, cb = _class_attrs(ta), _class_attrs(tb)
    if ca.keys() != cb.keys():
        return False
    for name in ca:
        x, y = ca[name], cb[name]
        fx, fy = getattr(x, "__code__", None), getattr(y, "__code__", None)
        if fx is not None or fy is not None:
            if fx is None or fy is None:
                return False
            if _code_key(fx) != _code_key(fy):
                # Different method bodies: the objects may behave differently, or not.
                # Nothing about their values can say which, so claim neither.
                raise Incomparable(f"method {name} differs")
        elif not _same(x, y, depth + 1):
            return False
    return True


def _same(a: object, b: object, depth: int = 0) -> bool:
    """Equality the way a test author means it: floats within tolerance, NaN equals NaN.

    Functions, classes, modules and generators are not values: comparing them raises
    `Incomparable` rather than guessing. The reference and the patch are executed
    separately, so a class either one defines is two distinct classes; instances are
    compared by class name, class-level attributes, method bodies and instance attributes.
    A class that defines `__eq__` is trusted when both objects share the class; across the
    two programs its `__eq__` is asked first, and a "no" is not taken as proof (it may just
    be an `isinstance` check failing across namespaces).
    """
    if depth > 50:  # a cyclic structure: give up the structural walk, fall back to ==
        return _eq(a, b)
    if isinstance(a, _OPAQUE) or isinstance(b, _OPAQUE):
        raise Incomparable(f"{type(a).__name__} vs {type(b).__name__}")
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, int | float) and isinstance(b, int | float):
        if isinstance(a, float) or isinstance(b, float):
            if math.isnan(a) and math.isnan(b):
                return True
            return math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-9)
        return a == b
    if isinstance(a, list | tuple) and isinstance(b, list | tuple):
        return (
            type(a) is type(b)
            and len(a) == len(b)
            and all(_same(x, y, depth + 1) for x, y in zip(a, b, strict=True))
        )
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k], depth + 1) for k in a)
    ta, tb = type(a), type(b)
    user = ta.__module__ not in ("builtins",) and hasattr(a, "__dict__")
    if user and ta.__qualname__ == tb.__qualname__ and hasattr(b, "__dict__"):
        custom_eq = "__eq__" in vars(ta) or "__eq__" in vars(tb)
        if ta is tb:
            return _eq(a, b) if custom_eq else _same(vars(a), vars(b), depth + 1)
        if not _same_class_body(ta, tb, depth):
            return False
        if custom_eq:
            if _eq(a, b):
                return True
            raise Incomparable(f"{ta.__qualname__}.__eq__ said no across programs")
        return _same(vars(a), vars(b), depth + 1)
    if ta is not tb:
        return False
    return _eq(a, b)


def _eq(a: object, b: object) -> bool:
    try:
        return bool(a == b)
    except Exception:
        return False


def _short(v: object) -> str:
    try:
        s = repr(v)
    except Exception as e:  # a repr that raises is still an observable result
        s = f"<unrepresentable {type(e).__name__}>"
    return s if len(s) <= 200 else s[:197] + "..."


class _Namespaces:
    """One executed module namespace per distinct source, reused across its items."""

    def __init__(self, setup: str):
        self.setup = setup
        self._cache: dict[str, dict | BaseException] = {}

    def get(self, code: str) -> dict:
        hit = self._cache.get(code)
        if hit is None:
            ns: dict = {"__name__": "__candidate__"}
            try:
                exec(compile(code, "<candidate>", "exec"), ns)
                if self.setup:
                    exec(compile(self.setup, "<setup>", "exec"), ns)
                hit = ns
            except ItemTimeout:
                raise  # not a property of the code: do not cache it
            except BaseException as e:  # SystemExit and friends are candidate behaviour
                hit = e
            self._cache[code] = hit
        if isinstance(hit, BaseException):
            raise hit
        return hit


def _run_assert(nss: _Namespaces, item: dict) -> dict:
    try:
        ns = nss.get(item["code"])
        exec(compile(item["test"], "<test>", "exec"), ns)
    except AssertionError:
        return {"status": "fail"}
    except ItemTimeout:
        raise
    except BaseException as e:
        msg = str(e).splitlines()[0][:120] if str(e) else ""
        return {"status": "error", "detail": type(e).__name__ + (f": {msg}" if msg else "")}
    return {"status": "pass"}


def _call(ns: dict, fn: str, args_src: str) -> tuple[str, object]:
    try:
        # In the program's own namespace, so arguments may name what it defines
        # (`Pair(5, 24)`) or what the setup code built (`root`).
        args = eval("(" + args_src + ",)", ns)
        return "ok", ns[fn](*args)
    except ItemTimeout:
        raise
    except BaseException as e:
        return "raise", type(e).__name__


def _run_compare(nss: _Namespaces, ctx: dict, item: dict) -> dict:
    """Call the reference and the candidate on one input and say whether they agree."""
    ref_ns = nss.get(ctx["ref"])
    rs, rv = _call(ref_ns, ctx["fn"], item["input"])
    try:
        cand_ns = nss.get(item["code"])
    except ItemTimeout:
        raise
    except BaseException as e:
        return {"status": "differs", "ref": _show(rs, rv), "cand": f"load: {type(e).__name__}"}
    cs, cv = _call(cand_ns, ctx["fn"], item["input"])
    out = {"ref_status": rs}
    try:
        same = rs == cs and (rs == "raise" and rv == cv or rs == "ok" and _same(rv, cv))
    except Incomparable as e:
        out.update(status="incomparable", detail=str(e)[:200])
        return out
    if same:
        out["status"] = "same"
    else:
        out.update(status="differs", ref=_show(rs, rv), cand=_show(cs, cv))
    return out


def _show(status: str, value: object) -> str:
    return f"raises {value}" if status == "raise" else _short(value)


def _guarded(watchdog: _Watchdog, seconds: float, run, *args) -> dict:
    """Run one item under the watchdog; the result carries how long the whole item took."""
    t0 = time.perf_counter()
    try:
        watchdog.arm(seconds)
        try:
            res = run(*args)
        finally:
            watchdog.disarm()
    except ItemTimeout:
        watchdog.disarm()
        res = {"status": "timeout"}
    res["s"] = round(time.perf_counter() - t0, 6)
    return res


def run_job(job: dict, watchdog: _Watchdog) -> None:
    # Undo the one piece of interpreter state MBPP solutions commonly change, so an
    # earlier job's `sys.setrecursionlimit(10**6)` cannot turn this job's infinite
    # recursion into a hard crash instead of a RecursionError.
    sys.setrecursionlimit(RECURSION_LIMIT)
    ctx = job.get("ctx", {})
    stop_on = set(job.get("stop_on", ()))
    seconds = float(job.get("item_timeout", 4.0))
    nss = _Namespaces(ctx.get("setup", ""))
    # Folders to import from (a program's own directory, so `import helper` next to it
    # works); prepended for this job only.
    extra = [p for p in ctx.get("sys_path", ()) if p not in sys.path]
    sys.path[:0] = extra
    before = set(sys.modules)
    try:
        _run_items(job, ctx, watchdog, nss, stop_on, seconds)
    finally:
        # The worker is reused: forget the folders, and any module imported from them.
        for p in extra:
            if p in sys.path:
                sys.path.remove(p)
        for name in set(sys.modules) - before if extra else ():
            path = getattr(sys.modules[name], "__file__", None) or ""
            if any(path.startswith(os.path.join(p, "")) for p in extra):
                del sys.modules[name]
    _OUT.write(DONE + "\n")
    _OUT.flush()


def _run_items(
    job: dict, ctx: dict, watchdog: _Watchdog, nss: _Namespaces, stop_on: set, seconds: float
) -> None:
    stopped: set = set()
    # Candidate code prints and writes stderr; none of it may reach the protocol stream.
    sys.stdout = io.StringIO()
    sys.stderr = io.StringIO()
    for i in range(job["start"], len(job["items"])):
        item = job["items"][i]
        g = item.get("g")
        if g is not None and g in stopped:
            continue
        if job["mode"] == "asserts":
            res = _guarded(watchdog, seconds, _run_assert, nss, item)
        else:
            res = _guarded(watchdog, seconds, _run_compare, nss, ctx, item)
        res["i"] = i
        _emit(res)
        if g is not None and res["status"] in stop_on:
            stopped.add(g)
        # Keep the swallowed output from growing without bound across thousands of items.
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()


def main() -> None:
    """Serve jobs, one JSON object per line on stdin, until stdin closes."""
    jobs = sys.stdin
    # A candidate that calls input() must get EOF, not the next job.
    sys.stdin = io.StringIO("")
    watchdog = _Watchdog()
    for line in jobs:
        if line.strip():
            run_job(json.loads(line), watchdog)


if __name__ == "__main__":
    main()

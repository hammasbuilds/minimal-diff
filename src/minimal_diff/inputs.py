"""Generated inputs for differential testing: nearby values of the arguments the asserts use.

Adapted from `differential.py` in hammasbuilds/mbpp-false-accepts. Candidate inputs come
from the asserts themselves, because perturbing a literal that is already there - a
number off by one, a list with its last element dropped, a string emptied - stays inside
the type the function expects. Random values of a guessed type mostly produce TypeErrors
on both sides, which separate nothing.

`fuzz_inputs` goes further for the same reason: random, type-preserving mutations of
those same literals, several arguments at once. The two sets are kept in that order, so
a witness's position says whether the cheap perturbations would have found it alone.

Every input is later run on the reference and kept only if the reference returns a
value, deterministically. An input the reference itself rejects is outside the
function's domain and says nothing about a patch.
"""

from __future__ import annotations

import ast
import random
import string


def call_args(test: str, fn: str) -> list[ast.expr] | None:
    """The argument expressions of the call to `fn` in `assert fn(a, b) == c`.

    Matched by name, not by position: in `assert set(fn(x)) == {1}` the first call in
    the tree is `set(...)`, and perturbing its argument would feed `fn` garbage.
    Calls with keyword or starred arguments are skipped; they cannot be replayed as a
    plain positional argument list.
    """
    try:
        tree = ast.parse(test.strip())
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == fn
            and not node.keywords
            and not any(isinstance(a, ast.Starred) for a in node.args)
        ):
            return list(node.args)
    return None


def _const(v: object) -> ast.expr:
    return ast.Constant(v)


def perturb(node: ast.expr) -> list[ast.expr]:
    """Nearby values of the same type as `node`."""
    out: list[ast.expr] = []
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = node.operand
        if isinstance(inner, ast.Constant) and isinstance(inner.value, int | float):
            return perturb(_const(-inner.value))
    if isinstance(node, ast.Constant):
        v = node.value
        if isinstance(v, bool):
            out.append(_const(not v))
        elif isinstance(v, int):
            for w in (v + 1, v - 1, v + 2, v - 2, 0, 1, 2, 3, -1, -v, 2 * v, v // 2, 10, 100):
                out.append(_const(w))
        elif isinstance(v, float):
            for w in (v + 1.0, v - 1.0, v / 2, 0.0, 1.0, -v, 2.5):
                out.append(_const(w))
        elif isinstance(v, str):
            for w in (v + v[:1], v[:-1], v[1:], "", v.upper(), v.lower(), v[::-1], v * 2, v + " "):
                out.append(_const(w))
    elif isinstance(node, ast.List | ast.Tuple | ast.Set):
        elts = list(node.elts)
        cls = type(node)

        def make(items: list[ast.expr]) -> ast.expr:
            return cls(elts=items, ctx=ast.Load()) if cls is not ast.Set else cls(elts=items)

        if elts:
            out.append(make(elts[:-1]))
            out.append(make(elts[1:]))
            out.append(make(elts + [elts[-1]]))
            out.append(make(list(reversed(elts))))
            out.append(make(elts[:1]))
            for i, e in enumerate(elts[:4]):
                for p in perturb(e)[:3]:
                    new = list(elts)
                    new[i] = p
                    out.append(make(new))
        if cls is not ast.Set:  # `{}` is a dict, not an empty set
            out.append(make([]))
    return out


def _src(node: ast.expr) -> str | None:
    try:
        ast.fix_missing_locations(node)
        return ast.unparse(node)
    except (ValueError, AttributeError, RecursionError):
        return None


def generated_inputs(tests: tuple[str, ...] | list[str], fn: str, cap: int = 150) -> list[str]:
    """Argument lists to try, as source, starting with the asserts' own arguments.

    Round-robin over the asserts, so a cap does not spend every slot on the first one.
    """
    per_test: list[list[str]] = []
    for t in tests:
        args = call_args(t, fn)
        if args is None:
            continue
        variants: list[str] = []
        base = [_src(a) for a in args]
        if all(s is not None for s in base):
            variants.append(", ".join(base))  # type: ignore[arg-type]
        for i, a in enumerate(args):
            for p in perturb(a):
                new = list(args)
                new[i] = p
                parts = [_src(x) for x in new]
                if all(s is not None for s in parts):
                    variants.append(", ".join(parts))  # type: ignore[arg-type]
        per_test.append(variants)
    out: list[str] = []
    seen: set[str] = set()
    depth = 0
    while len(out) < cap and any(depth < len(v) for v in per_test):
        for v in per_test:
            if depth < len(v) and v[depth] not in seen:
                seen.add(v[depth])
                out.append(v[depth])
                if len(out) >= cap:
                    break
        depth += 1
    return out


# --------------------------------------------------------------------------- random fuzzing

_ALPHABET = string.ascii_lowercase + string.ascii_uppercase + string.digits + " _-.,"


def _mutate(v: object, rng: random.Random, depth: int = 0) -> object:
    """A random value of the same type as `v`, usually near it."""
    if isinstance(v, bool):
        return rng.random() < 0.5
    if isinstance(v, int):
        r = rng.random()
        if r < 0.4:
            return v + rng.randint(-3, 3)
        if r < 0.7:
            return rng.randint(0, 30) if v >= 0 else rng.randint(-30, 30)
        return rng.choice([0, 1, 2, -1, v * 2, v // 2, rng.randint(-1000, 1000)])
    if isinstance(v, float):
        return rng.choice([v * rng.uniform(0.5, 1.5), round(rng.uniform(-10, 100), 2), 0.0, -v])
    if isinstance(v, str):
        chars = list(v)
        pool = (v or "") + _ALPHABET
        for _ in range(rng.randint(1, 3)):
            op = rng.random()
            if op < 0.35 and chars:
                chars.pop(rng.randrange(len(chars)))
            elif op < 0.7:
                chars.insert(rng.randint(0, len(chars)), rng.choice(pool))
            elif chars:
                chars[rng.randrange(len(chars))] = rng.choice(pool)
        return "".join(chars)
    if isinstance(v, list | tuple | set) and depth < 3:
        items = list(v)
        r = rng.random()
        if r < 0.2 and items:
            items.pop(rng.randrange(len(items)))
        elif r < 0.4 and items:
            items.insert(rng.randint(0, len(items)), _mutate(rng.choice(items), rng, depth + 1))
        elif r < 0.5:
            rng.shuffle(items)
        items = [_mutate(x, rng, depth + 1) if rng.random() < 0.4 else x for x in items]
        if isinstance(v, set):
            try:
                return set(items)
            except TypeError:  # a mutated element became unhashable
                return set(v)
        return type(v)(items)
    if isinstance(v, dict) and depth < 3:
        out = {k: (_mutate(x, rng, depth + 1) if rng.random() < 0.5 else x) for k, x in v.items()}
        if out and rng.random() < 0.2:
            out.pop(rng.choice(list(out)))
        return out
    return v


def _literal_args(src: str) -> list | None:
    try:
        args = ast.literal_eval("(" + src + ",)")
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return None
    return list(args)


def fuzz_inputs(seeds: list[str], n: int = 350, key: str = "", cap_chars: int = 2000) -> list[str]:
    """`n` random argument lists, each a type-preserving mutation of a random seed.

    Wider than `generated_inputs`: values move further, several arguments change at
    once, list elements are inserted and reordered. Seeds that are not plain literals
    (a call to a class defined in the solution, say) cannot be mutated this way and are
    skipped. Seeded by `key`, so a rebuild produces the same inputs.
    """
    parsed = [a for s in seeds if (a := _literal_args(s)) is not None]
    if not parsed:
        return []
    rng = random.Random(f"fuzz/{key}")
    out: list[str] = []
    seen = set(seeds)
    for _ in range(n * 4):
        if len(out) >= n:
            break
        base = rng.choice(parsed)
        args = [_mutate(a, rng) if rng.random() < 0.7 else a for a in base]
        try:
            src = ", ".join(repr(a) for a in args)
        except (ValueError, RecursionError):
            continue
        if src not in seen and len(src) <= cap_chars:
            seen.add(src)
            out.append(src)
    return out

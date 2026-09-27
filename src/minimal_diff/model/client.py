"""Talk to Ollama, and never ask it the same question twice.

Every generation is cached on disk keyed by (model, system, prompt, options), so a run
that is interrupted resumes where it stopped, and re-scoring never re-generates. The
cache is the record of what the model said; the scores are derived from it.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

DEFAULT_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen2.5-coder:14b"
# Greedy and seeded: the arms differ by instruction, not by sampling luck. 4096 new
# tokens is several times the longest reference program; a reply that still hits the
# limit is recorded as truncated rather than scored as a broken repair.
DEFAULT_OPTIONS = {"temperature": 0.0, "seed": 0, "num_ctx": 8192, "num_predict": 4096}
RETRIES = 3


@dataclass(frozen=True)
class Reply:
    text: str
    done_reason: str = "stop"  # Ollama's: "stop", or "length" when num_predict ran out

    @property
    def truncated(self) -> bool:
        return self.done_reason == "length"


class Client(Protocol):
    model: str

    def generate(self, system: str, prompt: str) -> Reply: ...


def _opener(url: str) -> urllib.request.OpenerDirector:
    """No proxy for a local server. urllib honours HTTP_PROXY even for 127.0.0.1, and a
    corporate or dead proxy then swallows every request to the local Ollama."""
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost"):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


class ModelUnavailable(RuntimeError):
    pass


class _Transient(Exception):
    """A failure worth retrying: dropped connection, 5xx, a 200 without a message."""


@dataclass
class OllamaClient:
    model: str = DEFAULT_MODEL
    url: str = DEFAULT_URL
    options: dict = field(default_factory=lambda: dict(DEFAULT_OPTIONS))
    timeout: float = 600.0
    backoff: float = 5.0  # seconds before the first retry; doubles each time

    def generate(self, system: str, prompt: str) -> Reply:
        last: Exception | None = None
        for attempt in range(RETRIES):
            try:
                return self._once(system, prompt)
            except _Transient as e:
                last = e
                if attempt + 1 < RETRIES:
                    time.sleep(self.backoff * 2**attempt)
        raise ModelUnavailable(f"{self.model} at {self.url}: {last} (after {RETRIES} tries)")

    def _once(self, system: str, prompt: str) -> Reply:
        body = json.dumps(
            {
                "model": self.model,
                "stream": False,
                "options": self.options,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{self.url}/api/chat", data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with _opener(self.url).open(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            detail = f"HTTP {e.code}: {e.read()[:200]!r}"
            if e.code >= 500:
                raise _Transient(detail) from e
            raise ModelUnavailable(f"ollama refused {self.model}: {detail}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise _Transient(f"cannot reach ollama: {e}") from e
        try:
            d = json.loads(body)
            text = d["message"]["content"]
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            raise _Transient(f"reply without a message: {body[:200]!r}") from e
        return Reply(text, d.get("done_reason") or "stop")


@dataclass
class FakeClient:
    """A deterministic stand-in: `respond(system, prompt) -> text or Reply`. Counts calls."""

    respond: Callable[[str, str], str | Reply]
    model: str = "fake"
    calls: int = 0

    def generate(self, system: str, prompt: str) -> Reply:
        self.calls += 1
        r = self.respond(system, prompt)
        return r if isinstance(r, Reply) else Reply(r)


def cache_key(model: str, system: str, prompt: str, options: dict) -> str:
    blob = json.dumps(
        {"model": model, "system": system, "prompt": prompt, "options": options}, sort_keys=True
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass
class CachedClient:
    """Wraps a client; a cache hit never calls it."""

    inner: Client
    cache_dir: Path
    hits: int = 0
    misses: int = 0

    @property
    def model(self) -> str:
        return self.inner.model

    @property
    def options(self) -> dict:
        # Part of the key: a different temperature is a different question.
        return dict(getattr(self.inner, "options", {}))

    def _path(self, system: str, prompt: str) -> Path:
        key = cache_key(self.inner.model, system, prompt, self.options)
        safe = self.inner.model.replace(":", "_").replace("/", "_")
        return self.cache_dir / safe / key[:2] / f"{key}.json"

    def cached(self, system: str, prompt: str) -> bool:
        return self._path(system, prompt).exists()

    def generate(self, system: str, prompt: str) -> Reply:
        p = self._path(system, prompt)
        if p.exists():
            self.hits += 1
            d = json.loads(p.read_text(encoding="utf-8"))
            return Reply(d["response"], d.get("done_reason", "stop"))
        self.misses += 1
        reply = self.inner.generate(system, prompt)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "model": self.inner.model,
                    "options": self.options,
                    "system": system,
                    "prompt": prompt,
                    "response": reply.text,
                    "done_reason": reply.done_reason,
                }
            ),
            encoding="utf-8",
        )
        tmp.replace(p)  # atomic: a killed run never leaves half a cache entry
        return reply

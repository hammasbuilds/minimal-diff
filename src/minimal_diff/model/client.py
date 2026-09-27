"""Talk to Ollama, and never ask it the same question twice.

Every generation is cached on disk keyed by (model, system, prompt, options), so a run
that is interrupted resumes where it stopped, and re-scoring never re-generates. The
cache is the record of what the model said; the scores are derived from it.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

DEFAULT_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen2.5-coder:14b"
# Greedy and seeded: the arms differ by instruction, not by sampling luck.
DEFAULT_OPTIONS = {"temperature": 0.0, "seed": 0, "num_ctx": 4096, "num_predict": 1024}


class Client(Protocol):
    model: str

    def generate(self, system: str, prompt: str) -> str: ...


class ModelUnavailable(RuntimeError):
    pass


@dataclass
class OllamaClient:
    model: str = DEFAULT_MODEL
    url: str = DEFAULT_URL
    options: dict = field(default_factory=lambda: dict(DEFAULT_OPTIONS))
    timeout: float = 600.0

    def generate(self, system: str, prompt: str) -> str:
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
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))["message"]["content"]
        except urllib.error.HTTPError as e:
            raise ModelUnavailable(
                f"ollama returned HTTP {e.code} for {self.model}: {e.read()[:200]!r}"
            ) from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise ModelUnavailable(f"cannot reach ollama at {self.url}: {e}") from e


@dataclass
class FakeClient:
    """A deterministic stand-in: `respond(system, prompt) -> text`. Counts its calls."""

    respond: Callable[[str, str], str]
    model: str = "fake"
    calls: int = 0

    def generate(self, system: str, prompt: str) -> str:
        self.calls += 1
        return self.respond(system, prompt)


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

    def generate(self, system: str, prompt: str) -> str:
        p = self._path(system, prompt)
        if p.exists():
            self.hits += 1
            return json.loads(p.read_text(encoding="utf-8"))["response"]
        self.misses += 1
        text = self.inner.generate(system, prompt)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "model": self.inner.model,
                    "options": self.options,
                    "system": system,
                    "prompt": prompt,
                    "response": text,
                }
            ),
            encoding="utf-8",
        )
        tmp.replace(p)  # atomic: a killed run never leaves half a cache entry
        return text

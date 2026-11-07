"""Model backends for the slim fabric: the result type, the protocol, and a
local Ollama client.

Exported from AXIOM's ``axiom_event_token.backends``, cut down to what the
AxLab experiments use.  Hosted backends (NIM, DeepSeek and the rest) are not
included: the default run makes no network calls, and the only network
backend here talks to an Ollama server you run yourself.

Environment (OllamaBackend only):
  OLLAMA_URL      default "http://localhost:11434"
  OLLAMA_MODEL    default "llama3.2:3b"
  OLLAMA_NUM_GPU  optional; passed to Ollama as options.num_gpu
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass(frozen=True)
class BackendResult:
    """One generation's transport-level facts."""
    text:           str
    input_tokens:   int
    output_tokens:  int
    latency_ms:     int
    backend:        str
    model:          str
    prefill_ms:     int = 0   # Ollama prompt_eval_duration / 1e6; 0 = not reported
    # Fraction of draft tokens accepted under speculative decoding (0..1);
    # None when the backend did not run it or cannot report it.
    accepted_token_rate: Optional[float] = None

    @property
    def tokens_per_s(self) -> float:
        """output_tokens / latency; 0 if unmeasured."""
        if self.latency_ms <= 0 or self.output_tokens <= 0:
            return 0.0
        return round(self.output_tokens / (self.latency_ms / 1000.0), 2)


class BackendError(RuntimeError):
    """Generation failed (timeout, connection error, non-2xx, malformed)."""


class SLMBackend(Protocol):
    """Minimum contract every backend implements."""
    name: str
    model: str

    def generate(
        self,
        *,
        system: str,
        prompt: str,
        max_output_tokens: int,
        timeout_s: float = 60.0,
    ) -> BackendResult: ...


class OllamaBackend:
    """A local Ollama server over /api/chat.

    System and user go as separate roles so Ollama can reuse the cached
    system-prompt tokens across requests; ``keep_alive`` keeps the model (and
    its KV state) loaded between calls.  Ollama's ``prompt_eval_count`` and
    ``eval_count`` become the input / output token counts.
    """
    name: str = "local"

    def __init__(self, *, model: Optional[str] = None, url: Optional[str] = None) -> None:
        self.model = model or os.environ.get("OLLAMA_MODEL", "llama3.2:3b")
        self._url = (url or os.environ.get("OLLAMA_URL", "http://localhost:11434")).rstrip("/")

    def generate(
        self,
        *,
        system: str,
        prompt: str,
        max_output_tokens: int,
        timeout_s: float = 60.0,
    ) -> BackendResult:
        import requests

        options = {"temperature": 0.3, "num_predict": int(max_output_tokens)}
        num_gpu = os.environ.get("OLLAMA_NUM_GPU")
        if num_gpu not in (None, ""):
            options["num_gpu"] = int(num_gpu)
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "keep_alive": "10m",
            "options": options,
        }
        t0 = time.monotonic()
        try:
            resp = requests.post(f"{self._url}/api/chat", json=body, timeout=timeout_s)
        except requests.RequestException as e:
            raise BackendError(f"Ollama transport: {e}") from e
        latency_ms = int((time.monotonic() - t0) * 1000)
        if not resp.ok:
            raise BackendError(f"Ollama HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
        except ValueError as e:
            raise BackendError(f"Ollama malformed JSON: {e}") from e
        return BackendResult(
            text=data.get("message", {}).get("content", ""),
            input_tokens=int(data.get("prompt_eval_count", 0)),
            output_tokens=int(data.get("eval_count", 0)),
            latency_ms=latency_ms,
            backend=self.name,
            model=self.model,
            prefill_ms=int(data.get("prompt_eval_duration", 0) / 1_000_000),
        )


# Name used by the original AXIOM benchmark.
LocalNanoBackend = OllamaBackend

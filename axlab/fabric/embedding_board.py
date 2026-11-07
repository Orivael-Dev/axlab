"""EmbeddingBoard — shared semantic routing surface for the slim fabric.

Inference OS Layer 1 (Inference Router).  Each board row is one small
L2-normalised vector; an event is scored by a single matrix-vector product
over the board rather than a Python loop.  SlimAgentPool keeps a row per
agent KIND (template, or tagged spawn batch), not per agent.

Footprint: rows × dim × 4 bytes (float32, so scoring is one BLAS gemv with
no per-event upcast).  7,550 rows × 384 dims ≈ 11.6 MB.

Two embedders:

  hashed_embed      Deterministic feature-hashing of word unigrams +
                    bigrams.  No model, microsecond-fast, safe on the
                    request path.  Lexical, not semantic — texts that share
                    words land near each other, paraphrases do not.
  OllamaEmbedder    Real sentence embeddings from a local Ollama embedding
                    model (e.g. all-minilm, 384 dims).  Called when agents
                    are created and once per event, never per agent.

Unlike capsule._role_embedding (a SHA-256 of the whole role string, where
similar roles get unrelated vectors), both embedders put similar texts
near each other.
"""
from __future__ import annotations

import hashlib
import re
from typing import Callable, Optional

import numpy as np

DEFAULT_DIM = 384

_WORD_RE = re.compile(r"[a-z0-9_]+")

Embedder = Callable[[str], np.ndarray]


def tokenize(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _bucket(feature: str, dim: int) -> tuple[int, float]:
    h = int.from_bytes(hashlib.blake2b(feature.encode("utf-8"),
                                       digest_size=8).digest(), "big")
    return h % dim, (1.0 if (h >> 63) & 1 else -1.0)


def hashed_embed(text: str, dim: int = DEFAULT_DIM) -> np.ndarray:
    """Signed feature-hashing embedding (unigrams + bigrams), L2-normalised."""
    vec   = np.zeros(dim, dtype=np.float32)
    words = tokenize(text)
    feats = words + [f"{a} {b}" for a, b in zip(words, words[1:])]
    for f in feats:
        i, s = _bucket(f, dim)
        vec[i] += s
    n = float(np.linalg.norm(vec))
    return vec / n if n > 0 else vec


class OllamaEmbedder:
    """Sentence embeddings via Ollama's /api/embed (e.g. ``all-minilm``)."""

    def __init__(self, model: str = "all-minilm",
                 url: str = "http://localhost:11434",
                 timeout_s: float = 30.0) -> None:
        self.model     = model
        self._url      = url.rstrip("/")
        self._timeout  = timeout_s

    def __call__(self, text: str) -> np.ndarray:
        import requests
        r = requests.post(f"{self._url}/api/embed",
                          json={"model": self.model, "input": text},
                          timeout=self._timeout)
        r.raise_for_status()
        vec = np.asarray(r.json()["embeddings"][0], dtype=np.float32)
        n   = float(np.linalg.norm(vec))
        return vec / n if n > 0 else vec


class EmbeddingBoard:
    """Growable float32 matrix of routing vectors."""

    def __init__(self, capacity: int, dim: int = DEFAULT_DIM,
                 embedder: Optional[Embedder] = None) -> None:
        self.dim      = dim
        self.embedder = embedder or (lambda t: hashed_embed(t, dim))
        self._vecs    = np.zeros((capacity, dim), dtype=np.float32)

    @property
    def nbytes(self) -> int:
        return self._vecs.nbytes

    def grow(self, capacity: int) -> None:
        if capacity <= len(self._vecs):
            return
        new = np.zeros((capacity, self.dim), dtype=np.float32)
        new[: len(self._vecs)] = self._vecs
        self._vecs = new

    def embed(self, text: str) -> np.ndarray:
        vec = np.asarray(self.embedder(text), dtype=np.float32)
        if vec.shape != (self.dim,):
            raise ValueError(f"embedder returned shape {vec.shape}, "
                             f"board dim is {self.dim}")
        return vec

    def post(self, row: int, vec: np.ndarray) -> None:
        self._vecs[row] = vec

    def similarity(self, query: np.ndarray, n: int) -> np.ndarray:
        """Cosine similarity of ``query`` against the first ``n`` rows."""
        return self._vecs[:n] @ query.astype(np.float32)

"""SlimAgentPool — tens of thousands of agents as packed, signed records.

Inference OS Layer 1 (routing + wake) over Layer 2 state.

MiniSRDAgent costs ~1.5 KB per agent and AgentRouter.score scans every agent
in Python (measured 1.33 s/event at 75k).  The slim pool splits an agent
into two parts:

  AgentTemplate   Everything agents of one kind share — role, wake
                  conditions, skills, tool permissions, governance limits,
                  system prompt.  Stored and signed ONCE per kind.
  per-agent row   template index, board row, lifecycle state, wake counter,
                  last-active time, optional tags, and a 16-byte HMAC binding
                  the row to its template hash.  ~37 bytes of numpy columns
                  per agent.

EmbeddingBoard rows are per KIND, not per agent: one row per template, plus
one per tagged spawn() batch (its role + tags differ).  Untagged agents point
at their template's row, so the cosine pass covers templates + tagged
batches — at 75k agents / 50 templates / 7.5k tagged that is ~7.5k dot
products, not 75k (measured 55 ms/event with a row per agent).

Routing (route()) is vectorised: template keyword hits are computed per
template (few), broadcast to agents with one fancy-index, combined with tag
hits from an inverted index and an optional EmbeddingBoard cosine, then
top-k via argpartition.  No Python loop over agents.

Among equally-scored agents of the same template, the least-recently-active
one wins — agents of one kind behave like a worker pool.

Per-agent signatures are truncated HMAC-SHA256 (128 bits) — enough for
tamper detection of in-memory/at-rest rows; templates keep full-width
signatures like the rest of the fabric.
"""
from __future__ import annotations

import enum
import hashlib
import hmac
import json
import struct
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from axlab.signing import derive_key
from axlab.fabric.embedding_board import EmbeddingBoard, tokenize

TEMPLATE_KEY_NS = b"axiom-slim-agent-template-v1"
ROW_KEY_NS      = b"axiom-slim-agent-row-v1"
ROW_SIG_BYTES   = 16

_TEMPLATE_KEY: Optional[bytes] = None
_ROW_KEY:      Optional[bytes] = None


def _template_key() -> bytes:
    global _TEMPLATE_KEY
    if _TEMPLATE_KEY is None:
        _TEMPLATE_KEY = derive_key(TEMPLATE_KEY_NS)
    return _TEMPLATE_KEY


def _row_key() -> bytes:
    global _ROW_KEY
    if _ROW_KEY is None:
        _ROW_KEY = derive_key(ROW_KEY_NS)
    return _ROW_KEY


class AgentState(enum.IntEnum):
    DORMANT      = 0   # no work; eligible for wake
    READY        = 1   # queued for a model slot
    RUNNING      = 2   # holding a model slot right now
    WAITING_TOOL = 3   # suspended on a tool call — costs no model time
    ARCHIVED     = 4   # never routed again


# ─── AgentTemplate ───────────────────────────────────────────────────────────


@dataclass
class AgentTemplate:
    """Shared definition for every agent of one kind (signed once)."""
    name:              str
    role:              str
    wake_conditions:   list[str]
    system_prompt:     str
    skills:            list[str] = field(default_factory=list)
    tool_permissions:  list[str] = field(default_factory=list)
    governance_limits: list[str] = field(default_factory=list)
    signature:         str       = ""

    def _as_dict(self) -> dict:
        return {
            "name":              self.name,
            "role":              self.role,
            "wake_conditions":   self.wake_conditions,
            "system_prompt":     self.system_prompt,
            "skills":            self.skills,
            "tool_permissions":  self.tool_permissions,
            "governance_limits": self.governance_limits,
        }

    def _canonical(self) -> bytes:
        return json.dumps(self._as_dict(), sort_keys=True,
                          separators=(",", ":"), ensure_ascii=True).encode()

    @property
    def template_hash(self) -> bytes:
        return hashlib.sha256(self._canonical()).digest()

    def sign(self) -> "AgentTemplate":
        sig = hmac.new(_template_key(), self._canonical(),
                       hashlib.sha256).hexdigest()
        return AgentTemplate(**self._as_dict(), signature=sig)

    def verify(self) -> bool:
        if not self.signature:
            return False
        expected = hmac.new(_template_key(), self._canonical(),
                            hashlib.sha256).hexdigest()
        return hmac.compare_digest(self.signature, expected)


@dataclass(frozen=True)
class RoutedAgent:
    agent_idx:    int
    template:     str
    score:        float
    keyword_part: float
    tag_part:     float
    embed_part:   float


# ─── SlimAgentPool ───────────────────────────────────────────────────────────


class SlimAgentPool:
    """Packed column store of agents + vectorised router."""

    def __init__(
        self,
        capacity: int = 1024,
        *,
        board: Optional[EmbeddingBoard] = None,
        tag_weight: float = 0.5,
        embed_weight: float = 0.5,
    ) -> None:
        self._n            = 0
        self.tag_weight    = tag_weight
        self.embed_weight  = embed_weight
        self.board         = board

        self.templates:   list[AgentTemplate] = []
        self._tmpl_index: dict[str, int]      = {}
        self._tmpl_hash:  list[bytes]         = []

        self._state       = np.zeros(capacity, dtype=np.uint8)
        self._template    = np.zeros(capacity, dtype=np.uint32)
        self._board_row   = np.zeros(capacity, dtype=np.uint32)
        self._board_rows  = 0
        self._tmpl_row:   list[int] = []
        self._wakes       = np.zeros(capacity, dtype=np.uint32)
        self._last_active = np.zeros(capacity, dtype=np.float64)
        self._sig         = np.zeros((capacity, ROW_SIG_BYTES), dtype=np.uint8)

        # Sparse: only tagged agents pay for tags.
        self._tags:      dict[int, tuple[str, ...]] = {}
        self._tag_index: dict[str, list[int]]       = {}


    # ── Size / footprint ─────────────────────────────────────────────

    def __len__(self) -> int:
        return self._n

    @property
    def capacity(self) -> int:
        return len(self._state)

    def column_nbytes(self) -> int:
        """Bytes held by the per-agent numpy columns (excludes the board)."""
        return sum(a.nbytes for a in (self._state, self._template,
                                      self._board_row, self._wakes,
                                      self._last_active, self._sig))

    @property
    def board_rows(self) -> int:
        """EmbeddingBoard rows in use (templates + tagged spawn batches)."""
        return self._board_rows

    def _post_board(self, vec: np.ndarray) -> int:
        row = self._board_rows
        if row >= len(self.board._vecs):
            self.board.grow(max(row + 1, 2 * len(self.board._vecs), 64))
        self.board.post(row, vec)
        self._board_rows += 1
        return row

    def _grow(self, need: int) -> None:
        if need <= self.capacity:
            return
        cap = max(need, self.capacity * 2)

        def g(a: np.ndarray) -> np.ndarray:
            new = np.zeros((cap,) + a.shape[1:], dtype=a.dtype)
            new[: len(a)] = a
            return new

        self._state, self._template = g(self._state), g(self._template)
        self._board_row = g(self._board_row)
        self._wakes, self._last_active = g(self._wakes), g(self._last_active)
        self._sig = g(self._sig)

    # ── Templates ────────────────────────────────────────────────────

    def add_template(self, template: AgentTemplate) -> int:
        if not template.verify():
            raise ValueError(f"template {template.name!r} is unsigned or "
                             f"its signature does not verify")
        if template.name in self._tmpl_index:
            raise ValueError(f"template {template.name!r} already registered")
        idx = len(self.templates)
        self.templates.append(template)
        self._tmpl_index[template.name] = idx
        self._tmpl_hash.append(template.template_hash)
        self._tmpl_row.append(
            self._post_board(self.board.embed(template.role))
            if self.board is not None else 0)
        return idx

    def template_of(self, agent_idx: int) -> AgentTemplate:
        return self.templates[int(self._template[agent_idx])]

    # ── Agents ───────────────────────────────────────────────────────

    def spawn(self, template_name: str, count: int = 1,
              tags: Optional[list[str]] = None) -> list[int]:
        """Create ``count`` dormant agents of a template; returns their ids."""
        t_idx = self._tmpl_index[template_name]
        start = self._n
        self._grow(start + count)
        end   = start + count
        self._template[start:end] = t_idx
        self._state[start:end]    = AgentState.DORMANT
        tag_tuple = tuple(t.lower() for t in tags) if tags else ()
        row = self._tmpl_row[t_idx]
        if self.board is not None and tag_tuple:
            row = self._post_board(self.board.embed(
                self.templates[t_idx].role + " " + " ".join(tag_tuple)))
        self._board_row[start:end] = row
        for i in range(start, end):
            if tag_tuple:
                self._tags[i] = tag_tuple
                for t in tag_tuple:
                    self._tag_index.setdefault(t, []).append(i)
            self._sign_row(i)
        self._n = end
        return list(range(start, end))

    def state(self, agent_idx: int) -> AgentState:
        return AgentState(int(self._state[agent_idx]))

    def tags(self, agent_idx: int) -> tuple[str, ...]:
        return self._tags.get(agent_idx, ())

    def set_state(self, agent_idx: int, state: AgentState) -> None:
        self._state[agent_idx] = state
        if state == AgentState.RUNNING:
            self._wakes[agent_idx] += 1
            self._last_active[agent_idx] = time.time()
        self._sign_row(agent_idx)

    def ids_in_state(self, *states: AgentState) -> np.ndarray:
        """Agent ids currently in any of ``states``."""
        return np.flatnonzero(np.isin(self._state[: self._n],
                                      np.array([int(s) for s in states], dtype=np.uint8)))

    def count_by_state(self) -> dict[str, int]:
        counts = np.bincount(self._state[: self._n],
                             minlength=len(AgentState))
        return {s.name: int(counts[s]) for s in AgentState}

    # ── Row signatures ───────────────────────────────────────────────

    def _row_bytes(self, i: int) -> bytes:
        t = int(self._template[i])
        return (struct.pack(">IBI", i, int(self._state[i]), int(self._wakes[i]))
                + self._tmpl_hash[t]
                + "\x1f".join(self._tags.get(i, ())).encode("utf-8"))

    def _row_mac(self, i: int) -> bytes:
        return hmac.new(_row_key(), self._row_bytes(i),
                        hashlib.sha256).digest()[:ROW_SIG_BYTES]

    def _sign_row(self, i: int) -> None:
        self._sig[i] = np.frombuffer(self._row_mac(i), dtype=np.uint8)

    def verify_row(self, i: int) -> bool:
        return hmac.compare_digest(bytes(self._sig[i]), self._row_mac(i))

    # ── Routing ──────────────────────────────────────────────────────

    def route(
        self,
        event_text: str,
        k: int = 4,
        *,
        min_score: float = 0.25,
        query_vec: Optional[np.ndarray] = None,
    ) -> list[RoutedAgent]:
        """Pick up to ``k`` wakeable agents (DORMANT or READY) for an event.

        Score = template keyword fraction
              + tag_weight   × (agent tag hits / agent tag count)
              + embed_weight × cosine(event, agent vector)   [if board]
        Ties go to the least-recently-active agent.
        """
        n = self._n
        if n == 0 or k <= 0:
            return []
        text  = event_text.lower()
        words = set(tokenize(event_text))

        tmpl_kw = np.array([
            sum(1 for kw in t.wake_conditions if kw.lower() in text)
            / max(1, len(t.wake_conditions))
            for t in self.templates
        ], dtype=np.float32)
        kw_part = tmpl_kw[self._template[:n]]

        tag_part = np.zeros(n, dtype=np.float32)
        hit_agents: dict[int, int] = {}
        for w in words:
            for i in self._tag_index.get(w, ()):
                hit_agents[i] = hit_agents.get(i, 0) + 1
        for i, hits in hit_agents.items():
            tag_part[i] = self.tag_weight * hits / len(self._tags[i])

        if self.board is not None:
            q = query_vec if query_vec is not None else self.board.embed(event_text)
            sim = np.clip(self.board.similarity(q, self._board_rows), 0.0, None)
            embed_part = self.embed_weight * sim[self._board_row[:n]]
        else:
            embed_part = np.zeros(n, dtype=np.float32)

        score = kw_part + tag_part + embed_part
        eligible = (self._state[:n] <= AgentState.READY) & (score >= min_score)
        cand = np.flatnonzero(eligible)
        if cand.size == 0:
            return []

        # Primary: score desc.  Secondary: least recently active first.
        # Round scores so float noise doesn't defeat the LRU tie-break.
        # Keep everything tied with the k-th best so LRU is applied across
        # the whole tie group, not an arbitrary slice of it.
        key = np.round(score[cand], 4)
        if cand.size > k:
            kth  = np.partition(key, cand.size - k)[cand.size - k]
            keep = key >= kth
            cand, key = cand[keep], key[keep]
        order = np.lexsort((self._last_active[cand], -key))[:k]
        return [
            RoutedAgent(
                agent_idx    = int(cand[j]),
                template     = self.templates[int(self._template[cand[j]])].name,
                score        = float(score[cand[j]]),
                keyword_part = float(kw_part[cand[j]]),
                tag_part     = float(tag_part[cand[j]]),
                embed_part   = float(embed_part[cand[j]]),
            )
            for j in order
        ]

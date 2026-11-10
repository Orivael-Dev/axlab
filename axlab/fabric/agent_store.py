"""ContinuationStore + AgentMessage — suspended agent state and inboxes.

Inference OS Layer 2 (Memory).

An agent that is not holding a model slot is just a continuation:

  AgentContext   recent transcript text (bounded), the pending tool call it
                 is suspended on (if any), and a turn counter.  Never a KV
                 cache — a resumed agent re-sends text and relies on the
                 engine's prefix cache for the shared system prompt.

Memory policy:
  - Each hot context keeps at most ``hot_bytes_per_agent`` of recent text;
    the older prefix is appended to the SQLite ``history`` table.
  - The hot set as a whole is bounded by ``hot_budget_bytes``; when over,
    least-recently-used contexts are parked to SQLite and reloaded on
    next access.
  - ``drop()`` releases an agent that can never run again (archived by a
    fleet L4): hot context, parked copy and undelivered inbox go; the
    ``history`` audit rows stay on disk.

Agents talk by text, not hidden states: AgentMessage is HMAC-signed so the
Governance Guard and audit trail can inspect and verify every hop.  Inboxes
live in SQLite so a flood of messages to dormant agents costs disk, not RAM.

Single-threaded by design: all calls come from the asyncio event loop
thread (SlimFabricRuntime only offloads model generation to worker threads),
so one sqlite3 connection is safe.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional

from axlab.signing import derive_key

MESSAGE_KEY_NS = b"axiom-slim-agent-msg-v1"
EXTERNAL       = -1   # sender id for events that come from outside the fabric

_MESSAGE_KEY: Optional[bytes] = None

# Rough per-context Python object overhead added to text length when
# accounting the hot budget (str header, dataclass, OrderedDict slot).
_CTX_OVERHEAD = 256


def _message_key() -> bytes:
    global _MESSAGE_KEY
    if _MESSAGE_KEY is None:
        _MESSAGE_KEY = derive_key(MESSAGE_KEY_NS)
    return _MESSAGE_KEY


# ─── AgentMessage ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AgentMessage:
    sender:    int
    recipient: int
    body:      str
    ts:        float
    signature: str = ""

    def _canonical(self) -> bytes:
        return json.dumps(
            {"sender": self.sender, "recipient": self.recipient,
             "body": self.body, "ts": self.ts},
            sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ).encode()

    @classmethod
    def signed(cls, sender: int, recipient: int, body: str,
               ts: Optional[float] = None) -> "AgentMessage":
        m = cls(sender, recipient, body, time.time() if ts is None else ts)
        sig = hmac.new(_message_key(), m._canonical(), hashlib.sha256).hexdigest()
        return cls(sender, recipient, body, m.ts, sig)

    def verify(self) -> bool:
        if not self.signature:
            return False
        expected = hmac.new(_message_key(), self._canonical(),
                            hashlib.sha256).hexdigest()
        return hmac.compare_digest(self.signature, expected)


# ─── AgentContext ────────────────────────────────────────────────────────────


@dataclass
class PendingTool:
    call_id: str
    tool:    str
    args:    dict


@dataclass
class AgentContext:
    text:    str                   = ""
    pending: Optional[PendingTool] = None
    turns:   int                   = 0

    def to_json(self) -> str:
        return json.dumps({
            "text":    self.text,
            "pending": None if self.pending is None else {
                "call_id": self.pending.call_id,
                "tool":    self.pending.tool,
                "args":    self.pending.args,
            },
            "turns":   self.turns,
        })

    @classmethod
    def from_json(cls, s: str) -> "AgentContext":
        d = json.loads(s)
        p = d.get("pending")
        return cls(text=d["text"],
                   pending=None if p is None else PendingTool(**p),
                   turns=d.get("turns", 0))


# ─── ContinuationStore ───────────────────────────────────────────────────────


class ContinuationStore:
    def __init__(
        self,
        db_path: str = ":memory:",
        *,
        hot_budget_bytes: int = 2 * 1024 ** 3,
        hot_bytes_per_agent: int = 16 * 1024,
    ) -> None:
        self.hot_budget_bytes    = hot_budget_bytes
        self.hot_bytes_per_agent = hot_bytes_per_agent
        self._hot: "OrderedDict[int, AgentContext]" = OrderedDict()
        self._sizes: dict[int, int] = {}   # bytes charged per hot agent
        self._hot_bytes = 0
        self.parks = 0
        self.rejected_messages = 0

        self._db = sqlite3.connect(db_path)
        self._db.executescript("""
            PRAGMA journal_mode = WAL;
            PRAGMA synchronous  = NORMAL;
            CREATE TABLE IF NOT EXISTS parked  (agent INTEGER PRIMARY KEY,
                                                ctx TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS history (agent INTEGER NOT NULL,
                                                ts REAL NOT NULL,
                                                text TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS history_agent ON history(agent);
            CREATE TABLE IF NOT EXISTS inbox   (id INTEGER PRIMARY KEY,
                                                recipient INTEGER NOT NULL,
                                                msg TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS inbox_recipient ON inbox(recipient);
        """)

    # ── Accounting ───────────────────────────────────────────────────

    @property
    def hot_bytes(self) -> int:
        return self._hot_bytes

    @property
    def hot_count(self) -> int:
        return len(self._hot)

    def parked_count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM parked").fetchone()[0]

    @staticmethod
    def _size(ctx: AgentContext) -> int:
        return len(ctx.text) + _CTX_OVERHEAD

    # ── Contexts ─────────────────────────────────────────────────────

    def get(self, agent: int) -> AgentContext:
        """Hot context for ``agent`` (unparked if needed, new if unknown)."""
        ctx = self._hot.get(agent)
        if ctx is not None:
            self._hot.move_to_end(agent)
            return ctx
        row = self._db.execute("SELECT ctx FROM parked WHERE agent = ?",
                               (agent,)).fetchone()
        if row is not None:
            self._db.execute("DELETE FROM parked WHERE agent = ?", (agent,))
            ctx = AgentContext.from_json(row[0])
        else:
            ctx = AgentContext()
        self._charge(agent, ctx)
        return ctx

    def put(self, agent: int, ctx: AgentContext) -> None:
        """Store ``ctx`` as the hot context, trimming old text to history."""
        if len(ctx.text) > self.hot_bytes_per_agent:
            cut = len(ctx.text) - self.hot_bytes_per_agent
            self._db.execute("INSERT INTO history VALUES (?, ?, ?)",
                             (agent, time.time(), ctx.text[:cut]))
            ctx.text = ctx.text[cut:]
        self._charge(agent, ctx)

    def append(self, agent: int, text: str) -> AgentContext:
        ctx = self.get(agent)
        ctx.text += text
        self.put(agent, ctx)
        return ctx

    def history(self, agent: int) -> str:
        """Full cold history (older than the hot window), oldest first."""
        rows = self._db.execute(
            "SELECT text FROM history WHERE agent = ? ORDER BY rowid",
            (agent,)).fetchall()
        return "".join(r[0] for r in rows)

    def _charge(self, agent: int, ctx: AgentContext) -> None:
        # Recompute from the stored charge, not the (possibly mutated) old
        # object — callers edit the context returned by get() in place.
        self._hot_bytes -= self._sizes.get(agent, 0)
        size = self._size(ctx)
        self._hot[agent]    = ctx
        self._hot.move_to_end(agent)
        self._sizes[agent]  = size
        self._hot_bytes    += size
        self._evict(protect=agent)

    def _evict(self, protect: int) -> None:
        while self._hot_bytes > self.hot_budget_bytes and len(self._hot) > 1:
            agent, ctx = next(iter(self._hot.items()))
            if agent == protect:
                self._hot.move_to_end(agent)
                continue
            self._hot.popitem(last=False)
            self._hot_bytes -= self._sizes.pop(agent)
            self._db.execute("INSERT OR REPLACE INTO parked VALUES (?, ?)",
                             (agent, ctx.to_json()))
            self.parks += 1

    def drop(self, agents) -> tuple[int, int]:
        """Release every trace of ``agents`` that only a live agent needs.

        Removes the hot context (and its charge against the budget), any
        parked copy, and undelivered inbox messages.  ``history`` — the
        trimmed transcript audit trail — is kept.  Returns
        ``(hot_bytes_freed, inbox_messages_dropped)``.  Unknown agents are
        a no-op, so calling it twice is harmless.
        """
        ids = [int(a) for a in agents]
        freed = 0
        for a in ids:
            if self._hot.pop(a, None) is not None:
                size = self._sizes.pop(a, 0)
                self._hot_bytes -= size
                freed += size
        rows = [(a,) for a in ids]
        self._db.executemany("DELETE FROM parked WHERE agent = ?", rows)
        before = self._db.total_changes
        self._db.executemany("DELETE FROM inbox WHERE recipient = ?", rows)
        return freed, self._db.total_changes - before

    # ── Inboxes ──────────────────────────────────────────────────────

    def post(self, msg: AgentMessage) -> None:
        if not msg.verify():
            raise ValueError("refusing unsigned or tampered AgentMessage")
        self._db.execute(
            "INSERT INTO inbox (recipient, msg) VALUES (?, ?)",
            (msg.recipient, json.dumps(msg.__dict__)))

    def has_inbox(self, agent: int) -> bool:
        return self._db.execute(
            "SELECT 1 FROM inbox WHERE recipient = ? LIMIT 1",
            (agent,)).fetchone() is not None

    def take_inbox(self, agent: int) -> list[AgentMessage]:
        """Pop and return every verified message for ``agent``, oldest first.

        Messages whose signature no longer verifies (tampered at rest) are
        dropped and counted in ``rejected_messages``.
        """
        rows = self._db.execute(
            "SELECT id, msg FROM inbox WHERE recipient = ? ORDER BY id",
            (agent,)).fetchall()
        if not rows:
            return []
        self._db.execute("DELETE FROM inbox WHERE recipient = ?", (agent,))
        out = []
        for _, raw in rows:
            m = AgentMessage(**json.loads(raw))
            if m.verify():
                out.append(m)
            else:
                self.rejected_messages += 1
        return out

    def close(self) -> None:
        self._db.commit()
        self._db.close()

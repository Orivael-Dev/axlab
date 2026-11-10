"""Runtime boundaries for the slim fabric — what one agent's failure may cost.

Inference OS Layer 4 (Governance Guard), enforced inside the Layer 1–2 slim
fabric runtime.  Opt-in: ``SlimFabricRuntime(..., boundaries=Boundaries(...))``.
With ``boundaries=None`` (the default) the runtime behaves exactly as before.

The kill switch (L1–L4) stops a whole fleet.  These boundaries stop ONE agent
at the edge of what it was given, so a failing, confused or hostile agent
cannot spend, act or spread past its assignment while the rest keep working.

  Capability                      Where
  ------------------------------  ---------------------------------------------
  Deterministic policy            ``limits`` (numeric ceilings per tool
  enforcement                     argument, fail-closed) + ``policy`` (a pure
                                  function).  Checked on every TOOL line,
                                  before the tool runs.  No model in the loop.
  Permission boundaries           template ``tool_permissions`` (unchanged),
                                  ``revoke()`` at runtime, quarantine.
  Execution limits                ``agent_token_budget`` / ``fleet_token_budget``
                                  (reserved BEFORE a model call, so a call
                                  that could overrun never starts),
                                  ``tool_retry_ceiling``, ``denial_ceiling``.
  Human oversight                 ``approver``: an over-limit call is held for
                                  it; with no approver it is refused.
  Fleet-wide intervention         the existing FleetSwitch (L1–L4).
  Accountability                  ``BoundaryLedger``: one HMAC-chained receipt
                                  per decision, bound to the policy digest.
  Failure containment             ``max_sends_per_agent`` / ``max_inbound_per_agent``
                                  and quarantine: a quarantined agent sends
                                  nothing and is sent nothing.

THE BUDGET BOUND.  A model call is charged after it returns, but reserved
before it starts, at an upper bound: the UTF-8 byte length of system + prompt
divided by ``bytes_per_token_floor`` (1.0 is sound for byte-level BPE: a token
covers at least one byte), plus ``prompt_overhead_tokens`` for the chat
template, plus ``max_output_tokens``.  If the reservation does not fit the
agent's remaining budget (or the fleet's), the call does not happen.  So an
overrun is impossible unless the backend reports more input tokens than the
bound allows — and that case is counted (``budget_breaches``), never hidden.

CANNOT_MUTATE: ``Boundaries`` is frozen; agents only produce text, and no
directive reads or writes it.  ``revoke()`` and quarantine only ever narrow
what an agent may do; nothing here widens a permission at runtime.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Mapping, Optional, Union

from axlab.signing import derive_key

RECEIPT_KEY_NS = b"axiom-fabric-boundary-receipt-v1"
_KEY: Optional[bytes] = None


def _key() -> bytes:
    global _KEY
    if _KEY is None:
        _KEY = derive_key(RECEIPT_KEY_NS)
    return _KEY


def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()


ALLOW, DENY, HOLD = "allow", "deny", "hold"

PolicyFn = Callable[[int, str, dict], Optional[str]]          # -> refusal reason or None
ApproverFn = Callable[[int, str, dict, str], Union[bool, Awaitable[bool]]]


@dataclass(frozen=True)
class Boundaries:
    """What any one agent may do and spend.  0 means "no limit" for every
    count and budget.  ``limits`` maps a tool to {argument: max value}."""

    limits: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    policy: Optional[PolicyFn] = None
    approver: Optional[ApproverFn] = None
    approver_timeout_s: float = 300.0

    tool_retry_ceiling: int = 3        # consecutive failures of one tool, one agent
    denial_ceiling: int = 5            # refusals before the agent is quarantined
    agent_token_budget: int = 0        # lifetime tokens per agent (in + out)
    fleet_token_budget: int = 0        # lifetime tokens across the runtime
    max_sends_per_agent: int = 0       # lifetime SENDs an agent may make
    max_inbound_per_agent: int = 0     # lifetime agent-to-agent messages it may receive

    bytes_per_token_floor: float = 1.0
    prompt_overhead_tokens: int = 64

    def digest(self) -> str:
        """Identifies this policy in every receipt.  Callables are named, not
        hashed: swap the function and the name should change with it."""
        doc = {k: getattr(self, k) for k in (
            "limits", "approver_timeout_s", "tool_retry_ceiling", "denial_ceiling",
            "agent_token_budget", "fleet_token_budget", "max_sends_per_agent",
            "max_inbound_per_agent", "bytes_per_token_floor", "prompt_overhead_tokens")}
        doc["limits"] = {t: dict(v) for t, v in self.limits.items()}
        doc["policy"] = getattr(self.policy, "__qualname__", None)
        doc["approver"] = getattr(self.approver, "__qualname__", None)
        return hashlib.sha256(_canonical(doc)).hexdigest()[:16]

    def check_limits(self, tool: str, args: dict) -> Optional[str]:
        """Over-limit reason, or None.  Fail-closed: a limited argument that is
        missing or not a number is treated as over the limit."""
        for arg, ceiling in (self.limits.get(tool) or {}).items():
            raw = args.get(arg)
            try:
                v = float(str(raw).lstrip("$").replace(",", ""))
            except (TypeError, ValueError):
                return f"{arg} is not a number (limit {ceiling:g})"
            if not math.isfinite(v) or v > ceiling:
                return f"{arg}={raw} is over the limit {ceiling:g}"
        return None


class BoundaryLedger:
    """Append-only, HMAC-chained receipts: one per tool decision, tool result,
    quarantine, refused message and refused model call."""

    def __init__(self, policy_digest: str, path: Optional[Union[str, Path]] = None) -> None:
        self.policy = policy_digest
        self.receipts: list[dict] = []
        self._prev = "0" * 64
        self._fh = open(path, "a", encoding="utf-8") if path else None

    def add(self, kind: str, agent: int, **fields) -> dict:
        r = {"seq": len(self.receipts) + 1, "at": round(time.time(), 6), "kind": kind,
             "agent": int(agent), "policy": self.policy, "prev": self._prev, **fields}
        r["mac"] = hmac.new(_key(), _canonical(r), hashlib.sha256).hexdigest()
        self._prev = r["mac"]
        self.receipts.append(r)
        if self._fh:
            self._fh.write(json.dumps(r, sort_keys=True) + "\n")
            self._fh.flush()
        return r

    def verify(self) -> bool:
        prev = "0" * 64
        for i, r in enumerate(self.receipts, 1):
            body = {k: v for k, v in r.items() if k != "mac"}
            if r.get("seq") != i or r.get("prev") != prev:
                return False
            mac = hmac.new(_key(), _canonical(body), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(mac, r.get("mac", "")):
                return False
            prev = r["mac"]
        return True

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None


def args_hash(args: dict) -> str:
    return hashlib.sha256(_canonical(args)).hexdigest()[:16]


class BoundaryState:
    """Per-runtime counters the boundaries are enforced against."""

    def __init__(self, bounds: Boundaries, ledger_path=None) -> None:
        self.b = bounds
        self.ledger = BoundaryLedger(bounds.digest(), ledger_path)
        self.spent: dict[int, int] = {}
        self.reserved: dict[int, int] = {}
        self.fleet_spent = 0
        self.fleet_reserved = 0
        # Highest spent + reservation each agent reached: what a budget must
        # cover for that agent's work to fit (the reservation is the bound,
        # not the real cost, so this is above what it actually spends).
        self.peak_commit: dict[int, int] = {}
        self.fail_streak: dict[tuple[int, str], int] = {}
        self.denials: dict[int, int] = {}
        self.sends: dict[int, int] = {}
        self.inbound: dict[int, int] = {}
        self.quarantined: dict[int, str] = {}
        self.revoked_tools: set[str] = set()
        self.revoked_template: set[tuple[str, str]] = set()
        self.revoked_agent: set[tuple[int, str]] = set()

    # ── budgets ──────────────────────────────────────────────────────

    def bound(self, system: str, prompt: str, max_output_tokens: int) -> int:
        nbytes = len(system.encode("utf-8")) + len(prompt.encode("utf-8"))
        return (math.ceil(nbytes / max(self.b.bytes_per_token_floor, 1e-9))
                + self.b.prompt_overhead_tokens + int(max_output_tokens))

    def reserve(self, agent: int, amount: int) -> Optional[str]:
        """Reserve ``amount`` for one call, or return why not."""
        b = self.b
        if b.agent_token_budget and self.spent.get(agent, 0) + amount > b.agent_token_budget:
            return (f"agent budget: this call could cost up to {amount} tokens, "
                    f"{b.agent_token_budget - self.spent.get(agent, 0)} left")
        if b.fleet_token_budget and (self.fleet_spent + self.fleet_reserved + amount
                                     > b.fleet_token_budget):
            return "fleet budget exhausted"
        self.reserved[agent] = amount
        self.fleet_reserved += amount
        commit = self.spent.get(agent, 0) + amount
        if commit > self.peak_commit.get(agent, 0):
            self.peak_commit[agent] = commit
        return None

    def settle(self, agent: int, actual: int) -> bool:
        """Charge the real cost; False if it exceeded the reservation."""
        amount = self.reserved.pop(agent, 0)
        self.fleet_reserved -= amount
        self.spent[agent] = self.spent.get(agent, 0) + actual
        self.fleet_spent += actual
        return actual <= amount

    def release(self, agent: int) -> None:
        self.fleet_reserved -= self.reserved.pop(agent, 0)

    # ── tools ────────────────────────────────────────────────────────

    def revoked(self, agent: int, template: str, tool: str) -> bool:
        return (tool in self.revoked_tools or (template, tool) in self.revoked_template
                or (agent, tool) in self.revoked_agent)

    def decide(self, agent: int, template: str, tool: str, args: dict) -> tuple[str, str]:
        """(verdict, reason) for a permitted, known tool.  Deterministic."""
        if agent in self.quarantined:
            return DENY, "agent is quarantined: " + self.quarantined[agent]
        if self.revoked(agent, template, tool):
            return DENY, "permission revoked"
        ceiling = self.b.tool_retry_ceiling
        if ceiling and self.fail_streak.get((agent, tool), 0) >= ceiling:
            return DENY, f"retry ceiling: {tool} failed {ceiling} times in a row"
        if self.b.policy is not None:
            why = self.b.policy(agent, tool, args)
            if why:
                return DENY, "policy: " + why
        over = self.b.check_limits(tool, args)
        if over:
            return (HOLD if self.b.approver is not None else DENY), over
        return ALLOW, "within limits"

    def tool_result(self, agent: int, tool: str, ok: bool) -> None:
        k = (agent, tool)
        if ok:
            self.fail_streak.pop(k, None)
        else:
            self.fail_streak[k] = self.fail_streak.get(k, 0) + 1

    def denied(self, agent: int) -> bool:
        """Count a refusal; True if the agent has now hit the denial ceiling."""
        self.denials[agent] = self.denials.get(agent, 0) + 1
        return bool(self.b.denial_ceiling) and self.denials[agent] >= self.b.denial_ceiling

    # ── messages ─────────────────────────────────────────────────────

    def send_refusal(self, sender: int, recipient: int) -> Optional[str]:
        if sender in self.quarantined:
            return "sender is quarantined"
        if recipient in self.quarantined:
            return "recipient is quarantined"
        if self.b.max_sends_per_agent and self.sends.get(sender, 0) >= self.b.max_sends_per_agent:
            return f"send limit ({self.b.max_sends_per_agent}) reached"
        if (self.b.max_inbound_per_agent
                and self.inbound.get(recipient, 0) >= self.b.max_inbound_per_agent):
            return "recipient's inbound limit reached"
        self.sends[sender] = self.sends.get(sender, 0) + 1
        self.inbound[recipient] = self.inbound.get(recipient, 0) + 1
        return None

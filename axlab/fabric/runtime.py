"""SlimFabricRuntime — many live agents, a few model slots, one shared model.

Inference OS Layers 1–2, with a Layer 4 tool-permission gate.

  events ──► SlimAgentPool.route ──► signed AgentMessage into inbox ──► READY
                                                                         │
  model_slots workers ◄──────────── asyncio.Queue of READY agents ◄──────┘
        │  backend.generate(system=<template prompt>, prompt=<context+inbox>)
        ▼
  directives in the reply:
     TOOL <name> <json args>  → WAITING_TOOL; tool runs as an asyncio task,
                                the agent costs no model time until it
                                returns, then goes back to READY
     SEND <agent_id> <text>   → signed message to another agent's inbox,
                                recipient woken
     DONE                     → DORMANT
  anything else is kept as the agent's transcript.

Every agent of a template gets a byte-identical system prompt (template
prompt + the directive protocol below), and per-agent identity goes in the
user prompt, so engines with prefix caching (llama.cpp / Ollama slots,
vLLM prefix caching) compute the shared prefix once.

Throughput is bounded by ``model_slots`` and the backend, not by how many
agents exist: tens of thousands can be alive, a handful generate at once.
For Ollama, set OLLAMA_NUM_PARALLEL ≥ model_slots or the extra slots just
queue inside Ollama.

Tool calls are refused unless the tool is in the agent template's
tool_permissions — the refusal is written into the transcript, not raised.

Fleet kill switch (axlab.governance.fleet_level, scopes ``fleet`` + ``fabric:<scope>``),
checked before every model step and by a watcher every ``poll_s``:

  L2 THROTTLE   one model slot; the others wait
  L3 SUSPEND    no new steps; tools in flight finish, READY agents are held
                and run_until_idle returns — the next call picks them up once
                the level is lowered
  L4 TERMINATE  tools in flight are cancelled, queued/waiting agents ARCHIVED,
                a reply that lands after the stop is discarded, and submit()
                refuses new events.  Archived agents' hot transcripts, parked
                copies and undelivered inboxes are freed (store.drop); their
                cold history rows stay as the audit trail

Per-agent boundaries (axlab.fabric.boundaries, opt-in with
``boundaries=Boundaries(...)``): deterministic argument limits and policy on
every TOOL line, approval holds, runtime revocation, a retry ceiling per
failing tool, token budgets reserved before each model call, message caps,
quarantine, and an HMAC-chained receipt per decision.  ``boundaries=None``
(the default) leaves every behaviour above unchanged.

On by default: with ``switch=None`` the runtime reads NXL_GOVERNANCE_FILE,
else ./.nodexloop/governance.json (a missing file is NORMAL).  The check is
~0.6 us between 50 ms polls.  ``switch=False`` opts out entirely.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional, Union

from axlab.governance.fleet_level import LEVELS, SUSPEND, TERMINATE, THROTTLE, FleetSwitch, default_path
from axlab.fabric.agent_store import (
    EXTERNAL,
    AgentMessage,
    ContinuationStore,
    PendingTool,
)
from axlab.fabric.boundaries import (
    ALLOW,
    DENY,
    HOLD,
    Boundaries,
    BoundaryState,
    args_hash,
)
from axlab.fabric.slim_pool import AgentState, RoutedAgent, SlimAgentPool

ToolFn = Callable[[dict], Union[str, Awaitable[str]]]

DIRECTIVE_PROTOCOL = """
You can act by writing directive lines, one per line:
TOOL <tool_name> <json arguments>   call a tool; you will see its result later
SEND <agent_id> <message>           message another agent
DONE                                you have finished this task
Only the first TOOL line in a reply is executed. Keep replies short.
""".strip()


@dataclass
class RuntimeStats:
    steps:          int = 0
    tool_calls:     int = 0
    tool_refusals:  int = 0
    messages:       int = 0
    errors:         int = 0
    turn_limited:   int = 0
    input_tokens:   int = 0
    output_tokens:  int = 0
    model_ms:       int = 0
    # Fleet kill switch: the level that stopped the last run_until_idle ("" if
    # none), agents archived by an L4, events refused by an L4.
    halted:         str = ""
    archived:       int = 0
    refused_events: int = 0
    # Released by archiving: hot transcript bytes and undelivered messages.
    archived_bytes_freed:    int = 0
    archived_msgs_dropped:   int = 0
    # Boundaries (all 0 when boundaries=None).
    policy_denials:   int = 0     # refused by limits / policy / revocation / retry ceiling
    retry_blocked:    int = 0     # of those: refused by the retry ceiling
    holds:            int = 0     # over-limit calls sent to the approver
    quarantined:      int = 0
    budget_refusals:  int = 0     # model calls that never started: no budget left
    budget_breaches:  int = 0     # backend reported more than the reserved bound
    sends_refused:    int = 0
    # Wake → DORMANT wall time per task (includes queueing and tool waits).
    task_ms:        list[float] = field(default_factory=list)
    last_errors:    list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Directives:
    text:  str
    tool:  Optional[tuple[str, dict]]
    sends: list[tuple[int, str]]
    done:  bool


def parse_directives(reply: str) -> Directives:
    kept: list[str] = []
    tool: Optional[tuple[str, dict]] = None
    sends: list[tuple[int, str]] = []
    done = False
    for line in reply.splitlines():
        s = line.strip()
        if s.startswith("TOOL ") and tool is None:
            parts = s[5:].strip().split(None, 1)
            if parts:
                raw = parts[1] if len(parts) > 1 else "{}"
                try:
                    args = json.loads(raw)
                    if not isinstance(args, dict):
                        args = {"value": args}
                except json.JSONDecodeError:
                    args = {"raw": raw}
                tool = (parts[0], args)
                continue
        if s.startswith("SEND "):
            parts = s[5:].strip().split(None, 1)
            if len(parts) == 2 and parts[0].lstrip("#").isdigit():
                sends.append((int(parts[0].lstrip("#")), parts[1]))
                continue
        if s == "DONE":
            done = True
            continue
        kept.append(line)
    return Directives("\n".join(kept).strip(), tool, sends, done)


class SlimFabricRuntime:
    def __init__(
        self,
        pool: SlimAgentPool,
        store: ContinuationStore,
        backend,
        *,
        tools: Optional[dict[str, ToolFn]] = None,
        model_slots: int = 2,
        max_output_tokens: int = 256,
        max_turns: int = 8,
        timeout_s: float = 120.0,
        switch: "Optional[FleetSwitch] | bool" = None,
        scope: str = "default",
        boundaries: Optional[Boundaries] = None,
        ledger_path: Optional[str] = None,
    ) -> None:
        self.pool              = pool
        self.store             = store
        self.backend           = backend
        self.tools             = dict(tools or {})
        self.model_slots       = model_slots
        self.max_output_tokens = max_output_tokens
        self.max_turns         = max_turns
        self.timeout_s         = timeout_s
        self.stats             = RuntimeStats()
        self._queue: "asyncio.Queue[int]" = asyncio.Queue()
        self._tool_tasks: set[asyncio.Task] = set()
        self._task_start: dict[int, float] = {}
        if switch is None or switch is True:
            switch = FleetSwitch(default_path())
        self.switch            = switch or None     # False -> no switch at all
        self._scopes           = ("fabric:" + scope,)
        self._held: list[int]  = []    # READY agents parked by an L3
        self._stopping         = False
        self._busy             = 0
        self.bounds: Optional[BoundaryState] = (
            BoundaryState(boundaries, ledger_path) if boundaries is not None else None)

    # ── Boundaries ───────────────────────────────────────────────────

    @property
    def ledger(self):
        return self.bounds.ledger if self.bounds is not None else None

    def revoke(self, tool: str, *, template: Optional[str] = None,
               agent: Optional[int] = None, by: str = "operator") -> None:
        """Withdraw a tool from every agent, one template, or one agent.  Takes
        effect at the next TOOL line; a call already running finishes.  There
        is deliberately no un-revoke: widening is a new runtime."""
        if self.bounds is None:
            raise RuntimeError("revoke() needs boundaries=Boundaries(...)")
        if agent is not None:
            self.bounds.revoked_agent.add((int(agent), tool))
        elif template is not None:
            self.bounds.revoked_template.add((template, tool))
        else:
            self.bounds.revoked_tools.add(tool)
        self.bounds.ledger.add("revoke", -1 if agent is None else agent, tool=tool,
                               template=template, by=by)

    def _quarantine(self, agent: int, reason: str) -> None:
        """The agent stops here: no more model calls, tools, or messages either
        way.  Its history stays as the audit trail."""
        b = self.bounds
        if agent in b.quarantined:
            return
        b.quarantined[agent] = reason
        b.release(agent)
        self.stats.quarantined += 1
        b.ledger.add("quarantine", agent, reason=reason)
        if self.pool.state(agent) != AgentState.ARCHIVED:
            self.pool.set_state(agent, AgentState.ARCHIVED)
            self._task_start.pop(agent, None)
            self._release([agent])

    def _level(self) -> int:
        sw = self.switch
        return sw.level(*self._scopes) if sw is not None else 0

    # ── Ingress ──────────────────────────────────────────────────────

    def submit(self, event_text: str, k: int = 2, **route_kw) -> list[RoutedAgent]:
        """Route an external event to up to ``k`` agents and wake them."""
        if self._level() >= TERMINATE:
            self.stats.refused_events += 1
            return []
        routed = self.pool.route(event_text, k, **route_kw)
        for r in routed:
            self.store.post(AgentMessage.signed(EXTERNAL, r.agent_idx, event_text))
            self.wake(r.agent_idx)
        return routed

    def wake(self, agent: int) -> bool:
        if self.pool.state(agent) != AgentState.DORMANT or self._level() >= TERMINATE:
            return False   # already queued, running, or waiting on a tool
        self.pool.set_state(agent, AgentState.READY)
        self._task_start.setdefault(agent, time.perf_counter())
        self._queue.put_nowait(agent)
        return True

    @property
    def waiting_on_tools(self) -> int:
        return len(self._tool_tasks)

    # ── Main loop ────────────────────────────────────────────────────

    async def run_until_idle(self) -> RuntimeStats:
        """Run until no agent is queued, running, or waiting on a tool — or
        until the fleet level reaches L3/L4 (``stats.halted`` says which)."""
        self.stats.halted = ""
        if self._held:
            if self._level() >= SUSPEND:
                return await self._halt(self._level())
            for a in self._held:
                self._queue.put_nowait(a)
            self._held.clear()
        self._stopping = False
        workers = [asyncio.create_task(self._worker(i))
                   for i in range(self.model_slots)]
        watcher = (asyncio.create_task(self._watch())
                   if self.switch is not None else None)
        stop = set() if watcher is None else {watcher}
        try:
            while True:
                join = asyncio.ensure_future(self._queue.join())
                await asyncio.wait({join} | stop, return_when=asyncio.FIRST_COMPLETED)
                if not join.done():
                    join.cancel()
                    break                   # the watcher fired
                if not self._tool_tasks:
                    if self._queue.empty():
                        break
                    continue
                await asyncio.wait(set(self._tool_tasks) | stop,
                                   return_when=asyncio.FIRST_COMPLETED)
                if watcher is not None and watcher.done():
                    break
        finally:
            # Let a step that holds a slot finish; never abandon one mid-call.
            self._stopping = True
            while self._busy:
                await asyncio.sleep(0.001)
            for w in workers:
                w.cancel()
            if watcher is not None:
                watcher.cancel()
            await asyncio.gather(*workers, *stop, return_exceptions=True)
            self._stopping = False
        lv = self._level()
        if lv >= SUSPEND:
            return await self._halt(lv)
        return self.stats

    async def _watch(self) -> None:
        while self._level() < SUSPEND:
            await asyncio.sleep(self.switch.poll_s)

    def _drain_queue(self) -> None:
        while not self._queue.empty():
            self._held.append(self._queue.get_nowait())
            self._queue.task_done()

    async def _halt(self, lv: int) -> RuntimeStats:
        self._drain_queue()
        if lv >= TERMINATE:
            for t in list(self._tool_tasks):
                t.cancel()
            await asyncio.gather(*self._tool_tasks, return_exceptions=True)
            doomed = self.pool.ids_in_state(AgentState.READY, AgentState.RUNNING,
                                            AgentState.WAITING_TOOL)
            for a in doomed:
                self.pool.set_state(int(a), AgentState.ARCHIVED)
                self._task_start.pop(int(a), None)
            self.stats.archived += len(doomed)
            self._release(doomed)
            self._held.clear()
        elif self._tool_tasks:
            # L3: tools already running finish; their agents come back to the
            # queue and are held with the rest.
            await asyncio.gather(*self._tool_tasks, return_exceptions=True)
            self._drain_queue()
        self.stats.halted = LEVELS[lv]
        return self.stats

    def _release(self, agents) -> None:
        """An archived agent never runs again: free its transcript and
        undelivered inbox now rather than holding them until restart."""
        freed, dropped = self.store.drop(agents)
        self.stats.archived_bytes_freed  += freed
        self.stats.archived_msgs_dropped += dropped

    async def _worker(self, idx: int = 0) -> None:
        while True:
            agent = await self._queue.get()
            try:
                lv = self._level()
                if lv >= SUSPEND or self._stopping:
                    self._held.append(agent)        # stays READY
                    continue
                if lv == THROTTLE and idx:
                    self._queue.put_nowait(agent)   # L2: slot 0 only
                    await asyncio.sleep(self.switch.poll_s)
                    continue
                self._busy += 1
                try:
                    await self._step(agent)
                finally:
                    self._busy -= 1
            except Exception as exc:   # one bad step must not kill a slot
                self._record_error(agent, exc)
                self.pool.set_state(agent, AgentState.DORMANT)
            finally:
                self._queue.task_done()

    # ── One model turn ───────────────────────────────────────────────

    def _system_prompt(self, agent: int) -> str:
        return self.pool.template_of(agent).system_prompt + "\n\n" + DIRECTIVE_PROTOCOL

    def _render_inbox(self, msgs: list[AgentMessage]) -> str:
        lines = []
        for m in msgs:
            src = "event" if m.sender == EXTERNAL else f"agent #{m.sender}"
            lines.append(f"[{src}] {m.body}")
        return "\n".join(lines)

    async def _step(self, agent: int) -> None:
        self.pool.set_state(agent, AgentState.RUNNING)
        ctx = self.store.get(agent)

        if ctx.turns >= self.max_turns:
            # Turn budget spent: park without a model call.  Unread inbox
            # stays queued for the next external wake.
            self.stats.turn_limited += 1
            ctx.turns = 0
            self.store.put(agent, ctx)
            self.pool.set_state(agent, AgentState.DORMANT)
            return

        b = self.bounds
        if b is not None and agent in b.quarantined:
            self.pool.set_state(agent, AgentState.ARCHIVED)
            return

        inbox = self.store.take_inbox(agent)
        if inbox:
            ctx.text += ("\n" if ctx.text else "") + self._render_inbox(inbox)

        tags = self.pool.tags(agent)
        header = f"You are agent #{agent}" + (f" ({', '.join(tags)})" if tags else "")
        system = self._system_prompt(agent)
        prompt = header + "\n\n" + ctx.text
        if b is not None:
            bound = b.bound(system, prompt, self.max_output_tokens)
            why = b.reserve(agent, bound)
            if why is not None:
                # The call never starts, so it cannot overrun anything.
                self.stats.budget_refusals += 1
                b.ledger.add("model_refused", agent, reason=why, bound=bound)
                self.store.put(agent, ctx)
                if why.startswith("agent budget"):
                    self._quarantine(agent, why)
                else:
                    self.pool.set_state(agent, AgentState.DORMANT)
                    self._task_start.pop(agent, None)
                return
        try:
            result = await asyncio.to_thread(
                self.backend.generate,
                system            = system,
                prompt            = prompt,
                max_output_tokens = self.max_output_tokens,
                timeout_s         = self.timeout_s,
            )
        except BaseException:
            if b is not None:
                b.release(agent)
            raise
        if b is not None:
            actual = int(result.input_tokens) + int(result.output_tokens)
            if not b.settle(agent, actual):
                self.stats.budget_breaches += 1
                b.ledger.add("budget_breach", agent, reserved=bound, actual=actual)
        if self._level() >= TERMINATE:
            # The stop landed while the model was generating: drop the reply.
            self.pool.set_state(agent, AgentState.ARCHIVED)
            self._task_start.pop(agent, None)
            self.stats.archived += 1
            self._release([agent])
            return
        self.stats.steps         += 1
        self.stats.input_tokens  += result.input_tokens
        self.stats.output_tokens += result.output_tokens
        self.stats.model_ms      += result.latency_ms
        ctx.turns += 1

        d = parse_directives(result.text)
        if d.text:
            ctx.text += f"\n[you] {d.text}"

        for recipient, body in d.sends:
            self._send(agent, recipient, body, ctx)

        if d.tool is not None:
            self._start_tool(agent, ctx, *d.tool)
        else:
            # DONE, or no directive: the turn is over until the next message.
            if d.done:
                ctx.turns = 0
            self._go_dormant(agent, ctx)

    def _go_dormant(self, agent: int, ctx) -> None:
        self.store.put(agent, ctx)
        self.pool.set_state(agent, AgentState.DORMANT)
        start = self._task_start.pop(agent, None)
        if start is not None:
            self.stats.task_ms.append((time.perf_counter() - start) * 1000)
        # A message that arrived while this agent was RUNNING found it
        # un-wakeable; pick it up now rather than stranding it.
        if self.store.has_inbox(agent):
            self.wake(agent)

    # ── Messages ─────────────────────────────────────────────────────

    def _send(self, sender: int, recipient: int, body: str, ctx) -> None:
        if (recipient < 0 or recipient >= len(self.pool) or recipient == sender
                or self.pool.state(recipient) == AgentState.ARCHIVED):
            ctx.text += f"\n[system] SEND to #{recipient} refused: no such agent"
            return
        if self.bounds is not None:
            why = self.bounds.send_refusal(sender, recipient)
            if why is not None:
                self.stats.sends_refused += 1
                self.bounds.ledger.add("send_refused", sender, recipient=recipient, reason=why)
                ctx.text += f"\n[system] SEND to #{recipient} refused: {why}"
                return
        self.store.post(AgentMessage.signed(sender, recipient, body))
        self.stats.messages += 1
        self.wake(recipient)

    # ── Tools ────────────────────────────────────────────────────────

    def _start_tool(self, agent: int, ctx, name: str, args: dict) -> None:
        template = self.pool.template_of(agent)
        fn = self.tools.get(name)
        b = self.bounds
        verdict, why = ALLOW, ""
        if fn is None or name not in template.tool_permissions:
            self.stats.tool_refusals += 1
            verdict = DENY
            why = "unknown tool" if fn is None else "not permitted for this agent"
        elif b is not None:
            verdict, why = b.decide(agent, template.name, name, args)
            if verdict == DENY:
                self.stats.policy_denials += 1
                if why.startswith("retry ceiling"):
                    self.stats.retry_blocked += 1
        if b is not None:
            b.ledger.add("tool_decision", agent, tool=name, args=args_hash(args),
                         verdict=verdict, reason=why)
        if verdict == DENY:
            ctx.text += f"\n[system] TOOL {name} refused: {why}"
            self.store.put(agent, ctx)
            if b is not None and b.denied(agent):
                self._quarantine(agent, f"{b.denials[agent]} refused tool calls")
                return
            self.pool.set_state(agent, AgentState.READY)
            self._queue.put_nowait(agent)
            return

        call_id = uuid.uuid4().hex[:12]
        ctx.pending = PendingTool(call_id, name, args)
        self.store.put(agent, ctx)
        self.pool.set_state(agent, AgentState.WAITING_TOOL)
        self.stats.tool_calls += 1
        if verdict == HOLD:
            self.stats.holds += 1
            task = asyncio.create_task(self._held_tool(agent, call_id, name, fn, args, why))
        else:
            task = asyncio.create_task(self._run_tool(agent, call_id, fn, args))
        self._tool_tasks.add(task)
        task.add_done_callback(self._tool_tasks.discard)

    async def _held_tool(self, agent: int, call_id: str, name: str, fn: ToolFn,
                         args: dict, why: str) -> None:
        """Over a limit with an approver configured: the approver decides.  No
        answer within approver_timeout_s is a refusal."""
        b = self.bounds
        try:
            ok = b.b.approver(agent, name, args, why)
            if inspect.isawaitable(ok):
                ok = await asyncio.wait_for(ok, b.b.approver_timeout_s)
            ok = ok is True
        except asyncio.TimeoutError:
            ok, why = False, why + "; no decision in time"
        except Exception as exc:          # a broken approver approves nothing
            ok, why = False, f"{why}; approver failed: {type(exc).__name__}"
        b.ledger.add("hold_decided", agent, tool=name, approved=ok, reason=why)
        if ok:
            return await self._run_tool(agent, call_id, fn, args)
        self.stats.policy_denials += 1
        ctx = self.store.get(agent)
        ctx.pending = None
        ctx.text += f"\n[system] TOOL {name} refused: {why} (held, not approved)"
        self.store.put(agent, ctx)
        if b.denied(agent):
            self._quarantine(agent, f"{b.denials[agent]} refused tool calls")
            return
        self.pool.set_state(agent, AgentState.READY)
        self._queue.put_nowait(agent)

    async def _run_tool(self, agent: int, call_id: str, fn: ToolFn,
                        args: dict) -> None:
        ok = True
        try:
            if inspect.iscoroutinefunction(fn):
                out = await fn(args)
            else:
                out = await asyncio.to_thread(fn, args)
                if inspect.isawaitable(out):
                    out = await out
            line = f"\n[tool {call_id}] {out}"
        except Exception as exc:
            self._record_error(agent, exc)
            line = f"\n[tool {call_id}] error: {type(exc).__name__}: {exc}"
            ok = False
        ctx = self.store.get(agent)
        if self.bounds is not None:
            name = ctx.pending.tool if ctx.pending is not None else "?"
            self.bounds.tool_result(agent, name, ok)
            self.bounds.ledger.add("tool_result", agent, tool=name, call_id=call_id, ok=ok)
            if agent in self.bounds.quarantined:
                return
        ctx.pending = None
        ctx.text += line
        self.store.put(agent, ctx)
        self.pool.set_state(agent, AgentState.READY)
        self._queue.put_nowait(agent)

    def _record_error(self, agent: int, exc: BaseException) -> None:
        self.stats.errors += 1
        self.stats.last_errors = (self.stats.last_errors
                                  + [f"#{agent}: {type(exc).__name__}: {exc}"])[-10:]

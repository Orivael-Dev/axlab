"""Unit tests for the slim agent fabric — pool, board, store, runtime."""
from __future__ import annotations

import asyncio
import sys
import time

import numpy as np
import pytest


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv("AXLAB_MASTER_KEY", "test" + "0" * 60)
    for mod in list(sys.modules):
        if mod.startswith(("axlab.fabric", "axlab.signing")):
            sys.modules.pop(mod, None)
    yield


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _template(name="billing", kws=("refund", "invoice"), tools=("lookup",)):
    from axlab.fabric.slim_pool import AgentTemplate
    return AgentTemplate(
        name=name, role=f"{name} agent", wake_conditions=list(kws),
        system_prompt=f"You are a {name} agent.",
        tool_permissions=list(tools),
    ).sign()


def _pool(board=False, **kw):
    from axlab.fabric.embedding_board import EmbeddingBoard
    from axlab.fabric.slim_pool import SlimAgentPool
    pool = SlimAgentPool(4, board=EmbeddingBoard(4, dim=64) if board else None, **kw)
    pool.add_template(_template("billing", ("refund", "invoice")))
    pool.add_template(_template("shipping", ("delivery", "tracking"), tools=()))
    return pool


class Scripted:
    """Backend whose reply is chosen by a function of the prompt."""
    name, model = "scripted", "test"

    def __init__(self, fn):
        self.fn = fn
        self.calls = []

    def generate(self, *, system, prompt, max_output_tokens, timeout_s=60.0):
        from axlab.fabric.backends import BackendResult
        self.calls.append((system, prompt))
        text = self.fn(prompt)
        return BackendResult(text=text, input_tokens=1, output_tokens=1,
                             latency_ms=1, backend=self.name, model=self.model)


# ─── Templates and rows ───────────────────────────────────────────────────────


def test_unsigned_template_rejected():
    from axlab.fabric.slim_pool import AgentTemplate, SlimAgentPool
    t = AgentTemplate(name="x", role="x", wake_conditions=[], system_prompt="")
    with pytest.raises(ValueError):
        SlimAgentPool().add_template(t)


def test_template_tamper_detected():
    t = _template()
    t.tool_permissions.append("shell")
    assert not t.verify()


def test_spawn_grows_and_rows_verify():
    pool = _pool()
    ids = pool.spawn("billing", 10)          # capacity starts at 4
    assert ids == list(range(10)) and len(pool) == 10
    assert all(pool.verify_row(i) for i in ids)


def test_row_tamper_detected():
    from axlab.fabric.slim_pool import AgentState
    pool = _pool()
    pool.spawn("billing", 2)
    pool._state[1] = AgentState.ARCHIVED     # bypass set_state
    assert pool.verify_row(0) and not pool.verify_row(1)


# ─── Routing ──────────────────────────────────────────────────────────────────


def test_route_by_template_keyword():
    pool = _pool()
    pool.spawn("billing", 3)
    pool.spawn("shipping", 3)
    routed = pool.route("where is my delivery", k=2)
    assert [r.template for r in routed] == ["shipping", "shipping"]


def test_route_no_match_returns_empty():
    pool = _pool()
    pool.spawn("billing", 3)
    assert pool.route("tell me a joke", k=2) == []


def test_route_skips_busy_agents():
    from axlab.fabric.slim_pool import AgentState
    pool = _pool()
    pool.spawn("billing", 2)
    pool.set_state(0, AgentState.WAITING_TOOL)
    assert [r.agent_idx for r in pool.route("refund please", k=2)] == [1]


def test_route_spreads_load_least_recently_active():
    from axlab.fabric.slim_pool import AgentState
    pool = _pool()
    pool.spawn("billing", 3)
    seen = []
    for _ in range(3):
        a = pool.route("refund", k=1)[0].agent_idx
        seen.append(a)
        pool.set_state(a, AgentState.RUNNING)   # stamps last_active
        time.sleep(0.002)
        pool.set_state(a, AgentState.DORMANT)
    assert sorted(seen) == [0, 1, 2]


def test_route_tag_prefers_tagged_agent():
    pool = _pool()
    pool.spawn("billing", 3)
    tagged = pool.spawn("billing", 1, tags=["acct0042"])[0]
    assert pool.route("refund for acct0042", k=1)[0].agent_idx == tagged


def test_hashed_embed_puts_shared_words_closer():
    from axlab.fabric.embedding_board import hashed_embed
    a = hashed_embed("refund my invoice")
    b = hashed_embed("invoice refund needed")
    c = hashed_embed("printer wifi broken")
    assert float(a @ b) > float(a @ c)
    assert abs(float(np.linalg.norm(a)) - 1.0) < 1e-5


def test_board_similarity_matches_dot_product():
    from axlab.fabric.embedding_board import EmbeddingBoard
    board = EmbeddingBoard(2, dim=16)
    rng = np.random.default_rng(0)
    vecs = rng.standard_normal((10, 16)).astype(np.float32)
    board.grow(10)
    for i, v in enumerate(vecs):
        board.post(i, v)
    q = rng.standard_normal(16).astype(np.float32)
    assert np.allclose(board.similarity(q, 10), vecs @ q)


def test_route_with_board_runs_and_scores_embed():
    pool = _pool(board=True)
    pool.spawn("billing", 2)
    r = pool.route("billing agent refund", k=1)[0]
    assert r.embed_part > 0 and r.template == "billing"


def test_board_rows_per_kind_not_per_agent():
    pool = _pool(board=True)                     # 2 templates → 2 rows
    pool.spawn("billing", 100)
    pool.spawn("shipping", 50)
    assert pool.board_rows == 2
    tagged = pool.spawn("billing", 3, tags=["acct0042"])   # one batch row
    assert pool.board_rows == 3
    assert len({int(pool._board_row[i]) for i in tagged}) == 1
    r = pool.route("billing agent acct0042", k=1)[0]
    assert r.agent_idx in tagged


# ─── ContinuationStore ────────────────────────────────────────────────────────


def test_store_trims_old_text_to_history():
    from axlab.fabric.agent_store import ContinuationStore
    s = ContinuationStore(hot_bytes_per_agent=10)
    s.append(1, "0123456789ABCDE")
    assert s.get(1).text == "56789ABCDE"
    assert s.history(1) == "01234"


def test_store_parks_and_restores_lru():
    from axlab.fabric.agent_store import ContinuationStore, PendingTool
    s = ContinuationStore(hot_budget_bytes=900)   # ~3 contexts of 256+ bytes
    for a in range(6):
        ctx = s.get(a)
        ctx.text = f"agent {a}"
        if a == 0:
            ctx.pending = PendingTool("c0", "lookup", {"q": 1})
        s.put(a, ctx)
    assert s.parks > 0 and s.hot_bytes <= 900
    back = s.get(0)                               # unparked from SQLite
    assert back.text == "agent 0" and back.pending.tool == "lookup"
    assert s.hot_bytes == sum(s._sizes.values())


def test_store_accounting_survives_in_place_edits():
    from axlab.fabric.agent_store import ContinuationStore
    s = ContinuationStore()
    for _ in range(5):
        ctx = s.get(7)
        ctx.text += "x" * 100                     # mutate the hot object
        s.put(7, ctx)
    assert s.hot_bytes == len(s.get(7).text) + 256


def test_message_signed_and_tamper_refused():
    from dataclasses import replace
    from axlab.fabric.agent_store import AgentMessage, ContinuationStore
    s = ContinuationStore()
    m = AgentMessage.signed(1, 2, "hello")
    s.post(m)
    with pytest.raises(ValueError):
        s.post(replace(m, body="evil"))
    assert s.has_inbox(2)
    assert [x.body for x in s.take_inbox(2)] == ["hello"]
    assert not s.has_inbox(2)


# ─── Runtime ──────────────────────────────────────────────────────────────────


def test_parse_directives():
    from axlab.fabric.runtime import parse_directives
    d = parse_directives('thinking\nTOOL lookup {"q": 1}\nTOOL other {}\n'
                         'SEND #3 hi there\nDONE')
    assert d.tool == ("lookup", {"q": 1})
    assert d.sends == [(3, "hi there")]
    assert d.done and "thinking" in d.text and "TOOL other" in d.text


def _runtime(pool, fn, **kw):
    from axlab.fabric.agent_store import ContinuationStore
    from axlab.fabric.runtime import SlimFabricRuntime
    backend = Scripted(fn)
    return SlimFabricRuntime(pool, ContinuationStore(), backend, **kw), backend


def test_runtime_tool_suspend_resume_done():
    from axlab.fabric.slim_pool import AgentState
    pool = _pool()
    pool.spawn("billing", 2)
    seen_waiting = []

    async def lookup(args):
        seen_waiting.append(pool.state(0))
        await asyncio.sleep(0.01)
        return "order 42 refunded"

    rt, backend = _runtime(
        pool,
        lambda p: "DONE" if "[tool " in p else 'TOOL lookup {"q": 42}',
        tools={"lookup": lookup})
    routed = rt.submit("refund please", k=1)
    stats = asyncio.run(rt.run_until_idle())
    assert routed[0].agent_idx == 0
    assert seen_waiting == [AgentState.WAITING_TOOL]
    assert stats.steps == 2 and stats.tool_calls == 1 and stats.errors == 0
    assert "order 42 refunded" in backend.calls[1][1]
    assert pool.state(0) == AgentState.DORMANT
    # Shared prefix: identical system prompt for every agent of a template.
    assert backend.calls[0][0] == backend.calls[1][0]


def test_runtime_refuses_unpermitted_tool():
    pool = _pool()
    pool.spawn("shipping", 1)                    # shipping has no tools
    rt, backend = _runtime(
        pool,
        lambda p: "DONE" if "refused" in p else "TOOL lookup {}",
        tools={"lookup": lambda a: "should not run"})
    rt.submit("tracking number?", k=1)
    stats = asyncio.run(rt.run_until_idle())
    assert stats.tool_refusals == 1 and stats.tool_calls == 0
    assert "not permitted" in backend.calls[1][1]


def test_runtime_send_wakes_recipient():
    pool = _pool()
    pool.spawn("billing", 1)                     # agent 0
    pool.spawn("shipping", 1)                    # agent 1
    rt, backend = _runtime(
        pool,
        lambda p: ("SEND 1 please ship order 42\nDONE"
                   if p.startswith("You are agent #0") else "DONE"))
    rt.submit("refund invoice", k=1)
    stats = asyncio.run(rt.run_until_idle())
    assert stats.messages == 1
    assert any("[agent #0] please ship order 42" in p for _, p in backend.calls)


def test_runtime_message_to_running_agent_not_stranded():
    from axlab.fabric.agent_store import AgentMessage
    pool = _pool()
    pool.spawn("billing", 1)
    loop_box = []

    def fn(prompt):
        # Runs in a worker thread while agent 0 is RUNNING; the store is
        # loop-thread-only, so deliver the message via the loop.
        if len(backend.calls) == 1:
            loop_box[0].call_soon_threadsafe(
                rt.store.post, AgentMessage.signed(-1, 0, "second event"))
            time.sleep(0.05)
        return "DONE"

    rt, backend = _runtime(pool, fn)

    async def main():
        loop_box.append(asyncio.get_running_loop())
        rt.submit("refund", k=1)
        await rt.run_until_idle()

    asyncio.run(main())
    assert len(backend.calls) == 2
    assert "second event" in backend.calls[1][1]


def test_runtime_turn_limit():
    pool = _pool()
    pool.spawn("billing", 1)
    rt, backend = _runtime(pool, lambda p: "TOOL lookup {}",
                           tools={"lookup": lambda a: "again"}, max_turns=3)
    rt.submit("refund", k=1)
    stats = asyncio.run(rt.run_until_idle())
    assert stats.steps == 3 and stats.turn_limited == 1

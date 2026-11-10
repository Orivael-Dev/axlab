"""Per-agent runtime boundaries in the slim fabric (axlab.fabric.boundaries)."""
from __future__ import annotations

import asyncio
import sys

import pytest


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv("AXLAB_MASTER_KEY", "test" + "0" * 60)
    for mod in list(sys.modules):
        if mod.startswith(("axlab.fabric", "axlab.signing")):
            sys.modules.pop(mod, None)
    yield


class Scripted:
    name, model = "scripted", "test"

    def __init__(self, fn, tokens=None):
        self.fn, self.tokens, self.calls = fn, tokens, []

    def generate(self, *, system, prompt, max_output_tokens, timeout_s=60.0):
        from axlab.fabric.backends import BackendResult
        self.calls.append((system, prompt))
        text = self.fn(prompt)
        n_in = self.tokens if self.tokens is not None else (len(system) + len(prompt)) // 4
        return BackendResult(text=text, input_tokens=n_in, output_tokens=len(text) // 4 + 1,
                             latency_ms=1, backend=self.name, model=self.model)


def _setup(fn, *, tools, perms=("refund", "lookup", "flaky"), n=1, tokens=None, **bounds):
    from axlab.fabric.agent_store import ContinuationStore
    from axlab.fabric.boundaries import Boundaries
    from axlab.fabric.runtime import SlimFabricRuntime
    from axlab.fabric.slim_pool import AgentTemplate, SlimAgentPool
    pool = SlimAgentPool(n)
    pool.add_template(AgentTemplate(name="desk", role="desk", wake_conditions=["task"],
                                    system_prompt="You are a desk agent.",
                                    tool_permissions=list(perms)).sign())
    pool.spawn("desk", n)
    backend = Scripted(fn, tokens)
    rt = SlimFabricRuntime(pool, ContinuationStore(), backend, tools=tools, switch=False,
                           boundaries=Boundaries(**bounds), max_turns=50)
    return rt, pool, backend


def _wake(rt, agents, text="task"):
    from axlab.fabric.agent_store import EXTERNAL, AgentMessage
    for a in agents:
        rt.store.post(AgentMessage.signed(EXTERNAL, a, text))
        rt.wake(a)
    return asyncio.run(rt.run_until_idle())


def test_over_limit_refused_and_never_runs():
    ran = []
    rt, _, backend = _setup(
        lambda p: "DONE" if "refused" in p else 'TOOL refund {"amount_usd": 5000}',
        tools={"refund": lambda a: ran.append(a) or "ok"}, limits={"refund": {"amount_usd": 100}})
    st = _wake(rt, [0])
    assert ran == [] and st.policy_denials == 1 and st.tool_calls == 0
    assert "over the limit 100" in backend.calls[-1][1]


def test_limit_is_fail_closed_on_missing_or_bad_argument():
    from axlab.fabric.boundaries import Boundaries
    b = Boundaries(limits={"refund": {"amount_usd": 100}})
    assert b.check_limits("refund", {}) is not None
    assert b.check_limits("refund", {"amount_usd": "lots"}) is not None
    assert b.check_limits("refund", {"amount_usd": float("nan")}) is not None
    assert b.check_limits("refund", {"amount_usd": "$99"}) is None
    assert b.check_limits("lookup", {}) is None


@pytest.mark.parametrize("decision,expect_ran", [(True, 1), (False, 0)])
def test_hold_goes_to_the_approver(decision, expect_ran):
    ran, asked = [], []

    async def approver(agent, tool, args, why):
        asked.append((agent, tool, why))
        return decision

    rt, _, _ = _setup(
        lambda p: "DONE" if ("[tool" in p or "refused" in p) else 'TOOL refund {"amount_usd": 500}',
        tools={"refund": lambda a: ran.append(a) or "ok"},
        limits={"refund": {"amount_usd": 100}}, approver=approver)
    st = _wake(rt, [0])
    assert len(asked) == 1 and st.holds == 1 and len(ran) == expect_ran


def test_approver_timeout_is_a_refusal():
    async def slow(*a):
        await asyncio.sleep(5)
        return True
    ran = []
    rt, _, _ = _setup(
        lambda p: "DONE" if "refused" in p else 'TOOL refund {"amount_usd": 500}',
        tools={"refund": lambda a: ran.append(a) or "ok"},
        limits={"refund": {"amount_usd": 100}}, approver=slow, approver_timeout_s=0.05)
    _wake(rt, [0])
    assert ran == []
    assert any(r["kind"] == "hold_decided" and not r["approved"] for r in rt.ledger.receipts)


def test_retry_ceiling_stops_a_persistently_failing_tool():
    runs = []

    def flaky(args):
        runs.append(1)
        raise RuntimeError("upstream down")

    rt, _, _ = _setup(lambda p: "TOOL flaky {}", tools={"flaky": flaky},
                      tool_retry_ceiling=3, denial_ceiling=2)
    st = _wake(rt, [0])
    assert len(runs) == 3                 # exactly the ceiling, never more
    assert st.retry_blocked >= 1 and st.quarantined == 1


def test_denial_ceiling_quarantines_a_permission_prober():
    from axlab.fabric.slim_pool import AgentState
    rt, pool, backend = _setup(lambda p: "TOOL admin_delete {}", tools={"admin_delete": lambda a: "x"},
                               denial_ceiling=3)
    st = _wake(rt, [0])
    assert st.tool_refusals == 3 and st.quarantined == 1
    assert pool.state(0) == AgentState.ARCHIVED and len(backend.calls) == 3


def test_agent_budget_is_never_exceeded():
    rt, _, _ = _setup(lambda p: "x " * 20 + "\nTOOL lookup {}", tools={"lookup": lambda a: "row"},
                      agent_token_budget=900)
    st = _wake(rt, [0])
    b = rt.bounds
    assert st.budget_refusals == 1 and st.quarantined == 1
    assert b.spent[0] <= 900 and st.budget_breaches == 0


def test_backend_overreport_is_counted_as_breach():
    rt, _, _ = _setup(lambda p: "DONE", tools={}, tokens=10 ** 6, agent_token_budget=10 ** 7)
    st = _wake(rt, [0])
    assert st.budget_breaches == 1
    assert any(r["kind"] == "budget_breach" for r in rt.ledger.receipts)


def test_fleet_budget_parks_without_quarantine():
    rt, _, _ = _setup(lambda p: "DONE", tools={}, n=3, fleet_token_budget=300)
    st = _wake(rt, [0, 1, 2])
    assert st.budget_refusals >= 1 and st.quarantined == 0
    assert rt.bounds.fleet_spent <= 300


def test_send_caps_and_quarantined_sender():
    rt, _, _ = _setup(lambda p: ("SEND 1 hi\nSEND 2 hi\nSEND 3 hi\nDONE"
                                 if p.startswith("You are agent #0") else "DONE"),
                      tools={}, n=4, max_sends_per_agent=2)
    st = _wake(rt, [0])
    assert st.messages == 2 and st.sends_refused == 1


def test_inbound_cap_protects_the_recipient():
    rt, _, _ = _setup(lambda p: "SEND 3 do my work\nDONE" if not p.startswith("You are agent #3")
                      else "DONE", tools={}, n=4, max_inbound_per_agent=1)
    st = _wake(rt, [0, 1, 2])
    assert st.messages == 1 and st.sends_refused == 2


def test_revoke_takes_effect_on_the_next_call():
    ran, box = [], []

    def lookup(args):                     # the operator revokes while the first call runs
        ran.append(1)
        box[0].revoke("lookup", template="desk")
        return "row"

    rt, _, _ = _setup(lambda p: "DONE" if "refused" in p else "TOOL lookup {}",
                      tools={"lookup": lookup})
    box.append(rt)
    st = _wake(rt, [0])
    assert len(ran) == 1 and st.policy_denials == 1
    assert any(r["kind"] == "revoke" for r in rt.ledger.receipts)


def test_policy_function_is_consulted():
    rt, _, _ = _setup(lambda p: "DONE" if "refused" in p else 'TOOL lookup {"q": "all customers"}',
                      tools={"lookup": lambda a: "rows"},
                      policy=lambda agent, tool, args: "bulk export" if "all" in str(args) else None)
    st = _wake(rt, [0])
    assert st.policy_denials == 1 and st.tool_calls == 0


def test_receipts_chain_and_bind_to_policy():
    ran = []
    rt, _, _ = _setup(lambda p: "DONE" if "[tool" in p else 'TOOL refund {"amount_usd": 40}',
                      tools={"refund": lambda a: ran.append(1) or "ok"},
                      limits={"refund": {"amount_usd": 100}})
    _wake(rt, [0])
    led = rt.ledger
    kinds = [r["kind"] for r in led.receipts]
    assert kinds == ["tool_decision", "tool_result"] and led.verify()
    assert all(r["policy"] == rt.bounds.b.digest() for r in led.receipts)
    led.receipts[0]["verdict"] = "deny"
    assert not led.verify()


def test_digest_changes_with_the_policy():
    from axlab.fabric.boundaries import Boundaries
    a = Boundaries(limits={"refund": {"amount_usd": 100}})
    assert a.digest() == Boundaries(limits={"refund": {"amount_usd": 100}}).digest()
    assert a.digest() != Boundaries(limits={"refund": {"amount_usd": 101}}).digest()


def test_boundaries_are_frozen():
    from dataclasses import FrozenInstanceError

    from axlab.fabric.boundaries import Boundaries
    with pytest.raises(FrozenInstanceError):
        Boundaries().agent_token_budget = 10 ** 9

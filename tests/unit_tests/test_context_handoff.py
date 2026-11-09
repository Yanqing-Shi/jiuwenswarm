# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Conversation takeover against real context buffers and an isolated store."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from openjiuwen.core.context_engine import ContextEngine, ContextEngineConfig
from openjiuwen.core.foundation.llm import AssistantMessage, UserMessage
from openjiuwen.harness.schema.state import DeepAgentState
from jiuwenswarm.runtime import context_handoff as handoff


class MemorySession:
    def __init__(self, agent_id, store, state=None):
        self.agent_id, self.store = agent_id, store
        self.state = deepcopy(state or store.get(agent_id, {}))
        self.fail = None

    def get_session_id(self):
        return "conversation-test"

    def get_state(self, key):
        return self.state.get(key)

    def update_state(self, values):
        self.state.update(values)

    async def commit(self):
        if self.fail == "before":
            raise OSError("commit failed")
        self.store[self.agent_id] = deepcopy(self.state)
        if self.fail == "after":
            self.fail = None
            raise OSError("commit acknowledgement lost")


class Agent:
    def __init__(self, agent_id):
        self.card = SimpleNamespace(id=agent_id)
        self.react_agent = SimpleNamespace(context_engine=ContextEngine(
            ContextEngineConfig(enable_openrouter_model_context_window_tokens=False),
        ))
        self.idle = True
        self.guard = lambda: None

    def conversation_is_idle(self):
        return self.idle

    def set_conversation_guard(self, guard):
        self.guard = guard

    def load_state(self, session):
        return DeepAgentState.from_session_dict(session.get_state("deepagent") or {})

    def save_state(self, session):
        pass


@pytest.fixture
def setup_handoff(monkeypatch):
    from jiuwenswarm.server.runtime.session import session_history

    journal, store = {}, {}
    monkeypatch.setattr(session_history, "history_exists", lambda _: False)

    def build():
        controller = handoff.ConversationHandoff("conversation-test")
        controller._read = lambda: deepcopy(journal)

        def write(owner, pending):
            journal.update(deepcopy({"owner": owner, "pending": pending}))
            controller.owner, controller.pending = deepcopy(owner), deepcopy(pending)

        async def restore(identity):
            return MemorySession(identity["agent_id"], store)

        controller._write, controller._restore = write, restore
        return controller

    return build, store, journal


async def seed(agent_id, store, messages):
    agent = Agent(agent_id)
    session = MemorySession(agent_id, store, {"deepagent": {"iteration": 17}, "trajectory_member_turn": 8})
    await agent.react_agent.context_engine.create_context(session=session, history_messages=messages)
    return agent, session


def contents(agent, session):
    context = agent.react_agent.context_engine.get_context(session_id=session.get_session_id())
    return [message.content for message in context.get_messages()]


async def append(agent, session, *messages):
    context = agent.react_agent.context_engine.get_context(session_id=session.get_session_id())
    context.load_state({context.context_id(): {"messages": [*context.get_messages(), *messages]}})


@pytest.mark.asyncio
async def test_agent_team_cached_agent_receives_leader_generated_answer(setup_handoff):
    build, store, journal = setup_handoff
    controller = build()
    agent, agent_session = await seed("actual-agent", store, [UserMessage(content="A-random")])
    first = await controller.begin("agent", "r1")
    await first.ready(agent, agent_session)
    await first.close()
    leader, leader_session = await seed("custom-leader-id", store, [UserMessage(content="stale-team")])
    leader_session.update_state({"team_prompt_context": {"delivered": True}})
    switch = await controller.begin("team", "r2")
    with pytest.raises(handoff.ContextHandoffError):
        agent.guard()
    await switch.ready(leader, leader_session)
    assert contents(leader, leader_session) == ["A-random"]
    assert leader_session.get_state("team_prompt_context") is None
    assert leader_session.get_state("deepagent") == {"iteration": 17}
    assert leader_session.get_state("trajectory_member_turn") == 8
    leader.guard()
    await append(leader, leader_session, UserMessage(content="B"), AssistantMessage(content="D-leader-secret"))
    await switch.close()
    back = await controller.begin("agent", "r3")
    await back.ready(agent, agent_session)
    assert contents(agent, agent_session) == ["A-random", "B", "D-leader-secret"]
    await agent.react_agent.context_engine.create_context(session=agent_session)
    assert contents(agent, agent_session) == ["A-random", "B", "D-leader-secret"]
    with pytest.raises(handoff.ContextHandoffError):
        leader.guard()
    assert journal["owner"]["agent_id"] == "actual-agent"
    await back.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", ["runtime", "followup", "task"])
@pytest.mark.parametrize("profile", ["work", "code"])
async def test_busy_source_rejects_without_changing_owner(setup_handoff, busy, profile):
    build, store, journal = setup_handoff
    controller = build()
    agent, session = await seed("agent", store, [UserMessage(content="A")])
    first = await controller.begin("agent", "a", profile=profile)
    await first.ready(agent, session)
    await first.close()
    if busy == "runtime":
        agent.idle = False
    elif busy == "followup":
        session.update_state({"deepagent": {"pending_follow_ups": ["queued"]}})
    else:
        session.update_state({"deepagent": {"task_plan": {"tasks": [{"id": "x", "status": "in_progress"}]}}})
    with pytest.raises(handoff.ContextHandoffError):
        await controller.begin("team", "b", profile=profile)
    assert journal["owner"]["kind"] == "agent"
    assert not controller.lock.locked()
    agent.guard()


@pytest.mark.asyncio
async def test_handoff_freezes_input_for_entire_commit_window(setup_handoff):
    build, store, _ = setup_handoff
    controller = build()
    source, session = await seed("agent", store, [UserMessage(content="A")])
    first = await controller.begin("agent", "a")
    await first.ready(source, session)
    await first.close()
    target, target_session = await seed("leader", store, [])
    attempt = await controller.begin("team", "b")
    entered, release = asyncio.Event(), asyncio.Event()
    commit = target_session.commit

    async def slow_commit():
        entered.set()
        await release.wait()
        await commit()

    target_session.commit = slow_commit
    task = asyncio.create_task(attempt.ready(target, target_session))
    await entered.wait()
    with pytest.raises(handoff.ContextHandoffError):
        source.guard()
    with pytest.raises(handoff.ContextHandoffError):
        target.guard()
    with pytest.raises(handoff.ContextHandoffError):
        await controller.begin("agent", "c")
    release.set()
    await task
    target.guard()
    await attempt.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["before", "after"])
async def test_target_commit_failure_restores_memory_and_resolves_durable_receipt(setup_handoff, failure):
    build, store, journal = setup_handoff
    controller = build()
    source, source_session = await seed("agent", store, [UserMessage(content="A")])
    first = await controller.begin("agent", "a")
    await first.ready(source, source_session)
    await first.close()
    target, target_session = await seed("leader", store, [UserMessage(content="old")])
    attempt = await controller.begin("team", "b")
    target_session.fail = failure
    with pytest.raises(OSError):
        await attempt.ready(target, target_session)
    assert contents(target, target_session) == ["old"]
    await attempt.close()
    if failure == "before":
        assert journal["owner"]["kind"] == "agent"
        source.guard()
    else:
        assert journal["owner"]["kind"] == "team"
        assert contents(target, target_session) == ["A"]
        target.guard()
    assert journal["pending"] is None


@pytest.mark.asyncio
async def test_restart_after_target_commit_completes_owner_update_without_replacing_new_history(setup_handoff):
    build, store, journal = setup_handoff
    controller = build()
    source, session = await seed("agent", store, [UserMessage(content="A")])
    first = await controller.begin("agent", "a")
    await first.ready(source, session)
    await first.close()
    target, target_session = await seed("leader", store, [])
    attempt = await controller.begin("team", "b")
    write = controller._write

    def crash(owner, pending):
        if pending is None and owner["kind"] == "team":
            raise OSError("metadata write crashed")
        write(owner, pending)

    controller._write = crash
    with pytest.raises(OSError):
        await attempt.ready(target, target_session)
    assert journal["pending"]["target"]["agent_id"] == "leader"
    restarted = build()
    new_agent, new_session = await seed("agent", store, [UserMessage(content="stale-cache")])
    back = await restarted.begin("agent", "c")
    await back.ready(new_agent, new_session)
    assert contents(new_agent, new_session) == ["A"]
    assert journal["owner"]["kind"] == "agent"
    await back.close()


@pytest.mark.parametrize("mode,expected", [
    ("agent", "agent"), ("agent.plan", "agent"), ("agent.work.plan", "agent"),
    ("team", "team"), ("team.plan", "team"), ("team.work.plan", "team"),
    ("agent.code.normal", "agent"), ("agent.code.plan", "agent"),
    ("team.code.normal", "team"), ("team.code.plan", "team"),
    ("team.design", None), ("code", None),
])
def test_mode_scope(mode, expected):
    assert handoff.request_kind(SimpleNamespace(params={"mode": mode})) == expected


@pytest.mark.parametrize("mode,expected", [
    ("agent.work.normal", "agent"), ("agent.work.plan", "agent"),
    ("team.work.normal", "team"), ("team.work.plan", "team"),
    ("agent.code.normal", "agent"), ("agent.code.plan", "agent"),
    ("team.code.normal", "team"), ("team.code.plan", "team"),
    ("agent", None), ("team", None),
    ("team.design", None),
])
def test_canonical_work_mode_overrides_stored_code_profile(mode, expected):
    assert handoff.request_kind(SimpleNamespace(params={
        "mode": mode, "work_mode": "code",
    })) == expected


@pytest.mark.parametrize("mode,expected", [
    ("agent.code.normal", "agent"), ("agent.code.plan", "agent"),
    ("team.code.normal", "team"), ("team.code.plan", "team"),
])
def test_explicit_code_mode_overrides_stored_work_profile(mode, expected):
    request = SimpleNamespace(params={"mode": mode, "work_mode": "work"})
    assert handoff.request_kind(request) == expected
    assert handoff.request_profile(request) == "code"


@pytest.mark.asyncio
async def test_code_agent_and_leader_share_conversation_both_ways(setup_handoff):
    build, store, journal = setup_handoff
    controller = build()
    agent, agent_session = await seed("code-single", store, [UserMessage(content="A")])
    first = await controller.begin("agent", "first", profile="code")
    await first.ready(agent, agent_session)
    await first.close()

    leader, leader_session = await seed("code-leader", store, [UserMessage(content="stale")])
    to_team = await controller.begin("team", "second", profile="code")
    await to_team.ready(leader, leader_session)
    assert contents(leader, leader_session) == ["A"]
    await append(leader, leader_session, AssistantMessage(content="B"))
    await to_team.close()

    to_agent = await controller.begin("agent", "third", profile="code")
    await to_agent.ready(agent, agent_session)
    assert contents(agent, agent_session) == ["A", "B"]
    assert journal["owner"] == {"kind": "agent", "agent_id": "code-single", "profile": "code"}
    await to_agent.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_profile,requested_profile", [("work", "code"), ("code", "work")])
async def test_cross_profile_switch_rejected_before_transfer(setup_handoff, owner_profile, requested_profile):
    build, store, journal = setup_handoff
    controller = build()
    source, source_session = await seed("source", store, [UserMessage(content="A")])
    first = await controller.begin("agent", "first", profile=owner_profile)
    await first.ready(source, source_session)
    await first.close()
    target, target_session = await seed("leader", store, [UserMessage(content="private")])
    with pytest.raises(handoff.ContextHandoffError, match="profile_mismatch"):
        await controller.begin("team", "second", profile=requested_profile)
    assert journal["owner"]["agent_id"] == "source"
    assert contents(target, target_session) == ["private"]
    source.guard()


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["work", "code"])
async def test_cold_source_uses_exact_card_checkpoint_and_excludes_private_member_history(setup_handoff, profile):
    build, store, _ = setup_handoff
    c = build()
    leader, session = await seed("custom-chief", store, [AssistantMessage(content="D")])
    first = await c.begin("team", "first", profile=profile)
    await first.ready(leader, session)
    await first.close()
    store["private-worker"] = {"context": {"default_context_id": {"messages": [UserMessage(content="private")]}}}
    cold = build()
    agent, target = await seed("single", store, [UserMessage(content="stale")])
    attempt = await cold.begin("agent", "second", profile=profile)
    await attempt.ready(agent, target)
    assert contents(agent, target) == ["D"]
    await attempt.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["work", "code"])
async def test_same_executor_request_keeps_context_while_busy(setup_handoff, profile):
    build, store, _ = setup_handoff
    c = build()
    agent, session = await seed("single", store, [UserMessage(content="A")])
    first = await c.begin("agent", "a", profile=profile)
    await first.ready(agent, session)
    await first.close()
    agent.idle = False
    next_request = await c.begin("agent", "b", profile=profile)
    await next_request.ready(agent, session)
    assert contents(agent, session) == ["A"]
    assert session.get_state("context_handoff") is None
    await next_request.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["work", "code"])
async def test_no_waiter_team_producer_retains_handoff_until_leader_ready(setup_handoff, monkeypatch, profile):
    from openjiuwen.agent_teams.harness.conversation import prepare_leader_conversation

    build, store, journal = setup_handoff
    c = build()
    monkeypatch.setattr(handoff, "get_handoff", lambda *_args: c)
    source, session = await seed("single", store, [UserMessage(content="A")])
    first = await c.begin("agent", "a", profile=profile)
    await first.ready(source, session)
    await first.close()
    target, target_session = await seed("chief", store, [])
    release = asyncio.Event()

    async def producer():
        await release.wait()
        await prepare_leader_conversation(target, target_session)
        target.guard()
        await append(target, target_session, UserMessage(content="B"))

    request = SimpleNamespace(session_id="conversation-test", request_id="b", params={
        "mode": "team" if profile == "work" else "team.code.normal",
        "work_mode": profile,
    })
    async with handoff.team_conversation_request(request) as attempt:
        with handoff.bind_team_handoff(attempt):
            task = asyncio.create_task(producer())
            handoff.retain_team_handoff(task)
    assert c.lock.locked()
    with pytest.raises(handoff.ContextHandoffError):
        source.guard()
    release.set()
    await task
    assert journal["owner"]["kind"] == "team"
    assert contents(target, target_session) == ["A", "B"]
    assert not c.lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("profile", ["work", "code"])
async def test_adapter_imports_before_dispatch_for_both_request_paths(setup_handoff, monkeypatch, streaming, profile):
    from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse, AgentResponseChunk
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
    from jiuwenswarm.server.runtime.agent_adapter.interface_code import JiuwenSwarmCodeAdapter

    build, store, _ = setup_handoff
    c = build()
    monkeypatch.setattr(handoff, "get_handoff", lambda *_args: c)
    leader, session = await seed("chief", store, [AssistantMessage(content="D")])
    first = await c.begin("team", "a", profile=profile)
    await first.ready(leader, session)
    await first.close()
    target, target_session = await seed("single", store, [UserMessage(content="stale")])
    target._loop_session = target_session
    adapter_class = JiuWenSwarmDeepAdapter if profile == "work" else JiuwenSwarmCodeAdapter
    adapter = adapter_class.__new__(adapter_class)
    adapter._is_session_scoped_adapter = True
    adapter._instance = target
    dispatched = []

    async def dispatch(request, _inputs):
        target.guard()
        assert contents(target, target_session) == ["D"]
        dispatched.append(request.params["query"])
        return AgentResponse(request_id=request.request_id, channel_id="web", ok=True)

    async def dispatch_stream(request, inputs):
        await dispatch(request, inputs)
        yield AgentResponseChunk(request_id=request.request_id, channel_id="web", is_complete=True)

    adapter._process_message_with_host_context = dispatch
    adapter._process_stream_with_host_context = dispatch_stream
    request = AgentRequest(session_id="conversation-test", request_id="b", channel_id="web",
                           params={"mode": "agent" if profile == "work" else "agent.code.normal",
                                   "work_mode": profile, "query": "B"})
    if streaming:
        chunks = [chunk async for chunk in adapter.process_message_stream_impl(request, {})]
        assert chunks[-1].is_complete
    else:
        assert (await adapter.process_message_impl(request, {})).ok
    assert dispatched == ["B"]


@pytest.mark.asyncio
async def test_background_input_does_not_release_reserved_user_query(setup_handoff):
    build, store, _ = setup_handoff
    c = build()
    source, session = await seed("single", store, [UserMessage(content="A")])
    first = await c.begin("agent", "a")
    await first.ready(source, session)
    await first.close()
    leader, child = await seed("chief", store, [])
    attempt = await c.begin("team", "b")
    await attempt.ready(leader, child)
    leader.guard()
    leader.guard("unrelated-request")
    with pytest.raises(handoff.ContextHandoffError, match="input_pending"):
        await c.begin("agent", "c")
    leader.guard("b")
    back = await c.begin("agent", "c")
    await back.ready(source, session)
    await back.close()
    await attempt.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", ["runtime", "goal", "resume"])
async def test_busy_target_preserves_source_and_its_query(setup_handoff, busy):
    build, store, journal = setup_handoff
    c = build()
    source, session = await seed("single", store, [UserMessage(content="A")])
    first = await c.begin("agent", "a")
    await first.ready(source, session)
    await first.close()
    leader, child = await seed("chief", store, [UserMessage(content="stale")])
    parent = None
    if busy == "runtime":
        leader.idle = False
    elif busy == "goal":
        child.update_state({"harness.goal.record": {"status": "paused"}})
    else:
        parent = MemorySession("team", store, {"teams": {"team": {"pending_resume": {"query": "old"}}}})
        parent.get_team_id = lambda: "team"
    attempt = await c.begin("team", "b")
    with pytest.raises(handoff.ContextHandoffError):
        await attempt.ready(leader, child, parent)
    await attempt.close()
    assert journal["owner"]["kind"] == "agent"
    assert contents(leader, child) == ["stale"]
    source.guard()


@pytest.mark.asyncio
async def test_same_mode_startup_queues_but_cross_mode_rejects(setup_handoff):
    build, store, _ = setup_handoff
    c = build()
    source, session = await seed("single", store, [])
    first = await c.begin("agent", "a")
    queued = asyncio.create_task(c.begin("agent", "b"))
    await asyncio.sleep(0)
    assert not queued.done()
    with pytest.raises(handoff.ContextHandoffError, match="in_progress"):
        await c.begin("team", "c")
    await first.ready(source, session)
    await first.close()
    second = await queued
    await second.ready(source, session)
    await second.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["work", "code"])
async def test_team_stream_handoff_survives_consumption_by_different_tasks(setup_handoff, monkeypatch, profile):
    from openjiuwen.agent_teams.harness.conversation import prepare_leader_conversation
    from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
    from jiuwenswarm.server.runtime.agent_adapter import team_helpers

    build, store, journal = setup_handoff
    c = build()
    monkeypatch.setattr(handoff, "get_handoff", lambda _: c)
    source, session = await seed("single", store, [UserMessage(content="A")])
    first = await c.begin("agent", "a", profile=profile)
    await first.ready(source, session)
    await first.close()
    leader, child = await seed("chief", store, [])
    dispatched = []

    async def stream(request, inputs, _agent):
        await prepare_leader_conversation(leader, child)
        assert contents(leader, child) == ["A"]
        context = handoff.team_handoff_run_context(None)
        leader.guard(context["conversation_request_id"])
        dispatched.append(inputs["query"])
        await append(leader, child, UserMessage(content=inputs["query"]), AssistantMessage(content="D"))
        yield AgentResponseChunk(request_id="b", channel_id="web", payload={"content": "D"})
        yield AgentResponseChunk(request_id="b", channel_id="web", is_complete=True)

    monkeypatch.setattr(team_helpers, "_process_team_message_stream_locked", stream)
    request = AgentRequest(session_id="conversation-test", request_id="b", channel_id="web", params={
        "mode": "team" if profile == "work" else "team.code.normal", "work_mode": profile,
    })
    output = team_helpers.process_team_message_stream(request, {"query": "B"}, object())
    chunk = await asyncio.create_task(anext(output))
    assert chunk.payload["content"] == "D"
    assert (await anext(output)).is_complete
    await output.aclose()
    assert handoff._TEAM_ATTEMPT.get() is None
    assert dispatched == ["B"]
    assert journal["owner"]["agent_id"] == "chief"


@pytest.mark.asyncio
async def test_new_target_keeps_configured_message_processor(setup_handoff):
    from pydantic import BaseModel
    from openjiuwen.core.context_engine.processor.base import ContextProcessor

    class ProcessorConfig(BaseModel):
        prefix: str = "target-policy:"

    @ContextEngine.register_processor()
    class HandoffPolicyProcessor(ContextProcessor):
        async def trigger_add_messages(self, context, messages_to_add, **kwargs):
            return True

        async def on_add_messages(self, context, messages_to_add, **kwargs):
            return None, [UserMessage(content=self.config.prefix + message.content) for message in messages_to_add]

        def save_state(self):
            return {}

        def load_state(self, state):
            pass

    build, store, _ = setup_handoff
    c = build()
    source, session = await seed("single", store, [UserMessage(content="A")])
    first = await c.begin("agent", "a")
    await first.ready(source, session)
    await first.close()
    target, child = Agent("chief"), MemorySession("chief", store)
    target.react_agent._config = SimpleNamespace(context_processors=[("HandoffPolicyProcessor", ProcessorConfig())])
    attempt = await c.begin("team", "b")
    await attempt.ready(target, child)
    context = target.react_agent.context_engine.get_context(session_id=child.get_session_id())
    await context.add_messages(UserMessage(content="B"))
    assert contents(target, child) == ["A", "target-policy:B"]
    assert contents(source, session) == ["A"]
    await attempt.close()

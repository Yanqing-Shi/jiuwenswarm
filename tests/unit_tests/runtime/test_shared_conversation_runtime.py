"""Shared product conversations through the host Session assembly boundaries."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from openjiuwen.core.context_engine import ConversationSnapshot
from openjiuwen.core.foundation.llm import AssistantMessage, Model, ModelClientConfig, ModelRequestConfig, UserMessage
from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk
from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.core.session.checkpointer import CheckpointerFactory
from openjiuwen.core.session.checkpointer.inmemory import InMemoryCheckpointer
from openjiuwen.core.session.checkpointer.persistence import PersistenceCheckpointerProvider
from openjiuwen.harness import create_deep_agent

from jiuwenswarm.runtime.session.conversation import initialize_main_conversation, load_main_binding
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.server.runtime.session import lifecycle, session_history, session_metadata


@pytest_asyncio.fixture(params=["in_memory", "sqlite"])
async def shared_runtime(request, tmp_path, monkeypatch):
    from openjiuwen.agent_teams import paths

    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OPENJIUWEN_HOME", str(tmp_path / "core"))
    monkeypatch.setattr(paths, "_configured_openjiuwen_home", tmp_path / "core")
    sessions = tmp_path / "agent" / "sessions"
    monkeypatch.setattr(lifecycle, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(lifecycle, "get_agent_root_dir", lambda: tmp_path / "agent")
    monkeypatch.setattr(session_metadata, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: sessions)
    checkpointer = (InMemoryCheckpointer() if request.param == "in_memory" else
                    await PersistenceCheckpointerProvider().create({"db_type": "sqlite", "db_path": str(tmp_path / "cp")}))
    monkeypatch.setattr(CheckpointerFactory, "_default_checkpointer", checkpointer)
    sid = "shared-runtime"
    session_metadata.init_session_metadata(session_id=sid, channel_id="web", user_id="user",
                                           mode="agent.work.normal", work_mode="work")
    binding = await initialize_main_conversation(sid)
    await Runner.start()
    yield SimpleNamespace(sid=sid, binding=binding, checkpointer=checkpointer, root=tmp_path)
    await Runner.stop()
    session_metadata._METADATA_QUEUE.join()
    session_metadata.remove_session_metadata_cache(sid)
    if request.param == "sqlite":
        await checkpointer._kv_store.engine.dispose()


def model(monkeypatch, *, name="main-model"):
    value = Model(ModelClientConfig(client_provider="OpenAI", api_key="test", api_base="https://model.invalid"),
                  ModelRequestConfig(model_name=name))
    requests = []

    async def invoke(messages, **kwargs):
        requests.append(deepcopy(messages))
        return AssistantMessage(content="answer")

    async def stream(messages, **kwargs):
        requests.append(deepcopy(messages))
        yield AssistantMessageChunk(content="answer")

    monkeypatch.setattr(value, "invoke", invoke)
    monkeypatch.setattr(value, "stream", stream)
    return value, requests


@pytest.mark.asyncio
async def test_main_record_is_created_only_for_a_session_without_earlier_conversation_data(shared_runtime):
    from openjiuwen.core.session.agent import create_agent_session

    env = shared_runtime
    again = await initialize_main_conversation(env.sid, mode="team.work.normal")
    assert again.key == env.binding.key

    assert await initialize_main_conversation("other-mode", mode="acp") is None
    assert await load_main_binding("other-mode") is None

    history = session_history.get_read_history_path("has-history")
    history.parent.mkdir(parents=True, exist_ok=True)
    history.write_text("")
    assert await initialize_main_conversation("has-history") is None
    assert await load_main_binding("has-history") is None

    old = create_agent_session(session_id="has-checkpoint", card=AgentCard(id="agent"), checkpointer=env.checkpointer)
    await old.pre_run()
    old.update_state({"context": {"default_context_id": {"messages": [UserMessage(content="old window")]}}})
    await old.post_run()
    assert await initialize_main_conversation("has-checkpoint") is None
    assert await load_main_binding("has-checkpoint") is None


_NPC_ROSTER = {"members": [{"name": "fisherman", "role_instructions": "You are the old fisherman.",
                           "title": "Fisherman", "desc": "knows the sea"}]}


@pytest.mark.asyncio
async def test_application_role_gets_a_main_record_only_in_its_native_team_form(shared_runtime):
    """A text-only role rebuilds its input from history and has no window; only its Team form stores one."""
    cases = {
        "single-role": ({}, "agent.work.normal", False),
        "consulting-lead": ({"npc_team": _NPC_ROSTER, "npc_member_sessions": {"fisherman": "consulted-member"}},
                            "agent.work.normal", False),
        "consulted-member": ({"npc_member": {"leader_session_id": "consulting-lead", "name": "fisherman"}},
                             "agent.work.normal", False),
        "team-lead": ({"npc_team": _NPC_ROSTER}, "team.work.normal", True),
    }
    try:
        for sid, (fields, mode, shared) in cases.items():
            session_metadata.init_session_metadata(
                session_id=sid, channel_id="web", user_id="user", mode=mode, work_mode="work",
                execution_profile="app_role", role_instructions="You are the village chief.", **fields,
            )
            binding = await initialize_main_conversation(sid, mode=mode)
            assert (binding is not None) is shared, sid
            assert (await load_main_binding(sid) is not None) is shared, sid
            stored = session_metadata.get_session_metadata(sid, cache_bust=True, enable_writeback=False)
            assert stored.get("context_storage") == ("shared" if shared else None), sid
    finally:
        session_metadata._METADATA_QUEUE.join()
        for sid in cases:
            session_metadata.remove_session_metadata_cache(sid)


@pytest.mark.asyncio
async def test_deep_adapter_binds_before_restore_and_reads_the_shared_record(shared_runtime, monkeypatch):
    from jiuwenswarm.agents.harness.common.session_ops_service import warmup_session_context
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod

    env = shared_runtime
    llm, requests = model(monkeypatch)
    adapter = JiuWenSwarmDeepAdapter()
    adapter._is_session_scoped_adapter = True
    adapter._instance = create_deep_agent(model=llm, card=AgentCard(id="main", name="main"),
                                         system_prompt="current main rules", enable_task_loop=False,
                                         enable_read_image_multimodal=False)
    try:
        await adapter.start_interaction(env.sid)
        session = adapter._instance.loop_session
        assert session.get_conversation_binding().key == env.binding.key
        assert session.checkpointer() is env.checkpointer
        await env.binding.store.commit(env.binding.key, ConversationSnapshot(messages=[UserMessage(content="shared history")]))
        assert await warmup_session_context(deep_agent=adapter._instance, session_id=env.sid)
        assert adapter._instance.react_agent.context_engine.get_context(session_id=env.sid).get_messages()[0].content == "shared history"
        request = AgentRequest(request_id="A", session_id=env.sid, channel_id="web", req_method=ReqMethod.CHAT_SEND,
                               params={"mode": "agent.work.normal", "query": "A"})
        async with adapter._conversation_request_admission(request):
            result = await adapter._instance.invoke({"query": "A"}, session)
        assert result["output"] == "answer"
        assert [m.content for m in requests[-1] if m.role != "system"] == ["shared history", "A"]
        assert [m.content for m in (await env.binding.store.read(env.binding.key)).messages] == ["shared history", "A", "answer"]
    finally:
        await adapter._instance.stop()


@pytest.mark.asyncio
async def test_marked_shared_missing_record_never_falls_back_to_history(shared_runtime):
    from openjiuwen.core.common.exception.errors import BaseError
    from openjiuwen.core.context_engine import ContextEngine
    from openjiuwen.core.session.agent import create_agent_session

    env = shared_runtime
    assert (await load_main_binding(env.sid)).key == env.binding.key
    binding = await load_main_binding(env.sid)
    await binding.store._kv_store.delete(binding.store._storage_key(binding.key))
    session = create_agent_session(session_id=env.sid, card=AgentCard(id="main"),
                                   checkpointer=env.checkpointer, conversation_binding=binding)
    await session.pre_run()
    with pytest.raises(BaseError):
        await ContextEngine().create_context(session=session, history_messages=[UserMessage(content="old UI history")])
    assert (await load_main_binding(env.sid)).key == binding.key


@pytest.mark.asyncio
async def test_old_session_is_not_migrated_or_given_an_empty_shared_record(shared_runtime):
    old = "legacy-runtime"
    session_metadata.init_session_metadata(session_id=old, channel_id="web", mode="agent.work.normal")
    assert await load_main_binding(old) is None
    assert "context_storage" not in session_metadata.get_session_metadata(old)


@pytest.mark.asyncio
async def test_queued_metadata_cannot_remove_the_shared_storage_marker(shared_runtime):
    sid = shared_runtime.sid
    session_metadata._write_metadata_sync(sid, {"session_id": sid, "title": "late title"})
    stored = session_metadata.get_session_metadata(sid, cache_bust=True, enable_writeback=False)
    assert stored["context_storage"] == "shared"
    assert stored["title"] == "late title"


@pytest.mark.asyncio
async def test_team_producer_binds_the_actual_leader_to_the_existing_main_window(shared_runtime, monkeypatch):
    from openjiuwen.agent_teams.harness import bind_leader_preparation
    from openjiuwen.agent_teams.schema.blueprint import LeaderSpec, TeamAgentSpec
    from openjiuwen.harness.schema.deep_agent_spec import DeepAgentSpec, TeamModelConfig
    from jiuwenswarm.runtime.session.conversation import prepare_shared_leader
    from jiuwenswarm.server.runtime.agent_adapter import team_helpers

    env = shared_runtime
    requests, events = [], []
    completed = asyncio.Event()

    async def invoke(self, messages, **kwargs):
        requests.append(deepcopy(messages))
        return AssistantMessage(content="leader answer")

    async def stream(self, messages, **kwargs):
        requests.append(deepcopy(messages))
        yield AssistantMessageChunk(content="leader answer")

    async def broadcast(_channel, _sid, event):
        events.append(event)
        if event.get("event_type") in {"chat.final", "chat.error"}:
            completed.set()
        return 1

    monkeypatch.setattr(Model, "invoke", invoke)
    monkeypatch.setattr(Model, "stream", stream)
    monkeypatch.setattr(team_helpers, "_broadcast_event", broadcast)
    config = TeamModelConfig(model_client_config=ModelClientConfig(client_provider="OpenAI", api_key="test",
                                                                  api_base="https://model.invalid"),
                             model_request_config=ModelRequestConfig(model_name="leader-model"))
    spec = TeamAgentSpec(
        team_name="shared-window-team", spawn_mode="inprocess", evolution_enabled=False,
        leader=LeaderSpec(member_name="custom-chief", display_name="Actual Chief"),
        agents={"leader": DeepAgentSpec(model=config, system_prompt="current Leader rules",
                                        enable_security_rail=False, enable_sys_operation=False,
                                        enable_read_image_multimodal=False)},
    )
    await env.binding.store.commit(env.binding.key, ConversationSnapshot(messages=[UserMessage(content="A from main")]))
    session_metadata.patch_session_metadata(env.sid, {"mode": "team.work.normal"})
    with bind_leader_preparation(prepare_shared_leader):
        task = asyncio.create_task(team_helpers._consume_stream_with_query("web", env.sid, spec, "B to Leader",
                                                                           round_id=1, round_request_id="B"))
    try:
        await asyncio.wait_for(completed.wait(), 15)
        assert not [event for event in events if event.get("event_type") == "chat.error"], events
        assert requests
        assert "A from main" in repr(requests[0]) and "B to Leader" in repr(requests[0])
        assert "current Leader rules" in repr(requests[0])
        saved = await env.binding.store.read(env.binding.key)
        assert "B to Leader" in repr(saved.messages) and "leader answer" in repr(saved.messages)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_native_npc_team_keeps_the_lead_on_the_main_record_and_each_member_on_its_own(
    shared_runtime, monkeypatch, request,
):
    """An application role on the native Team runtime: the lead speaks from the Session's main record,
    a consulted member from its private window, and both speeches reach the player."""
    import json

    from openjiuwen.agent_teams import paths
    from openjiuwen.agent_teams.harness import bind_leader_preparation
    from openjiuwen.agent_teams.schema.blueprint import LeaderSpec, StorageSpec, TeamAgentSpec
    from openjiuwen.core.foundation.llm import ToolCall
    from openjiuwen.harness.schema.deep_agent_spec import DeepAgentSpec, TeamModelConfig
    from jiuwenswarm.agents.swarm.assembly import build_app_role_team_spec
    from jiuwenswarm.runtime.app_role import is_app_role_team
    from jiuwenswarm.runtime.session.conversation import prepare_shared_leader
    from jiuwenswarm.server.runtime.agent_adapter import team_helpers

    env = shared_runtime
    # The imports above may point the Team home at the default data folder; keep this run in the test's own.
    monkeypatch.setattr(paths, "_configured_openjiuwen_home", env.root / "core")
    # Team runtimes outlive a test in this process, so each run gets its own Session, team and team database.
    suffix = request.node.callspec.id.replace("_", "-")
    sid, team_name = f"npc-team-{suffix}", f"npc-village-{suffix}"
    session_metadata.init_session_metadata(
        session_id=sid, channel_id="web", user_id="user", mode="team.work.normal", work_mode="work",
        title="Village Office", execution_profile="app_role", role_instructions="You are the village chief.",
        npc_team=_NPC_ROSTER,
    )
    metadata = session_metadata.get_session_metadata(sid, cache_bust=True, enable_writeback=False)
    assert is_app_role_team(metadata) and "npc_member_sessions" not in metadata
    binding = await initialize_main_conversation(sid, mode="team.work.normal")
    assert binding is not None

    events = []
    spoken = {"team_leader": asyncio.Event(), "fisherman": asyncio.Event()}

    def reply(messages, kwargs):
        tools = [getattr(tool, "name", None) for tool in kwargs.get("tools") or []]
        if "create_task" not in tools:
            return "Fisherman: the east pier at dawn.", None
        if not any(message.role == "tool" for message in messages):
            return "", [ToolCall(id="assign-fisherman", type="function", name="create_task", arguments=json.dumps({
                "tasks": [{"title": "Answer the player", "content": "Tell the player where to fish.",
                           "assignee": "fisherman"}],
            }))]
        return "Chief: our fisherman knows best.", None

    async def invoke(self, messages, **kwargs):
        content, tool_calls = reply(messages, kwargs)
        return AssistantMessage(content=content, tool_calls=tool_calls)

    async def stream(self, messages, **kwargs):
        content, tool_calls = reply(messages, kwargs)
        yield AssistantMessageChunk(content=content, tool_calls=tool_calls)

    async def broadcast(_channel, _sid, event):
        events.append(event)
        if event.get("event_type") == "chat.final" and event.get("member_name") in spoken:
            spoken[event["member_name"]].set()
        return 1

    monkeypatch.setattr(Model, "invoke", invoke)
    monkeypatch.setattr(Model, "stream", stream)
    monkeypatch.setattr(team_helpers, "_broadcast_event", broadcast)
    config = TeamModelConfig(model_client_config=ModelClientConfig(client_provider="OpenAI", api_key="test",
                                                                  api_base="https://model.invalid"),
                             model_request_config=ModelRequestConfig(model_name="npc-model"))
    spec = build_app_role_team_spec(
        TeamAgentSpec(
            team_name=team_name, spawn_mode="inprocess", evolution_enabled=False,
            leader=LeaderSpec(member_name="leader", display_name="Chief"),
            agents={"leader": DeepAgentSpec(model=config, enable_security_rail=False, enable_sys_operation=False,
                                            enable_read_image_multimodal=False)},
            storage=StorageSpec(type="sqlite", params={"connection_string": str(env.root / "team.sqlite")}),
        ),
        metadata,
    )
    assert str(env.root) in spec.agents["fisherman"].workspace.root_path
    member_window = binding.child(kind="team_member", identity=json.dumps([team_name, "fisherman"], ensure_ascii=False))
    with bind_leader_preparation(prepare_shared_leader):
        task = asyncio.create_task(team_helpers._consume_stream_with_query(
            "web", sid, spec, "Where can I fish?", round_id=1, round_request_id="turn-1",
        ))
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in spoken.values())), 30)
        assert not [event for event in events if event.get("event_type") == "chat.error"], events
        finals = {event["member_name"]: event for event in events if event.get("event_type") == "chat.final"}
        assert finals["team_leader"]["content"] == "Chief: our fisherman knows best."
        assert finals["fisherman"]["content"] == "Fisherman: the east pier at dawn."
        assert finals["fisherman"]["display_name"] == "Fisherman"

        async def window_with(window, text):
            for _ in range(100):
                saved = repr((await window.store.read(window.key)).messages)
                if text in saved:
                    return saved
                await asyncio.sleep(0.05)
            raise AssertionError(f"{text!r} never reached {window.key}: {saved}")

        main = await window_with(binding, "Chief: our fisherman knows best.")
        private = await window_with(member_window, "Fisherman: the east pier at dawn.")
        assert "Where can I fish?" in main and "Tell the player where to fish." in private
        assert "Fisherman: the east pier at dawn." not in main and "Chief: our fisherman knows best." not in private
        assert session_metadata.get_session_metadata(sid, cache_bust=True, enable_writeback=False)["mode"] == "team.work.normal"
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        session_metadata._METADATA_QUEUE.join()
        session_metadata.remove_session_metadata_cache(sid)


@pytest.mark.asyncio
async def test_public_runtime_keeps_one_main_record_across_all_eight_modes(shared_runtime, monkeypatch):
    from openjiuwen.agent_teams.harness import bind_leader_preparation
    from openjiuwen.agent_teams.schema.blueprint import LeaderSpec, TeamAgentSpec
    from openjiuwen.harness.schema.deep_agent_spec import DeepAgentSpec, TeamModelConfig
    from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse, AgentResponseChunk
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.runtime.service import AgentRuntime
    from jiuwenswarm.runtime.session.conversation import prepare_shared_leader
    from jiuwenswarm.server.runtime.agent_adapter import team_helpers
    from tests.unit_tests.runtime.test_runtime_service import FakeAgentManager

    env = shared_runtime
    calls, adapters = [], {}
    queues = {}

    async def invoke(self, messages, **kwargs):
        calls.append(deepcopy(messages))
        return AssistantMessage(content="model answer")

    async def stream(self, messages, **kwargs):
        calls.append(deepcopy(messages))
        yield AssistantMessageChunk(content="model answer")

    async def broadcast(_channel, sid, event):
        await queues[sid].put(event)
        return 1

    monkeypatch.setattr(Model, "invoke", invoke)
    monkeypatch.setattr(Model, "stream", stream)
    monkeypatch.setattr(team_helpers, "_broadcast_event", broadcast)

    def role_spec(mode):
        return DeepAgentSpec(
            model=TeamModelConfig(model_client_config=ModelClientConfig(client_provider="OpenAI", api_key="test",
                                                                        api_base="https://model.invalid"),
                                   model_request_config=ModelRequestConfig(model_name=mode)),
            system_prompt=f"current rules for {mode}", enable_security_rail=False, enable_sys_operation=False,
            enable_read_image_multimodal=False,
        )

    async def main_adapter(mode):
        profile = mode.split(".")[1]
        adapter = adapters.get(profile)
        if adapter is None:
            spec = role_spec(mode).model_copy(update={"enable_task_loop": False})
            adapter = JiuWenSwarmDeepAdapter()
            adapter._is_session_scoped_adapter = True
            adapter._instance = spec.build()
            await adapter.start_interaction(env.sid)
            adapters[profile] = adapter
        return adapter

    async def unary(request):
        mode = request.params["mode"]
        if mode.startswith("team."):
            chunks = [chunk async for chunk in streaming(request)]
            return AgentResponse(request_id=request.request_id, channel_id=request.channel_id,
                                 payload=next(chunk.payload for chunk in reversed(chunks)
                                              if chunk.payload.get("event_type") in {"chat.final", "chat.error"}))
        adapter = await main_adapter(mode)
        async with adapter._conversation_request_admission(request):
            from jiuwenswarm.agents.harness.common.session_ops_service import resolve_live_agent_session

            result = await adapter._instance.invoke({"query": request.params["query"]},
                                                     resolve_live_agent_session(adapter._instance, env.sid))
        return AgentResponse(request_id=request.request_id, channel_id=request.channel_id,
                             payload={"event_type": "chat.final", "content": result["output"]})

    async def streaming(request):
        if request.params["mode"].startswith("agent."):
            response = await unary(request)
            yield AgentResponseChunk(request_id=request.request_id, channel_id=request.channel_id,
                                     payload=response.payload, is_complete=True)
            return
        mode = request.params["mode"]
        spec = TeamAgentSpec(team_name="matrix-" + mode.replace(".", "-"), spawn_mode="inprocess",
                             evolution_enabled=False, leader=LeaderSpec(member_name="actual-chief"),
                             agents={"leader": role_spec(mode)})
        queue = queues.setdefault(env.sid, asyncio.Queue())
        with bind_leader_preparation(prepare_shared_leader):
            producer = asyncio.create_task(team_helpers._consume_stream_with_query(
                "web", env.sid, spec, request.params["query"], round_id=1, round_request_id=request.request_id,
            ))
        try:
            while True:
                event = await asyncio.wait_for(queue.get(), 15)
                yield AgentResponseChunk(request_id=request.request_id, channel_id=request.channel_id,
                                         payload=event, is_complete=event.get("event_type") == "chat.final")
                if event.get("event_type") in {"chat.final", "chat.error"}:
                    return
        finally:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)

    manager = FakeAgentManager()
    manager.agent.process_message = unary
    manager.agent.process_message_stream = streaming
    manager.agent.get_live_session_instance = lambda _sid: (
        adapters[session_metadata.get_session_metadata(env.sid)["mode"].split(".")[1]]._instance
        if session_metadata.get_session_metadata(env.sid)["mode"].split(".")[1] in adapters else None
    )

    async def ensure_live(_sid):
        return (await main_adapter(session_metadata.get_session_metadata(env.sid)["mode"]))._instance

    manager.agent.ensure_live_session_instance = ensure_live
    runtime = AgentRuntime(agent_manager=manager, initializer=AsyncMock())
    await runtime.start()
    modes = ["agent.work.normal", "team.work.normal", "agent.work.plan", "team.work.plan",
             "agent.code.normal", "team.code.normal", "agent.code.plan", "team.code.plan", "agent.work.normal"]
    current = modes[0]
    try:
        for index, mode in enumerate(modes):
            if index == 2:
                from openjiuwen.core.context_engine import ContextEngine, ContextEngineConfig
                from openjiuwen.core.context_engine.processor.compressor.micro_compact_processor import (
                    MICRO_COMPACT_CLEARED_MARKER, MicroCompactProcessorConfig,
                )
                from openjiuwen.core.foundation.llm import ToolCall, ToolMessage
                from openjiuwen.core.session.agent import create_agent_session
                from jiuwenswarm.runtime.session.operation_lock import acquire_operation

                with acquire_operation("session", env.sid):
                    compactor = create_agent_session(session_id="compactor", card=AgentCard(id="compactor"),
                                                       checkpointer=env.checkpointer, conversation_binding=env.binding)
                    await compactor.pre_run()
                    context = await ContextEngine(ContextEngineConfig(
                        enable_openrouter_model_context_window_tokens=False,
                    )).create_context(session=compactor, processors=[(
                        "MicroCompactProcessor", MicroCompactProcessorConfig(trigger_threshold=1, keep_recent_per_tool=1),
                    )])
                    for number in range(3):
                        await context.add_messages([
                            AssistantMessage(tool_calls=[ToolCall(id=f"compact-{number}", name="web_search",
                                                                   type="function", arguments="{}")]),
                            ToolMessage(tool_call_id=f"compact-{number}", content=f"old-result-{number}:" + "x" * 100),
                        ])
                    await context.compress_context()
            if mode != current:
                runtime.switch_session_mode(session_id=env.sid, target_mode=mode, expected_mode=current,
                                            channel_id="web", user_id="user")
                current = mode
            request = AgentRequest(request_id=f"matrix-{index}", session_id=env.sid, channel_id="web", user_id="user",
                                   req_method=ReqMethod.CHAT_SEND, params={"mode": mode, "query": f"turn-{index}"})
            events = await asyncio.wait_for(runtime.invoke(request, trigger_hook=False), 20)
            assert not [event for event in events if event.event_type == "chat.error"], events
            assert any(event.event_type == "chat.final" for event in events), events
            assert f"turn-{index}" in repr(calls[-1])
            if index:
                assert f"turn-{index - 1}" in repr(calls[-1])
            if index >= 2:
                assert MICRO_COMPACT_CLEARED_MARKER in repr(calls[-1])
                assert "old-result-0:" not in repr(calls[-1])
            assert (await load_main_binding(env.sid)).key == env.binding.key
        assert len(calls) == len(modes)
        saved = await env.binding.store.read(env.binding.key)
        for index in range(len(modes)):
            assert sum(message.role == "user" and f"turn-{index}" in str(message.content)
                       for message in saved.messages) == 1
    finally:
        for adapter in adapters.values():
            await adapter._instance.stop()
        await runtime.close()


@pytest.mark.asyncio
async def test_cached_leader_rejects_background_work_after_mode_switch_without_leaking_ownership(shared_runtime, monkeypatch):
    from openjiuwen.agent_teams.harness import HarnessState, NativeHarness, TeamHarness, bind_leader_preparation
    from openjiuwen.agent_teams.schema.team import TeamRole
    from openjiuwen.core.session.agent_team import create_agent_team_session
    from openjiuwen.harness.schema.deep_agent_spec import DeepAgentSpec, TeamModelConfig
    from jiuwenswarm.runtime.context import reset_runtime_context, set_runtime_context
    from jiuwenswarm.runtime.service import AgentRuntime
    from jiuwenswarm.runtime.session.conversation import prepare_shared_leader
    from jiuwenswarm.runtime.session.operation_lock import acquire_operation
    from tests.unit_tests.runtime.test_runtime_service import FakeAgentManager

    env = shared_runtime
    session_metadata.patch_session_metadata(env.sid, {"mode": "team.work.normal"})
    llm, requests = model(monkeypatch)
    spec = DeepAgentSpec(model=TeamModelConfig(model_client_config=llm.model_client_config,
                                               model_request_config=llm.model_config),
                         enable_security_rail=False, enable_sys_operation=False, enable_read_image_multimodal=False)
    monkeypatch.setattr(Model, "invoke", lambda self, *args, **kwargs: llm.invoke(*args, **kwargs))
    monkeypatch.setattr(Model, "stream", lambda self, *args, **kwargs: llm.stream(*args, **kwargs))
    native = NativeHarness(spec)
    harness = TeamHarness(spec, None, native, role=TeamRole.LEADER, member_name="actual-chief")
    team_session = create_agent_team_session(session_id=env.sid, conversation_binding=env.binding,
                                             checkpointer=env.checkpointer)
    await team_session.pre_run()
    runtime = AgentRuntime(agent_manager=FakeAgentManager(), initializer=AsyncMock())
    await runtime.start()
    await runtime._session_coordinator.register_session(env.sid, "web")
    token = set_runtime_context(runtime, runtime.agent_manager)
    try:
        with bind_leader_preparation(prepare_shared_leader):
            await harness.start(team_session=team_session)
        await native.send("first Leader turn")
        async with asyncio.timeout(5):
            while native.state is not HarnessState.IDLE:
                await asyncio.sleep(0.01)
        assert len(requests) == 1
        runtime.switch_session_mode(session_id=env.sid, target_mode="agent.work.normal",
                                    expected_mode="team.work.normal", channel_id="web", user_id="user")
        with pytest.raises(RuntimeError, match="session mode changed"):
            await native.send("stale background input")
        assert len(requests) == 1
        with acquire_operation("session", env.sid):
            pass
    finally:
        reset_runtime_context(token)
        await harness.stop()
        await runtime.close()


@pytest.mark.asyncio
async def test_spawned_member_session_has_only_its_private_binding_and_restores_it(shared_runtime):
    from openjiuwen.core.context_engine import ContextEngine
    from openjiuwen.core.session.agent_team import create_agent_team_session

    env = shared_runtime
    parent = create_agent_team_session(session_id=env.sid, conversation_binding=env.binding, checkpointer=env.checkpointer)
    await parent.pre_run()
    card = AgentCard(id="member-card", name="Member display")
    member = await parent.create_member_session(member_id="registered-member")
    assert member.get_conversation_binding() is None
    await member.pre_run()
    child = member.create_agent_session(card=card, member_name="registered-member")
    await child.pre_run()
    private = child.get_conversation_binding()
    assert private.key != env.binding.key
    private_context = await ContextEngine().create_context(session=child)
    await private_context.add_messages(UserMessage(content="private X"))
    await private_context.save_conversation()
    await member.commit()
    assert (await env.binding.store.read(env.binding.key)).messages == []
    restored_parent = create_agent_team_session(session_id=env.sid, conversation_binding=env.binding,
                                                checkpointer=env.checkpointer)
    await restored_parent.pre_run()
    restored_member = await restored_parent.create_member_session(member_id="registered-member")
    restored_child = restored_member.create_agent_session(card=card, member_name="registered-member")
    await restored_child.pre_run()
    assert restored_child.get_conversation_binding().key == private.key
    assert (await ContextEngine().create_context(session=restored_child)).get_messages()[0].content == "private X"


from tests.unit_tests.agentserver.permissions.test_permission_cold_build import cold
from tests.unit_tests.agentserver.permissions.test_permission_answer_cutover import answer_host, _interrupt, _request


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["manual", "auto"])
@pytest.mark.parametrize("kind", ["permission", "ask"])
async def test_shared_conversation_resumes_registered_tool_approval_once(
    shared_runtime, answer_host, monkeypatch, mode, kind,
):
    h = answer_host
    binding = await initialize_main_conversation(h.adapter._parent_session_id)
    answer = await _interrupt(h, monkeypatch, mode=mode, kind=kind)
    assert h.adapter._instance.loop_session.get_conversation_binding().key == binding.key
    from jiuwenswarm.agents.harness.common.session_ops_service import warmup_session_context

    assert await warmup_session_context(deep_agent=h.adapter._instance, session_id=h.adapter._parent_session_id,
                                        refresh_existing=True)
    result = await _request(h, **answer)
    assert result.ok
    assert h.executions == ([{"value": "once"}] if kind == "permission" else [])
    assert len(h.script.calls) == 2
    snapshot = await binding.store.read(binding.key)
    assert snapshot.messages[-1].content == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["manual", "auto"])
async def test_shared_refresh_failure_keeps_the_original_approval_retryable(shared_runtime, answer_host, monkeypatch, mode):
    from openjiuwen.core.single_agent.interrupt.state import INTERRUPTION_KEY

    h = answer_host
    binding = await initialize_main_conversation(h.adapter._parent_session_id)
    answer = await _interrupt(h, monkeypatch, mode=mode, kind="permission")

    async def fail_read(_key):
        raise OSError("shared storage unavailable during resume")

    with monkeypatch.context() as patch:
        patch.setattr(binding.store, "read", fail_read)
        from openjiuwen.core.common.exception.errors import BaseError

        with pytest.raises(BaseError):
            await _request(h, **answer)
    assert h.executions == []
    assert h.adapter._instance.loop_session.get_state(INTERRUPTION_KEY) is not None
    result = await _request(h, **answer)
    assert result.ok
    assert h.executions == [{"value": "once"}]
    assert len(h.script.calls) == 2


# --- Rewinding a shared Session -------------------------------------------------

_TURNS = [("first", "one"), ("second", "two"), ("third", "three")]


def _turn_records(turns):
    records = []
    for index, (question, answer) in enumerate(turns, start=1):
        records.append({"role": "user", "id": f"u{index}", "request_id": f"r{index}",
                        "content": question, "timestamp": float(index)})
        records.append({"role": "assistant", "content": answer, "request_id": f"r{index}",
                        "event_type": "chat.final", "timestamp": index + 0.5})
    return records


def _write_history(sid, turns):
    import json

    path = session_history.get_read_history_path(sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in _turn_records(turns)), encoding="utf-8")
    return path


async def _three_turn_record(env):
    messages = [message for question, answer in _TURNS
                for message in (UserMessage(content=question), AssistantMessage(content=answer))]
    await env.binding.store.commit(env.binding.key, ConversationSnapshot(messages=messages))


async def _record_contents(env):
    return [message.content for message in (await env.binding.store.read(env.binding.key)).messages]


@pytest.fixture
def rewind_history(shared_runtime, monkeypatch):
    sessions = shared_runtime.root / "agent" / "sessions"
    monkeypatch.setattr("jiuwenswarm.agents.harness.common.session_ops_service.get_agent_sessions_dir",
                        lambda: sessions)
    return shared_runtime


@pytest.mark.asyncio
async def test_rewind_replaces_the_shared_record_that_the_next_turn_reads(rewind_history, monkeypatch):
    from jiuwenswarm.agents.harness.common.session_ops_service import rewind_session_context
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod

    env = rewind_history
    llm, requests = model(monkeypatch)
    adapter = JiuWenSwarmDeepAdapter()
    adapter._is_session_scoped_adapter = True
    adapter._instance = create_deep_agent(model=llm, card=AgentCard(id="main", name="main"),
                                         system_prompt="rules", enable_task_loop=False,
                                         enable_read_image_multimodal=False)
    try:
        await adapter.start_interaction(env.sid)
        await _three_turn_record(env)
        _write_history(env.sid, _TURNS[:1])  # the caller has already truncated the history
        assert await rewind_session_context(deep_agent=adapter._instance, session_id=env.sid, turn_index=1)
        contents = await _record_contents(env)
        assert contents[:2] == ["first", "one"]
        assert not {"second", "two", "third", "three"} & set(contents)

        request = AgentRequest(request_id="B", session_id=env.sid, channel_id="web", req_method=ReqMethod.CHAT_SEND,
                               params={"mode": "agent.work.normal", "query": "B"})
        async with adapter._conversation_request_admission(request):
            await adapter._instance.invoke({"query": "B"}, adapter._instance.loop_session)
        seen = [m.content for m in requests[-1] if m.role != "system"]
        assert seen[:2] == ["first", "one"] and seen[-1] == "B"
        assert not {"second", "two", "third", "three"} & set(seen)
    finally:
        await adapter._instance.stop()


@pytest.mark.asyncio
async def test_rewind_reports_failure_when_the_shared_record_is_not_written(rewind_history, monkeypatch):
    from jiuwenswarm.agents.harness.common.session_ops_service import rewind_session_context

    env = rewind_history
    llm, _requests = model(monkeypatch)
    adapter = JiuWenSwarmDeepAdapter()
    adapter._is_session_scoped_adapter = True
    adapter._instance = create_deep_agent(model=llm, card=AgentCard(id="main", name="main"),
                                         system_prompt="rules", enable_task_loop=False,
                                         enable_read_image_multimodal=False)
    try:
        await adapter.start_interaction(env.sid)
        await _three_turn_record(env)
        _write_history(env.sid, _TURNS[:1])
        monkeypatch.setattr(env.binding.store, "commit", AsyncMock(side_effect=OSError("store unavailable")))
        assert not await rewind_session_context(deep_agent=adapter._instance, session_id=env.sid, turn_index=1)
    finally:
        await adapter._instance.stop()


@pytest.mark.asyncio
async def test_rewind_without_a_live_session_still_replaces_the_shared_record(rewind_history, monkeypatch):
    from jiuwenswarm.agents.harness.common.session_ops_service import rewind_session_context

    env = rewind_history
    llm, _requests = model(monkeypatch)
    agent = create_deep_agent(model=llm, card=AgentCard(id="root", name="root"), system_prompt="rules",
                              enable_task_loop=False, enable_read_image_multimodal=False)
    await agent.ensure_initialized()
    await _three_turn_record(env)
    _write_history(env.sid, _TURNS[:1])
    assert await rewind_session_context(deep_agent=agent, session_id=env.sid, turn_index=1)
    contents = await _record_contents(env)
    assert contents[:2] == ["first", "one"]
    assert not {"second", "two", "third", "three"} & set(contents)


@pytest.mark.asyncio
async def test_shared_session_operation_owns_only_a_shared_session(shared_runtime):
    from jiuwenswarm.runtime.session.conversation import shared_session_operation
    from jiuwenswarm.runtime.session.operation_lock import SessionOperationBusyError, acquire_operation

    env = shared_runtime
    async with shared_session_operation(env.sid):
        with pytest.raises(SessionOperationBusyError):
            acquire_operation("session", env.sid)
    acquire_operation("session", env.sid).close()

    session_metadata.init_session_metadata(session_id="legacy-rewind", channel_id="web", mode="agent.work.normal")
    async with shared_session_operation("legacy-rewind"):
        acquire_operation("session", "legacy-rewind").close()


def _rewind_request(sid, method):
    from jiuwenswarm.common.schema.agent import AgentRequest

    return AgentRequest(request_id="rewind-1", channel_id="web", session_id=sid, req_method=method,
                        params={"session_id": sid, "turn_index": 1})


async def _http_rewind(handler, request):
    from jiuwenswarm.server.context import AgentServerServices, RequestContext

    responses = []

    class _Sink:
        async def send_response(self, response):
            responses.append(response)

        async def send_chunk(self, chunk, *, sequence):
            return True

    services = AgentServerServices(SimpleNamespace(_agent_manager=SimpleNamespace(agents={})))
    await handler(RequestContext(request=request, sink=_Sink(), connection_id="1", services=services))
    return responses


async def _ws_rewind(method_name, request, monkeypatch):
    from jiuwenswarm.server import agent_ws_server

    responses = []

    async def send_wire(_ws, payload):
        responses.append(payload)

    monkeypatch.setattr(agent_ws_server, "send_wire_payload", send_wire)
    monkeypatch.setattr(agent_ws_server, "encode_agent_response_for_wire",
                        lambda response, response_id=None: response)
    server = object.__new__(agent_ws_server.AgentWebSocketServer)
    await getattr(server, method_name)(None, request, asyncio.Lock())
    return responses


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["http_full", "http_context", "ws_full", "ws_context"])
async def test_rewind_of_a_busy_shared_session_changes_nothing(rewind_history, monkeypatch, entry):
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.runtime.session.operation_lock import acquire_operation
    from jiuwenswarm.server.handlers import session as handlers

    env = rewind_history
    path = _write_history(env.sid, _TURNS)
    before = path.read_bytes()
    method = ReqMethod.SESSION_REWIND if entry.endswith("full") else ReqMethod.SESSION_REWIND_CONTEXT
    request = _rewind_request(env.sid, method)
    with acquire_operation("session", env.sid):
        if entry == "http_full":
            responses = await _http_rewind(handlers.handle_session_rewind, request)
        elif entry == "http_context":
            responses = await _http_rewind(handlers.handle_session_rewind_context, request)
        elif entry == "ws_full":
            responses = await _ws_rewind("_handle_session_rewind_full", request, monkeypatch)
        else:
            responses = await _ws_rewind("_handle_session_rewind_context", request, monkeypatch)
    assert len(responses) == 1
    assert responses[0].ok is False
    assert responses[0].payload["code"] == "SESSION_BUSY"
    assert path.read_bytes() == before


# --- Leader ownership that no round will release --------------------------------

async def _started_shared_leader(env, monkeypatch):
    from openjiuwen.agent_teams.harness import NativeHarness, TeamHarness, bind_leader_preparation
    from openjiuwen.agent_teams.schema.team import TeamRole
    from openjiuwen.core.session.agent_team import create_agent_team_session
    from openjiuwen.harness.schema.deep_agent_spec import DeepAgentSpec, TeamModelConfig
    from jiuwenswarm.runtime.context import set_runtime_context
    from jiuwenswarm.runtime.service import AgentRuntime
    from jiuwenswarm.runtime.session.conversation import prepare_shared_leader
    from tests.unit_tests.runtime.test_runtime_service import FakeAgentManager

    session_metadata.patch_session_metadata(env.sid, {"mode": "team.work.normal"})
    llm, requests = model(monkeypatch)
    spec = DeepAgentSpec(model=TeamModelConfig(model_client_config=llm.model_client_config,
                                               model_request_config=llm.model_config),
                         enable_security_rail=False, enable_sys_operation=False, enable_read_image_multimodal=False)
    monkeypatch.setattr(Model, "invoke", lambda self, *args, **kwargs: llm.invoke(*args, **kwargs))
    monkeypatch.setattr(Model, "stream", lambda self, *args, **kwargs: llm.stream(*args, **kwargs))
    native = NativeHarness(spec)
    harness = TeamHarness(spec, None, native, role=TeamRole.LEADER, member_name="actual-chief")
    team_session = create_agent_team_session(session_id=env.sid, conversation_binding=env.binding,
                                             checkpointer=env.checkpointer)
    await team_session.pre_run()
    runtime = AgentRuntime(agent_manager=FakeAgentManager(), initializer=AsyncMock())
    await runtime.start()
    await runtime._session_coordinator.register_session(env.sid, "web")
    token = set_runtime_context(runtime, runtime.agent_manager)
    with bind_leader_preparation(prepare_shared_leader):
        await harness.start(team_session=team_session)
    return SimpleNamespace(native=native, harness=harness, runtime=runtime, token=token, requests=requests)


async def _stop_shared_leader(leader):
    from jiuwenswarm.runtime.context import reset_runtime_context

    reset_runtime_context(leader.token)
    await leader.harness.stop()
    await leader.runtime.close()


@pytest.mark.asyncio
async def test_idle_leader_resume_without_query_leaves_the_session_free(shared_runtime, monkeypatch):
    from openjiuwen.agent_teams.harness import HarnessState
    from jiuwenswarm.runtime.session.operation_lock import acquire_operation

    env = shared_runtime
    leader = await _started_shared_leader(env, monkeypatch)
    try:
        await leader.native.send("first Leader turn")
        async with asyncio.timeout(5):
            while leader.native.state is not HarnessState.IDLE:
                await asyncio.sleep(0.01)
        await leader.native.resume()
        assert leader.native.state is HarnessState.IDLE
        acquire_operation("session", env.sid).close()
    finally:
        await _stop_shared_leader(leader)


@pytest.mark.asyncio
async def test_leader_admission_for_work_that_never_starts_is_returned(shared_runtime, monkeypatch):
    from jiuwenswarm.runtime.session.operation_lock import SessionOperationBusyError, acquire_operation

    env = shared_runtime
    leader = await _started_shared_leader(env, monkeypatch)
    try:
        leader.native.check_conversation_admission()
        with pytest.raises(SessionOperationBusyError):
            acquire_operation("session", env.sid)
        leader.native.release_conversation_admission()
        acquire_operation("session", env.sid).close()
    finally:
        await _stop_shared_leader(leader)


@pytest.mark.asyncio
async def test_snapshot_step_leaves_a_shared_window_to_its_record(tmp_path, monkeypatch):
    """A shared window is not copied into a snapshot file or the checkpoint; other contexts still are."""
    from openjiuwen.core.context_engine import ConversationBinding, ConversationKey

    from jiuwenswarm.server.runtime.session import context_snapshot as snapshots

    monkeypatch.setattr(lifecycle, "get_agent_sessions_dir", lambda: tmp_path / "sessions")
    monkeypatch.setattr(lifecycle, "get_agent_root_dir", lambda: tmp_path)
    shared = ConversationBinding(ConversationKey("one-session", "main"), SimpleNamespace())
    values = {snapshots.SNAPSHOT_KEY: {"file": "stale.json"}}
    session = SimpleNamespace(
        get_session_id=lambda: "one-session",
        get_conversation_binding=lambda context_id="default_context_id": (
            shared if context_id == "default_context_id" else None
        ),
        get_state=values.get,
        update_state=values.update,
    )
    engine = SimpleNamespace(get_context=lambda session_id: SimpleNamespace())
    local = {"messages": [UserMessage(content="kept in the checkpoint")], "offload_messages": {}}
    states = {
        "default_context_id": {"messages": [UserMessage(content="lives in the record")], "offload_messages": {}},
        "side_context": local,
    }

    prepared = await snapshots.prepare_context_snapshot(context_engine=engine, session=session, states=states)

    assert prepared == {"side_context": local}
    assert values[snapshots.SNAPSHOT_KEY] is None
    assert not list(tmp_path.rglob("*.json"))

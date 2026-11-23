"""Host-only preparation must follow native commit, interruption, and ownership."""
import asyncio
import copy
from types import SimpleNamespace

import pytest

from openjiuwen.core.context_engine import ContextEngine
from openjiuwen.core.foundation.llm import UserMessage, ToolMessage

from jiuwenswarm.runtime.context_checkpointer import bind_context_checkpoint
from jiuwenswarm.server.runtime.session import context_snapshot as snapshots
from jiuwenswarm.server.runtime.session.history_replay import HistoryReplayError
from tests.unit_tests.test_context_snapshots import stored, user, save, native_session


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupted", [False, True])
async def test_native_commit_and_interrupt_prepare_matching_state(stored, monkeypatch, interrupted):
    _, delegate = await native_session(stored, monkeypatch)
    stored.write([user("old"), user("pending")])
    engine = await save(stored, [UserMessage(content="old")])
    context = engine.get_context(session_id=stored.sid)
    context.set_messages([UserMessage(content="admitted input")])
    context.history_recovery_covered.add("pending")
    stored.session.update_state({"pending_permission": {"call_id": "write-1"}})
    await engine.save_contexts(stored.session)
    if interrupted:
        await stored.session.commit_interrupt()
    else:
        await stored.session.commit()
    loaded = snapshots.load_checkpoint_snapshot(stored.sid, delegate.disk[snapshots.SNAPSHOT_KEY])
    assert loaded.contexts["default_context_id"]["messages"][0].content == "admitted input"
    assert delegate.disk["context"]["default_context_id"]["messages"][0].content == "admitted input"
    assert delegate.disk["pending_permission"]["call_id"] == "write-1"


@pytest.mark.asyncio
async def test_failed_native_commit_keeps_previous_reference(stored, monkeypatch):
    _, delegate = await native_session(stored, monkeypatch)
    stored.write([user("old")])
    engine = await save(stored, [UserMessage(content="committed")])
    previous = copy.deepcopy(delegate.disk)
    engine.get_context(session_id=stored.sid).set_messages([UserMessage(content="not committed")])
    delegate.fail = True
    with pytest.raises(OSError, match="controlled commit failure"):
        await stored.session.commit()
    assert delegate.disk == previous
    assert stored.session.get_state(snapshots.SNAPSHOT_KEY) == previous[snapshots.SNAPSHOT_KEY]
    loaded = snapshots.load_checkpoint_snapshot(stored.sid, previous[snapshots.SNAPSHOT_KEY])
    assert loaded.contexts["default_context_id"]["messages"][0].content == "committed"


@pytest.mark.asyncio
async def test_stale_session_cannot_replace_new_owner_checkpoint(stored, monkeypatch):
    _, first = await native_session(stored, monkeypatch)
    stored.write([user("old")])
    old_session = stored.session
    engine = await save(stored, [UserMessage(content="first owner")])
    previous = copy.deepcopy(first.disk)
    _, second = await native_session(stored, monkeypatch)
    next_engine = ContextEngine()
    await next_engine.create_context(session=stored.session, history_messages=[UserMessage(content="second owner")])
    bind_context_checkpoint(next_engine, stored.session)
    with pytest.raises(HistoryReplayError) as raised:
        await old_session.commit()
    assert raised.value.code == "HISTORY_RESTORE_CHANGED"
    assert first.disk == previous
    assert not second.disk
    assert engine.get_context(session_id=stored.sid).get_messages()[0].content == "first owner"


@pytest.mark.asyncio
async def test_native_memory_offloads_are_preserved_within_budget(stored, monkeypatch):
    _, delegate = await native_session(stored, monkeypatch)
    stored.write([user("old")])
    engine = await save(stored, [UserMessage(content="summary")])
    context = engine.get_context(session_id=stored.sid)
    context.offload_messages("handle", [ToolMessage(content="ORIGINAL", tool_call_id="call")])
    await stored.session.commit()
    loaded = snapshots.load_checkpoint_snapshot(stored.sid, delegate.disk[snapshots.SNAPSHOT_KEY])
    message = loaded.contexts["default_context_id"]["offload_messages"]["handle"][0]
    assert isinstance(message, ToolMessage) and message.content == "ORIGINAL"
    assert message.tool_call_id == "call"


@pytest.mark.asyncio
async def test_large_native_offloads_fail_before_checkpoint_publication(stored, monkeypatch):
    _, delegate = await native_session(stored, monkeypatch)
    stored.write([user("old")])
    engine = await save(stored, [UserMessage(content="summary")])
    previous = copy.deepcopy(delegate.disk)
    monkeypatch.setattr(snapshots, "MAX_SNAPSHOT_BYTES", 2048)
    engine.get_context(session_id=stored.sid).offload_messages("large", [UserMessage(content="x" * 8192)])
    with pytest.raises(HistoryReplayError) as raised:
        await stored.session.commit()
    assert raised.value.code == "HISTORY_RESTORE_LIMIT"
    assert delegate.disk == previous
    assert stored.session.get_state(snapshots.SNAPSHOT_KEY) == previous[snapshots.SNAPSHOT_KEY]


@pytest.mark.asyncio
async def test_consumed_inputs_need_no_new_core_metadata_field(stored, monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter.session_input import SessionInputGuard

    _, delegate = await native_session(stored, monkeypatch)
    stored.write([user("old"), user("pending")])
    engine = await save(stored, [UserMessage(content="old")])
    context = engine.get_context(session_id=stored.sid)
    agent = object()
    owner = SimpleNamespace(react_agent=agent, loop_session=stored.session,
        active_round=SimpleNamespace(work=SimpleNamespace(kind="user", request_id="pending")))
    callback = SimpleNamespace(agent=agent, session=stored.session,
        inputs=SimpleNamespace(source="query", parts=["admitted input"]), context=context, extra={})
    await SessionInputGuard(owner).on_user_message(callback)
    await stored.session.commit()
    loaded = snapshots.load_checkpoint_snapshot(stored.sid, delegate.disk[snapshots.SNAPSHOT_KEY])
    assert loaded.cursor.offset == stored.path.stat().st_size


@pytest.mark.asyncio
async def test_effective_saves_do_not_reinsert_completed_consumption(stored, monkeypatch):
    from jiuwenswarm.agents.harness.common.rails.stream_event_rail import JiuSwarmStreamEventRail
    from jiuwenswarm.common.schema.message import SESSION_REQUEST_COMPLETED_EVENT
    from jiuwenswarm.server.runtime.agent_adapter.session_input import SessionInputGuard

    _, delegate = await native_session(stored, monkeypatch)
    stored.write([user("old"),
        {"role": "assistant", "request_id": "old", "event_type": SESSION_REQUEST_COMPLETED_EVENT}, user("pending"),
        {"role": "assistant", "request_id": "pending", "event_type": SESSION_REQUEST_COMPLETED_EVENT}])
    engine = await save(stored, [UserMessage(content="old")])
    context = engine.get_context(session_id=stored.sid)
    agent = object()
    owner = SimpleNamespace(react_agent=agent, loop_session=stored.session,
        active_round=SimpleNamespace(work=SimpleNamespace(kind="user", request_id="pending")))
    callback = SimpleNamespace(agent=agent, session=stored.session, context=context,
        inputs=SimpleNamespace(source="query", parts=["admitted input"]), extra={})
    await SessionInputGuard(owner).on_user_message(callback)
    rail = JiuSwarmStreamEventRail()
    rail._main_sessions[stored.sid] = stored.session
    rail._deep_agent = SimpleNamespace(react_agent=SimpleNamespace(context_engine=engine))
    # Even a stale callback extra must not be another source of consumption.
    callback.extra = {rail._SID_KEY: stored.sid, "_history_consumed_requests": {"pending"}}
    for _ in range(3):
        await rail._save_effective_context(callback)
        loaded = snapshots.load_checkpoint_snapshot(stored.sid, delegate.disk[snapshots.SNAPSHOT_KEY])
        assert loaded.cursor.offset == stored.path.stat().st_size
        assert not loaded.covered_requests
        assert not context.history_recovery_covered


@pytest.mark.asyncio
async def test_inherited_owner_does_not_capture_a_child_session(stored, monkeypatch):
    from openjiuwen.core.session.agent import create_agent_session
    from openjiuwen.core.single_agent import AgentCard
    from jiuwenswarm.runtime.context_checkpointer import OWNER_KEY

    _, delegate = await native_session(stored, monkeypatch)
    stored.write([user("old")])
    engine = await save(stored, [UserMessage(content="parent")])
    child = create_agent_session(session_id=stored.sid + "-child", card=AgentCard(id="child", name="child"))
    await child.pre_run(inputs=None)
    child.update_state({OWNER_KEY: stored.session.get_state(OWNER_KEY), "child_data": "kept"})
    await child.commit()
    assert delegate.disk["child_data"] == "kept"
    assert snapshots.SNAPSHOT_KEY not in delegate.disk
    assert engine.get_context(session_id=stored.sid).get_messages()[0].content == "parent"


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
async def test_concurrent_commits_keep_frozen_window_and_order(stored, monkeypatch, explicit):
    _, delegate = await native_session(stored, monkeypatch)
    stored.write([user("old")])
    engine = await save(stored, [UserMessage(content="initial")])
    entered, release = asyncio.Event(), asyncio.Event()
    original = delegate.post_agent_execute
    recorded = []

    async def delayed(session):
        if not recorded:
            recorded.append("entered")
            entered.set()
            await release.wait()
        await original(session)
        disk = copy.deepcopy(delegate.disk)
        loaded = snapshots.load_checkpoint_snapshot(stored.sid, disk[snapshots.SNAPSHOT_KEY])
        window = disk["context"]["default_context_id"]["messages"][0].content
        assert loaded.contexts["default_context_id"]["messages"][0].content == window
        recorded.append(window)

    monkeypatch.setattr(delegate, "post_agent_execute", delayed)
    context = engine.get_context(session_id=stored.sid)
    context.set_messages([UserMessage(content="first")])
    first = asyncio.create_task(snapshots.save_context_snapshot(context_engine=engine, session=stored.session)
                                if explicit else stored.session.commit())
    await asyncio.wait_for(entered.wait(), 5)
    context.set_messages([UserMessage(content="second")])
    await engine.save_contexts(stored.session)
    second = asyncio.create_task(stored.session.commit())
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), 5)
    assert recorded == ["entered", "first", "second"]


@pytest.mark.asyncio
async def test_cancelled_commit_preserves_committed_reference_and_releases_lock(stored, monkeypatch):
    _, delegate = await native_session(stored, monkeypatch)
    stored.write([user("old")])
    engine = await save(stored, [UserMessage(content="committed")])
    previous = copy.deepcopy(delegate.disk)
    entered = asyncio.Event()
    original = delegate.post_agent_execute

    async def blocked(session):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(delegate, "post_agent_execute", blocked)
    engine.get_context(session_id=stored.sid).set_messages([UserMessage(content="not yet committed")])
    task = asyncio.create_task(stored.session.commit())
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert delegate.disk == previous
    assert stored.session.get_state(snapshots.SNAPSHOT_KEY) == previous[snapshots.SNAPSHOT_KEY]
    monkeypatch.setattr(delegate, "post_agent_execute", original)
    await asyncio.wait_for(stored.session.commit(), 5)
    assert delegate.disk["context"]["default_context_id"]["messages"][0].content == "not yet committed"


@pytest.mark.asyncio
async def test_accepted_compaction_of_a_shared_window_commits_through_the_checkpoint(stored, monkeypatch):
    """The install saves the window, then the Session commit saves the contexts again inside this wrapper."""
    from openjiuwen.core.context_engine import (
        CompactionCandidate, ContextEngineConfig, ConversationBinding, ConversationKey, ConversationSnapshot,
    )
    from openjiuwen.core.context_engine.processor.compressor.full_compact_processor import FullCompactProcessorConfig
    from openjiuwen.core.foundation.llm import AssistantMessage
    from openjiuwen.core.session.agent import create_agent_session
    from openjiuwen.core.single_agent import AgentCard

    checkpointer, _ = await native_session(stored, monkeypatch)
    binding = ConversationBinding(ConversationKey(stored.sid, "main"), checkpointer.conversation_store())
    await binding.store.create(binding.key)
    await binding.store.commit(binding.key, ConversationSnapshot(messages=[
        UserMessage(content="Keep the public API stable. " * 200),
        AssistantMessage(content="Read the importer and its callers. " * 200),
        UserMessage(content="Write the parser tests next. " * 200),
        AssistantMessage(content="Parser tests are half written."),
    ]))
    # A shared window is bound before the Session's first run.
    stored.session = create_agent_session(session_id=stored.sid, card=AgentCard(id="test-agent", name="test"))
    stored.session.bind_conversation(binding)
    await stored.session.pre_run(inputs=None)
    engine = ContextEngine(ContextEngineConfig(
        enable_tiktoken_counter=False, enable_openrouter_model_context_window_tokens=False,
    ))
    context = await engine.create_context(session=stored.session, processors=[("FullCompactProcessor", (
        FullCompactProcessorConfig(trigger_total_tokens=1, messages_to_keep=1, compression_call_max_tokens=100000)
    ))])

    class Model:
        async def invoke(self, *args, **kwargs):
            return AssistantMessage(content="Importer work: parser tests half written.")

    context._processors[0]._model = Model()
    bind_context_checkpoint(engine, stored.session)

    async def accept(request, next_handler):
        return await next_handler(request)

    engine.register_compaction_hook(accept)
    result = await asyncio.wait_for(engine.compact_context(session=stored.session, trigger="manual"), 5)

    assert isinstance(result, CompactionCandidate)
    assert not context.compaction_uncertain()
    record = (await binding.store.read(binding.key)).messages
    assert any("parser tests half written" in str(message.content) for message in record)
    assert [message.content for message in record] == [message.content for message in context.get_messages()]

"""Operation ownership across Runtime runs and mode changes."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.runtime.service import AgentRuntime, SessionModeSwitchError
from jiuwenswarm.runtime.session import RuntimeSessionCoordinator, SessionWorkKind
from jiuwenswarm.runtime.session.model import TeamExecutionDetails
from jiuwenswarm.runtime.session.operation_lock import acquire_operation, SessionOperationBusyError
from jiuwenswarm.server.runtime.session import lifecycle as lc, session_metadata as sm


@pytest.fixture
def operation_session(tmp_path, monkeypatch):
    root = tmp_path / "agent"
    sessions = root / "sessions"
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(lc, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(lc, "get_agent_root_dir", lambda: root)
    monkeypatch.setattr(sm, "get_agent_sessions_dir", lambda: sessions)
    directory = sessions / "shared-operation"
    directory.mkdir(parents=True)
    lc.atomic_json(directory / "metadata.json", dict(
        session_id=directory.name, channel_id="tui", user_id="test-user",
        project_id="default", mode="agent.work.normal", work_mode="work",
    ))
    yield directory.name
    sm._METADATA_QUEUE.join()
    sm.remove_session_metadata_cache(directory.name)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [
    SessionWorkKind.CHAT_UNARY, SessionWorkKind.CHAT_STREAM,
    SessionWorkKind.HEARTBEAT, SessionWorkKind.TEAM_BACKGROUND,
])
async def test_active_operation_excludes_mode_switch(operation_session, kind):
    sid = operation_session
    coordinator = RuntimeSessionCoordinator()
    await coordinator.register_session(sid, "tui")
    runtime = AgentRuntime(agent_manager=AsyncMock(), session_coordinator=coordinator)
    entered, finish = asyncio.Event(), asyncio.Event()

    async def work():
        entered.set()
        await finish.wait()
        return "saved"

    async def stream_work():
        entered.set()
        yield "output"
        await finish.wait()

    async def run():
        if kind is SessionWorkKind.CHAT_STREAM:
            return [item async for item in coordinator.run_stream(sid, "run", kind, stream_work)]
        return await coordinator.run_unary(
            sid, "run", kind, work,
            team=TeamExecutionDetails() if kind is SessionWorkKind.TEAM_BACKGROUND else None,
        )

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(SessionModeSwitchError) as busy:
            runtime.switch_session_mode(
                session_id=sid, target_mode="team.work.normal", expected_mode="agent.work.normal",
                channel_id="tui", user_id="test-user",
            )
        assert busy.value.code == "SESSION_BUSY"
        assert lc.raw_metadata(sid)["mode"] == "agent.work.normal"
    finally:
        finish.set()
        await asyncio.wait_for(task, 2)
        await coordinator.close()
    with acquire_operation("session", sid):
        pass


@pytest.mark.asyncio
async def test_waiting_interaction_keeps_lock_and_answer_uses_owner(operation_session):
    coordinator = RuntimeSessionCoordinator()
    sid = operation_session
    await coordinator.register_session(sid, "tui")
    await coordinator.run_unary(
        sid, "question", SessionWorkKind.CHAT_UNARY,
        lambda: asyncio.sleep(0, result="ask"), interaction_ids=lambda _: frozenset({"question"}),
    )
    with pytest.raises(SessionOperationBusyError):
        acquire_operation("session", sid)

    async def answer():
        with pytest.raises(SessionOperationBusyError):
            acquire_operation("session", sid)
        return "saved answer"

    assert await asyncio.wait_for(coordinator.deliver_control(sid, ("question",), answer), 2) == "saved answer"
    with acquire_operation("session", sid):
        pass
    await coordinator.close()


def test_execution_lock_is_nonblocking_across_processes(operation_session):
    program = """
import sys
from jiuwenswarm.runtime.session.operation_lock import acquire_operation, SessionOperationBusyError
try:
    with acquire_operation('session', sys.argv[1]):
        pass
except SessionOperationBusyError:
    sys.exit(23)
"""
    with acquire_operation("session", operation_session):
        result = subprocess.run([sys.executable, "-c", program, operation_session], timeout=10, capture_output=True)
        assert result.returncode == 23, result.stderr.decode()
    result = subprocess.run([sys.executable, "-c", program, operation_session], timeout=10, capture_output=True)
    assert result.returncode == 0, result.stderr.decode()


def test_run_lock_leaves_the_archive_and_delete_owner_lock_free(operation_session):
    """Archive and delete keep their own lock; a run must not make them wait."""
    owner = lc.resource_path("session", operation_session).parent.parent / "owners" / f"session_{operation_session}.json"
    with acquire_operation("session", operation_session):
        with lc.file_lock(owner, blocking=False):
            pass


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_public_chat_entry_holds_lock_until_executor_finishes(operation_session, monkeypatch, streaming):
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from tests.unit_tests.runtime.test_runtime_service import FakeAgentManager

    sid = operation_session
    manager = FakeAgentManager()
    runtime = AgentRuntime(agent_manager=manager, initializer=AsyncMock())
    entered, finish = asyncio.Event(), asyncio.Event()
    original_unary = manager.agent.process_message
    original_stream = manager.agent.process_message_stream

    async def unary(request):
        entered.set()
        await finish.wait()
        return await original_unary(request)

    async def stream(request):
        async for chunk in original_stream(request):
            yield chunk
        entered.set()
        await finish.wait()

    monkeypatch.setattr(manager.agent, "process_message", unary)
    monkeypatch.setattr(manager.agent, "process_message_stream", stream)
    request = AgentRequest(
        request_id="public-chat", session_id=sid, channel_id="tui", user_id="test-user",
        req_method=ReqMethod.CHAT_SEND, is_stream=streaming,
        params={"mode": "agent.work.normal", "query": "hello"},
    )

    async def run():
        if streaming:
            return [event async for event in runtime.stream(request, trigger_hook=False)]
        return await runtime.invoke(request, trigger_hook=False)

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        with pytest.raises(SessionModeSwitchError) as busy:
            runtime.switch_session_mode(
                session_id=sid, target_mode="agent.work.plan", expected_mode="agent.work.normal",
                channel_id="tui", user_id="test-user",
            )
        assert busy.value.code == "SESSION_BUSY"
        with pytest.raises(SessionOperationBusyError):
            acquire_operation("session", sid)
    finally:
        finish.set()
        await asyncio.wait_for(task, 3)
        await runtime.close()
    with acquire_operation("session", sid):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_before_start", [False, True])
async def test_native_leader_keeps_ownership_through_final_checkpoint(
    operation_session, monkeypatch, crash_before_start,
):
    from openjiuwen.agent_teams.harness import NativeHarness, HarnessState
    from openjiuwen.core.runner import Runner
    from openjiuwen.core.single_agent.schema.agent_card import AgentCard
    from openjiuwen.harness.factory import DeepAgentParts
    from openjiuwen.harness.schema.config import DeepAgentConfig
    from jiuwenswarm.runtime.context import set_runtime_context, reset_runtime_context
    from jiuwenswarm.runtime.session.native_operation import bind_native_operation

    class LeaderSpec:
        def resolve_parts(self, context=None):
            return DeepAgentParts(
                config=DeepAgentConfig(
                    card=AgentCard(id="operation-leader", name="leader", description="test leader"),
                    enable_task_loop=True,
                ),
                rails=[], tool_cards=[], tool_instances=[],
            )

    await Runner.start()
    harness = NativeHarness(LeaderSpec())
    coordinator = RuntimeSessionCoordinator()
    runtime = AgentRuntime(agent_manager=AsyncMock(), session_coordinator=coordinator)
    entered, finish, idle = asyncio.Event(), asyncio.Event(), asyncio.Event()
    consumer = None
    try:
        await harness.start()
        sid = harness.session_id
        await coordinator.register_session(sid, "tui")
        context_token = set_runtime_context(runtime, AsyncMock())
        try:
            await bind_native_operation(harness, sid, lambda request_id=None: None)
        finally:
            reset_runtime_context(context_token)
        monkeypatch.setattr(harness.react_agent, "invoke", AsyncMock(return_value={"output": "done"}))
        original_commit = harness.commit_conversation

        async def commit(session):
            entered.set()
            await finish.wait()
            await original_commit(session)

        async def state_changed(*, new):
            if new is HarnessState.IDLE:
                idle.set()

        monkeypatch.setattr(harness, "commit_conversation", commit)
        await harness.subscribe(on_state=state_changed)

        async def drain():
            return [chunk async for chunk in harness.outputs()]

        consumer = asyncio.create_task(drain())
        if crash_before_start:
            def fail_start(*args, **kwargs):
                raise RuntimeError("native failed before RUNNING")

            monkeypatch.setattr(harness, "_start_round", fail_start)
            with pytest.raises(RuntimeError, match="before RUNNING"):
                await harness.send("background continuation")
            await asyncio.sleep(0)
            with acquire_operation("session", sid):
                pass
            return
        # No response waiter or Runtime stream owns this autonomous send.
        await harness.send("background continuation")
        await asyncio.wait_for(entered.wait(), 3)
        assert harness.state is HarnessState.RUNNING
        with pytest.raises(SessionOperationBusyError):
            acquire_operation("session", sid)
        finish.set()
        await asyncio.wait_for(idle.wait(), 3)
        with acquire_operation("session", sid):
            pass
    finally:
        finish.set()
        await harness.stop()
        if consumer is not None:
            await consumer
        await coordinator.close()
        await Runner.stop()


@pytest.mark.asyncio
async def test_cancelled_stream_retains_lock_while_executor_closes(operation_session):
    coordinator = RuntimeSessionCoordinator()
    sid = operation_session
    await coordinator.register_session(sid, "tui")
    closing, finish = asyncio.Event(), asyncio.Event()

    async def executor():
        try:
            yield "output"
            await asyncio.Event().wait()
        finally:
            closing.set()
            await finish.wait()

    stream = coordinator.run_stream(sid, "cancel", SessionWorkKind.CHAT_STREAM, executor)
    assert await anext(stream) == "output"
    close_task = asyncio.create_task(stream.aclose())
    try:
        await asyncio.wait_for(closing.wait(), 2)
        with pytest.raises(SessionOperationBusyError):
            acquire_operation("session", sid)
    finally:
        finish.set()
        await asyncio.wait_for(close_task, 2)
        await coordinator.close()
    with acquire_operation("session", sid):
        pass


@pytest.mark.asyncio
async def test_closing_coordinator_releases_waiterless_round_reference(operation_session):
    sid = operation_session
    coordinator = RuntimeSessionCoordinator()
    snapshot = await coordinator.register_session(sid, "tui")
    coordinator.resume_waiting_for_input(sid, snapshot.generation)
    with pytest.raises(SessionOperationBusyError):
        acquire_operation("session", sid)
    await coordinator.close()
    with acquire_operation("session", sid):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_public_chat_does_not_start_during_a_mode_switch(operation_session, monkeypatch, streaming):
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from tests.unit_tests.runtime.test_runtime_service import FakeAgentManager

    sid = operation_session
    manager = FakeAgentManager()
    runtime = AgentRuntime(agent_manager=manager, initializer=AsyncMock())
    entered, release = threading.Event(), threading.Event()
    original_switch = runtime._switch_session_mode_locked

    def pause_then_call(function, *args, **kwargs):
        entered.set()
        if not release.wait(5):
            raise TimeoutError("test did not release mutation")
        return function(*args, **kwargs)

    monkeypatch.setattr(runtime, "_switch_session_mode_locked",
                        lambda **kwargs: pause_then_call(original_switch, **kwargs))
    mutation = asyncio.create_task(asyncio.to_thread(
        runtime.switch_session_mode, session_id=sid, target_mode="agent.code.normal",
        expected_mode="agent.work.normal", channel_id="tui", user_id="test-user",
    ))

    request = AgentRequest(
        request_id="chat-during-mutation", session_id=sid, channel_id="tui", user_id="test-user",
        req_method=ReqMethod.CHAT_SEND, is_stream=streaming,
        params={"mode": "agent.work.normal", "query": "hello"},
    )

    async def chat():
        if streaming:
            return [event async for event in runtime.stream(request, trigger_hook=False)]
        return await runtime.invoke(request, trigger_hook=False)

    task = None
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task = asyncio.create_task(chat())
        await asyncio.sleep(0.1)
        assert manager.agent_calls == []
        assert not task.done()
        release.set()
        await asyncio.wait_for(mutation, 3)
        await asyncio.wait_for(task, 3)
        assert manager.agent_calls
        assert all(call["mode"] == "code" and call["sub_mode"] == "normal" for call in manager.agent_calls)
        assert lc.raw_metadata(sid)["mode"] == "agent.code.normal"
    finally:
        release.set()
        await asyncio.gather(mutation, *([task] if task is not None else []), return_exceptions=True)
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("before, after", [
    ("agent.work.normal", "team.work.normal"),
    ("team.work.normal", "agent.work.normal"),
])
async def test_chat_waiting_behind_a_mode_switch_runs_as_the_committed_run_type(
    operation_session, monkeypatch, streaming, before, after,
):
    """The run type chosen before ownership is confirmed under it, not trusted."""
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.runtime.session.model import SessionExecutionState
    from tests.unit_tests.runtime.test_runtime_service import FakeAgentManager

    sid = operation_session
    sm.patch_session_metadata(sid, {"mode": before})
    sm._METADATA_QUEUE.join()
    manager = FakeAgentManager()
    runtime = AgentRuntime(agent_manager=manager, initializer=AsyncMock())
    entered, release = threading.Event(), threading.Event()
    original_switch = runtime._switch_session_mode_locked

    def paused_switch(**kwargs):
        entered.set()
        if not release.wait(5):
            raise TimeoutError("test did not release the mode switch")
        return original_switch(**kwargs)

    monkeypatch.setattr(runtime, "_switch_session_mode_locked", paused_switch)
    mutation = asyncio.create_task(asyncio.to_thread(
        runtime.switch_session_mode, session_id=sid, target_mode=after,
        expected_mode=before, channel_id="tui", user_id="test-user",
    ))
    request = AgentRequest(
        request_id="waiting-chat", session_id=sid, channel_id="tui", user_id="test-user",
        req_method=ReqMethod.CHAT_SEND, is_stream=streaming,
        params={"mode": before, "query": "hello"},
    )

    async def chat():
        if streaming:
            return [event async for event in runtime.stream(request, trigger_hook=False)]
        return await runtime.invoke(request, trigger_hook=False)

    task = None
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task = asyncio.create_task(chat())
        await asyncio.sleep(0.1)
        assert manager.agent_calls == []
        assert not task.done()
        release.set()
        await asyncio.wait_for(mutation, 3)
        await asyncio.wait_for(task, 5)
        executions = [
            item for item in runtime._session_coordinator.snapshot_session(sid).executions
            if item.request_id == "waiting-chat"
        ]
        team_kinds = {SessionWorkKind.TEAM_REQUEST, SessionWorkKind.TEAM_CONTROL, SessionWorkKind.TEAM_BACKGROUND}
        assert [item.work_kind in team_kinds for item in executions] == [
            before.startswith("team"), after.startswith("team"),
        ]
        assert executions[0].state is SessionExecutionState.CANCELLED
        assert executions[-1].state is not SessionExecutionState.FAILED
        assert manager.agent_calls
        assert lc.raw_metadata(sid)["mode"] == after
    finally:
        release.set()
        await asyncio.gather(mutation, *([task] if task is not None else []), return_exceptions=True)
        await runtime.close()
    with acquire_operation("session", sid):
        pass


@pytest.mark.asyncio
async def test_leader_confirms_the_mode_once_per_owned_operation(operation_session):
    """The mode cannot change under an owned operation, so later sends skip the check."""
    from jiuwenswarm.runtime.session.native_operation import NativeOperationBinding

    sid = operation_session
    coordinator = RuntimeSessionCoordinator()
    await coordinator.register_session(sid, channel_id="tui")
    class Harness:
        state = "idle"

    harness = Harness()
    binding = NativeOperationBinding(coordinator, sid, harness)
    checks = []
    binding.guard = lambda: checks.append("checked")
    try:
        binding.check()
        binding.check()
        assert checks == ["checked"]
        with pytest.raises(SessionOperationBusyError):
            acquire_operation("session", sid)
        await binding.on_state(new="idle")
        binding.check()
        assert checks == ["checked", "checked"]

        def changed():
            raise RuntimeError("session mode changed")

        await binding.on_state(new="idle")
        binding.guard = changed
        with pytest.raises(RuntimeError, match="mode changed"):
            binding.check()
        with acquire_operation("session", sid):
            pass
    finally:
        await binding.on_state(new="terminated")
        await coordinator.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_run_behind_repeated_mode_switches_stays_queued_until_one_settles(monkeypatch, streaming):
    """Mode switch then run: the run waits and starts; it never asks the user to retry."""
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.runtime.session.model import SessionRouteChangedError

    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())
    monkeypatch.setattr(runtime, "start", AsyncMock())
    switches = ["team.work.normal", "agent.work.normal", "team.code.normal", "agent.code.plan", "team.work.plan"]
    attempts = []

    async def routed(*_args, **_kwargs):
        attempts.append(len(attempts))
        if len(attempts) <= len(switches):
            raise SessionRouteChangedError(switches[len(attempts) - 1])
        return ["ran"]

    async def routed_stream(*_args, **_kwargs):
        for item in await routed():
            yield item

    monkeypatch.setattr(runtime, "_invoke_routed", routed)
    monkeypatch.setattr(runtime, "_stream_under_tenant", routed_stream)
    request = AgentRequest(
        request_id="queued", session_id="any", channel_id="tui", user_id="test-user",
        req_method=ReqMethod.CHAT_SEND, is_stream=streaming, params={"query": "hello"},
    )
    if streaming:
        result = [event async for event in runtime.stream(request, trigger_hook=False)]
    else:
        result = await runtime.invoke(request, trigger_hook=False)
    assert result == ["ran"]
    assert len(attempts) == len(switches) + 1

    # The same committed mode twice means nothing switched: surface the defect.
    attempts.clear()
    switches[:] = ["team.work.normal", "team.work.normal", "agent.work.normal"]
    with pytest.raises(SessionRouteChangedError):
        if streaming:
            [event async for event in runtime.stream(request, trigger_hook=False)]
        else:
            await runtime.invoke(request, trigger_hook=False)
    assert len(attempts) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_mode_switch_is_refused_while_an_earlier_run_is_still_starting(
    operation_session, streaming,
):
    """Run first, then mode switch: a run that has not started yet still keeps its mode."""
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from tests.unit_tests.runtime.test_runtime_service import FakeAgentManager

    sid = operation_session
    manager = FakeAgentManager()
    runtime = AgentRuntime(agent_manager=manager, initializer=AsyncMock())
    starting, proceed = asyncio.Event(), asyncio.Event()

    async def slow_start(_session_id, _work_kind):
        starting.set()
        await proceed.wait()
        return None

    runtime._session_coordinator.set_operation_admission(slow_start)
    request = AgentRequest(
        request_id="slow-to-start", session_id=sid, channel_id="tui", user_id="test-user",
        req_method=ReqMethod.CHAT_SEND, is_stream=streaming,
        params={"mode": "agent.work.normal", "query": "hello"},
    )

    async def chat():
        if streaming:
            return [event async for event in runtime.stream(request, trigger_hook=False)]
        return await runtime.invoke(request, trigger_hook=False)

    def switch():
        return runtime.switch_session_mode(
            session_id=sid, target_mode="team.work.normal", expected_mode="agent.work.normal",
            channel_id="tui", user_id="test-user",
        )

    task = asyncio.create_task(chat())
    try:
        await asyncio.wait_for(starting.wait(), 3)
        # The run does not own the Session yet; nothing has been executed.
        with acquire_operation("session", sid):
            pass
        assert manager.agent_calls == []
        with pytest.raises(SessionModeSwitchError) as busy:
            switch()
        assert busy.value.code == "SESSION_BUSY"
        assert lc.raw_metadata(sid)["mode"] == "agent.work.normal"
        proceed.set()
        await asyncio.wait_for(task, 5)
        assert manager.agent_calls
        # The run kept the single-agent route it was sent in.
        assert all(call["mode"] != "team" for call in manager.agent_calls)
        assert [
            item.work_kind for item in runtime._session_coordinator.snapshot_session(sid).executions
            if item.request_id == "slow-to-start"
        ] == [SessionWorkKind.CHAT_STREAM if streaming else SessionWorkKind.CHAT_UNARY]
        assert switch()["changed"] is True
    finally:
        proceed.set()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()

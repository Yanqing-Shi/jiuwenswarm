"""Explicit TUI mode switches keep one Session and its project index."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from unittest.mock import AsyncMock
from weakref import WeakValueDictionary

import psutil
import pytest

from gateway.channel_manager.tui.tui_connect import CLI_FORWARD_REQ_METHODS
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.service import AgentRuntime, SessionModeSwitchError
from jiuwenswarm.runtime.session import SessionWorkKind
from jiuwenswarm.server.runtime.session import lifecycle, session_metadata


@pytest.fixture(params=[("default_code", ""), ("project-123", "C:/projects/sample")])
def mode_session(tmp_path, monkeypatch, request):
    sessions = tmp_path / "sessions"
    project_id, project_dir = request.param
    monkeypatch.setattr(session_metadata, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(lifecycle, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(lifecycle, "get_agent_root_dir", lambda: tmp_path)
    session_metadata.init_session_metadata(
        session_id="one-session",
        channel_id="tui",
        user_id="user-1",
        mode="agent.code.normal",
        project_id=project_id,
        project_dir=project_dir,
        work_mode="code",
    )
    yield project_id, project_dir
    session_metadata._METADATA_QUEUE.join()
    session_metadata.remove_session_metadata_cache("one-session")


def test_mode_set_preserves_project_index_and_blocks_stale_write(mode_session, monkeypatch):
    from jiuwenswarm.server.runtime.session import session_history

    markers = []
    monkeypatch.setattr(session_history, "append_session_mode_changed", lambda **item: markers.append(item))
    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())
    stale = session_metadata.get_session_metadata(
        "one-session", cache_bust=True, enable_writeback=False,
    )

    project_id, project_dir = mode_session
    result = runtime.switch_session_mode(
        session_id="one-session",
        target_mode="agent.work.normal",
        expected_mode="agent.code.normal",
        channel_id="tui",
        user_id="user-1",
    )
    assert result == {
        "previous_mode": "agent.code.normal",
        "mode": "agent.work.normal",
        "work_mode": "code",
        "project_id": project_id,
        "changed": True,
    }
    assert len(markers) == 1
    assert markers[0]["mode"] == "agent.work.normal"

    stale["mode"] = "agent.code.normal"
    stale["title"] = "late title"
    session_metadata._write_metadata_sync(
        "one-session",
        stale,
        session_metadata._MetadataWriteOptions(
            merge_fields=frozenset({"mode", "title"}),
        ),
    )
    persisted = session_metadata.get_session_metadata(
        "one-session", cache_bust=True, enable_writeback=False,
    )
    assert persisted["mode"] == "agent.work.normal"
    assert persisted["title"] == "late title"
    assert persisted["project_id"] == project_id
    assert persisted["project_dir"] == project_dir
    assert persisted["work_mode"] == "code"

    # Identity recovery must not undo the revision guard for a partial write.
    session_metadata._write_metadata_sync(
        "one-session",
        {"mode": "agent.code.normal", "title": "partial write"},
    )
    persisted = session_metadata.get_session_metadata(
        "one-session", cache_bust=True, enable_writeback=False,
    )
    assert persisted["mode"] == "agent.work.normal"
    assert persisted["mode_revision"] == 1
    assert persisted["project_id"] == project_id
    assert persisted["project_dir"] == project_dir

    repeated = runtime.switch_session_mode(
        session_id="one-session",
        target_mode="agent.work.normal",
        expected_mode="agent.code.normal",
        channel_id="tui",
        user_id="user-1",
    )
    assert repeated["changed"] is False
    assert len(markers) == 1


def test_mode_set_rejects_stale_expectation_and_busy_session(mode_session):
    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())
    with pytest.raises(SessionModeSwitchError) as conflict:
        runtime.switch_session_mode(
            session_id="one-session",
            target_mode="team.work.normal",
            expected_mode="agent.work.normal",
            channel_id="tui",
            user_id="user-1",
        )
    assert conflict.value.code == "MODE_CONFLICT"

    runtime.begin_chat_request("one-session", "in-flight")
    with pytest.raises(SessionModeSwitchError) as busy:
        runtime.switch_session_mode(
            session_id="one-session",
            target_mode="team.work.normal",
            expected_mode="agent.code.normal",
            channel_id="tui",
            user_id="user-1",
        )
    assert busy.value.code == "SESSION_BUSY"
    runtime.end_chat_request("one-session", "in-flight")
    assert session_metadata.get_session_metadata(
        "one-session", cache_bust=True, enable_writeback=False,
    )["mode"] == "agent.code.normal"


def test_mode_set_rejects_another_user(mode_session):
    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())
    with pytest.raises(SessionModeSwitchError) as denied:
        runtime.switch_session_mode(
            session_id="one-session",
            target_mode="agent.work.normal",
            expected_mode="agent.code.normal",
            channel_id="tui",
            user_id="user-2",
        )
    assert denied.value.code == "NOT_FOUND"
    assert session_metadata.get_session_metadata(
        "one-session", cache_bust=True, enable_writeback=False,
    )["mode"] == "agent.code.normal"


def test_mode_get_reads_persisted_mode_and_checks_user(mode_session):
    project_id, _project_dir = mode_session
    assert AgentRuntime.get_session_mode(session_id="one-session", user_id="user-1") == {
        "mode": "agent.code.normal",
        "work_mode": "code",
        "project_id": project_id,
    }
    with pytest.raises(SessionModeSwitchError) as denied:
        AgentRuntime.get_session_mode(session_id="one-session", user_id="user-2")
    assert denied.value.code == "NOT_FOUND"


def test_mode_set_rejects_foreign_live_runtime_owner(mode_session):
    owner = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        with lifecycle.resource_lock("session", "one-session"):
            state = lifecycle.state("session", "one-session")
            state["runtime_owner"] = {
                "pid": owner.pid,
                "started_at": psutil.Process(owner.pid).create_time(),
            }
            lifecycle.save_locked("session", "one-session", state)

        runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())
        with pytest.raises(SessionModeSwitchError) as busy:
            runtime.switch_session_mode(
                session_id="one-session",
                target_mode="team.work.normal",
                expected_mode="agent.code.normal",
                channel_id="tui",
                user_id="user-1",
            )
        assert busy.value.code == "SESSION_BUSY"
        assert session_metadata.get_session_metadata(
            "one-session", cache_bust=True, enable_writeback=False,
        )["mode"] == "agent.code.normal"
    finally:
        owner.terminate()
        owner.wait(timeout=5)


def test_uncommitted_cross_profile_chat_uses_committed_mode(mode_session, monkeypatch):
    from jiuwenswarm.server.runtime.session import session_history

    monkeypatch.setattr(session_history, "append_session_mode_changed", lambda **item: None)
    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())
    runtime.switch_session_mode(
        session_id="one-session",
        target_mode="agent.work.normal",
        expected_mode="agent.code.normal",
        channel_id="tui",
        user_id="user-1",
    )
    request = AgentRequest(
        request_id="late-chat",
        channel_id="tui",
        session_id="one-session",
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": "agent.code.normal", "work_mode": "code", "query": "hello"},
    )
    normalized = AgentRuntime.normalize_request(request)
    assert normalized.params["mode"] == "agent.work.normal"
    assert normalized.params["work_mode"] == "code"


@pytest.mark.asyncio
async def test_cold_chat_reclassifies_after_remote_switch_before_claim(mode_session, monkeypatch):
    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())
    request = AgentRequest(
        request_id="cold-chat",
        channel_id="tui",
        session_id="one-session",
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": "agent.code.normal", "query": "hello"},
    )
    assert AgentRuntime.session_work_kind(AgentRuntime.normalize_request(request)) is SessionWorkKind.CHAT_UNARY

    async def claim_after_remote_commit(_request):
        session_metadata.compare_and_set_session_mode(
            "one-session", "team.work.normal", "agent.code.normal", user_id="user-1",
        )
        lifecycle.claim_runtime("one-session")

    monkeypatch.setattr(runtime, "_ensure_session_registered", claim_after_remote_commit)
    normalized = await runtime._normalize_tui_chat_after_claim(request)
    assert normalized.params["mode"] == "team.work.normal"
    assert AgentRuntime.session_work_kind(normalized) is SessionWorkKind.TEAM_REQUEST


def test_mode_set_is_a_forwarded_protocol_method():
    assert ReqMethod("mode.set") is ReqMethod.MODE_SET
    assert ReqMethod("mode.get") is ReqMethod.MODE_GET
    assert "mode.set" in CLI_FORWARD_REQ_METHODS
    assert "mode.get" in CLI_FORWARD_REQ_METHODS


@pytest.mark.asyncio
async def test_mode_get_waits_for_timed_out_mode_sets_during_runtime_start(monkeypatch):
    from jiuwenswarm.server import agent_ws_server

    started = asyncio.Event()
    ready = asyncio.Event()
    stored_mode = "agent.code.normal"
    read_modes = []
    completed_sets = []
    responses = []

    class Runtime:
        async def start(self):
            started.set()
            await ready.wait()

        def switch_session_mode(self, *, target_mode, **_kwargs):
            nonlocal stored_mode
            changed = stored_mode != target_mode
            stored_mode = target_mode
            completed_sets.append(changed)
            return {"mode": stored_mode, "changed": changed}

    def get_session_mode(*, session_id, user_id):
        assert (session_id, user_id) == ("one-session", "user-1")
        assert completed_sets == [True, False]
        read_modes.append(stored_mode)
        return {"mode": stored_mode}

    async def send_wire(_ws, payload):
        responses.append(payload)

    server = object.__new__(agent_ws_server.AgentWebSocketServer)
    server._mode_request_locks = WeakValueDictionary()
    monkeypatch.setattr(server, "_execution_runtime", lambda: Runtime())
    monkeypatch.setattr(AgentRuntime, "get_session_mode", staticmethod(get_session_mode))
    monkeypatch.setattr(agent_ws_server, "send_wire_payload", send_wire)
    send_lock = asyncio.Lock()

    def request(request_id, method):
        return AgentRequest(
            request_id=request_id,
            channel_id="tui",
            session_id="one-session",
            user_id="user-1",
            req_method=method,
            params={
                "session_id": "one-session",
                "mode": "team.work.normal",
                "expected_mode": "agent.code.normal",
            },
        )

    first = asyncio.create_task(server._handle_mode_set(None, request("set-1", ReqMethod.MODE_SET), send_lock))
    await asyncio.wait_for(started.wait(), timeout=1)
    second = asyncio.create_task(server._handle_mode_set(None, request("set-2", ReqMethod.MODE_SET), send_lock))
    await asyncio.sleep(0)
    read = asyncio.create_task(server._handle_mode_get(None, request("get", ReqMethod.MODE_GET), send_lock))
    await asyncio.sleep(0)
    assert not read.done()
    assert read_modes == []

    ready.set()
    await asyncio.wait_for(asyncio.gather(first, second, read), timeout=1)
    assert read_modes == ["team.work.normal"]
    assert len(responses) == 3

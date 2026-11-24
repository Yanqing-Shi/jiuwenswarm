"""mode.route asks the local mode router where a prompt should run and commits nothing."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock
from weakref import WeakValueDictionary

import httpx
import pytest

from jiuwenswarm.common.mode_router_config import mode_router_enabled, mode_router_settings
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.service import AgentRuntime, SessionModeSwitchError
from jiuwenswarm.server.runtime.session import (
    lifecycle,
    mode_router,
    mode_router_tasks,
    session_history,
    session_metadata,
)

ROUTER_URL = "http://127.0.0.1:19100"


@pytest.fixture(params=sorted(session_metadata.MODE_SWITCH_CHANNELS))
def session_channel(request):
    """Every mode-switch channel shares the mode.route logic."""
    return request.param


@pytest.fixture
def route_session(session_channel, tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(session_metadata, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(lifecycle, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(lifecycle, "get_agent_root_dir", lambda: tmp_path)
    monkeypatch.setenv("MODE_ROUTER_URL", ROUTER_URL)
    session_metadata.init_session_metadata(
        session_id="one-session",
        channel_id=session_channel,
        user_id="user-1",
        mode="agent.code.normal",
        project_id="default_code",
        project_dir="",
        work_mode="code",
    )
    yield sessions / "one-session"
    session_metadata._METADATA_QUEUE.join()
    session_metadata.remove_session_metadata_cache("one-session")
    # A routed prompt turns the task journal on for the process; later tests start without it.
    mode_router_tasks._activated = False
    mode_router_tasks._item_limit = mode_router_tasks.DEFAULT_HISTORY_ITEMS
    mode_router_tasks._COVERED.clear()


def _use_router(monkeypatch, handler):
    """Answer router calls with ``handler`` and return the list of request bodies it saw."""
    seen = []

    async def record(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        assert request.url == f"{ROUTER_URL}/v1/route"
        result = handler(request)
        return await result if asyncio.iscoroutine(result) else result

    monkeypatch.setattr(
        mode_router, "_build_client",
        lambda settings: httpx.AsyncClient(
            transport=httpx.MockTransport(record), timeout=settings.timeout_seconds,
        ),
    )
    return seen


def _answer(mode, **extra):
    return lambda request: httpx.Response(200, json={"mode": mode, **extra})


async def _route(channel_id, *, query="build a website", user_id="user-1", session_id="one-session",
                 current_mode=None):
    context = AgentRuntime.session_mode_route_context(
        session_id=session_id, channel_id=channel_id, user_id=user_id, current_mode=current_mode,
    )
    return await AgentRuntime.route_session_mode(context, query=query, request_id="route-1")


def _write_history(session_dir, records):
    session_dir.mkdir(parents=True, exist_ok=True)
    with (session_dir / "history.jsonl").open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def _persisted_mode():
    metadata = session_metadata.get_session_metadata(
        "one-session", cache_bust=True, enable_writeback=False,
    )
    return metadata["mode"], metadata.get("mode_revision")


@pytest.mark.parametrize("current,route,expected", [
    ("agent.work.normal", "team", "team.work.normal"),
    ("agent.code.normal", "team", "team.code.normal"),
    ("agent.code.plan", "team", "team.code.plan"),
    ("team.work.plan", "agent", "agent.work.plan"),
    ("team.code.normal", "team", "team.code.normal"),
    ("team.design", "agent", None),
    ("agent.work.normal", "auto", None),
    (None, "team", None),
])
def test_route_target_keeps_the_profile_and_plan_state(current, route, expected):
    assert session_metadata.switch_target_for_route(current, route) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("answer,expected_mode,switch_needed", [
    ("team", "team.code.normal", True),
    ("agent", "agent.code.normal", False),
])
async def test_mode_route_names_the_target_and_commits_nothing(
    route_session, session_channel, monkeypatch, answer, expected_mode, switch_needed,
):
    seen = _use_router(monkeypatch, _answer(answer, confidence=0.8, reason="needs several workers"))
    before = _persisted_mode()

    decision = await _route(session_channel)

    assert decision == {
        "route": answer,
        "mode": expected_mode,
        "current_mode": "agent.code.normal",
        "switch_needed": switch_needed,
        "confidence": 0.8,
        "reason": "needs several workers",
    }
    assert seen == [{
        "request_id": "route-1",
        "session_id": "one-session",
        "query": "build a website",
        "current_mode": "agent",
        "work_mode": "code",
        "history": [],
    }]
    assert _persisted_mode() == before


def _five_tasks():
    """Five completed tasks, the fourth in Team mode, with the event records a run leaves between."""
    records = []
    for index in range(1, 6):
        request_id = f"req-{index}"
        mode = "team.code.normal" if index == 4 else "agent.code.normal"
        records.append({"role": "user", "request_id": request_id, "content": f"prompt {index}", "mode": mode})
        records.append({"event_type": "chat.llm_call_start", "request_id": request_id})
        records.append({
            "role": "assistant", "event_type": "chat.reasoning", "request_id": request_id,
            "content": "thinking",
        })
        records.append({
            "role": "leader" if index == 4 else "assistant", "event_type": "chat.final",
            "request_id": request_id, "content": f"answer {index}" + ("!" * 5000 if index == 5 else ""),
            "mode": mode,
        })
        records.append({
            "role": "assistant", "event_type": "chat.request_completed", "request_id": request_id,
            "id": f"{request_id}:request_completed", "status": "success", "content": "",
        })
    return records


@pytest.mark.asyncio
async def test_mode_route_sends_every_earlier_task_in_order_and_in_full(
    route_session, session_channel, monkeypatch,
):
    """Prompt 1, answer 1, ... up to the task that just ended; the new prompt is the query."""
    _write_history(route_session, _five_tasks())
    seen = _use_router(monkeypatch, _answer("agent"))

    await _route(session_channel, query="prompt 6 " + "?" * 9000)

    assert [(item["role"], item["content"][:8], item["mode"]) for item in seen[0]["history"]] == [
        (role, f"{text} {index}", "team" if index == 4 else "agent")
        for index in range(1, 6)
        for role, text in (("user", "prompt"), ("assistant", "answer"))
    ]
    assert seen[0]["history"][-1]["content"] == "answer 5" + "!" * 5000
    assert seen[0]["query"] == "prompt 6 " + "?" * 9000
    # Text only: neither the system prompt nor the tools are part of the request.
    assert set(seen[0]) == {"request_id", "session_id", "query", "current_mode", "work_mode", "history"}


@pytest.mark.asyncio
async def test_mode_route_applies_the_optional_caps(route_session, session_channel, monkeypatch):
    from jiuwenswarm.common import mode_router_config

    capped = mode_router_config.ModeRouterSettings(
        url=ROUTER_URL, history_items=3, history_chars=100, query_chars=10,
    )
    monkeypatch.setattr(mode_router_config, "mode_router_settings", lambda config=None: capped)
    _write_history(route_session, _five_tasks())
    seen = _use_router(monkeypatch, _answer("agent"))

    await _route(session_channel, query="a prompt longer than ten characters")

    # The latest three items: the earliest go out, also from the middle of a task.
    history = seen[0]["history"]
    assert [(item["role"], item["content"][:8], item["mode"]) for item in history] == [
        ("assistant", "answer 4", "team"), ("user", "prompt 5", "agent"), ("assistant", "answer 5", "agent"),
    ]
    assert history[-1]["content"] == ("answer 5" + "!" * 5000)[:100] + " [truncated]"
    assert seen[0]["query"] == "a prompt l [truncated]"


@pytest.mark.asyncio
async def test_mode_route_keeps_a_limited_number_of_items_by_default(route_session, session_channel, monkeypatch):
    """Twenty items; the new prompt is not one of them."""
    records = []
    for index in range(1, 14):
        records.append({"role": "user", "request_id": f"r{index}", "content": f"prompt {index}"})
        records.append({
            "role": "assistant", "event_type": "chat.final", "request_id": f"r{index}", "content": f"answer {index}",
        })
    _write_history(route_session, records)
    seen = _use_router(monkeypatch, _answer("agent"))

    await _route(session_channel, query="prompt 14")

    assert mode_router_settings().history_items == 20
    assert [item["content"] for item in seen[0]["history"]] == [
        f"{kind} {index}" for index in range(4, 14) for kind in ("prompt", "answer")
    ]
    assert seen[0]["query"] == "prompt 14"


@pytest.mark.asyncio
async def test_mode_route_sends_typed_prompts_and_one_answer_per_run(route_session, session_channel, monkeypatch):
    """Prompts sent into a run are flagged and kept; interim, member and stray answers are left out."""
    done = lambda request_id, status="success": {  # noqa: E731
        "role": "assistant", "event_type": "chat.request_completed", "request_id": request_id,
        "id": f"{request_id}:request_completed", "status": status, "content": "",
    }
    _write_history(route_session, [
        {"role": "user", "request_id": "t1", "content": "team prompt", "mode": "team.code.normal"},
        {"role": "teammate", "event_type": "chat.final", "request_id": "t1", "content": "member answer"},
        {"role": "leader", "event_type": "chat.final", "request_id": "t1", "content": "interim answer"},
        {
            "role": "user", "request_id": "t2", "content": "message to the Leader", "mode": "team.code.normal",
            "sent_during_team_round": True,
        },
        done("t2"),
        {"role": "leader", "event_type": "chat.final", "request_id": "t1", "content": "team answer"},
        {"event_type": "session.mode_changed", "request_id": "", "mode": "agent.code.normal"},
        done("t1"),
        {"role": "user", "request_id": "a1", "content": "stopped prompt", "mode": "agent.code.normal"},
        {
            "role": "user", "request_id": "s1", "content": "send now", "mode": "agent.code.normal",
            "is_supplemental_input": True,
        },
        done("a1", "cancelled"),
        # A request with an empty prompt (the page's read-aloud call) still answers.
        {"role": "user", "request_id": "x1", "content": "", "mode": "agent.code.normal"},
        {"role": "assistant", "event_type": "chat.final", "request_id": "x1", "content": "stray answer"},
        done("x1"),
    ])
    seen = _use_router(monkeypatch, _answer("agent"))

    await _route(session_channel)

    assert seen[0]["history"] == [
        {"role": "user", "content": "team prompt", "mode": "team"},
        {"role": "user", "content": "message to the Leader", "mode": "team", "during_run": True},
        {"role": "assistant", "content": "team answer", "mode": "team"},
        {"role": "user", "content": "stopped prompt", "mode": "agent"},
        {"role": "user", "content": "send now", "mode": "agent", "during_run": True},
    ]


@pytest.mark.asyncio
async def test_mode_route_survives_unreadable_history(route_session, session_channel, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("history is unreadable")

    monkeypatch.setattr(mode_router, "task_history", broken)
    seen = _use_router(monkeypatch, _answer("team"))

    decision = await _route(session_channel)

    assert decision["mode"] == "team.code.normal"
    assert seen[0]["history"] == []


@pytest.mark.asyncio
async def test_mode_route_for_a_new_conversation_uses_the_mode_the_client_names(
    session_channel, monkeypatch,
):
    monkeypatch.setenv("MODE_ROUTER_URL", ROUTER_URL)
    seen = _use_router(monkeypatch, _answer("team"))

    decision = await _route(session_channel, session_id="", current_mode="agent.work.plan")

    assert decision["mode"] == "team.work.plan"
    assert decision["current_mode"] == "agent.work.plan"
    assert decision["switch_needed"] is True
    assert seen[0]["session_id"] is None
    assert seen[0]["current_mode"] == "agent"
    assert seen[0]["work_mode"] == "work"
    assert seen[0]["history"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("current_mode", [None, "", "agent", "team.design", "agent.work.bogus", 3])
async def test_mode_route_for_a_new_conversation_needs_a_switchable_mode(
    session_channel, monkeypatch, current_mode,
):
    monkeypatch.setenv("MODE_ROUTER_URL", ROUTER_URL)
    seen = _use_router(monkeypatch, _answer("team"))

    with pytest.raises(SessionModeSwitchError) as refused:
        await _route(session_channel, session_id="", current_mode=current_mode)

    assert refused.value.code == "BAD_REQUEST"
    assert seen == []


def _raise(error):
    def handler(request):
        raise error

    return handler


@pytest.mark.asyncio
@pytest.mark.parametrize("handler,code", [
    (_raise(httpx.ConnectError("refused")), "ROUTER_UNAVAILABLE"),
    (_raise(httpx.ReadTimeout("slow")), "ROUTER_UNAVAILABLE"),
    (lambda request: httpx.Response(500, json={"mode": "team"}), "ROUTER_UNAVAILABLE"),
    (lambda request: httpx.Response(200, text="not json"), "ROUTER_BAD_ANSWER"),
    (lambda request: httpx.Response(200, json=["team"]), "ROUTER_BAD_ANSWER"),
    (_answer("auto"), "ROUTER_BAD_ANSWER"),
    (_answer("team.work.normal"), "ROUTER_BAD_ANSWER"),
    (_answer(None), "ROUTER_BAD_ANSWER"),
])
async def test_mode_route_reports_a_router_that_gives_no_usable_answer(
    route_session, session_channel, monkeypatch, handler, code,
):
    _use_router(monkeypatch, handler)
    before = _persisted_mode()

    with pytest.raises(SessionModeSwitchError) as refused:
        await _route(session_channel)

    assert refused.value.code == code
    assert _persisted_mode() == before


@pytest.mark.asyncio
async def test_mode_route_is_refused_while_no_router_is_configured(
    route_session, session_channel, monkeypatch,
):
    monkeypatch.delenv("MODE_ROUTER_URL")
    seen = _use_router(monkeypatch, _answer("team"))

    with pytest.raises(SessionModeSwitchError) as refused:
        await _route(session_channel)

    assert refused.value.code == "ROUTER_DISABLED"
    assert seen == []


@pytest.mark.asyncio
async def test_mode_route_answers_only_the_recorded_owner(route_session, session_channel, monkeypatch):
    seen = _use_router(monkeypatch, _answer("team"))
    # Another channel never sees the Session, whether or not it can switch modes itself.
    other_channel = "feishu"

    for channel_id, user_id in ((session_channel, "user-2"), (other_channel, "user-1")):
        with pytest.raises(SessionModeSwitchError) as refused:
            await _route(channel_id, user_id=user_id)
        assert refused.value.code == "NOT_FOUND"
    with pytest.raises(SessionModeSwitchError) as missing:
        await _route(session_channel, session_id="no-such-session")
    assert missing.value.code == "NOT_FOUND"
    assert seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bound_fields", [
    {"agent_group_name": "research-experts"},
    {"team_group_identity": {"name": "research-experts"}},
    {"session_runtime_binding": {"agent": "claude"}},
    {"cron_id": "daily-report"},
    {"execution_profile": "app_role", "role_instructions": "You are the village chief."},
])
async def test_mode_route_skips_sessions_with_a_bound_executor(
    route_session, session_channel, monkeypatch, bound_fields,
):
    """A Session that cannot switch is not sent to the router."""
    metadata_path = route_session / "metadata.json"
    stored = json.loads(metadata_path.read_text(encoding="utf-8"))
    stored.update(bound_fields)
    metadata_path.write_text(json.dumps(stored), encoding="utf-8")
    session_metadata.remove_session_metadata_cache("one-session")
    seen = _use_router(monkeypatch, _answer("team"))

    with pytest.raises(SessionModeSwitchError) as refused:
        await _route(session_channel)

    assert refused.value.code == "BAD_REQUEST"
    assert seen == []


def _mode_server(monkeypatch, runtime=None):
    from jiuwenswarm.server import agent_ws_server

    responses = []

    async def send_wire(_ws, payload):
        responses.append(payload)

    server = object.__new__(agent_ws_server.AgentWebSocketServer)
    server._mode_request_locks = WeakValueDictionary()
    monkeypatch.setattr(agent_ws_server, "send_wire_payload", send_wire)
    monkeypatch.setattr(
        agent_ws_server, "encode_agent_response_for_wire",
        lambda response, response_id=None: response,
    )
    monkeypatch.setattr(
        server, "_execution_runtime",
        (lambda: runtime) if runtime is not None
        else (lambda: pytest.fail("mode.route must not start the Runtime")),
    )
    return server, responses


def _route_request(channel_id, params, *, session_id="one-session", request_id="route-1"):
    return AgentRequest(
        request_id=request_id,
        channel_id=channel_id,
        session_id=session_id,
        user_id="user-1",
        req_method=ReqMethod.MODE_ROUTE,
        params=params,
    )


@pytest.mark.asyncio
async def test_mode_route_handler_answers_with_the_decision(route_session, session_channel, monkeypatch):
    seen = _use_router(monkeypatch, _answer("team"))
    server, responses = _mode_server(monkeypatch)

    await server._handle_mode_route(
        None,
        _route_request(session_channel, {"session_id": "one-session", "query": "build a website"}),
        asyncio.Lock(),
    )

    assert len(responses) == 1
    assert responses[0].ok is True
    assert responses[0].payload["mode"] == "team.code.normal"
    assert responses[0].payload["switch_needed"] is True
    assert seen[0]["request_id"] == "route-1"


@pytest.mark.asyncio
async def test_mode_route_handler_routes_a_conversation_that_is_not_saved_yet(
    session_channel, monkeypatch,
):
    monkeypatch.setenv("MODE_ROUTER_URL", ROUTER_URL)
    seen = _use_router(monkeypatch, _answer("team"))
    server, responses = _mode_server(monkeypatch)

    await server._handle_mode_route(
        None,
        # The Gateway gives a frame without session_id a routing session of
        # its own; only params name a saved conversation.
        _route_request(
            session_channel, {"query": "build a website", "current_mode": "agent.work.normal"},
            session_id="sess_assigned_by_gateway",
        ),
        asyncio.Lock(),
    )

    assert responses[0].ok is True
    assert responses[0].payload["mode"] == "team.work.normal"
    assert seen[0]["session_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("channel_id,params,code", [
    ("feishu", {"session_id": "one-session", "query": "hello"}, "BAD_REQUEST"),
    ("web", {"session_id": "one-session"}, "BAD_REQUEST"),
    ("web", {"session_id": "one-session", "query": "  "}, "BAD_REQUEST"),
    ("web", {"session_id": "one-session", "query": 3}, "BAD_REQUEST"),
    ("tui", {"session_id": "other-session", "query": "hello"}, "BAD_REQUEST"),
    # Without a session_id the request is a new conversation and must name its mode.
    ("web", {"query": "hello"}, "BAD_REQUEST"),
])
async def test_mode_route_handler_rejects_malformed_requests(monkeypatch, channel_id, params, code):
    monkeypatch.setenv("MODE_ROUTER_URL", ROUTER_URL)
    seen = _use_router(monkeypatch, _answer("team"))
    server, responses = _mode_server(monkeypatch)

    await server._handle_mode_route(None, _route_request(channel_id, params), asyncio.Lock())

    assert len(responses) == 1
    assert responses[0].ok is False
    assert responses[0].payload["code"] == code
    assert seen == []


@pytest.mark.asyncio
async def test_mode_route_handler_reports_router_codes(route_session, session_channel, monkeypatch):
    _use_router(monkeypatch, _raise(httpx.ConnectError("refused")))
    server, responses = _mode_server(monkeypatch)

    await server._handle_mode_route(
        None,
        _route_request(session_channel, {"session_id": "one-session", "query": "hello"}),
        asyncio.Lock(),
    )

    assert responses[0].ok is False
    assert responses[0].payload["code"] == "ROUTER_UNAVAILABLE"


@pytest.mark.asyncio
async def test_a_mode_set_is_not_held_back_by_a_route_waiting_on_the_router(
    route_session, session_channel, monkeypatch,
):
    """The router call runs outside the Session's mode lock."""
    monkeypatch.setattr(session_history, "append_session_mode_changed", lambda **item: None)
    router_called = asyncio.Event()
    release_router = asyncio.Event()

    async def slow(request):
        router_called.set()
        await release_router.wait()
        return httpx.Response(200, json={"mode": "team"})

    _use_router(monkeypatch, slow)
    runtime = AgentRuntime(agent_manager=AsyncMock(), initializer=AsyncMock())
    server, responses = _mode_server(monkeypatch, runtime)

    route = asyncio.create_task(server._handle_mode_route(
        None,
        _route_request(session_channel, {"session_id": "one-session", "query": "hello"}),
        asyncio.Lock(),
    ))
    await asyncio.wait_for(router_called.wait(), timeout=5)
    await asyncio.wait_for(server._handle_mode_set(
        None,
        AgentRequest(
            request_id="set-1", channel_id=session_channel, session_id="one-session", user_id="user-1",
            req_method=ReqMethod.MODE_SET,
            params={
                "session_id": "one-session", "mode": "team.code.normal",
                "expected_mode": "agent.code.normal",
            },
        ),
        asyncio.Lock(),
    ), timeout=5)

    assert [response.request_id for response in responses] == ["set-1"]
    assert responses[0].ok is True
    release_router.set()
    await asyncio.wait_for(route, timeout=5)
    # The route read the mode before the switch; the caller sees that from current_mode.
    assert responses[1].payload["current_mode"] == "agent.code.normal"
    assert responses[1].payload["switch_needed"] is True


@pytest.mark.parametrize("config,environment,enabled", [
    ({}, None, False),
    ({"mode_router": {"url": ""}}, None, False),
    ({"mode_router": {"url": "${MODE_ROUTER_URL:-}"}}, None, False),
    ({"mode_router": {"url": "http://127.0.0.1:19100/"}}, None, True),
    ({"mode_router": "http://127.0.0.1:19100"}, None, False),
    ({}, "http://127.0.0.1:19100", True),
    ({"mode_router": {"url": "http://127.0.0.1:1"}}, "http://127.0.0.1:19100", True),
    ({}, "127.0.0.1:19100", False),
])
def test_the_router_is_on_only_when_an_address_is_set(monkeypatch, config, environment, enabled):
    if environment is None:
        monkeypatch.delenv("MODE_ROUTER_URL", raising=False)
    else:
        monkeypatch.setenv("MODE_ROUTER_URL", environment)

    assert mode_router_enabled(config) is enabled
    if enabled:
        assert mode_router_settings(config).url == "http://127.0.0.1:19100"


def test_router_settings_fall_back_to_defaults_for_unusable_values(monkeypatch):
    monkeypatch.delenv("MODE_ROUTER_URL", raising=False)

    settings = mode_router_settings({"mode_router": {
        "url": "http://127.0.0.1:19100",
        "timeout_seconds": "fast",
        "history_items": -4,
        "history_chars": True,
        "query_chars": "500",
    }})

    assert settings.timeout_seconds == 3.0
    assert settings.history_items == 0
    assert settings.history_chars == 0
    assert settings.query_chars == 500


def test_texts_are_sent_in_full_unless_a_cap_is_set(monkeypatch):
    monkeypatch.delenv("MODE_ROUTER_URL", raising=False)

    settings = mode_router_settings({"mode_router": {"url": "http://127.0.0.1:19100"}})

    assert (settings.history_items, settings.history_chars, settings.query_chars) == (20, 0, 0)
    assert mode_router.cut_text("x" * 50_000, settings.query_chars) == "x" * 50_000
    assert mode_router.cut_text("x" * 12, 10) == "x" * 10 + " [truncated]"

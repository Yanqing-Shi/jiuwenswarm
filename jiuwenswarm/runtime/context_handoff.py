# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Single-session conversation ownership for work Agent and native Team Leader.

Metadata holds identity and the small recovery journal; messages stay in each
executor's own checkpoint. The gate is process-local like the Session runtime.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from weakref import WeakValueDictionary, ref

from agent_gateway.common.mode_matrix import deprecate_mode

METADATA_KEY = "conversation_handoff"
_CODE_AGENT_MODES = {"agent.code.normal", "agent.code.plan"}
_CODE_TEAM_MODES = {"team.code.normal", "team.code.plan"}
_WORK_AGENT_MODES = {"agent.work.normal", "agent.work.plan"}
_WORK_TEAM_MODES = {"team.work.normal", "team.work.plan"}
_CONTROLLERS: WeakValueDictionary[str, "ConversationHandoff"] = WeakValueDictionary()
_TEAM_ATTEMPT: ContextVar["HandoffAttempt | None"] = ContextVar("team_handoff_attempt", default=None)


class ContextHandoffError(RuntimeError):
    """A failed switch never admits the new query."""

    def __init__(self, code: str, *, retryable: bool = True):
        self.code = code
        self.retryable = retryable
        super().__init__(f"暂不可切换对话执行者：{code}")

    def payload(self) -> dict[str, Any]:
        return {"event_type": "chat.error", "error": str(self),
                "code": self.code, "retryable": self.retryable}


def request_kind(request: Any) -> str | None:
    params = request.params or {}
    from jiuwenswarm.runtime.session.model import TEAM_ATTACH_ONLY_PARAM

    if params.get(TEAM_ATTACH_ONLY_PARAM) is True:
        return None
    query = params.get("query")
    if isinstance(query, str) and query.lstrip().startswith("/"):
        return None
    canonical_mode = params.get("mode")
    explicit_modes = _WORK_AGENT_MODES | _WORK_TEAM_MODES | _CODE_AGENT_MODES | _CODE_TEAM_MODES
    # The canonical mode takes precedence over a session's stored profile.
    # Keep the profile gate for legacy modes such as bare "agent" or "team".
    if params.get("work_mode") == "code" and canonical_mode not in explicit_modes:
        return None
    mode = deprecate_mode(canonical_mode or "agent")
    if mode in _WORK_AGENT_MODES or canonical_mode in _CODE_AGENT_MODES:
        return "agent"
    if mode in _WORK_TEAM_MODES or canonical_mode in _CODE_TEAM_MODES:
        return "team"
    return None


def request_profile(request: Any) -> str:
    mode = (request.params or {}).get("mode")
    return "code" if mode in _CODE_AGENT_MODES | _CODE_TEAM_MODES else "work"


def get_handoff(session_id: str) -> "ConversationHandoff":
    return _CONTROLLERS.setdefault(session_id, ConversationHandoff(session_id))


@dataclass
class LiveConversation:
    agent_ref: Any
    session_ref: Any
    identity: dict[str, str]
    team_session_ref: Any = None

    @property
    def agent(self):
        return self.agent_ref()

    @property
    def session(self):
        return self.session_ref()

    @property
    def team_session(self):
        return self.team_session_ref() if self.team_session_ref else None


def _require_no_team_resume(team_session: Any) -> None:
    if team_session is not None:
        teams = team_session.get_state("teams") or {}
        if any(value.get("pending_resume") is not None for value in teams.values()):
            raise ContextHandoffError("context_handoff_pending_team_resume")


async def _require_quiescent(session: Any, agent: Any = None) -> None:
    from openjiuwen.core.single_agent.interrupt.state import INTERRUPTION_KEY, pending_interrupts
    from openjiuwen.harness.schema.state import DeepAgentState

    if agent is not None and not agent.conversation_is_idle():
        raise ContextHandoffError("context_handoff_busy")
    state = agent.load_state(session) if agent is not None else DeepAgentState.from_session_dict(
        session.get_state("deepagent") or {}
    )
    tasks = state.task_plan.tasks if state.task_plan else []
    if state.pending_follow_ups or any(task.status in {"pending", "in_progress"} for task in tasks):
        raise ContextHandoffError("context_handoff_pending_work")
    if pending_interrupts(session.get_state(INTERRUPTION_KEY)):
        raise ContextHandoffError("context_handoff_pending_interaction")
    goal = session.get_state("harness.goal.record") or {}
    if goal.get("status") in {"active", "paused"}:
        raise ContextHandoffError("context_handoff_pending_goal")
    if agent is not None:
        peers_idle = getattr(agent, "conversation_peers_are_idle", None)
        if peers_idle is not None and not await peers_idle(session_id=session.get_session_id()):
            raise ContextHandoffError("context_handoff_team_pending_work")


class ConversationHandoff:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.lock = asyncio.Lock()
        self.owner: dict[str, str] | None = None
        self.pending: dict[str, Any] | None = None
        self.live: dict[str, LiveConversation] = {}
        self.frozen = False
        self.input_pending: str | None = None
        self.preparing_kind: str | None = None

    def _read(self) -> dict[str, Any]:
        from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

        return get_session_metadata(self.session_id, cache_bust=True).get(METADATA_KEY) or {}

    def _write(self, owner, pending) -> None:
        from jiuwenswarm.server.runtime.session.session_metadata import patch_session_metadata

        patch_session_metadata(self.session_id, {
            METADATA_KEY: {"owner": owner, "pending": pending},
        })
        self.owner, self.pending = deepcopy(owner), deepcopy(pending)

    async def _restore(self, identity):
        from openjiuwen.core.session.agent import create_agent_session
        from openjiuwen.core.single_agent import AgentCard

        session = create_agent_session(session_id=self.session_id, card=AgentCard(id=identity["agent_id"]))
        await session.pre_run()
        return session

    async def _check_cold_team(self, identity) -> None:
        if identity.get("team_id"):
            from openjiuwen.core.session.agent_team import create_agent_team_session

            team_session = create_agent_team_session(session_id=self.session_id, team_id=identity["team_id"])
            await team_session.pre_run()
            _require_no_team_resume(team_session)
            from openjiuwen.agent_teams.harness.conversation import persisted_team_conversation_is_idle

            if not await persisted_team_conversation_is_idle(team_session):
                raise ContextHandoffError("context_handoff_team_pending_work")

    def _live(self, identity) -> LiveConversation | None:
        value = self.live.get(identity["agent_id"])
        if value is None or value.agent is None or value.session is None:
            self.live.pop(identity["agent_id"], None)
            return None
        phase = getattr(value.agent, "state", None)
        if getattr(phase, "value", phase) == "terminated":
            return None
        return value

    def _guard(self, identity, request_id: str | None = None) -> None:
        if self.frozen or self.pending or self.owner != identity:
            raise ContextHandoffError("context_handoff_inactive_owner")
        if request_id is not None and self.input_pending == request_id:
            self.input_pending = None

    def _identity(self, agent, kind, team_session=None, *, profile="work") -> dict[str, str]:
        identity = {"kind": kind, "agent_id": str(agent.card.id)}
        if profile == "code":
            identity["profile"] = profile
        if team_session is not None:
            identity["team_id"] = team_session.get_team_id()
        return identity

    def _register(self, agent, session, kind, team_session=None, *, profile="work") -> dict[str, str]:
        identity = self._identity(agent, kind, team_session, profile=profile)
        self.live[identity["agent_id"]] = LiveConversation(
            ref(agent), ref(session), identity, ref(team_session) if team_session is not None else None,
        )
        agent.set_conversation_guard(lambda request_id=None: self._guard(identity, request_id))
        return identity

    async def _recover(self) -> None:
        if not self.pending:
            return
        target = self.pending.get("target")
        if target:
            restored = await self._restore(target)
            if restored.get_state("context_handoff") == self.pending["marker"]:
                live = self._live(target)
                if live is not None:
                    # A commit may have reached storage before raising. The
                    # failed importer rolled memory back; reload the receipt's
                    # committed messages before permitting any new execution.
                    from openjiuwen.core.context_engine.conversation import export_conversation, replace_conversation

                    await replace_conversation(live.session, live.agent.react_agent.context_engine,
                                               export_conversation(restored), self.pending["marker"],
                                               reset_state_keys=("team_prompt_context",) if target["kind"] == "team" else ())
                self._write(target, None)
                return
        self._write(self.owner, None)

    async def begin(self, kind: str, request_id: str, *, profile: str = "work") -> "HandoffAttempt":
        if profile not in {"work", "code"}:
            raise ValueError(f"unsupported conversation profile: {profile}")
        if self.lock.locked() and (
            self.preparing_kind != kind or self.frozen or self.pending
            or (self.owner is not None and self.owner["kind"] != kind)
        ):
            raise ContextHandoffError("context_handoff_in_progress")
        # Ordinary same-owner inputs retain their existing startup queueing.
        # Only a competing ownership change must fail without waiting.
        await self.lock.acquire()
        self.preparing_kind = kind
        attempt = HandoffAttempt(self, kind, request_id, profile)
        try:
            journal = self._read()
            self.owner, self.pending = journal.get("owner"), journal.get("pending")
            self.frozen = bool(self.pending)
            await self._recover()
            if self.owner and self.owner.get("profile", "work") != profile:
                raise ContextHandoffError("context_handoff_profile_mismatch", retryable=False)
            if self.owner and self.owner["kind"] != kind:
                if self.input_pending is not None:
                    raise ContextHandoffError("context_handoff_input_pending")
                live = self._live(self.owner)
                if live is not None:
                    await _require_quiescent(live.session, live.agent)
                    _require_no_team_resume(live.team_session)
                # Freeze synchronously before the first storage await. Native
                # sends and automatic continuation share the same guard.
                self.frozen = True
                source_session = live.session if live else await self._restore(self.owner)
                if live is None:
                    await self._check_cold_team(self.owner)
                await _require_quiescent(source_session, live.agent if live else None)
                from openjiuwen.core.context_engine.conversation import export_conversation

                engine = live.agent.react_agent.context_engine if live else None
                try:
                    attempt.snapshot = export_conversation(source_session, engine)
                except ValueError as exc:
                    raise ContextHandoffError(str(exc), retryable=False) from exc
                if engine is not None:
                    await engine.save_contexts(source_session)
                    live.agent.save_state(source_session)
                    await source_session.commit()
                self._write(self.owner, {"request_id": request_id, "source": self.owner,
                                         "target_kind": kind, "target": None})
            # With a cached target there is no startup callback to wait for.
            if kind == "team":
                for live in list(self.live.values()):
                    if live.identity["kind"] == kind and self._live(live.identity) is not None:
                        await attempt.ready(live.agent, live.session, live.team_session)
                        break
            return attempt
        except BaseException:
            await attempt.close()
            raise


class HandoffAttempt:
    def __init__(self, coordinator: ConversationHandoff, kind: str, request_id: str, profile: str):
        self.coordinator = coordinator
        self.kind = kind
        self.request_id = request_id
        self.profile = profile
        self.snapshot = None
        self.finished = False
        self.closed = False
        self.background_task: asyncio.Task | None = None

    async def _prepare_context(self, agent: Any, session: Any) -> None:
        engine = agent.react_agent.context_engine
        if engine.get_context(session_id=session.get_session_id()) is None:
            from jiuwenswarm.agents.harness.common.session_ops_service import _get_context_processors

            await engine.create_context(session=session, processors=_get_context_processors(agent.react_agent))

    async def ready(self, agent, session, team_session=None) -> None:
        c = self.coordinator
        if session.get_session_id() != c.session_id:
            raise ContextHandoffError("context_handoff_session_mismatch", retryable=False)
        if self.closed and not self.finished:
            raise ContextHandoffError("context_handoff_request_closed")
        if self.finished:
            # Persistent Team cycles inherit the original callback. They may
            # rebuild their native only while Team still owns the conversation.
            if c.owner != c._identity(agent, self.kind, team_session, profile=self.profile) or c.frozen:
                raise ContextHandoffError("context_handoff_inactive_owner")
            c._register(agent, session, self.kind, team_session, profile=self.profile)
            return
        identity = c._register(agent, session, self.kind, team_session, profile=self.profile)
        if self.snapshot is not None:
            if identity["agent_id"] == c.owner["agent_id"]:
                raise ContextHandoffError("context_handoff_identity_collision", retryable=False)
            await _require_quiescent(session, agent)
            _require_no_team_resume(team_session)
            await self._prepare_context(agent, session)
            marker = {"id": self.request_id, "source_agent_id": c.owner["agent_id"],
                      "target_agent_id": identity["agent_id"]}
            c._write(c.owner, {**c.pending, "target": identity, "marker": marker})
            from openjiuwen.core.context_engine.conversation import replace_conversation

            await replace_conversation(session, agent.react_agent.context_engine, self.snapshot, marker,
                                       reset_state_keys=("team_prompt_context",) if self.kind == "team" else ())
            c._write(identity, None)
        elif c.owner is None:
            self._check_legacy_owner()
            # Establish a native empty checkpoint as well, so a failed first
            # input can still be handed off without guessing from public history.
            engine = agent.react_agent.context_engine
            await self._prepare_context(agent, session)
            await engine.save_contexts(session)
            await session.commit()
            c._write(identity, None)
        elif c.owner != identity:
            raise ContextHandoffError("context_handoff_identity_changed", retryable=False)
        c.frozen = False
        if self.snapshot is not None:
            c.input_pending = self.request_id
        self.finished = True
        c.preparing_kind = None
        c.lock.release()

    def _check_legacy_owner(self) -> None:
        from jiuwenswarm.server.runtime.session.session_history import history_exists, load_history_records

        c = self.coordinator
        if not history_exists(c.session_id):
            return
        history = load_history_records(c.session_id)
        prior = [item for item in history if item.get("request_id") != self.request_id
                 and item.get("role") == "user"]
        if not prior:
            return
        mode = deprecate_mode(prior[-1].get("mode") or "")
        if mode not in {f"{self.kind}.{self.profile}.normal", f"{self.kind}.{self.profile}.plan"}:
            raise ContextHandoffError("context_handoff_source_unknown", retryable=False)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        c = self.coordinator
        if not self.finished:
            try:
                await c._recover()
                c.frozen = False
            finally:
                c.preparing_kind = None
                c.lock.release()
        if c.input_pending == self.request_id:
            c.input_pending = None


@asynccontextmanager
async def agent_conversation_request(request: Any, agent: Any):
    if request_kind(request) != "agent":
        yield
        return
    from jiuwenswarm.agents.harness.common.session_ops_service import resolve_live_agent_session

    session = resolve_live_agent_session(agent, request.session_id)
    if session is None:
        raise ContextHandoffError("context_handoff_target_session_missing")
    profile = request_profile(request)
    attempt = await get_handoff(request.session_id).begin("agent", request.request_id, profile=profile)
    try:
        await attempt.ready(agent, session)
        yield
    finally:
        await attempt.close()


@asynccontextmanager
async def team_conversation_request(request: Any):
    if request_kind(request) != "team":
        yield None
        return
    profile = request_profile(request)
    attempt = await get_handoff(request.session_id).begin("team", request.request_id, profile=profile)
    try:
        yield attempt
    finally:
        if attempt.background_task is None or attempt.background_task.done():
            await attempt.close()


@contextmanager
def bind_team_handoff(attempt: HandoffAttempt | None):
    """Bind only while advancing a stream, never across its public yields."""
    if attempt is None:
        yield
        return
    from openjiuwen.agent_teams.harness.conversation import bind_leader_preparation

    token = _TEAM_ATTEMPT.set(attempt)
    try:
        with bind_leader_preparation(attempt.ready):
            yield
    finally:
        _TEAM_ATTEMPT.reset(token)


def team_handoff_run_context(run_context: dict[str, Any] | None) -> dict[str, Any] | None:
    attempt = _TEAM_ATTEMPT.get()
    if attempt is None:
        return run_context
    return {**(run_context or {}), "conversation_request_id": attempt.request_id}


def retain_team_handoff(task: asyncio.Task) -> None:
    """Transfer startup ownership to the producer for no-waiter Team inputs."""
    attempt = _TEAM_ATTEMPT.get()
    if attempt is None:
        return
    attempt.background_task = task

    def completed(_task):
        cleanup = asyncio.create_task(attempt.close())
        # Retrieve cleanup errors; a failed recovery keeps the admission gate
        # closed until the next explicit request retries it.
        def report(result):
            if not result.cancelled() and result.exception() is not None:
                import logging
                logging.getLogger(__name__).error("Team handoff cleanup failed", exc_info=result.exception())
        cleanup.add_done_callback(report)

    task.add_done_callback(completed)

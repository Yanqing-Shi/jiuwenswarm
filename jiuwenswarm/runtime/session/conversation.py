"""Host assembly of a product Session's fixed shared conversation slot."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import BaseError
from openjiuwen.core.context_engine import ConversationBinding, ConversationKey
from openjiuwen.core.session.checkpointer import CheckpointerFactory

STORAGE_FIELD = "context_storage"
SHARED_STORAGE = "shared"
NATIVE_MODES = frozenset(
    f"{executor}.{profile}.{phase}"
    for executor in ("agent", "team") for profile in ("work", "code") for phase in ("normal", "plan")
)


def supports_shared_mode(mode: str | None) -> bool:
    from agent_gateway.common.mode_matrix import deprecate_mode

    return deprecate_mode(mode or "agent") in NATIVE_MODES


def session_mode_guard(session_id: str):
    """Capture the selected route; evaluate it only under execution ownership."""
    from agent_gateway.common.mode_matrix import deprecate_mode
    from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

    expected = get_session_metadata(session_id, cache_bust=True, enable_writeback=False).get("mode")

    def check() -> None:
        if expected:
            current = get_session_metadata(session_id, cache_bust=True, enable_writeback=False).get("mode")
            if deprecate_mode(current or "") != deprecate_mode(expected):
                raise RuntimeError("session mode changed; select the current executor before starting work")

    return check


def main_binding(session_id: str) -> ConversationBinding:
    checkpointer = CheckpointerFactory.get_checkpointer()
    return ConversationBinding(ConversationKey(session_id, "main"), checkpointer.conversation_store())


async def initialize_main_conversation(session_id: str, *, mode: str | None = None) -> ConversationBinding | None:
    """The single entry that creates a Session's main record.

    Called where a Session comes into existence. The record is created only in
    a native mode and only when the Session has no earlier conversation data;
    an older Session returns None and keeps its previous behaviour. A text-only
    application role rebuilds its model input from history and has no window to
    store; only its native Team form gets a record. Creation is idempotent, so
    callers need no lock.
    """
    from jiuwenswarm.runtime.app_role import APP_ROLE_PROFILE, is_app_role_team
    from jiuwenswarm.server.runtime.session.session_history import history_exists
    from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata, patch_session_metadata

    if not supports_shared_mode(mode):
        return None
    role = get_session_metadata(session_id, cache_bust=True, enable_writeback=False)
    if role.get("execution_profile") == APP_ROLE_PROFILE and not is_app_role_team(role):
        return None
    binding = main_binding(session_id)
    if history_exists(session_id) or await CheckpointerFactory.get_checkpointer().session_exists(session_id):
        return None
    await binding.store.create(binding.key)
    if get_session_metadata(session_id, cache_bust=True, enable_writeback=False):
        patch_session_metadata(session_id, {STORAGE_FIELD: SHARED_STORAGE})
    return binding


async def load_main_binding(session_id: str) -> ConversationBinding | None:
    """Resolve a shared Session without converting a missing old conversation."""
    from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata, patch_session_metadata

    metadata = get_session_metadata(session_id, cache_bust=True, enable_writeback=False)
    binding = main_binding(session_id)
    if metadata.get(STORAGE_FIELD) == SHARED_STORAGE:
        return binding
    # SDK-created Sessions can allocate their record before metadata exists.
    try:
        await binding.store.read(binding.key)
    except BaseError as exc:
        if exc.code == StatusCode.CONTEXT_CONVERSATION_NOT_FOUND.code:
            return None
        raise
    if metadata:
        patch_session_metadata(session_id, {STORAGE_FIELD: SHARED_STORAGE})
    return binding


@asynccontextmanager
async def shared_session_operation(session_id: str) -> AsyncIterator[bool]:
    """Own a shared Session's operation while a host action rewrites its record.

    Fails fast with ``SessionOperationBusyError`` while a run or a mode switch
    owns the Session. A Session without a shared record keeps its earlier,
    unlocked behaviour. Yields whether the Session is shared.
    """
    from jiuwenswarm.runtime.session.operation_lock import acquire_operation

    if await load_main_binding(session_id) is None:
        yield False
        return
    with acquire_operation("session", session_id):
        yield True


async def prepare_shared_leader(native: Any, child: Any, team_session: Any) -> None:
    """Keep background Leader ownership independent of the old copy handoff."""
    from jiuwenswarm.runtime.session.native_operation import bind_native_operation

    binding = team_session.get_conversation_binding() if team_session is not None else None
    if not isinstance(binding, ConversationBinding):
        return
    if child.get_conversation_binding() != binding:
        raise RuntimeError("actual Leader did not inherit the shared main conversation")
    await bind_native_operation(native, binding.key.session_id, session_mode_guard(binding.key.session_id))

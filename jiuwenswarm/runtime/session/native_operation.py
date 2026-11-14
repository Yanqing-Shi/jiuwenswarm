"""Bind actual Leader execution to Session ownership through NativeHarness hooks."""

from __future__ import annotations

import asyncio
from typing import Any, Callable
from weakref import ref, WeakSet

from jiuwenswarm.runtime.session.coordinator import RuntimeSessionCoordinator


class NativeOperationBinding:
    def __init__(self, coordinator: RuntimeSessionCoordinator, session_id: str, harness: Any) -> None:
        self._coordinator = coordinator
        self._session_id = session_id
        self._token = object()
        self._supervisor: asyncio.Task | None = None
        self._harness = ref(harness)
        self._callers: WeakSet[asyncio.Task] = WeakSet()
        self.guard: Callable[[], None] | None = None

    def check(self) -> None:
        harness = self._harness()
        if harness is None or getattr(harness.state, "value", harness.state) == "terminated":
            if self.guard is not None:
                self.guard()
            return
        acquired = self._coordinator.begin_native_operation(self._session_id, self._token)
        # The mode cannot change while this Leader owns the operation, so the
        # host guard only runs when ownership is newly taken.
        if acquired and self.guard is not None:
            try:
                self.guard()
            except BaseException:
                self._coordinator.end_native_operation(self._token)
                raise
        caller = asyncio.current_task()
        if caller is not None and caller not in self._callers:
            self._callers.add(caller)
            caller.add_done_callback(self._on_caller_done)

    async def on_state(self, *, new: Any) -> None:
        phase = getattr(new, "value", new)
        if phase == "running":
            supervisor = asyncio.current_task()
            if supervisor is not None and supervisor is not self._supervisor:
                self._supervisor = supervisor
                supervisor.add_done_callback(self._on_supervisor_done)
        elif phase in {"idle", "terminated"}:
            # IDLE follows NativeHarness's final checkpoint commit. A round
            # event or a disconnected response waiter can happen before it.
            self._coordinator.end_native_operation(self._token)

    def _on_supervisor_done(self, task: asyncio.Task) -> None:
        self._coordinator.end_native_operation(self._token)

    def _on_caller_done(self, task: asyncio.Task) -> None:
        # The dispatch guard also runs in the supervisor, including failures
        # before its first RUNNING transition. A disconnected sender alone
        # cannot release ownership of a still-running native.
        harness = self._harness()
        if harness is None or getattr(harness.state, "value", harness.state) == "terminated":
            self._coordinator.end_native_operation(self._token)


async def bind_native_operation(
    agent: Any, session_id: str, guard: Callable[[], None],
) -> None:
    from jiuwenswarm.runtime.context import get_current_runtime

    runtime = get_current_runtime()
    if runtime is None:
        agent.set_conversation_guard(guard)
        return
    binding = getattr(agent, "_swarm_operation_binding", None)
    if binding is None:
        binding = NativeOperationBinding(runtime._session_coordinator, session_id, agent)
        await agent.subscribe(on_state=binding.on_state)
        agent._swarm_operation_binding = binding
    binding.guard = guard
    agent.set_conversation_guard(binding.check)

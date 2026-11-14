"""Session execution ownership shared by runs and mode switches.

Separate from short-lived resource commits and from the archive/delete
service's own owner lock: those flows keep their existing behaviour and do not
take this lock. ``kind`` is kept so they can join it later.
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path


class SessionOperationBusyError(RuntimeError):
    """Another operation owns the resource's execution lock."""


_REGISTRY_LOCK = threading.Lock()
_OWNERS: dict[Path, _Owner] = {}


@dataclass
class _Owner:
    path: Path
    file_lock: AbstractContextManager
    references: int = 0


class OperationLease:
    """One explicit reference to an admitted operation's execution ownership.

    Control deliveries and detached Team rounds retain the existing owner;
    unrelated operations must acquire the resource themselves.
    """

    def __init__(self, owner: _Owner) -> None:
        self._owner = owner
        self._closed = False
        owner.references += 1

    def retain(self) -> OperationLease:
        with _REGISTRY_LOCK:
            if self._closed:
                raise RuntimeError("operation lease is closed")
            return OperationLease(self._owner)

    def close(self) -> None:
        with _REGISTRY_LOCK:
            if self._closed:
                return
            self._closed = True
            owner = self._owner
            owner.references -= 1
            if not owner.references:
                try:
                    owner.file_lock.__exit__(None, None, None)
                finally:
                    _OWNERS.pop(owner.path, None)

    def __enter__(self) -> OperationLease:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def acquire_operation(kind: str, resource_id: str) -> OperationLease:
    """Try once; the lock file sits beside, not on, the lifecycle owner locks."""
    from jiuwenswarm.server.runtime.session import lifecycle as lc

    path = (
        lc.resource_path(kind, resource_id).parent.parent
        / "operations"
        / f"{kind}_{resource_id}.json"
    ).resolve()
    with _REGISTRY_LOCK:
        if path in _OWNERS:
            raise SessionOperationBusyError("resource operation is already running")
        file_lock = lc.file_lock(path, blocking=False)
        try:
            file_lock.__enter__()
        except BlockingIOError as exc:
            raise SessionOperationBusyError("resource operation is already running") from exc
        owner = _Owner(path, file_lock)
        _OWNERS[path] = owner
        return OperationLease(owner)


async def wait_for_operation(session_id: str) -> OperationLease:
    """Wait inside the existing chat lane without blocking the event loop."""
    while True:
        try:
            return acquire_operation("session", session_id)
        except SessionOperationBusyError:
            await asyncio.sleep(0.01)

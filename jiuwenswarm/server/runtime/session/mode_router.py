# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Client for the optional local mode router.

The router is a separate local service that reads a prompt with the earlier
tasks of the conversation and answers whether the next run belongs in Agent or
Team mode.
This module only asks and validates the answer; committing a switch stays with
``mode.set``. Settings are in ``jiuwenswarm.common.mode_router_config``.
"""

from __future__ import annotations

from typing import Any

import httpx

from jiuwenswarm.common.mode_router_config import ModeRouterSettings

ROUTES = ("agent", "team")
_REASON_CHARS = 500
_TRUNCATED = " [truncated]"


class ModeRouterError(ValueError):
    """The router could not give a usable answer."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def route_of(mode: Any) -> str | None:
    """The Agent/Team part of a mode name, or None when it has neither."""
    if not isinstance(mode, str):
        return None
    head = mode.strip().lower().split(".", 1)[0]
    return head if head in ROUTES else None


def cut_text(text: str, limit: int) -> str:
    """Cut ``text`` to ``limit`` characters and mark the cut; 0 or less means no limit."""
    return text if limit <= 0 or len(text) <= limit else text[:limit] + _TRUNCATED


def task_history(session_id: str, *, items: int, chars: int) -> list[dict[str, Any]]:
    """Return what came before in a Session as router messages, oldest first.

    In recorded order: every prompt the person typed, and the final answer of
    each run. A prompt sent into a run that was already going carries
    ``during_run``; a run that ended without an answer gives its prompts
    alone. Only the latest ``items`` messages are kept, and ``chars`` cuts
    each text; 0 means no limit.

    Blocking: the read waits for queued history writes, so the answer of a
    run that has just ended is included. Call it off the event loop.
    """
    from jiuwenswarm.server.runtime.session.mode_router_tasks import read_session_tasks

    messages: list[dict[str, Any]] = []
    for task in read_session_tasks(session_id, items=items):
        for item in task:
            message: dict[str, Any] = {"role": item["role"], "content": cut_text(item["content"], chars)}
            route = route_of(item["mode"])
            if route:
                message["mode"] = route
            if item.get("during_run"):
                message["during_run"] = True
            messages.append(message)
    return messages


def _build_client(settings: ModeRouterSettings) -> httpx.AsyncClient:
    # The router is a local service; proxy variables must not capture the call.
    return httpx.AsyncClient(timeout=settings.timeout_seconds, trust_env=False)


async def request_decision(settings: ModeRouterSettings, payload: dict[str, Any]) -> dict[str, Any]:
    """Ask the router once and return ``{"route", "confidence", "reason"}``."""
    if not settings.url:
        raise ModeRouterError("mode router is not configured", code="ROUTER_DISABLED")
    try:
        async with _build_client(settings) as client:
            response = await client.post(f"{settings.url}/v1/route", json=payload)
    except httpx.HTTPError as exc:
        raise ModeRouterError(
            f"mode router did not answer: {type(exc).__name__}", code="ROUTER_UNAVAILABLE",
        ) from exc
    if response.status_code != 200:
        raise ModeRouterError(
            f"mode router answered HTTP {response.status_code}", code="ROUTER_UNAVAILABLE",
        )
    try:
        answer = response.json()
    except ValueError as exc:
        raise ModeRouterError("mode router answer is not JSON", code="ROUTER_BAD_ANSWER") from exc
    route = route_of(answer.get("mode")) if isinstance(answer, dict) else None
    if route is None or answer["mode"].strip().lower() != route:
        raise ModeRouterError(
            "mode router answer must name 'agent' or 'team'", code="ROUTER_BAD_ANSWER",
        )
    confidence = answer.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        confidence = None
    reason = answer.get("reason")
    return {
        "route": route,
        "confidence": confidence,
        "reason": cut_text(reason.strip(), _REASON_CHARS) if isinstance(reason, str) else "",
    }

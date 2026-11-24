# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Settings of the optional local mode router.

Plain configuration, so it lives in ``common``: the Front config query reports
whether the router is on without loading server code. The router is off unless
``MODE_ROUTER_URL`` or ``mode_router.url`` names its address.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

DEFAULT_TIMEOUT_SECONDS = 3.0
# How many earlier items (prompts and answers) are kept for the router. When one
# more comes in, the earliest goes out. 0 means no limit.
DEFAULT_HISTORY_ITEMS = 20
_MAX_HISTORY_ITEMS = 1_000_000
# Texts are sent in full. Optional caps for a router with a small context; 0
# means no limit.
DEFAULT_HISTORY_CHARS = 0
DEFAULT_QUERY_CHARS = 0


@dataclass(frozen=True)
class ModeRouterSettings:
    url: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    history_items: int = DEFAULT_HISTORY_ITEMS
    history_chars: int = DEFAULT_HISTORY_CHARS
    query_chars: int = DEFAULT_QUERY_CHARS


def _number(value: Any, default: float, *, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default
    try:
        number = float(value)
    except ValueError:
        return default
    return min(max(number, low), high)


def mode_router_settings(config: dict[str, Any] | None = None) -> ModeRouterSettings:
    """Read the ``mode_router`` section; a blank ``url`` means the router is off."""
    if config is None:
        # Imported lazily: the config module pulls in KV-cache types that the
        # Front config query resolves these settings without.
        from jiuwenswarm.common.config import get_config

        config = get_config()
    section = config.get("mode_router") if isinstance(config, dict) else None
    if not isinstance(section, dict):
        section = {}
    # The environment wins, so a launcher can turn the router on without
    # editing a config file that predates the ``mode_router`` section.
    url = os.environ.get("MODE_ROUTER_URL") or section.get("url")
    url = url.strip().rstrip("/") if isinstance(url, str) else ""
    # An unresolved ``${VAR}`` placeholder is not an address.
    if not url.startswith(("http://", "https://")):
        url = ""
    return ModeRouterSettings(
        url=url,
        timeout_seconds=_number(
            section.get("timeout_seconds"), DEFAULT_TIMEOUT_SECONDS, low=0.1, high=60.0,
        ),
        history_items=int(_number(
            section.get("history_items"), DEFAULT_HISTORY_ITEMS, low=0, high=_MAX_HISTORY_ITEMS,
        )),
        history_chars=int(_number(
            section.get("history_chars"), DEFAULT_HISTORY_CHARS, low=0, high=10_000_000,
        )),
        query_chars=int(_number(
            section.get("query_chars"), DEFAULT_QUERY_CHARS, low=0, high=10_000_000,
        )),
    )


def mode_router_enabled(config: dict[str, Any] | None = None) -> bool:
    return bool(mode_router_settings(config).url)

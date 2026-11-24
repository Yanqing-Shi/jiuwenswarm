# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Per-Session task container read by the mode router.

A task is one run: the prompt that starts it, the prompts the person sends into
it while it is going, and its final answer. For a new prompt the mode router
reads what the person typed and what was finally answered, in the order it was
recorded: prompt 1, answer 1, prompt 2, a prompt sent during run 2, answer 2,
... and then the new prompt.

The container is a small journal next to the Session's history file. It holds
only the history records a task is made of: the prompts the person typed and
the final answers of the main speaker. The history writer adds a row the moment
its record is on disk, so reading the tasks never walks the event records of a
run.

The container holds a limited number of items (prompts and answers). When one
more comes in, the earliest goes out; the history file keeps everything.

The history file stays the source: every row names the history offset it came
from, and the journal names the history file it belongs to. A journal that is
missing, belongs to a replaced history file (rewind, restore, fork) or is behind
is rebuilt or caught up from the history file on the next read.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from jiuwenswarm.common.mode_router_config import DEFAULT_HISTORY_ITEMS

logger = logging.getLogger(__name__)

TASKS_FILENAME = "mode_router_tasks.jsonl"
# 2: prompts sent into a running turn are rows of their own.
_VERSION = 2
_HISTORY_FILENAME = "history.jsonl"
# The speakers whose final answer is the answer of the run. A Team member's
# final answer ("teammate") is part of the run, not its result.
_LEADER = "leader"
_MAIN_SPEAKERS = frozenset({"assistant", _LEADER})
# Set on a prompt the server recorded while a Team round was in flight.
TEAM_ROUND_INPUT_FIELD = "sent_during_team_round"

# Guards the journal files and ``_COVERED``. Taken after the history file lock.
_LOCK = threading.Lock()
# history path -> (device, inode, history offset the journal is known to cover).
# Memory only: records that are not part of a task move it without a write.
_COVERED: OrderedDict[Path, tuple[int, int, int]] = OrderedDict()
_COVERED_LIMIT = 512
# Set by the first routed prompt of this process; see ``_keeps_journal``.
_activated = False
# How many items a container keeps; 0 means no limit. The history writer has no
# cheap way to read the configuration, so it uses the limit of the last routed
# prompt, and the default until there was one.
_item_limit = DEFAULT_HISTORY_ITEMS


def _keeps_journal() -> bool:
    """Whether the history writer maintains journals in this process.

    Without a mode router nothing reads them, so nothing is written. The
    environment check is free; a router named only in ``config.yaml`` turns
    the writer on with its first routed prompt, and the read catches up.
    """
    return _activated or bool(os.environ.get("MODE_ROUTER_URL"))


def _is_team(mode: Any) -> bool:
    return isinstance(mode, str) and mode.strip().lower().split(".", 1)[0] == "team"


def journal_row(record: dict[str, Any]) -> dict[str, Any] | None:
    """The journal row for one main-history record, or None when it is not part of a task.

    ``prompt`` is text the person typed: it starts a task, or, with
    ``during_run``, went into the turn that was already going (a steered or
    supplemental message, a message to the Leader of a running Team round).
    ``turn`` starts a turn that is not a task (nobody typed it, or it has no
    text), so what is answered next is not the answer of the task before it.
    ``final`` is a final answer of the main speaker.
    """
    if record.get("subagent_id") or record.get("transcript_only") is True:
        return None
    event = record.get("event_type")
    role = str(record.get("role") or "").strip().lower()
    request_id = str(record.get("request_id") or "").strip()
    content = record.get("content")
    text = content if isinstance(content, str) and content.strip() else ""
    if role in {"user", "human"}:
        if event:
            return None
        from jiuwenswarm.server.runtime.session.session_history import is_agent_initiated_record

        during_run = bool(record.get("is_supplemental_input") or record.get(TEAM_ROUND_INPUT_FIELD))
        typed = bool(
            text
            and request_id
            and not record.get("is_goal_objective_message")
            # Stored as a user row, but the person did not type it.
            and not is_agent_initiated_record(record)
        )
        if not typed:
            # Input to a turn that is already going starts no turn either.
            return None if during_run else {"t": "turn"}
        timestamp = record.get("timestamp")
        mode = record.get("mode")
        row = {
            "t": "prompt",
            "rid": request_id,
            "ts": float(timestamp) if type(timestamp) in (int, float) else 0.0,
            "mode": mode if isinstance(mode, str) else "",
            "content": text,
        }
        if during_run:
            row["during_run"] = True
        return row
    if event == "chat.final" and role in _MAIN_SPEAKERS and text and request_id:
        return {"t": "final", "rid": request_id, "by": role, "content": text}
    return None


def assemble_tasks(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group journal rows into tasks, oldest first; each task is its items in recorded order.

    See ``_assemble`` for the rules.
    """
    return [items for _, items in _assemble(rows)]


def _assemble(rows: list[dict[str, Any]]) -> list[tuple[int, list[dict[str, Any]]]]:
    """Group journal rows into tasks; each comes with the index of the row it starts at.

    An item is ``{"role": "user" | "assistant", "content", "mode"}``; a prompt
    sent into a running turn also has ``"during_run": True``.

    A prompt that starts a turn starts a task. Prompts sent into that turn stay
    in it, in their place. Of the final answers of the turn only the last one
    is kept, where it was recorded: an interim answer is not what the run
    delivered. An answer belongs to the task when it was recorded under one of
    the task's requests, or, for a Team task, when the Leader spoke it. A Team
    keeps one response stream for its whole life, so the Leader's answers to
    later prompts are all recorded under the request that opened the stream.
    """
    tasks: list[tuple[int, list[dict[str, Any]]]] = []
    items: list[dict[str, Any]] | None = None
    request_ids: set[Any] = set()
    mode = ""
    answer: dict[str, Any] | None = None
    for index, row in enumerate(rows):
        kind = row.get("t")
        if kind == "prompt":
            item = {"role": "user", "content": row.get("content") or "", "mode": row.get("mode") or ""}
            if row.get("during_run"):
                item["during_run"] = True
                if items is None:
                    # Typed into a turn that is not a task: kept, with nothing to answer it.
                    tasks.append((index, [item]))
                    continue
                request_ids.add(row.get("rid"))
            else:
                items, request_ids, mode, answer = [], {row.get("rid")}, item["mode"], None
                tasks.append((index, items))
            items.append(item)
        elif kind == "turn":
            items = None
        elif kind == "final" and items is not None:
            if row.get("rid") in request_ids or (row.get("by") == _LEADER and _is_team(mode)):
                if answer is not None:
                    items[:] = [item for item in items if item is not answer]
                answer = {"role": "assistant", "content": row.get("content") or "", "mode": mode}
                items.append(answer)
    return tasks


def _keep_latest(
    rows: list[dict[str, Any]], limit: int,
) -> tuple[int, list[list[dict[str, Any]]]]:
    """Cut the tasks of ``rows`` to their latest ``limit`` items; 0 means no limit.

    Returns the index of the first row still needed and the tasks that are
    left. The earliest items go first, also out of the middle of a task. The
    rows of a task that is only partly left are all still needed: the answer
    of a task is recognised from the prompt that started it.
    """
    assembled = _assemble(rows)
    if limit <= 0:
        return 0, [items for _, items in assembled]
    if not assembled:
        # Rows of turns nobody typed. The last one still says how far the journal covers the history.
        return max(len(rows) - 1, 0), []
    kept: list[list[dict[str, Any]]] = []
    first_row = 0
    room = limit
    for start, items in reversed(assembled):
        if room <= 0:
            break
        kept.append(items[-room:])
        room -= len(items)
        first_row = start
    kept.reverse()
    return first_row, kept


def _remember(history_path: Path, covered: tuple[int, int, int]) -> None:
    _COVERED[history_path] = covered
    _COVERED.move_to_end(history_path)
    while len(_COVERED) > _COVERED_LIMIT:
        _COVERED.popitem(last=False)


def _head(identity: tuple[int, int]) -> dict[str, Any]:
    return {"t": "head", "v": _VERSION, "dev": identity[0], "ino": identity[1]}


def _dump(row: dict[str, Any]) -> str:
    return json.dumps(row, ensure_ascii=False) + "\n"


def _start_journal(tasks_path: Path, identity: tuple[int, int], rows: list[dict[str, Any]] | None = None) -> None:
    """Write a journal anew: its head, and the rows that stay in it."""
    temporary = tasks_path.with_name(f".{tasks_path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write("".join(_dump(row) for row in (_head(identity), *(rows or ()))))
    os.replace(temporary, tasks_path)


def _append_rows(tasks_path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    with tasks_path.open("a", encoding="utf-8") as stream:
        stream.write("".join(_dump(row) for row in rows))


def _load_journal(tasks_path: Path, identity: tuple[int, int]) -> list[dict[str, Any]] | None:
    """The rows of a journal that belongs to this history file, or None when it must be rebuilt."""
    try:
        text = tasks_path.read_text(encoding="utf-8")
    except (FileNotFoundError, UnicodeDecodeError):
        return None
    rows: list[dict[str, Any]] = []
    lines = text.split("\n")
    # Without a final line break the last line is an append that was cut short.
    for line in lines[:-1]:
        try:
            row = json.loads(line)
        except ValueError:
            return None
        if not isinstance(row, dict):
            return None
        rows.append(row)
    if lines[-1] or not rows or rows[0] != _head(identity):
        return None
    return rows[1:]


def _scan_history(history_path: Path, start: int, end: int) -> tuple[list[dict[str, Any]], int]:
    """Journal rows of the history records in ``[start, end)`` and the offset reached."""
    rows: list[dict[str, Any]] = []
    offset = start
    with history_path.open("rb") as stream:
        stream.seek(start)
        while offset < end:
            line = stream.readline()
            if not line.endswith(b"\n") or offset + len(line) > end:
                break
            offset += len(line)
            try:
                record = json.loads(line)
            except ValueError:
                continue
            row = journal_row(record) if isinstance(record, dict) else None
            if row is not None:
                row["at"] = offset
                rows.append(row)
    return rows, offset


def note_history_append(history_path: Path, record: dict[str, Any], before: os.stat_result | None) -> None:
    """Add the journal row of one record the history writer has just appended.

    Called with the Session's history serialized. Never raises: the history
    write has already succeeded, and a journal that falls behind is caught up
    by the next read.
    """
    if history_path.name != _HISTORY_FILENAME or not _keeps_journal():
        return
    try:
        from jiuwenswarm.server.runtime.session.session_history import _managed_history_session_id

        # Subagent transcripts live below the Session folder and are not tasks.
        if _managed_history_session_id(history_path) != history_path.parent.name:
            return
        after = history_path.stat()
        identity = (after.st_dev, after.st_ino)
        tasks_path = history_path.with_name(TASKS_FILENAME)
        with _LOCK:
            if before is None or before.st_size == 0:
                # A new history file: the journal starts with it.
                _start_journal(tasks_path, identity)
            else:
                covered = _COVERED.get(history_path)
                if covered is not None and covered[:2] == identity and covered[2] >= after.st_size:
                    return
                if covered is None or covered[:2] != identity or covered[2] != before.st_size:
                    # Records were appended that this process did not see.
                    _COVERED.pop(history_path, None)
                    return
            row = journal_row(record)
            if row is not None:
                row["at"] = after.st_size
                _append_rows(tasks_path, [row])
                if _item_limit > 0:
                    # One more item came in: the earliest goes out.
                    rows = _load_journal(tasks_path, identity)
                    first_row = _keep_latest(rows, _item_limit)[0] if rows else 0
                    if first_row > 0:
                        _start_journal(tasks_path, identity, rows[first_row:])
            _remember(history_path, (*identity, after.st_size))
    except Exception:
        logger.debug("mode router task journal not updated: %s", history_path, exc_info=True)
        with _LOCK:
            _COVERED.pop(history_path, None)


def _sync_journal(history_path: Path, limit: int) -> list[list[dict[str, Any]]]:
    """Bring the journal up to the end of the history file; return its tasks, cut to ``limit`` items."""
    tasks_path = history_path.with_name(TASKS_FILENAME)
    stat = history_path.stat()
    identity = (stat.st_dev, stat.st_ino)
    rows = _load_journal(tasks_path, identity)
    covered = None
    if rows is not None:
        covered = max((row["at"] for row in rows if type(row.get("at")) is int), default=0)
        cached = _COVERED.get(history_path)
        if cached is not None and cached[:2] == identity and cached[2] >= covered:
            covered = cached[2]
        if covered > stat.st_size:
            covered = None
    if covered is None:
        # No journal yet, or one of another history file: start over from the history.
        _start_journal(tasks_path, identity)
        rows, covered = [], 0
    added, reached = _scan_history(history_path, covered, stat.st_size)
    rows = rows + added
    first_row, tasks = _keep_latest(rows, limit)
    if first_row > 0:
        _start_journal(tasks_path, identity, rows[first_row:])
    else:
        _append_rows(tasks_path, added)
    _remember(history_path, (*identity, reached))
    return tasks


def read_session_tasks(session_id: str, *, items: int | None = None) -> list[list[dict[str, Any]]]:
    """Return the tasks of a Session, oldest first; see ``assemble_tasks``.

    ``items`` is how many items (prompts and answers) are kept, the latest
    ones; 0 means no limit, None the limit in use. The container is cut to it
    as well, and the history writer keeps to it from then on.

    Blocking: waits for queued history writes first, so the answer of a run
    that has just ended is included. Call it off the event loop.
    """
    global _activated, _item_limit
    from jiuwenswarm.server.runtime.session import session_history

    _activated = True
    if items is not None:
        _item_limit = max(int(items), 0)
    limit = _item_limit
    session_history.flush_history_writes()
    history_path = session_history.get_read_history_path(session_id)
    if history_path.suffix.lower() != ".jsonl":
        # The legacy single-file history has no offsets to follow.
        rows = [
            row
            for row in (journal_row(record) for record in session_history.load_history_records(session_id))
            if row is not None
        ]
        return _keep_latest(rows, limit)[1]
    with session_history.FILE_LOCK, _LOCK:
        if not history_path.exists():
            return []
        return _sync_journal(history_path, limit)

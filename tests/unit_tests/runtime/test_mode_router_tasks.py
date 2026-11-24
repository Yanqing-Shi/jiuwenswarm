"""The mode router's task container: one prompt and the final answer of its run, per task."""

from __future__ import annotations

import json

import pytest

from jiuwenswarm.server.runtime.session import (
    lifecycle,
    mode_router_tasks,
    session_history,
    session_metadata,
)

SESSION_ID = "one-session"
TEAM = "team.work.normal"


@pytest.fixture
def session_dir(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(session_metadata, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(lifecycle, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(lifecycle, "get_agent_root_dir", lambda: tmp_path)
    monkeypatch.setenv("MODE_ROUTER_URL", "http://127.0.0.1:19100")
    monkeypatch.setattr(mode_router_tasks, "_activated", False)
    monkeypatch.setattr(mode_router_tasks, "_item_limit", mode_router_tasks.DEFAULT_HISTORY_ITEMS)
    mode_router_tasks._COVERED.clear()
    yield sessions / SESSION_ID
    session_history.flush_history_writes()
    mode_router_tasks._COVERED.clear()


def _prompt(request_id, content, mode="agent.work.normal", **extra):
    return {"role": "user", "request_id": request_id, "content": content, "mode": mode, "timestamp": 1.0, **extra}


def _final(request_id, content, role="assistant"):
    return {
        "role": role, "event_type": "chat.final", "request_id": request_id, "content": content, "timestamp": 2.0,
    }


def _done(request_id, status="success"):
    return {
        "id": f"{request_id}:request_completed", "role": "assistant", "event_type": "chat.request_completed",
        "request_id": request_id, "content": "", "feedback_only": True, "status": status, "timestamp": 3.0,
    }


def _event(request_id, event_type="chat.tool_call"):
    return {"role": "assistant", "event_type": event_type, "request_id": request_id, "content": "", "timestamp": 1.5}


MODE_CHANGED = {"event_type": "session.mode_changed", "request_id": "", "mode": "agent.work.normal", "timestamp": 4.0}


def _write(*records):
    """Through the real history writer, as a run does."""
    for record in records:
        session_history._enqueue_history_item(SESSION_ID, dict(record))
    session_history.flush_history_writes()


def _journal(session_dir):
    path = session_dir / mode_router_tasks.TASKS_FILENAME
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _brief(tasks):
    """Each task as its texts in order: "U:" a prompt, "S:" a prompt sent into the run, "A:" the answer."""
    return [
        [
            ("A:" if item["role"] == "assistant" else "S:" if item.get("during_run") else "U:") + item["content"]
            for item in task
        ]
        for task in tasks
    ]


def _tasks(**options):
    return _brief(mode_router_tasks.read_session_tasks(SESSION_ID, **options))


def _assembled(*records):
    rows = [row for row in map(mode_router_tasks.journal_row, records) if row is not None]
    return _brief(mode_router_tasks.assemble_tasks(rows))


@pytest.mark.parametrize("record,kind", [
    (_prompt("r", "hello"), "prompt"),
    (_final("r", "answer"), "final"),
    (_final("r", "answer", role="leader"), "final"),
    # A turn of its own, but not a task:
    (_prompt("r", "  "), "turn"),
    (_prompt("", "no request"), "turn"),
    (_prompt("r", "objective", is_goal_objective_message=True), "turn"),
    (_prompt("r", "from another session", message_origin="session"), "turn"),
    (_prompt("r", "heartbeat", metadata={"automation": {"kind": "heartbeat"}}), "turn"),
    # Typed into a turn that is already going:
    (_prompt("r", "steer", is_supplemental_input=True), "prompt"),
    (_prompt("r", "to the Leader", TEAM, sent_during_team_round=True), "prompt"),
    # Into a running turn, but nobody typed it: neither a prompt nor a turn of its own.
    (_prompt("r", "from another session", is_supplemental_input=True, message_origin="session"), None),
    (_prompt("r", " ", is_supplemental_input=True), None),
    (_prompt("r", "design event", event_type="design.note"), None),
    # Not a prompt and not the answer of a run:
    (_prompt("r", "child transcript", subagent_id="sub-1"), None),
    (_final("r", "member answer", role="teammate"), None),
    (_final("r", ""), None),
    (_done("r"), None),
    (MODE_CHANGED, None),
    (_event("r"), None),
    ({**_final("r", "thinking"), "event_type": "chat.reasoning"}, None),
])
def test_only_prompts_and_final_answers_enter_the_container(record, kind):
    if record.get("message_origin"):
        record = {**record, "message_origin": session_history.SESSION_MESSAGE_ORIGIN}
    row = mode_router_tasks.journal_row(record)

    assert (row or {}).get("t") == kind
    if kind in {"prompt", "final"}:
        assert row["content"] == record["content"] and row["rid"] == "r"
    if kind == "prompt":
        steered = bool(record.get("is_supplemental_input") or record.get("sent_during_team_round"))
        assert row.get("during_run", False) is steered


def test_a_task_is_a_prompt_and_the_final_answer_of_its_run():
    assert _assembled(
        _prompt("a", "first prompt"), _event("a"), _final("a", "first answer"), _done("a"),
        _prompt("b", "second prompt"), _final("b", "draft"), _final("b", "second answer"), _done("b", "error"),
        # A request with no prompt of its own (the page's read-aloud call) answers nobody's task.
        _prompt("x", ""), _final("x", "stray answer"), _done("x"),
        _final("y", "another stray answer"),
        _prompt("c", "third prompt"),
    ) == [
        ["U:first prompt", "A:first answer"],
        ["U:second prompt", "A:second answer"],
        ["U:third prompt"],
    ]


def test_a_stopped_task_keeps_its_prompt_and_has_no_answer():
    assert _assembled(
        _prompt("a", "stopped prompt"), _event("a"), _done("a", "cancelled"),
        _prompt("b", "next prompt"), _final("b", "next answer"), _done("b"),
    ) == [
        ["U:stopped prompt"],
        ["U:next prompt", "A:next answer"],
    ]


def test_a_prompt_sent_into_a_run_stays_in_it_and_only_the_last_answer_is_kept():
    """Everything the person typed, in recorded order; an interim answer is not what the run delivered."""
    assert _assembled(
        _prompt("a", "agent prompt"), _final("a", "interim answer"),
        _prompt("s1", "send now", is_supplemental_input=True),
        # Recorded under the steered message's own request: still an answer of this run.
        _final("s1", "second interim answer"),
        _prompt("s2", "send now again", is_supplemental_input=True),
        _final("a", "agent answer"), _done("a"),
        _prompt("b", "stopped while steering"), _prompt("s3", "too late", is_supplemental_input=True), _done("b"),
        _prompt("c", "answered before the steer"), _final("c", "early answer"),
        _prompt("s4", "afterthought", is_supplemental_input=True), _done("c"),
    ) == [
        ["U:agent prompt", "S:send now", "S:send now again", "A:agent answer"],
        ["U:stopped while steering", "S:too late"],
        ["U:answered before the steer", "A:early answer", "S:afterthought"],
    ]


def test_team_answers_follow_the_prompts_although_they_share_one_request():
    """A Team keeps one response stream: every answer of the Leader is recorded under its first request."""
    tasks = mode_router_tasks.assemble_tasks([row for row in map(mode_router_tasks.journal_row, (
        _prompt("t1", "first team prompt", TEAM),
        _final("t1", "member answer", role="teammate"),
        _final("t1", "work assigned", role="leader"),
        _prompt("m1", "message to the Leader", TEAM, sent_during_team_round=True), _done("m1"),
        _final("t1", "first team answer", role="leader"),
        # Sent to the idle Team: a task of its own, acknowledged at once, answered under the first request.
        _prompt("t2", "second team prompt", TEAM), _done("t2"),
        _final("t1", "interim", role="leader"), _final("t1", "second team answer", role="leader"),
        MODE_CHANGED, _done("t1"),
        _prompt("a1", "agent prompt"), _final("a1", "agent answer"), _done("a1"),
        # A late word of the Leader is not the answer of the Agent task.
        _final("t1", "late team answer", role="leader"),
    )) if row is not None])

    assert _brief(tasks) == [
        ["U:first team prompt", "S:message to the Leader", "A:first team answer"],
        ["U:second team prompt", "A:second team answer"],
        ["U:agent prompt", "A:agent answer"],
    ]
    assert [[item["mode"].split(".")[0] for item in task] for task in tasks] == [
        ["team", "team", "team"], ["team", "team"], ["agent", "agent"],
    ]


def test_a_turn_nobody_typed_does_not_answer_the_task_before_it():
    assert _assembled(
        _prompt("t1", "team prompt", TEAM), _final("t1", "team answer", role="leader"),
        _prompt("h1", "heartbeat", TEAM, metadata={"automation": {"kind": "heartbeat"}}),
        _final("t1", "heartbeat answer", role="leader"),
        # Typed into the turn nobody started: kept, but that turn's answer is not.
        _prompt("m1", "typed into the heartbeat turn", TEAM, sent_during_team_round=True),
        _final("t1", "another heartbeat answer", role="leader"),
        _prompt("g1", "objective", is_goal_objective_message=True), _final("g1", "goal answer"),
    ) == [["U:team prompt", "A:team answer"], ["S:typed into the heartbeat turn"]]


def test_the_history_writer_fills_the_container_as_records_land(session_dir):
    _write(_prompt("a", "first prompt"), _event("a"), _final("a", "first answer"), _done("a"))

    rows = _journal(session_dir)

    history_path = session_dir / "history.jsonl"
    history = history_path.stat()
    assert rows[0] == {"t": "head", "v": 2, "dev": history.st_dev, "ino": history.st_ino}
    assert [(row["t"], row["content"]) for row in rows[1:]] == [
        ("prompt", "first prompt"), ("final", "first answer"),
    ]
    # Each row names where its record ends in the history file.
    lines = history_path.read_bytes().splitlines(keepends=True)
    assert [row["at"] for row in rows[1:]] == [len(lines[0]), sum(map(len, lines[:3]))]
    assert _tasks() == [["U:first prompt", "A:first answer"]]
    assert _journal(session_dir) == rows


def test_no_container_is_kept_without_a_router_and_the_first_read_fills_it(session_dir, monkeypatch):
    monkeypatch.delenv("MODE_ROUTER_URL")
    _write(_prompt("a", "first prompt"), _final("a", "first answer"), _done("a"))
    assert not (session_dir / mode_router_tasks.TASKS_FILENAME).exists()

    assert _tasks() == [["U:first prompt", "A:first answer"]]

    # A routed prompt turned the writer on for this process.
    _write(_prompt("b", "second prompt"), _final("b", "second answer"), _done("b"))
    assert [row["content"] for row in _journal(session_dir)[1:]] == [
        "first prompt", "first answer", "second prompt", "second answer",
    ]
    assert _tasks()[-1] == ["U:second prompt", "A:second answer"]


def test_records_the_writer_did_not_see_are_caught_up_once(session_dir):
    _write(_prompt("a", "first prompt"), _final("a", "first answer"), _done("a"))
    assert len(_tasks()) == 1
    # Another process appends to the history file.
    with (session_dir / "history.jsonl").open("a", encoding="utf-8") as stream:
        for record in (_prompt("b", "second prompt"), _final("b", "second answer")):
            stream.write(json.dumps(record) + "\n")
    _write(_done("b"), _prompt("c", "third prompt"))
    assert [row["content"] for row in _journal(session_dir)[1:]] == ["first prompt", "first answer"]

    expected = [
        ["U:first prompt", "A:first answer"],
        ["U:second prompt", "A:second answer"],
        ["U:third prompt"],
    ]
    assert _tasks() == expected
    _write(_final("c", "third answer"), _done("c"))
    assert _tasks() == [*expected[:2], ["U:third prompt", "A:third answer"]]
    assert [row["t"] for row in _journal(session_dir)] == ["head", *["prompt", "final"] * 3]


def test_the_container_follows_a_replaced_history_file(session_dir):
    """A rewind or restore replaces the history file; the container starts over from it."""
    _write(
        _prompt("a", "first prompt"), _final("a", "first answer"), _done("a"),
        _prompt("b", "second prompt"), _final("b", "second answer"), _done("b"),
    )
    assert len(_tasks()) == 2
    history_path = session_dir / "history.jsonl"
    kept = [json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines()][:3]
    session_history._write_records_to_path(history_path, kept)

    assert _tasks() == [["U:first prompt", "A:first answer"]]
    _write(_prompt("c", "new second prompt"), _final("c", "new second answer"), _done("c"))
    assert _tasks() == [
        ["U:first prompt", "A:first answer"],
        ["U:new second prompt", "A:new second answer"],
    ]


def test_a_damaged_container_is_rebuilt_from_the_history(session_dir):
    _write(_prompt("a", "first prompt"), _final("a", "first answer"), _done("a"))
    expected = [["U:first prompt", "A:first answer"]]
    assert _tasks() == expected
    tasks_path = session_dir / mode_router_tasks.TASKS_FILENAME
    for damage in ("not json\n", "", tasks_path.read_text(encoding="utf-8")[:-10]):
        tasks_path.write_text(damage, encoding="utf-8")
        mode_router_tasks._COVERED.clear()
        assert _tasks() == expected
    tasks_path.unlink()
    assert _tasks() == expected


def test_subagent_transcripts_stay_out_of_the_container(session_dir):
    _write(_prompt("a", "first prompt"))
    session_history._enqueue_history_item(SESSION_ID, _prompt("s", "child prompt"), subagent_id="sub-1")
    session_history._enqueue_history_item(SESSION_ID, _final("s", "child answer"), subagent_id="sub-1")
    _write(_final("a", "first answer"))

    assert _tasks() == [["U:first prompt", "A:first answer"]]
    assert [row["t"] for row in _journal(session_dir)] == ["head", "prompt", "final"]


def test_a_container_of_an_earlier_version_is_rebuilt(session_dir):
    """Version 1 left out the prompts sent into a running turn."""
    _write(_prompt("a", "first prompt"), _prompt("s", "send now", is_supplemental_input=True), _final("a", "answer"))
    tasks_path = session_dir / mode_router_tasks.TASKS_FILENAME
    rows = _journal(session_dir)
    old = [{**rows[0], "v": 1}, rows[1], rows[3]]
    tasks_path.write_text("".join(json.dumps(row) + "\n" for row in old), encoding="utf-8")
    mode_router_tasks._COVERED.clear()

    assert _tasks() == [["U:first prompt", "S:send now", "A:answer"]]
    assert _journal(session_dir) == rows


def _three_tasks():
    """Seven items: a task with a prompt sent into it, a stopped task, an answered task."""
    return (
        _prompt("a", "prompt 1"), _prompt("s", "steer 1", is_supplemental_input=True), _final("a", "answer 1"),
        _prompt("b", "prompt 2"), _done("b", "cancelled"),
        _prompt("c", "prompt 3"), _final("c", "interim 3"), _final("c", "answer 3"),
        _prompt("d", "prompt 4"),
    )


@pytest.mark.parametrize("limit,expected", [
    (0, [["U:prompt 1", "S:steer 1", "A:answer 1"], ["U:prompt 2"], ["U:prompt 3", "A:answer 3"], ["U:prompt 4"]]),
    (7, [["U:prompt 1", "S:steer 1", "A:answer 1"], ["U:prompt 2"], ["U:prompt 3", "A:answer 3"], ["U:prompt 4"]]),
    # The earliest items go out first, also from the middle of a task.
    (6, [["S:steer 1", "A:answer 1"], ["U:prompt 2"], ["U:prompt 3", "A:answer 3"], ["U:prompt 4"]]),
    (5, [["A:answer 1"], ["U:prompt 2"], ["U:prompt 3", "A:answer 3"], ["U:prompt 4"]]),
    (4, [["U:prompt 2"], ["U:prompt 3", "A:answer 3"], ["U:prompt 4"]]),
    (2, [["A:answer 3"], ["U:prompt 4"]]),
    (1, [["U:prompt 4"]]),
])
def test_only_the_latest_items_are_kept(limit, expected):
    rows = [row for row in map(mode_router_tasks.journal_row, _three_tasks()) if row is not None]

    first_row, tasks = mode_router_tasks._keep_latest(rows, limit)

    assert _brief(tasks) == expected
    # What is left of the rows gives the same tasks: the rows of a partly kept task all stay.
    assert _brief(mode_router_tasks._keep_latest(rows[first_row:], limit)[1]) == expected
    assert (first_row == 0) is (limit == 0 or limit >= 5)


def test_the_container_drops_its_earliest_items_as_new_ones_come_in(session_dir, monkeypatch):
    monkeypatch.setattr(mode_router_tasks, "_item_limit", 3)
    _write(*_three_tasks())

    # Written by the history writer alone: the limit holds without a routed prompt.
    rows = _journal(session_dir)
    assert [row.get("content") for row in rows[1:]] == ["prompt 3", "interim 3", "answer 3", "prompt 4"]
    assert _tasks() == [["U:prompt 3", "A:answer 3"], ["U:prompt 4"]]

    _write(_final("d", "answer 4"), _done("d"), _prompt("e", "prompt 5"))
    assert [row.get("content") for row in _journal(session_dir)[1:]] == ["prompt 4", "answer 4", "prompt 5"]
    assert _tasks() == [["U:prompt 4", "A:answer 4"], ["U:prompt 5"]]


def test_a_routed_prompt_sets_the_limit_and_cuts_the_container_to_it(session_dir, monkeypatch):
    monkeypatch.setattr(mode_router_tasks, "_item_limit", 0)
    _write(*_three_tasks())
    assert len(_journal(session_dir)) == 9

    assert _tasks(items=2) == [["A:answer 3"], ["U:prompt 4"]]
    assert [row.get("content") for row in _journal(session_dir)[1:]] == ["prompt 3", "interim 3", "answer 3", "prompt 4"]
    # The history writer keeps to that limit from then on, and nothing comes back or twice.
    _write(_final("d", "answer 4"))
    assert [row.get("content") for row in _journal(session_dir)[1:]] == ["prompt 4", "answer 4"]
    assert _tasks() == [["U:prompt 4", "A:answer 4"]]
    # A larger limit does not bring back what went out of the container ...
    assert _tasks(items=0) == [["U:prompt 4", "A:answer 4"]]
    # ... but the history still has it: a container rebuilt from the history is cut from the full record.
    (session_dir / mode_router_tasks.TASKS_FILENAME).unlink()
    assert _tasks(items=5) == [["U:prompt 2"], ["U:prompt 3", "A:answer 3"], ["U:prompt 4", "A:answer 4"]]


def test_a_session_without_history_has_no_tasks(session_dir):
    assert _tasks() == []
    assert not session_dir.exists()


def test_a_prompt_recorded_during_a_team_round_is_marked(monkeypatch):
    """The history shows no other difference between a prompt to an idle Team and one to a running round."""
    from jiuwenswarm.agents.harness.team import team_manager
    from jiuwenswarm.server.runtime.agent_adapter import interface

    class Manager:
        @staticmethod
        def is_round_active(session_id):
            return session_id == "running"

    monkeypatch.setattr(team_manager, "_team_manager", None)
    assert interface._mark_team_round_input({"skills": []}, "running") == {"skills": []}

    monkeypatch.setattr(team_manager, "_team_manager", Manager())
    assert interface._mark_team_round_input(None, "idle") is None
    assert interface._mark_team_round_input({"skills": []}, "idle") == {"skills": []}
    marked = interface._mark_team_round_input({"skills": []}, "running")
    assert marked == {"skills": [], mode_router_tasks.TEAM_ROUND_INPUT_FIELD: True}
    assert interface._mark_team_round_input(None, "running") == {mode_router_tasks.TEAM_ROUND_INPUT_FIELD: True}
    assert mode_router_tasks.journal_row(_prompt("r", "to the Leader", TEAM, **marked))["during_run"] is True

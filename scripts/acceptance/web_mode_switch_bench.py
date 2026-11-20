"""Drive one Web Session through in-session mode switches and report what happened.

The runner talks to the Gateway exactly as the browser does (channel ``web``):
``session.create`` -> ``chat.send`` -> ``mode.set`` -> ``chat.send`` -> ... ->
``mode.get``. It never calls an Adapter directly, so a run exercises the same
backend path as the Web UI and the TUI.

Example::

    python scripts/acceptance/web_mode_switch_bench.py \\
        --sequence agent.work.normal,team.work.normal,agent.work.normal \\
        --prompt "Remember the code word: violet." \\
        --prompt "Ask a teammate to repeat the code word." \\
        --prompt "What was the code word?" \\
        --out bench.json

Each sequence entry is the mode for one turn: the first creates the Session,
every later entry is committed with ``mode.set`` before its turn. After the
run the report adds the Session's ``metadata.json`` route fields and the
``history.jsonl`` mode-change, usage and error records.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import websockets

PERMISSION_SOURCES = {"permission_interrupt", "confirm_interrupt"}
HISTORY_EVENTS = {"session.mode_changed", "chat.usage_summary", "context.usage", "chat.error"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--web-url", default="ws://127.0.0.1:19000/ws", help="Gateway Web channel URL.")
    parser.add_argument("--user-id", default="", help="Optional Web user_id, sent as the URL query.")
    parser.add_argument(
        "--sequence",
        required=True,
        help="Comma-separated canonical modes, one per turn, e.g. agent.work.normal,team.work.normal.",
    )
    parser.add_argument(
        "--prompt",
        action="append",
        required=True,
        help="Prompt for one turn; repeat once per turn, or give one prompt to reuse for every turn.",
    )
    parser.add_argument("--project-dir", default="", help="Optional project directory for the Session.")
    parser.add_argument("--turn-timeout", type=float, default=600.0, help="Seconds allowed for one turn.")
    parser.add_argument("--busy-retries", type=int, default=10, help="mode.set retries while SESSION_BUSY.")
    parser.add_argument("--busy-delay", type=float, default=1.0, help="Seconds between SESSION_BUSY retries.")
    parser.add_argument("--settle-seconds", type=float, default=2.0, help="Wait before reading Session files.")
    parser.add_argument(
        "--sessions-dir",
        default="",
        help="Server Session root; defaults to this process's JIUWENSWARM_DATA_DIR resolution.",
    )
    parser.add_argument("--no-auto-approve", action="store_true", help="Fail on permission prompts instead.")
    parser.add_argument("--out", default="", help="Write the JSON report here as well as to stdout.")
    args = parser.parse_args(argv)
    args.modes = [mode.strip() for mode in args.sequence.split(",") if mode.strip()]
    if not args.modes:
        parser.error("--sequence needs at least one mode")
    profiles = {mode.split(".")[1] if mode.count(".") == 2 else "" for mode in args.modes}
    if len(profiles) != 1 or profiles & {""}:
        parser.error("every --sequence mode must be canonical and share one work/code profile")
    args.work_mode = profiles.pop()
    if len(args.prompt) not in {1, len(args.modes)}:
        parser.error("give one --prompt, or one per --sequence entry")
    return args


class Channel:
    """One Web connection with request/response correlation and event buffering."""

    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._pending_events: list[dict[str, Any]] = []

    async def request(self, method: str, params: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
        """Send one unary request and return its ``res`` frame; buffer other frames."""
        request_id = f"bench-{method}-{uuid.uuid4().hex[:8]}"
        await self.send(request_id, method, params)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = await self.recv(deadline - time.monotonic())
            if frame.get("type") == "res" and frame.get("id") == request_id:
                return frame
            self._pending_events.append(frame)
        raise TimeoutError(f"{method} did not answer within {timeout}s")

    async def send(self, request_id: str, method: str, params: dict[str, Any]) -> None:
        frame = {"type": "req", "id": request_id, "method": method, "params": params}
        await self._ws.send(json.dumps(frame, ensure_ascii=False))

    async def recv(self, timeout: float) -> dict[str, Any]:
        if self._pending_events:
            return self._pending_events.pop(0)
        raw = await asyncio.wait_for(self._ws.recv(), timeout=max(0.1, timeout))
        frame = json.loads(raw)
        return frame if isinstance(frame, dict) else {}


def _payload(frame: dict[str, Any]) -> dict[str, Any]:
    payload = frame.get("payload")
    return payload if isinstance(payload, dict) else {}


def _event_type(frame: dict[str, Any]) -> str:
    return str(_payload(frame).get("event_type") or frame.get("event") or "")


def _for_session(frame: dict[str, Any], session_id: str) -> bool:
    owner = _payload(frame).get("session_id") or frame.get("session_id")
    return not owner or owner == session_id


def _turn_finished(payload: dict[str, Any]) -> bool:
    """An idle status ends the turn; a Team round must also report completion."""
    return payload.get("is_processing") is False and payload.get("is_complete") is not False


def _base_chat_params(args: argparse.Namespace, session_id: str, mode: str) -> dict[str, Any]:
    params: dict[str, Any] = {"session_id": session_id, "mode": mode, "work_mode": args.work_mode}
    if args.project_dir:
        params.update(project_dir=args.project_dir, cwd=args.project_dir, trusted_dirs=[args.project_dir])
    return params


async def run_turn(
    channel: Channel,
    args: argparse.Namespace,
    session_id: str,
    mode: str,
    prompt: str,
) -> dict[str, Any]:
    """Send one prompt and wait until the Session stops processing it."""
    request_id = f"bench-turn-{uuid.uuid4().hex[:8]}"
    params = _base_chat_params(args, session_id, mode)
    params.update(content=prompt, query=prompt)
    started = time.monotonic()
    await channel.send(request_id, "chat.send", params)
    final_text = ""
    errors: list[dict[str, Any]] = []
    approvals = 0
    events = 0
    settled = False
    deadline = started + args.turn_timeout
    while time.monotonic() < deadline:
        try:
            frame = await channel.recv(deadline - time.monotonic())
        except TimeoutError:
            break
        if frame.get("type") == "res" and frame.get("id") == request_id and not frame.get("ok", True):
            errors.append({"code": frame.get("code"), "error": frame.get("error")})
            settled = True
            break
        if not _for_session(frame, session_id):
            continue
        events += 1
        payload = _payload(frame)
        event_type = _event_type(frame)
        if event_type == "chat.ask_user_question" and payload.get("source") in PERMISSION_SOURCES:
            if args.no_auto_approve:
                errors.append({"code": "PERMISSION_PROMPT", "error": "permission prompt during benchmark"})
                settled = True
                break
            approvals += 1
            answer = _base_chat_params(args, session_id, mode)
            answer.update(
                content="",
                query="",
                request_id=str(payload.get("request_id") or ""),
                source=str(payload.get("source")),
                answers=[{"selected_options": ["session_allow"], "custom_input": ""}],
            )
            await channel.send(f"bench-approve-{uuid.uuid4().hex[:8]}", "chat.send", answer)
            continue
        if event_type == "chat.final" and isinstance(payload.get("content"), str):
            final_text = payload["content"]
        elif event_type == "chat.error":
            errors.append({"code": payload.get("code"), "error": payload.get("error") or payload.get("content")})
        elif event_type == "chat.processing_status" and _turn_finished(payload):
            settled = True
            break
    if not settled:
        errors.append({"code": "TURN_TIMEOUT", "error": f"no idle status within {args.turn_timeout}s"})
    return {
        "kind": "turn",
        "request_id": request_id,
        "mode": mode,
        "prompt": prompt,
        "duration_s": round(time.monotonic() - started, 3),
        "events": events,
        "approvals": approvals,
        "final_chars": len(final_text),
        "final_text": final_text,
        "errors": errors,
    }


async def switch_mode(
    channel: Channel,
    args: argparse.Namespace,
    session_id: str,
    current: str,
    target: str,
) -> dict[str, Any]:
    """Commit ``target`` with mode.set, retrying while the previous turn drains."""
    started = time.monotonic()
    busy_retries = 0
    params = {"session_id": session_id, "mode": target, "expected_mode": current}
    while True:
        frame = await channel.request("mode.set", params)
        if frame.get("ok", False) or frame.get("code") != "SESSION_BUSY" or busy_retries >= args.busy_retries:
            break
        busy_retries += 1
        await asyncio.sleep(args.busy_delay)
    return {
        "kind": "mode.set",
        "expected_mode": current,
        "target_mode": target,
        "ok": bool(frame.get("ok", False)),
        "payload": _payload(frame),
        "code": frame.get("code"),
        "error": frame.get("error"),
        "busy_retries": busy_retries,
        "duration_s": round(time.monotonic() - started, 3),
    }


def read_session_files(sessions_dir: Path, session_id: str) -> dict[str, Any]:
    """Summarize the route fields and mode/usage records the server persisted."""
    session_dir = sessions_dir / session_id
    report: dict[str, Any] = {"session_dir": str(session_dir)}
    metadata_path = session_dir / "metadata.json"
    if metadata_path.is_file():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        report["metadata"] = {
            key: metadata.get(key)
            for key in ("channel_id", "mode", "mode_revision", "work_mode", "project_id", "conversation_handoff")
        }
    history_path = session_dir / "history.jsonl"
    records: dict[str, list[dict[str, Any]]] = {name: [] for name in sorted(HISTORY_EVENTS)}
    if history_path.is_file():
        for line in history_path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_type = record.get("event_type") if isinstance(record, dict) else None
            if event_type in records:
                records[event_type].append(record)
    report["history"] = records
    return report


def default_sessions_dir() -> Path:
    from jiuwenswarm.common.utils import get_agent_sessions_dir

    return Path(get_agent_sessions_dir())


async def run(args: argparse.Namespace) -> dict[str, Any]:
    url = args.web_url
    if args.user_id:
        url = f"{url}{'&' if '?' in url else '?'}{urlencode({'user_id': args.user_id})}"
    prompts = args.prompt * len(args.modes) if len(args.prompt) == 1 else args.prompt
    steps: list[dict[str, Any]] = []
    async with websockets.connect(url, max_size=None, ping_timeout=60) as ws:
        channel = Channel(ws)
        created = await channel.request(
            "session.create",
            {
                "create_token": f"bench-{uuid.uuid4().hex}",
                "mode": args.modes[0],
                "work_mode": args.work_mode,
                "title": "mode switch benchmark",
                **({"project_dir": args.project_dir} if args.project_dir else {}),
            },
        )
        session_id = str(_payload(created).get("session_id") or "")
        if not created.get("ok", False) or not session_id:
            raise RuntimeError(f"session.create failed: {created}")
        current = args.modes[0]
        for index, (mode, prompt) in enumerate(zip(args.modes, prompts)):
            if index and mode != current:
                switched = await switch_mode(channel, args, session_id, current, mode)
                steps.append(switched)
                if not switched["ok"]:
                    break
                current = str(switched["payload"].get("mode") or mode)
            steps.append(await run_turn(channel, args, session_id, current, prompt))
        final_mode = await channel.request("mode.get", {"session_id": session_id})
    await asyncio.sleep(args.settle_seconds)
    sessions_dir = Path(args.sessions_dir) if args.sessions_dir else default_sessions_dir()
    return {
        "session_id": session_id,
        "sequence": args.modes,
        "steps": steps,
        "mode_get": {"ok": final_mode.get("ok"), "payload": _payload(final_mode), "code": final_mode.get("code")},
        "persisted": read_session_files(sessions_dir, session_id),
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = asyncio.run(run(args))
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    failed = any(
        (step["kind"] == "mode.set" and not step["ok"]) or (step["kind"] == "turn" and step["errors"])
        for step in report["steps"]
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

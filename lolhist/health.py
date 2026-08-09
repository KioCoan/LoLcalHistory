"""Watcher health, persisted so a broken capture is visible without log-reading.

This exists because of a real failure: a cross-thread SQLite error made every
capture throw, the watcher caught it, logged it, reconnected, and went on
printing "waiting for games to finish". It looked healthy for an entire session
while recording nothing.

The lesson generalises beyond that one bug. The watcher is a background process
whose success is silent and whose failure is also silent, so it has to leave a
record something else can inspect — `doctor` and the dashboard both read this.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any

from . import config

log = logging.getLogger(__name__)

_LOCK = threading.RLock()
MAX_UNMAPPED = 20


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _path():
    return config.DATA_DIR / "health.json"


def load() -> dict[str, Any]:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(state: dict[str, Any]) -> None:
    config.ensure_dirs()
    try:
        _path().write_text(json.dumps(state, indent=1), encoding="utf-8")
    except OSError as exc:
        # Health reporting must never be the thing that breaks a capture.
        log.debug("could not write health file: %s", exc)


def update(**fields: Any) -> dict[str, Any]:
    with _LOCK:
        state = load()
        state.update(fields)
        _write(state)
        return state


def record_start(account: str | None = None) -> None:
    with _LOCK:
        state = load()
        state.update(
            {
                "watcher_started_at": _now(),
                "watcher_last_seen_at": _now(),
                "account": account,
                "consecutive_failures": 0,
            }
        )
        state.setdefault("captures_ok", 0)
        state.setdefault("captures_failed", 0)
        _write(state)


def record_heartbeat() -> None:
    update(watcher_last_seen_at=_now())


def record_capture(game_id: int, source: str, mode: str | None = None) -> None:
    with _LOCK:
        state = load()
        state["captures_ok"] = int(state.get("captures_ok", 0)) + 1
        state["consecutive_failures"] = 0
        state["last_capture_at"] = _now()
        state["last_capture_game_id"] = game_id
        state["last_capture_source"] = source
        state["last_capture_mode"] = mode
        state["watcher_last_seen_at"] = _now()
        _write(state)


def record_failure(kind: str, message: str) -> int:
    """Record a failed capture and return the new consecutive-failure count."""
    with _LOCK:
        state = load()
        state["captures_failed"] = int(state.get("captures_failed", 0)) + 1
        streak = int(state.get("consecutive_failures", 0)) + 1
        state["consecutive_failures"] = streak
        state["last_error"] = {"at": _now(), "kind": kind, "message": message[:500]}
        state["watcher_last_seen_at"] = _now()
        _write(state)
        return streak


def record_missed() -> None:
    """A game ended but no stats block appeared before the retries ran out.

    Deliberately NOT counted as a failure. Some modes may simply not expose the
    block, and the post-game history sweep covers the game anyway — treating it
    as an error would train you to ignore the warning that matters.
    """
    with _LOCK:
        state = load()
        state["captures_missed"] = int(state.get("captures_missed", 0)) + 1
        state["last_missed_at"] = _now()
        state["watcher_last_seen_at"] = _now()
        _write(state)


def record_unmapped_uri(uri: str) -> None:
    with _LOCK:
        state = load()
        seen = state.get("unmapped_uris") or []
        if uri not in seen:
            seen.append(uri)
            state["unmapped_uris"] = seen[-MAX_UNMAPPED:]
            _write(state)


def is_degraded(state: dict[str, Any] | None = None) -> bool:
    state = load() if state is None else state
    return int(state.get("consecutive_failures", 0)) > 0


def describe(state: dict[str, Any] | None = None) -> list[str]:
    """Human-readable status lines, worst news first."""
    state = load() if state is None else state
    if not state:
        return ["Watcher   : never run"]

    lines: list[str] = []
    streak = int(state.get("consecutive_failures", 0))
    ok = int(state.get("captures_ok", 0))
    failed = int(state.get("captures_failed", 0))

    if streak:
        error = state.get("last_error") or {}
        lines.append(f"Watcher   : DEGRADED - {streak} capture(s) failed in a row")
        lines.append(f"            last error ({error.get('kind')}): {error.get('message')}")
        lines.append(f"            at {error.get('at')}")
    else:
        missed = int(state.get("captures_missed", 0))
        tail = f", {missed} missed (picked up by history sweep)" if missed else ""
        lines.append(f"Watcher   : {ok} captured, {failed} failed{tail}")

    if state.get("last_capture_at"):
        lines.append(
            f"Last catch: game {state.get('last_capture_game_id')} "
            f"({state.get('last_capture_mode') or '?'}) via {state.get('last_capture_source')} "
            f"at {state['last_capture_at']}"
        )
    else:
        lines.append("Last catch: nothing captured yet")

    if state.get("watcher_last_seen_at"):
        lines.append(f"Last seen : {state['watcher_last_seen_at']}")

    unmapped = state.get("unmapped_uris") or []
    if unmapped:
        lines.append(f"Unmapped  : {', '.join(unmapped)}")

    return lines

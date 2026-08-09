"""Sweeping the client's own match history.

This is the path proven to work for ARAM: Mayhem, which the public Riot API
refuses outright. The client keeps only a short recent window, so this catches
games played while the watcher was not running rather than serving as a full
archive on its own.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Iterator

from . import store
from .client import LcuClient
from .models import rank_of
from .normalize import normalize_history_game
from .static_data import StaticData

log = logging.getLogger(__name__)

MATCH_LIST = "/lol-match-history/v1/products/lol/current-summoner/matches"
GAME_DETAIL = "/lol-match-history/v1/games/{game_id}"
PAGE_SIZE = 20


def _games_from_list(payload: Any) -> list[dict]:
    """Dig the game list out of its wrapper.

    The endpoint answers `{"games": {"games": [...]}}`, but the double nesting
    has not always been there, so both shapes are accepted.
    """
    if isinstance(payload, dict):
        games = payload.get("games")
        if isinstance(games, dict):
            inner = games.get("games")
            if isinstance(inner, list):
                return [g for g in inner if isinstance(g, dict)]
        if isinstance(games, list):
            return [g for g in games if isinstance(g, dict)]
    if isinstance(payload, list):
        return [g for g in payload if isinstance(g, dict)]
    return []


def iter_history(client: LcuClient, max_games: int = 200) -> Iterator[dict]:
    """Yield match-history summaries, newest first, until the client runs dry.

    The client retains only a short window and does not always honour
    `begIndex` past the end of it — asking for a later page can hand back the
    same games again. So paging stops as soon as a page contains nothing new,
    rather than trusting the index to advance.
    """
    begin = 0
    yielded = 0
    seen_ids: set[tuple] = set()

    while yielded < max_games:
        end = begin + PAGE_SIZE - 1
        payload = client.get_json_or_none(MATCH_LIST, begIndex=begin, endIndex=end)
        games = _games_from_list(payload)
        if not games:
            return

        fresh = 0
        for game in games:
            key = (game.get("gameId"), game.get("platformId"))
            if key in seen_ids:
                continue
            seen_ids.add(key)
            fresh += 1
            yield game
            yielded += 1
            if yielded >= max_games:
                return

        if fresh == 0 or len(games) < PAGE_SIZE:
            return
        begin += PAGE_SIZE


def run(
    client: LcuClient,
    conn: sqlite3.Connection,
    static: StaticData,
    max_games: int = 200,
    refetch: bool = False,
) -> dict[str, int]:
    """Store every history game not already held at equal or better detail."""
    known = store.known_keys(conn)
    history_rank = rank_of("history")
    counts = {"seen": 0, "inserted": 0, "upgraded": 0, "skipped": 0, "failed": 0}

    for summary in iter_history(client, max_games=max_games):
        counts["seen"] += 1
        game_id = summary.get("gameId")
        if not isinstance(game_id, int):
            counts["failed"] += 1
            continue

        platform_id = str(summary.get("platformId") or "")
        existing = known.get((game_id, platform_id))

        if existing is not None and existing == history_rank and not refetch:
            counts["skipped"] += 1
            continue

        if existing is not None and existing > history_rank and not refetch:
            # Already held from a better source. Still worth passing through,
            # because that source may lack the queue id, map id and version
            # that the list entry carries — `upsert_match` fills only blanks.
            # The summary alone is enough for that, so no detail fetch.
            payload, detail = summary, None
        else:
            # The per-game endpoint carries all ten players; the list entry has
            # only you. A failed detail fetch costs detail, not the game.
            detail = client.get_json_or_none(GAME_DETAIL.format(game_id=game_id))
            payload = detail if detail else summary
            if detail is None:
                log.debug("no detail for game %s; falling back to the list entry", game_id)

        try:
            match = normalize_history_game(payload, static, platform_hint=platform_id)
        except Exception as exc:
            log.warning("could not parse game %s: %s", game_id, exc)
            counts["failed"] += 1
            continue

        if not match.game_id:
            counts["failed"] += 1
            continue

        match.raw_path = store.archive_raw(payload, match.game_id, match.platform_id, "history")
        outcome = store.upsert_match(conn, match)
        counts[outcome] = counts.get(outcome, 0) + 1

    return counts

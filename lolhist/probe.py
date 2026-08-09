"""Capture real payloads before writing anything that depends on their shape.

Run this with the client open — ideally right after a game, while the
end-of-game screen is still up. It dumps everything verbatim to data/samples/
and reports which modes the match-history endpoint actually lists, which is the
open question for League Classic and ARAM: Mayhem.
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import config
from .backfill import GAME_DETAIL, MATCH_LIST, _games_from_list
from .client import LcuClient
from .static_data import AUGMENT_ASSETS, CHAMPION_ASSET, QUEUES_ENDPOINT

PROBE_ENDPOINTS: tuple[tuple[str, dict], ...] = (
    ("/lol-summoner/v1/current-summoner", {}),
    ("/lol-gameflow/v1/gameflow-phase", {}),
    ("/lol-gameflow/v1/session", {}),
    ("/lol-end-of-game/v1/eog-stats-block", {}),
    (MATCH_LIST, {"begIndex": 0, "endIndex": 19}),
    (QUEUES_ENDPOINT, {}),
    (CHAMPION_ASSET, {}),
    *((asset, {}) for asset in AUGMENT_ASSETS),
)


def _slug(path: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", path).strip("-").lower()


def _save(path: str, payload: Any) -> str:
    target = config.SAMPLES_DIR / f"{_slug(path)}.json"
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(target)


def run(client: LcuClient) -> None:
    config.ensure_dirs()
    print(f"Writing samples to {config.SAMPLES_DIR}\n")

    history_payload: Any = None
    for path, params in PROBE_ENDPOINTS:
        payload = client.get_json_or_none(path, **params)
        if payload is None:
            print(f"  --  {path}  (not available right now)")
            continue
        _save(path, payload)
        print(f"  ok  {path}")
        if path == MATCH_LIST:
            history_payload = payload

    games = _games_from_list(history_payload)
    if games:
        detail = client.get_json_or_none(GAME_DETAIL.format(game_id=games[0].get("gameId")))
        if detail is not None:
            _save("game-detail", detail)
            print("  ok  /lol-match-history/v1/games/{id}")

    _report_modes(games)


def _report_modes(games: list[dict]) -> None:
    """Show which modes the client's match history actually lists.

    This is the answer to whether League Classic and ARAM: Mayhem are reachable
    through the history endpoint, or only via end-of-game capture.
    """
    if not games:
        print("\nNo games in the client's match history right now.")
        return

    seen: dict[tuple, int] = {}
    for game in games:
        key = (game.get("queueId"), game.get("gameMode"), game.get("gameType"))
        seen[key] = seen.get(key, 0) + 1

    print(f"\nModes present in the last {len(games)} history entries:")
    for (queue_id, mode, game_type), count in sorted(seen.items(), key=lambda kv: -kv[1]):
        print(f"  {count:>3}x  queueId={queue_id}  gameMode={mode}  gameType={game_type}")
    print(
        "\nIf a mode you played is missing here, it is end-of-game capture only "
        "— keep `watch` running for it."
    )

"""Champion, queue and augment names, served by the client itself.

Deliberately not Data Dragon or CommunityDragon. Those would be outbound
network calls, and they lag or lead the patch you are actually playing. The
client already ships this data for its own UI, so we read it from there and
cache it locally.

Names are always a convenience layer: the numeric ID is what gets stored, so an
unrecognised new augment degrades to "Augment 137" rather than vanishing.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from . import config
from .client import LcuClient

log = logging.getLogger(__name__)

CHAMPION_ASSET = "/lol-game-data/assets/v1/champion-summary.json"
QUEUES_ENDPOINT = "/lol-game-queues/v1/queues"

# Arena-style augments live in cherry-augments.json. Mayhem reuses the augment
# system, and may or may not ship its own file — every candidate that 404s is
# skipped, so listing a plausible one costs nothing and catches it if it exists.
AUGMENT_ASSETS = (
    "/lol-game-data/assets/v1/cherry-augments.json",
    "/lol-game-data/assets/v1/mayhem-augments.json",
    "/lol-game-data/assets/v1/augments.json",
)


class StaticData:
    def __init__(
        self,
        champions: dict[int, str] | None = None,
        queues: dict[int, str] | None = None,
        augments: dict[int, str] | None = None,
    ) -> None:
        self.champions = champions or {}
        self.queues = queues or {}
        self.augments = augments or {}

    def champion(self, champion_id: int | None) -> str | None:
        return self.champions.get(champion_id) if champion_id is not None else None

    def queue(self, queue_id: int | None) -> str | None:
        return self.queues.get(queue_id) if queue_id is not None else None

    def augment(self, augment_id: int | None) -> str | None:
        return self.augments.get(augment_id) if augment_id is not None else None

    def summary(self) -> str:
        return (
            f"{len(self.champions)} champions, {len(self.queues)} queues, "
            f"{len(self.augments)} augments"
        )


def _cache_path(name: str):
    return config.STATIC_DIR / f"{name}.json"


def _cache_is_fresh(name: str) -> bool:
    path = _cache_path(name)
    if not path.exists():
        return False
    age_days = (time.time() - path.stat().st_mtime) / 86400
    return age_days < config.STATIC_MAX_AGE_DAYS


def _write_cache(name: str, payload: Any) -> None:
    config.ensure_dirs()
    _cache_path(name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _read_cache(name: str) -> Any | None:
    path = _cache_path(name)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("static cache %s is unreadable; ignoring", path)
        return None


def _as_records(payload: Any) -> list[dict]:
    """Accept either a bare list or an object wrapping one.

    cherry-augments.json is `{"augments": [...]}` while champion-summary.json is
    a plain list, and Riot has moved things between the two shapes before.
    """
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("augments", "data", "items", "queues"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        # A dict keyed by id, e.g. {"1": {...}}
        return [item for item in payload.values() if isinstance(item, dict)]
    return []


def _index_names(payload: Any, name_keys: tuple[str, ...]) -> dict[int, str]:
    index: dict[int, str] = {}
    for record in _as_records(payload):
        raw_id = record.get("id")
        if raw_id is None:
            continue
        try:
            key = int(raw_id)
        except (TypeError, ValueError):
            continue
        for name_key in name_keys:
            name = record.get(name_key)
            if isinstance(name, str) and name.strip():
                index[key] = name.strip()
                break
    return index


def refresh(client: LcuClient) -> None:
    """Pull fresh asset data from the client and cache it."""
    champions = client.get_json_or_none(CHAMPION_ASSET)
    if champions is not None:
        _write_cache("champions", champions)

    queues = client.get_json_or_none(QUEUES_ENDPOINT)
    if queues is not None:
        _write_cache("queues", queues)

    merged: list[dict] = []
    for asset in AUGMENT_ASSETS:
        payload = client.get_json_or_none(asset)
        if payload is None:
            continue
        log.debug("augment asset %s returned %d records", asset, len(_as_records(payload)))
        merged.extend(_as_records(payload))
    if merged:
        _write_cache("augments", merged)


def load(client: LcuClient | None = None) -> StaticData:
    """Load names, refreshing from the client when it is available and stale."""
    if client is not None:
        stale = not all(_cache_is_fresh(name) for name in ("champions", "queues", "augments"))
        if stale:
            try:
                refresh(client)
            except Exception as exc:  # never let a name lookup break a capture
                log.warning("could not refresh static data: %s", exc)

    return StaticData(
        champions=_index_names(_read_cache("champions"), ("name", "alias")),
        queues=_index_names(_read_cache("queues"), ("name", "shortName", "description")),
        augments=_index_names(_read_cache("augments"), ("nameTRA", "name")),
    )

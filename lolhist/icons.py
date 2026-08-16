"""A local mirror of the client's own icon art.

The dashboard shows champion portraits, item builds and your profile picture.
None of it is fetched from the web: the client already ships every image for its
own UI, so they are copied out of it once and served from disk afterwards. That
keeps the art offline — nothing here contacts Riot, Data Dragon or a CDN — and
means the page still renders with League closed, which is most of the time you
would read it.

Only what your history actually references is copied. A fresh install mirrors a
couple of hundred small PNGs on its first sync and almost nothing after that.

Every icon is optional by design. A missing file 404s and the page falls back to
the name it was already showing, so a friend who has not run the watcher yet
sees the dashboard exactly as it was before this existed.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Iterable

from . import config, static_data, store
from .client import LcuClient

log = logging.getLogger(__name__)

# Champion and profile art is addressed by id directly. Items and spells are
# not: their art lives at arbitrary paths that only the asset file knows.
CHAMPION_URL = "/lol-game-data/assets/v1/champion-icons/{key}.png"
PROFILE_URL = "/lol-game-data/assets/v1/profile-icons/{key}.jpg"

# One extension per kind, so serving a file needs no lookup. Matches what the
# client actually returns for each.
KINDS = {
    "champion": ".png",
    "item": ".png",
    "spell": ".png",
    "profile": ".jpg",
    # Runes. `perk` is one rune, `perkstyle` is a whole tree. Neither is ever
    # referenced by the database — a live match is not stored — so these are
    # mirrored through `ensure()` rather than `sync()`.
    "perk": ".png",
    "perkstyle": ".png",
    # League Classic's runes are the old marks/seals/glyphs/quintessences, with
    # their own ids and their own asset file, so they cannot share `perk`.
    "jaderune": ".png",
}

# A ceiling on one sync, so a first run against a large history cannot spin for
# a long time before the watcher gets on with its actual job. Whatever is left
# is picked up by the next sync; the count is logged rather than swallowed.
MAX_PER_SYNC = 800


def directory(kind: str):
    return config.STATIC_DIR / "icons" / kind


def path_for(kind: str, key: int):
    """Where an icon lives on disk, or None if the kind is not one we mirror."""
    suffix = KINDS.get(kind)
    if suffix is None:
        return None
    return directory(kind) / f"{int(key)}{suffix}"


def cached(kind: str, key: int) -> bool:
    path = path_for(kind, key)
    return bool(path and path.exists() and path.stat().st_size > 0)


def referenced(conn: sqlite3.Connection) -> dict[str, set[int]]:
    """Every icon this database would like to show.

    Covers all participants, not just you: the expanded match row lists both
    teams with their champions and builds.
    """
    items = " UNION ".join(
        f"SELECT item{slot} AS id FROM participants WHERE item{slot} > 0" for slot in range(7)
    )
    queries = {
        "champion": "SELECT DISTINCT champion_id AS id FROM participants WHERE champion_id > 0",
        "item": items,
        "spell": (
            "SELECT DISTINCT spell1_id AS id FROM participants WHERE spell1_id > 0"
            " UNION SELECT DISTINCT spell2_id FROM participants WHERE spell2_id > 0"
        ),
        "profile": "SELECT profile_icon_id AS id FROM me WHERE profile_icon_id > 0",
    }

    wanted: dict[str, set[int]] = {}
    # Under the store's lock. This runs on a worker thread against the same
    # connection the watcher writes captures through, and reading it unlocked
    # while a capture was in flight corrupted the database — two b-trees
    # claiming the same pages. Mirroring some art is never worth that.
    with store.lock():
        for kind, sql in queries.items():
            try:
                wanted[kind] = {row[0] for row in conn.execute(sql) if row[0]}
            except sqlite3.Error as exc:
                # A column added by a later migration may be absent on an old file.
                log.debug("cannot list %s icons: %s", kind, exc)
                wanted[kind] = set()
    return wanted


# Kinds whose art lives at an arbitrary path only the asset file knows, and the
# cache each one's paths are read from. Champion and profile art is addressed by
# id directly, so neither appears here.
ASSET_BACKED = {
    "item": "items",
    "spell": "spells",
    "perk": "perks",
    "perkstyle": "perkstyles",
    "jaderune": "jaderunes",
}


def _asset_paths(kinds: Iterable[str]) -> dict[str, dict]:
    """`{kind: {id: iconPath}}`, read from the caches on disk.

    Loaded once per sync rather than per icon: each of these files is a few
    hundred KB of JSON, and there are hundreds of icons to resolve against them.
    """
    return {
        kind: static_data.asset_map(ASSET_BACKED[kind], "iconPath")
        for kind in kinds
        if kind in ASSET_BACKED
    }


def _url_for(kind: str, key: int, paths: dict[str, dict]) -> str | None:
    if kind == "champion":
        return CHAMPION_URL.format(key=key)
    if kind == "profile":
        return PROFILE_URL.format(key=key)
    return paths.get(kind, {}).get(key)


def _fetch_one(client: LcuClient, kind: str, key: int, paths: dict[str, dict]) -> bool:
    """Copy one icon out of the client. False means "not available", not "broken".

    Never raises. A missing asset entry is ordinary — a mode-specific item whose
    file the client does not ship, most likely — and so is a failed request.
    """
    url = _url_for(kind, key, paths)
    if not url:
        return False
    try:
        response = client.get(url)
        if response.status_code >= 400 or not response.content:
            return False
        path_for(kind, key).write_bytes(response.content)
        return True
    except Exception:
        log.debug("could not mirror %s icon %s", kind, key, exc_info=True)
        return False


def ensure(client: LcuClient, kind: str, ids: Iterable[int]) -> int:
    """Mirror specific icons now, for art the database never references.

    `sync` works from what is stored, which cannot cover a live match: nothing
    about the game you are playing is written down, so its runes would never be
    asked for. This takes the ids straight from the caller instead.

    Cheap on the common path — once a keystone has been copied it is skipped
    without touching the client, and there are only ten players in a game.
    """
    if kind not in KINDS:
        return 0
    wanted = {int(i) for i in ids if i}
    missing = {i for i in wanted if not cached(kind, i)}
    if not missing:
        return 0

    directory(kind).mkdir(parents=True, exist_ok=True)
    paths = _asset_paths([kind])
    fetched = sum(1 for key in sorted(missing) if _fetch_one(client, kind, key, paths))
    if fetched:
        log.debug("mirrored %d new %s icon(s)", fetched, kind)
    return fetched


def sync(client: LcuClient, conn: sqlite3.Connection) -> dict[str, int]:
    """Copy any referenced icon that is not on disk yet.

    Never raises: art is a nicety, and a failure here must not cost a capture.
    """
    counts = {"fetched": 0, "missing": 0, "skipped": 0}

    wanted = referenced(conn)
    paths = _asset_paths(wanted)

    for kind, keys in wanted.items():
        directory(kind).mkdir(parents=True, exist_ok=True)
        for key in sorted(keys):
            if cached(kind, key):
                continue
            if counts["fetched"] >= MAX_PER_SYNC:
                counts["skipped"] += 1
                continue
            if _fetch_one(client, kind, key, paths):
                counts["fetched"] += 1
            else:
                counts["missing"] += 1

    if counts["skipped"]:
        log.info(
            "icon sync stopped at %d this round; %d left for the next one",
            MAX_PER_SYNC, counts["skipped"],
        )
    if counts["fetched"]:
        log.info("mirrored %d new icon(s) from the client", counts["fetched"])
    return counts

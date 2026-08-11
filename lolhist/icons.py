"""A local mirror of the client's own icon art.

The dashboard shows champion portraits, item builds and your profile picture.
None of it is fetched from the web: the client already ships every image for its
own UI, so they are copied out of it once and served from disk afterwards. That
keeps the hard rule intact — no outbound network calls — and means the page
still renders with League closed, which is most of the time you would read it.

Only what your history actually references is copied. A fresh install mirrors a
couple of hundred small PNGs on its first sync and almost nothing after that.

Every icon is optional by design. A missing file 404s and the page falls back to
the name it was already showing, so a friend who has not run the watcher yet
sees the dashboard exactly as it was before this existed.
"""

from __future__ import annotations

import logging
import sqlite3

from . import config, static_data
from .client import LcuClient

log = logging.getLogger(__name__)

# Champion and profile art is addressed by id directly. Items and spells are
# not: their art lives at arbitrary paths that only the asset file knows.
CHAMPION_URL = "/lol-game-data/assets/v1/champion-icons/{key}.png"
PROFILE_URL = "/lol-game-data/assets/v1/profile-icons/{key}.jpg"

# One extension per kind, so serving a file needs no lookup. Matches what the
# client actually returns for each.
KINDS = {"champion": ".png", "item": ".png", "spell": ".png", "profile": ".jpg"}

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
    for kind, sql in queries.items():
        try:
            wanted[kind] = {row[0] for row in conn.execute(sql) if row[0]}
        except sqlite3.Error as exc:
            # A column added by a later migration may not exist on an old file.
            log.debug("cannot list %s icons: %s", kind, exc)
            wanted[kind] = set()
    return wanted


def _url_for(kind: str, key: int, item_paths: dict, spell_paths: dict) -> str | None:
    if kind == "champion":
        return CHAMPION_URL.format(key=key)
    if kind == "profile":
        return PROFILE_URL.format(key=key)
    if kind == "item":
        return item_paths.get(key)
    if kind == "spell":
        return spell_paths.get(key)
    return None


def sync(client: LcuClient, conn: sqlite3.Connection) -> dict[str, int]:
    """Copy any referenced icon that is not on disk yet.

    Never raises: art is a nicety, and a failure here must not cost a capture.
    """
    counts = {"fetched": 0, "missing": 0, "skipped": 0}

    item_paths = static_data.asset_map("items", "iconPath")
    spell_paths = static_data.asset_map("spells", "iconPath")

    for kind, keys in referenced(conn).items():
        directory(kind).mkdir(parents=True, exist_ok=True)
        for key in sorted(keys):
            if cached(kind, key):
                continue
            if counts["fetched"] >= MAX_PER_SYNC:
                counts["skipped"] += 1
                continue

            url = _url_for(kind, key, item_paths, spell_paths)
            if not url:
                # No asset entry for this id — a mode-specific item whose file
                # the client does not ship, most likely. Nothing to fetch.
                counts["missing"] += 1
                continue
            try:
                response = client.get(url)
                if response.status_code >= 400 or not response.content:
                    counts["missing"] += 1
                    continue
                path_for(kind, key).write_bytes(response.content)
                counts["fetched"] += 1
            except Exception:
                log.debug("could not mirror %s icon %s", kind, key, exc_info=True)
                counts["missing"] += 1

    if counts["skipped"]:
        log.info(
            "icon sync stopped at %d this round; %d left for the next one",
            MAX_PER_SYNC, counts["skipped"],
        )
    if counts["fetched"]:
        log.info("mirrored %d new icon(s) from the client", counts["fetched"])
    return counts

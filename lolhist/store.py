"""SQLite persistence and the gzipped raw-payload archive.

Every capture is archived verbatim before it is parsed. The LCU is unsupported
and its payload shapes drift between patches; keeping the raw JSON means a shape
change costs a re-parse of files we already hold, never lost history.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import config
from .models import Match, Participant

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

# The watcher runs its captures and history sweeps on worker threads (so a slow
# write cannot stall the WebSocket event loop) while holding a connection opened
# on the main thread. SQLite rejects that by default, so the connection is
# opened thread-agnostic and every statement is serialized through this lock
# instead. Re-entrant because upserts call into the helpers below.
_DB_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Columns added after the first release. `CREATE TABLE IF NOT EXISTS` does
# nothing to a table that already exists, so an existing database needs these
# added explicitly or every query naming them fails.
_ADDED_COLUMNS = {
    "matches": (
        ("my_rank_queue", "TEXT"),
        ("my_lp_before", "INTEGER"),
        ("my_tier_before", "TEXT"),
        ("my_division_before", "TEXT"),
        ("my_lp_delta", "INTEGER"),
        ("my_lp_after", "INTEGER"),
        ("my_tier_after", "TEXT"),
        ("my_division_after", "TEXT"),
    ),
    "me": (
        ("profile_icon_id", "INTEGER"),
    ),
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue  # table not created yet; the schema script will handle it
        for name, decl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    _migrate_rank_progress(conn)


def _migrate_rank_progress(conn: sqlite3.Connection) -> None:
    """Attribute the rank series to an account.

    `puuid` belongs in the primary key rather than merely alongside it, and
    SQLite cannot alter a key in place, so this rebuilds the table. Existing
    rows predate multi-account support, which means exactly one account can
    have written them — whichever is in `me` — so the copy is lossless.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(rank_progress)")}
    if not existing or "puuid" in existing:
        return  # fresh database, or already migrated

    owner = conn.execute("SELECT puuid FROM me ORDER BY updated_at DESC LIMIT 1").fetchone()
    # An empty `me` leaves the observations unattributed rather than guessing.
    # They then belong to nobody, which is honest: no account will diff against
    # them, and the next reading starts a clean series.
    owner_puuid = owner["puuid"] if owner else ""

    conn.execute(
        """
        CREATE TABLE rank_progress_migrating (
            puuid         TEXT    NOT NULL,
            taken_at      TEXT    NOT NULL,
            queue_type    TEXT    NOT NULL,
            tier          TEXT,
            division      TEXT,
            league_points INTEGER,
            wins          INTEGER,
            losses        INTEGER,
            PRIMARY KEY (puuid, taken_at, queue_type)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO rank_progress_migrating
            (puuid, taken_at, queue_type, tier, division, league_points, wins, losses)
        SELECT ?, taken_at, queue_type, tier, division, league_points, wins, losses
        FROM rank_progress
        """,
        (owner_puuid,),
    )
    conn.execute("DROP TABLE rank_progress")
    conn.execute("ALTER TABLE rank_progress_migrating RENAME TO rank_progress")


def open_db(path: Path | None = None) -> sqlite3.Connection:
    """Open the database, creating it and its schema if needed."""
    config.ensure_dirs()
    conn = sqlite3.connect(path or config.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    with _DB_LOCK:
        conn.execute("PRAGMA foreign_keys = ON")
        # Migrate before the schema script: it recreates the views, which would
        # fail if they reference columns the old tables do not have yet.
        _migrate(conn)
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()
    return conn


def archive_raw(payload: Any, game_id: int | str, platform_id: str, source: str) -> str:
    """Write a payload to the raw archive and return its path, relative to data/."""
    config.ensure_dirs()
    stem = f"{platform_id or 'unknown'}-{game_id}-{source}.json.gz"
    target = config.RAW_DIR / stem
    with gzip.open(target, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    return str(target.relative_to(config.DATA_DIR))


def read_raw(relative_path: str) -> Any:
    with gzip.open(config.DATA_DIR / relative_path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def set_me(
    conn: sqlite3.Connection,
    puuid: str,
    game_name: str | None,
    tagline: str | None,
    profile_icon_id: int | None = None,
) -> None:
    with _DB_LOCK:
        conn.execute(
            """
            INSERT INTO me (puuid, riot_id_game_name, riot_id_tagline,
                            profile_icon_id, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(puuid) DO UPDATE SET
                riot_id_game_name = excluded.riot_id_game_name,
                riot_id_tagline   = excluded.riot_id_tagline,
                -- Keeps the last known picture when a caller does not supply
                -- one, so the header does not lose its avatar.
                profile_icon_id   = COALESCE(excluded.profile_icon_id,
                                             me.profile_icon_id),
                updated_at        = excluded.updated_at
            """,
            (puuid, game_name, tagline, profile_icon_id, _now()),
        )
        conn.commit()


def my_puuids(conn: sqlite3.Connection) -> set[str]:
    with _DB_LOCK:
        return {row["puuid"] for row in conn.execute("SELECT puuid FROM me")}


def active_puuid(conn: sqlite3.Connection) -> str | None:
    """The account most recently seen logged in.

    `set_me` runs on every session connect, so `updated_at` already tracks this
    — the account currently in the client keeps being touched while the others
    go stale. No extra bookkeeping needed.
    """
    with _DB_LOCK:
        row = conn.execute(
            "SELECT puuid FROM me ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
    return row["puuid"] if row else None


def accounts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every account this database holds, most recently seen first."""
    with _DB_LOCK:
        rows = conn.execute(
            """
            SELECT me.puuid, me.riot_id_game_name, me.riot_id_tagline,
                   me.profile_icon_id, me.updated_at,
                   (SELECT COUNT(*) FROM v_my_matches v WHERE v.puuid = me.puuid) AS games
            FROM me
            ORDER BY me.updated_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def known_keys(conn: sqlite3.Connection) -> dict[tuple[int, str], int]:
    """Every stored match keyed by (game_id, platform_id), valued by source rank."""
    with _DB_LOCK:
        return {
            (row["game_id"], row["platform_id"]): row["source_rank"]
            for row in conn.execute("SELECT game_id, platform_id, source_rank FROM matches")
        }


def match_count(conn: sqlite3.Connection) -> int:
    with _DB_LOCK:
        return conn.execute("SELECT COUNT(*) AS n FROM matches").fetchone()["n"]


def upsert_match(conn: sqlite3.Connection, match: Match) -> str:
    """Store a match. Returns 'inserted', 'upgraded' or 'enriched'.

    A capture from a weaker source never overwrites a stronger one, so a
    backfill sweep running after the watcher cannot degrade rows that already
    carry full end-of-game detail.

    It is still allowed to fill in blanks, though, and it has to be: the two
    payloads know different things. The end-of-game block has every player's
    augments but no queue id, map id or platform; the history entry has all
    three. Discarding the weaker capture outright would leave Mayhem games
    permanently labelled "KIWI" with a null queue.
    """
    with _DB_LOCK:
        return _upsert_match_locked(conn, match)


def _upsert_match_locked(conn: sqlite3.Connection, match: Match) -> str:
    row = conn.execute(
        "SELECT source_rank FROM matches WHERE game_id = ? AND platform_id = ?",
        match.key,
    ).fetchone()

    if row is not None and row["source_rank"] > match.rank:
        _fill_blanks(conn, match)
        conn.commit()
        return "enriched"
    outcome = "inserted" if row is None else "upgraded"

    conn.execute(
        """
        INSERT INTO matches (
            game_id, platform_id, queue_id, queue_name, game_mode, game_type,
            map_id, game_creation_ms, game_duration_s, game_version,
            winning_team_id, source, source_rank, captured_at, raw_path
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(game_id, platform_id) DO UPDATE SET
            -- COALESCE, not plain assignment. A better source is better at
            -- describing players, but it is not necessarily more complete about
            -- the match: the end-of-game block has no queue id, map id or
            -- version, and overwriting those with its nulls loses real data.
            queue_id         = COALESCE(excluded.queue_id, matches.queue_id),
            queue_name       = COALESCE(excluded.queue_name, matches.queue_name),
            game_mode        = COALESCE(excluded.game_mode, matches.game_mode),
            game_type        = COALESCE(excluded.game_type, matches.game_type),
            map_id           = COALESCE(excluded.map_id, matches.map_id),
            game_creation_ms = COALESCE(excluded.game_creation_ms, matches.game_creation_ms),
            game_duration_s  = COALESCE(excluded.game_duration_s, matches.game_duration_s),
            game_version     = COALESCE(excluded.game_version, matches.game_version),
            winning_team_id  = COALESCE(excluded.winning_team_id, matches.winning_team_id),
            source           = excluded.source,
            source_rank      = excluded.source_rank,
            captured_at      = excluded.captured_at,
            raw_path         = excluded.raw_path
        """,
        (
            match.game_id, match.platform_id, match.queue_id, match.queue_name,
            match.game_mode, match.game_type, match.map_id, match.game_creation_ms,
            match.game_duration_s, match.game_version, match.winning_team_id,
            match.source, match.rank, _now(), match.raw_path,
        ),
    )

    # Replace rather than merge the children: a richer source may describe more
    # participants or more augment slots, and a partial merge would leave stale
    # rows behind from the weaker capture.
    conn.execute(
        "DELETE FROM participant_augments WHERE game_id = ? AND platform_id = ?", match.key
    )
    conn.execute("DELETE FROM participants WHERE game_id = ? AND platform_id = ?", match.key)

    for participant in match.participants:
        _insert_participant(conn, match, participant)

    _touch_players(conn, match.participants)
    conn.commit()
    return outcome


def save_player_ranks(conn: sqlite3.Connection, ranks_by_puuid: dict) -> int:
    """Record the latest known rank for each player and queue."""
    now = _now()
    written = 0
    with _DB_LOCK:
        for puuid, ranks in ranks_by_puuid.items():
            for rank in ranks.values():
                conn.execute(
                    """
                    INSERT INTO player_ranks
                        (puuid, queue_type, tier, division, league_points,
                         wins, losses, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(puuid, queue_type) DO UPDATE SET
                        tier = excluded.tier, division = excluded.division,
                        league_points = excluded.league_points,
                        wins = excluded.wins, losses = excluded.losses,
                        updated_at = excluded.updated_at
                    """,
                    (
                        puuid, rank.queue_type, rank.tier, rank.division,
                        rank.league_points, rank.wins, rank.losses, now,
                    ),
                )
                written += 1
        conn.commit()
    return written


def save_participant_ranks(
    conn: sqlite3.Connection, match: Match, ranks_by_puuid: dict
) -> int:
    """Pin each player's rank as it stood for this game."""
    written = 0
    with _DB_LOCK:
        for participant in match.participants:
            ranks = ranks_by_puuid.get(participant.puuid or "")
            if not ranks:
                continue
            for rank in ranks.values():
                if not rank.is_ranked:
                    continue
                conn.execute(
                    """
                    INSERT INTO participant_ranks
                        (game_id, platform_id, participant_id, queue_type,
                         tier, division, league_points)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(game_id, platform_id, participant_id, queue_type)
                    DO UPDATE SET tier = excluded.tier, division = excluded.division,
                                  league_points = excluded.league_points
                    """,
                    (
                        match.game_id, match.platform_id, participant.participant_id,
                        rank.queue_type, rank.tier, rank.division, rank.league_points,
                    ),
                )
                written += 1
        conn.commit()
    return written


def save_rank_progress(conn: sqlite3.Connection, ranks: dict, puuid: str) -> None:
    """Append an observation of one account's rank."""
    if not puuid:
        return  # an unattributed observation would pollute somebody's series
    now = _now()
    with _DB_LOCK:
        for rank in ranks.values():
            if not rank.is_ranked:
                continue
            conn.execute(
                """
                INSERT OR REPLACE INTO rank_progress
                    (puuid, taken_at, queue_type, tier, division, league_points,
                     wins, losses)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    puuid, now, rank.queue_type, rank.tier, rank.division,
                    rank.league_points, rank.wins, rank.losses,
                ),
            )
        conn.commit()


def latest_rank_progress(conn: sqlite3.Connection, puuid: str) -> dict:
    """The most recent observation of each of one account's ladders."""
    from .ranked import Rank

    if not puuid:
        return {}

    with _DB_LOCK:
        rows = conn.execute(
            """
            SELECT queue_type, tier, division, league_points, wins, losses
            FROM rank_progress
            WHERE puuid = ?
              AND (queue_type, taken_at) IN (
                SELECT queue_type, MAX(taken_at) FROM rank_progress
                WHERE puuid = ? GROUP BY queue_type
            )
            """,
            (puuid, puuid),
        ).fetchall()
    return {
        row["queue_type"]: Rank(
            queue_type=row["queue_type"], tier=row["tier"], division=row["division"],
            league_points=row["league_points"], wins=row["wins"], losses=row["losses"],
        )
        for row in rows
    }


def record_lp_change(
    conn: sqlite3.Connection, match_key: tuple, queue_type: str, delta: int, after
) -> None:
    with _DB_LOCK:
        conn.execute(
            """
            UPDATE matches SET
                my_rank_queue     = ?,
                my_lp_delta       = ?,
                my_lp_after       = ?,
                my_tier_after     = ?,
                my_division_after = ?
            WHERE game_id = ? AND platform_id = ?
            """,
            (
                queue_type, delta, after.league_points, after.tier, after.division,
                match_key[0], match_key[1],
            ),
        )
        conn.commit()


def record_rank_before(
    conn: sqlite3.Connection, match_key: tuple, queue_type: str, before
) -> None:
    """Pin the rank you held going into this game.

    Written immediately at capture. The LP a game awarded may not have landed
    yet, and waiting for it inside the running session means losing the reading
    entirely if the app closes first — with the "before" on record, the delta
    can be completed on any later launch.
    """
    with _DB_LOCK:
        conn.execute(
            """
            UPDATE matches SET
                my_rank_queue      = ?,
                my_lp_before       = ?,
                my_tier_before     = ?,
                my_division_before = ?
            WHERE game_id = ? AND platform_id = ? AND my_lp_delta IS NULL
            """,
            (
                queue_type, before.league_points, before.tier, before.division,
                match_key[0], match_key[1],
            ),
        )
        conn.commit()


def pending_lp_match(conn: sqlite3.Connection, puuid: str, max_age_hours: int = 6):
    """The most recent game one account is still waiting on an LP change for.

    Only ever one, and only a recent one: if several ranked games have gone by
    unsettled, the current rank cannot say which game moved what, and a guess
    would be worse than leaving it blank.

    Scoped to the account that played the game. Otherwise logging into a second
    account would settle the first account's pending game against the second
    account's rank.
    """
    if not puuid:
        return None
    with _DB_LOCK:
        return conn.execute(
            """
            SELECT m.game_id, m.platform_id, m.my_rank_queue, m.my_lp_before,
                   m.my_tier_before, m.my_division_before, m.game_creation_ms
            FROM matches m
            JOIN participants p
              ON p.game_id = m.game_id AND p.platform_id = m.platform_id
            WHERE p.puuid = ?
              AND m.my_lp_before IS NOT NULL
              AND m.my_lp_delta IS NULL
              AND m.my_rank_queue IS NOT NULL
              AND m.game_creation_ms >= (CAST(strftime('%s','now') AS INTEGER) - ?) * 1000
            ORDER BY m.game_creation_ms DESC
            LIMIT 1
            """,
            (puuid, max_age_hours * 3600),
        ).fetchone()


def derive_lp_from_snapshots(conn: sqlite3.Connection, puuid: str) -> int:
    """Recover one account's LP changes from the rank snapshots already on record.

    Every live capture pins your rank as it stood just after that game. Two
    consecutive games on the same ladder therefore bracket a change, and the
    difference belongs to the later one — so games captured before LP tracking
    existed can still get their number, without inventing anything.

    Only games that actually move a ladder are chained: an ARAM game sitting
    between two Classic games has a solo-queue snapshot, but attributing Classic
    movement across it would be wrong. Games belonging to a different account
    are excluded for the same reason, one step further out.
    """
    from . import ranked

    if not puuid:
        return 0

    filled = 0
    with _DB_LOCK:
        for ladder in ranked.TRACKED_QUEUES:
            rows = conn.execute(
                """
                SELECT m.game_id, m.platform_id, m.queue_id, m.game_mode,
                       m.my_lp_delta, pr.tier, pr.division, pr.league_points
                FROM v_my_matches v
                JOIN matches m
                  ON m.game_id = v.game_id AND m.platform_id = v.platform_id
                JOIN participant_ranks pr
                  ON  pr.game_id = v.game_id AND pr.platform_id = v.platform_id
                  AND pr.participant_id = v.participant_id AND pr.queue_type = ?
                WHERE v.puuid = ?
                ORDER BY m.game_creation_ms ASC
                """,
                (ladder, puuid),
            ).fetchall()

            on_ladder = [
                row for row in rows
                if ranked.affects_ladder(row["queue_id"], row["game_mode"]) == ladder
            ]

            for previous, current in zip(on_ladder, on_ladder[1:]):
                if current["my_lp_delta"] is not None:
                    continue
                before = ranked.Rank(
                    ladder, previous["tier"], previous["division"], previous["league_points"]
                )
                after = ranked.Rank(
                    ladder, current["tier"], current["division"], current["league_points"]
                )
                delta = ranked.diff_points(before, after)
                if delta is None:
                    continue
                conn.execute(
                    """
                    UPDATE matches SET
                        my_rank_queue = ?, my_lp_delta = ?, my_lp_after = ?,
                        my_tier_after = ?, my_division_after = ?
                    WHERE game_id = ? AND platform_id = ?
                    """,
                    (
                        ladder, delta, after.league_points, after.tier, after.division,
                        current["game_id"], current["platform_id"],
                    ),
                )
                filled += 1
        conn.commit()
    return filled


def most_recent_match_key(conn: sqlite3.Connection) -> tuple | None:
    with _DB_LOCK:
        row = conn.execute(
            "SELECT game_id, platform_id FROM matches"
            " ORDER BY game_creation_ms DESC, captured_at DESC LIMIT 1"
        ).fetchone()
    return (row["game_id"], row["platform_id"]) if row else None


def players_needing_ranks(
    conn: sqlite3.Connection, stale_days: int = 7, limit: int | None = None
) -> list[str]:
    """Players with no rank recorded, or one older than `stale_days`.

    Ordered by most recently played, so a capped run covers the games you are
    most likely to be looking at.
    """
    with _DB_LOCK:
        rows = conn.execute(
            """
            SELECT p.puuid, MAX(m.game_creation_ms) AS last_seen
            FROM participants p
            JOIN matches m ON m.game_id = p.game_id AND m.platform_id = p.platform_id
            LEFT JOIN player_ranks r ON r.puuid = p.puuid
            WHERE p.puuid IS NOT NULL
            GROUP BY p.puuid
            HAVING MAX(COALESCE(r.updated_at, '')) < datetime('now', ?)
            ORDER BY last_seen DESC
            """,
            (f"-{int(stale_days)} days",),
        ).fetchall()
    puuids = [row["puuid"] for row in rows]
    return puuids[:limit] if limit else puuids


def resolve_names(conn: sqlite3.Connection, static) -> dict[str, int]:
    """Re-derive every display name from its stored id.

    Names are only ever a convenience layer over the ids, which means they can
    always be rebuilt: run this after a patch adds augments the cached assets
    did not know about, or to correct rows written while the cache was cold.
    """
    counts = {"queues": 0, "champions": 0, "augments": 0}
    with _DB_LOCK:
        for queue_id, name in static.queues.items():
            cur = conn.execute(
                "UPDATE matches SET queue_name = ?"
                " WHERE queue_id = ? AND (queue_name IS NULL OR queue_name != ?)",
                (name, queue_id, name),
            )
            counts["queues"] += cur.rowcount

        for champion_id, name in static.champions.items():
            cur = conn.execute(
                "UPDATE participants SET champion_name = ?"
                " WHERE champion_id = ? AND (champion_name IS NULL OR champion_name != ?)",
                (name, champion_id, name),
            )
            counts["champions"] += cur.rowcount

        for augment_id, name in static.augments.items():
            cur = conn.execute(
                "UPDATE participant_augments SET augment_name = ?"
                " WHERE augment_id = ? AND (augment_name IS NULL OR augment_name != ?)",
                (name, augment_id, name),
            )
            counts["augments"] += cur.rowcount

        conn.commit()
    return counts


def unresolved_counts(conn: sqlite3.Connection) -> dict[str, int]:
    with _DB_LOCK:
        return {
            "matches_without_queue_name": conn.execute(
                "SELECT COUNT(*) FROM matches WHERE queue_name IS NULL"
            ).fetchone()[0],
            "participants_without_champion_name": conn.execute(
                "SELECT COUNT(*) FROM participants WHERE champion_name IS NULL"
            ).fetchone()[0],
            "augments_without_name": conn.execute(
                "SELECT COUNT(*) FROM participant_augments WHERE augment_name IS NULL"
            ).fetchone()[0],
        }


def _fill_blanks(conn: sqlite3.Connection, match: Match) -> None:
    """Fill only the match-level columns the stored row is missing.

    COALESCE keeps whatever is already there, so this can never downgrade a
    field. Participants are left alone entirely — the stored row came from a
    better source and knows them more completely.
    """
    conn.execute(
        """
        UPDATE matches SET
            queue_id         = COALESCE(queue_id, ?),
            queue_name       = COALESCE(queue_name, ?),
            game_mode        = COALESCE(game_mode, ?),
            game_type        = COALESCE(game_type, ?),
            map_id           = COALESCE(map_id, ?),
            game_creation_ms = COALESCE(game_creation_ms, ?),
            game_duration_s  = COALESCE(game_duration_s, ?),
            game_version     = COALESCE(game_version, ?),
            winning_team_id  = COALESCE(winning_team_id, ?)
        WHERE game_id = ? AND platform_id = ?
        """,
        (
            match.queue_id, match.queue_name, match.game_mode, match.game_type,
            match.map_id, match.game_creation_ms, match.game_duration_s,
            match.game_version, match.winning_team_id, match.game_id, match.platform_id,
        ),
    )


def _insert_participant(conn: sqlite3.Connection, match: Match, p: Participant) -> None:
    items = (list(p.items) + [None] * 7)[:7]
    conn.execute(
        """
        INSERT INTO participants (
            game_id, platform_id, participant_id, puuid, riot_id_game_name,
            riot_id_tagline, summoner_name, team_id, champion_id, champion_name,
            position, win, placement, kills, deaths, assists, cs, gold_earned,
            damage_to_champions, damage_taken, vision_score, champ_level,
            item0, item1, item2, item3, item4, item5, item6, spell1_id, spell2_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            match.game_id, match.platform_id, p.participant_id, p.puuid,
            p.riot_id_game_name, p.riot_id_tagline, p.summoner_name, p.team_id,
            p.champion_id, p.champion_name, p.position,
            None if p.win is None else int(p.win),
            p.placement, p.kills, p.deaths, p.assists, p.cs, p.gold_earned,
            p.damage_to_champions, p.damage_taken, p.vision_score, p.champ_level,
            *items, p.spell1_id, p.spell2_id,
        ),
    )
    for augment in p.augments:
        conn.execute(
            """
            INSERT INTO participant_augments
                (game_id, platform_id, participant_id, slot, augment_id, augment_name)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                match.game_id, match.platform_id, p.participant_id,
                augment.slot, augment.augment_id, augment.augment_name,
            ),
        )


def _touch_players(conn: sqlite3.Connection, participants: Iterable[Participant]) -> None:
    now = _now()
    for p in participants:
        if not p.puuid:
            continue
        conn.execute(
            """
            INSERT INTO players
                (puuid, riot_id_game_name, riot_id_tagline, summoner_name, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(puuid) DO UPDATE SET
                riot_id_game_name = COALESCE(excluded.riot_id_game_name, players.riot_id_game_name),
                riot_id_tagline   = COALESCE(excluded.riot_id_tagline, players.riot_id_tagline),
                summoner_name     = COALESCE(excluded.summoner_name, players.summoner_name),
                last_seen         = excluded.last_seen
            """,
            (p.puuid, p.riot_id_game_name, p.riot_id_tagline, p.summoner_name, now, now),
        )

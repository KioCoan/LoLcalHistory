"""The local dashboard.

Read-only and offline by construction: it opens the SQLite file, serves one
page and a handful of JSON endpoints, and makes no outbound requests. The page
ships its own CSS and JS for the same reason — a CDN link would be a call to
somebody else's server carrying a referrer.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from flask import Flask, jsonify, render_template, request

from .. import config, health, ranked


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params)]


def _filters() -> tuple[str, list]:
    """Build the shared WHERE fragment for queue / champion / recency filters."""
    clauses: list[str] = []
    params: list = []

    queue = request.args.get("queue", type=int)
    if queue is not None:
        clauses.append("queue_id = ?")
        params.append(queue)

    champion = request.args.get("champion", type=int)
    if champion is not None:
        clauses.append("champion_id = ?")
        params.append(champion)

    days = request.args.get("days", type=int)
    if days:
        clauses.append(
            "game_creation_ms >= (CAST(strftime('%s', 'now') AS INTEGER) - ?) * 1000"
        )
        params.append(days * 86400)

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def create_app() -> Flask:
    app = Flask(__name__)
    # Jinja caches compiled templates for the process lifetime otherwise, so an
    # edit to the page would need a server restart to show up.
    app.config["TEMPLATES_AUTO_RELOAD"] = True

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/api/health")
    def api_health():
        """Watcher status, so a silently broken capture surfaces on the page."""
        state = health.load()
        return jsonify(
            {
                "degraded": health.is_degraded(state),
                "lines": health.describe(state),
                "captures_ok": state.get("captures_ok", 0),
                "captures_failed": state.get("captures_failed", 0),
                "consecutive_failures": state.get("consecutive_failures", 0),
                "last_capture_at": state.get("last_capture_at"),
                "last_seen_at": state.get("watcher_last_seen_at"),
                "last_error": state.get("last_error"),
            }
        )

    @app.route("/api/filters")
    def api_filters():
        with get_conn() as conn:
            return jsonify(
                {
                    "queues": _rows(
                        conn,
                        "SELECT queue_id, COALESCE(queue_name, game_mode, 'Queue ' || queue_id)"
                        " AS queue_name, COUNT(*) AS games FROM v_my_matches"
                        " GROUP BY queue_id ORDER BY games DESC",
                    ),
                    "champions": _rows(
                        conn,
                        "SELECT champion_id, champion_name, COUNT(*) AS games"
                        " FROM v_my_matches WHERE champion_id IS NOT NULL"
                        " GROUP BY champion_id ORDER BY champion_name",
                    ),
                }
            )

    @app.route("/api/summary")
    def api_summary():
        where, params = _filters()
        with get_conn() as conn:
            summary = conn.execute(
                f"""
                SELECT COUNT(*) AS games,
                       SUM(COALESCE(win, 0)) AS wins,
                       SUM(COALESCE(game_duration_s, 0)) AS seconds,
                       SUM(COALESCE(kills, 0)) AS kills,
                       SUM(COALESCE(deaths, 0)) AS deaths,
                       SUM(COALESCE(assists, 0)) AS assists
                FROM v_my_matches{where}
                """,
                tuple(params),
            ).fetchone()
            account = conn.execute(
                "SELECT riot_id_game_name, riot_id_tagline FROM me LIMIT 1"
            ).fetchone()
            return jsonify(
                {
                    "summary": dict(summary) if summary else {},
                    "account": dict(account) if account else None,
                }
            )

    @app.route("/api/matches")
    def api_matches():
        where, params = _filters()
        limit = min(request.args.get("limit", default=200, type=int), 1000)
        with get_conn() as conn:
            matches = _rows(
                conn,
                f"""
                SELECT game_id, platform_id, queue_id, queue_name, game_mode,
                       game_creation_ms, game_duration_s, champion_id, champion_name,
                       win, placement, kills, deaths, assists, cs, gold_earned,
                       damage_to_champions, vision_score, source, team_id, participant_id,
                       my_rank_queue, my_lp_delta, my_lp_after, my_tier_after,
                       my_division_after
                FROM v_my_matches{where}
                ORDER BY game_creation_ms DESC LIMIT ?
                """,
                tuple(params) + (limit,),
            )
            me = {row["puuid"] for row in conn.execute("SELECT puuid FROM me")}

            # Attach augments and team-mates per match so the row can expand.
            for match in matches:
                match["augments"] = [
                    row["augment_name"]
                    for row in conn.execute(
                        "SELECT a.augment_name FROM participant_augments a"
                        " JOIN v_my_matches v ON v.game_id = a.game_id"
                        "  AND v.platform_id = a.platform_id"
                        "  AND v.participant_id = a.participant_id"
                        " WHERE a.game_id = ? ORDER BY a.slot",
                        (match["game_id"],),
                    )
                ]

                # Classic games are ranked on their own ladder, so the rank
                # shown has to follow the mode rather than always being solo Q.
                queue_type = ranked.ranked_queue_for(match["queue_id"], match["game_mode"])
                match["rank_queue_type"] = queue_type
                match["team"] = _team_with_ranks(conn, match, queue_type)
                match["my_rank"] = next(
                    (p for p in match["team"] if p["puuid"] in me and p["tier"]), None
                )
            return jsonify(matches)

    def _team_with_ranks(conn, match, queue_type):
        """Team list with each player's rank on the ladder this mode uses.

        Prefers the rank captured with the game; falls back to the player's
        current rank, flagged so the two are never confused — a backfilled game
        can only ever show what someone is ranked today.
        """
        return _rows(
            conn,
            """
            SELECT p.participant_id, p.puuid, p.champion_name, p.riot_id_game_name,
                   p.riot_id_tagline, p.summoner_name, p.kills, p.deaths, p.assists,
                   p.team_id, p.win,
                   COALESCE(pr.tier, plr.tier)                   AS tier,
                   COALESCE(pr.division, plr.division)           AS division,
                   COALESCE(pr.league_points, plr.league_points) AS league_points,
                   CASE WHEN pr.tier IS NOT NULL THEN 1 ELSE 0 END AS rank_at_match
            FROM participants p
            LEFT JOIN participant_ranks pr
              ON  pr.game_id = p.game_id AND pr.platform_id = p.platform_id
              AND pr.participant_id = p.participant_id AND pr.queue_type = ?
            LEFT JOIN player_ranks plr
              ON  plr.puuid = p.puuid AND plr.queue_type = ?
            WHERE p.game_id = ? AND p.platform_id = ?
            ORDER BY p.team_id, p.participant_id
            """,
            (queue_type, queue_type, match["game_id"], match["platform_id"]),
        )

    @app.route("/api/champions")
    def api_champions():
        where, params = _filters()
        with get_conn() as conn:
            return jsonify(
                _rows(
                    conn,
                    f"""
                    SELECT champion_id,
                           COALESCE(champion_name, 'Champion ' || champion_id) AS champion_name,
                           COUNT(*) AS games,
                           SUM(COALESCE(win, 0)) AS wins,
                           COUNT(*) - SUM(COALESCE(win, 0)) AS losses,
                           ROUND(100.0 * SUM(COALESCE(win, 0)) / COUNT(*), 1) AS win_rate,
                           ROUND(AVG(kills), 1) AS avg_kills,
                           ROUND(AVG(deaths), 1) AS avg_deaths,
                           ROUND(AVG(assists), 1) AS avg_assists,
                           ROUND(AVG(cs), 0) AS avg_cs,
                           ROUND(AVG(damage_to_champions), 0) AS avg_damage
                    FROM v_my_matches{where}
                    GROUP BY champion_id
                    ORDER BY games DESC, win_rate DESC
                    """,
                    tuple(params),
                )
            )

    @app.route("/api/augments")
    def api_augments():
        where, params = _filters()
        with get_conn() as conn:
            return jsonify(
                _rows(
                    conn,
                    f"""
                    SELECT a.augment_id,
                           COALESCE(a.augment_name, 'Augment ' || a.augment_id) AS augment_name,
                           COUNT(*) AS games,
                           SUM(COALESCE(v.win, 0)) AS wins,
                           COUNT(*) - SUM(COALESCE(v.win, 0)) AS losses,
                           ROUND(100.0 * SUM(COALESCE(v.win, 0)) / COUNT(*), 1) AS win_rate,
                           ROUND(AVG(v.kills), 1) AS avg_kills,
                           ROUND(AVG(v.deaths), 1) AS avg_deaths,
                           ROUND(AVG(v.assists), 1) AS avg_assists
                    FROM v_my_matches v
                    JOIN participant_augments a
                      ON a.game_id = v.game_id AND a.platform_id = v.platform_id
                     AND a.participant_id = v.participant_id
                    {where}
                    GROUP BY a.augment_id
                    ORDER BY games DESC, win_rate DESC
                    """,
                    tuple(params),
                )
            )

    @app.route("/api/teammates")
    def api_teammates():
        where, params = _filters()
        min_games = request.args.get("min_games", default=2, type=int)
        with get_conn() as conn:
            return jsonify(
                _rows(
                    conn,
                    f"""
                    SELECT t.puuid,
                           COALESCE(pl.riot_id_game_name, t.summoner_name,
                                    substr(t.puuid, 1, 8)) AS name,
                           pl.riot_id_tagline AS tagline,
                           solo.tier        AS solo_tier,
                           solo.division    AS solo_division,
                           classic.tier     AS classic_tier,
                           classic.division AS classic_division,
                           COUNT(*) AS games,
                           SUM(COALESCE(t.win, 0)) AS wins,
                           COUNT(*) - SUM(COALESCE(t.win, 0)) AS losses,
                           ROUND(100.0 * SUM(COALESCE(t.win, 0)) / COUNT(*), 1) AS win_rate,
                           MAX(v.game_creation_ms) AS last_played_ms
                    FROM v_my_matches v
                    JOIN participants t
                      ON t.game_id = v.game_id AND t.platform_id = v.platform_id
                     AND t.team_id = v.team_id
                    LEFT JOIN players pl ON pl.puuid = t.puuid
                    LEFT JOIN player_ranks solo
                      ON solo.puuid = t.puuid AND solo.queue_type = 'RANKED_SOLO_5x5'
                    LEFT JOIN player_ranks classic
                      ON classic.puuid = t.puuid AND classic.queue_type = 'JADE_RANKED_SOLO_5x5'
                    {where}
                      {'AND' if where else 'WHERE'} t.puuid IS NOT NULL
                      AND t.puuid NOT IN (SELECT puuid FROM me)
                    GROUP BY t.puuid
                    HAVING games >= ?
                    ORDER BY games DESC, win_rate DESC
                    """,
                    tuple(params) + (min_games,),
                )
            )

    return app

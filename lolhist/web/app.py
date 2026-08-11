"""The local dashboard.

Read-only: it opens the SQLite file and serves one page plus a handful of JSON
endpoints. The page ships its own CSS and JS rather than linking a CDN, and
every image comes from the icon mirror, so rendering it reaches nobody.

One endpoint does leave the machine — `/api/update` asks GitHub whether a newer
release exists. It sends nothing about your history; see `lolhist/updates.py`.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from flask import Flask, jsonify, render_template, request, send_file

from .. import config, health, icons, ranked, static_data, store, updates


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params)]


def _selected_account(conn: sqlite3.Connection) -> str | None:
    """Whose history to show: the requested account, else whoever is logged in.

    Resolved on the server rather than left to the page. A dashboard left open
    across an account switch would otherwise fall back to showing both accounts
    pooled together, which is exactly the confusion this is here to prevent.

    None only when no account is known yet — a database backfilled before the
    watcher has ever connected. Nothing is scoped in that case, which shows
    everything rather than nothing.
    """
    requested = request.args.get("account")
    if requested:
        return requested
    return store.active_puuid(conn)


def _filters(conn: sqlite3.Connection) -> tuple[str, list, str | None]:
    """Build the shared WHERE fragment for account / queue / champion / recency.

    Column names are qualified, so every caller must alias `v_my_matches` as
    `v`. Bare names broke the moment the fragment was spliced beside a join on
    `participants`, which has its own `champion_id` and `puuid`.
    """
    clauses: list[str] = []
    params: list = []

    account = _selected_account(conn)
    if account:
        clauses.append("v.puuid = ?")
        params.append(account)

    queue = request.args.get("queue", type=int)
    if queue is not None:
        clauses.append("v.queue_id = ?")
        params.append(queue)

    champion = request.args.get("champion", type=int)
    if champion is not None:
        clauses.append("v.champion_id = ?")
        params.append(champion)

    days = request.args.get("days", type=int)
    if days:
        clauses.append(
            "v.game_creation_ms >= (CAST(strftime('%s', 'now') AS INTEGER) - ?) * 1000"
        )
        params.append(days * 86400)

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params, account


def create_app(on_show=None) -> Flask:
    """The dashboard.

    `on_show` raises the desktop window; supplied by the desktop app so a second
    launch can bring the running one forward. Absent under `lolhist serve`,
    where there is no window to raise.
    """
    app = Flask(__name__)
    # Jinja caches compiled templates for the process lifetime otherwise, so an
    # edit to the page would need a server restart to show up.
    app.config["TEMPLATES_AUTO_RELOAD"] = True

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/favicon.ico")
    def favicon():
        icon = config.PACKAGE_DIR / "assets" / "icon.ico"
        if not icon.exists():
            return "", 404
        return send_file(icon, mimetype="image/vnd.microsoft.icon")

    @app.route("/api/show", methods=["POST"])
    def api_show():
        """Bring the desktop window forward. Called by a second launch."""
        if on_show is None:
            return jsonify({"shown": False}), 409
        on_show()
        return jsonify({"shown": True})

    @app.route("/api/update")
    def api_update():
        """Whether a newer release exists.

        `?refresh=1` asks GitHub now; otherwise this answers from the cached
        result, which is at most a few hours old. See `lolhist/updates.py` for
        exactly what leaves the machine.
        """
        if request.args.get("refresh"):
            return jsonify(updates.check(force=True))
        return jsonify(updates.state())

    @app.route("/api/update/install", methods=["POST"])
    def api_update_install():
        """Download, verify and run the installer, then quit for it."""
        return jsonify(updates.start_install())

    @app.route("/icon/<kind>/<int:key>")
    def icon(kind: str, key: int):
        """An icon from the local mirror.

        A 404 here is ordinary, not an error: it means the watcher has not
        copied that one out of the client yet. The page listens for it and keeps
        showing the name instead.
        """
        path = icons.path_for(kind, key)
        if path is None or not path.exists():
            return "", 404
        response = send_file(path, mimetype="image/jpeg" if kind == "profile" else "image/png")
        # The file is named after an immutable id, so re-fetching it is waste.
        response.headers["Cache-Control"] = "public, max-age=604800"
        return response

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

    @app.route("/api/assets")
    def api_assets():
        """Item and spell names, for the tooltips on the build icons.

        Read from the cached asset files, so this works with League closed.
        Fetched once per page load — these only change on a patch.
        """
        return jsonify(
            {
                "items": {str(k): v for k, v in static_data.asset_map("items", "name").items()},
                "spells": {str(k): v for k, v in static_data.asset_map("spells", "name").items()},
            }
        )

    @app.route("/api/accounts")
    def api_accounts():
        """Every account in this database, most recently logged in first."""
        with get_conn() as conn:
            return jsonify(
                {"accounts": store.accounts(conn), "selected": _selected_account(conn)}
            )

    @app.route("/api/filters")
    def api_filters():
        """The queue and champion lists, scoped to the selected account.

        Deliberately ignores the queue/champion filters themselves — a filter
        list that narrowed as you used it would strand you on one option.
        """
        with get_conn() as conn:
            account = _selected_account(conn)
            scope = " WHERE v.puuid = ?" if account else ""
            params = (account,) if account else ()
            return jsonify(
                {
                    "queues": _rows(
                        conn,
                        "SELECT v.queue_id,"
                        " COALESCE(v.queue_name, v.game_mode, 'Queue ' || v.queue_id)"
                        " AS queue_name, COUNT(*) AS games FROM v_my_matches v"
                        f"{scope} GROUP BY v.queue_id ORDER BY games DESC",
                        params,
                    ),
                    "champions": _rows(
                        conn,
                        "SELECT v.champion_id, v.champion_name, COUNT(*) AS games"
                        " FROM v_my_matches v"
                        f"{scope}{' AND' if scope else ' WHERE'} v.champion_id IS NOT NULL"
                        " GROUP BY v.champion_id ORDER BY v.champion_name",
                        params,
                    ),
                }
            )

    @app.route("/api/summary")
    def api_summary():
        with get_conn() as conn:
            where, params, selected = _filters(conn)
            summary = conn.execute(
                f"""
                SELECT COUNT(*) AS games,
                       SUM(COALESCE(v.win, 0)) AS wins,
                       SUM(COALESCE(v.game_duration_s, 0)) AS seconds,
                       SUM(COALESCE(v.kills, 0)) AS kills,
                       SUM(COALESCE(v.deaths, 0)) AS deaths,
                       SUM(COALESCE(v.assists, 0)) AS assists
                FROM v_my_matches v{where}
                """,
                tuple(params),
            ).fetchone()
            # The name shown must be the account these numbers belong to. The
            # old `LIMIT 1` picked one arbitrarily, so a second account made the
            # header label somebody else's games.
            account = conn.execute(
                "SELECT riot_id_game_name, riot_id_tagline, profile_icon_id FROM me"
                " WHERE puuid = ? LIMIT 1",
                (selected or "",),
            ).fetchone()
            return jsonify(
                {
                    "summary": dict(summary) if summary else {},
                    "account": dict(account) if account else None,
                }
            )

    @app.route("/api/matches")
    def api_matches():
        limit = min(request.args.get("limit", default=200, type=int), 1000)
        with get_conn() as conn:
            where, params, selected = _filters(conn)
            matches = _rows(
                conn,
                f"""
                SELECT v.game_id, v.platform_id, v.queue_id, v.queue_name, v.game_mode,
                       v.game_creation_ms, v.game_duration_s, v.champion_id,
                       v.champion_name, v.win, v.placement, v.kills, v.deaths,
                       v.assists, v.cs, v.gold_earned, v.damage_to_champions,
                       v.vision_score, v.source, v.team_id, v.participant_id,
                       v.my_rank_queue, v.my_lp_delta, v.my_lp_after, v.my_tier_after,
                       v.my_division_after, v.spell1_id, v.spell2_id,
                       v.item0, v.item1, v.item2, v.item3, v.item4, v.item5, v.item6
                FROM v_my_matches v{where}
                ORDER BY v.game_creation_ms DESC LIMIT ?
                """,
                tuple(params) + (limit,),
            )
            mine = (
                {selected}
                if selected
                else {row["puuid"] for row in conn.execute("SELECT puuid FROM me")}
            )

            # Attach augments and team-mates per match so the row can expand.
            for match in matches:
                # Keyed straight off this row's participant. Going through the
                # view would pull in a second set of augments for a game both
                # of your accounts played.
                match["augments"] = [
                    row["augment_name"]
                    for row in conn.execute(
                        "SELECT augment_name FROM participant_augments"
                        " WHERE game_id = ? AND platform_id = ? AND participant_id = ?"
                        " ORDER BY slot",
                        (match["game_id"], match["platform_id"], match["participant_id"]),
                    )
                ]

                # Classic games are ranked on their own ladder, so the rank
                # shown has to follow the mode rather than always being solo Q.
                queue_type = ranked.ranked_queue_for(match["queue_id"], match["game_mode"])
                match["rank_queue_type"] = queue_type
                match["team"] = _team_with_ranks(conn, match, queue_type)
                match["my_rank"] = next(
                    (p for p in match["team"] if p["puuid"] in mine and p["tier"]), None
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
            SELECT p.participant_id, p.puuid, p.champion_id, p.champion_name,
                   p.riot_id_game_name, p.riot_id_tagline, p.summoner_name,
                   p.kills, p.deaths, p.assists, p.team_id, p.win,
                   p.spell1_id, p.spell2_id,
                   p.item0, p.item1, p.item2, p.item3, p.item4, p.item5, p.item6,
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
        with get_conn() as conn:
            where, params, _selected = _filters(conn)
            return jsonify(
                _rows(
                    conn,
                    f"""
                    SELECT v.champion_id,
                           COALESCE(v.champion_name, 'Champion ' || v.champion_id)
                               AS champion_name,
                           COUNT(*) AS games,
                           SUM(COALESCE(v.win, 0)) AS wins,
                           COUNT(*) - SUM(COALESCE(v.win, 0)) AS losses,
                           ROUND(100.0 * SUM(COALESCE(v.win, 0)) / COUNT(*), 1) AS win_rate,
                           ROUND(AVG(v.kills), 1) AS avg_kills,
                           ROUND(AVG(v.deaths), 1) AS avg_deaths,
                           ROUND(AVG(v.assists), 1) AS avg_assists,
                           ROUND(AVG(v.cs), 0) AS avg_cs,
                           ROUND(AVG(v.damage_to_champions), 0) AS avg_damage
                    FROM v_my_matches v{where}
                    GROUP BY v.champion_id
                    ORDER BY games DESC, win_rate DESC
                    """,
                    tuple(params),
                )
            )

    @app.route("/api/augments")
    def api_augments():
        with get_conn() as conn:
            where, params, _selected = _filters(conn)
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
        min_games = request.args.get("min_games", default=2, type=int)
        with get_conn() as conn:
            where, params, selected = _filters(conn)
            # Exclude only the account being viewed. A second account of yours
            # that you actually duo with is a real team-mate, and hiding every
            # account in `me` made it vanish from this list entirely.
            if selected:
                exclude, exclude_params = "t.puuid != ?", (selected,)
            else:
                exclude, exclude_params = "t.puuid NOT IN (SELECT puuid FROM me)", ()
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
                      AND {exclude}
                    GROUP BY t.puuid
                    HAVING games >= ?
                    ORDER BY games DESC, win_rate DESC
                    """,
                    tuple(params) + exclude_params + (min_games,),
                )
            )

    return app

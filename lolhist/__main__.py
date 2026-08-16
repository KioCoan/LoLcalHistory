"""Command line entry point: doctor | probe | live | backfill | watch | serve | stats."""

from __future__ import annotations

import argparse
import json
import logging
import pathlib
import shutil
import sqlite3
import sys
from datetime import datetime

from . import backfill as backfill_mod
from . import config, health, icons, live, probe, ranked, static_data, store
from .client import connect
from .connection import ClientUnavailable, installed_league_dirs, read_lockfile


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    # httpx logs a line per request at INFO, which buries our own output during
    # a backfill of a few hundred games.
    for noisy in ("httpx", "httpcore", "websockets"):
        logging.getLogger(noisy).setLevel(logging.DEBUG if verbose else logging.WARNING)


def _print_health() -> bool:
    """Print watcher status. Returns True when something needs attention."""
    state = health.load()
    print()
    for line in health.describe(state):
        print(line)
    if health.is_degraded(state):
        print()
        print("  !! The watcher is running but not recording games.")
        print("  !! Restart it, and if it keeps failing check the error above.")
        return True
    return False


def cmd_doctor(args: argparse.Namespace) -> int:
    installs = installed_league_dirs()
    print("Install    : " + (str(installs[0]) if installs else "NOT FOUND"))
    for extra in installs[1:]:
        print(f"             also: {extra}")
    if not installs:
        print(f"             checked {config.RIOT_INSTALLS_JSON} and the usual folders")

    creds = read_lockfile()
    if creds is None:
        print("Lockfile   : not found or unreadable")
    else:
        print(f"Lockfile   : parsed, port {creds.port} (pid {creds.pid})")

    try:
        client = connect()
    except ClientUnavailable as exc:
        print(f"\nClient     : NOT RUNNING\n             {exc}")
        # Still worth reporting: a degraded watcher is the more serious problem
        # of the two, and it does not stop being true when the client is shut.
        _print_health()
        return 1

    with client:
        summoner = client.current_summoner()
        region = client.get_json_or_none("/riotclient/region-locale") or {}
        phase = client.get_json_or_none("/lol-gameflow/v1/gameflow-phase")
        static = static_data.load(client)

        name = summoner.get("gameName") or summoner.get("displayName")
        tag = summoner.get("tagLine")
        print(f"\nClient     : running on port {client.credentials.port}")
        print(f"Account    : {name}#{tag}" if tag else f"Account    : {name}")
        print(f"puuid      : {summoner.get('puuid')}")
        print(f"Region     : {region.get('region')}  ({region.get('locale')})")
        print(f"Phase      : {phase}")
        print(f"Static data: {static.summary()}")

    conn = store.open_db()
    print(f"Database   : {config.DB_PATH}  ({store.match_count(conn)} matches)")
    conn.close()

    # Non-zero exit when the watcher is broken, so a scheduled `doctor` run can
    # be used as an alarm rather than something you have to read.
    return 1 if _print_health() else 0


def cmd_probe(args: argparse.Namespace) -> int:
    try:
        client = connect()
    except ClientUnavailable as exc:
        print(f"Cannot probe: {exc}")
        return 1
    with client:
        probe.run(client)
    return 0


def cmd_live(args: argparse.Namespace) -> int:
    """Print the current game, for checking the live view without the window.

    Also the way to confirm the in-game API is reachable: that port only
    listens while a game is actually running, so it cannot be tested from the
    client alone.
    """
    snapshot = live.snapshot()
    if args.json:
        print(json.dumps(snapshot, indent=2, ensure_ascii=False))
        return 0 if snapshot["live"] else 1

    if not snapshot["live"]:
        print(snapshot["reason"])
        return 1

    mode = snapshot["queue_name"] or snapshot["game_mode"] or "unknown mode"
    clock = snapshot["game_time_s"]
    print(f"{mode} - {clock // 60}:{clock % 60:02d} elapsed")
    if not snapshot["ingame"]:
        print(
            "The in-game API did not answer, so runes and live scores are missing.\n"
            "Everything else here came from the client."
        )

    for team_id, side in ((100, "Blue"), (200, "Red")):
        print(f"\n{side} team")
        for player in snapshot["players"]:
            if player["team_id"] != team_id:
                continue
            _print_live_player(player)
    return 0


def _print_live_player(player: dict) -> None:
    name = player["name"] + (f"#{player['tagline']}" if player["tagline"] else "")
    print(f"  {player['champion_name'] or '?':<14} {name}")

    ranks = "  ".join(
        f"{r['queue_label']} {(r['tier'] or '').title()} {r['division'] or ''}".strip()
        + (f" {r['league_points']} LP" if r["league_points"] is not None else "")
        for r in player["ranks"]
    )
    rate = player["win_rate"]
    if not rate:
        record = "no ranked games this split"
    elif rate["complete"]:
        record = f"{rate['wins']}W {rate['losses']}L ({rate['rate']}% on {rate['queue_title']})"
    else:
        # Losses are only published for the logged-in account.
        record = f"{rate['wins']}W on {rate['queue_title']} (losses not published)"
    print(f"      {ranks or 'Unranked'}  |  {record}")

    mastery = player["mastery"]
    if mastery:
        print(f"      mastery {mastery['level']} on this champion, {mastery['points']:,} pts")
    top = ", ".join(
        f"{m['champion_name'] or m['champion_id']} ({m['points']:,})"
        for m in player["top_mastery"]
    )
    if top:
        print(f"      most played: {top}")

    runes = player.get("runes")
    if runes:
        print(
            f"      runes: {runes['keystone_name'] or runes['keystone_id']}"
            f" ({runes['primary_style_name']} / {runes['secondary_style_name']})"
        )

    # League Classic's old page, which only ever exists for your own account.
    classic = player.get("classic_runes")
    if classic:
        print(f"      rune page {classic['page_name'] or ''}".rstrip())
        for key, label in (("marks", "marks"), ("seals", "seals"),
                           ("glyphs", "glyphs"), ("quints", "quints")):
            entries = classic.get(key) or []
            if entries:
                shown = ", ".join(f"{e['count']}x {e['name'] or e['id']}" for e in entries)
                print(f"        {label:<7} {shown}")
    masteries = player.get("masteries")
    if masteries:
        print(f"      masteries: {masteries['page_name'] or 'page'}"
              f" - {masteries['points']} points")
    scores = player.get("scores")
    if scores:
        print(
            f"      {scores['kills']}/{scores['deaths']}/{scores['assists']}"
            f"  {scores['cs']} CS"
        )


def cmd_backfill(args: argparse.Namespace) -> int:
    try:
        client = connect()
    except ClientUnavailable as exc:
        print(f"Cannot backfill: {exc}")
        return 1

    conn = store.open_db()
    with client:
        summoner = client.current_summoner()
        if summoner.get("puuid"):
            store.set_me(
                conn,
                summoner["puuid"],
                summoner.get("gameName") or summoner.get("displayName"),
                summoner.get("tagLine"),
                summoner.get("profileIconId"),
            )
        static = static_data.load(client)
        counts = backfill_mod.run(
            client, conn, static, max_games=args.max, refetch=args.refetch
        )
        # The client is open right now, which is the only time icons for the
        # games just imported can be copied out of it.
        mirrored = icons.sync(client, conn)

    print(
        f"Seen {counts['seen']}  inserted {counts['inserted']}  "
        f"upgraded {counts['upgraded']}  enriched {counts.get('enriched', 0)}  "
        f"already stored {counts['skipped']}  failed {counts['failed']}"
    )
    print(f"Total matches stored: {store.match_count(conn)}")
    if mirrored["fetched"]:
        print(f"Mirrored {mirrored['fetched']} icon(s) from the client.")
    conn.close()
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    from .watcher import watch

    conn = store.open_db()
    print("Watching for finished games. Leave this running; Ctrl+C to stop.\n")
    try:
        watch(conn, sweep_after_game=not args.no_sweep)
    finally:
        conn.close()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .web.app import create_app

    app = create_app()
    print(f"Dashboard at http://{args.host}:{args.port}  (Ctrl+C to stop)")
    app.run(host=args.host, port=args.port, debug=False)
    return 0


def cmd_ranks(args: argparse.Namespace) -> int:
    """Fetch current ranks for players seen in your history."""
    try:
        client = connect()
    except ClientUnavailable as exc:
        print(f"Cannot fetch ranks: {exc}")
        return 1

    conn = store.open_db()
    with client:
        me_puuid = client.current_summoner().get("puuid") or ""
        mine = ranked.fetch_mine(client)
        if mine and me_puuid:
            # Scoped to the logged-in account: a second account's ladders are
            # its own, and mixing the two series invents LP changes.
            store.save_rank_progress(conn, mine, me_puuid)
            store.save_player_ranks(conn, {me_puuid: mine})
            print("Your ladders:")
            for rank in mine.values():
                if rank.is_ranked:
                    print(f"  {rank.queue_type:<24} {rank.label()}  ({rank.wins}W {rank.losses}L)")

        targets = (
            [row["puuid"] for row in conn.execute(
                "SELECT DISTINCT puuid FROM participants WHERE puuid IS NOT NULL")]
            if args.all
            else store.players_needing_ranks(conn, args.stale_days)
        )
        print(f"\nLooking up {len(targets)} player(s)...")
        results = ranked.fetch_many(client, targets)
        written = store.save_player_ranks(conn, results)

    derived = store.derive_lp_from_snapshots(conn, me_puuid)
    if derived:
        print(f"\nWorked out the LP change for {derived} earlier game(s) from stored snapshots.")

    got_a_tier = sum(
        1 for ranks in results.values() if any(r.is_ranked for r in ranks.values())
    )
    print(
        f"Stored {written} rank rows for {len(results)} players; "
        f"{got_a_tier} have a rank on at least one ladder."
    )
    print(
        "\nNote: this is each player's rank *now*. Only games captured live by "
        "`watch` record what they were ranked at the time."
    )
    conn.close()
    return 0


def cmd_resolve_names(args: argparse.Namespace) -> int:
    """Rebuild display names from stored ids."""
    try:
        client = connect()
    except ClientUnavailable:
        client = None
        print("Client not running — using the cached asset data.\n")

    static = static_data.load(client)
    if client:
        client.close()
    print(f"Static data: {static.summary()}")

    conn = store.open_db()
    counts = store.resolve_names(conn, static)
    print(
        f"Updated {counts['queues']} queue names, {counts['champions']} champion names, "
        f"{counts['augments']} augment names."
    )
    for label, count in store.unresolved_counts(conn).items():
        if count:
            print(f"  still unresolved: {label} = {count}")
    conn.close()
    return 0


def cmd_migrate_data(args: argparse.Namespace) -> int:
    """Move a pre-packaging `data/` directory to the shared location."""
    import shutil

    source = config.LEGACY_DATA_DIR
    target = config.DATA_DIR

    if not (source / "history.db").exists():
        print(f"Nothing to migrate: no database at {source}")
        return 0
    if source.resolve() == target.resolve():
        print("Already in the shared location.")
        return 0

    existing = 0
    if (target / "history.db").exists():
        conn = store.open_db()
        existing = store.match_count(conn)
        conn.close()
    if existing:
        print(
            f"Refusing to overwrite {target}, which already holds {existing} matches.\n"
            "Move it aside first if you really want the older database."
        )
        return 1

    target.mkdir(parents=True, exist_ok=True)
    for name in ("history.db", "history.db-wal", "history.db-shm", "health.json"):
        item = source / name
        if item.exists():
            shutil.copy2(item, target / name)
    for folder in ("raw", "static", "samples"):
        item = source / folder
        if item.is_dir():
            shutil.copytree(item, target / folder, dirs_exist_ok=True)

    conn = store.open_db()
    print(f"Migrated {store.match_count(conn)} matches from {source}\n           to {target}")
    conn.close()
    print("The original is left untouched; delete it once you are happy.")
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    from .desktop import main as run_desktop

    return run_desktop(with_watcher=not args.no_watcher, port=args.port)


# What a replay of the archive cannot produce. Match payloads carry the games;
# these carry who the players are and what they were ranked, which comes from
# separate client calls that can never be repeated for a game already played.
# Dropping them is why a rebuilt history showed every player as unranked.
_CARRIED_TABLES = ("me", "players", "player_ranks", "participant_ranks", "rank_progress")


def _carry_forward(conn, *sources) -> dict[str, int]:
    """Copy the un-reconstructible tables out of older databases.

    Reads every source given, newest intention first, and never lets one
    unreadable file stop the rest — a damaged database usually still has most
    of its tables, and those rows are the only copy in existence.
    """
    carried = {table: 0 for table in _CARRIED_TABLES}
    for source in sources:
        if not source:
            continue
        source = pathlib.Path(source)
        if not source.exists():
            continue
        try:
            previous = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        previous.row_factory = sqlite3.Row
        try:
            for table in _CARRIED_TABLES:
                try:
                    rows = previous.execute(f"SELECT * FROM {table}").fetchall()
                except sqlite3.Error:
                    continue  # missing or damaged; the others may still be fine
                for row in rows:
                    columns = row.keys()
                    placeholders = ",".join("?" * len(columns))
                    try:
                        with store.lock():
                            conn.execute(
                                f"INSERT OR IGNORE INTO {table} "
                                f"({','.join(columns)}) VALUES ({placeholders})",
                                tuple(row),
                            )
                        carried[table] += 1
                    except sqlite3.Error:
                        pass
        finally:
            previous.close()
    with store.lock():
        conn.commit()
    return carried


def cmd_rebuild(args: argparse.Namespace) -> int:
    """Reconstruct the database from the raw archive.

    The last resort, and the reason every payload is kept. Replays each
    archived capture through the same normaliser that wrote it the first time,
    then puts the archived rank and LP readings back on top.
    """
    import gzip

    from .normalize import normalize

    live = config.DB_PATH
    staged = live.with_name(f"{live.stem}-rebuilding{live.suffix}")
    staged.unlink(missing_ok=True)

    if (config.DATA_DIR / "instance.json").exists():
        print("The app appears to be running, and it holds the database open.")
        print("Quit it from the tray icon first, then run this again.")
        return 1

    files = sorted(config.RAW_DIR.glob("*.json.gz"))
    payloads = [p for p in files if not p.name.endswith("-ranks.json.gz")]
    if not payloads:
        print(f"No archived payloads in {config.RAW_DIR}.")
        return 1

    # Some end-of-game payloads were archived before the platform was known and
    # are named "unknown-...". They carry no platform of their own either, and
    # the column cannot be null — so borrow the one every other file agrees on.
    platforms = [p.name.split("-", 1)[0] for p in payloads]
    known = [p for p in platforms if p != "unknown"]
    fallback = max(set(known), key=known.count) if known else ""

    conn = store.open_db(staged)
    static = static_data.load()
    print(f"replaying {len(payloads)} archived payloads ({static.summary()})")

    # History first, so end-of-game captures upsert over them exactly as they
    # did live. The platform comes from the filename: an end-of-game payload
    # carries none, and without it the row keys on ('', game_id) and sits
    # beside its own history row instead of merging into it.
    ok = failed = 0
    for path in sorted(payloads, key=lambda p: ("-eog" in p.name, p.name)):
        source = "eog" if "-eog" in path.name else "history"
        platform = path.name.split("-", 1)[0]
        try:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                payload = json.load(handle)
            match = normalize(payload, static, source=source,
                              platform_hint=fallback if platform == "unknown" else platform)
            if not match.game_id or not match.participants:
                failed += 1
                continue
            match.raw_path = str(path.relative_to(config.DATA_DIR))
            store.upsert_match(conn, match)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  {path.name}: {exc}")
            failed += 1

    ranks = store.restore_ranks_from_archive(conn)

    carried = _carry_forward(conn, live, args.carry_from)

    # An end-of-game payload carries no queue id, and the live watcher fills
    # that in from the client's gameflow session — which no archive holds. Left
    # alone, those games show up as "KIWI" and "JADE".
    with store.lock():
        for mode, label in (("KIWI", "ARAM: Mayhem"), ("JADE", "Classic")):
            conn.execute(
                "UPDATE matches SET queue_name = ? "
                "WHERE queue_name IS NULL AND game_mode = ?",
                (label, mode),
            )
        conn.commit()

    total = store.match_count(conn)
    for table, n in sorted(carried.items()):
        if n:
            print(f"carried {n} {table} row(s) across")
    print(f"replayed {ok}, skipped {failed}")
    print(f"restored ranks for {ranks['participants']} players "
          f"and LP for {ranks['matches']} games")
    print(f"rebuilt {total} matches")
    conn.close()

    if not store.is_readable(staged):
        print("The rebuilt database failed its integrity check; leaving it as "
              f"{staged.name} and not replacing anything.")
        return 1

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if live.exists():
        aside = live.with_name(f"{live.stem}-replaced-{stamp}{live.suffix}")
        live.replace(aside)
        print(f"kept the previous database as {aside.name}")
    for suffix in ("-wal", "-shm"):
        companion = live.with_name(live.name + suffix)
        if companion.exists():
            companion.unlink()
    staged.replace(live)

    print("\nDone. Reopen the app; sign in once so it knows which account is "
          "yours, and it will fill in anything the archive could not.")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    """Put the verified backup back, keeping the current file aside."""
    live = config.DB_PATH
    backup = live.with_name(f"{live.stem}-backup{live.suffix}")

    if not backup.exists():
        print(f"No backup at {backup}.")
        print("The raw archive can still rebuild the history — see the README.")
        return 1

    held = store.backup_match_count(backup)
    current = store.backup_match_count(live) if live.exists() else 0
    print(f"backup holds {held} matches; the current database holds {current}")

    if held <= current and not args.force:
        print("The backup is not better than what you have. Use --force to restore anyway.")
        return 1
    if not store.is_readable(backup):
        print("The backup does not pass its integrity check; refusing to restore from it.")
        return 1

    # The copy happens first, into a temporary file beside the target. Moving
    # the current database aside before securing the replacement means a failure
    # anywhere in between leaves no database at all — which is exactly what
    # happened the first time this ran.
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    staged = live.with_name(f"{live.stem}-restoring{live.suffix}")
    shutil.copy2(backup, staged)

    if live.exists():
        aside = live.with_name(f"{live.stem}-replaced-{stamp}{live.suffix}")
        live.replace(aside)
        print(f"kept the current database as {aside.name}")
    for suffix in ("-wal", "-shm"):
        companion = live.with_name(live.name + suffix)
        if companion.exists():
            companion.unlink()

    staged.replace(live)
    print(f"restored {held} matches from {backup.name}")
    print("Close the app first if it is running, then reopen it.")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    conn = store.open_db()
    total = store.match_count(conn)
    print(f"{total} matches stored\n")
    if not total:
        print("Nothing yet. Open the client and run `backfill`, or leave `watch` running.")
        conn.close()
        return 0

    # Scoped to the account last logged in. `v_champion_stats` pools every
    # account in `me`, which is wrong the moment there is more than one.
    me_puuid = store.active_puuid(conn)
    scope = " WHERE puuid = ?" if me_puuid else ""
    rows = conn.execute(
        "SELECT COALESCE(champion_name, 'Champion ' || champion_id) AS champion_name,"
        "       COUNT(*) AS games,"
        "       SUM(COALESCE(win, 0)) AS wins,"
        "       COUNT(*) - SUM(COALESCE(win, 0)) AS losses,"
        "       ROUND(100.0 * SUM(COALESCE(win, 0)) / COUNT(*), 1) AS win_rate,"
        "       ROUND(AVG(kills), 1) AS avg_kills,"
        "       ROUND(AVG(deaths), 1) AS avg_deaths,"
        "       ROUND(AVG(assists), 1) AS avg_assists"
        f" FROM v_my_matches{scope}"
        " GROUP BY champion_id ORDER BY games DESC, win_rate DESC LIMIT ?",
        ((me_puuid, args.limit) if me_puuid else (args.limit,)),
    ).fetchall()

    known = store.accounts(conn)
    if len(known) > 1:
        current = next((a for a in known if a["puuid"] == me_puuid), None)
        if current:
            tag = f"#{current['riot_id_tagline']}" if current["riot_id_tagline"] else ""
            print(f"Showing {current['riot_id_game_name']}{tag} "
                  f"({len(known)} accounts on this machine)\n")

    print(f"{'Champion':<16}{'G':>4}{'W':>4}{'L':>4}{'WR%':>7}   KDA")
    for row in rows:
        kda = f"{row['avg_kills']}/{row['avg_deaths']}/{row['avg_assists']}"
        print(
            f"{row['champion_name'][:15]:<16}{row['games']:>4}{row['wins']:>4}"
            f"{row['losses']:>4}{row['win_rate']:>7}   {kda}"
        )
    conn.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lolhist",
        description="Local League of Legends match history, read from the game client.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument(
        "--version", action="version", version=f"{config.APP_NAME} {config.APP_VERSION}"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check the client connection and the database").set_defaults(
        func=cmd_doctor
    )
    sub.add_parser("probe", help="dump raw client payloads to data/samples").set_defaults(
        func=cmd_probe
    )

    p_live = sub.add_parser("live", help="show the match being played right now")
    p_live.add_argument("--json", action="store_true", help="print the raw snapshot")
    p_live.set_defaults(func=cmd_live)

    p_backfill = sub.add_parser("backfill", help="import recent games from the client's history")
    p_backfill.add_argument("--max", type=int, default=200, help="most games to walk (default 200)")
    p_backfill.add_argument(
        "--refetch", action="store_true", help="re-import games already stored"
    )
    p_backfill.set_defaults(func=cmd_backfill)

    p_watch = sub.add_parser("watch", help="capture games as they finish")
    p_watch.add_argument(
        "--no-sweep", action="store_true", help="skip the history sweep after each game"
    )
    p_watch.set_defaults(func=cmd_watch)

    p_serve = sub.add_parser("serve", help="run the local dashboard")
    p_serve.add_argument("--host", default=config.WEB_HOST)
    p_serve.add_argument("--port", type=int, default=config.WEB_PORT)
    p_serve.set_defaults(func=cmd_serve)

    sub.add_parser(
        "resolve-names", help="rebuild champion/queue/augment names from stored ids"
    ).set_defaults(func=cmd_resolve_names)

    p_ranks = sub.add_parser("ranks", help="fetch ranks for players in your history")
    p_ranks.add_argument(
        "--stale-days", type=int, default=7, help="refresh ranks older than this (default 7)"
    )
    p_ranks.add_argument("--all", action="store_true", help="refresh every known player")
    p_ranks.set_defaults(func=cmd_ranks)

    sub.add_parser(
        "migrate-data", help="move an old project-local data/ folder to the shared location"
    ).set_defaults(func=cmd_migrate_data)

    p_gui = sub.add_parser("gui", help="run the desktop app (window + tracking in one)")
    p_gui.add_argument(
        "--no-watcher", action="store_true", help="open the window without tracking"
    )
    p_gui.add_argument("--port", type=int, default=None, help="fix the internal port")
    p_gui.set_defaults(func=cmd_gui)

    p_rebuild = sub.add_parser(
        "rebuild",
        help="reconstruct the database from the raw archive (last resort)"
    )
    p_rebuild.add_argument(
        "--carry-from", metavar="DB",
        help="also copy players and ranks out of this older database"
    )
    p_rebuild.set_defaults(func=cmd_rebuild)

    p_restore = sub.add_parser(
        "restore", help="put the verified backup back after a loss"
    )
    p_restore.add_argument(
        "--force", action="store_true",
        help="restore even when the backup is not larger"
    )
    p_restore.set_defaults(func=cmd_restore)

    p_stats = sub.add_parser("stats", help="champion summary in the terminal")
    p_stats.add_argument("--limit", type=int, default=20)
    p_stats.set_defaults(func=cmd_stats)

    return parser


def _survive_unprintable_names() -> None:
    """Stop a name the console cannot render from killing the command.

    Riot IDs are free-form Unicode and the Windows console still defaults to a
    legacy code page, so printing a lobby is enough to raise
    UnicodeEncodeError — the live view fell over on a Japanese name. Replacing
    the character loses a glyph; not replacing it loses the whole command.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _survive_unprintable_names()
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

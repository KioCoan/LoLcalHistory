"""Command line entry point: doctor | probe | backfill | watch | serve | stats."""

from __future__ import annotations

import argparse
import logging
import sys

from . import backfill as backfill_mod
from . import config, health, probe, ranked, static_data, store
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
            )
        static = static_data.load(client)
        counts = backfill_mod.run(
            client, conn, static, max_games=args.max, refetch=args.refetch
        )

    print(
        f"Seen {counts['seen']}  inserted {counts['inserted']}  "
        f"upgraded {counts['upgraded']}  enriched {counts.get('enriched', 0)}  "
        f"already stored {counts['skipped']}  failed {counts['failed']}"
    )
    print(f"Total matches stored: {store.match_count(conn)}")
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
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check the client connection and the database").set_defaults(
        func=cmd_doctor
    )
    sub.add_parser("probe", help="dump raw client payloads to data/samples").set_defaults(
        func=cmd_probe
    )

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

    p_stats = sub.add_parser("stats", help="champion summary in the terminal")
    p_stats.add_argument("--limit", type=int, default=20)
    p_stats.set_defaults(func=cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

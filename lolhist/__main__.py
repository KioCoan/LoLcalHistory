"""Command line entry point: doctor | probe | backfill | watch | serve | stats."""

from __future__ import annotations

import argparse
import logging
import sys

from . import backfill as backfill_mod
from . import config, health, probe, ranked, static_data, store
from .client import connect
from .connection import ClientUnavailable, read_lockfile


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
    print(f"Lockfile   : {config.LOCKFILE_PATH}")
    creds = read_lockfile()
    if creds is None:
        print("             not found or unreadable")
    else:
        print(f"             parsed, port {creds.port} (pid {creds.pid})")

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
        mine = ranked.fetch_mine(client)
        if mine:
            store.save_rank_progress(conn, mine)
            summoner = client.current_summoner()
            if summoner.get("puuid"):
                store.save_player_ranks(conn, {summoner["puuid"]: mine})
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


def cmd_stats(args: argparse.Namespace) -> int:
    conn = store.open_db()
    total = store.match_count(conn)
    print(f"{total} matches stored\n")
    if not total:
        print("Nothing yet. Open the client and run `backfill`, or leave `watch` running.")
        conn.close()
        return 0

    rows = conn.execute(
        "SELECT champion_name, games, wins, losses, win_rate, avg_kills, avg_deaths, avg_assists"
        " FROM v_champion_stats ORDER BY games DESC, win_rate DESC LIMIT ?",
        (args.limit,),
    ).fetchall()
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

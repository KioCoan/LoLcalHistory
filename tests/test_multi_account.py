"""A second Riot account must not contaminate the first.

Logging into another account on the same PC used to merge everything: both
accounts wrote into one rank series, so the first game after a switch measured
a smurf's Silver against a main's Diamond and produced a large invented LP
change. The history merged too, with no way to tell whose games were whose.

Everything here is one database — the accounts share team-mates and matches
they both played — but every stat is attributed to the account that earned it.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from lolhist import config, store
from lolhist.models import Match, Participant
from lolhist.ranked import CLASSIC, SOLO, Rank
from lolhist.web import app as web_app

MAIN = "puuid-main"
ALT = "puuid-alt"


def a_match(game_id, puuid, *, queue_id=4310, game_mode="JADE", creation_ms=None,
            champion_id=29, others=()) -> Match:
    participants = [
        Participant(
            participant_id=1, puuid=puuid, team_id=100, win=True, champion_id=champion_id
        )
    ]
    for index, other in enumerate(others, start=2):
        participants.append(
            Participant(participant_id=index, puuid=other, team_id=100, win=True)
        )
    return Match(
        game_id=game_id,
        platform_id="BR1",
        queue_id=queue_id,
        queue_name="Classic",
        game_mode=game_mode,
        game_creation_ms=creation_ms if creation_ms is not None else int(time.time() * 1000),
        source="eog",
        participants=participants,
    )


@pytest.fixture
def conn():
    """The isolated database the dashboard will also open (see conftest)."""
    connection = store.open_db(config.DB_PATH)
    yield connection
    connection.close()


@pytest.fixture
def two_accounts(conn):
    """Main seen first, alt logged in afterwards."""
    store.set_me(conn, MAIN, "Kiozin", "uwu")
    conn.execute("UPDATE me SET updated_at = '2026-01-01T00:00:00+00:00' WHERE puuid = ?", (MAIN,))
    store.set_me(conn, ALT, "Smurf", "BR1")
    conn.execute("UPDATE me SET updated_at = '2026-06-01T00:00:00+00:00' WHERE puuid = ?", (ALT,))
    conn.commit()
    return conn


class TestActiveAccount:
    def test_the_most_recently_seen_account_is_the_active_one(self, two_accounts):
        assert store.active_puuid(two_accounts) == ALT

    def test_reconnecting_as_the_main_moves_it_back(self, two_accounts):
        """`set_me` runs on every session connect, so this is what a switch does."""
        store.set_me(two_accounts, MAIN, "Kiozin", "uwu")
        assert store.active_puuid(two_accounts) == MAIN

    def test_an_empty_database_has_no_active_account(self, conn):
        assert store.active_puuid(conn) is None

    def test_accounts_are_listed_with_their_game_counts(self, two_accounts):
        store.upsert_match(two_accounts, a_match(1, MAIN))
        store.upsert_match(two_accounts, a_match(2, MAIN))
        store.upsert_match(two_accounts, a_match(3, ALT))

        listed = {row["puuid"]: row for row in store.accounts(two_accounts)}
        assert listed[MAIN]["games"] == 2
        assert listed[ALT]["games"] == 1
        assert store.accounts(two_accounts)[0]["puuid"] == ALT, "newest first"


class TestRankSeriesIsolation:
    """The core corruption: one rank series shared by two ladders."""

    def test_one_account_never_reads_the_other_s_rank(self, two_accounts):
        store.save_rank_progress(
            two_accounts, {SOLO: Rank(SOLO, "DIAMOND", "II", 40)}, MAIN
        )
        store.save_rank_progress(
            two_accounts, {SOLO: Rank(SOLO, "SILVER", "IV", 12)}, ALT
        )

        assert store.latest_rank_progress(two_accounts, MAIN)[SOLO].tier == "DIAMOND"
        assert store.latest_rank_progress(two_accounts, ALT)[SOLO].tier == "SILVER"

    def test_a_later_observation_by_the_alt_does_not_become_the_main_s(self, two_accounts):
        """Ordering by time alone was the bug — whoever wrote last won."""
        store.save_rank_progress(
            two_accounts, {SOLO: Rank(SOLO, "DIAMOND", "II", 40)}, MAIN
        )
        time.sleep(1.05)  # `taken_at` has second resolution
        store.save_rank_progress(
            two_accounts, {SOLO: Rank(SOLO, "IRON", "IV", 0)}, ALT
        )

        assert store.latest_rank_progress(two_accounts, MAIN)[SOLO].tier == "DIAMOND"

    def test_an_unattributed_observation_is_refused(self, conn):
        """Better to record nothing than to file it under an empty account."""
        store.save_rank_progress(conn, {SOLO: Rank(SOLO, "GOLD", "I", 50)}, "")
        assert conn.execute("SELECT COUNT(*) FROM rank_progress").fetchone()[0] == 0
        assert store.latest_rank_progress(conn, "") == {}


class TestLpSettlementIsolation:
    def test_the_alt_does_not_settle_the_main_s_pending_game(self, two_accounts):
        match = a_match(1, MAIN)
        store.upsert_match(two_accounts, match)
        store.record_rank_before(
            two_accounts, match.key, CLASSIC, Rank(CLASSIC, "SILVER", "II", 97)
        )

        assert store.pending_lp_match(two_accounts, MAIN) is not None
        assert store.pending_lp_match(two_accounts, ALT) is None, (
            "the alt would have settled this against its own ladder"
        )

    def test_derivation_does_not_chain_across_accounts(self, two_accounts):
        """Two games on the same ladder bracket an LP change — but only if the
        same account played both."""

        def add(game_id, puuid, creation_ms, lp):
            store.upsert_match(two_accounts, a_match(game_id, puuid, creation_ms=creation_ms))
            two_accounts.execute(
                "INSERT INTO participant_ranks (game_id, platform_id, participant_id,"
                " queue_type, tier, division, league_points) VALUES (?,?,?,?,?,?,?)",
                (game_id, "BR1", 1, CLASSIC, "SILVER", "I", lp),
            )
            two_accounts.commit()

        add(1, MAIN, 1_786_000_000_000, 55)
        add(2, ALT, 1_786_000_300_000, 5)     # a different ladder entirely
        add(3, MAIN, 1_786_000_600_000, 84)

        assert store.derive_lp_from_snapshots(two_accounts, MAIN) == 1
        deltas = dict(two_accounts.execute("SELECT game_id, my_lp_delta FROM matches"))
        assert deltas[3] == 29, "chained from the main's own previous game"
        assert deltas[2] is None, "the alt's game must not inherit the main's movement"


class TestMigration:
    """An existing single-account database must come through intact."""

    @pytest.fixture
    def legacy_db(self, tmp_path):
        path = tmp_path / "legacy.db"
        old = sqlite3.connect(path)
        old.executescript(
            """
            CREATE TABLE me (
                puuid TEXT PRIMARY KEY, riot_id_game_name TEXT,
                riot_id_tagline TEXT, updated_at TEXT
            );
            CREATE TABLE rank_progress (
                taken_at TEXT NOT NULL, queue_type TEXT NOT NULL, tier TEXT,
                division TEXT, league_points INTEGER, wins INTEGER, losses INTEGER,
                PRIMARY KEY (taken_at, queue_type)
            );
            """
        )
        old.execute(
            "INSERT INTO me VALUES (?, 'Kiozin', 'uwu', '2026-01-01T00:00:00+00:00')",
            (MAIN,),
        )
        for taken_at, lp in (("2026-01-01T00:00:00+00:00", 55), ("2026-01-02T00:00:00+00:00", 84)):
            old.execute(
                "INSERT INTO rank_progress VALUES (?, ?, 'SILVER', 'I', ?, 10, 8)",
                (taken_at, CLASSIC, lp),
            )
        old.commit()
        old.close()
        return path

    def test_existing_observations_are_attributed_and_kept(self, legacy_db):
        conn = store.open_db(legacy_db)
        try:
            rows = conn.execute(
                "SELECT puuid, league_points FROM rank_progress ORDER BY taken_at"
            ).fetchall()
            assert len(rows) == 2, "no observation may be dropped"
            assert [row["puuid"] for row in rows] == [MAIN, MAIN]
            assert store.latest_rank_progress(conn, MAIN)[CLASSIC].league_points == 84
        finally:
            conn.close()

    def test_migrating_twice_is_harmless(self, legacy_db):
        store.open_db(legacy_db).close()
        conn = store.open_db(legacy_db)
        try:
            assert conn.execute("SELECT COUNT(*) FROM rank_progress").fetchone()[0] == 2
        finally:
            conn.close()

    def test_observations_with_no_account_are_left_unattributed(self, tmp_path):
        """Nobody will diff against them, which beats guessing an owner."""
        path = tmp_path / "ownerless.db"
        old = sqlite3.connect(path)
        old.executescript(
            """
            CREATE TABLE me (
                puuid TEXT PRIMARY KEY, riot_id_game_name TEXT,
                riot_id_tagline TEXT, updated_at TEXT
            );
            CREATE TABLE rank_progress (
                taken_at TEXT NOT NULL, queue_type TEXT NOT NULL, tier TEXT,
                division TEXT, league_points INTEGER, wins INTEGER, losses INTEGER,
                PRIMARY KEY (taken_at, queue_type)
            );
            INSERT INTO rank_progress
                VALUES ('2026-01-01T00:00:00+00:00', 'RANKED_SOLO_5x5',
                        'GOLD', 'I', 50, 1, 1);
            """
        )
        old.commit()
        old.close()

        conn = store.open_db(path)
        try:
            assert conn.execute("SELECT puuid FROM rank_progress").fetchone()[0] == ""
        finally:
            conn.close()


class TestDashboardScoping:
    @pytest.fixture
    def client(self, two_accounts):
        """A Flask test client over a database holding both accounts' games."""
        store.upsert_match(two_accounts, a_match(1, MAIN, champion_id=29))
        store.upsert_match(two_accounts, a_match(2, MAIN, champion_id=29))
        store.upsert_match(
            two_accounts, a_match(3, ALT, champion_id=64, others=("friend",))
        )
        store.upsert_match(
            two_accounts, a_match(4, ALT, champion_id=64, others=("friend",))
        )
        two_accounts.commit()
        return web_app.create_app().test_client()

    def test_the_default_is_whoever_is_logged_in(self, client):
        body = client.get("/api/summary").get_json()
        assert body["summary"]["games"] == 2
        assert body["account"]["riot_id_game_name"] == "Smurf", (
            "the header must name the account these numbers belong to"
        )

    def test_the_other_account_is_reachable_by_puuid(self, client):
        body = client.get(f"/api/summary?account={MAIN}").get_json()
        assert body["summary"]["games"] == 2
        assert body["account"]["riot_id_game_name"] == "Kiozin"

    def test_matches_are_scoped(self, client):
        rows = client.get(f"/api/matches?account={MAIN}").get_json()
        assert {row["game_id"] for row in rows} == {1, 2}

    def test_champion_lists_do_not_bleed_across_accounts(self, client):
        champions = client.get(f"/api/champions?account={MAIN}").get_json()
        assert {c["champion_id"] for c in champions} == {29}

        filters = client.get(f"/api/filters?account={ALT}").get_json()
        assert {c["champion_id"] for c in filters["champions"]} == {64}

    def test_accounts_endpoint_lists_both(self, client):
        body = client.get("/api/accounts").get_json()
        assert [a["puuid"] for a in body["accounts"]] == [ALT, MAIN]
        assert body["selected"] == ALT

    def test_teammates_filtered_by_champion_does_not_raise(self, client):
        """The filter fragment is spliced beside a join on `participants`, which
        has its own `champion_id` — unqualified, SQLite calls it ambiguous."""
        response = client.get(f"/api/teammates?account={ALT}&champion=64&min_games=2")
        assert response.status_code == 200
        assert {row["puuid"] for row in response.get_json()} == {"friend"}

    def test_the_other_account_shows_up_as_a_teammate(self, two_accounts, client):
        """Hiding every account in `me` erased a smurf you actually duo with."""
        for game_id in (10, 11):
            store.upsert_match(two_accounts, a_match(game_id, MAIN, others=(ALT,)))
        two_accounts.commit()

        rows = client.get(f"/api/teammates?account={MAIN}&min_games=2").get_json()
        assert ALT in {row["puuid"] for row in rows}
        assert MAIN not in {row["puuid"] for row in rows}, "you are not your own team-mate"


class TestWatcherFollowsTheSwitch:
    @pytest.fixture
    def watcher(self, conn):
        from lolhist.watcher import Watcher

        return Watcher(conn)

    def test_each_account_gets_its_history_imported(self, watcher):
        """`_synced` was a flag, so a switch mid-run left the new account's
        existing games missing until the app was restarted."""
        synced: list[str] = []
        watcher._initial_sync = lambda client, static: synced.append(watcher._me_puuid)

        for puuid, name in ((MAIN, "Kiozin"), (ALT, "Smurf"), (MAIN, "Kiozin")):
            watcher._remember_me({"puuid": puuid, "gameName": name, "tagLine": "x"})
            key = watcher._me_puuid or "?"
            if key not in watcher._synced:
                watcher._synced.add(key)
                watcher._initial_sync(None, None)

        assert synced == [MAIN, ALT], "the alt needs a sync; going back does not"

    def test_priming_reads_the_logged_in_account_s_ladder(self, watcher, conn):
        store.set_me(conn, MAIN, "Kiozin", "uwu")
        store.set_me(conn, ALT, "Smurf", "BR1")
        store.save_rank_progress(conn, {SOLO: Rank(SOLO, "DIAMOND", "II", 40)}, MAIN)
        store.save_rank_progress(conn, {SOLO: Rank(SOLO, "SILVER", "IV", 12)}, ALT)

        class NoRanks:
            def get_json_or_none(self, path, **params):
                return None

        watcher._remember_me({"puuid": ALT, "gameName": "Smurf", "tagLine": "BR1"})
        watcher._prime_my_ranks(NoRanks())

        assert watcher._my_ranks[SOLO].tier == "SILVER", (
            "the baseline for the next game must be this account's own rank"
        )

    def test_an_unidentified_account_skips_rank_work(self, watcher):
        class Explodes:
            def get_json_or_none(self, path, **params):
                raise AssertionError("looked up ranks with no account to file them under")

        watcher._prime_my_ranks(Explodes())
        watcher._settle_lp(Explodes())
        assert watcher._my_ranks == {}

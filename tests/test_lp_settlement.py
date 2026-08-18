"""LP settlement must survive the app being closed.

The first implementation computed the LP change 30 seconds after a game, inside
the live session. Closing the app in that window lost the reading permanently —
which is what happened in practice. The rank held going into a game is now
written at capture time, so the change can be completed on any later launch.
"""

from __future__ import annotations

import pytest

from lolhist import ranked, store
from lolhist.models import Match, Participant
from lolhist.ranked import CLASSIC, FLEX, SOLO, Rank


def a_match(game_id=1, queue_id=4310, game_mode="JADE", creation_ms=None) -> Match:
    import time

    return Match(
        game_id=game_id,
        platform_id="BR1",
        queue_id=queue_id,
        queue_name="Classic",
        game_mode=game_mode,
        game_creation_ms=creation_ms if creation_ms is not None else int(time.time() * 1000),
        source="eog",
        participants=[Participant(participant_id=1, puuid="me", team_id=100, win=True)],
    )


@pytest.fixture
def conn(tmp_path):
    connection = store.open_db(tmp_path / "lp.db")
    yield connection
    connection.close()


class TestLadderAttribution:
    """Only games that can move a ladder should wait for an LP change."""

    @pytest.mark.parametrize("queue_id", [4310, 4320])
    def test_classic_games_affect_the_classic_ladder(self, queue_id):
        assert ranked.affects_ladder(queue_id, "JADE") == CLASSIC

    def test_ranked_sr_queues(self):
        assert ranked.affects_ladder(420, "CLASSIC") == SOLO
        assert ranked.affects_ladder(440, "CLASSIC") == FLEX

    @pytest.mark.parametrize("queue_id,mode", [(2400, "KIWI"), (450, "ARAM"), (430, "CLASSIC")])
    def test_unranked_modes_affect_nothing(self, queue_id, mode):
        """Mayhem shows your solo rank but cannot change it. If it were treated
        as pending it would absorb the next real ranked game's LP."""
        assert ranked.affects_ladder(queue_id, mode) is None


class TestSettlement:
    def test_before_rank_is_stored_then_settled_later(self, conn):
        match = a_match()
        store.upsert_match(conn, match)
        before = Rank(CLASSIC, "SILVER", "II", 97)
        store.record_rank_before(conn, match.key, CLASSIC, before)

        pending = store.pending_lp_match(conn, "me")
        assert pending is not None
        assert pending["my_lp_before"] == 97
        assert pending["my_rank_queue"] == CLASSIC

        after = Rank(CLASSIC, "SILVER", "I", 15)
        delta = ranked.diff_points(before, after)
        store.record_lp_change(conn, match.key, CLASSIC, delta, after)

        row = conn.execute(
            "SELECT my_lp_delta, my_tier_after, my_lp_after FROM matches"
        ).fetchone()
        assert row["my_lp_delta"] > 0, "a promotion is a gain"
        assert row["my_tier_after"] == "SILVER"
        assert row["my_lp_after"] == 15

    def test_settled_matches_stop_being_pending(self, conn):
        match = a_match()
        store.upsert_match(conn, match)
        store.record_rank_before(conn, match.key, CLASSIC, Rank(CLASSIC, "SILVER", "II", 97))
        store.record_lp_change(conn, match.key, CLASSIC, 18, Rank(CLASSIC, "SILVER", "II", 115))
        assert store.pending_lp_match(conn, "me") is None

    def test_only_the_most_recent_pending_game_is_offered(self, conn):
        """Two unsettled games mean the current rank cannot say which moved what."""
        older = a_match(game_id=1, creation_ms=1_700_000_000_000)
        newer = a_match(game_id=2)
        for match in (older, newer):
            store.upsert_match(conn, match)
            store.record_rank_before(
                conn, match.key, CLASSIC, Rank(CLASSIC, "SILVER", "II", 97)
            )

        pending = store.pending_lp_match(conn, "me")
        assert pending["game_id"] == 2

    def test_stale_games_are_never_settled(self, conn):
        """An old pending game must not absorb today's LP change."""
        old = a_match(game_id=3, creation_ms=1_600_000_000_000)
        store.upsert_match(conn, old)
        store.record_rank_before(conn, old.key, CLASSIC, Rank(CLASSIC, "SILVER", "II", 97))
        assert store.pending_lp_match(conn, "me", max_age_hours=6) is None

    def test_derives_lp_from_consecutive_snapshots(self, conn):
        """Games captured before LP tracking existed can still be recovered."""
        store.set_me(conn, "me", "Kiozin", "uwu")

        def add(game_id, creation_ms, lp, queue_id=4310, mode="JADE"):
            match = a_match(game_id=game_id, queue_id=queue_id,
                            game_mode=mode, creation_ms=creation_ms)
            store.upsert_match(conn, match)
            conn.execute(
                "INSERT INTO participant_ranks (game_id, platform_id, participant_id,"
                " queue_type, tier, division, league_points) VALUES (?,?,?,?,?,?,?)",
                (game_id, "BR1", 1, CLASSIC, "SILVER", "I", lp),
            )
            conn.commit()

        add(1, 1_786_000_000_000, 55)
        add(2, 1_786_000_600_000, 84)

        assert store.derive_lp_from_snapshots(conn, "me") == 1
        row = conn.execute(
            "SELECT my_lp_delta, my_lp_after FROM matches WHERE game_id = 2"
        ).fetchone()
        assert row["my_lp_delta"] == 29
        assert row["my_lp_after"] == 84

        # The first game has nothing before it, so it stays blank rather than
        # being given an invented number.
        assert conn.execute(
            "SELECT my_lp_delta FROM matches WHERE game_id = 1"
        ).fetchone()[0] is None

    def test_derivation_skips_games_that_do_not_move_the_ladder(self, conn):
        """An ARAM game between two Classic games must not break the chain."""
        store.set_me(conn, "me", "Kiozin", "uwu")

        def add(game_id, creation_ms, lp, queue_id, mode):
            match = a_match(game_id=game_id, queue_id=queue_id,
                            game_mode=mode, creation_ms=creation_ms)
            store.upsert_match(conn, match)
            conn.execute(
                "INSERT INTO participant_ranks (game_id, platform_id, participant_id,"
                " queue_type, tier, division, league_points) VALUES (?,?,?,?,?,?,?)",
                (game_id, "BR1", 1, CLASSIC, "SILVER", "I", lp),
            )
            conn.commit()

        add(1, 1_786_000_000_000, 55, 4310, "JADE")
        add(2, 1_786_000_300_000, 55, 2400, "KIWI")   # Mayhem, changes nothing
        add(3, 1_786_000_600_000, 84, 4310, "JADE")

        store.derive_lp_from_snapshots(conn, "me")
        deltas = dict(conn.execute("SELECT game_id, my_lp_delta FROM matches"))
        assert deltas[3] == 29, "chained across the unrelated game"
        assert deltas[2] is None, "Mayhem cannot move the Classic ladder"

    def test_derivation_is_idempotent(self, conn):
        store.set_me(conn, "me", "Kiozin", "uwu")
        for game_id, creation, lp in ((1, 1_786_000_000_000, 55), (2, 1_786_000_600_000, 84)):
            store.upsert_match(conn, a_match(game_id=game_id, creation_ms=creation))
            conn.execute(
                "INSERT INTO participant_ranks (game_id, platform_id, participant_id,"
                " queue_type, tier, division, league_points) VALUES (?,?,?,?,?,?,?)",
                (game_id, "BR1", 1, CLASSIC, "SILVER", "I", lp),
            )
        conn.commit()

        assert store.derive_lp_from_snapshots(conn, "me") == 1
        assert store.derive_lp_from_snapshots(conn, "me") == 0

    def test_recording_before_does_not_clobber_a_settled_delta(self, conn):
        match = a_match()
        store.upsert_match(conn, match)
        store.record_lp_change(conn, match.key, CLASSIC, 18, Rank(CLASSIC, "SILVER", "II", 115))
        store.record_rank_before(conn, match.key, CLASSIC, Rank(CLASSIC, "GOLD", "IV", 5))

        row = conn.execute("SELECT my_lp_delta, my_lp_before FROM matches").fetchone()
        assert row["my_lp_delta"] == 18
        assert row["my_lp_before"] is None


class TestRepairingWhatTheWrongLadderRecorded:
    """The +842 rows already written to people's databases.

    An LP change is never reported by the client — it is always the difference
    between two rank observations, and those are still on record. So a bad
    delta is not lost data, it is a stale calculation, and the fix is to do it
    again rather than to ask anyone to rebuild.
    """

    def stored(self, conn, game_id=1):
        return conn.execute(
            "SELECT my_lp_delta FROM matches WHERE game_id = ?", (game_id,)
        ).fetchone()["my_lp_delta"]

    def a_settled_promotion(self, conn, delta):
        """The reported game, written as the old code would have written it."""
        match = a_match()
        store.upsert_match(conn, match)
        store.record_rank_before(conn, match.key, CLASSIC, Rank(CLASSIC, "WOOD", "I", 80))
        store.record_lp_change(
            conn, match.key, CLASSIC, delta, Rank(CLASSIC, "SILVER", "IV", 22)
        )
        return match

    def test_a_wrong_delta_is_worked_out_again(self, conn):
        self.a_settled_promotion(conn, delta=842)
        assert store._repair_lp_deltas(conn) == 1
        assert self.stored(conn) == 42

    def test_a_correct_delta_is_left_alone(self, conn):
        self.a_settled_promotion(conn, delta=42)
        assert store._repair_lp_deltas(conn) == 0, "a row that was right was rewritten"

    def test_a_row_with_no_rank_before_is_handed_back_to_the_deriver(self, conn):
        """Rows filled from the snapshot chain have an after and no before.

        Nothing local can recompute those, so clearing the number is the honest
        move: the deriver walks the chain again with the ladder corrected.
        """
        match = a_match()
        store.upsert_match(conn, match)
        store.record_lp_change(
            conn, match.key, CLASSIC, 842, Rank(CLASSIC, "SILVER", "IV", 22)
        )
        assert store._repair_lp_deltas(conn) == 1
        assert self.stored(conn) is None

    def test_it_runs_once_and_not_on_every_launch(self, tmp_path):
        """It rewrites rows, so a second unasked-for pass is worth ruling out."""
        path = tmp_path / "once.db"
        first = store.open_db(path)
        assert first.execute("PRAGMA user_version").fetchone()[0] == store._REPAIR_VERSION
        self.a_settled_promotion(first, delta=842)
        first.close()

        second = store.open_db(path)
        assert second.execute(
            "SELECT my_lp_delta FROM matches WHERE game_id = 1"
        ).fetchone()["my_lp_delta"] == 842, "the repair ran a second time"
        second.close()

    def test_an_upgraded_database_is_repaired_on_the_next_open(self, tmp_path):
        path = tmp_path / "upgrade.db"
        old = store.open_db(path)
        self.a_settled_promotion(old, delta=842)
        old.execute("PRAGMA user_version = 0")     # as it was before this release
        old.commit()
        old.close()

        upgraded = store.open_db(path)
        assert upgraded.execute(
            "SELECT my_lp_delta FROM matches WHERE game_id = 1"
        ).fetchone()["my_lp_delta"] == 42
        upgraded.close()

    def test_the_solo_ladder_is_not_disturbed(self, conn):
        match = a_match(queue_id=420, game_mode="CLASSIC")
        store.upsert_match(conn, match)
        store.record_rank_before(conn, match.key, SOLO, Rank(SOLO, "EMERALD", "II", 98))
        store.record_lp_change(conn, match.key, SOLO, 14, Rank(SOLO, "EMERALD", "I", 12))
        assert store._repair_lp_deltas(conn) == 0
        assert self.stored(conn) == 14

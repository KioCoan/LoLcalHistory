"""Rank and LP readings must survive a rebuild.

The raw archive is what makes a rebuild lossless, and it was only ever half an
archive. Match payloads went in; the rank each player held during the game and
the LP it moved did not. Those come from separate calls made while the game is
still fresh and, unlike the match, can never be asked for again — so every
rebuild silently emptied the LP column, four times over.
"""

from __future__ import annotations

import gzip
import json

import pytest

from lolhist import config, store
from lolhist.models import Match, Participant
from lolhist.ranked import SOLO, Rank

ME = "puuid-me"


@pytest.fixture
def match() -> Match:
    return Match(
        game_id=7, platform_id="BR1", queue_id=420, game_mode="CLASSIC",
        game_creation_ms=1_786_000_000_000, source="eog",
        participants=[
            Participant(participant_id=1, puuid=ME, team_id=100, win=True),
            Participant(participant_id=2, puuid="someone", team_id=100, win=True),
        ],
    )


@pytest.fixture
def conn(match):
    connection = store.open_db(config.DB_PATH)
    store.set_me(connection, ME, "Kiozin", "uwu")
    store.upsert_match(connection, match)
    yield connection
    connection.close()


def archive_for(match) -> dict:
    path = config.RAW_DIR / f"{match.platform_id}-{match.game_id}-ranks.json.gz"
    assert path.exists(), "nothing was archived"
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


class TestArchiving:
    def test_participant_ranks_are_archived(self, conn, match):
        store.save_participant_ranks(
            conn, match,
            {ME: {SOLO: Rank(SOLO, "EMERALD", "II", 41)},
             "someone": {SOLO: Rank(SOLO, "GOLD", "I", 12)}},
        )
        rows = archive_for(match)["participant_ranks"]
        assert len(rows) == 2
        assert [1, SOLO, "EMERALD", "II", 41] in rows

    def test_the_two_halves_of_an_lp_change_both_survive(self, conn, match):
        """Written at different moments, sometimes a launch apart. The later
        one must not erase the earlier."""
        store.record_rank_before(conn, match.key, SOLO, Rank(SOLO, "EMERALD", "II", 41))
        store.record_lp_change(conn, match.key, SOLO, 23, Rank(SOLO, "EMERALD", "II", 64))

        mine = archive_for(match)["mine"]
        assert mine["lp_before"] == 41, "the rank going in was overwritten"
        assert mine["delta"] == 23
        assert mine["lp_after"] == 64
        assert mine["queue"] == SOLO

    def test_ranks_and_lp_share_one_file(self, conn, match):
        store.save_participant_ranks(conn, match, {ME: {SOLO: Rank(SOLO, "IRON", "IV", 3)}})
        store.record_lp_change(conn, match.key, SOLO, -18, Rank(SOLO, "IRON", "IV", 3))

        data = archive_for(match)
        assert data["participant_ranks"], "adding LP dropped the ranks"
        assert data["mine"]["delta"] == -18


class TestRestoring:
    def test_a_rebuilt_database_gets_its_lp_back(self, conn, match, tmp_path):
        """The complaint, directly: every rebuild lost the LP column."""
        store.save_participant_ranks(conn, match, {ME: {SOLO: Rank(SOLO, "EMERALD", "II", 41)}})
        store.record_rank_before(conn, match.key, SOLO, Rank(SOLO, "EMERALD", "II", 41))
        store.record_lp_change(conn, match.key, SOLO, 23, Rank(SOLO, "EMERALD", "II", 64))

        # A rebuild: a brand new database with the match replayed and nothing else.
        rebuilt = store.open_db(tmp_path / "rebuilt.db")
        try:
            store.set_me(rebuilt, ME, "Kiozin", "uwu")
            store.upsert_match(rebuilt, match)
            assert rebuilt.execute(
                "SELECT my_lp_delta FROM matches"
            ).fetchone()[0] is None, "the replay alone should not know the LP"

            counts = store.restore_ranks_from_archive(rebuilt)

            row = rebuilt.execute(
                "SELECT my_rank_queue, my_lp_before, my_lp_delta, my_lp_after FROM matches"
            ).fetchone()
            assert (row["my_lp_before"], row["my_lp_delta"], row["my_lp_after"]) == (41, 23, 64)
            assert row["my_rank_queue"] == SOLO

            rank = rebuilt.execute(
                "SELECT tier, division, league_points FROM participant_ranks"
            ).fetchone()
            assert (rank["tier"], rank["division"], rank["league_points"]) == ("EMERALD", "II", 41)
            assert counts["participants"] == 1 and counts["matches"] == 1
        finally:
            rebuilt.close()

    def test_restoring_with_no_archive_is_harmless(self, tmp_path):
        rebuilt = store.open_db(tmp_path / "empty.db")
        try:
            assert store.restore_ranks_from_archive(rebuilt) == {
                "participants": 0, "matches": 0
            }
        finally:
            rebuilt.close()

    def test_an_unreadable_archive_entry_is_skipped(self, conn, match, tmp_path):
        store.record_lp_change(conn, match.key, SOLO, 5, Rank(SOLO, "GOLD", "I", 20))
        (config.RAW_DIR / "BR1-999-ranks.json.gz").write_bytes(b"not gzip")

        rebuilt = store.open_db(tmp_path / "rebuilt.db")
        try:
            store.upsert_match(rebuilt, match)
            counts = store.restore_ranks_from_archive(rebuilt)
            assert counts["matches"] == 1, "one bad file stopped the whole restore"
        finally:
            rebuilt.close()

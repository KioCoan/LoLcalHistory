"""An expanded match shows every ladder each player is ranked on.

It used to show exactly one — whichever ladder the mode was read against — so
a Classic game hid the solo queue rank of everyone in it, and someone ranked
only in Flex read as unranked outright.

The two rank sources still differ in kind and must never be conflated: the
snapshot taken when the game was captured is what someone *was*, the player's
current rank is only what they are today. The fallback is per ladder, because a
capture routinely holds one ladder and not the others.
"""

from __future__ import annotations

import time

import pytest

from lolhist import config, store
from lolhist.models import Match, Participant
from lolhist.ranked import CLASSIC, FLEX, SOLO, Rank
from lolhist.web import app as web_app

ME = "puuid-me"
THEM = "puuid-them"


def a_match(game_id=1, *, queue_id=4310, game_mode="JADE", their_team=200) -> Match:
    return Match(
        game_id=game_id, platform_id="BR1", queue_id=queue_id,
        queue_name="League Classic", game_mode=game_mode,
        game_creation_ms=int(time.time() * 1000), source="eog",
        participants=[
            Participant(participant_id=1, puuid=ME, team_id=100, win=True,
                        champion_id=60029, champion_name="Twitch"),
            Participant(participant_id=2, puuid=THEM, team_id=their_team,
                        win=their_team == 100, champion_id=64,
                        champion_name="LeeSin"),
        ],
    )


@pytest.fixture
def conn():
    connection = store.open_db(config.DB_PATH)
    store.set_me(connection, ME, "Kiozin", "uwu")
    yield connection
    connection.close()


def players(client, *, account=ME) -> dict[int, dict]:
    """The team list of the only match, keyed by participant id."""
    matches = client.get(f"/api/matches?account={account}").get_json()
    assert matches, "the match did not come back at all"
    return {p["participant_id"]: p for p in matches[0]["team"]}


def ladders(player) -> dict[str, dict]:
    return {r["queue_type"]: r for r in player["ranks"]}


class TestEveryLadder:
    @pytest.fixture
    def client(self, conn):
        match = a_match()
        store.upsert_match(conn, match)
        store.save_player_ranks(conn, {
            ME: {
                SOLO: Rank(SOLO, "EMERALD", "II", 41),
                FLEX: Rank(FLEX, "GOLD", "I", 88),
                CLASSIC: Rank(CLASSIC, "WOOD", "III", 12),
            },
            # Ranked on one ladder only. The other two must not appear.
            THEM: {
                SOLO: Rank(SOLO, "DIAMOND", "IV", 3),
                FLEX: Rank(FLEX, tier=None),
                CLASSIC: Rank(CLASSIC, tier=None),
            },
        })
        conn.commit()
        return web_app.create_app().test_client()

    def test_all_three_ladders_are_returned(self, client):
        mine = ladders(players(client)[1])
        assert set(mine) == {SOLO, FLEX, CLASSIC}
        assert (mine[SOLO]["tier"], mine[SOLO]["division"]) == ("EMERALD", "II")
        assert mine[FLEX]["tier"] == "GOLD"
        assert mine[CLASSIC]["tier"] == "WOOD", "Classic runs its own tier names"

    def test_unranked_ladders_are_left_out(self, client):
        assert set(ladders(players(client)[2])) == {SOLO}, (
            "an unranked ladder must be absent, not present and blank"
        )

    def test_the_ladders_come_back_in_display_order(self, client):
        assert [r["queue_type"] for r in players(client)[1]["ranks"]] == [
            SOLO, FLEX, CLASSIC
        ]

    def test_each_ladder_is_labelled_by_its_initial(self, client):
        """Ten players against three ladders each: the names cost more width
        than the ranks they label, so the page shows S/F/C and puts the name in
        the tooltip."""
        ranks = players(client)[1]["ranks"]
        assert [r["queue_label"] for r in ranks] == ["S", "F", "C"]
        assert [r["queue_title"] for r in ranks] == ["Solo/Duo", "Flex", "Classic"]

    def test_the_ladder_the_game_counted_for_is_marked(self, client):
        """A Classic game moves the Classic ladder and nothing else."""
        mine = ladders(players(client)[1])
        assert mine[CLASSIC]["is_game_ladder"] is True
        assert mine[SOLO]["is_game_ladder"] is False
        assert mine[FLEX]["is_game_ladder"] is False

    def test_the_flat_rank_still_follows_the_mode(self, client):
        """The Rank column of the table shows one rank; it must be the right one."""
        me = players(client)[1]
        assert (me["tier"], me["division"]) == ("WOOD", "III")

    def test_my_rank_is_picked_out_for_the_table(self, client):
        match = client.get(f"/api/matches?account={ME}").get_json()[0]
        assert match["my_rank"]["puuid"] == ME
        assert match["my_rank"]["tier"] == "WOOD"


class TestAtMatchVersusCurrent:
    @pytest.fixture
    def client(self, conn):
        match = a_match()
        store.upsert_match(conn, match)
        # Current ranks for both ladders...
        store.save_player_ranks(conn, {
            ME: {SOLO: Rank(SOLO, "EMERALD", "II", 41),
                 CLASSIC: Rank(CLASSIC, "WOOD", "III", 12)},
        })
        # ...but a snapshot for only one of them, which is the ordinary case:
        # the capture stores the ladder the game was played on.
        store.save_participant_ranks(
            conn, match, {ME: {CLASSIC: Rank(CLASSIC, "SALT", "I", 90)}}
        )
        conn.commit()
        return web_app.create_app().test_client()

    def test_the_captured_rank_wins_for_its_own_ladder(self, client):
        classic = ladders(players(client)[1])[CLASSIC]
        assert classic["tier"] == "SALT", "the current rank overwrote the snapshot"
        assert classic["rank_at_match"] == 1

    def test_the_other_ladder_still_falls_back(self, client):
        """Half a snapshot must not cost the player their other ladders."""
        solo = ladders(players(client)[1])[SOLO]
        assert solo["tier"] == "EMERALD"
        assert solo["rank_at_match"] == 0, "a current rank must not read as a snapshot"


class TestNoRanksAtAll:
    @pytest.fixture
    def client(self, conn):
        store.upsert_match(conn, a_match())
        conn.commit()
        return web_app.create_app().test_client()

    def test_players_come_back_with_an_empty_ladder_list(self, client):
        team = players(client)
        assert len(team) == 2
        assert team[1]["ranks"] == [] and team[2]["ranks"] == []

    def test_the_flat_rank_is_empty_rather_than_missing(self, client):
        """The page reads these keys unconditionally."""
        me = team = players(client)[1]
        assert me["tier"] is None and me["division"] is None
        assert me["league_points"] is None and me["rank_at_match"] == 0
        assert team is me


class TestModesThatMoveNoLadder:
    @pytest.fixture
    def client(self, conn):
        store.upsert_match(conn, a_match(queue_id=450, game_mode="ARAM"))
        store.save_player_ranks(conn, {ME: {SOLO: Rank(SOLO, "EMERALD", "II", 41)}})
        conn.commit()
        return web_app.create_app().test_client()

    def test_nothing_is_marked_as_the_games_ladder(self, client):
        """ARAM shows your solo rank for context but cannot change it."""
        assert all(not r["is_game_ladder"] for r in players(client)[1]["ranks"])

    def test_the_solo_rank_is_still_shown(self, client):
        assert ladders(players(client)[1])[SOLO]["tier"] == "EMERALD"


class TestTeammatesTab:
    @pytest.fixture
    def client(self, conn):
        for game_id in (1, 2):
            store.upsert_match(conn, a_match(game_id, their_team=100))
        store.save_player_ranks(conn, {
            THEM: {SOLO: Rank(SOLO, "DIAMOND", "IV", 3),
                   FLEX: Rank(FLEX, "PLATINUM", "II", 55)},
        })
        conn.commit()
        return web_app.create_app().test_client()

    def test_flex_is_reported_alongside_solo_and_classic(self, client):
        """The tab listed two ladders while the match view showed three."""
        rows = client.get(f"/api/teammates?account={ME}&min_games=1").get_json()
        them = next(r for r in rows if r["puuid"] == THEM)
        assert (them["solo_tier"], them["flex_tier"]) == ("DIAMOND", "PLATINUM")
        assert them["classic_tier"] is None

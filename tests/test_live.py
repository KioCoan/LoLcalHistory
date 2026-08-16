"""The live match view.

Two things here are worth more than the rest and are tested first: that a
finished game cannot be shown as a live one, and that the client is not
rediscovered on every poll. Both were found against a real client rather than
imagined — `gameData` really does stay populated with the previous match while
you sit in a lobby, and discovery really can shell out to PowerShell.
"""

from __future__ import annotations

import pytest

from lolhist import live
from lolhist.connection import ClientUnavailable
from lolhist.ranked import Rank
from lolhist.static_data import StaticData

BLUE = [
    {"puuid": "p1", "championId": 64, "selectedPosition": "JUNGLE"},
    {"puuid": "p2", "championId": 84, "selectedPosition": "MIDDLE"},
]
RED = [
    {"puuid": "p3", "championId": 12, "selectedPosition": "UTILITY"},
]

GAME_DATA = {
    "gameId": 5001,
    "queue": {"id": 420, "gameMode": "CLASSIC", "name": "Ranked Solo", "isRanked": True},
    "teamOne": BLUE,
    "teamTwo": RED,
    "playerChampionSelections": [
        {"puuid": "p1", "championId": 64, "spell1Id": 11, "spell2Id": 4},
        {"puuid": "p2", "championId": 84, "spell1Id": 4, "spell2Id": 14},
        {"puuid": "p3", "championId": 12, "spell1Id": 4, "spell2Id": 3},
    ],
}

NAMES = {
    "p1": {"gameName": "Kiozin", "tagLine": "BR1", "summonerLevel": 379, "profileIconId": 7},
    "p2": {"gameName": "Docinho", "tagLine": "br1", "summonerLevel": 120, "profileIconId": 8},
    "p3": {"gameName": "Terceiro", "tagLine": "BR1", "summonerLevel": 44, "profileIconId": 9},
}

MASTERY = {
    "p1": [
        {"championId": 64, "championLevel": 25, "championPoints": 247_593},
        {"championId": 157, "championLevel": 11, "championPoints": 127_383},
        {"championId": 84, "championLevel": 13, "championPoints": 122_507},
        {"championId": 1, "championLevel": 2, "championPoints": 900},
    ],
    "p2": [{"championId": 84, "championLevel": 7, "championPoints": 50_000}],
    "p3": [],
}

# p1 is the logged-in account, so its losses are real. The client reports a
# flat zero for everyone else no matter how much they have played, which is
# exactly what p2 looks like here — 51 wins and, supposedly, no defeats.
RANKS = {
    "p1": {
        "RANKED_SOLO_5x5": Rank("RANKED_SOLO_5x5", "EMERALD", "II", 41, wins=6, losses=4),
        "RANKED_FLEX_SR": Rank("RANKED_FLEX_SR", "EMERALD", "II", 11, wins=10, losses=7),
    },
    "p2": {"RANKED_SOLO_5x5": Rank("RANKED_SOLO_5x5", "GOLD", "I", 8, wins=51, losses=0)},
    "p3": {},
}

STATIC = StaticData(champions={64: "Lee Sin", 84: "Akali", 12: "Alistar", 157: "Yasuo", 1: "Annie"})


class FakeLcu:
    """The League Client, answering only the paths the live view asks for."""

    def __init__(self, phase="InProgress", game_data=None):
        self.phase = phase
        self.game_data = GAME_DATA if game_data is None else game_data
        self.calls: list[str] = []

    def get_json_or_none(self, path, **params):
        self.calls.append(path)
        if path == "/lol-summoner/v1/current-summoner":
            return {"puuid": "p1"}
        if path == live.GAMEFLOW_PHASE:
            return self.phase
        if path == live.GAMEFLOW_SESSION:
            return {"phase": self.phase, "gameData": self.game_data}
        if path.startswith("/lol-summoner/v2/summoners/puuid/"):
            return NAMES.get(path.rsplit("/", 1)[-1])
        if path.startswith("/lol-champion-mastery/"):
            return MASTERY.get(path.split("/")[3], [])
        return None

    def get(self, path, **params):
        class Response:
            status_code = 404
            content = b""

        return Response()

    def close(self):
        pass


class FakeLive:
    """The in-game API. `payload=None` is a closed port, which is the norm."""

    def __init__(self, payload=None):
        self.payload = payload
        self.calls = 0

    def all_game_data(self):
        self.calls += 1
        return self.payload

    def close(self):
        pass


def in_game_payload(*, riot_ids=True):
    """What the game reports. `riot_ids=False` forces the champion fallback."""

    def player(name, tag, champion, keystone, kills):
        entry = {
            "championName": champion,
            "level": 11,
            "isDead": False,
            "respawnTimer": 0.0,
            "runes": {
                "keystone": {"id": keystone, "displayName": "Conqueror"},
                "primaryRuneTree": {"id": 8000, "displayName": "Precision"},
                "secondaryRuneTree": {"id": 8400, "displayName": "Resolve"},
            },
            "scores": {"kills": kills, "deaths": 1, "assists": 3, "creepScore": 90, "wardScore": 4.5},
            "items": [{"itemID": 3006, "slot": 0}, {"itemID": 3340, "slot": 6}],
        }
        if riot_ids:
            entry["riotIdGameName"] = name
            entry["riotIdTagLine"] = tag
        return entry

    return {
        "gameData": {"gameTime": 742.5},
        "allPlayers": [
            player("Kiozin", "br1", "Lee Sin", 8010, 5),
            player("Docinho", "BR1", "Akali", 8112, 2),
            player("Terceiro", "BR1", "Alistar", 8437, 0),
        ],
    }


@pytest.fixture
def session(monkeypatch):
    """A session wired to fakes, with the roster's rank lookup stubbed."""
    monkeypatch.setattr(live.ranked, "fetch_many", lambda client, puuids: RANKS)
    monkeypatch.setattr(live.static_data, "load", lambda client: STATIC)
    monkeypatch.setattr(live, "_mirror_icons", lambda client, players: None)

    def build(lcu=None, feed=None):
        lcu = lcu or FakeLcu()
        return live.LiveSession(connect_fn=lambda: lcu, live_client=feed or FakeLive()), lcu

    return build


# -- the phase gate --------------------------------------------------------


def test_populated_game_data_in_a_lobby_is_not_a_live_game(session):
    """The exact state a real client sits in between games.

    `gameData` still holds the match just played, so anything keyed off its
    presence would show a finished game as live.
    """
    instance, _ = session(FakeLcu(phase="Lobby"))
    snapshot = instance.snapshot()

    assert snapshot["live"] is False
    assert snapshot["players"] == []
    assert "Lobby" in snapshot["reason"]


@pytest.mark.parametrize("phase", ["None", "Matchmaking", "ChampSelect", "PreEndOfGame", "EndOfGame"])
def test_only_in_progress_counts_as_live(session, phase):
    instance, _ = session(FakeLcu(phase=phase))
    assert instance.snapshot()["live"] is False


def test_in_progress_is_live(session):
    instance, _ = session()
    snapshot = instance.snapshot()

    assert snapshot["live"] is True
    assert snapshot["game_id"] == 5001
    assert snapshot["queue_name"] == "Ranked Solo"
    assert snapshot["is_ranked"] is True
    assert [p["team_id"] for p in snapshot["players"]] == [100, 100, 200]


# -- what the client alone can answer --------------------------------------


def test_full_snapshot_without_the_in_game_api(session):
    """A closed port 2999 costs runes and live scores, and nothing else."""
    instance, _ = session(feed=FakeLive(payload=None))
    snapshot = instance.snapshot()

    assert snapshot["live"] is True
    assert snapshot["ingame"] is False

    me = snapshot["players"][0]
    assert me["name"] == "Kiozin"
    assert me["tagline"] == "BR1"
    assert me["champion_name"] == "Lee Sin"
    assert me["spell1_id"] == 11
    assert me["runes"] is None
    assert me["scores"] is None
    # The half that does not depend on the game process is all present.
    assert [r["queue_type"] for r in me["ranks"]] == ["RANKED_SOLO_5x5", "RANKED_FLEX_SR"]
    assert me["win_rate"]["rate"] == 60.0
    assert me["mastery"]["level"] == 25


def test_only_your_own_record_gets_a_win_rate():
    """The client reports wins for anyone but losses only for you.

    Trusting the zero it returns for everyone else gives the whole lobby a
    100% win rate, which is worse than showing no rate at all.
    """
    stranger = _win_rate_for("p2", complete=False)
    assert stranger["wins"] == 51
    assert stranger["losses"] is None
    assert stranger["rate"] is None
    assert stranger["complete"] is False

    mine = _win_rate_for("p1", complete=True)
    assert (mine["wins"], mine["losses"], mine["rate"]) == (6, 4, 60.0)
    assert mine["complete"] is True


def _win_rate_for(puuid, *, complete):
    return live._win_rate(RANKS[puuid], "RANKED_SOLO_5x5", complete=complete)


def test_the_logged_in_account_is_the_one_marked_complete(session):
    instance, _ = session()
    players = {p["puuid"]: p for p in instance.snapshot()["players"]}

    assert players["p1"]["is_me"] is True
    assert players["p1"]["win_rate"]["complete"] is True
    assert players["p2"]["is_me"] is False
    assert players["p2"]["win_rate"]["complete"] is False


def test_ranks_use_the_shape_the_page_already_renders(session):
    """The Matches tab's rank helper must draw these without changes."""
    instance, _ = session()
    me = instance.snapshot()["players"][0]

    solo = me["ranks"][0]
    assert solo["queue_label"] == "S"
    assert solo["queue_title"] == "Solo/Duo"
    assert solo["is_game_ladder"] is True       # queue 420 moves solo queue
    assert me["ranks"][1]["is_game_ladder"] is False
    # Read from the client seconds ago, so the "current rank, not the one they
    # held" asterisk the history tab shows would be wrong.
    assert solo["rank_at_match"] == 1


def test_a_player_with_no_ranked_games_has_no_record(session):
    """Blank, not 0% — they have not lost, they have not played."""
    instance, _ = session()
    unranked = instance.snapshot()["players"][2]

    assert unranked["win_rate"] is None
    assert unranked["ranks"] == []


def test_a_stranger_with_no_wins_shows_nothing_rather_than_zero():
    """Their only readable number is wins, so no wins means no record at all."""
    ranks = {"RANKED_SOLO_5x5": Rank("RANKED_SOLO_5x5", "IRON", "IV", 0, wins=0, losses=0)}
    assert live._win_rate(ranks, "RANKED_SOLO_5x5", complete=False) is None


def test_mastery_covers_the_pick_and_the_three_best(session):
    instance, _ = session()
    me = instance.snapshot()["players"][0]

    assert me["mastery"]["champion_id"] == 64
    assert me["mastery"]["points"] == 247_593
    assert [m["champion_name"] for m in me["top_mastery"]] == ["Lee Sin", "Yasuo", "Akali"]


def test_classic_champion_ids_find_their_mastery():
    """Classic ships champions at a 60000 offset; mastery only knows the base id."""
    assert live.base_champion_id(60084) == 84
    assert live.base_champion_id(84) == 84
    assert live.base_champion_id(0) == 0


def test_a_classic_pick_still_resolves_its_champion(session):
    game = dict(GAME_DATA, teamOne=[{"puuid": "p1", "championId": 60064}], teamTwo=[])
    instance, _ = session(FakeLcu(game_data=game))
    player = instance.snapshot()["players"][0]

    assert player["champion_name"] == "Lee Sin"
    assert player["mastery"]["points"] == 247_593


# -- the join with the in-game API -----------------------------------------


def test_runes_and_scores_join_on_riot_id(session):
    instance, _ = session(feed=FakeLive(in_game_payload()))
    snapshot = instance.snapshot()

    assert snapshot["ingame"] is True
    assert snapshot["game_time_s"] == 742

    me = snapshot["players"][0]
    assert me["runes"]["keystone_id"] == 8010
    assert me["runes"]["secondary_style_id"] == 8400
    assert me["scores"] == {"kills": 5, "deaths": 1, "assists": 3, "cs": 90, "vision": 4.5}
    # Flattened into the slots the page's build cell already reads.
    assert me["item0"] == 3006
    assert me["item6"] == 3340


def test_the_riot_id_join_ignores_tag_case(session):
    """The client reports `br1` and the game reports `BR1` for the same player."""
    instance, _ = session(feed=FakeLive(in_game_payload()))
    players = instance.snapshot()["players"]

    assert all(p["runes"] is not None for p in players)


def test_champion_name_rescues_a_player_with_no_riot_id(session):
    instance, _ = session(feed=FakeLive(in_game_payload(riot_ids=False)))
    players = instance.snapshot()["players"]

    assert [p["runes"]["keystone_id"] for p in players] == [8010, 8112, 8437]


def test_a_feed_matching_nobody_is_not_treated_as_in_game(session):
    payload = {"gameData": {"gameTime": 10.0}, "allPlayers": [
        {"riotIdGameName": "Stranger", "riotIdTagLine": "EUW", "championName": "Zed"}
    ]}
    instance, _ = session(feed=FakeLive(payload))
    snapshot = instance.snapshot()

    assert snapshot["ingame"] is False
    assert all(p["runes"] is None for p in snapshot["players"])


# -- cost control ----------------------------------------------------------


def test_the_roster_is_built_once_per_game(session):
    """The expensive half must not be refetched on every poll.

    Names and mastery are three requests per player and none of it changes
    mid-game; the page polls every few seconds.
    """
    feed = FakeLive(in_game_payload())
    instance, lcu = session(feed=feed)

    instance.snapshot()
    after_first = len(lcu.calls)
    lookups = [c for c in lcu.calls if "summoners" in c or "mastery" in c]
    assert len(lookups) == 6                      # 3 players, name + mastery each

    for _ in range(5):
        instance.snapshot()

    # Each later poll costs the phase check and the session read, nothing more.
    assert len(lcu.calls) == after_first + 10
    assert [c for c in lcu.calls if "mastery" in c] == [c for c in lookups if "mastery" in c]
    # The in-game feed genuinely changes, so it is read every time.
    assert feed.calls == 6


def test_a_new_game_rebuilds_the_roster(session):
    lcu = FakeLcu()
    instance, _ = session(lcu)
    instance.snapshot()

    lcu.game_data = dict(GAME_DATA, gameId=5002)
    lcu.calls.clear()
    instance.snapshot()

    assert any("mastery" in call for call in lcu.calls)


def test_leaving_the_game_forgets_the_roster(session):
    lcu = FakeLcu()
    instance, _ = session(lcu)
    instance.snapshot()

    lcu.phase = "Lobby"
    assert instance.snapshot()["live"] is False

    lcu.phase = "InProgress"
    lcu.calls.clear()
    instance.snapshot()
    assert any("mastery" in call for call in lcu.calls)


def test_a_closed_client_is_not_rediscovered_on_every_poll(monkeypatch):
    """Discovery can spawn a PowerShell process, and this is polled constantly."""
    attempts = []

    def refuse():
        attempts.append(1)
        raise ClientUnavailable("no client")

    instance = live.LiveSession(connect_fn=refuse, live_client=FakeLive())
    for _ in range(20):
        assert instance.snapshot()["live"] is False

    assert len(attempts) == 1


def test_the_backoff_expires(monkeypatch):
    attempts = []

    def refuse():
        attempts.append(1)
        raise ClientUnavailable("no client")

    clock = [1000.0]
    monkeypatch.setattr(live.time, "monotonic", lambda: clock[0])

    instance = live.LiveSession(connect_fn=refuse, live_client=FakeLive())
    instance.snapshot()
    instance.snapshot()
    assert len(attempts) == 1

    clock[0] += live.RECONNECT_BACKOFF_SECONDS + 1
    instance.snapshot()
    assert len(attempts) == 2


def test_a_client_that_stops_answering_is_dropped(session):
    lcu = FakeLcu()
    instance, _ = session(lcu)
    assert instance.snapshot()["live"] is True

    lcu.phase = None                      # every request now returns nothing
    snapshot = instance.snapshot()
    assert snapshot["live"] is False
    assert "not running" in snapshot["reason"]


# -- League Classic's old runes and masteries ------------------------------


def a_loadout(rune_page=1, mastery_page=2):
    """An account loadout shaped like the client's, with two pages filled in."""
    lo = {
        "ACTIVE_RUNE_PAGE": {"itemId": rune_page, "inventoryType": "JADE_RUNE_PAGE"},
        "ACTIVE_MASTERY_PAGE": {"itemId": mastery_page, "inventoryType": "JADE_MASTERY_PAGE"},
        f"RUNE_PAGE_{rune_page}_NAME": {"data": {"name": "ad"}},
        f"MASTERY_PAGE_{mastery_page}_NAME": {
            "data": {"nameKey": "jade_mastery_preset_ad_offense_defense"}
        },
    }
    # Seven of one mark and two of another, then nine of a kind for the rest.
    marks = [775245] * 7 + [775251] * 2
    for i, rune in enumerate(marks, start=1):
        lo[f"RUNE_PAGE_{rune_page}_RED_{i}"] = {"itemId": rune}
    for i in range(1, 10):
        lo[f"RUNE_PAGE_{rune_page}_YELLOW_{i}"] = {"itemId": 775317}
        lo[f"RUNE_PAGE_{rune_page}_BLUE_{i}"] = {"itemId": 775289}
    for i, quint in enumerate([775335, 775335, 775412], start=1):
        lo[f"RUNE_PAGE_{rune_page}_QUINT_{i}"] = {"itemId": quint}
    for i in range(1, 31):
        lo[f"MASTERY_PAGE_{mastery_page}_MASTERY_{i}"] = {"itemId": 512 if i <= 18 else 522}
    return lo


JADE_NAMES = {
    775245: "Mark of Attack Damage", 775251: "Mark of Critical Chance",
    775317: "Seal of Armor", 775289: "Glyph of Magic Resist",
    775335: "Quintessence of Attack Damage", 775412: "Quintessence of Life Steal",
}


class JadeClient(FakeLcu):
    """In a League Classic game, with an old rune page on the account.

    The queue matters: the Classic loadout is reached for on the strength of
    the mode alone, so a client in any other mode must not see it.
    """

    def __init__(self, loadout=None, **kw):
        kw.setdefault("game_data", dict(GAME_DATA, queue=CLASSIC_QUEUE))
        super().__init__(**kw)
        self.loadout = a_loadout() if loadout is None else loadout

    def get_json_or_none(self, path, **params):
        if path == live.LOADOUTS:
            self.calls.append(path)
            return [{"scope": "ACCOUNT", "loadout": self.loadout}] if self.loadout else []
        return super().get_json_or_none(path, **params)


@pytest.fixture(autouse=True)
def _jade_names(monkeypatch):
    monkeypatch.setattr(live.static_data, "asset_map", lambda name, field: JADE_NAMES)


def test_the_classic_rune_page_is_read_from_the_account_loadout():
    """Classic reports no modern perks at all; its page lives on the loadout."""
    page = live.jade_loadout(JadeClient())["runes"]

    assert page["page_name"] == "ad"
    # Identical runes collapse to one entry with a count, commonest first.
    assert page["marks"] == [
        {"id": 775245, "name": "Mark of Attack Damage", "count": 7},
        {"id": 775251, "name": "Mark of Critical Chance", "count": 2},
    ]
    assert page["seals"] == [{"id": 775317, "name": "Seal of Armor", "count": 9}]
    assert page["glyphs"] == [{"id": 775289, "name": "Glyph of Magic Resist", "count": 9}]
    assert [q["count"] for q in page["quints"]] == [2, 1]


def test_the_classic_mastery_page_counts_points_per_mastery():
    mast = live.jade_loadout(JadeClient())["masteries"]

    assert mast["points"] == 30
    assert mast["spent"] == [{"id": 512, "points": 18}, {"id": 522, "points": 12}]
    # No translation for a preset's key is served anywhere, so it is tidied
    # into something readable rather than shown raw.
    assert mast["page_name"] == "AD Offense Defense"


def test_a_custom_page_keeps_its_own_name():
    lo = a_loadout()
    lo["MASTERY_PAGE_2_NAME"] = {"data": {"name": "my page"}}
    assert live.jade_loadout(JadeClient(lo))["masteries"]["page_name"] == "my page"


def test_no_loadout_is_not_an_error():
    assert live.jade_loadout(JadeClient(loadout={})) is None


def test_only_the_active_page_is_read():
    """Eight rune pages exist; the tab must show the one actually equipped."""
    lo = a_loadout(rune_page=3)
    page = live.jade_loadout(JadeClient(lo))["runes"]
    assert page["seals"] == [{"id": 775317, "name": "Seal of Armor", "count": 9}]


CLASSIC_QUEUE = {"id": 4310, "gameMode": "JADE", "name": "Classic 5v5", "isRanked": True}
# ARAM: Mayhem. Reports runes for nobody, exactly like Classic does — which is
# what made "no modern runes" a broken test for "this is Classic".
MAYHEM_QUEUE = {"id": 2400, "gameMode": "KIWI", "name": "ARAM: Mayhem"}


@pytest.mark.parametrize("queue_id, game_mode, expected", [
    (4310, "JADE", True),
    (4320, "JADE", True),
    (None, "jade", True),          # matched case-insensitively
    (2400, "KIWI", False),         # ARAM: Mayhem
    (450, "ARAM", False),
    (420, "CLASSIC", False),       # Summoner's Rift, despite the mode's name
    (None, None, False),
])
def test_only_league_classic_counts_as_classic(queue_id, game_mode, expected):
    assert live.is_classic(queue_id, game_mode) is expected


def test_mayhem_does_not_borrow_the_classic_rune_page(session, monkeypatch):
    """The bug this exists for: a Mayhem game shown wearing the Classic page.

    Mayhem reports no runes for anyone, and the old gate read that absence as
    "this must be Classic".
    """
    game = dict(GAME_DATA, queue=MAYHEM_QUEUE)
    lcu = JadeClient(game_data=game)
    instance, _ = session(lcu)
    monkeypatch.setattr(live, "_mirror_icons", lambda client, players: None)
    snapshot = instance.snapshot()

    assert snapshot["queue_name"] == "ARAM: Mayhem"
    assert all(p["classic_runes"] is None for p in snapshot["players"])
    assert all(p["masteries"] is None for p in snapshot["players"])
    # And the loadout is not even fetched, since there is nothing there for it.
    assert live.LOADOUTS not in lcu.calls


def test_classic_runes_land_on_your_row_and_nobody_elses(session, monkeypatch):
    """The loadout is account-scoped, so it can only ever describe you."""
    lcu = JadeClient()
    instance, _ = session(lcu)
    monkeypatch.setattr(live, "_mirror_icons", lambda client, players: None)
    players = {p["puuid"]: p for p in instance.snapshot()["players"]}

    assert players["p1"]["classic_runes"]["page_name"] == "ad"
    assert players["p1"]["masteries"]["points"] == 30
    assert players["p2"]["classic_runes"] is None
    assert players["p2"]["masteries"] is None


def test_the_loadout_is_read_once_per_match(session, monkeypatch):
    lcu = JadeClient()
    instance, _ = session(lcu)
    monkeypatch.setattr(live, "_mirror_icons", lambda client, players: None)
    for _ in range(4):
        instance.snapshot()

    assert lcu.calls.count(live.LOADOUTS) == 1


def test_a_modern_game_does_not_reach_for_the_classic_loadout(session, monkeypatch):
    """Summoner's Rift has no old page behind it, so it is never asked for."""
    lcu = JadeClient(game_data=GAME_DATA)          # queue 420
    instance, _ = session(lcu, feed=FakeLive(in_game_payload()))
    monkeypatch.setattr(live, "_mirror_icons", lambda client, players: None)
    players = instance.snapshot()["players"]

    assert live.LOADOUTS not in lcu.calls
    assert all(p["classic_runes"] is None for p in players)
    # The modern runes it does have are untouched by any of this.
    assert players[0]["runes"]["keystone_id"] == 8010


def test_a_broken_lookup_does_not_take_the_dashboard_down(session, monkeypatch):
    instance, _ = session()
    monkeypatch.setattr(
        live.ranked, "fetch_many", lambda client, puuids: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    snapshot = instance.snapshot()

    assert snapshot["live"] is False
    assert snapshot["players"] == []

"""Normalizer tests.

The two history fixtures are real payloads captured from the client — one
League Classic game (queue 4310) and one ARAM: Mayhem game (queue 2400) — so
these tests pin the mapping against data the client actually produced, and run
with the client closed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lolhist.normalize import Bag, _as_bool, looks_like_eog, normalize, normalize_history_game
from lolhist.static_data import StaticData

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def classic() -> dict:
    return load("history_classic.json")


@pytest.fixture
def mayhem() -> dict:
    return load("history_mayhem.json")


@pytest.fixture
def static() -> StaticData:
    return StaticData(
        champions={777: "Yone", 60029: "Twitch"},
        queues={2400: "ARAM: Mayhem", 4310: "Classic"},
        augments={1077: "Soul Siphon", 1361: "Draw Your Sword"},
    )


class TestBag:
    def test_matches_across_naming_conventions(self):
        """The two payloads disagree on casing; one lookup must serve both."""
        assert Bag({"CHAMPIONS_KILLED": 7}).int("championsKilled") == 7
        assert Bag({"championsKilled": 7}).int("CHAMPIONS_KILLED") == 7
        assert Bag({"champions_killed": 7}).int("championsKilled") == 7

    def test_first_present_candidate_wins(self):
        bag = Bag({"deaths": 3})
        assert bag.int("numDeaths", "deaths") == 3

    def test_missing_and_malformed_are_none(self):
        assert Bag({}).int("kills") is None
        assert Bag({"kills": "abc"}).int("kills") is None
        assert Bag(None).str("anything") is None


class TestWinParsing:
    @pytest.mark.parametrize("value", [True, 1, "Win", "true"])
    def test_truthy_forms(self, value):
        assert _as_bool(value) is True

    @pytest.mark.parametrize("value", [False, 0, "Fail", "false"])
    def test_falsy_forms(self, value):
        assert _as_bool(value) is False

    def test_unknown_is_none(self):
        assert _as_bool("maybe") is None
        assert _as_bool(None) is None


class TestClassicGame:
    def test_match_level_fields(self, classic, static):
        match = normalize_history_game(classic, static)
        assert match.game_id > 0
        assert match.platform_id == "BR1"
        assert match.queue_id == 4310
        assert match.game_mode == "JADE"
        assert match.game_duration_s > 0
        assert match.game_creation_ms > 0
        assert match.source == "history"
        assert match.winning_team_id in (100, 200)

    def test_full_lobby_with_one_winning_side(self, classic, static):
        """Detail payloads carry all ten players, five of them winners."""
        match = normalize_history_game(classic, static)
        assert len(match.participants) == 10
        assert sum(1 for p in match.participants if p.win) == 5
        assert sum(1 for p in match.participants if p.win is False) == 5

    def test_players_are_identified_by_puuid(self, classic, static):
        match = normalize_history_game(classic, static)
        puuids = {p.puuid for p in match.participants}
        assert len(puuids) == 10
        assert all(p.puuid for p in match.participants)
        assert any(p.riot_id_game_name for p in match.participants)

    def test_summoner_spells_are_read_from_the_participant(self, classic, static):
        """Regression: spells sit beside `stats`, not inside it."""
        match = normalize_history_game(classic, static)
        assert all(p.spell1_id is not None for p in match.participants)
        assert all(p.spell2_id is not None for p in match.participants)

    def test_core_stats_populated(self, classic, static):
        match = normalize_history_game(classic, static)
        for p in match.participants:
            assert p.kills is not None and p.deaths is not None and p.assists is not None
            assert p.cs is not None
            assert p.gold_earned is not None
            assert p.champion_id is not None
            assert p.team_id in (100, 200)

    def test_placement_is_absent_outside_placement_modes(self, classic, static):
        """A literal 0 in the payload means 'not applicable', not first place."""
        match = normalize_history_game(classic, static)
        assert all(p.placement is None for p in match.participants)


class TestMayhemGame:
    def test_queue_and_mode(self, mayhem, static):
        match = normalize_history_game(mayhem, static)
        assert match.queue_id == 2400
        assert match.game_mode == "KIWI"
        assert match.queue_name == "ARAM: Mayhem"

    def test_augments_are_captured(self, mayhem, static):
        match = normalize_history_game(mayhem, static)
        with_augments = [p for p in match.participants if p.augments]
        assert with_augments, "Mayhem games must yield augments"

    def test_empty_augment_slots_are_dropped(self, mayhem, static):
        """Unused slots come through as 0 and must not become augment rows."""
        match = normalize_history_game(mayhem, static)
        for p in match.participants:
            assert all(a.augment_id for a in p.augments)
            slots = [a.slot for a in p.augments]
            assert slots == sorted(slots)
            assert len(slots) == len(set(slots))

    def test_augment_names_resolve_when_known(self, mayhem, static):
        match = normalize_history_game(mayhem, static)
        named = [a for p in match.participants for a in p.augments if a.augment_name]
        ids = {a.augment_id for p in match.participants for a in p.augments}
        if ids & set(static.augments):
            assert named, "known augment ids should resolve to names"

    def test_unknown_ids_keep_their_number(self, mayhem):
        """Name lookup is a convenience; the id is the thing we store."""
        match = normalize_history_game(mayhem, StaticData())
        for p in match.participants:
            for augment in p.augments:
                assert augment.augment_id
                assert augment.augment_name is None


class TestSourceDetection:
    def test_history_payload_is_not_mistaken_for_eog(self, classic):
        assert looks_like_eog(classic) is False

    def test_eog_shape_is_recognised(self):
        payload = {"gameId": 1, "teams": [{"teamId": 100, "players": []}]}
        assert looks_like_eog(payload) is True

    def test_dispatch_follows_the_payload(self, classic, static):
        assert normalize(classic, static).source == "history"


@pytest.fixture
def eog() -> dict:
    return load("eog_mayhem.json")


class TestEndOfGameBlock:
    """Against a real ARAM: Mayhem end-of-game payload captured from the client.

    This block disagrees with the history payload on nearly everything: it uses
    SCREAMING_SNAKE stat names, hangs players off teams, keeps items and spells
    outside `stats`, times the game's end rather than its start, and omits both
    the queue id and the platform.
    """

    def test_match_level_fields(self, eog, static):
        match = normalize(eog, static, source="eog")
        assert match.source == "eog"
        assert match.game_id == 3270680426
        assert match.game_mode == "KIWI"
        assert match.game_duration_s > 0
        assert match.winning_team_id in (100, 200)

    def test_start_time_is_derived_from_the_end(self, eog, static):
        """The payload timestamps the end; a null start would sort it last."""
        match = normalize(eog, static, source="eog")
        assert match.game_creation_ms
        expected = eog["endOfGameTimestamp"] - eog["gameLength"] * 1000
        assert match.game_creation_ms == expected

    def test_platform_hint_is_applied(self, eog, static):
        """platform_id is half the primary key and this payload omits it."""
        assert normalize(eog, static, source="eog").platform_id == ""
        match = normalize(eog, static, source="eog", platform_hint="BR1")
        assert match.platform_id == "BR1"

    def test_queue_id_is_absent_and_that_is_expected(self, eog, static):
        """queueType is the mode codename, not a number — the sweep fills it."""
        match = normalize(eog, static, source="eog")
        assert match.queue_id is None
        assert match.game_mode == "KIWI"

    def test_all_ten_players_with_one_winning_side(self, eog, static):
        match = normalize(eog, static, source="eog")
        assert len(match.participants) == 10
        assert sum(1 for p in match.participants if p.win) == 5
        assert sum(1 for p in match.participants if p.win is False) == 5

    def test_losers_are_read_from_the_lose_key(self, eog, static):
        """Losing players carry LOSE=1 and no WIN key at all."""
        losers = [p for p in match_of(eog, static).participants if p.win is False]
        assert losers

    def test_screaming_snake_stats_are_mapped(self, eog, static):
        match = normalize(eog, static, source="eog")
        for p in match.participants:
            assert p.kills is not None, "CHAMPIONS_KILLED"
            assert p.deaths is not None, "NUM_DEATHS"
            assert p.assists is not None, "ASSISTS"
            assert p.gold_earned is not None, "GOLD_EARNED"
            assert p.damage_to_champions is not None, "TOTAL_DAMAGE_DEALT_TO_CHAMPIONS"
            assert p.cs is not None, "MINIONS_KILLED + NEUTRAL_MINIONS_KILLED"

    def test_items_and_spells_come_from_the_player(self, eog, static):
        """Regression: items are a list on the player, not ITEM0..ITEM6 in stats."""
        match = normalize(eog, static, source="eog")
        assert any(p.items for p in match.participants)
        assert all(p.spell1_id is not None for p in match.participants)
        assert all(p.champ_level is not None for p in match.participants)

    def test_augments_are_captured(self, eog, static):
        """PLAYER_AUGMENT_1..6, of which the trailing ones are 0 and dropped."""
        match = normalize(eog, static, source="eog")
        with_augments = [p for p in match.participants if p.augments]
        assert len(with_augments) == 10
        for p in with_augments:
            assert all(a.augment_id for a in p.augments)

    def test_champion_names_come_from_the_payload(self, eog):
        """championName is supplied, so names survive an empty asset cache."""
        match = normalize(eog, StaticData(), source="eog")
        assert all(p.champion_name for p in match.participants)

    def test_identities_present(self, eog, static):
        match = normalize(eog, static, source="eog")
        assert all(p.puuid for p in match.participants)
        assert all(p.riot_id_game_name for p in match.participants)

    def test_detected_as_eog_without_being_told(self, eog):
        assert looks_like_eog(eog) is True


def match_of(payload, static):
    return normalize(payload, static, source="eog")

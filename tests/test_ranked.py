"""Rank parsing, ladder selection and LP movement."""

from __future__ import annotations

import pytest

from lolhist import ranked
from lolhist.ranked import CLASSIC, FLEX, SOLO, Rank


def entry(tier="EMERALD", division="II", lp=41, wins=6, losses=4):
    return {
        "tier": tier, "division": division, "leaguePoints": lp,
        "wins": wins, "losses": losses, "queueType": "x",
    }


def payload(**queues):
    return {"queueMap": {k: v for k, v in queues.items()}}


class TestParse:
    def test_reads_the_tracked_ladders(self):
        ranks = ranked.parse(payload(
            RANKED_SOLO_5x5=entry(),
            JADE_RANKED_SOLO_5x5=entry("SILVER", "II", 97, 30, 14),
            RANKED_FLEX_SR=entry("EMERALD", "II", 11),
        ))
        assert set(ranks) == {SOLO, CLASSIC, FLEX}
        assert ranks[SOLO].tier == "EMERALD"
        assert ranks[CLASSIC].tier == "SILVER"
        assert ranks[CLASSIC].league_points == 97
        assert ranks[CLASSIC].wins == 30

    def test_unranked_entries_are_parsed_but_flagged(self):
        """The client returns empty strings rather than omitting the queue."""
        ranks = ranked.parse(payload(RANKED_SOLO_5x5=entry(tier="", division="NA", lp=0)))
        assert ranks[SOLO].is_ranked is False
        assert ranks[SOLO].tier is None
        assert ranks[SOLO].label() is None

    def test_tft_and_other_queues_are_ignored(self):
        ranks = ranked.parse(payload(RANKED_TFT=entry(), RANKED_SOLO_5x5=entry()))
        assert "RANKED_TFT" not in ranks

    @pytest.mark.parametrize("bad", [None, {}, [], {"queueMap": None}, "nope"])
    def test_malformed_payloads_are_empty(self, bad):
        assert ranked.parse(bad) == {}


class TestLadderSelection:
    """A Classic game must be read against the Classic ladder, not solo queue —
    they are separate ladders with separate tiers."""

    @pytest.mark.parametrize("queue_id", [4310, 4320])
    def test_classic_queues_use_the_classic_ladder(self, queue_id):
        assert ranked.ranked_queue_for(queue_id, "JADE") == CLASSIC

    def test_game_mode_alone_is_enough(self):
        assert ranked.ranked_queue_for(None, "JADE") == CLASSIC

    @pytest.mark.parametrize("queue_id,mode", [(2400, "KIWI"), (450, "ARAM"), (420, "CLASSIC")])
    def test_everything_else_uses_solo(self, queue_id, mode):
        assert ranked.ranked_queue_for(queue_id, mode) == SOLO


class TestLabels:
    def test_includes_division_and_points(self):
        assert Rank(SOLO, "EMERALD", "II", 41).label() == "Emerald II 41 LP"

    def test_classic_tier_names_render_the_same_way(self):
        assert Rank(CLASSIC, "WOOD", "III", 82).label() == "Wood III 82 LP"
        assert Rank(CLASSIC, "SALT", "II", 19).label() == "Salt II 19 LP"

    def test_apex_tiers_have_no_meaningful_division(self):
        assert Rank(SOLO, "MASTER", "NA", 320).label() == "Master 320 LP"

    def test_unranked_has_no_label(self):
        assert Rank(SOLO).label() is None


class TestPointsDiff:
    def test_simple_gain_and_loss(self):
        before = Rank(SOLO, "EMERALD", "II", 41)
        assert ranked.diff_points(before, Rank(SOLO, "EMERALD", "II", 59)) == 18
        assert ranked.diff_points(before, Rank(SOLO, "EMERALD", "II", 20)) == -21

    def test_promotion_reads_as_a_gain(self):
        """Naive subtraction would call a promotion a 75 point loss."""
        before = Rank(SOLO, "EMERALD", "II", 98)
        after = Rank(SOLO, "EMERALD", "I", 12)
        delta = ranked.diff_points(before, after)
        assert delta > 0, "promoting is not a loss"

    def test_demotion_reads_as_a_loss(self):
        before = Rank(SOLO, "EMERALD", "IV", 3)
        after = Rank(SOLO, "PLATINUM", "I", 75)
        assert ranked.diff_points(before, after) < 0

    def test_the_promotion_that_read_as_842(self):
        """The report. Wood I 80 LP won a game and landed Silver IV 22 LP.

        Both ladders shared one list of tiers, and Iron and Bronze sat between
        Wood and Silver in it. Classic has neither, so a one-tier promotion was
        charged for three and a 42 point win was recorded as +842.

        42 is not a guess: 80 + 42 is 122, which caps at 100 and carries the
        remaining 22 into the new tier — exactly the rank that was shown. The
        same account's other wins that session were +42, +42, +40 and +39.
        """
        before = Rank(CLASSIC, "WOOD", "I", 80)
        after = Rank(CLASSIC, "SILVER", "IV", 22)
        assert ranked.diff_points(before, after) == 42

    def test_the_classic_ladder_has_no_gaps_in_it(self):
        """Every step up Classic costs the same, which is what went wrong.

        A tier the ladder does not have, sitting between two it does, is
        invisible until someone is promoted across it.
        """
        tiers = ranked.LADDER_TIERS[CLASSIC]
        steps = [
            ranked.diff_points(Rank(CLASSIC, low, "I", 100), Rank(CLASSIC, high, "IV", 0))
            for low, high in zip(tiers, tiers[1:])
        ]
        assert steps == [0] * len(steps), dict(zip(zip(tiers, tiers[1:]), steps))

    def test_the_standard_ladder_has_no_gaps_either(self):
        tiers = [t for t in ranked.LADDER_TIERS[SOLO] if t != "GRANDMASTER"]
        tiers = [t for t in tiers if t != "CHALLENGER"]
        steps = [
            ranked.diff_points(Rank(SOLO, low, "I", 100), Rank(SOLO, high, "IV", 0))
            for low, high in zip(tiers, tiers[1:])
        ]
        assert steps == [0] * len(steps), dict(zip(zip(tiers, tiers[1:]), steps))

    def test_the_two_ladders_are_not_the_same_ladder(self):
        """Wood to Silver is one step on Classic and does not exist elsewhere."""
        assert "IRON" not in ranked.LADDER_TIERS[CLASSIC]
        assert "BRONZE" not in ranked.LADDER_TIERS[CLASSIC]
        assert "EMERALD" not in ranked.LADDER_TIERS[CLASSIC]
        assert "LEGEND" not in ranked.LADDER_TIERS[SOLO]

    def test_classic_tops_out_at_legend(self):
        before = Rank(CLASSIC, "DIAMOND", "I", 91)
        after = Rank(CLASSIC, "LEGEND", "NA", 12)
        assert ranked.diff_points(before, after) == 21

    def test_the_apex_tiers_are_one_pool_of_points(self):
        """Master, Grandmaster and Challenger are cutoffs, not tiers.

        Crossing one moves nobody's LP, so charging a tier for the crossing
        invents 400 points — the same fault as the merged ladder, one rung up.
        """
        at_500 = Rank(SOLO, "MASTER", "NA", 500)
        assert ranked.diff_points(at_500, Rank(SOLO, "GRANDMASTER", "NA", 500)) == 0
        assert ranked.diff_points(at_500, Rank(SOLO, "CHALLENGER", "NA", 540)) == 40

    def test_reaching_the_apex_still_counts_what_was_won(self):
        before = Rank(SOLO, "DIAMOND", "I", 88)
        after = Rank(SOLO, "MASTER", "NA", 8)
        assert ranked.diff_points(before, after) == 20

    def test_a_position_impossible_on_its_ladder_invents_nothing(self):
        """Iron on the Classic ladder cannot be placed, so it is not placed."""
        before = Rank(CLASSIC, "WOOD", "I", 99)
        after = Rank(CLASSIC, "IRON", "IV", 8)
        assert ranked.diff_points(before, after) != 401

    def test_no_change_is_zero(self):
        rank = Rank(SOLO, "EMERALD", "II", 41)
        assert ranked.diff_points(rank, rank) == 0

    def test_unranked_or_missing_sides_give_nothing(self):
        ranked_entry = Rank(SOLO, "EMERALD", "II", 41)
        assert ranked.diff_points(None, ranked_entry) is None
        assert ranked.diff_points(ranked_entry, None) is None
        assert ranked.diff_points(Rank(SOLO), ranked_entry) is None

    def test_unknown_tier_names_fall_back_to_raw_points(self):
        """A ladder we do not know the tier order of still yields something."""
        before = Rank("NEW_LADDER", "MYTHIC", "II", 10)
        after = Rank("NEW_LADDER", "MYTHIC", "II", 25)
        assert ranked.diff_points(before, after) == 15

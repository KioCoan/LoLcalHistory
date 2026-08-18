"""Reading rank from the client, for yourself and for anyone you played with.

`/lol-ranked/v1/ranked-stats/{puuid}` answers for arbitrary players, not just
the logged-in account, which is what makes showing your team-mates' ranks
possible at all.

The client keys ranks by an internal queue type. Two matter here:

    RANKED_SOLO_5x5        Summoner's Rift solo/duo
    JADE_RANKED_SOLO_5x5   League Classic's own ladder

They are genuinely separate ladders with separate tiers — Classic even uses its
own tier names (SALT, WOOD, ...) alongside the familiar ones — so a Classic
game must be shown against the Classic ladder, not solo queue.

Only *current* rank is ever available. Rank as it stood during an older game
cannot be recovered, which is why the watcher snapshots it at capture time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable

from .client import LcuClient

log = logging.getLogger(__name__)

CURRENT_RANKED = "/lol-ranked/v1/current-ranked-stats"
RANKED_BY_PUUID = "/lol-ranked/v1/ranked-stats/{puuid}"

SOLO = "RANKED_SOLO_5x5"
FLEX = "RANKED_FLEX_SR"
CLASSIC = "JADE_RANKED_SOLO_5x5"

# Queues whose games belong to the League Classic ladder rather than solo queue.
CLASSIC_QUEUE_IDS = {4310, 4320}
CLASSIC_GAME_MODES = {"JADE"}

# The ladders worth storing. Others exist (TFT variants) but are noise here.
# Ordered as they are shown: solo queue is what most people mean by "rank".
TRACKED_QUEUES = (SOLO, FLEX, CLASSIC)

QUEUE_LABELS = {SOLO: "Solo/Duo", FLEX: "Flex", CLASSIC: "Classic"}

# One letter each. A team list puts ten players against three ladders apiece, so
# the names alone cost more width than the ranks they label; the full name goes
# in the tooltip.
QUEUE_INITIALS = {SOLO: "S", FLEX: "F", CLASSIC: "C"}

TIER_LABELS = {
    "": None,
    "NONE": None,
    "UNRANKED": None,
}


@dataclass(frozen=True)
class Rank:
    queue_type: str
    tier: str | None = None
    division: str | None = None
    league_points: int | None = None
    wins: int | None = None
    losses: int | None = None

    @property
    def is_ranked(self) -> bool:
        return bool(self.tier)

    def label(self, with_points: bool = True) -> str | None:
        """"Emerald II 41 LP", or None when the player is unranked here."""
        if not self.tier:
            return None
        parts = [self.tier.title()]
        if self.division and self.division not in ("NA", ""):
            parts.append(self.division)
        text = " ".join(parts)
        if with_points and self.league_points is not None:
            text += f" {self.league_points} LP"
        return text


def ranked_queue_for(queue_id: int | None, game_mode: str | None) -> str:
    """Which ladder a match should be *displayed* against."""
    if queue_id in CLASSIC_QUEUE_IDS or (game_mode or "").upper() in CLASSIC_GAME_MODES:
        return CLASSIC
    return SOLO


# Queues that actually move a ladder. ARAM and Mayhem show your solo rank for
# context but cannot change it, so they must not be set up to await an LP
# change — nothing would ever arrive, and the pending game would sit there ready
# to absorb the next real one.
RANKED_QUEUE_IDS = {420, 440} | CLASSIC_QUEUE_IDS


def affects_ladder(queue_id: int | None, game_mode: str | None) -> str | None:
    """The ladder this game changes, or None if it changes nothing."""
    if queue_id in CLASSIC_QUEUE_IDS or (game_mode or "").upper() in CLASSIC_GAME_MODES:
        return CLASSIC
    if queue_id == 420:
        return SOLO
    if queue_id == 440:
        return FLEX
    return None


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.upper() in TIER_LABELS:
        return None
    return text


def parse(payload: Any) -> dict[str, Rank]:
    """Turn a ranked-stats payload into {queue_type: Rank} for tracked queues."""
    if not isinstance(payload, dict):
        return {}
    queue_map = payload.get("queueMap")
    if not isinstance(queue_map, dict):
        return {}

    ranks: dict[str, Rank] = {}
    for queue_type in TRACKED_QUEUES:
        entry = queue_map.get(queue_type)
        if not isinstance(entry, dict):
            continue
        ranks[queue_type] = Rank(
            queue_type=queue_type,
            tier=_clean(entry.get("tier")),
            division=_clean(entry.get("division")),
            league_points=entry.get("leaguePoints"),
            wins=entry.get("wins"),
            losses=entry.get("losses"),
        )
    return ranks


def fetch_mine(client: LcuClient) -> dict[str, Rank]:
    return parse(client.get_json_or_none(CURRENT_RANKED))


def fetch_for(client: LcuClient, puuid: str) -> dict[str, Rank]:
    return parse(client.get_json_or_none(RANKED_BY_PUUID.format(puuid=puuid)))


def fetch_many(client: LcuClient, puuids: Iterable[str]) -> dict[str, dict[str, Rank]]:
    """Ranks for several players. One request each; a lobby is only ten people."""
    results: dict[str, dict[str, Rank]] = {}
    for puuid in puuids:
        if not puuid or puuid in results:
            continue
        try:
            ranks = fetch_for(client, puuid)
        except Exception as exc:  # a rank lookup must never break a capture
            log.debug("rank lookup failed for %s: %s", puuid[:8], exc)
            continue
        if ranks:
            results[puuid] = ranks
    return results


def diff_points(before: Rank | None, after: Rank | None) -> int | None:
    """LP gained or lost between two observations of the same ladder.

    Tier changes make raw LP misleading — promoting resets you to a low LP in
    the new tier, so a +18 win can read as -75. The tier is folded into an
    absolute position first so the difference stays meaningful.
    """
    if before is None or after is None:
        return None
    if before.league_points is None or after.league_points is None:
        return None
    if not before.is_ranked or not after.is_ranked:
        return None
    return _absolute(after) - _absolute(before)


# Each ladder has its own tiers, low to high. These are not one ladder under two
# sets of names: League Classic has no Iron, Bronze or Emerald, and tops out at
# Legend rather than running Master through Challenger.
#
# Holding both in one list is what this exists to prevent. Iron and Bronze sat
# between Wood and Silver, so a Classic promotion from Wood I to Silver IV was
# charged for three tiers where the ladder has one, and a game worth 42 points
# was recorded as +842. Anything unknown still falls back to comparing LP alone.
_STANDARD_TIERS = (
    "IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND",
    "MASTER", "GRANDMASTER", "CHALLENGER",
)
_CLASSIC_TIERS = ("SALT", "WOOD", "SILVER", "GOLD", "PLATINUM", "DIAMOND", "LEGEND")

LADDER_TIERS = {SOLO: _STANDARD_TIERS, FLEX: _STANDARD_TIERS, CLASSIC: _CLASSIC_TIERS}

# Master, Grandmaster and Challenger are not three tiers' worth of points. They
# are one pool of LP with two cutoffs drawn through it, so crossing a cutoff
# moves nobody's LP and they share a rung. Giving each its own would invent 400
# points per crossing, exactly as the merged ladder did.
#
# Whether Classic's Legend works the same way is not known — no account here has
# reached it. If it turns out to share Diamond's pool, it belongs in this map.
_SHARES_RUNG_WITH = {"GRANDMASTER": "MASTER", "CHALLENGER": "MASTER"}

_DIVISION_ORDER = {"IV": 0, "III": 1, "II": 2, "I": 3}
_POINTS_PER_DIVISION = 100
_DIVISIONS_PER_TIER = 4


def _absolute(rank: Rank) -> int:
    """A single comparable number for a tier/division/LP position.

    Only meaningful against another position on the same ladder: the rungs are
    counted from that ladder's own bottom tier, and two ladders do not have the
    same number of them.
    """
    tiers = LADDER_TIERS.get(rank.queue_type, _STANDARD_TIERS)
    tier = (rank.tier or "").upper()
    try:
        tier_index = tiers.index(_SHARES_RUNG_WITH.get(tier, tier))
    except ValueError:
        return rank.league_points or 0
    division_index = _DIVISION_ORDER.get((rank.division or "").upper(), 0)
    rungs = tier_index * _DIVISIONS_PER_TIER + division_index
    return rungs * _POINTS_PER_DIVISION + (rank.league_points or 0)

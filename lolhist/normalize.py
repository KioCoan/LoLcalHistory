"""Turning raw LCU payloads into `Match` objects.

Two payload shapes arrive here and they do not agree with each other:

* `/lol-match-history/v1/games/{id}` uses the legacy match shape — a flat
  `participants` list with camelCase `stats`, and names in a parallel
  `participantIdentities` list.
* the end-of-game block groups players under `teams[].players[]` and uses the
  old SCREAMING_SNAKE stat names (`CHAMPIONS_KILLED`, `NUM_DEATHS`, ...).

Rather than hardcode one convention, every lookup goes through `Bag`, which
normalizes keys to lowercase alphanumerics. `CHAMPIONS_KILLED`,
`championsKilled` and `champions_killed` all resolve to the same field, so the
mappers keep working when Riot changes casing — which they have done before.

Anything not mapped here still survives in the gzipped raw archive.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .models import Augment, Match, Participant
from .static_data import StaticData

log = logging.getLogger(__name__)

_AUGMENT_KEY = re.compile(r"^(?:player)?augment(\d+)$")


class Bag:
    """Case- and separator-insensitive read-only view over a payload dict."""

    def __init__(self, raw: Any) -> None:
        self.raw: dict[str, Any] = raw if isinstance(raw, dict) else {}
        self._index: dict[str, Any] = {}
        for key, value in self.raw.items():
            self._index.setdefault(self._normalize(key), value)

    @staticmethod
    def _normalize(key: str) -> str:
        return re.sub(r"[^a-z0-9]", "", str(key).lower())

    def get(self, *candidates: str, default: Any = None) -> Any:
        for candidate in candidates:
            value = self._index.get(self._normalize(candidate))
            if value is not None:
                return value
        return default

    def int(self, *candidates: str) -> int | None:
        return _as_int(self.get(*candidates))

    def str(self, *candidates: str) -> str | None:
        value = self.get(*candidates)
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def bool(self, *candidates: str) -> bool | None:
        return _as_bool(self.get(*candidates))

    def sub(self, *candidates: str) -> Bag:
        return Bag(self.get(*candidates))

    def items(self):
        return self.raw.items()


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool | None:
    """Interpret the several ways this data expresses a win.

    Seen in the wild: real booleans, 1/0, "Win"/"Fail", and "true"/"false".
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"win", "true", "1", "yes"}:
        return True
    if text in {"fail", "loss", "lose", "false", "0", "no"}:
        return False
    return None


def _sum_ints(*values: Any) -> int | None:
    numbers = [n for n in (_as_int(v) for v in values) if n is not None]
    return sum(numbers) if numbers else None


def _extract_augments(stats: Bag) -> list[Augment]:
    """Collect augment slots by pattern, not by a fixed slot count.

    Mayhem and Arena hand out different numbers of augments and Riot has changed
    the count between patches, so anything matching `playerAugment<n>` or
    `augment<n>` is picked up regardless of how many there turn out to be.
    """
    found: list[tuple[int, int]] = []
    for key, value in stats.items():
        match = _AUGMENT_KEY.match(Bag._normalize(key))
        if not match:
            continue
        augment_id = _as_int(value)
        if augment_id:  # 0 means "empty slot"
            found.append((int(match.group(1)), augment_id))
    return [Augment(slot=slot, augment_id=augment_id) for slot, augment_id in sorted(found)]


def _resolve_names(participant: Participant, static: StaticData) -> None:
    participant.champion_name = static.champion(participant.champion_id)
    for augment in participant.augments:
        augment.augment_name = static.augment(augment.augment_id)


def _read_stats(target: Participant, stats: Bag) -> None:
    """Map the stat block, which is shared in spirit between both payloads."""
    target.win = stats.bool("win")
    if target.win is None:
        # The end-of-game block records only the side that applies: a losing
        # player carries LOSE=1 and no WIN key at all.
        lost = stats.bool("lose")
        if lost is not None:
            target.win = not lost
    target.kills = stats.int("championsKilled", "kills")
    target.deaths = stats.int("numDeaths", "deaths")
    target.assists = stats.int("assists")
    target.cs = _sum_ints(
        stats.get("minionsKilled", "totalMinionsKilled"),
        stats.get("neutralMinionsKilled"),
    )
    target.gold_earned = stats.int("goldEarned")
    target.damage_to_champions = stats.int(
        "totalDamageDealtToChampions", "physicalDamageDealtToChampions"
    )
    target.damage_taken = stats.int("totalDamageTaken")
    target.vision_score = stats.int("visionScore")
    target.champ_level = stats.int("level", "champLevel")
    # Placement is 1-based where it applies. Summoner's Rift and ARAM games
    # carry a literal 0 here, meaning "not a placement mode" — storing that as a
    # zero would drag every average placement down.
    placement = stats.int(
        "subteamPlacement", "playerSubteamPlacement", "placement", "playerPlacement"
    )
    target.placement = placement or None
    target.items = [n for n in (stats.int(f"item{i}") for i in range(7)) if n is not None]
    target.spell1_id = stats.int("spell1Id", "summonerSpell1")
    target.spell2_id = stats.int("spell2Id", "summonerSpell2")
    target.augments = _extract_augments(stats)


def normalize_history_game(payload: Any, static: StaticData, platform_hint: str = "") -> Match:
    """Map a `/lol-match-history/v1/games/{id}` payload."""
    game = Bag(payload)
    match = Match(
        game_id=game.int("gameId") or 0,
        platform_id=game.str("platformId") or platform_hint,
        queue_id=game.int("queueId"),
        game_mode=game.str("gameMode"),
        game_type=game.str("gameType"),
        map_id=game.int("mapId"),
        game_creation_ms=game.int("gameCreation", "gameCreationDate"),
        game_duration_s=game.int("gameDuration", "gameLength"),
        game_version=game.str("gameVersion"),
        source="history",
    )
    # Only ever the real queue name. Falling back to the mode codename here
    # would write a non-null "KIWI" that then blocks "ARAM: Mayhem" from ever
    # filling in. Display code falls back to game_mode instead.
    match.queue_name = static.queue(match.queue_id)

    for team in game.get("teams", default=[]) or []:
        team_bag = Bag(team)
        if team_bag.bool("win", "isWinningTeam"):
            match.winning_team_id = team_bag.int("teamId")

    identities = _identity_index(game.get("participantIdentities", default=[]) or [])

    for index, raw in enumerate(game.get("participants", default=[]) or [], start=1):
        entry = Bag(raw)
        participant = Participant(
            participant_id=entry.int("participantId") or index,
            team_id=entry.int("teamId"),
            champion_id=entry.int("championId"),
        )
        _read_stats(participant, entry.sub("stats"))

        # Summoner spells hang off the participant in this payload, not off its
        # stats block, so read them here rather than in the shared stat mapper.
        participant.spell1_id = entry.int("spell1Id") or participant.spell1_id
        participant.spell2_id = entry.int("spell2Id") or participant.spell2_id

        timeline = entry.sub("timeline")
        participant.position = timeline.str("lane") or entry.str("teamPosition", "individualPosition")

        identity = identities.get(participant.participant_id, Bag(None))
        participant.puuid = identity.str("puuid")
        participant.riot_id_game_name = identity.str("gameName", "riotIdGameName")
        participant.riot_id_tagline = identity.str("tagLine", "riotIdTagline")
        participant.summoner_name = identity.str("summonerName")

        _resolve_names(participant, static)
        match.participants.append(participant)

    return match


def _identity_index(identities: list) -> dict[int, Bag]:
    index: dict[int, Bag] = {}
    for entry in identities:
        bag = Bag(entry)
        participant_id = bag.int("participantId")
        if participant_id is not None:
            index[participant_id] = bag.sub("player")
    return index


def _eog_start_time(game: Bag, duration: int | None) -> int | None:
    """Work out when the game started.

    The end-of-game block timestamps the *end*, not the start. Without this the
    match would land with a null creation time and sort to the bottom of every
    view — and the history sweep could not correct it, because it is the weaker
    source and will not overwrite this row.
    """
    creation = game.int("gameCreation", "gameCreationDate", "gameStartTime")
    if creation:
        return creation
    ended = game.int("endOfGameTimestamp")
    if ended:
        return ended - (duration or 0) * 1000
    return None


def normalize_eog(payload: Any, static: StaticData, platform_hint: str = "") -> Match:
    """Map an end-of-game stats block, where players hang off teams."""
    game = Bag(payload)
    duration = game.int("gameLength", "gameDuration")
    match = Match(
        game_id=game.int("gameId") or 0,
        # This payload omits platformId, but it is half the primary key. Without
        # the caller's hint the same game would be stored twice: once as
        # ("", id) from here and once as ("BR1", id) from the history sweep.
        platform_id=game.str("platformId") or platform_hint,
        # queueType here is the mode codename ("KIWI"), not a queue number, so
        # queue_id is usually absent. The history sweep fills it in afterwards.
        queue_id=game.int("queueId"),
        game_mode=game.str("gameMode", "queueType"),
        game_type=game.str("gameType"),
        map_id=game.int("mapId"),
        game_creation_ms=_eog_start_time(game, duration),
        game_duration_s=duration,
        game_version=game.str("gameVersion"),
        source="eog",
    )
    match.queue_name = static.queue(match.queue_id)

    next_participant_id = 1
    for raw_team in game.get("teams", default=[]) or []:
        team = Bag(raw_team)
        team_id = team.int("teamId")
        team_won = team.bool("isWinningTeam", "win")
        if team_won:
            match.winning_team_id = team_id

        for raw_player in team.get("players", default=[]) or []:
            player = Bag(raw_player)
            participant = Participant(
                participant_id=player.int("participantId") or next_participant_id,
                puuid=player.str("puuid"),
                riot_id_game_name=player.str("riotIdGameName", "gameName"),
                riot_id_tagline=player.str("riotIdTagLine", "tagLine"),
                summoner_name=player.str("summonerName"),
                team_id=player.int("teamId") or team_id,
                champion_id=player.int("championId", "skinId"),
                position=player.str("selectedPosition", "position", "teamPosition"),
            )
            next_participant_id = max(next_participant_id, participant.participant_id) + 1

            # Some builds nest the numbers under "stats", others inline them on
            # the player object. Reading the player as a fallback bag costs
            # nothing and covers both.
            stats = player.sub("stats")
            _read_stats(participant, stats if stats.raw else player)
            if participant.win is None and team_won is not None:
                participant.win = team_won

            # Items, spells, level and the champion name live on the player
            # object here, not in its stat block — the opposite of the history
            # payload, where items are ITEM0..ITEM6 inside stats.
            items = player.get("items")
            if isinstance(items, list):
                participant.items = [n for n in (_as_int(i) for i in items) if n is not None]
            participant.spell1_id = player.int("spell1Id") or participant.spell1_id
            participant.spell2_id = player.int("spell2Id") or participant.spell2_id
            participant.champ_level = player.int("level") or participant.champ_level

            _resolve_names(participant, static)
            # championName is given outright here; prefer it over our lookup so
            # a champion missing from the cached assets still gets a name.
            participant.champion_name = player.str("championName") or participant.champion_name
            match.participants.append(participant)

    return match


def apply_queue_hint(match: Match, session: Any, static: StaticData) -> bool:
    """Fill a capture's missing queue from the client's gameflow session.

    The end-of-game block carries no queue id — only the mode codename, so a
    Mayhem game arrives labelled "KIWI". The gameflow session knows the real
    queue (`gameData.queue.id`), and knows it immediately, whereas the history
    sweep cannot help until the client's match history catches up.

    Returns True if anything was filled. The session's own game id is checked
    first: if the client has already moved on to another lobby, its queue must
    not be pinned onto the game that just ended.
    """
    game_data = Bag(session).sub("gameData")
    if not game_data.raw:
        return False

    session_game_id = game_data.int("gameId")
    if session_game_id and match.game_id and session_game_id != match.game_id:
        log.debug(
            "gameflow session is for game %s, not %s; ignoring its queue",
            session_game_id,
            match.game_id,
        )
        return False

    queue = game_data.sub("queue")
    if not queue.raw:
        return False

    changed = False
    if match.queue_id is None and queue.int("id") is not None:
        match.queue_id = queue.int("id")
        changed = True
    if match.queue_name is None:
        match.queue_name = static.queue(match.queue_id) or queue.str("name", "shortName")
        changed = changed or match.queue_name is not None
    if match.map_id is None and queue.int("mapId") is not None:
        match.map_id = queue.int("mapId")
        changed = True
    return changed


def looks_like_eog(payload: Any) -> bool:
    """True when the payload groups players under teams, as the EOG block does."""
    game = Bag(payload)
    teams = game.get("teams", default=None)
    if isinstance(teams, list):
        for team in teams:
            if isinstance(team, dict) and isinstance(team.get("players"), list):
                return True
    return False


def normalize(
    payload: Any,
    static: StaticData,
    source: str | None = None,
    platform_hint: str = "",
) -> Match:
    """Normalize a payload, detecting its shape when the source is not given."""
    use_eog = source == "eog" if source else looks_like_eog(payload)
    if use_eog:
        return normalize_eog(payload, static, platform_hint)
    return normalize_history_game(payload, static, platform_hint)

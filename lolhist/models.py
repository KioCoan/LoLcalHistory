"""The shape everything is normalized into before it reaches the database.

Both capture paths (end-of-game and match history) produce these, so the store
and the dashboard never need to know which endpoint a row came from.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Capture sources, ranked. A lower-ranked source may fill a gap but must never
# overwrite a row captured by a better one: the end-of-game block carries detail
# (augments especially) that the match-history summary does not.
SOURCE_RANKS = {"history": 10, "eog": 20}


def rank_of(source: str) -> int:
    return SOURCE_RANKS.get(source, 0)


@dataclass
class Augment:
    slot: int
    augment_id: int | None = None
    augment_name: str | None = None


@dataclass
class Participant:
    participant_id: int
    puuid: str | None = None
    riot_id_game_name: str | None = None
    riot_id_tagline: str | None = None
    summoner_name: str | None = None
    team_id: int | None = None
    champion_id: int | None = None
    champion_name: str | None = None
    position: str | None = None
    win: bool | None = None
    # Modes like Arena and Mayhem are not binary win/loss; placement is the
    # meaningful result there and stays None elsewhere.
    placement: int | None = None
    kills: int | None = None
    deaths: int | None = None
    assists: int | None = None
    cs: int | None = None
    gold_earned: int | None = None
    damage_to_champions: int | None = None
    damage_taken: int | None = None
    vision_score: int | None = None
    champ_level: int | None = None
    items: list[int] = field(default_factory=list)
    spell1_id: int | None = None
    spell2_id: int | None = None
    augments: list[Augment] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        if self.riot_id_game_name:
            if self.riot_id_tagline:
                return f"{self.riot_id_game_name}#{self.riot_id_tagline}"
            return self.riot_id_game_name
        return self.summoner_name or (self.puuid or "unknown")[:8]


@dataclass
class Match:
    game_id: int
    platform_id: str = ""
    queue_id: int | None = None
    queue_name: str | None = None
    game_mode: str | None = None
    game_type: str | None = None
    map_id: int | None = None
    game_creation_ms: int | None = None
    game_duration_s: int | None = None
    game_version: str | None = None
    winning_team_id: int | None = None
    source: str = "history"
    raw_path: str | None = None
    participants: list[Participant] = field(default_factory=list)

    @property
    def rank(self) -> int:
        return rank_of(self.source)

    @property
    def key(self) -> tuple[int, str]:
        return (self.game_id, self.platform_id)

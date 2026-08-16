"""The match you are in right now.

Everything else in this package looks backwards: the watcher captures a game
once it has finished, and the dashboard reads what was stored. This module is
the one place that answers "what is happening on screen", and none of what it
produces is written down — close the app mid-game and nothing is lost, because
there was nothing to lose.

Two sources, joined:

* **The League Client** (the LCU, the same API the rest of the package uses)
  owns the roster. It is the only one that reports **puuids**, and a puuid is
  what every rank and mastery lookup is keyed by.
* **The in-game API** (port 2999, served by the game process itself, no auth)
  owns what changes while you play: runes, scores, items, the clock.

The join is on Riot ID, because the in-game feed reports no puuid at all. A
champion-name fallback covers a player whose name the two sources spell
differently.

Two things learned the hard way, both worth stating plainly:

1. **`gameData` goes stale.** It stays fully populated with the *previous*
   match while the client sits in a lobby, so its presence proves nothing.
   Only the gameflow phase says whether a game is being played.
2. **Discovery is expensive.** `connect()` can fall back to a PowerShell
   process lookup, and this module is polled every few seconds by a page that
   is usually open while League is closed. Failing to connect therefore starts
   a backoff rather than trying again on the next poll.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

import httpx

from . import icons, ranked, static_data
from .client import LcuClient, build_ssl_context, connect
from .connection import ClientUnavailable
from .static_data import StaticData

log = logging.getLogger(__name__)

GAMEFLOW_PHASE = "/lol-gameflow/v1/gameflow-phase"
GAMEFLOW_SESSION = "/lol-gameflow/v1/session"
SUMMONER_BY_PUUID = "/lol-summoner/v2/summoners/puuid/{puuid}"
CHAMPION_MASTERY = "/lol-champion-mastery/v1/{puuid}/champion-mastery"

# The one phase in which `gameData` describes the game being played rather than
# the one before it. See the note at the top of this file.
LIVE_PHASE = "InProgress"

# The in-game API. Fixed port, no authentication, and it exists only while a
# game is actually running — a refused connection is the ordinary state.
LIVE_BASE_URL = "https://127.0.0.1:2999"
ALL_GAME_DATA = "/liveclientdata/allgamedata"

# How long to leave the client alone after a failed connection attempt.
RECONNECT_BACKOFF_SECONDS = 30

# Which side each roster field is. 100 and 200 are the ids the client uses, and
# what `participants.team_id` already holds, so the page renders both the same.
TEAM_FIELDS = (("teamOne", 100), ("teamTwo", 200))

TOP_MASTERY = 3

# League Classic ships its own champion ids at this offset — Akali is 84 on
# Summoner's Rift and 60084 there. Mastery is only ever recorded against the
# base id, so a Classic game needs the offset removed before it can be looked up.
CLASSIC_CHAMPION_OFFSET = 60000


class LiveClient:
    """The in-game API.

    Deliberately quiet and deliberately impatient. The port is closed whenever
    a game is not running, which is most of the time, and this is called on a
    few-second poll — so a refusal returns None rather than raising, and the
    timeouts are short enough that a hung socket cannot stall the dashboard.
    """

    def __init__(self, base_url: str = LIVE_BASE_URL) -> None:
        self._http = httpx.Client(
            base_url=base_url,
            # The game serves the same self-signed certificate the client does.
            verify=build_ssl_context(),
            timeout=httpx.Timeout(1.5, connect=0.4),
            headers={"Accept": "application/json"},
        )

    def all_game_data(self) -> Any | None:
        """Everything the game will tell us, or None if no game is running."""
        try:
            response = self._http.get(ALL_GAME_DATA)
        except httpx.HTTPError:
            return None
        if response.status_code >= 400:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    def close(self) -> None:
        self._http.close()


def _idle(reason: str, phase: str | None = None) -> dict[str, Any]:
    """The "nothing to show" answer, in the shape the page expects."""
    return {"live": False, "reason": reason, "phase": phase, "players": []}


def base_champion_id(champion_id: int | None) -> int:
    """A Classic champion id mapped back to the champion it is a copy of."""
    if not champion_id:
        return 0
    return (
        champion_id - CLASSIC_CHAMPION_OFFSET
        if champion_id >= CLASSIC_CHAMPION_OFFSET
        else champion_id
    )


def _champion_name(static: StaticData, champion_id: int | None) -> str | None:
    """The champion's name, falling back to the Rift original for Classic ids."""
    return static.champion(champion_id) or static.champion(base_champion_id(champion_id))


def _riot_key(name: str | None, tagline: str | None) -> str | None:
    """The join key between the two APIs.

    Case-folded because they disagree: the client reports `br1` and the game
    reports `BR1` for the same player.
    """
    if not name or not tagline:
        return None
    return f"{name}#{tagline}".casefold()


def _rank_rows(player_ranks: dict, ladder: str | None) -> list[dict]:
    """Every ladder this player is ranked on.

    Built in the same shape the Matches tab's team list uses, so the page's
    existing rank rendering draws these without knowing they came from a live
    game. Ladders they are unranked on are left out rather than shown blank.
    """
    rows = []
    for tracked in ranked.TRACKED_QUEUES:
        rank = player_ranks.get(tracked)
        if rank is None or not rank.is_ranked:
            continue
        rows.append(
            {
                "queue_type": tracked,
                "tier": rank.tier,
                "division": rank.division,
                "league_points": rank.league_points,
                # Read from the client seconds ago, so it is current by
                # definition. The history tab's "this may be today's rank, not
                # the one they held" asterisk would be a lie here.
                "rank_at_match": 1,
                "queue_label": ranked.QUEUE_INITIALS.get(tracked, tracked[:1]),
                "queue_title": ranked.QUEUE_LABELS.get(tracked, tracked),
                "is_game_ladder": tracked == ladder,
            }
        )
    return rows


def _win_rate(player_ranks: dict, queue_type: str, complete: bool) -> dict | None:
    """This split's ranked record on the ladder this game is read against.

    `complete` is the whole difficulty here. The client reports `wins` for any
    player but reports `losses` **only for the account that is logged in** —
    everyone else comes back with a real win count and a flat zero. Believing
    that gives every stranger in the game a 100% win rate, which is worse than
    showing nothing, so a rate is only ever computed for your own account and
    everyone else carries their win count alone.

    None when there is nothing to say, so the column reads empty rather than
    claiming a record of 0.
    """
    rank = player_ranks.get(queue_type)
    if rank is None:
        return None
    wins = rank.wins or 0
    losses = rank.losses or 0
    record = {
        "queue_type": queue_type,
        "queue_title": ranked.QUEUE_LABELS.get(queue_type, queue_type),
        "wins": wins,
        "complete": complete,
    }

    if not complete:
        # Losses are not published for other players; do not imply otherwise.
        return {**record, "losses": None, "games": None, "rate": None} if wins else None

    games = wins + losses
    if not games:
        return None
    return {
        **record,
        "losses": losses,
        "games": games,
        "rate": round(100.0 * wins / games, 1),
    }


def _mastery_row(entry: dict, static: StaticData) -> dict:
    champion_id = entry.get("championId") or 0
    return {
        "champion_id": champion_id,
        "champion_name": _champion_name(static, champion_id),
        "level": entry.get("championLevel"),
        "points": entry.get("championPoints"),
    }


def _mastery(client: LcuClient, puuid: str, champion_id: int, static: StaticData) -> dict:
    """Mastery on the champion being played, plus their three best.

    The first says how practised this pick is; the second says what they
    normally play, which is often the more useful read on an unfamiliar name.
    """
    blank = {"mastery": None, "top_mastery": []}
    if not puuid:
        return blank
    entries = client.get_json_or_none(CHAMPION_MASTERY.format(puuid=puuid))
    if not isinstance(entries, list):
        return blank

    entries = [e for e in entries if isinstance(e, dict)]
    wanted = {champion_id, base_champion_id(champion_id)} - {0}
    played = next((e for e in entries if e.get("championId") in wanted), None)
    ranked_by_points = sorted(
        entries, key=lambda e: e.get("championPoints") or 0, reverse=True
    )
    return {
        "mastery": _mastery_row(played, static) if played else None,
        "top_mastery": [_mastery_row(e, static) for e in ranked_by_points[:TOP_MASTERY]],
    }


def _position(raw: dict) -> str | None:
    value = raw.get("selectedPosition") or raw.get("selectedRole") or ""
    return value.title() if isinstance(value, str) and value else None


def _player(
    client: LcuClient,
    raw: dict,
    team_id: int,
    selections: dict[str, dict],
    ranks: dict[str, dict],
    static: StaticData,
    queue_type: str,
    ladder: str | None,
    me: str,
) -> dict:
    puuid = raw.get("puuid") or ""
    selection = selections.get(puuid) or {}
    champion_id = raw.get("championId") or selection.get("championId") or 0

    # The roster reports `summonerName` as an empty string on a modern client,
    # so the Riot ID has to be looked up. It is also what the in-game feed is
    # joined on, which makes this lookup load-bearing rather than cosmetic.
    summoner = (
        client.get_json_or_none(SUMMONER_BY_PUUID.format(puuid=puuid)) if puuid else None
    ) or {}
    player_ranks = ranks.get(puuid) or {}

    player = {
        "puuid": puuid,
        "team_id": team_id,
        "name": summoner.get("gameName") or raw.get("summonerName") or "?",
        "tagline": summoner.get("tagLine") or None,
        "summoner_level": summoner.get("summonerLevel"),
        "profile_icon_id": summoner.get("profileIconId") or raw.get("profileIconId"),
        "position": _position(raw),
        "champion_id": champion_id,
        "champion_name": _champion_name(static, champion_id),
        "spell1_id": selection.get("spell1Id"),
        "spell2_id": selection.get("spell2Id"),
        "is_me": bool(puuid) and puuid == me,
        "ranks": _rank_rows(player_ranks, ladder),
        "win_rate": _win_rate(player_ranks, queue_type, complete=bool(puuid) and puuid == me),
        # Only the in-game feed can fill these; they stay absent when it is not
        # reachable, and the page leaves those columns empty rather than blank
        # out the rest of the row.
        "runes": None,
        "scores": None,
        # League Classic's old rune and mastery pages. Only ever filled for the
        # logged-in account — the loadout they come from is account-scoped.
        "classic_runes": None,
        "masteries": None,
    }
    player.update(_mastery(client, puuid, champion_id, static))
    # Named item0..item6, matching what the page's build cell already reads, so
    # a live build renders through exactly the same code as a stored one.
    player.update({f"item{slot}": 0 for slot in range(7)})
    return player


def roster(client: LcuClient, game: dict, static: StaticData, me: str = "") -> list[dict]:
    """The ten players, with everything that does not change during the game.

    Names, ranks and mastery are all fixed for the duration of a match, which is
    what makes caching this whole list per game safe — and necessary, since
    building it costs about thirty requests.
    """
    selections = {
        sel["puuid"]: sel
        for sel in (game.get("playerChampionSelections") or [])
        if isinstance(sel, dict) and sel.get("puuid")
    }

    entries: list[tuple[int, dict]] = [
        (team_id, raw)
        for field, team_id in TEAM_FIELDS
        for raw in (game.get(field) or [])
        if isinstance(raw, dict)
    ]

    queue = game.get("queue") or {}
    queue_id = queue.get("id")
    game_mode = queue.get("gameMode")
    # Which ladder to read everyone against, and which one this game can
    # actually move — for ARAM that is none, and nothing gets highlighted.
    queue_type = ranked.ranked_queue_for(queue_id, game_mode)
    ladder = ranked.affects_ladder(queue_id, game_mode)

    ranks = ranked.fetch_many(client, [raw.get("puuid") for _, raw in entries])

    return [
        _player(client, raw, team_id, selections, ranks, static, queue_type, ladder, me)
        for team_id, raw in entries
    ]


# -- League Classic's runes and masteries ----------------------------------
#
# Classic runs the *old* systems: a rune page of marks, seals, glyphs and
# quintessences, and a mastery page of thirty points. Neither has anything to do
# with modern perks, and the in-game API reports nothing for either — its
# `runes` field is null for every player in a Classic game, which reads as "this
# mode has no runes" and is simply wrong.
#
# They live on the account loadout instead. That is an account-scoped resource,
# so this can only ever answer for whoever is logged in; every attempt to read
# another player's loadout is refused. Same ceiling as modern runes, reached by
# a different road.

LOADOUTS = "/lol-loadouts/v4/loadouts/scope/account"


def is_classic(queue_id: int | None, game_mode: str | None) -> bool:
    """Is this League Classic, and therefore an old rune page?

    Asked of the mode itself rather than inferred from the absence of modern
    runes. That inference was wrong and shipped: ARAM: Mayhem reports no runes
    for anyone either, so a Mayhem game was shown wearing the Classic page.
    Several modes report nothing; only this one has an old page behind it.

    Built on the same constants `ranked` matches Classic games with, so a new
    Classic queue id only has to be added in one place.
    """
    return (
        queue_id in ranked.CLASSIC_QUEUE_IDS
        or (game_mode or "").upper() in ranked.CLASSIC_GAME_MODES
    )

# The four slot colours of an old rune page, with how many each page holds.
JADE_RUNE_SLOTS = (
    ("marks", "RED", 9),
    ("seals", "YELLOW", 9),
    ("glyphs", "BLUE", 9),
    ("quints", "QUINT", 3),
)
JADE_MASTERY_SLOTS = 30


def _loadout(client: LcuClient) -> dict:
    payload = client.get_json_or_none(LOADOUTS)
    if not isinstance(payload, list) or not payload:
        return {}
    loadout = (payload[0] or {}).get("loadout")
    return loadout if isinstance(loadout, dict) else {}


def _slot_id(loadout: dict, key: str) -> int:
    entry = loadout.get(key)
    return (entry or {}).get("itemId") or 0 if isinstance(entry, dict) else 0


def _tidy_page_name(raw: Any) -> str | None:
    """A page's own name, or a preset's translation key made readable.

    Custom pages carry `{"name": "ad"}`; the presets carry
    `{"nameKey": "jade_mastery_preset_ad_offense_defense"}` and no translation
    for it is served anywhere, so the key is tidied into something a person can
    read rather than shown raw or dropped.
    """
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    key = raw.get("nameKey")
    if not isinstance(key, str) or not key:
        return None
    words = key.replace("jade_mastery_preset_", "").replace("jade_rune_preset_", "")
    return " ".join(w.upper() if w in ("ad", "ap") else w.capitalize()
                    for w in words.split("_") if w) or None


def _group(ids: list[int], names: dict[int, str]) -> list[dict]:
    """Identical runes collapsed to one entry with a count.

    A page is nine of a kind far more often than not, so listing them
    individually would be nine copies of one icon.
    """
    counts: dict[int, int] = {}
    for rune_id in ids:
        counts[rune_id] = counts.get(rune_id, 0) + 1
    return [
        {"id": rune_id, "name": names.get(rune_id), "count": count}
        for rune_id, count in sorted(counts.items(), key=lambda kv: -kv[1])
    ]


def jade_loadout(client: LcuClient) -> dict | None:
    """The logged-in account's Classic rune and mastery pages.

    None when the client reports no loadout at all; the two halves are
    independent, so a readable rune page still comes back if the mastery page
    is empty.
    """
    loadout = _loadout(client)
    if not loadout:
        return None

    names = static_data.asset_map("jaderunes", "title")

    runes = None
    page = _slot_id(loadout, "ACTIVE_RUNE_PAGE")
    if page:
        slots = {}
        for label, colour, count in JADE_RUNE_SLOTS:
            ids = [
                _slot_id(loadout, f"RUNE_PAGE_{page}_{colour}_{i}")
                for i in range(1, count + 1)
            ]
            slots[label] = _group([i for i in ids if i], names)
        if any(slots.values()):
            runes = {"page_name": _tidy_page_name(
                (loadout.get(f"RUNE_PAGE_{page}_NAME") or {}).get("data")), **slots}

    masteries = None
    page = _slot_id(loadout, "ACTIVE_MASTERY_PAGE")
    if page:
        spent: dict[int, int] = {}
        for i in range(1, JADE_MASTERY_SLOTS + 1):
            mastery = _slot_id(loadout, f"MASTERY_PAGE_{page}_MASTERY_{i}")
            if mastery:
                spent[mastery] = spent.get(mastery, 0) + 1
        if spent:
            masteries = {
                "page_name": _tidy_page_name(
                    (loadout.get(f"MASTERY_PAGE_{page}_NAME") or {}).get("data")),
                "points": sum(spent.values()),
                # Ids only. The client ships no asset naming individual Classic
                # masteries — every plausible path 404s and the catalog returns
                # empty — so there is nothing to resolve them against.
                "spent": [{"id": k, "points": v}
                          for k, v in sorted(spent.items(), key=lambda kv: -kv[1])],
            }

    if runes is None and masteries is None:
        return None
    return {"runes": runes, "masteries": masteries}


def _runes(raw: Any) -> dict | None:
    """Keystone and both trees.

    All the in-game API exposes for anyone but you — the six runes and three
    shards of a full page are not available for other players from any source,
    so this is the ceiling rather than a first pass.
    """
    if not isinstance(raw, dict):
        return None
    keystone = raw.get("keystone") or {}
    primary = raw.get("primaryRuneTree") or {}
    secondary = raw.get("secondaryRuneTree") or {}
    if not (keystone.get("id") or primary.get("id")):
        return None
    return {
        "keystone_id": keystone.get("id"),
        "keystone_name": keystone.get("displayName"),
        "primary_style_id": primary.get("id"),
        "primary_style_name": primary.get("displayName"),
        "secondary_style_id": secondary.get("id"),
        "secondary_style_name": secondary.get("displayName"),
    }


def _scores(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    return {
        "kills": raw.get("kills") or 0,
        "deaths": raw.get("deaths") or 0,
        "assists": raw.get("assists") or 0,
        "cs": raw.get("creepScore") or 0,
        "vision": raw.get("wardScore") or 0,
    }


def _items(raw: Any) -> dict[str, int]:
    """The build, flattened into the item0..item6 the page already draws."""
    slots = {f"item{slot}": 0 for slot in range(7)}
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        slot = entry.get("slot")
        if isinstance(slot, int) and 0 <= slot <= 6:
            slots[f"item{slot}"] = entry.get("itemID") or 0
    return slots


def overlay(players: list[dict], payload: Any) -> bool:
    """Merge the in-game feed onto the roster. False if it had nothing to add.

    Matched on Riot ID, with champion as a fallback — no game has the same
    champion twice on both sides, so it is a safe second key, and it rescues a
    player whose name the two APIs disagree about.
    """
    if not isinstance(payload, dict):
        return False
    feed = payload.get("allPlayers")
    if not isinstance(feed, list) or not feed:
        return False

    by_riot: dict[str, dict] = {}
    by_champion: dict[str, dict] = {}
    for entry in feed:
        if not isinstance(entry, dict):
            continue
        key = _riot_key(entry.get("riotIdGameName"), entry.get("riotIdTagLine"))
        if key:
            by_riot.setdefault(key, entry)
        champion = (entry.get("championName") or "").casefold()
        if champion:
            by_champion.setdefault(champion, entry)

    matched = False
    for player in players:
        entry = by_riot.get(_riot_key(player.get("name"), player.get("tagline")) or "")
        if entry is None:
            entry = by_champion.get((player.get("champion_name") or "").casefold())
        if entry is None:
            continue
        matched = True
        player["runes"] = _runes(entry.get("runes"))
        player["scores"] = _scores(entry.get("scores"))
        player["level"] = entry.get("level")
        player["is_dead"] = bool(entry.get("isDead"))
        player["respawn_s"] = entry.get("respawnTimer") or 0
        player.update(_items(entry.get("items")))
    return matched


def _mirror_icons(client: LcuClient, players: list[dict]) -> None:
    """Copy any art this match needs that is not on disk yet.

    The ordinary sync works from what the database references, which can never
    cover a live game — its runes are not stored, and an enemy may be playing a
    champion never seen before. Cheap once warm: everything already mirrored is
    skipped without touching the client.
    """
    try:
        icons.ensure(client, "champion", [p.get("champion_id") for p in players])
        icons.ensure(
            client,
            "spell",
            [s for p in players for s in (p.get("spell1_id"), p.get("spell2_id"))],
        )
        # Builds change as the game goes on, and an item nobody in your history
        # has ever bought has never been mirrored — which is most of the enemy
        # team's build the first time you meet a mode.
        icons.ensure(
            client, "item", [p.get(f"item{slot}") for p in players for slot in range(7)]
        )
        runes = [p["runes"] for p in players if p.get("runes")]
        icons.ensure(client, "perk", [r.get("keystone_id") for r in runes])
        icons.ensure(
            client,
            "perkstyle",
            [s for r in runes for s in (r.get("primary_style_id"), r.get("secondary_style_id"))],
        )
        icons.ensure(
            client,
            "jaderune",
            [
                entry["id"]
                for p in players
                if p.get("classic_runes")
                for label, _colour, _n in JADE_RUNE_SLOTS
                for entry in p["classic_runes"].get(label) or []
            ],
        )
    except Exception:
        # Art is decoration. A live match must still render without it.
        log.debug("could not mirror live icons", exc_info=True)


class LiveSession:
    """A cached view of the current game, safe to poll.

    The dashboard's web server is threaded, so every entry point here is behind
    one lock. Two tiers of caching sit under it:

    * The roster — names, ranks, mastery — is built once per game. It is about
      thirty requests, and none of it changes while the game is played.
    * The in-game feed is one request and genuinely changes, so it is fetched
      every time.
    """

    def __init__(
        self,
        connect_fn: Callable[[], LcuClient] = connect,
        live_client: LiveClient | None = None,
    ) -> None:
        self._connect = connect_fn
        self._lock = threading.Lock()
        self._live = live_client if live_client is not None else LiveClient()
        self._client: LcuClient | None = None
        self._retry_at = 0.0
        self._static: StaticData | None = None
        self._roster: list[dict] | None = None
        self._roster_key: Any = None
        # Whose losses can be believed. See `_win_rate`.
        self._me = ""
        # The Classic rune and mastery pages, read once per match.
        self._jade: dict | None = None

    # -- connection ------------------------------------------------------

    def _lcu(self) -> LcuClient | None:
        if self._client is not None:
            return self._client
        if time.monotonic() < self._retry_at:
            return None
        try:
            self._client = self._connect()
        except ClientUnavailable:
            # Discovery can shell out to PowerShell, and this is polled every
            # few seconds by a page that is usually open with League closed.
            # Backing off is the difference between that and a process spawn
            # every poll.
            self._retry_at = time.monotonic() + RECONNECT_BACKOFF_SECONDS
            return None
        return self._client

    def _drop(self) -> None:
        """Forget the connection. The next attempt rediscovers it."""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
        self._client = None
        self._static = None
        self._me = ""
        self._retry_at = time.monotonic() + RECONNECT_BACKOFF_SECONDS
        self._forget_match()

    def _forget_match(self) -> None:
        self._roster = None
        self._roster_key = None
        self._jade = None

    def close(self) -> None:
        with self._lock:
            self._drop()
            self._retry_at = 0.0

    # -- snapshot --------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            try:
                return self._snapshot()
            except Exception:
                # A live view is a nicety. It must never take the dashboard
                # down with it, and a dropped connection is recoverable.
                log.debug("live snapshot failed", exc_info=True)
                self._drop()
                return _idle("Lost the connection to the League Client.")

    def _snapshot(self) -> dict[str, Any]:
        client = self._lcu()
        if client is None:
            return _idle("League is not running.")

        phase = client.get_json_or_none(GAMEFLOW_PHASE)
        if phase is None:
            # It answered when we connected and does not now.
            self._drop()
            return _idle("League is not running.")
        if phase != LIVE_PHASE:
            self._forget_match()
            return _idle(f"No game in progress — the client is in {phase}.", phase=phase)

        session = client.get_json_or_none(GAMEFLOW_SESSION) or {}
        game = session.get("gameData") or {}
        players = self._players(client, game)
        if not players:
            return _idle("A game is running but its roster is not readable yet.", phase=phase)

        payload = self._live.all_game_data()
        ingame = overlay(players, payload)

        queue = game.get("queue") or {}
        # Only Classic. Its runes never come through the in-game feed, so they
        # are read from the account loadout instead — and only for you, since
        # that resource is account-scoped. Asked of the mode rather than of
        # whether modern runes turned up: plenty of modes report none.
        if is_classic(queue.get("id"), queue.get("gameMode")):
            self._apply_classic_loadout(client, players)

        if ingame or any(p.get("classic_runes") for p in players):
            _mirror_icons(client, players)

        static = self._static or static_data.StaticData()
        return {
            "live": True,
            "phase": phase,
            "game_id": game.get("gameId"),
            "queue_id": queue.get("id"),
            "queue_name": (
                queue.get("name")
                or queue.get("description")
                or static.queue(queue.get("id"))
                or queue.get("gameMode")
            ),
            "game_mode": queue.get("gameMode"),
            "is_ranked": bool(queue.get("isRanked")),
            "game_time_s": int((payload or {}).get("gameData", {}).get("gameTime") or 0),
            # False means the game process did not answer on port 2999. Ranks,
            # mastery and champions are all still there — runes and live scores
            # are the only things missing, and the page says so.
            "ingame": ingame,
            "players": players,
        }

    def _apply_classic_loadout(self, client: LcuClient, players: list[dict]) -> None:
        """Attach the Classic rune and mastery pages to your own row.

        Cached for the match like the rest of the roster: a page cannot be
        changed once the game has started.
        """
        if self._jade is None:
            try:
                self._jade = jade_loadout(client) or {}
            except Exception:
                log.debug("could not read the Classic loadout", exc_info=True)
                self._jade = {}
        if not self._jade:
            return
        for player in players:
            if player.get("is_me"):
                player["classic_runes"] = self._jade.get("runes")
                player["masteries"] = self._jade.get("masteries")

    def _identify(self, client: LcuClient) -> str:
        """Who is logged in. The one account whose losses the client reports.

        Cached for the connection: it can only change by logging out, which
        drops the connection with it.
        """
        if not self._me:
            summoner = client.get_json_or_none("/lol-summoner/v1/current-summoner") or {}
            self._me = summoner.get("puuid") or ""
        return self._me

    def _players(self, client: LcuClient, game: dict) -> list[dict]:
        """The roster, rebuilt only when the game changes."""
        key = game.get("gameId") or tuple(
            sorted(
                raw.get("puuid") or ""
                for field, _ in TEAM_FIELDS
                for raw in (game.get(field) or [])
                if isinstance(raw, dict)
            )
        )
        if not key:
            return []

        if self._roster is None or self._roster_key != key:
            if self._static is None:
                self._static = static_data.load(client)
            self._roster = roster(client, game, self._static, self._identify(client))
            self._roster_key = key

        # Copied so the per-poll overlay cannot accumulate on the cached list.
        return [dict(player) for player in self._roster]


_session = LiveSession()


def snapshot() -> dict[str, Any]:
    """The current game, or why there isn't one."""
    return _session.snapshot()


def close() -> None:
    _session.close()

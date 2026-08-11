"""The end-of-game watcher.

Holds the client's WebSocket open and captures the post-game stats block the
moment a match finishes. This is the path that gets full detail — augments
included — and the only one that can see games the match-history endpoint never
lists, such as customs.

Two deliberate choices:

* It subscribes to the whole event firehose and filters by URI prefix, instead
  of subscribing to one exact endpoint. The LCU is unsupported and endpoints get
  renamed; a prefix filter survives that, and unrecognised URIs under
  `/lol-end-of-game/` are logged so a rename is visible rather than silent.
* After each game it also runs a short match-history sweep. If the stats block
  was missed — client closed too fast, endpoint moved — the game is still
  recorded, just with less detail.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from typing import Any

import websockets
from websockets.asyncio.client import connect as ws_connect

from . import backfill, health, icons, ranked, static_data, store
from .client import LcuClient, build_ssl_context, connect
from .connection import ClientUnavailable
from .normalize import apply_queue_hint, normalize
from .static_data import StaticData

log = logging.getLogger(__name__)

SUBSCRIBE_FRAME = json.dumps([5, "OnJsonApiEvent"])
EVENT_OPCODE = 8

GAMEFLOW_URI = "/lol-gameflow/v1/gameflow-phase"
GAMEFLOW_SESSION = "/lol-gameflow/v1/session"
EOG_PREFIX = "/lol-end-of-game/"

# Phases meaning "the match is over and stats exist".
END_PHASES = {"PreEndOfGame", "EndOfGame", "WaitingForStats"}
# Phases meaning "back at the client", used to trigger the follow-up sweep.
IDLE_PHASES = {"None", "Lobby", "Matchmaking"}

EOG_ENDPOINTS = (
    "/lol-end-of-game/v1/eog-stats-block",
    "/lol-end-of-game/v1/champion-mastery-updates",
)

RECONNECT_MIN_SECONDS = 2
RECONNECT_MAX_SECONDS = 30

# How long to let the client rebuild its match-history cache before sweeping it.
SWEEP_DELAY_SECONDS = 30


class Watcher:
    def __init__(self, conn: sqlite3.Connection, sweep_after_game: bool = True) -> None:
        self.conn = conn
        self.sweep_after_game = sweep_after_game
        self.captured: set[int] = set()
        self.unknown_uris: set[str] = set()
        self._game_seen = False
        # The client fires a burst of end-of-game events per match. Without
        # this, each one would start its own retry loop and they would stack up.
        self._capturing = False
        # Set once a game has been captured, cleared when the client leaves the
        # post-game screen. Without it the repeated end-of-game events fire
        # fresh capture attempts for a game already stored, and each one that
        # finds the stats block gone gets logged as a miss.
        self._captured_this_cycle = False
        self._last_heartbeat = 0.0
        self._platform_id = ""
        # Which account is logged in right now. Everything rank-related is
        # scoped to it: a second account has its own ladders, and measuring one
        # against the other invents LP changes that never happened.
        self._me_puuid = ""
        # That account's ladders as last observed, so the next game's LP change
        # can be measured against them.
        self._my_ranks: dict = {}
        self._last_captured: tuple | None = None
        # Accounts whose client-side history has been imported this run. A set
        # rather than a flag so switching accounts mid-run imports the new one
        # instead of leaving its history missing until the app restarts.
        self._synced: set[str] = set()

    async def run_forever(self) -> None:
        delay = RECONNECT_MIN_SECONDS
        while True:
            try:
                await self._session()
                delay = RECONNECT_MIN_SECONDS
            except ClientUnavailable as exc:
                log.info("waiting for the League Client: %s", exc)
            except (OSError, websockets.WebSocketException) as exc:
                log.info("connection lost (%s); reconnecting", exc)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("unexpected watcher error; reconnecting")

            log.debug("reconnecting in %ss", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX_SECONDS)

    async def _session(self) -> None:
        """One connected session. Returns when the socket drops.

        Credentials are rediscovered on every call because the client picks a
        new port and token each time it launches.
        """
        client = await asyncio.to_thread(connect)
        try:
            static = await asyncio.to_thread(static_data.load, client)
            summoner = await asyncio.to_thread(client.current_summoner)
            self._remember_me(summoner)
            self._platform_id = await asyncio.to_thread(_resolve_platform_id, client)
            await asyncio.to_thread(self._prime_my_ranks, client)
            await asyncio.to_thread(health.record_start, _riot_id(summoner))
            log.info(
                "watching as %s (port %s) - %s",
                _riot_id(summoner),
                client.credentials.port,
                static.summary(),
            )
            for line in health.describe():
                log.info("  %s", line)

            # Falls back to a sentinel so a client that reports no puuid still
            # gets its history imported once, as it did before.
            sync_key = self._me_puuid or "?"
            if sync_key not in self._synced:
                self._synced.add(sync_key)
                await asyncio.to_thread(self._initial_sync, client, static)

            async with ws_connect(
                client.credentials.websocket_url,
                additional_headers={"Authorization": _basic_auth(client.credentials.password)},
                ssl=build_ssl_context(),
                ping_interval=30,
                max_size=None,
            ) as socket:
                await socket.send(SUBSCRIBE_FRAME)
                log.info("subscribed; waiting for games to finish")
                async for raw in socket:
                    await self._on_frame(raw, client, static)
        finally:
            client.close()

    def _remember_me(self, summoner: dict[str, Any]) -> None:
        puuid = summoner.get("puuid")
        if puuid:
            self._me_puuid = puuid
            store.set_me(
                self.conn,
                puuid,
                summoner.get("gameName") or summoner.get("displayName"),
                summoner.get("tagLine"),
                summoner.get("profileIconId"),
            )

    async def _on_frame(self, raw: Any, client: LcuClient, static: StaticData) -> None:
        # Cheap liveness marker, so `doctor` can distinguish "watcher is up and
        # idle" from "watcher died hours ago".
        now = time.monotonic()
        if now - self._last_heartbeat > 60:
            self._last_heartbeat = now
            await asyncio.to_thread(health.record_heartbeat)

        event = _parse_event(raw)
        if event is None:
            return
        uri = event.get("uri") or ""

        if uri == GAMEFLOW_URI:
            phase = event.get("data")
            log.debug("gameflow phase: %s", phase)
            if phase in END_PHASES:
                self._game_seen = True
                await self._capture(client, static)
            else:
                # Any other phase means the post-game screen is behind us, so
                # the next end-of-game belongs to a different game.
                self._captured_this_cycle = False
                if phase in IDLE_PHASES and self._game_seen:
                    self._game_seen = False
                    await self._after_game(client, static)
            return

        if uri.startswith(EOG_PREFIX):
            if uri not in EOG_ENDPOINTS and uri not in self.unknown_uris:
                # Not an error — just an endpoint we do not map yet. Worth
                # surfacing, because a renamed stats block would show up here.
                self.unknown_uris.add(uri)
                log.info("saw an unmapped end-of-game endpoint: %s", uri)
                await asyncio.to_thread(health.record_unmapped_uri, uri)
            await self._capture(client, static)

    async def _capture(self, client: LcuClient, static: StaticData) -> None:
        """Fetch and store the end-of-game block, retrying while it settles.

        The stats block does not appear the instant the phase changes, so this
        retries with a widening gap rather than giving up on the first miss.
        """
        if self._capturing or self._captured_this_cycle:
            return
        self._capturing = True
        try:
            for attempt in range(5):
                payload = await self._fetch_eog(client)
                if payload is not None:
                    # Read the session now, while the finished game is still the
                    # current one. It is the only place the queue id is
                    # available at capture time.
                    session = await asyncio.to_thread(
                        client.get_json_or_none, GAMEFLOW_SESSION
                    )
                    try:
                        stored = await asyncio.to_thread(
                            self._store, payload, static, session, client
                        )
                    except Exception as exc:
                        # A storage fault must not reach the reconnect loop.
                        # Tearing down the socket here is how this failure
                        # disguised itself as an ordinary reconnect before.
                        self._report_failure(exc, payload)
                        return
                    if stored:
                        return
                await asyncio.sleep(1.5 * (attempt + 1))

            # No payload ever appeared. Not necessarily wrong — the sweep will
            # still catch the game from match history.
            await asyncio.to_thread(health.record_missed)
            log.warning(
                "a game ended but no end-of-game stats appeared; "
                "relying on the history sweep for it"
            )
        finally:
            self._capturing = False

    def _report_failure(self, exc: Exception, payload: Any) -> None:
        """Make a failed capture impossible to miss."""
        game_id = payload.get("gameId") if isinstance(payload, dict) else None
        streak = health.record_failure(type(exc).__name__, str(exc))
        log.error(
            "FAILED TO RECORD game %s: %s: %s", game_id, type(exc).__name__, exc, exc_info=True
        )
        log.error(
            "*** %s capture(s) have now failed in a row - games are NOT being recorded. ***",
            streak,
        )
        log.error("*** Run `lolhist doctor` for details. ***")

    async def _fetch_eog(self, client: LcuClient) -> Any | None:
        for endpoint in EOG_ENDPOINTS:
            payload = await asyncio.to_thread(client.get_json_or_none, endpoint)
            if isinstance(payload, dict) and payload.get("gameId"):
                return payload
        return None

    def _prime_my_ranks(self, client: LcuClient) -> None:
        """Establish a baseline for measuring LP changes against.

        Prefers the last observation on record over a fresh read: if the
        watcher was restarted between games, the stored value is the one from
        before the game, and reading now would show a difference of zero.

        Reading it back scoped to this account is what makes a switch safe —
        the baseline comes from the same ladder that will be measured against.
        """
        if not self._me_puuid:
            log.debug("no puuid for the current account; skipping rank tracking")
            self._my_ranks = {}
            return

        self._my_ranks = store.latest_rank_progress(self.conn, self._me_puuid)
        if not self._my_ranks:
            self._my_ranks = ranked.fetch_mine(client)
            store.save_rank_progress(self.conn, self._my_ranks, self._me_puuid)

        # A game may have finished after the app was last closed. Its "before"
        # rank is on record, so the change it caused can still be worked out.
        try:
            self._settle_lp(client)
        except Exception:
            log.debug("could not settle a pending LP change", exc_info=True)

        for rank in self._my_ranks.values():
            if rank.is_ranked:
                log.info("  %s: %s", rank.queue_type, rank.label())

    def _store(
        self,
        payload: Any,
        static: StaticData,
        session: Any = None,
        client: LcuClient | None = None,
    ) -> bool:
        match = normalize(payload, static, source="eog", platform_hint=self._platform_id)
        if session is not None:
            apply_queue_hint(match, session, static)
        if not match.game_id:
            return False
        if match.game_id in self.captured:
            return True  # the client emits several events per game end
        if not match.participants:
            log.debug("end-of-game payload for %s had no players; waiting", match.game_id)
            return False

        match.raw_path = store.archive_raw(payload, match.game_id, match.platform_id, "eog")
        outcome = store.upsert_match(self.conn, match)
        self.captured.add(match.game_id)
        self._captured_this_cycle = True
        health.record_capture(match.game_id, "eog", match.queue_name or match.game_mode)
        self._last_captured = match.key
        if client is not None:
            self._capture_ranks(client, match)
            # LP is often already updated by the time the stats block exists;
            # try now, and the delayed pass will catch it if it is not.
            try:
                self._settle_lp(client)
            except Exception:
                log.debug("LP not settled at capture; will retry", exc_info=True)
        log.info(
            "captured %s (%s, %s players, %s) [%s]",
            match.game_id,
            match.queue_name or match.game_mode or "unknown mode",
            len(match.participants),
            _augment_note(match),
            outcome,
        )
        return True

    # Enough to cover a few recent games without a long wait on first launch.
    INITIAL_RANK_LOOKUPS = 60

    def _initial_sync(self, client: LcuClient, static: StaticData) -> None:
        """Fill the database once at startup.

        Without this a fresh install shows an empty dashboard until the first
        game finishes, which reads as broken. The client remembers roughly the
        last twenty games, so there is something to show immediately.
        """
        try:
            counts = backfill.run(client, self.conn, static, max_games=200)
            if counts["inserted"]:
                log.info("imported %s game(s) from the client's history", counts["inserted"])

            pending = store.players_needing_ranks(self.conn, limit=self.INITIAL_RANK_LOOKUPS)
            if pending:
                ranks = ranked.fetch_many(client, pending)
                store.save_player_ranks(self.conn, ranks)
                log.info("looked up ranks for %s player(s)", len(ranks))

            store.derive_lp_from_snapshots(self.conn, self._me_puuid)
        except Exception:
            log.warning("initial sync did not finish", exc_info=True)

        # Last, and on its own: the icons are decoration, so a slow or failing
        # mirror must not hold up or break anything above it.
        try:
            icons.sync(client, self.conn)
        except Exception:
            log.debug("icon mirror did not finish", exc_info=True)

    def _capture_ranks(self, client: LcuClient, match) -> None:
        """Pin every player's current rank to this game.

        Done at capture time on purpose: the client only ever reports rank as
        it is now, so this is the single moment where "what were they ranked
        during this game" is answerable.
        """
        try:
            ladder = ranked.affects_ladder(match.queue_id, match.game_mode)
            before = self._my_ranks.get(ladder) if ladder else None
            if before is not None and before.is_ranked:
                # Recorded before anything else can fail, and before waiting on
                # the new LP to appear.
                store.record_rank_before(self.conn, match.key, ladder, before)

            puuids = [p.puuid for p in match.participants if p.puuid]
            ranks = ranked.fetch_many(client, puuids)
            if not ranks:
                return
            store.save_player_ranks(self.conn, ranks)
            store.save_participant_ranks(self.conn, match, ranks)
            log.debug("stored ranks for %d of %d players", len(ranks), len(puuids))
        except Exception:
            # Ranks are a nice-to-have; never let them cost us the match.
            log.warning("could not capture ranks for game %s", match.game_id, exc_info=True)

    def _settle_lp(self, client: LcuClient) -> None:
        """Complete the LP change for the last ranked game, if one is waiting.

        Driven by what is stored rather than by what happened this session, so
        it works on a later launch just as well as immediately after the game.
        Called at capture, again after the game settles, and at startup.
        """
        if not self._me_puuid:
            return

        current = ranked.fetch_mine(client)
        if not current:
            return

        row = store.pending_lp_match(self.conn, self._me_puuid)
        if row is not None:
            queue_type = row["my_rank_queue"]
            before = ranked.Rank(
                queue_type=queue_type,
                tier=row["my_tier_before"],
                division=row["my_division_before"],
                league_points=row["my_lp_before"],
            )
            after = current.get(queue_type)
            delta = ranked.diff_points(before, after)
            if delta is not None:
                store.record_lp_change(
                    self.conn, (row["game_id"], row["platform_id"]), queue_type, delta, after
                )
                log.info(
                    "game %s: %+d LP on %s -> %s",
                    row["game_id"], delta, queue_type, after.label() or "unranked",
                )

        store.save_rank_progress(self.conn, current, self._me_puuid)
        self._my_ranks = current

    async def _after_game(self, client: LcuClient, static: StaticData) -> None:
        """Follow-up work once the client has settled after a game.

        Deliberately delayed. The client rebuilds its history cache after a game
        and briefly reports only a handful of entries, and LP takes a moment to
        update, so doing either immediately finds nothing.
        """
        await asyncio.sleep(SWEEP_DELAY_SECONDS)
        await asyncio.to_thread(self._settle_lp, client)

        # The game just played almost certainly brought in a champion or an item
        # not mirrored yet, and this is the last moment the client is reliably
        # open to ask.
        try:
            await asyncio.to_thread(icons.sync, client, self.conn)
        except Exception:
            log.debug("icon mirror did not finish", exc_info=True)

        if not self.sweep_after_game:
            return
        counts = await asyncio.to_thread(backfill.run, client, self.conn, static, 20)
        if counts["inserted"] or counts["upgraded"] or counts.get("enriched"):
            log.info(
                "history sweep: %s added, %s updated, %s filled in",
                counts["inserted"],
                counts["upgraded"],
                counts.get("enriched", 0),
            )


def _augment_note(match) -> str:
    total = sum(len(p.augments) for p in match.participants)
    return f"{total} augments" if total else "no augments"


def _resolve_platform_id(client: LcuClient) -> str:
    """Find this account's platform (e.g. "BR1").

    It is half the match primary key, and the end-of-game payload does not
    carry it — so without this a captured game and its history counterpart
    become two separate rows for the same match. There is no dedicated endpoint
    for it, so it is read from the most recent history entry.
    """
    payload = client.get_json_or_none(backfill.MATCH_LIST, begIndex=0, endIndex=1)
    for game in backfill._games_from_list(payload):
        platform = game.get("platformId")
        if platform:
            return str(platform)
    log.warning("could not determine platform id; captures may not merge with history")
    return ""


def _riot_id(summoner: dict[str, Any]) -> str:
    name = summoner.get("gameName") or summoner.get("displayName") or "?"
    tag = summoner.get("tagLine")
    return f"{name}#{tag}" if tag else name


def _basic_auth(password: str) -> str:
    import base64

    token = base64.b64encode(f"riot:{password}".encode()).decode()
    return f"Basic {token}"


def _parse_event(raw: Any) -> dict[str, Any] | None:
    """Unwrap an `[8, "OnJsonApiEvent", {...}]` frame."""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        message = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(message, list) or len(message) < 3:
        return None
    if message[0] != EVENT_OPCODE:
        return None
    return message[2] if isinstance(message[2], dict) else None


def watch(conn: sqlite3.Connection, sweep_after_game: bool = True) -> None:
    """Blocking entry point for the CLI."""
    watcher = Watcher(conn, sweep_after_game=sweep_after_game)
    try:
        asyncio.run(watcher.run_forever())
    except KeyboardInterrupt:
        log.info("stopped")

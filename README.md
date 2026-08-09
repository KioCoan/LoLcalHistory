# lol-local-history

A personal, local record of your League of Legends matches — champions, augments,
performance over time, and who you queue with.

Built on the **League Client (LCU) API**: the local HTTP + WebSocket server the game
client runs on `127.0.0.1`. It authenticates from a lockfile on disk, needs no API key,
and never expires.

## Why not the public Riot API

For the modes this was built for, the public API is closed, not merely inconvenient:

| Mode | queueId | Public Riot API | This tool |
|---|---|---|---|
| ARAM: Mayhem | 2400 | **403 Forbidden** ([dev-rel #1109](https://github.com/RiotGames/developer-relations/issues/1109)) | works |
| League Classic | 4310 / 4320 | [withheld deliberately](https://x.com/RiotGamesDevRel/status/2082130947223265497) | works |
| ARAM, Summoner's Rift | 450, 400/420/430 | works | works |

## Privacy

This tool is single-player and local by construction. It makes **no outbound network
calls at all** — champion, queue and augment names are read from the client's own game
data, not from Data Dragon or CommunityDragon. Nothing is uploaded, synced, published or
aggregated anywhere. `data/` is gitignored.

That matters beyond preference: Riot asked that League Classic data not be aggregated or
displayed publicly. A private record of your own games, that never leaves your machine,
is a different thing — and it stays that way only if the tool never grows an export.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

## Use

Check that the client is reachable and see your account:

```bash
.venv/Scripts/python.exe -m lolhist doctor
```

Import the games the client still remembers (about the last 20):

```bash
.venv/Scripts/python.exe -m lolhist backfill
```

Capture games as they finish — richer data, and the only path that sees custom games.
Leave it running while you play:

```bash
.venv/Scripts/python.exe -m lolhist watch
```

Browse everything:

```bash
.venv/Scripts/python.exe -m lolhist serve
```

Then open <http://127.0.0.1:8787>.

Fetch ranks for everyone you have played with:

```bash
.venv/Scripts/python.exe -m lolhist ranks
```

Other commands: `lolhist stats` for a quick terminal summary, `lolhist probe` to dump raw
client payloads to `data/samples/` when something needs diagnosing, and
`lolhist resolve-names` to rebuild champion, queue and augment names from the stored ids
— useful after a patch adds augments your cached assets did not know about.

## Ranks

Two separate ladders are tracked, because League Classic has its own:

| Ladder | Client queue type | Notes |
|---|---|---|
| Solo/Duo | `RANKED_SOLO_5x5` | shown for ARAM, Mayhem and Summoner's Rift |
| League Classic | `JADE_RANKED_SOLO_5x5` | its own tiers, including Salt and Wood |

The dashboard picks the ladder from the match's mode, so a Classic game shows Classic
rank and everything else shows Solo/Duo. Ranks are read for every player in the game, not
just you.

**A rank marked `*` is the player's rank *now*, not what they held during that game.** The
client only ever reports current rank, so games imported from history can be shown no
other way. Games captured live by `watch` pin everyone's rank at the time, and those
appear without the asterisk.

**LP change** is recorded per match by comparing your ladders before and after. Promotion
and demotion are handled — a promotion resets you to low LP in the new tier, which naive
subtraction would report as a large loss. It only appears for games on a ranked ladder,
so Classic games show it and ARAM/Mayhem do not.

## Knowing whether it is actually working

A background watcher whose success is silent and whose failure is also silent is the
easiest thing in the world to trust wrongly. This one had exactly that bug during
development: every capture threw, it caught the error, reconnected, and went on printing
"waiting for games to finish" while recording nothing for a whole session.

So its state is persisted to `data/health.json` and surfaced in two places:

- **`lolhist doctor`** prints capture counts, the last game caught, and any error — and
  **exits non-zero when the watcher is failing**, so it can be used as an alarm rather
  than something you have to read.
- **The dashboard** shows a red banner when captures are failing, naming the error, plus
  a always-visible one-line status under the summary cards.

A game that ends with no stats block available is reported separately as *missed*, not
failed, and does not raise the alarm — the history sweep still picks it up, and a warning
that cries wolf is a warning you learn to ignore.

## How it works

```
League Client ──lockfile──> connection ──┬── WebSocket ──> watcher  ──┐
                                         └── REST      ──> backfill ──┤
                                                                       v
                                         data/raw/*.json.gz  <──── normalize ──> SQLite
                                                                                    │
                                                              Flask dashboard <─────┘
```

**Two capture paths.** The watcher listens for end-of-game and grabs the full stats
block; backfill sweeps the client's match history for anything played while the watcher
was off. Rows are ranked by source, so a backfill sweep can never overwrite the richer
data a watcher capture already stored.

**The two payloads know different things**, and they disagree about almost everything
else. The end-of-game block uses `SCREAMING_SNAKE` stat names, hangs players off teams,
keeps items and spells outside `stats`, timestamps the game's *end* rather than its
start, and carries neither a queue id nor a platform. The history payload does the
opposite on every count. So a capture never blindly overwrites a stored row: a better
source replaces the participants, but match metadata is merged field by field, and a
weaker source is still allowed to fill in blanks. That is why a Mayhem game captured live
still ends up labelled "ARAM: Mayhem" rather than "KIWI".

**Raw payloads are archived.** Every capture is gzipped to `data/raw/` before it is
parsed. The LCU is unsupported and its payload shapes drift between patches; keeping the
raw JSON means a shape change costs a re-parse of files you already have, never lost
history.

**IDs are the source of truth.** Names are a display convenience resolved from the
client's assets. An augment the client doesn't recognise still gets stored, as
`Augment 1421`.

## Notes on the data

- `participant_augments` is a separate table because the augment count varies by mode
  and patch — a wide `augment1..6` table would need a migration each time Riot adds a slot.
- League Classic uses offset champion IDs (Twitch is `60029`, not `29`). The client's own
  `champion-summary.json` maps them; Data Dragon does not. Another reason for reading
  static data locally.
- `placement` is only set for placement modes. A literal `0` in the payload means "not
  applicable" and is stored as NULL.
- `puuid` is the stable player identity. Riot IDs change, so teammate tracking keys off
  the puuid and treats the name as display only.

## Caveats

The LCU is unofficial. Riot permits read-only use but can rename or remove endpoints
without notice. If a capture stops working, run `lolhist probe` — the watcher also logs
any unrecognised `/lol-end-of-game/` URI it sees, which is usually the first sign of a
rename.

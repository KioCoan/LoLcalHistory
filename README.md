# LoLcal History

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

**Nothing about your games ever leaves this machine.** No upload, no sync, no telemetry,
no export. Champion, queue, item and augment names — and every icon on the dashboard —
come from the game client's own files, not from Data Dragon, CommunityDragon or any CDN.
`data/` is gitignored.

That matters beyond preference: Riot asked that League Classic data not be aggregated or
displayed publicly. A private record of your own games, that never leaves your machine,
is a different thing — and it stays that way only if the tool never grows an export.

**The one request that goes out** is the update check: a `GET` to this repository's
public releases API to compare version numbers. It carries no query, no body and no
identifier, so all GitHub can observe is that some machine asked what the latest version
is. Turn it off completely with `LOLHIST_NO_UPDATE_CHECK=1`.

## Installing

Grab the latest **Setup.exe** from the [releases page](https://github.com/KioCoan/LoLcalHistory/releases)
and run it. It installs for the current user only — no administrator prompt — and adds a
Start Menu entry. Windows SmartScreen will warn that the publisher is unknown, because
the installer is not code-signed; choose **More info → Run anyway**.

After that the app updates itself: when a newer release exists, a banner appears with an
**Update now** button that downloads the installer, checks it against the published
SHA-256, runs it silently and reopens the app. Your history is never touched by an
upgrade or an uninstall.

### Cutting a release

The version in `lolhist/version.py` is the source of truth, and the workflow fails the
build if a tag disagrees with it. `pyproject.toml` has to match too, so bump both with:

```bash
.venv/Scripts/python.exe tools/bump_version.py 0.2.0
```

It prints the commit, tag and push commands to run next.

`.github/workflows/release.yml` runs the tests, builds the executable, compiles the Inno
Setup installer and publishes a release with the installer, a portable exe and
`SHA256SUMS.txt`. Use the workflow's **Run workflow** button to rehearse a build without
publishing anything.

## The app

The normal way to use this is the desktop app: one executable that tracks your games
*and* shows them, with no terminal and no browser.

```powershell
.\build-app.ps1
```

That produces `dist\LoLcal History.exe` (~26 MB, single file). Double-click it and it
opens a native window with the dashboard, while the watcher records games in the
background.

**Closing the window does not stop tracking.** It hides to the system tray, because you
will close it while playing and quitting would lose exactly the games you were about to
record. Right-click the tray icon to reopen it, or to quit for real.

The window uses the OS webview (Edge WebView2, already present on Windows 11), so no
browser engine is bundled and nothing is downloaded at runtime.

**Refreshing.** The view updates itself every 30 seconds, and there is a **Refresh**
button (also F5) for when you have just finished a game and do not want to wait. A native
window has no address bar to reload from, which is why both exist and why a failed
refresh says so on screen instead of leaving stale numbers up.

A background refresh keeps your place: open match rows stay open, your scroll position,
sort order and filter selections survive, and the table is never blanked mid-read. Rows
are keyed by game id rather than position, so a newly finished game arriving at the top
cannot shift an expanded row onto a different match. Refreshes pause while the window is
hidden in the tray.

Your history lives in `%LOCALAPPDATA%\LoLcal History` — the app and the CLI share it.
A frozen build unpacks to a temporary folder that is deleted on exit, so the database
cannot live beside the executable. Errors go to `app.log` in that same folder, since a
windowed app has nowhere to print.

If you used an earlier build, the old `lol-local-history` folder is moved across on first
run. That is a single atomic rename, and if it cannot happen — the app is already open,
or permissions refuse — the old folder simply keeps being used, rather than a fresh empty
history appearing beside it.

Upgrading from a source checkout that kept its data in `data/`:

```bash
.venv/Scripts/python.exe -m lolhist migrate-data
```

## Setup (for the CLI)

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

## Sharing it with someone else

`dist\LoLcal History.exe` is self-contained and account-agnostic. Nothing about your
account, region or install path is baked into it — the League install is looked up from
Riot's own `RiotClientInstalls.json`, so another drive works with no edits, and the
account, region and platform all come from whichever client is running.

**Send only the `.exe`.** Do not zip the project folder: `data/` holds your match history,
including other players' puuids and Riot IDs. It is gitignored, so sharing via git is
safe, but a zip of the folder is not.

What to warn them about:

- **Windows SmartScreen** will say "Windows protected your PC", because the executable is
  unsigned. More info → Run anyway. Signing it needs a paid code-signing certificate.
- **Antivirus false positives** are common for PyInstaller one-file builds. Nothing can be
  done about this short of signing.
- **Windows 10** may lack the Edge WebView2 runtime that Windows 11 ships with. If it is
  missing the app now falls back to opening the dashboard in their browser and keeps
  tracking, rather than failing — but installing the WebView2 runtime gets them the real
  window.
- **First launch imports history automatically** — roughly their last twenty games, plus
  ranks for the players in them — so the app has something in it before they have played
  a single game with it running.

Their data lives in their own `%LOCALAPPDATA%\LoLcal History`. Yours never travels with
the executable.

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

**LP change** is recorded per match. Promotion and demotion are handled — a promotion
resets you to low LP in the new tier, which naive subtraction would report as a large
loss. It only appears for games on a ranked ladder, so Classic games show it and
ARAM/Mayhem do not.

The rank you held *going into* a game is written the moment the game is captured, before
the new LP has necessarily landed. That ordering matters: the first version worked the
change out 30 seconds later inside the running session, and closing the app in that window
lost the reading for good. Now the change can be completed on any later launch.

`lolhist ranks` also back-fills LP for older games by differencing consecutive rank
snapshots on the same ladder. Games that cannot be bracketed — the first on a ladder, or
any with an unsettled gap — are left blank rather than given an invented number.

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

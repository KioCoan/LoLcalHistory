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
Your history is not in the repository at all — it lives in `%LOCALAPPDATA%\LoLcal History`,
and the legacy `data/` folder is gitignored along with the build output.

That matters beyond preference: Riot asked that League Classic data not be aggregated or
displayed publicly. A private record of your own games, that never leaves your machine,
is a different thing — and it stays that way only if the tool never grows an export.

**The one request that goes out** is the update check: a `GET` to this repository's
public releases API to compare version numbers, made when the app opens and once an hour
while it stays open. It carries no query, no body and no identifier, so all GitHub can
observe is that some machine asked what the latest version is. Turn it off completely
with `LOLHIST_NO_UPDATE_CHECK=1`.

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

The normal way to use this is the desktop app the installer above gives you: one
executable that tracks your games *and* shows them, with no terminal and no browser. It
opens a native window with the dashboard while the watcher records games in the
background.

To build it yourself from a checkout:

```powershell
.\build-app.ps1
```

That produces `dist\LoLcal History.exe` (~26 MB, single file), which runs the same way.

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

**The window reopens where you left it.** Its position, size and whether it was maximised
are saved when you close it, hide it to the tray, or quit for an update, and restored on
the next launch. A remembered position is checked against the monitors actually connected
first: if you leave the window on a second screen and then unplug it, the size is kept but
the position is dropped and the window is centred, rather than opening somewhere you
cannot see it. Nothing is ever restored minimised, since that looks exactly like the app
failing to start.

**The page grows with the window.** Maximise it and the text, icons, padding and the
content column all get larger together, rather than leaving a narrow strip of small type
in the middle of a big screen. The whole stylesheet is written in `rem` and hung off a
single root font size, which runs from 15px in the smallest allowed window to 21px
maximised on a 3440×1440 ultrawide. It follows whichever of the window's width and height
has less room — a window dragged wide but left short has nowhere to put type its width
alone would ask for — and since it reads the viewport rather than any maximised flag, a
window merely dragged larger is treated the same way. Windows display scaling is already
folded in, so 150% does not compound.

**Launching it twice** does not start a second copy — two watchers on one database would
both record every game and contend over the same rows. The second launch raises the
window of the one already running, which is what you wanted anyway if it was sitting in
the tray.

The current version is shown in the footer, alongside whether a newer release exists.

## What a match row shows

When, mode, champion, result, rank, LP, K/D/A, CS, damage, build, length and which
capture path recorded it.

The champion carries its portrait, and the build is the whole loadout: summoner spells,
six item slots, then the trinket set slightly apart. Hovering an item names it. Empty
slots are drawn rather than skipped, so a four-item game cannot be mistaken for a
six-item one.

Clicking a row expands it into a scoreboard: both teams with their portraits, builds,
K/D/A and rank on whichever ladder the mode uses, plus your augments for the game.

Icons are optional throughout. Until the watcher has run once there are none, and every
one that is missing simply leaves the name it sat beside — the layout does not depend on
the picture arriving.

## The live match

The **Live** tab shows the game you are in right now: both teams, every player's rank on
each ladder they play, their champion and mastery on it, their three most-played
champions, their runes, and their K/D/A, CS and build as the game goes on. It refreshes
every three seconds while you are looking at it, and not at all while you are not.

Nothing here is stored. Close the app mid-game and there is nothing to lose — the tab is
assembled fresh from the client each time you open it, which is also why it needs no
migration and cannot corrupt anything.

It is built from two sources. The client owns the roster, because it is the only one that
reports puuids, and a puuid is what every rank and mastery lookup is keyed by. The game
itself, on its own local port, owns everything that changes while you play. They are
joined on Riot ID. The expensive half — names, ranks, mastery, about thirty requests — is
built once per game and reused, because none of it changes mid-match; only the game's own
feed is re-read on each refresh.

Three limits, all of them the client's rather than choices made here:

- **Win rates exist only for your own account.** The client reports `wins` for anyone in
  the lobby but reports `losses` only for you — everyone else comes back with a real win
  count and a flat zero. Believing that would give every stranger in the game a 100% win
  rate, so their win count is shown alone and no rate is claimed for it.
- **Runes are the keystone and the two trees, and no more.** A full rune page is not
  published for other players by any API.
- **The tab is empty until the game actually starts.** Champion select is not covered.

**League Classic is its own case.** It runs the *old* systems — a rune page of marks,
seals, glyphs and quintessences, and a mastery page of thirty points — and reports nothing
through the modern rune fields, which are empty for every player in a Classic game. Those
pages live on the account loadout instead, so the tab shows yours in full: every rune with
its name and icon, and the mastery page with its point total. It is account-scoped, so the
other nine players' pages cannot be read at all, and the tab says so. Individual Classic
masteries have no name anywhere in the client's assets, so the mastery page is shown by
name and point count rather than rune by rune.

If the game's local port is not answering, ranks, records, champions and mastery still
fill in from the client; only runes and live scores go missing, and the tab says which.

`lolhist live` prints the same thing in the terminal, which is the quickest way to check
the in-game half is reachable — that port only exists while a game is running.

## More than one account

The database holds every account you sign in with, and attributes each stat to the one
that earned it. A selector appears in the filter bar **only when a second account has
been seen**, so the ordinary single-account window is unchanged.

The view defaults to whichever account is currently logged in, resolved on the server, so
a dashboard left open across a switch follows you rather than quietly pooling two
accounts' games. Picking an account explicitly overrides that until you change it back.

Rank series are kept per account. This matters more than it sounds: they were once shared,
so the first game after signing into a smurf measured its Silver against the main's
Diamond and invented a large LP change. An alt you actually duo with now shows up in
Teammates instead of being hidden.

## Where your data lives

Your history lives in `%LOCALAPPDATA%\LoLcal History` — the app and the CLI share it,
along with the raw archive, the icon mirror and the cached asset names. A frozen build
unpacks to a temporary folder that is deleted on exit, so the database cannot live beside
the executable. Errors go to `app.log` in that same folder, since a windowed app has
nowhere to print.

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
.\.venv\Scripts\python.exe -m pip install -e ".[dev,desktop]"
```

`desktop` pulls in the native window and tray icon; without it everything works
except `lolhist gui`. Add `build` as well if you want to produce the executable.

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

Other commands: `lolhist live` to print the match in progress (`--json` for the raw
snapshot), `lolhist stats` for a quick terminal summary, `lolhist probe` to dump raw
client payloads to the `samples/` folder when something needs diagnosing, and
`lolhist resolve-names` to rebuild champion, queue and augment names from the stored ids
— useful after a patch adds augments your cached assets did not know about.

All of those folders live under `%LOCALAPPDATA%\LoLcal History`, not in the checkout.

## Sharing it with someone else

Send them the [releases page](https://github.com/KioCoan/LoLcalHistory/releases). The
installer is self-contained and account-agnostic: nothing about your account, region or
install path is baked into it — the League install is looked up from Riot's own
`RiotClientInstalls.json`, so another drive works with no edits, and the account, region
and platform all come from whichever client is running. They get the update button too.

**Never send the project folder.** Your match history is not in the repo — it lives in
`%LOCALAPPDATA%\LoLcal History` and holds other players' puuids and Riot IDs — but a zip
of a working checkout can pick up a stray `data/` folder from an older build. The link,
or the `.exe` on its own, is always safe.

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

So its state is persisted to `health.json` in the data folder and surfaced in two places:

- **`lolhist doctor`** prints capture counts, the last game caught, and any error — and
  **exits non-zero when the watcher is failing**, so it can be used as an alarm rather
  than something you have to read.
- **The dashboard** shows a red banner when captures are failing, naming the error, plus
  an always-visible one-line status under the summary cards.

A game that ends with no stats block available is reported separately as *missed*, not
failed, and does not raise the alarm — the history sweep still picks it up, and a warning
that cries wolf is a warning you learn to ignore.

## How it works

```
League Client ──lockfile──> connection ──┬── WebSocket ──> watcher  ──┐
                                         ├── REST      ──> backfill ──┤
                                         │                             v
                                         │    raw/*.json.gz <── normalize ──> SQLite
                                         │                                       │
                                         └── assets ──> static/icons/ ──┐        │
                                                                        v        v
                                                                   Flask dashboard
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

**Raw payloads are archived.** Every capture is gzipped to `raw/` before it is parsed.
The LCU is unsupported and its payload shapes drift between patches; keeping the raw JSON
means a shape change costs a re-parse of files you already have, never lost history.

That has already earned its keep once for a reason it was not designed for: a database
corrupted beyond what SQLite's own `.recover` could rebuild — the table definitions were
gone — was restored in full by replaying the archive through the same normaliser that
wrote it the first time. Every match, participant and augment came back.

**Icons come from the client too.** Champion portraits, item icons, summoner spells and
your profile picture are copied out of the client's own assets into `static/icons/` the
first time they are referenced, then served from disk. So the dashboard renders with
League closed, and no image request ever leaves the machine.

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

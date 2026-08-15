"""Checking for, downloading and installing a new release.

**This is the one part of the tool that talks to the internet.** Everything else
is deliberately offline — match data never leaves the machine, and champion,
item and augment names come from the game client's own files rather than a CDN.
That rule is about your data, and it still holds here: the update check is a
GET to GitHub's public releases API for this repository, it sends no query and
no body, and the only thing GitHub learns is that some machine asked what the
latest version is. No match history, no Riot ID, no identifier of any kind.

Set `LOLHIST_NO_UPDATE_CHECK=1` to switch it off completely.

The installer is verified against the SHA-256 published in the release before it
is allowed to run. An unverified download is never executed — a corrupted or
substituted file would otherwise be run with the user's own privileges.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

from . import config
from .version import LATEST_RELEASE_API, RELEASES_URL, __version__, is_newer

log = logging.getLogger(__name__)

# How often to look again while the app sits open. One request an hour to a
# public endpoint, sending nothing about you — see the module docstring.
CHECK_INTERVAL_SECONDS = 3600
CACHE_FILE = "update.json"
CHECKSUM_ASSET = "SHA256SUMS.txt"
DOWNLOAD_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# Anything larger is not our installer; refuse it rather than filling the disk.
MAX_INSTALLER_BYTES = 300 * 1024 * 1024

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# The waiter has to outlive the app that starts it, invisibly.
#
# Deliberately NOT DETACHED_PROCESS, which is the obvious choice and does not
# work: with no console at all, PowerShell exits immediately without running
# anything, and Popen still reports success — a silent no-op. A new process
# group gives the same independence (no Ctrl+C or Ctrl+Break inherited from
# us), and Windows does not kill children when their parent exits anyway.
_INDEPENDENT = _NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

# How long the waiter gives us to shut down before installing anyway. Generous:
# the cost of waiting is a slower update, the cost of giving up early is the
# crash this whole arrangement exists to prevent.
QUIT_WAIT_SECONDS = 90

# Ordinary caution around a file the installer has only just written, not a
# fix for anything. The failure this was added for turned out to be the
# inherited unpack directory — see `_unfrozen_env`.
RELAUNCH_DELAY_SECONDS = 8

# Where the installer puts it. `PrivilegesRequired=lowest` and a fixed
# `DefaultDirName` in the .iss make this the same on every machine.
RELAUNCH_TARGET = (
    Path(os.environ.get("LOCALAPPDATA") or Path.home())
    / "Programs" / config.APP_NAME / f"{config.APP_NAME}.exe"
)

# Set by the desktop app so the updater can close the window before the
# installer tries to replace the executable underneath it. Absent when running
# `lolhist serve`, in which case the user is told to close the app themselves.
_quit_hook = None


def set_quit_hook(callback) -> None:
    global _quit_hook
    _quit_hook = callback


def enabled() -> bool:
    return os.environ.get("LOLHIST_NO_UPDATE_CHECK", "").strip() not in ("1", "true", "yes")


@dataclass
class Release:
    version: str = ""
    url: str = RELEASES_URL
    notes: str = ""
    published_at: str = ""
    installer_name: str = ""
    installer_url: str = ""
    installer_size: int = 0
    checksums_url: str = ""

    @property
    def installable(self) -> bool:
        return bool(self.installer_url and self.checksums_url)


@dataclass
class State:
    """What the dashboard polls. Also what gets cached between launches."""

    current: str = __version__
    latest: str = ""
    available: bool = False
    checked_at: float = 0.0
    releases_url: str = RELEASES_URL
    release: dict = field(default_factory=dict)
    # idle | downloading | verifying | installing | failed
    status: str = "idle"
    progress: int = 0
    error: str = ""


_state = State()
_lock = threading.Lock()


def _cache_path() -> Path:
    return config.DATA_DIR / CACHE_FILE


def _load_cache() -> None:
    try:
        raw = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    with _lock:
        for key in ("latest", "checked_at", "release"):
            if key in raw:
                setattr(_state, key, raw[key])
        # Recomputed rather than trusted: the cache may have been written by an
        # older build, and `available` is what drives a prompt to install.
        _state.available = bool(_state.latest) and is_newer(_state.latest)


def _save_cache() -> None:
    try:
        config.ensure_dirs()
        _cache_path().write_text(
            json.dumps(
                {
                    "latest": _state.latest,
                    "checked_at": _state.checked_at,
                    "release": _state.release,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError:
        log.debug("could not cache the update check", exc_info=True)


def state() -> dict:
    if not _state.checked_at:
        _load_cache()
    with _lock:
        snapshot = asdict(_state)
    snapshot["enabled"] = enabled()
    snapshot["frozen"] = config.FROZEN
    return snapshot


def _parse_release(payload: dict) -> Release:
    release = Release(
        version=(payload.get("tag_name") or "").lstrip("vV"),
        url=payload.get("html_url") or RELEASES_URL,
        notes=payload.get("body") or "",
        published_at=payload.get("published_at") or "",
    )
    for asset in payload.get("assets") or []:
        name = asset.get("name") or ""
        url = asset.get("browser_download_url") or ""
        if not url:
            continue
        if name == CHECKSUM_ASSET:
            release.checksums_url = url
        elif name.lower().endswith("setup.exe"):
            release.installer_name = name
            release.installer_url = url
            release.installer_size = int(asset.get("size") or 0)
    return release


def check(force: bool = False) -> dict:
    """Ask GitHub for the latest release. Cheap, cached, and never fatal."""
    if not enabled():
        return state()

    if not _state.checked_at:
        _load_cache()
    if not force and time.time() - _state.checked_at < CHECK_INTERVAL_SECONDS:
        return state()

    try:
        response = httpx.get(
            LATEST_RELEASE_API,
            timeout=httpx.Timeout(10.0, connect=5.0),
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"LoLcalHistory/{__version__}",
            },
            follow_redirects=True,
        )
        if response.status_code == 404:
            # No release published yet, or the repository is private. Neither is
            # an error worth showing anybody.
            log.debug("no published release to compare against")
            with _lock:
                _state.checked_at = time.time()
            return state()
        response.raise_for_status()
        release = _parse_release(response.json())
    except Exception as exc:
        log.debug("update check failed: %s", exc)
        with _lock:
            # Recorded so a machine that is simply offline does not retry on
            # every page refresh.
            _state.checked_at = time.time()
        return state()

    with _lock:
        _state.latest = release.version
        _state.release = asdict(release)
        _state.available = bool(release.version) and is_newer(release.version)
        _state.checked_at = time.time()
    _save_cache()

    if _state.available:
        log.info("version %s is available (running %s)", release.version, __version__)
    return state()


def _set(**fields) -> None:
    with _lock:
        for key, value in fields.items():
            setattr(_state, key, value)


def _expected_digest(text: str, filename: str) -> str | None:
    """Pull one file's hash out of a `sha256sum`-style listing."""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == filename:
            return parts[0].lower()
    return None


def _download(client: httpx.Client, url: str, target: Path, on_progress) -> None:
    with client.stream("GET", url) as response:
        response.raise_for_status()
        total = int(response.headers.get("content-length") or 0)
        if total > MAX_INSTALLER_BYTES:
            raise ValueError("the download is implausibly large; refusing it")
        written = 0
        with target.open("wb") as handle:
            for chunk in response.iter_bytes(chunk_size=64 * 1024):
                handle.write(chunk)
                written += len(chunk)
                if written > MAX_INSTALLER_BYTES:
                    raise ValueError("the download is implausibly large; refusing it")
                if total:
                    on_progress(int(100 * written / total))


def install(release: Release | None = None) -> None:
    """Download, verify and run the installer, then get out of its way.

    Runs on a worker thread; the dashboard follows along by polling `state()`.
    """
    if release is None:
        release = Release(**_state.release) if _state.release else Release()

    if not release.installable:
        _set(status="failed", error="This release has no installer attached.")
        return
    if not config.FROZEN:
        _set(
            status="failed",
            error="Running from a source checkout — update with git instead.",
        )
        return

    _set(status="downloading", progress=0, error="")
    try:
        config.ensure_dirs()
        staging = config.DATA_DIR / "updates"
        staging.mkdir(parents=True, exist_ok=True)
        target = staging / release.installer_name

        headers = {"User-Agent": f"LoLcalHistory/{__version__}"}
        with httpx.Client(timeout=DOWNLOAD_TIMEOUT, follow_redirects=True,
                          headers=headers) as client:
            _download(client, release.installer_url, target,
                      lambda pct: _set(progress=pct))

            _set(status="verifying", progress=100)
            listing = client.get(release.checksums_url)
            listing.raise_for_status()

        expected = _expected_digest(listing.text, release.installer_name)
        if not expected:
            raise ValueError(f"{release.installer_name} is not in {CHECKSUM_ASSET}")

        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if digest != expected:
            target.unlink(missing_ok=True)
            raise ValueError("the download does not match its published checksum")

        log.info("verified %s (%s)", release.installer_name, digest[:12])
        _set(status="installing")
        _launch(target)
    except Exception as exc:
        log.warning("update failed: %s", exc, exc_info=True)
        _set(status="failed", error=_readable(exc))


def _readable(exc: Exception) -> str:
    """Something a person can act on.

    The raw text is in the log; on screen, `[Errno 11001] getaddrinfo failed`
    tells nobody anything. Anything we have not phrased is passed through rather
    than replaced with a vague catch-all.
    """
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return "Could not reach GitHub — check your internet connection."
    if isinstance(exc, httpx.TimeoutException):
        return "The download timed out."
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code if exc.response is not None else "?"
        return f"GitHub refused the download ({code})."
    if isinstance(exc, OSError) and not isinstance(exc, httpx.HTTPError):
        return f"Could not write the download to disk: {exc.strerror or exc}"
    return str(exc) or exc.__class__.__name__


# How a one-file build tells its own child process where it unpacked itself.
# `_MEIPASS2` is the historical name; PyInstaller 6 renamed it and added the
# rest. Every one of them has to go — a single survivor is enough to redirect
# the relaunched app back into a directory that no longer exists.
_PYI_ENV_VARS = ("_MEIPASS2", "_PYI_APPLICATION_HOME_DIR", "_PYI_ARCHIVE_FILE",
                 "_PYI_PARENT_PROCESS_LEVEL", "_PYI_SPLASH_IPC")


def _unfrozen_env() -> dict[str, str]:
    """This app's environment with the one-file bookkeeping removed.

    A one-file build unpacks itself into `%TEMP%\\_MEIxxxxxx` and points its
    child there through the environment. Anything this process spawns inherits
    that pointer, and the updater spawns a process whose whole job is to outlive
    it — so the relaunched app started up believing it had already been unpacked,
    into a directory belonging to the version that had just been replaced.

    When the two builds used different Python versions it failed loudly:

        Failed to load Python DLL '...\\_MEI127082\\python313.dll'

    with `_MEI127082` being the *previous* app's directory, holding python314.dll.
    Matching versions would have hidden it, not fixed it: the new app would have
    quietly run the old release's unpacked code.

    Stripping these makes the relaunched app unpack itself, which is what a
    normal launch from the Start Menu does — and why starting it by hand always
    worked.
    """
    env = {k: v for k, v in os.environ.items() if k not in _PYI_ENV_VARS}
    # Belt and braces: PyInstaller has renamed these before and may again.
    return {k: v for k, v in env.items() if not k.startswith("_PYI_")}


def _launch(installer: Path) -> None:
    """Arrange for the installer to run once this app has fully exited.

    Not started directly, and the reason matters. A one-file build runs as two
    processes: a bootloader that unpacks the interpreter into %TEMP%\\_MEIxxxx
    and owns that directory, and a child that runs the code. Starting Setup
    first meant its Restart Manager found us still running and — under
    `CloseApplications=force` — killed the bootloader in the middle of our own
    orderly shutdown. The unpacked directory went with it while the child was
    still alive, and the child died on its next import:

        Failed to load Python DLL '...\\_MEI77522\\python313.dll'

    So a detached waiter watches for every one of our processes to disappear
    and only then starts Setup, which by that point has nothing to close and
    nothing to force. The force flag stays as a safety net for somebody running
    the installer by hand with the app open.
    """
    log_path = config.DATA_DIR / "install.log"
    flags = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"]
    # Wrapped in real quotes, because the data folder is "LoLcal History" and
    # Start-Process joins its argument list with spaces without quoting any of
    # it. Unquoted, Setup received "/LOG=...\LoLcal" and wrote its log to a
    # stray file of that name — which is how a failed update left no log at all
    # in the place anyone would look for one.
    quoted_log = str(log_path).replace('"', '""')
    arguments = ",".join([f"'{flag}'" for flag in flags] + [f"'\"/LOG={quoted_log}\"'"])
    # Doubled single quotes are PowerShell's escape inside a single-quoted
    # string; paths here are under the user's profile and may contain either.
    installer_literal = str(installer).replace("'", "''")
    process_name = config.APP_NAME.replace("'", "''")

    relaunch = str(RELAUNCH_TARGET).replace("'", "''")
    script = (
        # Both processes, not just this one: the bootloader is the one holding
        # the unpacked directory, and it outlives the child.
        f"$deadline = (Get-Date).AddSeconds({QUIT_WAIT_SECONDS}); "
        f"while ((Get-Process -Name '{process_name}' -ErrorAction SilentlyContinue) "
        f"-and (Get-Date) -lt $deadline) {{ Start-Sleep -Milliseconds 250 }}; "
        # A breath for the bootloader to finish removing its temp directory.
        "Start-Sleep -Milliseconds 800; "
        f"$s = Start-Process -FilePath '{installer_literal}' -ArgumentList {arguments} "
        "-PassThru; "
        "try { $s.WaitForExit() } catch {}; "
        # A breath before starting a 27 MB executable the installer has only
        # just finished writing, so an on-access scanner is not still holding
        # it. This pause was originally blamed for the "Failed to load Python
        # DLL" failure; it was not the cause — see `_unfrozen_env` — and the
        # delay is kept only as ordinary caution around a freshly written file.
        f"Start-Sleep -Seconds {RELAUNCH_DELAY_SECONDS}; "
        f"if (Test-Path '{relaunch}') {{ Start-Process -FilePath '{relaunch}' }}"
    )

    subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
         "-Command", script],
        creationflags=_INDEPENDENT,
        close_fds=True,
        env=_unfrozen_env(),
    )

    if _quit_hook is None:
        log.info("installer started; close the app to let it finish")
        return

    # A moment's grace so the browser receives the response that told it the
    # install had started, before the server it asked goes away.
    def bow_out() -> None:
        time.sleep(1.5)
        log.info("quitting so the installer can replace the executable")
        try:
            _quit_hook()
        except Exception:
            log.exception("could not quit cleanly; exiting anyway")
            os._exit(0)

    threading.Thread(target=bow_out, name="update-quit", daemon=True).start()


def start_install() -> dict:
    """Kick the install off in the background and report immediately."""
    if _state.status in ("downloading", "verifying", "installing"):
        return state()
    threading.Thread(target=install, name="update-install", daemon=True).start()
    _set(status="downloading", progress=0, error="")
    return state()


_stop_checking = threading.Event()


def _check_loop(interval: float) -> None:
    """Check once for the launch, then once an interval until asked to stop."""
    check(force=True)
    while not _stop_checking.wait(interval):
        # Nothing to learn mid-install, and the answer would be discarded
        # anyway — the version being installed is already known.
        if _state.status in ("downloading", "verifying", "installing"):
            continue
        check(force=True)


def check_in_background(interval: float = CHECK_INTERVAL_SECONDS) -> None:
    """Check on launch, then hourly for as long as the app stays open.

    The launch check is forced rather than cached. Opening the app is when
    someone expects to be told about a new version, and a cached answer meant a
    release went unnoticed across a restart because the app had last looked two
    hours earlier.

    Nothing re-checked at all before this: the interval existed but only one
    check was ever fired, so an app left open for a week never looked again.
    """
    if not enabled():
        log.info("update check disabled by LOLHIST_NO_UPDATE_CHECK")
        return
    _stop_checking.clear()
    threading.Thread(
        target=_check_loop, args=(interval,), name="update-check", daemon=True
    ).start()


def stop_checking() -> None:
    """End the recurring check. Called when the app shuts down."""
    _stop_checking.set()


def describe() -> str:
    current = state()
    if not current["enabled"]:
        return f"{config.APP_NAME} {__version__} (update check disabled)"
    if current["available"]:
        return f"{config.APP_NAME} {__version__} — {current['latest']} is available"
    return f"{config.APP_NAME} {__version__} (up to date)"


if __name__ == "__main__":  # `python -m lolhist.updates` for a quick look
    logging.basicConfig(level=logging.INFO)
    check(force=True)
    print(describe())
    sys.exit(0)

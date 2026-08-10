"""Finding the running League Client and authenticating against its local API.

The client writes a lockfile containing the port and a per-launch auth token:

    LeagueClient:<pid>:<port>:<token>:https

Two things about that file matter enough to be worth stating plainly:

1. It is NOT reliably deleted when the client exits, so its presence proves
   nothing. Callers must probe the port (see `client.connect`).
2. The port and token change on every client launch, so anything that
   reconnects must re-run discovery rather than cache credentials.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

# Both are candidates: LeagueClientUx.exe hosts the API, LeagueClient.exe is the
# launcher. Checking both makes the fallback resilient to Riot moving the flags.
CLIENT_PROCESSES = ("LeagueClientUx.exe", "LeagueClient.exe")

# Keeps a spawned helper from opening a console window. Only exists on Windows;
# zero is the "no special flags" default everywhere else.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class ClientUnavailable(RuntimeError):
    """The League Client is not running, or is not reachable on its API port."""


@dataclass(frozen=True)
class Credentials:
    port: int
    password: str
    protocol: str = "https"
    pid: int | None = None
    origin: str = "lockfile"

    @property
    def base_url(self) -> str:
        return f"{self.protocol}://127.0.0.1:{self.port}"

    @property
    def websocket_url(self) -> str:
        scheme = "wss" if self.protocol == "https" else "ws"
        return f"{scheme}://127.0.0.1:{self.port}/"

    def __repr__(self) -> str:  # never let the token reach a log or traceback
        return f"Credentials(port={self.port}, origin={self.origin!r}, password=<redacted>)"


def installed_league_dirs() -> list[Path]:
    """Where League is installed on this machine, best source first.

    Read from Riot's own install record rather than assumed, so a friend who
    installed to D: works without editing anything. Deliberately not a
    filesystem scan: an abandoned older install still contains a lockfile, and
    scanning would happily find it.
    """
    found: list[Path] = []

    try:
        raw = config.RIOT_INSTALLS_JSON.read_text(encoding="utf-8")
        installs = json.loads(raw)
    except (OSError, ValueError):
        installs = {}

    associated = installs.get("associated_client")
    if isinstance(associated, dict):
        for install_path in associated:
            # Riot writes these with forward slashes.
            candidate = Path(str(install_path).replace("/", "\\"))
            if candidate.is_dir() and candidate not in found:
                found.append(candidate)

    for candidate in config.FALLBACK_INSTALL_DIRS:
        if candidate.is_dir() and candidate not in found:
            found.append(candidate)

    return found


def lockfile_candidates() -> list[Path]:
    """Every lockfile path worth trying, in priority order."""
    if config.LOCKFILE_OVERRIDE:
        return [Path(config.LOCKFILE_OVERRIDE)]
    return [directory / "lockfile" for directory in installed_league_dirs()]


def read_lockfile(path: Path | None = None) -> Credentials | None:
    """Parse a lockfile, or return None if it is missing or malformed."""
    if path is None:
        for candidate in lockfile_candidates():
            credentials = read_lockfile(candidate)
            if credentials is not None:
                return credentials
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        log.debug("lockfile unreadable at %s: %s", path, exc)
        return None

    parts = raw.split(":")
    if len(parts) != 5:
        log.warning("lockfile at %s has %d fields, expected 5", path, len(parts))
        return None

    _name, pid, port, password, protocol = parts
    try:
        return Credentials(
            port=int(port),
            password=password,
            protocol=protocol,
            pid=int(pid),
            origin="lockfile",
        )
    except ValueError:
        log.warning("lockfile at %s has a non-numeric pid or port", path)
        return None


def read_process_args() -> Credentials | None:
    """Recover credentials from the client's command line.

    Used when the client is running but the lockfile is missing or unreadable
    (it lives under Program Files, so permissions can bite).
    """
    query = " ".join(f"Name='{name}'" for name in CLIENT_PROCESSES).replace(" Name=", " or Name=")
    script = (
        f"Get-CimInstance Win32_Process -Filter \"{query}\" "
        "| Select-Object -ExpandProperty CommandLine"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=15,
            # Without this the packaged app — which has no console of its own —
            # flashes a PowerShell window on screen every time this runs, which
            # is on every reconnect attempt while the client is closed.
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("process lookup failed: %s", exc)
        return None

    port = _first_match(r"--app-port=(\d+)", result.stdout)
    token = _first_match(r"--remoting-auth-token=([\w-]+)", result.stdout)
    if not port or not token:
        return None
    return Credentials(port=int(port), password=token, origin="process")


def _first_match(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text or "")
    return match.group(1) if match else None


def iter_credentials():
    """Every set of credentials worth trying, best first.

    More than one can turn up — a second install, or a lockfile left behind by
    a client that has since closed — so the caller probes each rather than
    trusting the first. That is what makes a stale lockfile harmless instead of
    fatal.
    """
    seen: set[tuple] = set()

    for candidate in lockfile_candidates():
        credentials = read_lockfile(candidate)
        if credentials is not None and credentials.port not in seen:
            seen.add(credentials.port)
            yield credentials

    from_process = read_process_args()
    if from_process is not None and from_process.port not in seen:
        yield from_process


def describe_search() -> str:
    """Where discovery looked, for an error message.

    Pure string building — it must not repeat the search itself, or reporting a
    failure would cost another process lookup.
    """
    searched = ", ".join(str(p) for p in lockfile_candidates()) or "no known install"
    return (
        "No League Client credentials found. Looked for a lockfile in "
        f"{searched} and for a running client process. Is the client open?"
    )


def discover() -> Credentials:
    """The first available credentials.

    Finding them does not mean the client is running — only a live probe can
    say that.
    """
    for credentials in iter_credentials():
        log.debug("discovered credentials via %s", credentials.origin)
        return credentials
    raise ClientUnavailable(describe_search())

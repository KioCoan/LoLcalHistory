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


def read_lockfile(path: Path | None = None) -> Credentials | None:
    """Parse the lockfile, or return None if it is missing or malformed."""
    path = path or config.LOCKFILE_PATH
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


def discover() -> Credentials:
    """Locate the client's API credentials, lockfile first then process args.

    Succeeding here means only that credentials were found. It does not mean the
    client is running — that requires a live probe.
    """
    for source in (read_lockfile, read_process_args):
        creds = source()
        if creds is not None:
            log.debug("discovered credentials via %s", creds.origin)
            return creds
    raise ClientUnavailable(
        f"No League Client credentials found. Looked for a lockfile at "
        f"{config.LOCKFILE_PATH} and for a running client process. "
        "Is the client open?"
    )

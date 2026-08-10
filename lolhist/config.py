"""Paths and tunables.

Every path is overridable by environment variable so the tool can be pointed at
a different install or data directory without editing code.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# True when running from a PyInstaller build rather than a source checkout.
FROZEN = bool(getattr(sys, "frozen", False))

# Package resources — schema.sql, the dashboard template, the icon. PyInstaller
# keeps `__file__` pointing inside its extraction directory, so as long as those
# files are bundled under `lolhist/` this resolves in both modes.
PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent

APP_NAME = "LoLcal History"

_APPDATA_ROOT = Path(os.environ.get("LOCALAPPDATA") or Path.home())
# What the folder was called before the app was named.
_LEGACY_APPDATA_DIR = _APPDATA_ROOT / "lol-local-history"


def _resolve_data_dir() -> Path:
    """Where your history lives — the same place for the app and the CLI.

    Two things force this out of the project directory: a frozen build unpacks
    to a temporary folder that is deleted on exit, and splitting the two would
    mean games landing in whichever database happened to be running.

    The folder was renamed with the app. Moving it is a single atomic rename, so
    it either happens or it does not — and if it cannot (the app is open, or
    permissions say no) the old location keeps being used rather than silently
    starting an empty history beside it.
    """
    override = os.environ.get("LOLHIST_DATA")
    if override:
        return Path(override)

    current = _APPDATA_ROOT / APP_NAME
    if current.exists() or not _LEGACY_APPDATA_DIR.exists():
        return current

    try:
        _LEGACY_APPDATA_DIR.rename(current)
        return current
    except OSError:
        return _LEGACY_APPDATA_DIR


DATA_DIR = _resolve_data_dir()

# The pre-packaging location, kept only so an existing checkout can be migrated.
LEGACY_DATA_DIR = PROJECT_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
SAMPLES_DIR = DATA_DIR / "samples"
STATIC_DIR = DATA_DIR / "static"
DB_PATH = Path(os.environ.get("LOLHIST_DB", DATA_DIR / "history.db"))

# Riot records where each product is installed here, so the League directory is
# looked up rather than guessed. Guessing would break for anyone who installed
# to another drive — and a filesystem scan would be worse, since an abandoned
# older install still has a lockfile lying in it.
RIOT_INSTALLS_JSON = (
    Path(os.environ.get("PROGRAMDATA") or r"C:\ProgramData") / "Riot Games" / "RiotClientInstalls.json"
)

# Tried only if the file above is missing or names nothing usable.
FALLBACK_INSTALL_DIRS = (
    Path(r"C:\Riot Games\League of Legends"),
    Path(os.environ.get("PROGRAMFILES") or r"C:\Program Files") / "Riot Games" / "League of Legends",
    Path(os.environ.get("PROGRAMFILES(X86)") or r"C:\Program Files (x86)")
    / "Riot Games" / "League of Legends",
)

# Set LOLHIST_LOCKFILE to bypass discovery entirely.
LOCKFILE_OVERRIDE = os.environ.get("LOLHIST_LOCKFILE")

# Riot's published root cert, if the user drops it here. Absent by default —
# fetching it would mean an outbound call, which this tool does not make.
RIOT_CA_BUNDLE = PACKAGE_DIR / "riotgames.pem"

WEB_HOST = os.environ.get("LOLHIST_HOST", "127.0.0.1")
WEB_PORT = int(os.environ.get("LOLHIST_PORT", "8787"))

# Static data is refreshed when the cache is older than this and the client is up.
STATIC_MAX_AGE_DAYS = 3


def ensure_dirs() -> None:
    for path in (DATA_DIR, RAW_DIR, SAMPLES_DIR, STATIC_DIR):
        path.mkdir(parents=True, exist_ok=True)

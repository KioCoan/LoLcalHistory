"""Paths and tunables.

Every path is overridable by environment variable so the tool can be pointed at
a different install or data directory without editing code.
"""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent

DATA_DIR = Path(os.environ.get("LOLHIST_DATA", PROJECT_DIR / "data"))
RAW_DIR = DATA_DIR / "raw"
SAMPLES_DIR = DATA_DIR / "samples"
STATIC_DIR = DATA_DIR / "static"
DB_PATH = Path(os.environ.get("LOLHIST_DB", DATA_DIR / "history.db"))

# The active install. Deliberately NOT a filesystem scan: this machine also has
# a stale D:\Riot Games install from 2024, and scanning would find its lockfile.
DEFAULT_LOCKFILE = Path(r"C:\Riot Games\League of Legends\lockfile")
LOCKFILE_PATH = Path(os.environ.get("LOLHIST_LOCKFILE", DEFAULT_LOCKFILE))

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

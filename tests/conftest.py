"""Shared test setup.

The data directory now defaults to a real user-profile location, so tests are
pinned to a temporary one. Without this a stray call to `archive_raw` or
`ensure_dirs` would write into the actual match history during a test run.
"""

from __future__ import annotations

import pytest

from lolhist import config


@pytest.fixture(autouse=True)
def isolate_data_dir(tmp_path, monkeypatch):
    root = tmp_path / "lolhist-data"
    monkeypatch.setattr(config, "DATA_DIR", root)
    monkeypatch.setattr(config, "RAW_DIR", root / "raw")
    monkeypatch.setattr(config, "SAMPLES_DIR", root / "samples")
    monkeypatch.setattr(config, "STATIC_DIR", root / "static")
    monkeypatch.setattr(config, "DB_PATH", root / "history.db")
    return root

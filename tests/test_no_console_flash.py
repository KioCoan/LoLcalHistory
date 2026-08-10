"""The packaged app must never flash a console window.

It is built windowed, so it has no console of its own — any helper process
spawned without the suppression flag opens a visible one. The process lookup
runs on every reconnect attempt while the client is closed, so a missing flag
means windows popping up repeatedly while you play.
"""

from __future__ import annotations

import subprocess

import pytest

from lolhist import client as client_mod
from lolhist import config, connection


@pytest.fixture
def no_lockfiles(monkeypatch):
    """Force the path that falls through to the process lookup."""
    monkeypatch.setattr(config, "LOCKFILE_OVERRIDE", None)
    monkeypatch.setattr(config, "RIOT_INSTALLS_JSON", config.Path("nope.json"))
    monkeypatch.setattr(config, "FALLBACK_INSTALL_DIRS", ())


class TestHiddenWindow:
    def test_process_lookup_suppresses_its_window(self, monkeypatch):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured.update(kwargs)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(connection.subprocess, "run", fake_run)
        connection.read_process_args()

        assert "creationflags" in captured, "spawned without window suppression"
        expected = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        assert captured["creationflags"] == expected

    def test_flag_constant_is_real_on_windows(self):
        """Guards against a typo silently degrading to zero."""
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            assert connection._NO_WINDOW == subprocess.CREATE_NO_WINDOW
            assert connection._NO_WINDOW != 0


class TestSingleLookup:
    """Two windows appeared per attempt, not one: the failure message was built
    by re-running the entire search."""

    def test_failed_connect_looks_up_processes_once(self, monkeypatch, no_lockfiles):
        calls = []

        def counting_lookup():
            calls.append(1)
            return None

        monkeypatch.setattr(connection, "read_process_args", counting_lookup)

        with pytest.raises(connection.ClientUnavailable):
            client_mod.connect()

        assert len(calls) == 1, f"process lookup ran {len(calls)} times, expected 1"

    def test_the_error_still_says_where_it_looked(self, monkeypatch, no_lockfiles):
        monkeypatch.setattr(connection, "read_process_args", lambda: None)

        with pytest.raises(connection.ClientUnavailable) as exc:
            client_mod.connect()

        message = str(exc.value)
        assert "lockfile" in message.lower()
        assert "client open" in message.lower()

    def test_describe_search_does_not_spawn_anything(self, monkeypatch):
        """It is called on the failure path; it must be pure string building."""

        def explode(*args, **kwargs):
            raise AssertionError("describe_search spawned a process")

        monkeypatch.setattr(connection.subprocess, "run", explode)
        assert "No League Client credentials found" in connection.describe_search()

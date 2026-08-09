"""Health reporting, and the guarantee that a capture fault stays contained."""

from __future__ import annotations

import asyncio

import pytest

from lolhist import config, health, store
from lolhist.static_data import StaticData
from lolhist.watcher import Watcher


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(config, "SAMPLES_DIR", tmp_path / "samples")
    monkeypatch.setattr(config, "STATIC_DIR", tmp_path / "static")
    return tmp_path


class TestHealthState:
    def test_absent_state_is_not_degraded(self):
        assert health.load() == {}
        assert health.is_degraded() is False
        assert "never run" in health.describe()[0]

    def test_failures_accumulate_and_degrade(self):
        assert health.record_failure("ProgrammingError", "wrong thread") == 1
        assert health.record_failure("ProgrammingError", "wrong thread") == 2
        assert health.is_degraded() is True

        state = health.load()
        assert state["captures_failed"] == 2
        assert state["last_error"]["kind"] == "ProgrammingError"

    def test_a_success_clears_the_streak(self):
        health.record_failure("Boom", "x")
        assert health.is_degraded() is True

        health.record_capture(123, "eog", "ARAM: Mayhem")
        assert health.is_degraded() is False
        state = health.load()
        assert state["captures_ok"] == 1
        assert state["last_capture_game_id"] == 123

    def test_missed_capture_is_not_a_failure(self):
        """A mode with no stats block must not raise a false alarm."""
        health.record_missed()
        assert health.is_degraded() is False
        assert health.load()["captures_missed"] == 1

    def test_degraded_state_says_so_loudly(self):
        health.record_failure("ProgrammingError", "same thread only")
        text = " ".join(health.describe())
        assert "DEGRADED" in text
        assert "same thread only" in text

    def test_unmapped_uris_are_deduped_and_capped(self):
        health.record_unmapped_uri("/lol-end-of-game/v1/new-thing")
        health.record_unmapped_uri("/lol-end-of-game/v1/new-thing")
        assert health.load()["unmapped_uris"] == ["/lol-end-of-game/v1/new-thing"]

        for i in range(health.MAX_UNMAPPED + 5):
            health.record_unmapped_uri(f"/lol-end-of-game/v1/thing-{i}")
        assert len(health.load()["unmapped_uris"]) == health.MAX_UNMAPPED


class TestCaptureFaultContainment:
    """The original bug: a storage error reached the reconnect loop, which
    treated it as a dropped connection and carried on reporting normality."""

    @pytest.fixture
    def watcher(self, tmp_path):
        conn = store.open_db(tmp_path / "w.db")
        yield Watcher(conn)
        conn.close()

    @pytest.fixture
    def client(self):
        """Stands in for the LCU client; every optional lookup comes back empty."""

        class StubClient:
            def get_json_or_none(self, path, **params):
                return None

        return StubClient()

    def test_store_failure_is_recorded_not_raised(self, watcher, client):
        payload = {"gameId": 999, "teams": []}

        async def fake_fetch(_client):
            return payload

        def explode(*_args):
            raise RuntimeError("SQLite objects created in a thread...")

        watcher._fetch_eog = fake_fetch
        watcher._store = explode

        # Must not raise: reaching the reconnect loop is what hid this before.
        asyncio.run(watcher._capture(client=client, static=StaticData()))

        state = health.load()
        assert state["captures_failed"] == 1
        assert state["consecutive_failures"] == 1
        assert "SQLite" in state["last_error"]["message"]
        assert health.is_degraded() is True

    def test_capture_flag_is_released_after_a_failure(self, watcher, client):
        """A stuck flag would silently disable every later capture."""

        async def fake_fetch(_client):
            return {"gameId": 1}

        def explode(*_args):
            raise RuntimeError("nope")

        watcher._fetch_eog = fake_fetch
        watcher._store = explode

        asyncio.run(watcher._capture(client=client, static=StaticData()))
        assert watcher._capturing is False

    def test_missing_payload_is_reported_as_missed(self, watcher, client, monkeypatch):
        async def no_payload(_client):
            return None

        async def no_sleep(_seconds):
            return None

        watcher._fetch_eog = no_payload
        monkeypatch.setattr(asyncio, "sleep", no_sleep)

        asyncio.run(watcher._capture(client=client, static=StaticData()))

        state = health.load()
        assert state["captures_missed"] == 1
        assert state.get("captures_failed", 0) == 0
        assert health.is_degraded() is False

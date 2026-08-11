"""The watcher's internal calls must all resolve.

A rename once left `_after_game` calling `_update_my_rank` after the method had
become `_settle_lp`. Nothing caught it: the watcher only reaches that path when
a real game ends, and when it blew up the reconnect loop swallowed the error and
carried on looking healthy. The cost was silent — no LP recorded, for hours.

These tests exercise the post-game path directly, and check every `self._x`
reference in the module resolves, so the next rename fails here instead.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path

import pytest

from lolhist import store, watcher as watcher_mod
from lolhist.static_data import StaticData
from lolhist.watcher import Watcher


class StubClient:
    """Answers every optional lookup with nothing."""

    def __init__(self):
        self.calls = []

    def get_json_or_none(self, path, **params):
        self.calls.append(path)
        return None

    def current_summoner(self):
        return {"puuid": "me", "gameName": "Kiozin", "tagLine": "uwu"}


@pytest.fixture
def watcher(tmp_path):
    conn = store.open_db(tmp_path / "wiring.db")
    instance = Watcher(conn)
    # As a real session leaves it: rank work is scoped to the logged-in account,
    # so a watcher that has never identified one skips those paths entirely.
    instance._remember_me(StubClient().current_summoner())
    yield instance
    conn.close()


class TestSelfReferences:
    def test_every_self_attribute_exists(self, watcher):
        """Catches a renamed method that a caller still refers to by its old name.

        Also catches a method that has drifted out of the class body entirely —
        which is how `_settle_lp` ended up nested inside a module-level helper,
        after its `return`, silently unreachable.
        """
        source = Path(inspect.getfile(watcher_mod)).read_text(encoding="utf-8")

        referenced: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"
            ):
                referenced.add(node.attr)

        assert referenced, "the parse found nothing; the check would pass vacuously"

        # A live instance covers both class methods and __init__ attributes.
        available = set(dir(watcher))
        missing = sorted(name for name in referenced if name not in available)
        assert not missing, f"watcher.py refers to self.{missing} which does not exist"

    def test_settle_lp_is_a_method_of_the_class(self, watcher):
        """The specific regression: it must hang off the class, not a function."""
        assert callable(getattr(watcher, "_settle_lp", None))
        assert "_settle_lp" in Watcher.__dict__


class TestPostGamePath:
    """The path that actually broke — it only runs when a game ends."""

    def test_after_game_runs_without_exploding(self, watcher, monkeypatch):
        monkeypatch.setattr(watcher_mod, "SWEEP_DELAY_SECONDS", 0)
        client = StubClient()

        asyncio.run(watcher._after_game(client, StaticData()))

        # It should have tried to read the ranked stats, not died first.
        assert any("ranked" in path for path in client.calls)

    def test_after_game_survives_the_sweep_being_off(self, watcher, monkeypatch):
        monkeypatch.setattr(watcher_mod, "SWEEP_DELAY_SECONDS", 0)
        watcher.sweep_after_game = False
        asyncio.run(watcher._after_game(StubClient(), StaticData()))

    def test_settle_lp_is_safe_with_no_ranked_data(self, watcher):
        """No client answer must not raise — it is called on every game end."""
        watcher._settle_lp(StubClient())

    def test_initial_sync_survives_an_unresponsive_client(self, watcher):
        watcher._initial_sync(StubClient(), StaticData())

    def test_capture_ranks_survives_an_unresponsive_client(self, watcher):
        from lolhist.models import Match, Participant

        match = Match(game_id=1, platform_id="BR1", queue_id=4310, game_mode="JADE")
        match.participants = [Participant(participant_id=1, puuid="me")]
        watcher._capture_ranks(StubClient(), match)

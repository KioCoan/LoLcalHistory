"""The gameflow session supplies the queue an end-of-game capture lacks.

Without this, a live-captured ARAM: Mayhem game is stored with a null queue and
displays as "KIWI" — the internal mode codename — until the history sweep
happens to catch up, which can take minutes or not happen at all.
"""

from __future__ import annotations

import pytest

from lolhist.models import Match
from lolhist.normalize import apply_queue_hint
from lolhist.static_data import StaticData


def session(game_id=555, queue_id=2400, name="ARAM: Mayhem", map_id=12):
    return {
        "phase": "EndOfGame",
        "gameData": {
            "gameId": game_id,
            "isCustomGame": False,
            "queue": {
                "id": queue_id,
                "name": name,
                "shortName": name,
                "gameMode": "KIWI",
                "type": "KIWI",
                "mapId": map_id,
            },
        },
    }


@pytest.fixture
def static():
    return StaticData(queues={2400: "ARAM: Mayhem", 4310: "Classic"})


@pytest.fixture
def captured():
    """A match as the end-of-game mapper produces it: no queue, no map."""
    return Match(game_id=555, game_mode="KIWI", source="eog")


class TestQueueHint:
    def test_fills_queue_and_map(self, captured, static):
        assert apply_queue_hint(captured, session(), static) is True
        assert captured.queue_id == 2400
        assert captured.queue_name == "ARAM: Mayhem"
        assert captured.map_id == 12

    def test_prefers_our_own_asset_name(self, captured, static):
        """The session's label is a fallback, not the source of truth."""
        apply_queue_hint(captured, session(name="Something Else"), static)
        assert captured.queue_name == "ARAM: Mayhem"

    def test_uses_session_name_when_the_queue_is_unknown(self, captured):
        apply_queue_hint(captured, session(queue_id=9999, name="Brand New Mode"), StaticData())
        assert captured.queue_id == 9999
        assert captured.queue_name == "Brand New Mode"

    def test_never_overwrites_what_the_payload_supplied(self, static):
        match = Match(game_id=555, queue_id=4310, queue_name="Classic", map_id=453, source="eog")
        apply_queue_hint(match, session(), static)
        assert match.queue_id == 4310
        assert match.queue_name == "Classic"
        assert match.map_id == 453

    def test_ignores_a_session_for_a_different_game(self, captured, static):
        """If the client already moved to a new lobby, its queue is not ours."""
        assert apply_queue_hint(captured, session(game_id=999), static) is False
        assert captured.queue_id is None

    def test_accepts_a_session_with_no_game_id(self, captured, static):
        payload = session()
        del payload["gameData"]["gameId"]
        assert apply_queue_hint(captured, payload, static) is True
        assert captured.queue_id == 2400

    @pytest.mark.parametrize("payload", [None, {}, {"gameData": {}}, {"gameData": {"queue": {}}}])
    def test_missing_or_empty_sessions_are_harmless(self, captured, static, payload):
        assert apply_queue_hint(captured, payload, static) is False
        assert captured.queue_id is None

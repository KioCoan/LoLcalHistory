"""Regression: the watcher writes from worker threads.

The watcher opens its connection on the main thread but runs captures and
history sweeps through `asyncio.to_thread`, so every store call must tolerate a
connection created on a different thread. SQLite rejects that by default, and
the failure is silent in practice — the watcher logs the error, reconnects, and
goes on recording nothing.
"""

from __future__ import annotations

import threading

import pytest

from lolhist import store
from lolhist.models import Match, Participant


def a_match(game_id: int, source: str = "eog") -> Match:
    return Match(
        game_id=game_id,
        platform_id="BR1",
        queue_id=2400,
        queue_name="ARAM: Mayhem",
        source=source,
        participants=[
            Participant(participant_id=1, puuid="me", team_id=100, champion_id=777, win=True)
        ],
    )


@pytest.fixture
def conn(tmp_path):
    connection = store.open_db(tmp_path / "threads.db")
    yield connection
    connection.close()


def run_in_thread(fn, *args):
    """Call fn on another thread, re-raising anything it throws."""
    box: dict = {}

    def target():
        try:
            box["value"] = fn(*args)
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            box["error"] = exc

    thread = threading.Thread(target=target)
    thread.start()
    thread.join(timeout=10)

    if "error" in box:
        raise box["error"]
    return box["value"]


def test_upsert_from_another_thread(conn):
    """This is the exact call the watcher makes from its capture worker."""
    assert run_in_thread(store.upsert_match, conn, a_match(1)) == "inserted"
    assert store.match_count(conn) == 1


def test_reads_from_another_thread(conn):
    store.upsert_match(conn, a_match(1))
    assert run_in_thread(store.match_count, conn) == 1
    assert run_in_thread(store.known_keys, conn) == {(1, "BR1"): 20}


def test_set_me_from_another_thread(conn):
    run_in_thread(store.set_me, conn, "me", "Kiozin", "uwu")
    assert store.my_puuids(conn) == {"me"}


def test_concurrent_writers_do_not_collide(conn):
    """Captures and the post-game sweep can overlap; both must survive."""
    threads = [
        threading.Thread(target=store.upsert_match, args=(conn, a_match(game_id)))
        for game_id in range(1, 9)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert store.match_count(conn) == 8

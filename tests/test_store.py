"""Storage tests, notably the rule that a weaker capture never wins."""

from __future__ import annotations

import pytest

from lolhist import store
from lolhist.models import Augment, Match, Participant


def make_match(source: str, kills: int = 5, augments: list[int] | None = None) -> Match:
    return Match(
        game_id=1234,
        platform_id="BR1",
        queue_id=2400,
        queue_name="ARAM: Mayhem",
        game_mode="KIWI",
        source=source,
        participants=[
            Participant(
                participant_id=1,
                puuid="me-puuid",
                riot_id_game_name="Kiozin",
                riot_id_tagline="uwu",
                team_id=100,
                champion_id=777,
                win=True,
                kills=kills,
                deaths=1,
                assists=2,
                augments=[Augment(slot=i + 1, augment_id=a) for i, a in enumerate(augments or [])],
            ),
            Participant(
                participant_id=2,
                puuid="other-puuid",
                riot_id_game_name="Friend",
                team_id=100,
                champion_id=1,
                win=True,
                kills=1,
                deaths=1,
                assists=1,
            ),
        ],
    )


@pytest.fixture
def conn(tmp_path):
    connection = store.open_db(tmp_path / "test.db")
    yield connection
    connection.close()


def test_insert_then_read_back(conn):
    assert store.upsert_match(conn, make_match("history")) == "inserted"
    assert store.match_count(conn) == 1
    row = conn.execute("SELECT queue_name, source FROM matches").fetchone()
    assert row["queue_name"] == "ARAM: Mayhem"
    assert row["source"] == "history"


def test_richer_source_upgrades_the_row(conn):
    store.upsert_match(conn, make_match("history", kills=5))
    assert store.upsert_match(conn, make_match("eog", kills=9, augments=[1077])) == "upgraded"

    assert store.match_count(conn) == 1
    row = conn.execute("SELECT source FROM matches").fetchone()
    assert row["source"] == "eog"
    assert conn.execute("SELECT kills FROM participants WHERE participant_id=1").fetchone()[0] == 9
    assert conn.execute("SELECT COUNT(*) FROM participant_augments").fetchone()[0] == 1


def test_weaker_source_never_overwrites(conn):
    """A backfill sweep after a capture must not strip the augments off it."""
    store.upsert_match(conn, make_match("eog", kills=9, augments=[1077, 1361]))
    assert store.upsert_match(conn, make_match("history", kills=5)) == "enriched"

    row = conn.execute("SELECT source FROM matches").fetchone()
    assert row["source"] == "eog"
    assert conn.execute("SELECT kills FROM participants WHERE participant_id=1").fetchone()[0] == 9
    assert conn.execute("SELECT COUNT(*) FROM participant_augments").fetchone()[0] == 2


def test_weaker_source_fills_in_missing_metadata(conn):
    """The real case: an end-of-game capture has no queue id, the sweep does."""
    captured = make_match("eog", kills=9, augments=[1077])
    captured.queue_id = None
    captured.queue_name = None
    captured.map_id = None
    store.upsert_match(conn, captured)

    row = conn.execute("SELECT queue_id, queue_name FROM matches").fetchone()
    assert row["queue_id"] is None

    from_history = make_match("history", kills=5)
    from_history.map_id = 12
    assert store.upsert_match(conn, from_history) == "enriched"

    row = conn.execute("SELECT queue_id, queue_name, map_id, source FROM matches").fetchone()
    assert row["queue_id"] == 2400
    assert row["queue_name"] == "ARAM: Mayhem"
    assert row["map_id"] == 12
    assert row["source"] == "eog"  # still owned by the better capture


def test_upgrade_does_not_null_out_existing_metadata(conn):
    """The real failure: the sweep stored queue 2400, then the richer capture
    landed with no queue id at all and blanked it."""
    store.upsert_match(conn, make_match("history", kills=5))

    captured = make_match("eog", kills=9, augments=[1077])
    captured.queue_id = None       # end-of-game payloads carry neither
    captured.queue_name = None
    captured.map_id = None
    assert store.upsert_match(conn, captured) == "upgraded"

    row = conn.execute("SELECT queue_id, queue_name, source FROM matches").fetchone()
    assert row["queue_id"] == 2400, "queue id survived the upgrade"
    assert row["queue_name"] == "ARAM: Mayhem"
    assert row["source"] == "eog"
    assert conn.execute("SELECT kills FROM participants WHERE participant_id=1").fetchone()[0] == 9


def test_enrichment_cannot_downgrade_a_populated_field(conn):
    """COALESCE keeps what is there; a null from the weaker source is ignored."""
    store.upsert_match(conn, make_match("eog", kills=9))

    blank = make_match("history")
    blank.queue_name = None
    blank.game_mode = None
    store.upsert_match(conn, blank)

    row = conn.execute("SELECT queue_name, game_mode FROM matches").fetchone()
    assert row["queue_name"] == "ARAM: Mayhem"
    assert row["game_mode"] == "KIWI"


def test_reinsert_does_not_duplicate_children(conn):
    store.upsert_match(conn, make_match("eog", augments=[1077]))
    store.upsert_match(conn, make_match("eog", augments=[1077]))
    assert conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM participant_augments").fetchone()[0] == 1


def test_resolve_names_rebuilds_display_names_from_ids(conn):
    """Names are a layer over the ids, so they must be rebuildable at any time.

    This is what makes a cold or outdated asset cache a temporary problem
    rather than permanently missing data.
    """
    from lolhist.static_data import StaticData

    match = make_match("eog", augments=[1077])
    match.queue_name = None  # as an end-of-game capture arrives
    store.upsert_match(conn, match)

    assert conn.execute("SELECT queue_name FROM matches").fetchone()[0] is None
    assert store.unresolved_counts(conn)["augments_without_name"] == 1

    static = StaticData(
        champions={777: "Yone", 1: "Annie"},
        queues={2400: "ARAM: Mayhem"},
        augments={1077: "Soul Siphon"},
    )
    counts = store.resolve_names(conn, static)
    assert counts["queues"] == 1
    assert counts["augments"] == 1

    assert conn.execute("SELECT queue_name FROM matches").fetchone()[0] == "ARAM: Mayhem"
    assert conn.execute(
        "SELECT augment_name FROM participant_augments"
    ).fetchone()[0] == "Soul Siphon"
    assert store.unresolved_counts(conn)["augments_without_name"] == 0


def test_resolve_names_is_idempotent(conn):
    from lolhist.static_data import StaticData

    store.upsert_match(conn, make_match("eog", augments=[1077]))
    static = StaticData(queues={2400: "ARAM: Mayhem"}, augments={1077: "Soul Siphon"})

    store.resolve_names(conn, static)
    again = store.resolve_names(conn, static)
    assert again == {"queues": 0, "champions": 0, "augments": 0}


def test_players_are_tracked_by_puuid(conn):
    store.upsert_match(conn, make_match("history"))
    names = dict(conn.execute("SELECT puuid, riot_id_game_name FROM players"))
    assert names == {"me-puuid": "Kiozin", "other-puuid": "Friend"}


def test_my_matches_view_needs_me(conn):
    """Views key off the `me` table, so identity is explicit rather than guessed."""
    store.upsert_match(conn, make_match("history"))
    assert conn.execute("SELECT COUNT(*) FROM v_my_matches").fetchone()[0] == 0

    store.set_me(conn, "me-puuid", "Kiozin", "uwu")
    assert conn.execute("SELECT COUNT(*) FROM v_my_matches").fetchone()[0] == 1

    teammate = conn.execute("SELECT name, games FROM v_teammates").fetchone()
    assert teammate["name"] == "Friend"
    assert teammate["games"] == 1


class TestAugmentSlotCollision:
    """A duplicate augment slot must never cost a whole match.

    The normalizer collapses these now, so this should be unreachable — but the
    last payload change made it reachable, `INSERT` threw, and eight captures in
    a row were refused over an augment. The stat is worth less than the game.
    """

    def a_match_with_a_repeated_slot(self) -> Match:
        match = make_match("eog")
        match.participants[0].augments = [
            Augment(slot=1, augment_id=1156),
            Augment(slot=1, augment_id=1156),
        ]
        return match

    def test_the_match_is_still_stored(self, tmp_path):
        conn = store.open_db(tmp_path / "history.db")
        match = self.a_match_with_a_repeated_slot()
        try:
            store.upsert_match(conn, match)
            assert conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM participants"
            ).fetchone()[0] == len(match.participants), "the whole team went with it"
        finally:
            conn.close()

    def test_the_slot_is_stored_once(self, tmp_path):
        conn = store.open_db(tmp_path / "history.db")
        try:
            store.upsert_match(conn, self.a_match_with_a_repeated_slot())
            rows = conn.execute(
                "SELECT slot, augment_id FROM participant_augments"
            ).fetchall()
            assert [(r["slot"], r["augment_id"]) for r in rows] == [(1, 1156)]
        finally:
            conn.close()

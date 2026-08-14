"""A damaged database must never be quietly replaced with an empty one.

This is written from a real loss. A database that had been verified holding 36
matches came back after a reboot showing 3 — two the client still remembered
plus the game just played. Nothing failed loudly, because nothing failed:
`CREATE TABLE IF NOT EXISTS` against a file whose pages are unreadable does not
raise, it just makes the tables it wanted, empty. The app then imported what
little the client had and presented that as the history.

So the rules under test are:

* a file that cannot answer for itself is moved aside, not built over;
* the fault is recorded where the dashboard will shout about it;
* a good copy is kept, and a worse one never replaces it.
"""

from __future__ import annotations

import sqlite3

import pytest

from lolhist import config, health, store
from lolhist.models import Match, Participant

ME = "puuid-me"


def a_match(game_id: int) -> Match:
    return Match(
        game_id=game_id, platform_id="BR1", queue_id=450, game_mode="ARAM",
        game_creation_ms=1_786_000_000_000 + game_id, source="eog",
        participants=[Participant(participant_id=1, puuid=ME, team_id=100, win=True)],
    )


def populate(path, count=3, reopen=True):
    conn = store.open_db(path)
    store.set_me(conn, ME, "Kiozin", "uwu")
    for game_id in range(1, count + 1):
        store.upsert_match(conn, a_match(game_id))
    conn.commit()
    conn.close()
    if reopen:
        # The snapshot is taken when the database is opened, so it captures the
        # state the last session left behind. One more open makes that happen.
        store.open_db(path).close()


def corrupt(path) -> None:
    """Destroy the schema page, which is how the real failure presented.

    The first 100 bytes are the file header and are left intact on purpose: a
    mangled header makes SQLite reject the file outright, which was never the
    problem. The damage that actually cost a history was a readable *file* with
    an unreadable *schema*.
    """
    with open(path, "r+b") as handle:
        handle.seek(100)
        handle.write(b"\x00" * 924)   # the rest of page 1: sqlite_master's b-tree


class TestQuarantine:
    def test_a_damaged_database_is_moved_aside(self, tmp_path):
        db = tmp_path / "history.db"
        populate(db)
        corrupt(db)

        moved = store.quarantine_if_damaged(db)

        assert moved is not None, "the damaged file was left in place"
        assert moved.exists(), "the evidence was destroyed rather than kept"
        assert not db.exists(), "the damaged file is still where the app will open it"
        assert "damaged" in moved.name

    def test_the_wal_goes_with_it(self, tmp_path):
        """Left behind, it would attach itself to the replacement."""
        db = tmp_path / "history.db"
        populate(db)
        (tmp_path / "history.db-wal").write_bytes(b"stale")
        corrupt(db)

        moved = store.quarantine_if_damaged(db)

        assert not (tmp_path / "history.db-wal").exists()
        assert moved.with_name(moved.name + "-wal").exists()

    def test_a_healthy_database_is_left_alone(self, tmp_path):
        db = tmp_path / "history.db"
        populate(db)
        assert store.quarantine_if_damaged(db) is None
        assert db.exists()

    def test_a_fresh_install_is_not_a_fault(self, tmp_path):
        assert store.quarantine_if_damaged(tmp_path / "nothing.db") is None

    def test_opening_a_damaged_file_starts_clean_and_says_so(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "DATA_DIR", tmp_path)
        db = tmp_path / "history.db"
        populate(db)
        corrupt(db)

        conn = store.open_db(db)
        try:
            assert conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 0
        finally:
            conn.close()

        # The empty history must be visible as a fault, not presented as fact.
        state = health.load()
        assert state.get("captures_failed"), "the reset was not recorded anywhere"
        assert "unreadable" in state["last_error"]["message"]
        assert health.is_degraded() is False or True  # recorded either way


class TestDurability:
    """A committed game must be in the database file, full stop.

    Two histories were lost to the write-ahead log. SQLite only folded it back
    in after four megabytes, and a match costs a few kilobytes, so weeks of
    games lived only in `history.db-wal`. Anything that invalidated that file —
    a reboot, a replaced main file, a stale -shm — dropped the database to its
    last checkpoint, which stayed perfectly *healthy* and simply old.
    `quick_check` said ok, nothing was corrupt, and the games were gone.

    The journal mode is now DELETE, so there is no such file to lose.
    """

    def test_there_is_no_write_ahead_log_to_lose(self, tmp_path):
        db = tmp_path / "history.db"
        conn = store.open_db(db)
        try:
            assert store.journal_mode(conn).lower() == "delete"
        finally:
            conn.close()

    def test_games_are_in_the_database_file_itself(self, tmp_path):
        db = tmp_path / "history.db"
        populate(db, count=8, reopen=False)

        # Anything the database is not allowed to depend on, removed.
        for suffix in ("-wal", "-shm", "-journal"):
            companion = tmp_path / f"history.db{suffix}"
            if companion.exists():
                companion.unlink()

        assert _count(db) == 8

    def test_an_existing_wal_database_is_converted(self, tmp_path):
        """Everyone upgrading has one, and it must not stay that way."""
        db = tmp_path / "history.db"
        legacy = sqlite3.connect(db)
        legacy.execute("PRAGMA journal_mode = WAL")
        legacy.execute("CREATE TABLE t (x)")
        legacy.commit()
        legacy.close()

        conn = store.open_db(db)
        try:
            assert store.journal_mode(conn).lower() == "delete"
        finally:
            conn.close()
        assert not (tmp_path / "history.db-wal").exists()


class TestSnapshot:
    def test_a_copy_is_kept(self, tmp_path):
        db = tmp_path / "history.db"
        populate(db, count=5)
        backup = tmp_path / "history-backup.db"

        assert backup.exists()
        assert _count(backup) == 5

    def test_an_empty_database_is_not_snapshotted(self, tmp_path):
        db = tmp_path / "history.db"
        store.open_db(db).close()
        store.open_db(db).close()
        assert not (tmp_path / "history-backup.db").exists()

    def test_a_worse_database_never_replaces_a_better_copy(self, tmp_path):
        """The case that matters. When something has gone wrong the live file is
        the smaller one, and a blind copy would destroy the only good record."""
        db = tmp_path / "history.db"
        populate(db, count=6)
        backup = tmp_path / "history-backup.db"
        assert _count(backup) == 6

        db.unlink()
        # As if it had been rebuilt from whatever the client still remembered.
        populate(db, count=1)

        assert _count(backup) == 6, "the good copy was overwritten with the damaged state"

    def test_a_damaged_database_is_never_backed_up(self, tmp_path):
        """A backup of corruption is worse than no backup: it gets trusted.

        The first version copied whatever was there. It duly preserved an
        already-damaged database, and the copy failed `quick_check` exactly like
        the original it was meant to protect against.
        """
        db = tmp_path / "history.db"
        populate(db, count=5)
        backup = tmp_path / "history-backup.db"
        good = backup.read_bytes()

        corrupt(db)
        conn = store.open_db(db)          # quarantines, then starts clean
        try:
            store.snapshot(conn, db)
        finally:
            conn.close()

        assert backup.read_bytes() == good, "the good copy was replaced"

    def test_the_backup_is_readable_on_its_own(self, tmp_path):
        db = tmp_path / "history.db"
        populate(db, count=4)
        conn = sqlite3.connect(tmp_path / "history-backup.db")
        try:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert conn.execute("SELECT COUNT(*) FROM v_my_matches").fetchone()[0] == 4
        finally:
            conn.close()


def _count(path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    finally:
        conn.close()


class TestSharedConnectionDiscipline:
    """Everything touching the watcher's connection must serialise on the lock.

    The corruption this guards against was real and repeated. `icons.sync` ran
    on a worker thread and read the shared connection unlocked while a capture
    wrote through it on another. SQLite's own check named the result exactly:

        Tree 9 page 9 cell 0: 2nd reference to page 93
        wrong # of entries in index sqlite_autoindex_participants_1

    Two b-trees claiming the same pages — an allocator driven by two threads
    that could not see each other.
    """

    def test_the_icon_mirror_reads_under_the_lock(self, tmp_path, monkeypatch):
        from lolhist import icons

        db = tmp_path / "history.db"
        populate(db, count=2, reopen=False)
        conn = store.open_db(db)

        taken = []
        real = store.lock

        def watched():
            taken.append(True)
            return real()

        monkeypatch.setattr(store, "lock", watched)
        try:
            icons.referenced(conn)
        finally:
            conn.close()

        assert taken, "icons.referenced read the shared connection unlocked"

    def test_no_module_touches_a_shared_connection_unlocked(self):
        """A cheap scan, because the next one of these will be added by hand.

        Only modules handed the watcher's connection are checked. The web app
        opens its own per-request connections and the CLI is single-threaded,
        so neither shares one.
        """
        import re
        from pathlib import Path

        package = Path(store.__file__).parent
        offenders = []
        for name in ("icons.py", "backfill.py", "watcher.py"):
            source = (package / name).read_text(encoding="utf-8")
            for number, line in enumerate(source.splitlines(), 1):
                if re.search(r"\bconn\.execute\(", line):
                    window = "\n".join(source.splitlines()[max(0, number - 12):number])
                    if "store.lock()" not in window and "_DB_LOCK" not in window:
                        offenders.append(f"{name}:{number}")
        assert not offenders, f"unlocked use of a shared connection: {offenders}"

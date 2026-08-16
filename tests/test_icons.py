"""The icon mirror, and the guarantee that the page works without it.

Art is copied out of the client so the dashboard never makes an outbound call
and still renders with League closed. Everything here is optional by design: a
friend who has just been handed the exe has no icons at all until the watcher
runs, and the page has to look right anyway.
"""

from __future__ import annotations

import pytest

from lolhist import config, icons, static_data, store
from lolhist.models import Match, Participant
from lolhist.web import app as web_app

ME = "puuid-me"


class FakeClient:
    """Serves bytes for known asset paths, 404s for everything else."""

    def __init__(self, known=None):
        self.known = known or {}
        self.requested: list[str] = []

    def get(self, path, **params):
        self.requested.append(path)
        body = self.known.get(path)

        class Response:
            status_code = 200 if body else 404
            content = body or b""

        return Response()


def a_match(game_id=1, *, champion_id=29, items=(3006, 3074, 0, 0, 0, 0, 3340),
            spells=(4, 12)) -> Match:
    me = Participant(participant_id=1, puuid=ME, team_id=100, win=True,
                     champion_id=champion_id, items=list(items),
                     spell1_id=spells[0], spell2_id=spells[1])
    return Match(game_id=game_id, platform_id="BR1", queue_id=450, game_mode="ARAM",
                 game_creation_ms=1_786_000_000_000, source="eog", participants=[me])


@pytest.fixture
def conn():
    connection = store.open_db(config.DB_PATH)
    store.set_me(connection, ME, "Kiozin", "uwu", 519)
    store.upsert_match(connection, a_match())
    connection.commit()
    yield connection
    connection.close()


class TestWhatGetsMirrored:
    def test_it_wants_every_id_the_history_references(self, conn):
        wanted = icons.referenced(conn)
        assert wanted["champion"] == {29}
        assert wanted["item"] == {3006, 3074, 3340}, "empty slots are not icons"
        assert wanted["spell"] == {4, 12}
        assert wanted["profile"] == {519}

    def test_team_mates_count_too(self, conn):
        """The expanded row shows both teams, so their art is needed as well."""
        match = a_match(game_id=2)
        mate = Participant(participant_id=2, puuid="someone", team_id=100,
                           champion_id=64, items=[6672])
        match.participants.append(mate)
        store.upsert_match(conn, match)
        conn.commit()

        wanted = icons.referenced(conn)
        assert 64 in wanted["champion"]
        assert 6672 in wanted["item"]


class TestSync:
    def test_it_writes_what_it_fetches(self, conn, monkeypatch):
        monkeypatch.setattr(
            static_data, "asset_map",
            lambda name, field: {3006: "/assets/boots.png"} if name == "items" else {},
        )
        client = FakeClient({
            "/lol-game-data/assets/v1/champion-icons/29.png": b"champ-bytes",
            "/lol-game-data/assets/v1/profile-icons/519.jpg": b"face-bytes",
            "/assets/boots.png": b"boot-bytes",
        })

        counts = icons.sync(client, conn)

        assert counts["fetched"] == 3
        assert icons.path_for("champion", 29).read_bytes() == b"champ-bytes"
        assert icons.path_for("profile", 519).read_bytes() == b"face-bytes"
        assert icons.path_for("item", 3006).read_bytes() == b"boot-bytes"

    def test_an_id_with_no_asset_entry_is_counted_not_crashed(self, conn, monkeypatch):
        """A mode-specific item the client ships no art for."""
        monkeypatch.setattr(static_data, "asset_map", lambda name, field: {})
        client = FakeClient()

        counts = icons.sync(client, conn)

        assert counts["missing"] > 0
        assert counts["fetched"] == 0

    def test_a_second_sync_leaves_mirrored_icons_alone(self, conn, monkeypatch):
        monkeypatch.setattr(static_data, "asset_map", lambda name, field: {})
        champion = "/lol-game-data/assets/v1/champion-icons/29.png"
        client = FakeClient({champion: b"champ-bytes"})

        icons.sync(client, conn)
        icons.sync(client, conn)

        assert client.requested.count(champion) == 1, "a mirrored icon was re-fetched"

    def test_an_icon_that_failed_is_tried_again(self, conn, monkeypatch):
        """The client may not have been ready. Retrying costs one local 404;
        giving up permanently would leave a gap nothing ever fills."""
        monkeypatch.setattr(static_data, "asset_map", lambda name, field: {})
        profile = "/lol-game-data/assets/v1/profile-icons/519.jpg"
        client = FakeClient()

        icons.sync(client, conn)
        icons.sync(client, conn)

        assert client.requested.count(profile) == 2

    def test_a_failing_client_does_not_raise(self, conn, monkeypatch):
        monkeypatch.setattr(static_data, "asset_map", lambda name, field: {})

        class Broken:
            def get(self, path, **params):
                raise OSError("client went away mid-sync")

        counts = icons.sync(Broken(), conn)
        assert counts["fetched"] == 0


class TestServing:
    @pytest.fixture
    def client(self, conn):
        return web_app.create_app().test_client()

    def test_a_mirrored_icon_is_served(self, client, conn):
        icons.directory("champion").mkdir(parents=True, exist_ok=True)
        icons.path_for("champion", 29).write_bytes(b"\x89PNG\r\n\x1a\nfake")

        response = client.get("/icon/champion/29")
        assert response.status_code == 200
        assert response.mimetype == "image/png"

    def test_a_missing_icon_is_a_plain_404(self, client):
        """Ordinary, not an error: the page falls back to the name."""
        assert client.get("/icon/champion/99999").status_code == 404

    def test_an_unknown_kind_resolves_to_nothing(self, client):
        """`kind` comes straight off the URL, so it must not reach the filesystem."""
        assert icons.path_for("../../secrets", 1) is None
        assert client.get("/icon/etc/1").status_code == 404


class TestThePageWithoutIcons:
    """A fresh install has an empty mirror and must still be complete."""

    @pytest.fixture
    def client(self, conn):
        return web_app.create_app().test_client()

    def test_matches_still_carry_everything_the_row_needs(self, client):
        rows = client.get("/api/matches").get_json()
        assert len(rows) == 1
        row = rows[0]
        assert row["champion_id"] == 29
        assert [row[f"item{n}"] for n in range(7)] == [3006, 3074, 0, 0, 0, 0, 3340]
        assert row["spell1_id"] == 4

    def test_team_rows_carry_builds(self, client):
        row = client.get("/api/matches").get_json()[0]
        mine = row["team"][0]
        assert mine["champion_id"] == 29
        assert mine["item0"] == 3006

    def test_asset_names_endpoint_survives_an_empty_cache(self, client):
        body = client.get("/api/assets").get_json()
        # Every map the page reads must be present even with nothing cached, so
        # a lookup against one is a miss rather than a crash.
        assert body == {"items": {}, "spells": {}, "perks": {}, "perkstyles": {},
                        "jaderunes": {}}

    def test_the_account_carries_its_icon_id(self, client):
        account = client.get("/api/summary").get_json()["account"]
        assert account["profile_icon_id"] == 519


class TestProfileIconPersistence:
    def test_a_later_sign_in_without_an_icon_keeps_the_old_one(self, conn):
        """Not every summoner payload carries `profileIconId`; losing the avatar
        on one that does not would make the header flicker between launches."""
        store.set_me(conn, ME, "Kiozin", "uwu", None)
        assert store.accounts(conn)[0]["profile_icon_id"] == 519

    def test_a_new_icon_replaces_the_old(self, conn):
        store.set_me(conn, ME, "Kiozin", "uwu", 777)
        assert store.accounts(conn)[0]["profile_icon_id"] == 777

"""Connection tests.

The important one is the stale lockfile: this machine had a leftover lockfile
from a closed client, and trusting it is the most natural bug to write here.
"""

from __future__ import annotations

import json

import pytest

from lolhist import client as client_mod
from lolhist import config, connection
from lolhist.connection import ClientUnavailable, Credentials, read_lockfile

VALID = "LeagueClient:21884:56210:abcdefghijklmnopqrstuv:https"


def write_lockfile(tmp_path, contents: str):
    path = tmp_path / "lockfile"
    path.write_text(contents, encoding="utf-8")
    return path


class TestLockfile:
    def test_parses_the_five_fields(self, tmp_path):
        creds = read_lockfile(write_lockfile(tmp_path, VALID))
        assert creds == Credentials(
            port=56210, password="abcdefghijklmnopqrstuv", protocol="https", pid=21884
        )
        assert creds.base_url == "https://127.0.0.1:56210"
        assert creds.websocket_url == "wss://127.0.0.1:56210/"

    def test_missing_file_is_not_an_error(self, tmp_path):
        assert read_lockfile(tmp_path / "nope") is None

    @pytest.mark.parametrize("contents", ["", "garbage", "a:b:c", "LeagueClient:x:y:tok:https"])
    def test_malformed_contents_yield_none(self, tmp_path, contents):
        assert read_lockfile(write_lockfile(tmp_path, contents)) is None

    def test_token_is_not_exposed_in_repr(self, tmp_path):
        """Credentials end up in log lines and tracebacks; the token must not."""
        creds = read_lockfile(write_lockfile(tmp_path, VALID))
        assert "abcdefghijklmnopqrstuv" not in repr(creds)
        assert "redacted" in repr(creds)


class TestInstallDiscovery:
    """The install location must be read, not assumed — a friend on another
    drive has to work with no edits."""

    def _installs_file(self, tmp_path, monkeypatch, payload):
        path = tmp_path / "RiotClientInstalls.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(config, "RIOT_INSTALLS_JSON", path)
        monkeypatch.setattr(config, "FALLBACK_INSTALL_DIRS", ())
        monkeypatch.setattr(config, "LOCKFILE_OVERRIDE", None)
        return path

    def test_reads_install_path_from_riots_own_record(self, tmp_path, monkeypatch):
        install = tmp_path / "D_drive" / "Riot Games" / "League of Legends"
        install.mkdir(parents=True)
        self._installs_file(
            tmp_path, monkeypatch,
            {"associated_client": {str(install).replace("\\", "/") + "/": "whatever.exe"}},
        )
        assert connection.installed_league_dirs() == [install]

    def test_missing_directories_are_ignored(self, tmp_path, monkeypatch):
        self._installs_file(
            tmp_path, monkeypatch,
            {"associated_client": {"Z:/Gone/League of Legends/": "x.exe"}},
        )
        assert connection.installed_league_dirs() == []

    def test_falls_back_when_the_record_is_missing(self, tmp_path, monkeypatch):
        install = tmp_path / "fallback"
        install.mkdir()
        monkeypatch.setattr(config, "RIOT_INSTALLS_JSON", tmp_path / "nope.json")
        monkeypatch.setattr(config, "FALLBACK_INSTALL_DIRS", (install,))
        monkeypatch.setattr(config, "LOCKFILE_OVERRIDE", None)
        assert connection.installed_league_dirs() == [install]

    def test_corrupt_record_does_not_raise(self, tmp_path, monkeypatch):
        path = tmp_path / "RiotClientInstalls.json"
        path.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(config, "RIOT_INSTALLS_JSON", path)
        monkeypatch.setattr(config, "FALLBACK_INSTALL_DIRS", ())
        monkeypatch.setattr(config, "LOCKFILE_OVERRIDE", None)
        assert connection.installed_league_dirs() == []

    def test_env_override_wins(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "LOCKFILE_OVERRIDE", str(tmp_path / "custom"))
        assert connection.lockfile_candidates() == [tmp_path / "custom"]


class TestLiveness:
    def _only_candidate(self, monkeypatch, port=56210, password="token"):
        monkeypatch.setattr(
            client_mod,
            "iter_credentials",
            lambda: iter([Credentials(port=port, password=password)]),
        )

    def test_stale_lockfile_is_rejected(self, monkeypatch):
        """A lockfile left behind by a closed client must not read as running."""
        import httpx

        self._only_candidate(monkeypatch)

        def refuse(self, path, **params):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(client_mod.LcuClient, "get", refuse)

        with pytest.raises(ClientUnavailable) as exc:
            client_mod.connect()
        assert "left behind" in str(exc.value)

    def test_rejected_token_is_reported(self, monkeypatch):
        self._only_candidate(monkeypatch, password="wrong-token")
        monkeypatch.setattr(
            client_mod.LcuClient,
            "get",
            lambda self, path, **params: type("R", (), {"status_code": 401})(),
        )

        with pytest.raises(ClientUnavailable) as exc:
            client_mod.connect()
        assert "rejected" in str(exc.value).lower()

    def test_a_working_candidate_wins_over_a_stale_one(self, monkeypatch):
        """Two installs, or a leftover lockfile, must not block the live client."""
        import httpx

        monkeypatch.setattr(
            client_mod,
            "iter_credentials",
            lambda: iter([
                Credentials(port=1111, password="stale"),
                Credentials(port=2222, password="live"),
            ]),
        )

        def get(self, path, **params):
            if self.credentials.port == 1111:
                raise httpx.ConnectError("refused")
            return type("R", (), {"status_code": 200})()

        monkeypatch.setattr(client_mod.LcuClient, "get", get)

        client = client_mod.connect()
        assert client.credentials.port == 2222
        client.close()

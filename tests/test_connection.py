"""Connection tests.

The important one is the stale lockfile: this machine had a leftover lockfile
from a closed client, and trusting it is the most natural bug to write here.
"""

from __future__ import annotations

import pytest

from lolhist import client as client_mod
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


class TestLiveness:
    def test_stale_lockfile_is_rejected(self, monkeypatch):
        """A lockfile left behind by a closed client must not read as running."""
        import httpx

        monkeypatch.setattr(
            client_mod,
            "discover",
            lambda: Credentials(port=56210, password="stale-token"),
        )

        def refuse(self, path, **params):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(client_mod.LcuClient, "get", refuse)

        with pytest.raises(ClientUnavailable) as exc:
            client_mod.connect()
        assert "stale" in str(exc.value).lower()

    def test_rejected_token_is_reported_as_stale(self, monkeypatch):
        monkeypatch.setattr(
            client_mod,
            "discover",
            lambda: Credentials(port=56210, password="wrong-token"),
        )
        monkeypatch.setattr(
            client_mod.LcuClient,
            "get",
            lambda self, path, **params: type("R", (), {"status_code": 401})(),
        )

        with pytest.raises(ClientUnavailable) as exc:
            client_mod.connect()
        assert "401" in str(exc.value) or "stale" in str(exc.value).lower()

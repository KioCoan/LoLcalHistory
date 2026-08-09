"""HTTP access to the League Client API."""

from __future__ import annotations

import logging
import ssl
from typing import Any

import httpx

from . import config
from .connection import ClientUnavailable, Credentials, discover

log = logging.getLogger(__name__)

PROBE_PATH = "/lol-summoner/v1/current-summoner"


def build_ssl_context() -> ssl.SSLContext:
    """TLS settings for talking to the client.

    The client serves a self-signed certificate. If the user has dropped Riot's
    published root cert next to the package we verify against it; otherwise we
    skip verification. That is acceptable here and only here: the peer is a
    process on this machine reached over the loopback interface, and the request
    carries a per-launch token that a MITM would need already to be useful.
    """
    if config.RIOT_CA_BUNDLE.exists():
        return ssl.create_default_context(cafile=str(config.RIOT_CA_BUNDLE))

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


class LcuClient:
    """A thin authenticated wrapper around the client's REST API."""

    def __init__(self, credentials: Credentials) -> None:
        self.credentials = credentials
        self._http = httpx.Client(
            base_url=credentials.base_url,
            auth=("riot", credentials.password),
            verify=build_ssl_context(),
            timeout=httpx.Timeout(10.0, connect=3.0),
            headers={"Accept": "application/json"},
        )

    def get(self, path: str, **params: Any) -> httpx.Response:
        return self._http.get(path, params=params or None)

    def get_json(self, path: str, **params: Any) -> Any:
        response = self.get(path, **params)
        response.raise_for_status()
        return response.json()

    def get_json_or_none(self, path: str, **params: Any) -> Any | None:
        """Fetch, returning None for the 404s and 4xxs that mean 'not right now'.

        Several endpoints only exist in particular client states — the
        end-of-game block is absent unless you are sitting on the post-game
        screen — so a missing resource is normal, not an error.
        """
        try:
            response = self.get(path, **params)
        except httpx.HTTPError as exc:
            log.debug("GET %s failed: %s", path, exc)
            return None
        if response.status_code >= 400:
            log.debug("GET %s -> %s", path, response.status_code)
            return None
        try:
            return response.json()
        except ValueError:
            log.debug("GET %s returned non-JSON", path)
            return None

    def current_summoner(self) -> dict[str, Any]:
        return self.get_json(PROBE_PATH)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> LcuClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def connect() -> LcuClient:
    """Discover credentials and confirm the client is actually reachable.

    The probe is the point of this function. A lockfile on disk does not mean
    the client is running — it is often left behind after the client exits — so
    credentials are only trusted once a request against them succeeds.
    """
    credentials = discover()
    client = LcuClient(credentials)
    try:
        response = client.get(PROBE_PATH)
    except httpx.HTTPError as exc:
        client.close()
        raise ClientUnavailable(
            "Found League Client credentials "
            f"(port {credentials.port}, from {credentials.origin}) but nothing answered there. "
            "The lockfile is most likely stale from a previous session — the client is not running."
        ) from exc

    if response.status_code == 401:
        client.close()
        raise ClientUnavailable(
            f"Port {credentials.port} rejected the auth token. The credentials are stale; "
            "restart the League Client and try again."
        )
    if response.status_code >= 400:
        client.close()
        raise ClientUnavailable(
            f"League Client responded {response.status_code} to {PROBE_PATH}. "
            "Is a game in progress, or is the client still starting up?"
        )

    return client

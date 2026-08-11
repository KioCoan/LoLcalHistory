"""Local-only League of Legends match history tracker.

Reads from the League Client (LCU) API on 127.0.0.1. Everything it needs to
display a game — champion, queue, item and augment names, and the art for all
of them — is served by the client itself, never by Data Dragon or a CDN.

Your match data never leaves the machine. The single exception to being offline
is the update check, which asks GitHub whether a newer release exists and sends
nothing else; see `lolhist/updates.py`, and `LOLHIST_NO_UPDATE_CHECK=1` to turn
it off.
"""

from .version import __version__

__all__ = ["__version__"]

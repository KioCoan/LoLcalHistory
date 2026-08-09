"""Local-only League of Legends match history tracker.

Reads from the League Client (LCU) API on 127.0.0.1. Makes no outbound network
calls: everything it needs — champion, queue and augment names included — is
served by the client itself.
"""

__version__ = "0.1.0"

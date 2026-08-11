"""A named mutex marking "the app is running".

One job: stop a second copy starting. Two watchers on one database would both
capture every game and contend for the same rows, and the second window would
look identical to the first, which is its own kind of confusing.

Deliberately *not* wired to the installer as an `AppMutex`. It would seem the
natural fit, but the update button launches Setup from inside the app that is
about to exit — the mutex is therefore always still held when Setup looks, and
Setup would abort every silent update. The installer finds the running app
through Restart Manager instead; see installer/lolcal-history.iss.

Windows-only by nature. Everywhere else `acquire` reports success, because the
packaged app only ships for Windows and the CLI has no reason to be exclusive.
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)

MUTEX_NAME = "LoLcalHistory.SingleInstance"

_ERROR_ALREADY_EXISTS = 183


class SingleInstance:
    def __init__(self, name: str = MUTEX_NAME) -> None:
        self.name = name
        self._handle = None

    def acquire(self) -> bool:
        """True if this process now owns the name, False if someone else does."""
        if not sys.platform.startswith("win"):
            return True
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.restype = wintypes.HANDLE
            kernel32.CreateMutexW.argtypes = [wintypes.LPCVOID, wintypes.BOOL, wintypes.LPCWSTR]

            handle = kernel32.CreateMutexW(None, False, self.name)
            error = ctypes.get_last_error()
            if not handle:
                log.debug("could not create the instance mutex (error %s)", error)
                return True  # never let this be the reason the app will not start
            self._handle = handle
            if error == _ERROR_ALREADY_EXISTS:
                self.release()
                return False
            return True
        except Exception:
            log.debug("instance mutex unavailable", exc_info=True)
            return True

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            import ctypes

            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(self._handle)
        except Exception:
            log.debug("could not release the instance mutex", exc_info=True)
        finally:
            self._handle = None

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc_info: object) -> None:
        self.release()

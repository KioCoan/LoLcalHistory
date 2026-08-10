"""The desktop application.

Runs everything in one process: the dashboard's web server on loopback, the
end-of-game watcher, and a native window showing the dashboard through the
operating system's own webview. No browser, no terminal, no separate steps.

Two decisions shape this file:

* **Closing the window does not quit.** You will close it while playing, and
  quitting would stop the watcher — losing exactly the games you were about to
  record. The window hides to the tray instead; quitting is explicit.
* **The server binds to a port the OS chooses**, not a fixed one, so launching
  the app never collides with a `lolhist serve` you left running.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from werkzeug.serving import make_server

from . import config, store
from .watcher import Watcher
from .web.app import create_app

log = logging.getLogger(__name__)

WINDOW_TITLE = config.APP_NAME
STARTUP_TIMEOUT_SECONDS = 15


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class DashboardServer:
    """The Flask app on a background thread, with a clean shutdown."""

    def __init__(self, host: str = "127.0.0.1", port: int | None = None) -> None:
        self.host = host
        self.port = port or _free_port()
        self._server = make_server(self.host, self.port, create_app(), threaded=True)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="dashboard", daemon=True
        )

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        self._thread.start()
        log.info("dashboard listening on %s", self.url)

    def wait_until_ready(self, timeout: float = STARTUP_TIMEOUT_SECONDS) -> bool:
        """Block until the server answers, so the window never opens on an error."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"{self.url}/api/health", timeout=1):
                    return True
            except (urllib.error.URLError, OSError):
                time.sleep(0.1)
        return False

    def stop(self) -> None:
        self._server.shutdown()


class WatcherThread(threading.Thread):
    """The end-of-game watcher on its own event loop.

    It gets a dedicated database connection: the store serialises access across
    threads, but handing the same connection to both the watcher and the web
    server would put their transactions in each other's way.
    """

    def __init__(self) -> None:
        super().__init__(name="watcher", daemon=True)
        self.conn = store.open_db()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self.started = threading.Event()

    def run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except asyncio.CancelledError:
            log.info("watcher stopped")
        except Exception:
            log.exception("watcher thread died")
        finally:
            self._loop.close()

    async def _main(self) -> None:
        self._task = asyncio.current_task()
        self.started.set()
        await Watcher(self.conn).run_forever()

    def stop(self) -> None:
        if self._loop and self._task and not self._task.done():
            self._loop.call_soon_threadsafe(self._task.cancel)
        self.join(timeout=5)
        try:
            self.conn.close()
        except Exception:
            pass


class TrayIcon:
    """A tray icon so the app keeps tracking with its window closed.

    Optional by design — if pystray or its dependencies are unavailable the app
    still runs, it just quits when you close the window. That is a worse
    experience, not a broken one, so it should not be a hard requirement.
    """

    def __init__(self, on_open, on_quit) -> None:
        self._icon = None
        self._thread = None
        self.on_open = on_open
        self.on_quit = on_quit

    def start(self) -> bool:
        try:
            import pystray
            from PIL import Image
        except ImportError:
            log.info("pystray unavailable; running without a tray icon")
            return False

        try:
            image = Image.open(config.PACKAGE_DIR / "assets" / "icon.ico")
        except Exception:
            log.debug("icon missing; drawing a placeholder")
            from PIL import ImageDraw

            image = Image.new("RGBA", (64, 64), (28, 32, 41, 255))
            ImageDraw.Draw(image).rectangle([18, 30, 28, 48], fill=(91, 157, 217))

        self._icon = pystray.Icon(
            "lolhist",
            image,
            WINDOW_TITLE,
            menu=pystray.Menu(
                pystray.MenuItem("Open", lambda *_: self.on_open(), default=True),
                pystray.MenuItem("Quit", lambda *_: self.on_quit()),
            ),
        )
        self._thread = threading.Thread(target=self._icon.run, name="tray", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass


class Application:
    def __init__(self, with_watcher: bool = True, port: int | None = None) -> None:
        self.server = DashboardServer(port=port)
        self.watcher = WatcherThread() if with_watcher else None
        self.tray: TrayIcon | None = None
        self.window: Any = None
        self._quitting = False

    def _on_closing(self) -> bool:
        """Hide to the tray instead of quitting, when there is a tray to hide to.

        Returning False cancels the close. Without a tray icon there would be no
        way to get the window back, so in that case the close is allowed through.
        """
        if self._quitting or self.tray is None:
            return True
        self.window.hide()
        return False

    def _open(self) -> None:
        if self.window is not None:
            self.window.show()

    def _quit(self) -> None:
        self._quitting = True
        if self.window is not None:
            try:
                self.window.destroy()
            except Exception:
                pass

    def _open_in_browser(self) -> int:
        """Last resort when no webview is available.

        Better than exiting: the machine may simply be missing the WebView2
        runtime, and everything except the native window still works.
        """
        import webbrowser

        log.warning("no webview available; falling back to the default browser")
        webbrowser.open(self.server.url)
        print(f"Could not open a native window. The dashboard is at {self.server.url}")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 0

    def run(self) -> int:
        try:
            import webview
        except ImportError:
            self.server.start()
            if self.watcher is not None:
                self.watcher.start()
            return self._open_in_browser()

        self.server.start()
        if not self.server.wait_until_ready():
            log.error("dashboard did not start within %ss", STARTUP_TIMEOUT_SECONDS)
            return 1

        if self.watcher is not None:
            self.watcher.start()
            self.watcher.started.wait(timeout=5)

        self.window = webview.create_window(
            WINDOW_TITLE,
            self.server.url,
            width=1280,
            height=860,
            min_size=(900, 600),
            confirm_close=False,
        )
        self.window.events.closing += self._on_closing

        self.tray = TrayIcon(on_open=self._open, on_quit=self._quit)
        if not self.tray.start():
            self.tray = None

        try:
            webview.start()
        except Exception:
            # Typically a missing Edge WebView2 runtime. Keep tracking and hand
            # the user a working dashboard rather than dying on a blank screen.
            log.exception("the native window failed to start")
            return self._open_in_browser()
        finally:
            self.shutdown()
        return 0

    def shutdown(self) -> None:
        log.info("shutting down")
        if self.tray is not None:
            self.tray.stop()
        if self.watcher is not None:
            self.watcher.stop()
        self.server.stop()


def _setup_file_logging() -> None:
    """A windowed build has nowhere to print, so give errors somewhere to land."""
    config.ensure_dirs()
    handler = logging.FileHandler(config.DATA_DIR / "app.log", encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s  %(levelname)-7s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    for noisy in ("httpx", "httpcore", "websockets", "werkzeug"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main(with_watcher: bool = True, port: int | None = None) -> int:
    if config.FROZEN:
        _setup_file_logging()
    return Application(with_watcher=with_watcher, port=port).run()


if __name__ == "__main__":
    raise SystemExit(main())

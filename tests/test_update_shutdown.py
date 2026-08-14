"""Quitting for an update must not tear down the window.

Two rounds of fixes went into the installer before the crash dialog turned out
to be ours. Windows recorded it plainly:

    .NET Runtime: the process was terminated due to an unhandled exception,
    exception code c0000005
    Faulting module: ...\\_MEI254362\\python314.dll

The updater ran the quit hook on a worker thread, and the hook called
`window.destroy()`. That unwinds pywebview's WinForms host, which lives in the
CLR on the main thread; driving it from elsewhere access-violates inside the
interpreter. The one-file bootloader was already removing the directory the
dying process was running from, so what the user saw was

    Failed to load Python DLL '...\\_MEIxxxxx\\python3xx.dll'

which reads like an installer problem and is not one. Setup's own log for that
run shows it found nothing to close and installed in half a second.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lolhist import desktop, updates


class FakeWatcher:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class FakeServer:
    def __init__(self):
        self.stopped = False
        self.url = "http://127.0.0.1:0"

    def stop(self):
        self.stopped = True


class FakeTray:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class FakeWindow:
    def __init__(self):
        self.destroyed = False

    def destroy(self):
        self.destroyed = True

    def hide(self):
        pass


@pytest.fixture
def app(monkeypatch):
    """An Application with nothing real behind it, and exit made observable."""
    instance = desktop.Application.__new__(desktop.Application)
    instance.server = FakeServer()
    instance.watcher = FakeWatcher()
    instance.tray = FakeTray()
    instance.window = FakeWindow()
    instance._quitting = False
    instance.exited = []
    monkeypatch.setattr(
        desktop.Application, "_exit", lambda self: self.exited.append(True)
    )
    return instance


class TestShutdownForUpdate:
    def test_the_window_is_never_destroyed(self, app):
        """The regression. Destroying it from this thread is what crashed."""
        app.shutdown_for_update()
        assert app.window.destroyed is False

    def test_everything_holding_data_is_closed_first(self, app):
        app.shutdown_for_update()
        assert app.watcher.stopped, "the watcher was left with the database open"
        assert app.server.stopped
        assert app.tray.stopped, "the tray icon would have been left behind"

    def test_it_actually_exits(self, app):
        app.shutdown_for_update()
        assert app.exited == [True]

    def test_it_exits_even_if_closing_something_fails(self, app):
        def explode():
            raise RuntimeError("watcher would not stop")

        app.watcher.stop = explode
        app.shutdown_for_update()

        assert app.exited == [True], (
            "a stuck component would leave the installer waiting for a process "
            "that never goes away"
        )

    def test_the_updater_is_wired_to_this_and_not_to_quit(self):
        """`_quit` is for the tray menu, where the main thread owns the window."""
        import inspect

        source = inspect.getsource(desktop.Application.run)
        assert "set_quit_hook(self.shutdown_for_update)" in source
        assert "set_quit_hook(self._quit)" not in source


class TestInstallerArguments:
    def test_the_log_path_is_quoted(self, monkeypatch):
        """The data folder is "LoLcal History". Start-Process joins its argument
        list on spaces without quoting, so an unquoted /LOG= was cut in half and
        Setup wrote its log to a stray file named after the first word."""
        spawned = {}
        monkeypatch.setattr(
            updates.subprocess, "Popen", lambda argv, **kw: spawned.update(argv=argv)
        )
        updates._launch(Path(r"C:\x\Setup.exe"))

        script = spawned["argv"][-1]
        assert '"/LOG=' in script, "the log path can be split on a space"

    def test_every_argument_survives_the_join(self, monkeypatch):
        spawned = {}
        monkeypatch.setattr(
            updates.subprocess, "Popen", lambda argv, **kw: spawned.update(argv=argv)
        )
        updates._launch(Path(r"C:\x\Setup.exe"))

        script = spawned["argv"][-1]
        for flag in ("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"):
            assert f"'{flag}'" in script

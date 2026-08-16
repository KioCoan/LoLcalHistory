"""Remembering where the window was.

Most of this is about refusing to restore a position rather than restoring one.
Saving coordinates is easy; the failure that matters is opening the window onto
a monitor that has since been unplugged, where it is invisible and the app
looks like it never started.
"""

from __future__ import annotations

import json

import pytest

from lolhist import geometry


class FakeScreen:
    def __init__(self, x, y, width, height):
        self.x, self.y, self.width, self.height = x, y, width, height


# The machine this was written on: an ultrawide with a second monitor to its
# right, sitting slightly lower.
WIDE = FakeScreen(0, 0, 3440, 1440)
SECOND = FakeScreen(3440, 182, 1920, 1080)
LAPTOP = FakeScreen(0, 0, 1366, 768)


def write(geometry_dict):
    geometry.config.ensure_dirs()
    geometry._path().write_text(json.dumps(geometry_dict), encoding="utf-8")


# -- saving and loading ----------------------------------------------------


def test_a_round_trip_keeps_the_geometry():
    geometry.save({"x": 120, "y": 80, "width": 1400, "height": 900, "maximized": False})
    assert geometry.load() == {
        "x": 120, "y": 80, "width": 1400, "height": 900, "maximized": False
    }


def test_no_file_yet_is_not_an_error():
    assert geometry.load() == {}
    placement = geometry.restore([WIDE])
    assert placement == {"width": geometry.DEFAULT_WIDTH, "height": geometry.DEFAULT_HEIGHT}


def test_a_corrupt_file_falls_back_to_the_default():
    geometry.config.ensure_dirs()
    geometry._path().write_text("{not json", encoding="utf-8")
    assert geometry.load() == {}
    assert geometry.restore([WIDE])["width"] == geometry.DEFAULT_WIDTH


def test_saving_never_raises(monkeypatch):
    """A read-only data directory must cost the position, not the shutdown.

    `save` is called from the app's exit path, and an exception there would
    turn a lost window position into a failed quit.
    """
    class Unwritable:
        def write_text(self, *a, **k):
            raise OSError("read-only")

    monkeypatch.setattr(geometry, "_path", lambda: Unwritable())
    geometry.save({"width": 1000, "height": 800})   # must not raise


# -- restoring a position --------------------------------------------------


def test_a_position_on_a_connected_screen_is_restored():
    write({"x": 3600, "y": 300, "width": 1400, "height": 900})
    placement = geometry.restore([WIDE, SECOND])
    assert (placement["x"], placement["y"]) == (3600, 300)
    assert (placement["width"], placement["height"]) == (1400, 900)


def test_a_position_on_an_unplugged_monitor_is_dropped():
    """The window was left on the second screen, which is now gone.

    The size is still worth keeping; the coordinates are not, and their absence
    is what asks the OS to centre the window.
    """
    write({"x": 3600, "y": 300, "width": 1400, "height": 900})
    placement = geometry.restore([WIDE])

    assert "x" not in placement and "y" not in placement
    assert (placement["width"], placement["height"]) == (1400, 900)


def test_a_window_hanging_off_an_edge_is_still_reachable():
    """Half off the right edge is untidy, not lost — it can still be dragged."""
    write({"x": 3300, "y": 700, "width": 1400, "height": 900})
    placement = geometry.restore([WIDE])
    assert (placement["x"], placement["y"]) == (3300, 700)


def test_a_sliver_on_screen_does_not_count():
    """Ten pixels of window is not something you can grab and pull back."""
    assert not geometry.is_reachable(3430, 700, 1400, 900, [(0, 0, 3440, 1440)])


def test_a_negative_position_is_fine_when_a_screen_is_there():
    """A monitor to the left of the primary starts at a negative x."""
    left = FakeScreen(-1920, 0, 1920, 1080)
    write({"x": -1800, "y": 100, "width": 1200, "height": 800})
    placement = geometry.restore([left, WIDE])
    assert (placement["x"], placement["y"]) == (-1800, 100)


def test_no_screen_information_trusts_what_was_saved():
    """Refusing to restore because screens could not be read punishes everyone."""
    write({"x": 400, "y": 200, "width": 1200, "height": 800})
    placement = geometry.restore([])
    assert (placement["x"], placement["y"]) == (400, 200)


# -- restoring a size ------------------------------------------------------


def test_a_size_larger_than_the_screen_is_clamped():
    """An ultrawide swapped for a laptop panel must not open a window off it."""
    write({"x": 0, "y": 0, "width": 3000, "height": 1300})
    placement = geometry.restore([LAPTOP])
    assert placement["width"] <= LAPTOP.width
    assert placement["height"] <= LAPTOP.height


def test_a_size_below_the_minimum_is_raised():
    write({"width": 200, "height": 100})
    placement = geometry.restore([WIDE])
    assert (placement["width"], placement["height"]) == (geometry.MIN_WIDTH, geometry.MIN_HEIGHT)


def test_maximized_is_remembered():
    write({"x": 10, "y": 10, "width": 1400, "height": 900, "maximized": True})
    assert geometry.restore([WIDE])["maximized"] is True


def test_not_maximized_does_not_pass_the_flag():
    """`maximized=False` and no flag mean the same thing; only send the flag."""
    write({"width": 1400, "height": 900, "maximized": False})
    assert "maximized" not in geometry.restore([WIDE])


@pytest.mark.parametrize("junk", [
    {"x": "left", "y": None, "width": [], "height": {}},
    {"x": True, "y": False},
    [1, 2, 3],
    "nonsense",
])
def test_junk_in_the_file_cannot_break_a_launch(junk):
    geometry.config.ensure_dirs()
    geometry._path().write_text(json.dumps(junk), encoding="utf-8")
    placement = geometry.restore([WIDE])
    assert placement["width"] >= geometry.MIN_WIDTH
    assert placement["height"] >= geometry.MIN_HEIGHT


# -- the tracker -----------------------------------------------------------


def test_the_tracker_follows_moves_and_resizes():
    tracker = geometry.Tracker()
    tracker.moved(300, 150)
    tracker.resized(1500, 950)
    assert tracker.geometry() == {
        "x": 300, "y": 150, "width": 1500, "height": 950, "maximized": False
    }


def test_a_maximized_window_keeps_the_size_underneath_it():
    """Otherwise un-maximising a restored window gives a full-screen one."""
    tracker = geometry.Tracker()
    tracker.moved(300, 150)
    tracker.resized(1500, 950)

    tracker.set_maximized(True)
    tracker.moved(0, 0)             # what the window reports while maximised
    tracker.resized(3440, 1440)

    assert tracker.geometry() == {
        "x": 300, "y": 150, "width": 1500, "height": 950, "maximized": True
    }


def test_a_nonsense_size_is_ignored():
    """Some backends report a garbage size on the way to being minimised."""
    tracker = geometry.Tracker(1400, 900)
    tracker.resized(0, 0)
    tracker.resized(160, 28)
    assert (tracker.width, tracker.height) == (1400, 900)


def test_the_tracker_starts_from_where_the_window_was_opened():
    """A window opened and closed without being dragged emits no move event."""
    placement = {"width": 1400, "height": 900, "x": 3600, "y": 300}
    tracker = geometry.Tracker.from_placement(placement)
    assert tracker.geometry() == {
        "x": 3600, "y": 300, "width": 1400, "height": 900, "maximized": False
    }


# -- how the app wires it up -----------------------------------------------


class FakeEvent:
    """A pywebview event: `+=` subscribes, and the backend calls what it holds."""

    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self, *args):
        for handler in self.handlers:
            handler(*args)


class FakeEvents:
    def __init__(self):
        for name in ("moved", "resized", "maximized", "restored", "closing"):
            setattr(self, name, FakeEvent())


class FakeWindow:
    def __init__(self):
        self.events = FakeEvents()
        self.hidden = False

    def hide(self):
        self.hidden = True


def an_app(tracker=None):
    from lolhist import desktop

    app = desktop.Application.__new__(desktop.Application)
    app.window = FakeWindow()
    app.geometry = tracker or geometry.Tracker()
    app.tray = object()
    app._quitting = False
    app._track_geometry()
    return app


def test_the_window_events_reach_the_tracker():
    """The arguments are pywebview's: moved gives (x, y), resized (width, height)."""
    app = an_app()
    app.window.events.moved.fire(2000, 300)
    app.window.events.resized.fire(1500, 950)

    assert app.geometry.geometry() == {
        "x": 2000, "y": 300, "width": 1500, "height": 950, "maximized": False
    }


def test_maximizing_and_restoring_are_followed():
    app = an_app()
    app.window.events.resized.fire(1500, 950)
    app.window.events.maximized.fire()
    assert app.geometry.maximized is True

    app.window.events.restored.fire()
    assert app.geometry.maximized is False
    assert (app.geometry.width, app.geometry.height) == (1500, 950)


def test_a_handler_that_throws_cannot_break_the_window():
    """A webview backend swallows exceptions from its own events.

    One raised here would leave the window unable to remember anything, and
    nothing on screen would say so.
    """
    app = an_app()
    app.window.events.moved.fire("not", "a position")
    app.window.events.resized.fire(None, None)
    app.window.events.moved.fire(500, 250)      # still working
    assert (app.geometry.x, app.geometry.y) == (500, 250)


def test_hiding_to_the_tray_saves_the_position():
    """The usual way this window stops being looked at, so it has to save."""
    app = an_app()
    app.window.events.moved.fire(2000, 300)
    app.window.events.resized.fire(1500, 950)

    assert app._on_closing() is False           # hidden, not closed
    assert app.window.hidden is True
    assert geometry.load() == {
        "x": 2000, "y": 300, "width": 1500, "height": 950, "maximized": False
    }


def test_a_full_cycle_survives_a_restart():
    """Open where it was, move it, close it, and find it there again."""
    tracker = geometry.Tracker.from_placement(geometry.restore([WIDE, SECOND]))
    tracker.moved(3600, 400)
    tracker.resized(1500, 1000)
    tracker.persist()

    assert geometry.restore([WIDE, SECOND]) == {
        "x": 3600, "y": 400, "width": 1500, "height": 1000
    }

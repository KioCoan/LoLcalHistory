"""Where the window was when you last closed it.

Small on its own, but the awkward part is not the saving — it is refusing to
restore a position that would strand the window somewhere you cannot reach it.
A second monitor is the ordinary case, not the exotic one: put the app on it,
unplug it or dock the laptop elsewhere, and the coordinates on file now name a
place that no longer exists. Windows will happily open a window there, entirely
off-screen, and the app looks like it failed to start.

So a saved position is a suggestion, checked against the screens that exist
right now. If enough of the window would land somewhere visible it is used; if
not, the size is kept and the position is dropped, and the OS centres it.

Two smaller rules, both learned from how the window is actually used:

* **A minimised window is never saved as minimised.** Restoring that state
  would look exactly like the app failing to open.
* **Maximised is remembered, and so is the size underneath it.** Un-maximising
  a window that has never had a normal size gives you whatever the OS guesses.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable

from . import config

log = logging.getLogger(__name__)

WINDOW_FILE = "window.json"

# The size the window opens at before it has ever been moved.
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 860

# Matches `min_size` on the window itself. Restoring anything smaller would
# fight the window manager on the first paint.
MIN_WIDTH = 900
MIN_HEIGHT = 600

# How much of the window has to overlap a screen before the position is
# trusted. Roughly a grabbable strip of title bar — a window you can see a
# sliver of but cannot drag is no more use than one you cannot see at all.
REACHABLE_X = 120
REACHABLE_Y = 40


def _path():
    return config.DATA_DIR / WINDOW_FILE


def load() -> dict[str, Any]:
    """The geometry on file, or an empty dict if there is none worth having."""
    try:
        raw = _path().read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        saved = json.loads(raw)
    except ValueError:
        log.debug("window geometry file is not valid JSON; ignoring")
        return {}
    return saved if isinstance(saved, dict) else {}


def save(geometry: dict[str, Any]) -> None:
    """Record the geometry. Never raises — a lost position is not worth a crash."""
    try:
        config.ensure_dirs()
        _path().write_text(json.dumps(geometry), encoding="utf-8")
    except OSError:
        log.debug("could not save the window geometry", exc_info=True)


def _int(value: Any) -> int | None:
    """Ints only, and not bools — `True` is an int and would pass silently."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def screen_rects(screens: Iterable[Any]) -> list[tuple[int, int, int, int]]:
    """`(x, y, width, height)` per screen, skipping any that cannot be read.

    Screens can sit at negative coordinates — a monitor to the left of the
    primary one starts below zero — so nothing here may assume the desktop
    begins at the origin.
    """
    rects = []
    for screen in screens or ():
        x, y = _int(getattr(screen, "x", None)), _int(getattr(screen, "y", None))
        width = _int(getattr(screen, "width", None))
        height = _int(getattr(screen, "height", None))
        if None in (x, y, width, height) or width <= 0 or height <= 0:
            continue
        rects.append((x, y, width, height))
    return rects


def is_reachable(
    x: int, y: int, width: int, height: int, rects: list[tuple[int, int, int, int]]
) -> bool:
    """Would enough of this window land on a screen to be usable?

    With no screen information at all this answers True: refusing to restore
    because the screen list could not be read would punish every launch on a
    platform that does not report one.
    """
    if not rects:
        return True
    for sx, sy, sw, sh in rects:
        overlap_x = min(x + width, sx + sw) - max(x, sx)
        overlap_y = min(y + height, sy + sh) - max(y, sy)
        if overlap_x >= min(REACHABLE_X, width) and overlap_y >= min(REACHABLE_Y, height):
            return True
    return False


def restore(screens: Iterable[Any] = ()) -> dict[str, Any]:
    """Window arguments for this launch, from the last one where sensible.

    Always returns a usable size. `x`/`y` are present only when the saved
    position still lands on a screen, and their absence is what asks the OS to
    centre the window.
    """
    saved = load()
    width = _int(saved.get("width")) or DEFAULT_WIDTH
    height = _int(saved.get("height")) or DEFAULT_HEIGHT

    rects = screen_rects(screens)
    # A screen smaller than the remembered size — a big monitor swapped for a
    # laptop panel — would otherwise open a window taller than the desktop.
    if rects:
        width = min(width, max(w for _x, _y, w, _h in rects))
        height = min(height, max(h for _x, _y, _w, h in rects))
    geometry: dict[str, Any] = {
        "width": max(width, MIN_WIDTH),
        "height": max(height, MIN_HEIGHT),
    }

    x, y = _int(saved.get("x")), _int(saved.get("y"))
    if x is not None and y is not None:
        if is_reachable(x, y, geometry["width"], geometry["height"], rects):
            geometry["x"], geometry["y"] = x, y
        else:
            log.info(
                "the window's last position (%s, %s) is off-screen now; centring it", x, y
            )

    if saved.get("maximized"):
        geometry["maximized"] = True
    return geometry


class Tracker:
    """Follows the window's geometry so it can be written down later.

    Kept in memory rather than written on every event for two reasons: a drag
    emits a move per frame, and the window is often already destroyed by the
    time there is a reason to save — reading `window.x` then is too late.

    Only the *normal* size and position are recorded. While maximised the window
    reports the whole screen, and storing that would mean un-maximising a
    restored window gave you a full-screen one.
    """

    def __init__(self, width: int = DEFAULT_WIDTH, height: int = DEFAULT_HEIGHT) -> None:
        self.width = width
        self.height = height
        self.x: int | None = None
        self.y: int | None = None
        self.maximized = False

    @classmethod
    def from_placement(cls, placement: dict[str, Any]) -> "Tracker":
        """Start from where the window was just told to open.

        Without this a window that is opened and closed without being dragged
        emits no move event, and saving would replace a perfectly good position
        with none at all.
        """
        tracker = cls(placement.get("width", DEFAULT_WIDTH),
                      placement.get("height", DEFAULT_HEIGHT))
        tracker.x = _int(placement.get("x"))
        tracker.y = _int(placement.get("y"))
        tracker.maximized = bool(placement.get("maximized"))
        return tracker

    def moved(self, x: Any, y: Any) -> None:
        if self.maximized:
            return
        x, y = _int(x), _int(y)
        if x is not None and y is not None:
            self.x, self.y = x, y

    def resized(self, width: Any, height: Any) -> None:
        if self.maximized:
            return
        width, height = _int(width), _int(height)
        # A minimise reports a nonsense size on some backends; anything below
        # the window's own minimum is not a size worth keeping.
        if width and height and width >= MIN_WIDTH and height >= MIN_HEIGHT:
            self.width, self.height = width, height

    def set_maximized(self, maximized: bool) -> None:
        self.maximized = bool(maximized)

    def geometry(self) -> dict[str, Any]:
        geometry: dict[str, Any] = {
            "width": self.width,
            "height": self.height,
            "maximized": self.maximized,
        }
        if self.x is not None and self.y is not None:
            geometry["x"], geometry["y"] = self.x, self.y
        return geometry

    def persist(self) -> None:
        save(self.geometry())

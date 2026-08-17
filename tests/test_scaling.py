"""The page grows with the window.

Maximising the app on a large screen used to leave a 1180px column of 14px text
stranded in the middle of it. The fix is one dial — the root font size, driven
by the viewport — and a stylesheet written entirely in rem, so type, icons,
padding and the content column all move together.

That only holds while every length stays relative. A single `font-size: 12px`
added later would quietly stop scaling, and nothing on screen would say so at
the size it was authored at. Most of what follows guards that, and the rest
checks the curve arrives at the sizes it was drawn for.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from lolhist import geometry, web

PAGE = (Path(web.__file__).parent / "templates" / "index.html").read_text(encoding="utf-8")
STYLE = re.search(r"<style>(.*?)</style>", PAGE, re.S).group(1)
# The comments explain the numbers; they are not the numbers, and a stray "21px"
# in a sentence must not read as a declaration.
CSS = re.sub(r"/\*.*?\*/", "", STYLE, flags=re.S)
ROOT = re.search(r":root\s*\{(.*?)\}", CSS, re.S).group(1)


def coefficients(name):
    """The slope and intercept of one of the two scale lines, read from the CSS."""
    found = re.search(rf"--{name}:\s*calc\(([\d.]+)v[wh]\s*\+\s*([\d.]+)px\)", CSS)
    assert found, f"--{name} is not a calc() of a viewport unit and an offset"
    return float(found.group(1)), float(found.group(2))


def bounds():
    found = re.search(r"font-size:\s*clamp\(\s*([\d.]+)px\s*,.*?,\s*([\d.]+)px\s*\)", ROOT, re.S)
    assert found, ":root has no clamped font-size"
    return float(found.group(1)), float(found.group(2))


def scale_at(width, height):
    """What root font size a window of this size gets, worked out in Python.

    The same arithmetic the browser does, from the same numbers — the point is
    to check where the line lands, not to re-declare it.
    """
    w_slope, w_base = coefficients("scale-w")
    h_slope, h_base = coefficients("scale-h")
    floor, ceiling = bounds()
    return max(floor, min(ceiling, min(w_slope * width / 100 + w_base,
                                       h_slope * height / 100 + h_base)))


# -- where the curve lands -------------------------------------------------


def test_the_default_window_looks_as_it_always_did():
    """The lower anchor is the size the app opens at, so nothing moved for it.

    If the default window size changes, this fails — correctly. The curve is
    drawn through that point on purpose and would need redrawing.
    """
    assert scale_at(geometry.DEFAULT_WIDTH, geometry.DEFAULT_HEIGHT) == pytest.approx(16, abs=0.1)


def test_a_maximized_ultrawide_gets_the_largest_type():
    """The upper anchor, and the case this feature was asked for."""
    assert scale_at(3440, 1440) == pytest.approx(21, abs=0.1)


def test_the_smallest_allowed_window_is_still_readable():
    floor, _ = bounds()
    assert scale_at(geometry.MIN_WIDTH, geometry.MIN_HEIGHT) == floor
    assert floor >= 14, "below this the tables stop being readable at all"


def test_growing_the_window_never_shrinks_the_type():
    sizes = [(900, 600), (1280, 860), (1600, 900), (1920, 1080), (2560, 1440), (3440, 1440)]
    scales = [scale_at(w, h) for w, h in sizes]
    assert scales == sorted(scales)


def test_a_wide_but_short_window_follows_its_height():
    """3440x600 is as wide as a maximised ultrawide and has none of the room.

    Scaling on width alone would fill it with 21px type and three visible rows.
    """
    assert scale_at(3440, 700) < scale_at(3440, 1440)
    assert "min(var(--scale-w), var(--scale-h))" in ROOT


def test_the_scale_is_capped_at_both_ends():
    floor, ceiling = bounds()
    assert scale_at(200, 200) == floor
    assert scale_at(10000, 10000) == ceiling


# -- and that everything is hung off it ------------------------------------


def declarations(prop):
    """Every value given to a property, minus the one on :root itself."""
    return [v.strip() for v in re.findall(rf"(?<![-\w]){prop}:\s*([^;}}]+)", CSS)
            if "clamp(" not in v]


def relative(value):
    """Nothing absolute, and nothing that resolves to a fixed number of pixels.

    The intrinsic keywords count as relative: a box sized to its own content
    grows with the text inside it, which is the whole point.
    """
    return bool(re.match(r"^[\d.]+rem$", value)) or value in {
        "inherit", "100%", "auto", "0", "50%",
        "max-content", "min-content", "fit-content",
    }


@pytest.mark.parametrize("value", declarations("font-size"))
def test_every_font_size_scales(value):
    assert relative(value), f"font-size: {value} is fixed and will not grow with the window"


@pytest.mark.parametrize("value", declarations("width") + declarations("height"))
def test_every_icon_and_box_scales(value):
    assert relative(value), f"a fixed {value} beside text that grows will look wrong at some size"


def test_the_body_font_is_relative_too():
    """It is set through the `font` shorthand, which the checks above miss."""
    found = re.search(r"body\s*\{[^}]*font:\s*([^;]+)", CSS)
    assert found and found.group(1).strip().startswith("0.875rem/")


def test_the_content_column_widens_with_the_type():
    """The original complaint: a pixel cap here is what stranded the page."""
    found = re.search(r"\.wrap\s*\{[^}]*max-width:\s*([^;]+)", CSS)
    assert found and found.group(1).strip().endswith("rem")


# Hairlines and the accent stripe on the notices. A 1px border is a 1px border
# at every size; scaling it only makes it blurry.
HAIRLINES = {"1px", "2px", "4px"}
# "Fully round", not a measurement.
ROUND = {"999px"}


def test_nothing_else_is_pinned_to_a_pixel_size():
    strays = []
    for line in CSS.splitlines():
        if "--scale-" in line or "font-size: clamp(" in line:
            continue                      # the dial itself has to be absolute
        strays += [(line.strip(), px) for px in re.findall(r"[\d.]+px", line)
                   if px not in HAIRLINES | ROUND]
    assert not strays


def test_no_fixed_sizes_crept_into_the_markup():
    """Inline styles are written in JavaScript and escape every check above."""
    body = PAGE[PAGE.index("</style>"):]
    assert not re.findall(r'style="[^"]*[\d.]px', body)

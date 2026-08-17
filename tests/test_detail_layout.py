"""How an opened match row lays its two teams out.

Each list used to be stretched across half the row, which left a player's build
a third of a screen from their name — barely noticeable at 1180px and glaring
once the page started growing with the window. The teams now meet at a rule
down the middle: each list is only as wide as its widest row, blue aligned to
its right edge and red to its left, both facing the line.

None of that survives on its own. Stretching comes back the moment the lists go
back to filling their grid track, and the alignment is carried by two class
names emitted from JavaScript that nothing else would miss.
"""

from __future__ import annotations

import re
from pathlib import Path

from lolhist import web

PAGE = (Path(web.__file__).parent / "templates" / "index.html").read_text(encoding="utf-8")
STYLE = re.search(r"<style>(.*?)</style>", PAGE, re.S).group(1)
CSS = re.sub(r"/\*.*?\*/", "", STYLE, flags=re.S)
DETAIL = re.search(r"function matchDetail\(.*?\n}", PAGE, re.S).group(0)


def rule(selector):
    """The declarations of one rule, by exact selector."""
    found = re.search(rf"(?m)^\s*{re.escape(selector)}\s*\{{(.*?)\}}", CSS, re.S)
    assert found, f"no rule for {selector}"
    return " ".join(found.group(1).split())


# -- the lists ---------------------------------------------------------------


def test_a_team_list_is_only_as_wide_as_its_widest_row():
    """The fix for the gap. Stretched, the build sat at the far edge instead."""
    assert "width: max-content" in rule(".teams ul")


def test_a_team_list_still_shrinks_when_there_is_no_room():
    """max-content alone would push the row off a small window."""
    assert "max-width: 100%" in rule(".teams ul")


def test_the_builds_stay_in_a_column():
    """Rows still fill the list, so builds line up rather than going ragged.

    It is the column that moved in, not the alignment that went away — losing
    it would mean hunting for each player's items at a different x.
    """
    assert "flex: 1 1 auto" in rule(".who")


def test_a_long_name_can_still_be_truncated():
    assert "min-width: 0" in rule(".who")


# -- the rule down the middle ------------------------------------------------


def test_the_divider_is_centred():
    assert "left: 50%" in rule(".teams.split::before")


def test_the_divider_is_inset_top_and_bottom():
    """Otherwise it reads as a border drawn on the row rather than a divider."""
    declarations = rule(".teams.split::before")
    assert "top: 1rem" in declarations and "bottom: 1rem" in declarations


def test_the_divider_sits_in_the_middle_of_the_gutter():
    """`left: 50%` only lands in the gap because the two tracks are equal.

    With `1fr 1fr` and a single column gap, the midpoint of the element and the
    midpoint of the gutter are the same point. Change either and the line moves
    into a team's text.
    """
    declarations = rule(".teams")
    assert "grid-template-columns: 1fr 1fr" in declarations
    assert re.search(r"column-gap: [\d.]+rem", declarations)
    assert "position: relative" in declarations, "the rule is positioned against this"


def test_the_teams_face_the_line():
    assert "align-items: flex-end" in rule(".teams .team.blue")


# -- and the markup that carries it -----------------------------------------


def test_both_teams_are_labelled_for_the_stylesheet():
    for side in ("team blue", "team red"):
        assert f'class="{side}"' in DETAIL, f"nothing selects the {side} wrapper"


def test_the_divider_is_only_drawn_when_it_divides_something():
    """A mode whose teams are not 100 and 200 leaves both lists empty.

    Drawing the rule unconditionally would put a stray mark down the middle of
    an empty block, which reads as a rendering fault rather than a divider.
    """
    assert re.search(r"blue && red \? \" split\" : \"\"", DETAIL)
    assert 'class="teams${split}"' in DETAIL


def test_each_side_is_only_built_once():
    """`split` has to be decided from the same strings the lists are made of.

    Calling `side()` again to test it would let the class and the content
    disagree if either ever stopped being a pure function of the row.
    """
    assert re.search(r"const blue = side\(100\), red = side\(200\);", DETAIL)
    assert DETAIL.count("side(100)") == 1 and DETAIL.count("side(200)") == 1
    assert "${blue}" in DETAIL and "${red}" in DETAIL

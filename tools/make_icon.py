"""Generate the application icon.

Drawn rather than shipped as a binary blob so it stays reviewable in the repo
and can be regenerated at any size. Run from the project root:

    .venv/Scripts/python.exe tools/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "lolhist" / "assets" / "icon.ico"
SIZES = [16, 24, 32, 48, 64, 128, 256]

BG = (28, 32, 41)
ACCENT = (91, 157, 217)
BAR = (70, 167, 88)


def draw(size: int) -> Image.Image:
    """A small bar chart on a rounded tile — history, at a glance."""
    scale = 8
    img = Image.new("RGBA", (size * scale, size * scale), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size * scale

    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22), fill=BG)

    margin = s * 0.22
    width = s * 0.13
    gap = s * 0.075
    heights = [0.30, 0.52, 0.40, 0.68]
    colours = [ACCENT, BAR, ACCENT, BAR]

    x = margin
    base = s - margin
    for height, colour in zip(heights, colours):
        top = base - (s - margin * 2) * height
        d.rounded_rectangle(
            [x, top, x + width, base], radius=int(width * 0.35), fill=colour
        )
        x += width + gap

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    frames = [draw(size) for size in SIZES]
    frames[-1].save(OUT, format="ICO", sizes=[(s, s) for s in SIZES])
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes, sizes {SIZES})")


if __name__ == "__main__":
    main()

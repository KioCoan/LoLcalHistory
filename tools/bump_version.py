"""Set the release version in the two places that must agree.

The release workflow refuses to build a tag that disagrees with
`lolhist/version.py`, which is the right check — a build reporting a different
number than the tag it shipped under would break the update comparison. But it
means bumping two files by hand before every tag, and forgetting one costs a
failed run and a deleted tag.

    .venv/Scripts/python.exe tools/bump_version.py 0.2.0

Prints the git commands rather than running them: tagging and pushing are
yours to decide on, and a script that pushes on your behalf is a script you
have to read before every use.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_PY = ROOT / "lolhist" / "version.py"
PYPROJECT = ROOT / "pyproject.toml"

# Digits and dots only. Anything else cannot become a Windows file version, and
# the installer build would abort on it.
VALID = re.compile(r"^\d+(\.\d+){1,3}$")


def _replace(path: Path, pattern: str, replacement: str) -> str:
    text = path.read_text(encoding="utf-8")
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.M)
    if count != 1:
        raise SystemExit(f"Could not find the version line in {path.name}.")
    path.write_text(updated, encoding="utf-8")
    return updated


def current() -> str:
    match = re.search(r'^__version__ = "([^"]+)"', VERSION_PY.read_text(encoding="utf-8"), re.M)
    return match.group(1) if match else "?"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {Path(argv[0]).name} <version>    (currently {current()})")
        return 2

    version = argv[1].lstrip("vV")
    if not VALID.match(version):
        print(f"'{version}' is not a version. Use digits and dots, e.g. 0.2.0.")
        return 2

    _replace(VERSION_PY, r'^__version__ = "[^"]+"', f'__version__ = "{version}"')
    _replace(PYPROJECT, r'^version = "[^"]+"', f'version = "{version}"')

    print(f"Set version {version} in {VERSION_PY.name} and {PYPROJECT.name}.\n")
    print("Then:")
    print(f'  git commit -am "Release {version}"')
    print(f"  git tag v{version}")
    print(f"  git push && git push origin v{version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

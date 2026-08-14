"""The one place the version number lives.

The release workflow refuses to build a tag that disagrees with this file. That
is deliberate: the update check compares the running version against the latest
release tag, so a build that reports a different number than the tag it shipped
under would either offer an update forever or never offer one at all.

Bump this, commit, then tag `v<the same number>`.
"""

from __future__ import annotations

__version__ = "0.1.6"

# GitHub repository the update check asks about. Only ever used to build the
# releases URL — nothing about your history is sent anywhere.
GITHUB_REPO = "KioCoan/LoLcalHistory"

RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases"
LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"


def parse(text: str) -> tuple[int, ...]:
    """`v1.2.3` -> `(1, 2, 3)`, for comparison.

    Anything after a dash is a pre-release marker and is dropped, so `1.2.0-rc1`
    compares equal to `1.2.0`. That is the conservative direction: it will never
    nag someone running a release candidate to "update" to the version they are
    already effectively on.

    Unparseable input gives `()`, which sorts below every real version and so
    can never trigger an update prompt on its own.
    """
    cleaned = (text or "").strip().lstrip("vV").split("-", 1)[0].split("+", 1)[0]
    parts: list[int] = []
    for chunk in cleaned.split("."):
        if not chunk.isdigit():
            return ()
        parts.append(int(chunk))
    return tuple(parts)


def is_newer(candidate: str, current: str = __version__) -> bool:
    """True when `candidate` is a release worth offering."""
    right = parse(candidate)
    left = parse(current)
    if not right or not left:
        return False
    # Pad so 1.2 and 1.2.0 compare equal rather than by length.
    width = max(len(left), len(right))
    return right + (0,) * (width - len(right)) > left + (0,) * (width - len(left))

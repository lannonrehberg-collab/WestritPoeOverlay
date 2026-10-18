"""Lightweight update check against GitHub Releases (notify-only, variant A).

Queries the latest release tag and compares it to the running version. No
self-replacement: the UI just notifies and links to the releases page.
"""

from __future__ import annotations

import httpx

from aldur_appraiser import __version__, netenv

REPO = "lannonrehberg-collab/WestritPoeOverlay"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
_API = f"https://api.github.com/repos/{REPO}/releases/latest"


def _parse(version: str) -> tuple[int, ...]:
    core = version.lstrip("vV").split("-")[0].split("+")[0]
    parts: list[int] = []
    for piece in core.split("."):
        if piece.isdigit():
            parts.append(int(piece))
        else:
            break
    return tuple(parts)


def is_newer(latest: str, current: str) -> bool:
    return _parse(latest) > _parse(current)


def latest_version(*, timeout: float = 8.0) -> str | None:
    """Latest release tag (without a leading 'v'), or None on any failure."""
    try:
        resp = netenv.get(
            _API,
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "aldur-appraiser", "Accept": "application/vnd.github+json"},
        )
        resp.raise_for_status()
        tag = resp.json().get("tag_name")
        return tag.lstrip("vV") if tag else None
    except (httpx.HTTPError, ValueError, KeyError):
        return None


def newer_release(current: str | None = None) -> str | None:
    """Return the latest version if it's newer than `current`, else None."""
    current = current or __version__
    latest = latest_version()
    return latest if (latest and is_newer(latest, current)) else None

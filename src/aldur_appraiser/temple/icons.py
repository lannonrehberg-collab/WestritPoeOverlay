"""Official temple room icons (poe2db CDN — the same source the Tetriszocker
editor uses). Downloaded once and cached as PNG (converted from webp so Qt loads
them reliably). Best-effort: offline / failed downloads simply yield no icon and
the editor falls back to its coloured tiles.
"""

from __future__ import annotations

from pathlib import Path

ICON_BASE = (
    "https://cdn.poe2db.tw/image/Art/Textures/Interface/2D/2DArt/"
    "UIImages/InGame/Incursion2/RoomIcons/"
)

# room id -> poe2db icon file stem (.webp). Paths/Architect rooms with no game
# room icon are omitted and fall back to the coloured tile.
ROOM_ICON_FILE: dict[str, str] = {
    "garrison": "IconGarrison",
    "legion_barracks": "IconViperLegionBarracks",
    "transcendent_barracks": "IconTranscendentBarracks",
    "commander": "IconCommander",
    "armoury": "IconArmoury",
    "smithy": "IconSmithy",
    "golem_works": "IconGolemWorks",
    "synthflesh_lab": "IconSynthflesh",
    "flesh_surgeon": "IconFleshSurgeon",
    "generator": "IconGenerator",
    "spymaster": "IconViperSpymaster",
    "alchemy_lab": "IconAlchemyLab",
    "thaumaturge": "IconThaumaturge",
    "corruption_chamber": "IconCorruption",
    "sacrificial_chamber": "IconSacrificialChamber",
    "treasure_vault": "IconVault",
    "currency_vault": "IconRewardCurrency",
    "lineage_gems_vault": "IconRewardCurrency",
    "tablets_vault": "IconRewardCurrency",
    "uniques_vault": "IconRewardCurrency",
    "augments_vault": "IconRewardCurrency",
}

_TIMEOUT = 4.0
# the poe2db CDN 403s the default httpx UA; a browser-like UA + Referer is fine.
_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://poe2db.tw/"}
_net_failed = False  # circuit breaker: after one failure, stop trying (offline)


def _icons_dir() -> Path:
    from aldur_appraiser.config import cache_dir

    d = cache_dir() / "temple_icons"
    d.mkdir(parents=True, exist_ok=True)
    return d


def icon_path(room_id: str) -> Path | None:
    """Cached PNG path for a room's icon, downloading + converting once. Returns
    None if the room has no icon or the download fails (best-effort)."""
    global _net_failed
    stem = ROOM_ICON_FILE.get(room_id)
    if not stem:
        return None
    out = _icons_dir() / f"{room_id}.png"
    if out.exists():
        return out
    if _net_failed:
        return None
    try:
        import io

        from PIL import Image

        from aldur_appraiser import netenv

        resp = netenv.get(
            ICON_BASE + stem + ".webp",
            timeout=_TIMEOUT,
            follow_redirects=True,
            headers=_HEADERS,
        )
        resp.raise_for_status()
        Image.open(io.BytesIO(resp.content)).convert("RGBA").save(out, "PNG")
        return out
    except Exception:  # noqa: BLE001 - best effort; any failure -> no icon
        _net_failed = True
        return None


def room_icon_paths() -> dict[str, Path]:
    """Local PNG path per room id (cached; downloads missing ones best-effort)."""
    out: dict[str, Path] = {}
    for rid in ROOM_ICON_FILE:
        p = icon_path(rid)
        if p is not None:
            out[rid] = p
    return out

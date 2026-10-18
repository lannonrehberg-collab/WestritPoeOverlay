"""Vision: read the drawn temple cards off a screenshot (Phase 5a).

Read-only — screen capture + OCR only, no input/memory access. Reuses the pricing
OCR stack (`vision/ocr.py`). `detect_hand` OCRs the image, anchors on the
"Room Cards" header and maps each card label below it to a room id via
`rooms.card_name_to_id` (in-game cards use alternate names: Dynamo=Generator,
Bronzeworks=Smithy, Chamber of Souls=Alchemy Lab, Sealed Vault=Treasure Vault).
Because it anchors on the header and keeps only the panel's column, it works on a
cropped Room Cards panel as well as a full game frame.
"""

from __future__ import annotations

import numpy as np

from aldur_appraiser.temple.rooms import card_name_to_id

# how wide the card column reaches from the header's left edge (ROI pixels);
# keeps the panel's card labels and drops far-away UI text (e.g. Temple Mods).
_COLUMN_WIDTH = 520

# --- medallion detection (icon template-matching) ----------------------------
# Medallion symbols are the room symbols rendered on item art, so we match each
# slot against the official room icons by Canny edges (background-invariant) at a
# few scales. A clean validated peak (Synthflesh 0.64 vs 0.20 runner-up) sits
# well above the noise floor, so this threshold separates them.
_MED_SCALES = range(36, 76, 6)
_MED_THRESHOLD = 0.42
_MED_WIN = 84  # medallion symbol+frame window at the captured resolution
_edge_templates_cache: dict[str, list] | None = None


def _edge_templates() -> dict[str, list]:
    global _edge_templates_cache
    if _edge_templates_cache is None:
        import cv2

        from aldur_appraiser.temple.icons import room_icon_paths
        from aldur_appraiser.vision import read_image

        out: dict[str, list] = {}
        for rid, path in room_icon_paths().items():
            ic = read_image(path, cv2.IMREAD_UNCHANGED)
            if ic is None:
                continue
            if ic.ndim == 3 and ic.shape[2] == 4:  # composite RGBA onto black
                alpha = ic[:, :, 3:4] / 255.0
                ic = (ic[:, :, :3] * alpha).astype("uint8")
            gray = cv2.cvtColor(ic, cv2.COLOR_BGR2GRAY)
            out[rid] = [cv2.Canny(cv2.resize(gray, (s, s)), 60, 160) for s in _MED_SCALES]
        _edge_templates_cache = out
    return _edge_templates_cache


def _best_room_match(gray_window: np.ndarray) -> tuple[str | None, float]:
    """Best (room id, score) matching a grayscale window against room icons."""
    import cv2

    win_edges = cv2.Canny(gray_window, 60, 160)
    best_rid, best = None, 0.0
    for rid, edges in _edge_templates().items():
        for tmpl in edges:
            if tmpl.shape[0] > win_edges.shape[0] or tmpl.shape[1] > win_edges.shape[1]:
                continue
            score = float(cv2.matchTemplate(win_edges, tmpl, cv2.TM_CCOEFF_NORMED).max())
            if score > best:
                best_rid, best = rid, score
    return best_rid, best


def detect_medallions(
    image: np.ndarray, *, threshold: float = _MED_THRESHOLD, engine=None
) -> list[str]:
    """Room ids granted by the medallions shown in `image` (a Medallions panel
    crop or a full frame), top to bottom. Anchors on the "Medallions" header (like
    the cards do on "Room Cards"), then matches each slot below it to a room icon
    and keeps confident peaks (de-duplicated)."""
    import cv2

    from aldur_appraiser.vision.ocr import get_engine

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    win = _MED_WIN
    if h < win or w < win:
        return []

    engine = engine or get_engine()
    # OCR a downscaled copy to find the header fast (RapidOCR upscales internally,
    # so a 5120-wide frame is very slow); scale the box back to full-res pixels.
    ocr_scale = 2560 / w if w > 2560 else 1.0
    ocr_img = cv2.resize(image, None, fx=ocr_scale, fy=ocr_scale) if ocr_scale < 1 else image
    header = next(
        (ln for ln in engine.lines(ocr_img) if "medall" in ln.text.lower() and ln.box), None
    )
    if header is not None:
        hx, hy = int(header.box[0] / ocr_scale), int(header.box[3] / ocr_scale)
        # the slots form a 2-column x 3-row grid below the header — the left column
        # starts slightly LEFT of the header text, the right column to its right.
        x_lo, x_hi = hx - 110, hx + 150
        y_lo, y_hi = hy + 20, hy + 560
    else:
        x_lo, x_hi = max(0, w - 320), w         # fallback: scan the right panel
        y_lo, y_hi = 0, h
    x_hi = min(x_hi, w - win)
    y_hi = min(y_hi, h - win)

    hits: list[tuple[int, int, float, str]] = []
    for y in range(max(0, y_lo), max(y_lo + 1, y_hi), 14):
        for x in range(max(0, x_lo), max(x_lo + 1, x_hi), 14):
            window = gray[y : y + win, x : x + win]
            if window.shape[:2] != (win, win):
                continue
            if int(cv2.Canny(window, 60, 160).sum()) < 4000:
                continue  # near-empty window -> skip the expensive match
            rid, score = _best_room_match(window)
            if rid is not None and score >= threshold:
                hits.append((y, x, score, rid))
    # 2D non-max suppression: one medallion per slot. Two medallions can share a
    # row (the panel has 2 columns), so suppress only when BOTH axes are close.
    hits.sort(key=lambda hh: -hh[2])
    kept: list[tuple[int, int, float, str]] = []
    for y, x, score, rid in hits:
        if all(abs(y - ky) > win * 0.7 or abs(x - kx) > win * 0.7 for ky, kx, _, _ in kept):
            kept.append((y, x, score, rid))
    kept.sort(key=lambda hh: (hh[0], hh[1]))  # reading order: top-to-bottom, left-to-right
    return [rid for _, _, _, rid in kept]


def detect_hand(image: np.ndarray, *, engine=None) -> list[str]:
    """Room ids of the drawn cards, top to bottom. Empty if no Room Cards panel
    is recognised. `image` is BGR (a panel crop or a full frame)."""
    from aldur_appraiser.vision.ocr import get_engine

    engine = engine or get_engine()
    lines = sorted(engine.lines(image), key=lambda ln: ln.top)
    if not lines:
        return []

    # find the "Room Cards" header to anchor the list (start + column).
    header = next((ln for ln in lines if "room card" in ln.text.lower()), None)
    if header is None:
        return []
    start_top = header.top + 1
    left = header.box[0] if header.box else 0.0

    hand: list[str] = []
    for ln in lines:
        if ln.top < start_top:
            continue
        x0 = ln.box[0] if ln.box else left
        if not (left - 50 <= x0 <= left + _COLUMN_WIDTH):
            continue  # outside the card column -> unrelated UI text
        rid = card_name_to_id(ln.text)
        if rid:
            hand.append(rid)
    return hand

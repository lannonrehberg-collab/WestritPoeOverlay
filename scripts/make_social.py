#!/usr/bin/env python3
"""Generate the GitHub social-preview banner (assets/social-preview.png, 1280x640).

Reuses the rune emblem from make_icon and adds the title/tagline. Upload it under
the repo's Settings -> Social preview.
"""

from __future__ import annotations

import glob
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from make_icon import render as render_rune  # noqa: E402

OUT = ROOT / "assets" / "social-preview.png"
W, H = 1280, 640
GOLD = (217, 200, 154, 255)
BLUE = (150, 190, 235, 255)
SUB = (150, 160, 180, 255)


def _font(bold: bool, size: int) -> ImageFont.FreeTypeFont:
    cands = glob.glob("/usr/share/fonts/**/*.ttf", recursive=True)
    # Prefer full-Latin sans families (many Noto fonts are non-Latin -> tofu).
    prefs = (
        ["liberationsans-bold.ttf", "dejavusans-bold.ttf", "notosans-bold.ttf"]
        if bold
        else ["liberationsans-regular.ttf", "dejavusans.ttf", "notosans-regular.ttf"]
    )
    by_name = {Path(f).name.lower(): f for f in cands}
    path = next((by_name[n] for n in prefs if n in by_name), None)
    if path is None:  # last resort: any non-italic sans
        path = next((f for f in cands if "sans" in f.lower() and "italic" not in f.lower()), None)
    return ImageFont.truetype(path, size) if path else ImageFont.load_default()


def main() -> None:
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # vertical gradient background
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=(int(10 + 6 * t), int(12 + 8 * t), int(20 + 12 * t), 255))

    # rune emblem on the left
    emblem = render_rune(440)
    img.alpha_composite(emblem, (90, (H - 440) // 2))

    # title + tagline
    d.text((560, 258), "Aldur Appraiser", font=_font(True, 72), fill=GOLD)
    d.text((564, 358), "PoE2 reward-valuation overlay", font=_font(False, 34), fill=BLUE)

    img.convert("RGB").save(OUT)
    print(f"wrote {OUT} ({W}x{H})")


if __name__ == "__main__":
    main()

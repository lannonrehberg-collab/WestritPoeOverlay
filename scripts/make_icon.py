#!/usr/bin/env python3
"""Generate the app icon (assets/icon.png + icon.ico) — run to regenerate.

A glowing blue rune on a dark plaque, echoing the Runeshape panel's look.
Pure Pillow so it's reproducible without design tools.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
OUT_PNG = ROOT / "assets" / "icon.png"
OUT_ICO = ROOT / "assets" / "icon.ico"

PLAQUE = (13, 16, 26, 255)       # near-black navy
BORDER = (45, 66, 110, 255)      # subtle blue edge
GLOW = (44, 120, 230, 255)       # blue glow
CORE = (150, 205, 255, 255)      # bright blue strokes


def _stroke(layer: Image.Image, segments, width: int, color) -> None:
    d = ImageDraw.Draw(layer)
    r = width / 2
    for a, b in segments:
        d.line([a, b], fill=color, width=width)
        for px, py in (a, b):  # round caps
            d.ellipse([px - r, py - r, px + r, py + r], fill=color)


def render(size: int = 256) -> Image.Image:
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = int(s * 0.06)
    d.rounded_rectangle(
        [m, m, s - m, s - m], radius=int(s * 0.20),
        fill=PLAQUE, outline=BORDER, width=max(2, s // 56),
    )

    # Ansuz rune (ᚨ): vertical stave with two parallel branches down-right.
    cx = s * 0.40
    top, bot = s * 0.24, s * 0.78
    arm = s * 0.30
    segments = [
        ((cx, top), (cx, bot)),                       # stave
        ((cx, top), (cx + arm, top + s * 0.16)),       # upper branch
        ((cx, top + s * 0.16), (cx + arm, top + s * 0.32)),  # lower branch
    ]

    w_core = max(4, int(s * 0.05))
    glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    _stroke(glow, segments, w_core + max(6, int(s * 0.045)), GLOW)
    glow = glow.filter(ImageFilter.GaussianBlur(radius=max(3, s // 36)))
    img.alpha_composite(glow)

    core = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    _stroke(core, segments, w_core, CORE)
    img.alpha_composite(core)
    return img


def main() -> None:
    OUT_PNG.parent.mkdir(parents=True, exist_ok=True)
    render(256).save(OUT_PNG)
    render(256).save(
        OUT_ICO, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    )
    print(f"wrote {OUT_PNG} and {OUT_ICO}")


if __name__ == "__main__":
    main()

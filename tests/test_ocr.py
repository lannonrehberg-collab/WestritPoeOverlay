"""OCR tests.

- A real end-to-end test on a *synthetically rendered* reward panel (italic
  serif on parchment). This exercises the actual RapidOCR engine without
  depending on the live game screenshot.
- A fixture test on the real screenshot, skipped until it is dropped into
  assets/fixtures/runeshape_01.png.

Both skip cleanly if the optional vision deps / assets are missing.
"""

from __future__ import annotations

import glob
from pathlib import Path

import pytest

cv2 = pytest.importorskip("cv2")
pytest.importorskip("rapidocr_onnxruntime")
np = pytest.importorskip("numpy")
PIL = pytest.importorskip("PIL")

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from aldur_appraiser.parse import parse_rows  # noqa: E402
from aldur_appraiser.pipeline import appraise_image  # noqa: E402
from aldur_appraiser.vision import ocr  # noqa: E402

DICT = ["Orb of Augmentation", "Orb of Transmutation", "Divine Orb", "Chaos Orb"]
FIXTURE = Path(__file__).resolve().parents[1] / "assets" / "fixtures" / "runeshape_01.png"


def _serif_italic_font(size: int) -> ImageFont.FreeTypeFont:
    cands = glob.glob("/usr/share/fonts/**/*.ttf", recursive=True)
    serif_italic = [f for f in cands if "serif" in f.lower() and "italic" in f.lower()]
    serif = [f for f in cands if "serif" in f.lower()]
    pool = serif_italic or serif or cands
    if not pool:
        pytest.skip("no truetype font available to render synthetic fixture")
    return ImageFont.truetype(pool[0], size)


def _render_panel(rows: list[str]) -> np.ndarray:
    font = _serif_italic_font(28)
    img = Image.new("RGB", (560, 40 + 52 * len(rows)), (224, 214, 188))
    draw = ImageDraw.Draw(img)
    for i, text in enumerate(rows):
        draw.text((14, 12 + 52 * i), text, fill=(40, 30, 20), font=font)
    return np.array(img)[:, :, ::-1].copy()  # RGB -> BGR


def test_ocr_reads_synthetic_reward_panel():
    img = _render_panel(["1x Orb of Augmentation", "1x Orb of Transmutation"])
    rows = ocr.read_reward_rows(img)
    assert parse_rows(rows, DICT) == [
        (1, "Orb of Augmentation"),
        (1, "Orb of Transmutation"),
    ]


def test_pipeline_on_synthetic_panel_ranks_endgame():
    img = _render_panel(["1x Divine Orb", "20x Chaos Orb"])
    prices = {"Divine Orb": 165.0, "Chaos Orb": 13.76}
    result = appraise_image(img, prices)
    # 20 * 13.76 = 275.2 ex > 165 ex
    assert result.best is not None and result.best.name == "Chaos Orb"


# Real fixtures (super-ultrawide 5120x1440 / 3437x1428). The visible currency
# reward is the bonus "1x Regal Orb"; the two main options are Uncut gems
# (non-currency, no quantity prefix) which the qty-pattern intentionally skips
# until Phase-3 row detection. Snap against the full live-style name list, not
# just the tiny DICT, since these panels contain Regal Orb.
REAL_NAMES = DICT + ["Regal Orb", "Greater Regal Orb", "Perfect Regal Orb"]
REAL_FIXTURES = [
    p for p in (FIXTURE, FIXTURE.with_name("runeshape_02.png")) if p.exists()
]


@pytest.mark.skipif(not REAL_FIXTURES, reason="real screenshot fixtures not provided yet")
@pytest.mark.parametrize("path", REAL_FIXTURES, ids=lambda p: p.name)
def test_ocr_isolates_currency_reward_from_full_screenshot(path):
    img = cv2.imread(str(path))
    assert img is not None
    options = parse_rows(ocr.read_reward_rows(img), REAL_NAMES)
    # Despite quest/chat/UI text on a 5120-wide frame, only the real currency
    # reward survives the qty-pattern + fuzzy cutoff.
    assert (1, "Regal Orb") in options

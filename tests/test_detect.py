"""Detection tests against the real fixtures + ROI geometry + capture helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

from aldur_appraiser.vision import detect as detect_mod  # noqa: E402
from aldur_appraiser.vision.capture import Region, _to_bgr  # noqa: E402
from aldur_appraiser.vision.detect import PanelBox, PanelDetector  # noqa: E402

FIXDIR = Path(__file__).resolve().parents[1] / "assets" / "fixtures"
FIXTURES = [
    p
    for p in (
        FIXDIR / "runeshape_01.png",
        FIXDIR / "runeshape_02.png",
        FIXDIR / "runeshape_03.png",
    )
    if p.exists()
]
REGAL_FIXTURES = [p for p in FIXTURES if p.name in {"runeshape_01.png", "runeshape_02.png"}]
RUNE_FIXTURE = FIXDIR / "runeshape_03.png"
NOBONUS_FIXTURE = FIXDIR / "runeshape_04.png"
TOOLTIP_FIXTURE = FIXDIR / "runeshape_06.png"
GEM_FIXTURE = FIXDIR / "runeshape_05.png"  # 8 skill/support gem options
UNIQUE_FIXTURE = FIXDIR / "runeshape_08.png"  # 4 "Unique <Class>" + 4 runes
SCROLLED_FIXTURES = [  # same panel, list scrolled to different offsets
    p for p in (FIXDIR / "runeshape_09.png", FIXDIR / "runeshape_10.png") if p.exists()
]


# --- capture helpers (no live display needed) --------------------------------


def test_to_bgr_drops_alpha():
    bgra = np.zeros((4, 5, 4), dtype=np.uint8)
    bgr = _to_bgr(bgra)
    assert bgr.shape == (4, 5, 3)


def test_region_to_mss_roundtrip():
    r = Region(10, 20, 30, 40)
    assert r.to_mss() == {"left": 10, "top": 20, "width": 30, "height": 40}


# --- ROI geometry (pure, no image) -------------------------------------------


def test_reward_roi_scales_with_header():
    panel = PanelBox(x=1085, y=140, w=267, h=68, scale=1.0, confidence=1.0)
    roi = panel.reward_roi()
    assert roi.left == 1085
    assert roi.top == 140 + 68          # one header-height below header top
    assert roi.width == int(267 * detect_mod.ROI_WIDTH_FROM_HEADER_W)
    assert roi.height == int(68 * detect_mod.ROI_HEIGHT_FROM_HEADER_H)


# --- detection on blanks -----------------------------------------------------


def test_no_panel_in_uniform_image():
    det = PanelDetector()
    blank = np.full((400, 600, 3), 30, dtype=np.uint8)
    assert det.find_panel(blank) is None


def test_no_panel_in_noise():
    det = PanelDetector()
    rng = np.random.default_rng(0)
    noise = rng.integers(0, 255, (400, 600, 3), dtype=np.uint8)
    assert det.find_panel(noise) is None


# --- detection on real fixtures ----------------------------------------------


@pytest.mark.skipif(not FIXTURES, reason="real fixtures not present")
def test_scale_lock_set_and_self_corrects():
    det = PanelDetector()
    img = cv2.imread(str(FIXTURES[0]))
    assert det.find_panel(img) is not None
    assert det._locked_scale is not None     # lock established on first detect
    assert det.find_panel(img) is not None    # fast band path still detects
    det._locked_scale = 0.45                  # stale/wrong lock
    assert det.find_panel(img) is not None    # full-sweep fallback recovers
    assert det._locked_scale != 0.45          # relocked to the real scale


@pytest.mark.skipif(not FIXTURES, reason="real fixtures not present")
@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.name)
def test_detects_panel_in_fixture(path):
    det = PanelDetector()
    img = cv2.imread(str(path))
    panel = det.find_panel(img)
    assert panel is not None
    assert panel.confidence > 0.8
    # ROI must land inside the frame and be non-empty
    crop = det.reward_image(img, panel)
    assert crop.size > 0
    assert crop.shape[0] > 0 and crop.shape[1] > 0


@pytest.mark.skipif(not REGAL_FIXTURES, reason="regal fixtures not present")
@pytest.mark.parametrize("path", REGAL_FIXTURES, ids=lambda p: p.name)
def test_roi_ocr_isolates_currency_reward(path):
    pytest.importorskip("rapidocr_onnxruntime")
    from aldur_appraiser.parse import parse_rows
    from aldur_appraiser.vision import ocr

    det = PanelDetector()
    img = cv2.imread(str(path))
    panel = det.find_panel(img)
    crop = det.reward_image(img, panel)
    rows = ocr.read_reward_rows(crop)
    options = parse_rows(rows, ["Regal Orb", "Divine Orb", "Chaos Orb"])
    assert (1, "Regal Orb") in options


@pytest.mark.skipif(not RUNE_FIXTURE.exists(), reason="rune fixture not present")
def test_roi_keeps_unknown_runes_and_finds_bonus_orb():
    pytest.importorskip("rapidocr_onnxruntime")
    from aldur_appraiser.parse import parse_rows
    from aldur_appraiser.vision import ocr

    det = PanelDetector()
    img = cv2.imread(str(RUNE_FIXTURE))
    crop = det.reward_image(img, det.find_panel(img))
    rows = ocr.read_reward_rows(crop)
    # ROI mode: every "Nx <name>" line kept; 4 runes + the bonus Artificer's Orb
    options = parse_rows(rows, ["Artificer's Orb", "Divine Orb"], keep_unknown=True)
    qtys_names = {name for _, name in options}
    assert "Artificer's Orb" in qtys_names         # qty "Ix" normalised to 1
    assert sum("Lesser" in n and "Rune" in n for n in qtys_names) >= 4


@pytest.mark.skipif(not NOBONUS_FIXTURE.exists(), reason="no-bonus fixture not present")
def test_qtyless_choice_kept_and_no_false_bonus():
    pytest.importorskip("rapidocr_onnxruntime")
    from aldur_appraiser.pipeline import appraise_roi

    det = PanelDetector()
    img = cv2.imread(str(NOBONUS_FIXTURE))
    roi = det.reward_image(img, det.find_panel(img))
    # panel has 3 choices (incl. a qty-less "Uncut Support Gem") and no bonus row
    prices = {"Blacksmith's Whetstone": 0.3, "Armourer's Scrap": 0.8}
    result = appraise_roi(roi, prices)
    assert len(result.items) == 3            # qty-less gem retained, not dropped
    assert result.bonus_items == []          # even spacing -> no bonus invented
    assert any("Uncut Support Gem" in v.name for v in result.items)


@pytest.mark.skipif(not TOOLTIP_FIXTURE.exists(), reason="tooltip fixture not present")
def test_hover_tooltip_text_excluded():
    pytest.importorskip("rapidocr_onnxruntime")
    from aldur_appraiser.pipeline import appraise_roi_rows

    det = PanelDetector()
    img = cv2.imread(str(TOOLTIP_FIXTURE))
    roi = det.reward_image(img, det.find_panel(img))
    prices = {
        "Lesser Ward Rune": 1.0,
        "Lesser Mind Rune": 0.1,
        "Artificer's Orb": 0.5,
        "Orb of Alchemy": 0.5,
    }
    names = [r.valuation.name for r in appraise_roi_rows(roi, prices)]
    # a rune-modifier tooltip ("Tempest Rune", "...Shocked or Chilled") overlaps
    # the panel; its right-misaligned text must not become an option
    assert any("Ward" in n for n in names) and any("Mind" in n for n in names)
    assert not any(w in n for n in names for w in ("Shock", "Chill", "Tempest", "gain"))


@pytest.mark.skipif(not GEM_FIXTURE.exists(), reason="gem fixture not present")
def test_skill_support_gems_not_falsely_priced():
    pytest.importorskip("rapidocr_onnxruntime")
    from aldur_appraiser.pipeline import appraise_roi_rows

    det = PanelDetector()
    img = cv2.imread(str(GEM_FIXTURE))
    roi = det.reward_image(img, det.find_panel(img))
    # these are exactly the currencies the gem names used to falsely snap to
    prices = {"Verisium": 0.0004, "Orb of Annulment": 97.0}
    rows = appraise_roi_rows(roi, prices)

    gems = [r for r in rows if r.valuation.name.lower().startswith(("skill:", "support:"))]
    assert len(gems) >= 4
    assert all(not r.valuation.known for r in gems)         # gems must stay "?"
    assert not any(r.valuation.name == "Orb of Annulment" for r in rows)  # no 97ex misfire


@pytest.mark.skipif(len(SCROLLED_FIXTURES) < 2, reason="scrolled fixtures not present")
@pytest.mark.parametrize("path", SCROLLED_FIXTURES, ids=lambda p: p.name)
def test_detects_panel_regardless_of_list_scroll(path):
    # A long reward list can be scrolled. The header template's lower strip
    # overlaps the first row / scrollbar, which changes with scroll and used to
    # drop the match below threshold (scrolled panel went undetected). Matching
    # only the stable title band must detect the panel at any scroll offset.
    det = PanelDetector()
    img = cv2.imread(str(path))
    panel = det.find_panel(img)
    assert panel is not None
    assert panel.confidence > 0.8
    assert det.reward_image(img, panel).size > 0


@pytest.mark.skipif(not UNIQUE_FIXTURE.exists(), reason="unique fixture not present")
def test_unique_items_not_falsely_priced():
    pytest.importorskip("rapidocr_onnxruntime")
    from aldur_appraiser.pipeline import appraise_roi_rows

    det = PanelDetector()
    img = cv2.imread(str(UNIQUE_FIXTURE))
    roi = det.reward_image(img, det.find_panel(img))
    prices = {"Exalted Orb": 1.0, "Divine Orb": 200.0, "Greater Inspiration Rune": 0.5}
    rows = appraise_roi_rows(roi, prices)

    uniques = [r for r in rows if r.valuation.name.lower().startswith("unique ")]
    assert len(uniques) >= 4
    assert all(not r.valuation.known for r in uniques)      # uniques must stay "?"
    # the priceable runes in the same panel are still valued
    assert any(r.valuation.known for r in rows)
    # the ROI must be tall enough to capture the whole 11-option list, not just
    # the first ~8 rows (regression guard for ROI_HEIGHT_FROM_HEADER_H)
    assert len(rows) == 11

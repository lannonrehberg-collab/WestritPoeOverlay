"""Read the drawn temple cards off the real screenshot fixtures (needs OCR).

Fixtures live in assets/fixtures (gitignored, local-only) — these tests skip when
they're absent, like test_detect.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

cv2 = pytest.importorskip("cv2")

from aldur_appraiser.temple.vision import detect_hand, detect_medallions  # noqa: E402

FIXDIR = Path(__file__).resolve().parents[1] / "assets" / "fixtures"
CARDS = FIXDIR / "temple_cards.png"
FULL = FIXDIR / "temple_full.png"
MEDALLIONS = FIXDIR / "temple_medallions.png"
MEDALLIONS_2COL = FIXDIR / "temple_medallions_02.jpg"
MEDALLIONS_SMITHY = FIXDIR / "temple_medallions_03.png"

EXPECTED = ["path", "generator", "treasure_vault", "path", "smithy", "path"]


def _has_room_icons() -> bool:
    from aldur_appraiser.temple.icons import room_icon_paths

    return bool(room_icon_paths())


@pytest.mark.skipif(not CARDS.exists(), reason="temple card fixture not present")
def test_detect_hand_reads_the_room_cards_panel():
    pytest.importorskip("rapidocr_onnxruntime")
    assert detect_hand(cv2.imread(str(CARDS))) == EXPECTED


@pytest.mark.skipif(not FULL.exists(), reason="full temple fixture not present")
def test_detect_hand_works_on_the_full_frame():
    pytest.importorskip("rapidocr_onnxruntime")
    # the same cards must be found even on the whole console screenshot
    assert detect_hand(cv2.imread(str(FULL)))[:6] == EXPECTED


@pytest.mark.skipif(not MEDALLIONS.exists(), reason="medallions fixture not present")
def test_detect_medallions_matches_room_icons():
    pytest.importorskip("rapidocr_onnxruntime")
    if not _has_room_icons():
        pytest.skip("room icons unavailable (offline) — needed as match templates")
    # the held medallion's symbol is the Synthflesh Lab room icon
    assert detect_medallions(cv2.imread(str(MEDALLIONS))) == ["synthflesh_lab"]


@pytest.mark.skipif(not MEDALLIONS_2COL.exists(), reason="2-column medallions fixture not present")
def test_detect_medallions_in_a_two_column_grid():
    pytest.importorskip("rapidocr_onnxruntime")
    if not _has_room_icons():
        pytest.skip("room icons unavailable (offline) — needed as match templates")
    # 4/6 panel: a Smithy room medallion (left column) plus three "+1 tier" arrow
    # medallions (right column) which don't match a room icon and are ignored.
    assert detect_medallions(cv2.imread(str(MEDALLIONS_2COL))) == ["smithy"]


@pytest.mark.skipif(not MEDALLIONS_SMITHY.exists(), reason="smithy medallion fixture not present")
def test_detect_medallions_smithy_not_golem_works():
    pytest.importorskip("rapidocr_onnxruntime")
    if not _has_room_icons():
        pytest.skip("room icons unavailable (offline) — needed as match templates")
    # the Smithy box medallion must match Smithy, not the similar Golem Works
    assert detect_medallions(cv2.imread(str(MEDALLIONS_SMITHY))) == ["smithy"]

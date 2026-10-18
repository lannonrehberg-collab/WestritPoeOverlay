"""Orchestration-loop tests: step(), frame-diff gate, panel show/hide, render."""

from __future__ import annotations

import pytest

pytest.importorskip("cv2")
import numpy as np  # noqa: E402

from aldur_appraiser.app import AppraiserLoop, render_console  # noqa: E402
from aldur_appraiser.vision.ocr import OcrLine  # noqa: E402

PRICES = {"Divine Orb": 165.0, "Artificer's Orb": 0.46}


class _FakePanel:
    def reward_roi(self):
        from aldur_appraiser.vision.capture import Region

        return Region(0, 0, 10, 10)


class FakeDetector:
    """Reports a panel when `present` is set; ROI is the frame itself."""

    def __init__(self):
        self.present = True

    def find_panel(self, frame):  # noqa: ARG002
        return _FakePanel() if self.present else None

    def reward_image(self, frame, panel):  # noqa: ARG002
        return frame


class FakeEngine:
    def __init__(self, lines):
        self._lines = [OcrLine(text=t, conf=0.9, top=float(i)) for i, t in enumerate(lines)]

    def lines(self, img):  # noqa: ARG002
        return self._lines


def _make_loop(detector, engine, results, hides):
    return AppraiserLoop(
        prices=PRICES,
        detector=detector,
        engine=engine,
        on_result=lambda a: results.append(a.result),
        on_hide=lambda: hides.append(True),
    )


def _frame(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(0, 255, (200, 300, 3), dtype=np.uint8)


def test_step_evaluates_and_reports():
    results, hides = [], []
    engine = FakeEngine(["1x Divine Orb", "1x Lesser Storm Rune"])
    loop = _make_loop(FakeDetector(), engine, results, hides)
    res = loop.step(_frame(1))
    assert res is not None
    assert res.best.name == "Divine Orb"
    assert res.incomplete is True
    assert len(results) == 1


def test_frame_diff_gate_skips_unchanged():
    results, hides = [], []
    loop = _make_loop(FakeDetector(), FakeEngine(["1x Divine Orb"]), results, hides)
    frame = _frame(2)
    assert loop.step(frame) is not None      # first: evaluated
    assert loop.step(frame) is None          # identical ROI: skipped
    assert len(results) == 1


def test_changed_roi_reevaluates():
    results, hides = [], []
    loop = _make_loop(FakeDetector(), FakeEngine(["1x Divine Orb"]), results, hides)
    assert loop.step(_frame(3)) is not None
    assert loop.step(_frame(99)) is not None  # different content -> re-evaluated
    assert len(results) == 2


def test_panel_hide_callback_fires_once():
    results, hides = [], []
    det = FakeDetector()
    loop = _make_loop(det, FakeEngine(["1x Divine Orb"]), results, hides)
    loop.step(_frame(4))          # panel visible
    det.present = False
    loop.step(_frame(5))          # panel gone -> hide
    loop.step(_frame(6))          # still gone -> no duplicate hide
    assert len(hides) == 1


def test_render_console_marks_best_and_incomplete():
    from aldur_appraiser.pricing.valuation import evaluate

    result = evaluate([(1, "Divine Orb"), (1, "Lesser Storm Rune")], PRICES)
    text = render_console(result, base="exalted")
    assert "Divine Orb" in text and "BEST" in text
    assert "unknown" in text
    assert "incomplete" in text

"""Overlay tests: HTML rendering (pure) + optional widget construction."""

from __future__ import annotations

import os

import pytest

from aldur_appraiser.pricing.valuation import evaluate
from aldur_appraiser.vision.overlay import result_to_html

PRICES = {"Divine Orb": 165.0, "Chaos Orb": 13.76}


def test_html_marks_best_and_unknown_and_incomplete():
    # "Mystery Rune" is absent from the price table -> unknown -> incomplete
    result = evaluate([(1, "Divine Orb"), (1, "Mystery Rune")], PRICES)
    html = result_to_html(result, "exalted")
    assert "Divine Orb" in html
    assert "Mystery Rune" in html
    assert "incomplete" in html
    assert "Aldur Appraiser" in html


def test_html_empty_result():
    result = evaluate([], PRICES)
    html = result_to_html(result, "exalted")
    assert "no reward options" in html


def test_html_stale_flag():
    result = evaluate([(1, "Divine Orb")], PRICES)
    assert "stale" in result_to_html(result, "exalted", stale=True)


@pytest.mark.skipif(not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"),
                    reason="no display for Qt widget test")
def test_build_overlay_constructs_and_renders():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from aldur_appraiser.vision.overlay import build_overlay

    app = QApplication.instance() or QApplication([])
    overlay = build_overlay("exalted")
    result = evaluate([(1, "Divine Orb"), (20, "Chaos Orb")], PRICES)
    overlay.post_result(result, False)
    app.processEvents()  # deliver the queued signal -> slot
    overlay.post_hide()
    app.processEvents()
    overlay.close()

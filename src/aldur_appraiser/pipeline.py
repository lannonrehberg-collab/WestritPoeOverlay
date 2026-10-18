"""Glue: image -> OCR -> parse -> valuation.

This is the game-independent end of the orchestration loop (app.py adds capture
+ detection on top). Running it on a full panel image works even without ROI
cropping: the "<qty>x <name>" pattern plus the fuzzy cutoff naturally isolate
reward lines from surrounding UI text. ROI cropping (detect.py) is a Phase-3
robustness/perf optimisation, not a correctness requirement here.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, replace
from typing import Protocol

import numpy as np
from rapidfuzz import fuzz

from aldur_appraiser.parse import parse_row, parse_rows
from aldur_appraiser.pricing.client import PriceTable
from aldur_appraiser.pricing.valuation import EvalResult, Valuation, evaluate, value_one
from aldur_appraiser.vision.ocr import OcrEngine, OcrLine, get_engine, read_reward_rows


@dataclass(frozen=True)
class RowAppraisal:
    """One reward row: its valuation plus the OCR box (ROI pixels) for inline UI."""

    valuation: Valuation
    box: tuple[float, float, float, float] | None


@dataclass(frozen=True)
class Appraisal:
    """Loop payload: ranked result (corner/console), per-row data (inline), and
    the anchor (ROI rect + frame size) to map rows to the screen."""

    result: EvalResult
    rows: list[RowAppraisal]
    anchor: tuple[int, int, int, int, int, int]

# A reward row separated from the ones above by more than this multiple of the
# typical row spacing is the bonus reward (sits below a "Bonus Reward" divider).
_BONUS_GAP_FACTOR = 1.6


class Detector(Protocol):
    """Duck-typed PanelDetector (kept here so pipeline stays cv2-free to import)."""

    def find_panel(self, frame: np.ndarray): ...
    def reward_image(self, frame: np.ndarray, panel) -> np.ndarray: ...


def _is_bonus_label(text: str) -> bool:
    return fuzz.ratio(text.lower(), "bonus reward") >= 75


def _is_ui_label(text: str) -> bool:
    """Non-reward chrome that may land in the ROI (the panel header)."""
    return fuzz.ratio(text.lower(), "runeshape combinations") >= 80


def right_aligned_lines(lines: list[OcrLine], roi_width: int, *, tol_frac: float = 0.12):
    """Keep only lines flush with the reward column's right margin.

    Reward names are right-aligned to a consistent edge; hover tooltips (rune
    modifier popups) sit further left, so dropping lines whose right edge is well
    left of the dominant margin removes tooltip text without touching rewards.
    Lines without a box (e.g. test fakes) are kept.
    """
    boxed = [ln for ln in lines if ln.box is not None]
    if len(boxed) < 2:
        return lines
    right = max(ln.box[2] for ln in boxed)
    tol = tol_frac * roi_width
    return [ln for ln in lines if ln.box is None or ln.box[2] >= right - tol]


def split_bonus(lines: list[OcrLine]) -> tuple[list[OcrLine], list[OcrLine]]:
    """Split reward rows into (choices, bonus).

    The bonus reward is always paid, so it must not be ranked. It's identified by
    a "Bonus Reward" divider label, or failing that by the large vertical gap
    that divider leaves before the final row.
    """
    rows = sorted(lines, key=lambda ln: ln.top)
    # 1) explicit divider label -> everything after it is bonus
    for i, ln in enumerate(rows):
        if _is_bonus_label(ln.text):
            return rows[:i], rows[i + 1 :]
    # 2) gap heuristic: the last row, if clearly separated, is the bonus
    if len(rows) >= 2:
        gaps = [rows[i + 1].top - rows[i].top for i in range(len(rows) - 1)]
        if len(gaps) >= 2:
            typical = statistics.median(gaps[:-1])
            if typical > 0 and gaps[-1] > _BONUS_GAP_FACTOR * typical:
                return rows[:-1], [rows[-1]]
    return rows, []


def appraise_roi_rows(
    roi: np.ndarray,
    prices: PriceTable,
    *,
    engine: OcrEngine | None = None,
    score_cutoff: int = 80,
) -> list[RowAppraisal]:
    """Per-row appraisal in visual (top-to-bottom) order, keeping OCR boxes.

    Single OCR pass; the bonus row is valued but flagged is_bonus and excluded
    from the best-choice marking. Used by the inline overlay and as the basis
    for appraise_roi()."""
    engine = engine or get_engine()
    lines = [ln for ln in engine.lines(roi) if ln.text and not _is_ui_label(ln.text)]
    lines = right_aligned_lines(lines, roi.shape[1])
    choice_lines, bonus_lines = split_bonus(lines)
    keys = list(prices.keys())

    rows: list[RowAppraisal] = []
    choices: list[Valuation] = []
    for ln, is_bonus in [(c, False) for c in choice_lines] + [(b, True) for b in bonus_lines]:
        opt = parse_row(ln.text, keys, score_cutoff=score_cutoff, keep_unknown=True)
        if opt is None:
            continue
        qty, name = opt
        v = value_one(qty, name, prices, is_bonus=is_bonus)
        rows.append(RowAppraisal(valuation=v, box=ln.box))
        if not is_bonus:
            choices.append(v)

    known = [v for v in choices if v.known]
    if known:
        best = max(known, key=lambda v: v.total)
        rows = [
            replace(r, valuation=replace(r.valuation, is_best=True)) if r.valuation is best else r
            for r in rows
        ]
    return rows


def rows_to_result(rows: list[RowAppraisal]) -> EvalResult:
    """Build the ranked EvalResult (corner HUD / console) from per-row data."""
    choices = [r.valuation for r in rows if not r.valuation.is_bonus]
    bonus = [r.valuation for r in rows if r.valuation.is_bonus]
    known = sorted((v for v in choices if v.known), key=lambda v: v.total, reverse=True)
    unknown = [v for v in choices if not v.known]
    return EvalResult(items=known + unknown, incomplete=bool(unknown), bonus_items=bonus)


def appraise_roi(
    roi: np.ndarray,
    prices: PriceTable,
    *,
    engine: OcrEngine | None = None,
    score_cutoff: int = 80,
) -> EvalResult:
    """OCR a reward-ROI, separate the always-paid bonus, value and rank choices."""
    rows = appraise_roi_rows(roi, prices, engine=engine, score_cutoff=score_cutoff)
    return rows_to_result(rows)


def appraise_image(
    image: np.ndarray,
    prices: PriceTable,
    *,
    engine: OcrEngine | None = None,
    detector: Detector | None = None,
    score_cutoff: int = 80,
) -> EvalResult:
    """OCR an image, parse reward options, value and rank them.

    If a detector is given and finds the panel, OCR runs on the reward ROI and
    every "<qty>x <name>" line is treated as a real reward (unknown currency
    kept -> known=False -> incomplete). Without a panel we fall back to OCRing
    the whole frame, where the price dictionary acts as the noise filter.
    """
    if detector is not None:
        panel = detector.find_panel(image)
        if panel is not None:
            roi = detector.reward_image(image, panel)
            return appraise_roi(roi, prices, engine=engine, score_cutoff=score_cutoff)

    # No panel: OCR the whole frame; the price dictionary filters noise and we
    # can't reliably locate a bonus divider, so don't split one out.
    rows = read_reward_rows(image, engine=engine)
    options = parse_rows(rows, prices.keys(), score_cutoff=score_cutoff, keep_unknown=False)
    return evaluate(options, prices)

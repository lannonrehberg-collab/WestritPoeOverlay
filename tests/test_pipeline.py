"""Pipeline glue tests with a fake OCR engine (no OCR dependency needed)."""

from __future__ import annotations

import numpy as np

from aldur_appraiser.pipeline import appraise_image
from aldur_appraiser.vision.ocr import OcrLine

PRICES = {"Divine Orb": 165.0, "Chaos Orb": 13.76, "Orb of Augmentation": 0.05}


class FakeEngine:
    def __init__(self, lines: list[str]):
        self._lines = [OcrLine(text=t, conf=0.9, top=float(i)) for i, t in enumerate(lines)]

    def lines(self, img):  # noqa: ARG002
        return self._lines


def _blank():
    return np.zeros((10, 10, 3), dtype=np.uint8)


def test_pipeline_filters_noise_and_ranks():
    engine = FakeEngine(
        [
            "Runeshape Combinations",        # header noise -> dropped
            "1x Orb of Augmentation",
            "20x Chaos Orb",
            "CRUDE BOW",                     # world text noise -> dropped
            "1x Divine Orb",
        ]
    )
    result = appraise_image(_blank(), PRICES, engine=engine)

    names = [v.name for v in result.items]
    assert "Divine Orb" in names and "Chaos Orb" in names
    # 20x chaos = 275.2 ex beats 1 divine = 165 ex
    assert result.best.name == "Chaos Orb"
    assert result.incomplete is False


def test_pipeline_marks_unknown_incomplete():
    engine = FakeEngine(["1x Divine Orb", "1x Mystery Relic"])
    result = appraise_image(_blank(), PRICES, engine=engine)
    # No detector -> whole-frame mode -> dictionary is the noise filter, so
    # "Mystery Relic" is dropped and isn't even an option.
    assert [v.name for v in result.items] == ["Divine Orb"]
    assert result.incomplete is False


class FakeDetector:
    """Returns a panel and passes the frame through as the reward ROI."""

    def find_panel(self, frame):  # noqa: ARG002
        return object()

    def reward_image(self, frame, panel):  # noqa: ARG002
        return frame


def test_split_bonus_by_label():
    from aldur_appraiser.pipeline import split_bonus

    lines = [
        OcrLine("1x Lesser Storm Rune", 0.9, 0),
        OcrLine("1x Lesser Iron Rune", 0.9, 60),
        OcrLine("Bonus Reward", 0.8, 120),
        OcrLine("1x Artificer's Orb", 0.9, 180),
    ]
    choices, bonus = split_bonus(lines)
    assert [c.text for c in choices] == ["1x Lesser Storm Rune", "1x Lesser Iron Rune"]
    assert [b.text for b in bonus] == ["1x Artificer's Orb"]


def test_split_bonus_by_gap():
    from aldur_appraiser.pipeline import split_bonus

    # even spacing for choices, then a big gap before the bonus row
    lines = [
        OcrLine("1x Lesser Storm Rune", 0.9, 0),
        OcrLine("1x Lesser Desert Rune", 0.9, 62),
        OcrLine("1x Lesser Iron Rune", 0.9, 124),
        OcrLine("1x Artificer's Orb", 0.9, 280),  # ~2.5x the typical gap below
    ]
    choices, bonus = split_bonus(lines)
    assert [b.text for b in bonus] == ["1x Artificer's Orb"]
    assert len(choices) == 3


def test_right_aligned_lines_drops_tooltip():
    from aldur_appraiser.pipeline import right_aligned_lines

    lines = [
        OcrLine("1x Lesser Ward Rune", 0.9, 12, (248, 12, 462, 30)),
        OcrLine("Cannot be Shocked or Chilled", 0.9, 147, (2, 147, 246, 165)),
        OcrLine("All Damage can Shock and Chill", 0.9, 175, (2, 175, 269, 193)),
        OcrLine("1x Artificer's Orb", 0.9, 136, (288, 136, 461, 156)),
    ]
    texts = [ln.text for ln in right_aligned_lines(lines, 520)]
    assert "1x Lesser Ward Rune" in texts and "1x Artificer's Orb" in texts
    assert "Cannot be Shocked or Chilled" not in texts
    assert "All Damage can Shock and Chill" not in texts


def test_split_bonus_none_when_even():
    from aldur_appraiser.pipeline import split_bonus

    lines = [OcrLine("1x A", 0.9, 0), OcrLine("1x B", 0.9, 60), OcrLine("1x C", 0.9, 120)]
    choices, bonus = split_bonus(lines)
    assert bonus == []
    assert len(choices) == 3


def test_pipeline_roi_mode_keeps_unknown_rewards():
    # In ROI mode every "Nx <name>" line is a real reward: unknown ones are
    # kept (known=False) and flag the comparison incomplete.
    engine = FakeEngine(
        [
            "1x Divine Orb",
            "1x Lesser Storm Rune",
            "1x Lesser Glacial Rune",
            "Bonus Reward",   # no qty -> still dropped
        ]
    )
    result = appraise_image(_blank(), PRICES, engine=engine, detector=FakeDetector())

    names = [v.name for v in result.items]
    assert "Divine Orb" in names
    assert "Lesser Storm Rune" in names and "Lesser Glacial Rune" in names
    assert "Bonus Reward" not in names
    assert result.best.name == "Divine Orb"     # only valuable option
    assert result.incomplete is True            # two unvaluable rewards present

"""Tests for parse.py — regex qty extraction + fuzzy name snapping."""

from __future__ import annotations

from aldur_appraiser.parse import parse_row, parse_rows, snap_name

DICT = [
    "Orb of Augmentation",
    "Orb of Transmutation",
    "Divine Orb",
    "Chaos Orb",
    "Exalted Orb",
]


def test_parse_clean_row():
    assert parse_row("1x Orb of Augmentation", DICT) == (1, "Orb of Augmentation")


def test_parse_handles_spacing_and_case():
    assert parse_row("20 X Chaos Orb", DICT) == (20, "Chaos Orb")
    assert parse_row("3X Divine Orb", DICT) == (3, "Divine Orb")


def test_fuzzy_fixes_ocr_slip():
    # classic italic/serif OCR slip: rn -> m
    assert parse_row("1x Orb of Augrnentation", DICT) == (1, "Orb of Augmentation")


def test_below_cutoff_returns_none():
    # gibberish name should not snap to anything
    assert parse_row("1x Zzqwx Blarg", DICT) is None


def test_no_quantity_returns_none():
    assert parse_row("Orb of Augmentation", DICT) is None


def test_absurd_quantity_rejected():
    assert parse_row("999999999x Divine Orb", DICT) is None


def test_empty_and_garbage():
    assert parse_row("", DICT) is None
    assert parse_row("   ", DICT) is None


def test_snap_name_direct():
    assert snap_name("Divin Orb", DICT) == "Divine Orb"
    assert snap_name("totally unknown thing", DICT) is None


def test_qty_normalises_confused_digits():
    # italic-serif OCR slips: "1x" -> "Ix" / "ix" / "lx"
    assert parse_row("Ix Divine Orb", DICT) == (1, "Divine Orb")
    assert parse_row("ix Chaos Orb", DICT) == (1, "Chaos Orb")
    assert parse_row("lx Divine Orb", DICT) == (1, "Divine Orb")


def test_keep_unknown_retains_real_reward_rows():
    # inside a detected ROI, a parseable line that doesn't snap is a real but
    # unvaluable reward (kept), not noise (dropped)
    assert parse_row("1x Lesser Storm Rune", DICT, keep_unknown=True) == (1, "Lesser Storm Rune")
    # without keep_unknown the same line is dropped
    assert parse_row("1x Lesser Storm Rune", DICT) is None


def test_keep_unknown_keeps_qtyless_reward():
    # inside the ROI a reward without a quantity prefix (e.g. a gem) is qty 1,
    # not dropped (UI labels are filtered out at the pipeline level instead)
    assert parse_row("Uncut Support Gem", DICT, keep_unknown=True) == (1, "Uncut Support Gem")
    # full-frame mode still requires the "Nx" anchor as a noise filter
    assert parse_row("Uncut Support Gem", DICT) is None


def test_unleveled_gem_stays_unknown():
    dictionary = DICT + ["Uncut Support Gem (Level 3)", "Uncut Support Gem (Level 5)"]
    # no level shown -> don't guess a (Level N) variant; keep the raw name (unknown)
    assert parse_row("Uncut Support Gem", dictionary, keep_unknown=True) == (
        1,
        "Uncut Support Gem",
    )
    # a level IS shown -> snapping to the matching leveled variant is fine
    assert parse_row("1x Uncut Support Gem (Level 5)", dictionary) == (
        1,
        "Uncut Support Gem (Level 5)",
    )


def test_untracked_name_does_not_snap_to_unrelated_rune():
    # an untracked / mis-OCR'd name must NOT fuzzy-match an unrelated currency
    # that merely shares the "Rune" token (WRatio scored these ~86, above the
    # cutoff, and showed a bogus price; ratio scores them ~50 -> rejected).
    d = ["Perfect Rebirth Rune", "Ancient Rune of Animosity", "Glacial Rune"]
    assert snap_name("Glacier Rune", d[:2]) is None          # no Glacial -> "?"
    # but a real OCR slip of the tracked name still snaps to it
    assert snap_name("Glacier Rune", d) == "Glacial Rune"
    # inside the ROI such a line is kept as unknown rather than mispriced
    assert parse_row("1x Glacier Rune", d[:2], keep_unknown=True) == (1, "Glacier Rune")


def test_base_tier_rune_snaps_to_base_not_a_tier_variant():
    # "Storm Rune" must map to the base entry, not Lesser/Greater/Perfect, even
    # though those all contain "Storm Rune" (exact match beats the tiered ones).
    d = ["Storm Rune", "Lesser Storm Rune", "Greater Storm Rune", "Perfect Storm Rune"]
    assert snap_name("Storm Rune", d) == "Storm Rune"
    assert parse_row("1x Storm Rune", d) == (1, "Storm Rune")


def test_skill_support_gems_stay_unknown():
    # gem names fuzzy-match currencies by accident (e.g. "… Verisium" -> Verisium,
    # "Remnants of Kalguur" -> Orb of Annulment); the Skill:/Support: rule prevents
    # pricing them and keeps them "?"
    d = DICT + ["Verisium", "Orb of Annulment"]
    assert parse_row("Skill: Powered by Verisium", d, keep_unknown=True) == (
        1,
        "Skill: Powered by Verisium",
    )
    assert parse_row("Skill: Remnants of Kalguur", d, keep_unknown=True) == (
        1,
        "Skill: Remnants of Kalguur",
    )
    assert parse_row("Support: Scouring Flame", d, keep_unknown=True) == (
        1,
        "Support: Scouring Flame",
    )
    # full-frame mode drops them entirely
    assert parse_row("Skill: Grim Pillars", d) is None


def test_unique_items_stay_unknown():
    # unique rewards render as "Unique <ItemClass>" (the specific unique is only
    # rolled on pick); the class isn't a currency and must never be priced -> "?"
    d = DICT + ["Exalted Orb"]
    assert parse_row("Unique Shield", d, keep_unknown=True) == (1, "Unique Shield")
    assert parse_row("Unique Sceptre", d, keep_unknown=True) == (1, "Unique Sceptre")
    assert parse_row("1x Unique Focus", d, keep_unknown=True) == (1, "Unique Focus")
    # full-frame mode drops them entirely
    assert parse_row("Unique Quiver", d) is None


def test_keep_unknown_rejects_tooltip_modifier_text():
    # hover tooltips OCR as modifier text; quantity-less mod lines must not
    # become options (they carry digits/%/+ or run long)
    assert parse_row("+12% to Lightning Resistance", DICT, keep_unknown=True) is None
    assert parse_row("Grants 25% increased Rarity", DICT, keep_unknown=True) is None
    long_mod = "adds one to maximum number of allocated runes"
    assert parse_row(long_mod, DICT, keep_unknown=True) is None
    # but a quantity-anchored row with a stray number is still kept
    assert parse_row("20x Chaos Orb", DICT) == (20, "Chaos Orb")


def test_parse_rows_drops_unparseable():
    rows = [
        "1x Orb of Augmentation",
        "garbage line",
        "2x Divine Orb",
        "",
    ]
    assert parse_rows(rows, DICT) == [
        (1, "Orb of Augmentation"),
        (2, "Divine Orb"),
    ]

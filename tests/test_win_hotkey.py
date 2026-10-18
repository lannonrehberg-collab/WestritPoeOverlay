"""Parse GTK accelerators into Win32 RegisterHotKey (modifiers, vk) pairs.

Only the parser is platform-independent and unit-testable here; registering an
actual hotkey needs a Windows message loop, so that's exercised live on Windows.
"""

from __future__ import annotations

from aldur_appraiser.win_hotkey import (
    _MOD_ALT,
    _MOD_CONTROL,
    _MOD_NOREPEAT,
    _MOD_SHIFT,
    _MOD_WIN,
    parse_accelerator,
)


def test_parses_ctrl_alt_letter():
    mods, vk = parse_accelerator("<Control><Alt>p")
    assert mods == (_MOD_CONTROL | _MOD_ALT | _MOD_NOREPEAT)
    assert vk == ord("P")


def test_primary_is_control_and_super_is_win():
    mods, _ = parse_accelerator("<Primary><Super>k")
    assert mods == (_MOD_CONTROL | _MOD_WIN | _MOD_NOREPEAT)


def test_shift_and_digit_key():
    mods, vk = parse_accelerator("<Shift><Control>5")
    assert mods == (_MOD_SHIFT | _MOD_CONTROL | _MOD_NOREPEAT)
    assert vk == ord("5")


def test_function_key():
    _, vk = parse_accelerator("<Alt>F9")
    assert vk == 0x70 + 8  # VK_F9


def test_named_key():
    _, vk = parse_accelerator("<Control>space")
    assert vk == 0x20


def test_bare_key_without_modifier_is_allowed():
    # a single key (no modifier) is a valid hotkey — e.g. the user wants just "0"
    mods, vk = parse_accelerator("0")
    assert mods == _MOD_NOREPEAT  # only NOREPEAT, no modifier bits
    assert vk == ord("0")


def test_unknown_modifier_rejected():
    assert parse_accelerator("<Bogus>p") is None


def test_empty_key_rejected():
    assert parse_accelerator("<Control><Alt>") is None

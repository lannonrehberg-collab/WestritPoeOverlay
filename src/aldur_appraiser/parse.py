"""Turn raw OCR reward lines into (qty, canonical_name) options.

The reward is text-only (e.g. "1x Orb of Augmentation"); the icons left of it
are the runeshape combination (recipe input), not the reward. So we OCR the
text, regex out the quantity, and fuzzy-snap the name against the known
currency list (the PriceTable keys) — which fixes OCR/italic slips like
"Orb of Augrnentation" -> "Orb of Augmentation". Anything below the fuzzy
cutoff returns None: better nothing than a wrong match.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from rapidfuzz import fuzz, process

# "1x Orb of Augmentation", "20 x Chaos Orb", "3X Divine Orb".
# The quantity token allows letters the OCR confuses with digits on the italic
# serif font ("1x" -> "Ix"/"ix"/"lx", "0" -> "O"); they're normalised below.
ROW_RE = re.compile(r"([0-9IilOo]+)\s*[xX×]\s*(.+)")
_QTY_FIX = str.maketrans({"I": "1", "i": "1", "l": "1", "O": "0", "o": "0"})

DEFAULT_CUTOFF = 80
# Reject absurd quantities (OCR garbage); real stacks stay well under this.
MAX_QTY = 100_000


def _parse_qty(token: str) -> int | None:
    fixed = token.translate(_QTY_FIX)
    return int(fixed) if fixed.isdigit() else None


# Hover tooltips show modifier text ("+12% to Lightning Resistance", "Grants
# 25% increased ..."); such lines carry digits/%/+ or run long, unlike the short
# Title-Case reward names. Used only to reject quantity-less lines.
_MOD_CHARS = re.compile(r"[0-9%+]")


def _is_mod_like(text: str) -> bool:
    return bool(_MOD_CHARS.search(text)) or len(text.split()) > 6


def _unleveled_gem(snapped: str, raw_name: str) -> bool:
    """A gem priced per level ('… (Level N)') but whose reward text shows no
    level. We won't guess the level -> treat as unknown ('?') instead."""
    return "(level" in snapped.lower() and not any(c.isdigit() for c in raw_name)


def _is_gem_reward(name: str) -> bool:
    """Skill/Support gem rewards ('Skill: …' / 'Support: …'). Their value depends
    on level/quality, which the panel doesn't show, and the names fuzzy-match
    currencies by accident (e.g. '… Verisium' -> Verisium) -> never price; '?'."""
    return name.lower().startswith(("skill:", "support:"))


def _is_unique_reward(name: str) -> bool:
    """Unique-item rewards render as 'Unique <ItemClass>' (e.g. 'Unique Shield',
    'Unique Sceptre') because the specific unique is only rolled when picked. The
    item class isn't a currency and its value is unknowable here, so never price
    it -> keep as unknown ('?')."""
    return name.lower().startswith("unique ")


def parse_row(
    raw: str,
    dictionary: Iterable[str],
    *,
    score_cutoff: int = DEFAULT_CUTOFF,
    keep_unknown: bool = False,
) -> tuple[int, str] | None:
    """Parse one OCR line into (qty, name), or None if unusable.

    With keep_unknown=False the name must fuzzy-snap to the dictionary (used on
    full frames, where the dictionary doubles as a noise filter). With
    keep_unknown=True a parseable "<qty>x <name>" line that doesn't snap keeps
    its cleaned raw name (used inside the detected reward ROI, where every such
    line is a genuine reward we just can't price -> later valued known=False).
    """
    if not raw:
        return None
    s = raw.strip()
    match = ROW_RE.search(s)
    if match:
        qty = _parse_qty(match.group(1))
        if qty is None or qty < 1 or qty > MAX_QTY:
            return None
        raw_name = match.group(2)
        had_qty = True
    elif keep_unknown:
        # Inside the reward ROI every row is an option; some (e.g. gems) have no
        # "Nx" prefix -> treat as quantity 1 rather than dropping them.
        qty = 1
        raw_name = s
        had_qty = False
    else:
        # Full-frame mode: the "Nx" anchor is our noise filter -> require it.
        return None

    raw_name = raw_name.strip().strip(".,:;")
    if not raw_name:
        return None
    # A quantity-less line that looks like modifier text (e.g. a hover tooltip:
    # "+12% to Lightning Resistance") is not a reward. Reject it *before* snapping
    # so it can't fuzzy-match a currency name by accident.
    if not had_qty and _is_mod_like(raw_name):
        return None
    # Skill/Support gems and unique items: don't price (value not knowable from
    # the panel) and don't let them fuzzy-snap to a currency -> keep as "?".
    if _is_gem_reward(raw_name) or _is_unique_reward(raw_name):
        return (qty, raw_name) if (keep_unknown and len(raw_name) >= 3) else None
    name = snap_name(raw_name, dictionary, score_cutoff=score_cutoff)
    if name is not None and not _unleveled_gem(name, raw_name):
        return qty, name
    if keep_unknown and len(raw_name) >= 3:
        return qty, raw_name  # unknown (e.g. a gem whose level isn't shown -> "?")
    return None


def snap_name(
    raw_name: str,
    dictionary: Iterable[str],
    *,
    score_cutoff: int = DEFAULT_CUTOFF,
) -> str | None:
    """Fuzzy-snap a noisy name to the closest known currency name.

    Uses plain Levenshtein ratio (not WRatio): WRatio over-rewards the shared
    'Rune'/'Orb' token, so an untracked or mis-OCR'd name like 'Glacier Rune'
    would match an unrelated 'Perfect Rebirth Rune' at ~86 and get a bogus price.
    Ratio keeps character-level OCR-slip correction ('Augrnentation' ->
    'Augmentation' = 92) while dropping those token-overlap false positives to
    ~50, so they fall below the cutoff and stay '?' instead of being mispriced.
    """
    cleaned = raw_name.strip().strip(".,:;")
    if not cleaned:
        return None
    result = process.extractOne(
        cleaned, list(dictionary), scorer=fuzz.ratio, score_cutoff=score_cutoff
    )
    return result[0] if result else None


def parse_rows(
    rows: Iterable[str],
    dictionary: Iterable[str],
    *,
    score_cutoff: int = DEFAULT_CUTOFF,
    keep_unknown: bool = False,
) -> list[tuple[int, str]]:
    """Parse many OCR lines, dropping any that don't yield a usable option."""
    dictionary = list(dictionary)  # reuse across rows
    out: list[tuple[int, str]] = []
    for raw in rows:
        opt = parse_row(raw, dictionary, score_cutoff=score_cutoff, keep_unknown=keep_unknown)
        if opt is not None:
            out.append(opt)
    return out

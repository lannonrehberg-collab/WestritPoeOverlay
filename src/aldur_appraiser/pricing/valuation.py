"""Value and rank reward options.

Values are already in the base currency (the source returns CurrentPrice in the
chosen ReferenceCurrency), so total = qty * unit. Unknown / thin-market currency
is never guessed: known=False, total=None. If any option is unknown the result
is flagged incomplete, because the "best" pick can't be stated with certainty.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aldur_appraiser.pricing.client import PriceTable


@dataclass(frozen=True)
class Valuation:
    name: str
    qty: int
    unit: float | None       # unit value in base currency, or None if unknown
    total: float | None      # qty * unit, or None if unknown
    known: bool
    is_best: bool = False
    is_bonus: bool = False    # always-paid bonus reward: shown, never ranked


@dataclass(frozen=True)
class EvalResult:
    items: list[Valuation]        # the choices, sorted: known by total desc, then unknown
    incomplete: bool              # True if at least one *choice* couldn't be valued
    bonus_items: list[Valuation] = field(default_factory=list)  # always paid, not ranked

    @property
    def best(self) -> Valuation | None:
        return next((v for v in self.items if v.is_best), None)


def format_value(
    total: float, divine_rate: float | None, *, base_unit: str = "exalted"
) -> tuple[float, str]:
    """Pick a friendly denomination: small values in the base unit, large ones in
    Divine. Returns (value, unit). divine_rate = exalted (base) per Divine."""
    if divine_rate and divine_rate > 0 and total >= divine_rate:
        return total / divine_rate, "divine"
    return total, base_unit


def divine_rate(prices: PriceTable) -> float | None:
    """Exalted (base) per Divine Orb, used to switch big values to Divine."""
    return prices.get("Divine Orb")


def value_one(qty: int, name: str, prices: PriceTable, *, is_bonus: bool = False) -> Valuation:
    """Value a single (qty, name) against the price table."""
    unit = prices.get(name)
    if unit is None:
        return Valuation(name=name, qty=qty, unit=None, total=None, known=False, is_bonus=is_bonus)
    return Valuation(
        name=name, qty=qty, unit=unit, total=qty * unit, known=True, is_bonus=is_bonus
    )


def evaluate(
    options: list[tuple[int, str]],
    prices: PriceTable,
    *,
    bonus: list[tuple[int, str]] | None = None,
) -> EvalResult:
    """Rank the choice `options`; value `bonus` separately (always paid, never best).

    The bonus reward is paid regardless of which option is picked, so it must not
    affect the ranking, the BEST marker, or the incomplete flag.
    """
    valuations = [value_one(qty, name, prices) for qty, name in options]
    known = [v for v in valuations if v.known]
    unknown = [v for v in valuations if not v.known]
    known.sort(key=lambda v: v.total, reverse=True)

    if known:
        top = known[0]
        known[0] = Valuation(**{**top.__dict__, "is_best": True})

    bonus_items = [value_one(qty, name, prices, is_bonus=True) for qty, name in (bonus or [])]
    return EvalResult(items=known + unknown, incomplete=bool(unknown), bonus_items=bonus_items)

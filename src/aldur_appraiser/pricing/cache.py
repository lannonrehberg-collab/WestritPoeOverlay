"""TTL disk cache for the price table.

Prices refresh ~hourly on the source, so we never fetch per frame. On a network
error we keep using the last cached table and flag it stale (shown discreetly in
the overlay).
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from aldur_appraiser.config import cache_dir
from aldur_appraiser.pricing.client import PriceTable, PricingError, fetch_price_table

# Bump when the table's shape/coverage changes (e.g. currency-only -> all
# categories) so older on-disk caches are ignored instead of served stale.
CACHE_VERSION = 2


@dataclass(frozen=True)
class CachedPrices:
    table: PriceTable
    fetched_at: float          # unix seconds
    stale: bool                # True if served from an expired/fallback cache

    @property
    def age_minutes(self) -> float:
        return (time.time() - self.fetched_at) / 60.0


def _cache_file(league: str, base: str) -> Path:
    safe = f"{league}_{base}".lower().replace(" ", "_").replace("/", "_")
    return cache_dir() / f"prices_{safe}.json"


def _read(path: Path) -> tuple[PriceTable, float] | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        # В файле может оказаться не словарь (обрывок, чужой JSON): раньше это
        # давало AttributeError мимо списка исключений и роняло загрузку цен
        # вместо перехода на старый кэш.
        if not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION:
            return None  # stale schema (e.g. pre-all-categories) -> refetch
        return raw["table"], float(raw["fetched_at"])
    except (json.JSONDecodeError, KeyError, OSError, ValueError, TypeError):
        return None


def _write(path: Path, table: PriceTable, fetched_at: float) -> None:
    # Номер процесса в имени заготовки: два процесса не затирают её друг другу.
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        tmp.write_text(
            json.dumps({"version": CACHE_VERSION, "table": table, "fetched_at": fetched_at}),
            encoding="utf-8",
        )
        tmp.replace(path)  # atomic on both POSIX and Windows
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def get_or_fetch(
    league: str,
    base: str = "exalted",
    *,
    ttl_minutes: int = 20,
    realm: str = "poe2",
    categories=None,  # None = all currency categories (matches fetch_price_table)
    fetcher: Callable[..., PriceTable] = fetch_price_table,
    now: Callable[[], float] = time.time,
) -> CachedPrices:
    """Return cached prices if fresh, else fetch. On fetch failure fall back to
    the last cache (stale=True). Raises PricingError only if there is no cache
    to fall back on."""
    path = _cache_file(league, base)
    cached = _read(path)

    if cached is not None:
        table, fetched_at = cached
        if (now() - fetched_at) / 60.0 < ttl_minutes:
            return CachedPrices(table=table, fetched_at=fetched_at, stale=False)

    try:
        table = fetcher(league, base, realm=realm, categories=categories)
        fetched_at = now()
        _write(path, table, fetched_at)
        return CachedPrices(table=table, fetched_at=fetched_at, stale=False)
    except PricingError:
        if cached is not None:
            table, fetched_at = cached
            return CachedPrices(table=table, fetched_at=fetched_at, stale=True)
        raise

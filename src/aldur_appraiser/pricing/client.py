"""poe2scout price fetching.

Endpoints verified 2026-06-15 against https://poe2scout.com/api/openapi.json:

    GET /{realm}/Leagues
        -> list of leagues; each has Value, IsCurrent, BaseCurrencyApiId, DivinePrice

    GET /{realm}/Leagues/{league}/Currencies/ByCategory
        ?Category=<cat>&ReferenceCurrency=<base>&Page=<n>&PerPage=<=250
        -> { CurrentPage, Pages, Total, Items: [ {
               ApiId, Text, CategoryApiId, IconUrl, ItemMetadata,
               PriceLogs: [{Price, Time, Quantity}],
               CurrentPrice,        # <-- unit value already in ReferenceCurrency
               CurrentQuantity,     # listed quantity (thin-market signal)
             } ] }

Because CurrentPrice is returned directly in the chosen ReferenceCurrency, no
ratio conversion is needed: the PriceTable values are already in `base`.
"""

from __future__ import annotations

from collections.abc import Iterable

import httpx

from aldur_appraiser import netenv

BASE_URL = "https://poe2scout.com/api"
MAX_PER_PAGE = 250
DEFAULT_TIMEOUT = 20.0

# Currencies with at least this listed quantity are treated as priced.
# Below it the market is too thin to trust -> excluded -> valued as known=False.
MIN_QUANTITY = 1

PriceTable = dict[str, float]


class PricingError(RuntimeError):
    """Raised when the price source cannot be reached or parsed."""


def _get(client: httpx.Client, path: str, params: dict | None = None) -> dict:
    try:
        resp = client.get(f"{BASE_URL}{path}", params=params)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as exc:  # network, timeout, status, decode
        raise PricingError(f"poe2scout request failed: {path}: {exc}") from exc


def list_currency_categories(
    realm: str, league: str, *, client: httpx.Client | None = None
) -> list[str]:
    """All currency-category ApiIds for a league (currency, runes, essences, …)."""
    owns = client is None
    client = client or netenv.client(timeout=DEFAULT_TIMEOUT)
    try:
        data = _get(client, f"/{realm}/Leagues/{league}/Items/Categories")
        return [c["ApiId"] for c in data.get("CurrencyCategories", []) if c.get("ApiId")]
    finally:
        if owns:
            client.close()


def fetch_currency_icon_urls(
    realm: str, league: str, *, client: httpx.Client | None = None
) -> dict[str, str]:
    """Icon URLs for the league's denominations: {'exalted': url, 'divine': url}.

    (Keys are the display units used by format_value; missing ones are omitted.)
    """
    owns = client is None
    client = client or netenv.client(timeout=DEFAULT_TIMEOUT)
    try:
        for lg in _get(client, f"/{realm}/Leagues"):
            if lg.get("Value") == league:
                urls = {
                    "exalted": lg.get("BaseCurrencyIconUrl"),
                    "divine": lg.get("DivineCurrencyIconUrl"),
                }
                return {k: v for k, v in urls.items() if v}
        return {}
    finally:
        if owns:
            client.close()


def fetch_base_icon_url(
    realm: str, league: str, *, client: httpx.Client | None = None
) -> str | None:
    """URL of the league's base-currency icon (e.g. the Exalted Orb image)."""
    return fetch_currency_icon_urls(realm, league, client=client).get("exalted")


def current_league(realm: str = "poe2", *, client: httpx.Client | None = None) -> str:
    """Return the league name flagged IsCurrent (non-hardcore preferred)."""
    owns = client is None
    client = client or netenv.client(timeout=DEFAULT_TIMEOUT)
    try:
        leagues = _get(client, f"/{realm}/Leagues")
        current = [lg for lg in leagues if lg.get("IsCurrent")]
        if not current:
            raise PricingError("no current league reported by poe2scout")
        # Prefer the softcore league (HC variants share IsCurrent).
        softcore = [lg for lg in current if not lg["Value"].upper().startswith("HC")]
        return (softcore or current)[0]["Value"]
    finally:
        if owns:
            client.close()


def _fetch_category(
    client: httpx.Client, realm: str, league: str, category: str, base: str
) -> list[dict]:
    """Page through one currency category, returning all item dicts."""
    items: list[dict] = []
    page = 1
    while True:
        data = _get(
            client,
            f"/{realm}/Leagues/{league}/Currencies/ByCategory",
            params={
                "Category": category,
                "ReferenceCurrency": base,
                "Page": page,
                "PerPage": MAX_PER_PAGE,
            },
        )
        items.extend(data.get("Items", []))
        if page >= int(data.get("Pages", 1)):
            break
        page += 1
    return items


def fetch_price_table(
    league: str,
    base: str = "exalted",
    *,
    realm: str = "poe2",
    categories: Iterable[str] | None = None,
    client: httpx.Client | None = None,
) -> PriceTable:
    """Build {canonical_name: unit_value_in_base} from poe2scout.

    Keys double as the OCR snap-dictionary (canonical currency names).
    Unknown/thin currencies are simply absent -> valued as known=False later.

    With no categories given, pulls *all* currency categories (currency, runes,
    essences, …) so reward types like Lesser runes get priced too.
    """
    owns = client is None
    client = client or netenv.client(timeout=DEFAULT_TIMEOUT)
    try:
        cats = list(categories) if categories else list_currency_categories(
            realm, league, client=client
        )
        table: PriceTable = {}
        fetched_any = False
        for category in cats:
            try:
                items = _fetch_category(client, realm, league, category, base)
            except PricingError:
                continue  # skip a bad/unsupported category, keep the rest
            fetched_any = True
            for it in items:
                name = it.get("Text")
                price = it.get("CurrentPrice")
                qty = it.get("CurrentQuantity") or 0
                if not name or price is None or qty < MIN_QUANTITY:
                    continue
                table[name] = float(price)
        # The reference currency itself is worth exactly 1 unit of base.
        table.setdefault(_base_display_name(base), 1.0)
        if not fetched_any:
            raise PricingError(f"could not fetch any category for league={league!r}")
        return table
    finally:
        if owns:
            client.close()


def _base_display_name(base: str) -> str:
    """Map a base ApiId to its canonical display name (best-effort)."""
    return {
        "exalted": "Exalted Orb",
        "divine": "Divine Orb",
        "chaos": "Chaos Orb",
    }.get(base.lower(), base)

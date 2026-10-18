"""Tests for the pricing core: client parsing, cache TTL/fallback, valuation."""

from __future__ import annotations

import httpx
import pytest

from aldur_appraiser.pricing import cache as cache_mod
from aldur_appraiser.pricing.client import (
    PricingError,
    fetch_price_table,
)
from aldur_appraiser.pricing.valuation import evaluate

# --- fixtures mirroring the real poe2scout response shape --------------------


def _item(name, price, qty=100):
    return {"Text": name, "CurrentPrice": price, "CurrentQuantity": qty}


def _page(items, page=1, pages=1):
    return {"CurrentPage": page, "Pages": pages, "Total": len(items), "Items": items}


def _mock_client(routes: dict[str, dict]) -> httpx.Client:
    """Build an httpx.Client whose responses come from a path-fragment->json map.

    Keys are matched as substrings of the request path, so tests don't need to
    reproduce the exact ``/api`` prefix or URL-encoding of league names.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        for fragment, payload in routes.items():
            if fragment in path:
                return httpx.Response(200, json=payload)
        raise AssertionError(f"unexpected request path: {path}")

    return httpx.Client(transport=httpx.MockTransport(handler))


# --- client ------------------------------------------------------------------


def test_fetch_price_table_parses_and_adds_base():
    routes = {
        "/poe2/Leagues/Runes of Aldur/Currencies/ByCategory": _page(
            [
                _item("Divine Orb", 165.0),
                _item("Chaos Orb", 13.76),
                _item("Orb of Augmentation", 0.05),
            ]
        )
    }
    with _mock_client(routes) as client:
        table = fetch_price_table(
            "Runes of Aldur", "exalted", categories=["currency"], client=client
        )

    assert table["Divine Orb"] == 165.0
    assert table["Orb of Augmentation"] == 0.05
    # base currency is implicitly worth 1.0
    assert table["Exalted Orb"] == 1.0


def test_fetch_skips_thin_and_priceless_currency():
    routes = {
        "/poe2/Leagues/L/Currencies/ByCategory": _page(
            [
                _item("Divine Orb", 165.0, qty=50),
                _item("No Market Orb", None, qty=999),
                _item("Thin Orb", 5.0, qty=0),  # below MIN_QUANTITY
            ]
        )
    }
    with _mock_client(routes) as client:
        table = fetch_price_table("L", "exalted", categories=["currency"], client=client)

    assert "Divine Orb" in table
    assert "No Market Orb" not in table
    assert "Thin Orb" not in table


def test_fetch_raises_on_network_error():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no network", request=request)

    with httpx.Client(transport=httpx.MockTransport(boom)) as client:
        with pytest.raises(PricingError):
            fetch_price_table("L", "exalted", categories=["currency"], client=client)


def test_fetch_all_categories_when_none_given():
    # no categories -> discover them via Items/Categories, then pull each
    routes = {
        "/Items/Categories": {
            "UniqueCategories": [],
            "CurrencyCategories": [
                {"ApiId": "currency", "Label": "Currency"},
                {"ApiId": "runes", "Label": "Runes"},
            ],
        },
        "Category=currency": _page([_item("Divine Orb", 165.0)]),
        "Category=runes": _page([_item("Lesser Robust Rune", 15.8)]),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "Items/Categories" in url:
            return httpx.Response(200, json=routes["/Items/Categories"])
        for frag in ("Category=currency", "Category=runes"):
            if frag in url:
                return httpx.Response(200, json=routes[frag])
        raise AssertionError(url)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        table = fetch_price_table("L", "exalted", client=client)

    assert table["Divine Orb"] == 165.0
    assert table["Lesser Robust Rune"] == 15.8  # runes category now included


# --- cache -------------------------------------------------------------------


def test_cache_fresh_skips_fetch(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_mod, "cache_dir", lambda: tmp_path)
    calls = {"n": 0}

    def fetcher(league, base, **kw):
        calls["n"] += 1
        return {"Divine Orb": 165.0}

    # first call fetches, second (within TTL) is served from disk
    cache_mod.get_or_fetch("L", "exalted", ttl_minutes=20, fetcher=fetcher)
    res = cache_mod.get_or_fetch("L", "exalted", ttl_minutes=20, fetcher=fetcher)

    assert calls["n"] == 1
    assert res.stale is False
    assert res.table["Divine Orb"] == 165.0


def test_cache_falls_back_to_stale_on_error(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_mod, "cache_dir", lambda: tmp_path)

    # seed a cache entry, then expire it
    t = [1000.0]
    cache_mod.get_or_fetch(
        "L",
        "exalted",
        ttl_minutes=20,
        fetcher=lambda *a, **k: {"Divine Orb": 1.0},
        now=lambda: t[0],
    )

    def failing(*a, **k):
        raise PricingError("down")

    res = cache_mod.get_or_fetch(
        "L", "exalted", ttl_minutes=20, fetcher=failing, now=lambda: t[0] + 9999
    )
    assert res.stale is True
    assert res.table["Divine Orb"] == 1.0


def test_cache_ignores_old_version(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_mod, "cache_dir", lambda: tmp_path)
    # a legacy cache file (no version field), timestamped in the future so TTL
    # alone would treat it as fresh
    legacy = cache_mod._cache_file("L", "exalted")
    legacy.write_text('{"table": {"Old Orb": 1.0}, "fetched_at": 1.0e12}', encoding="utf-8")

    calls = {"n": 0}

    def fetcher(*a, **k):
        calls["n"] += 1
        return {"New Orb": 2.0}

    res = cache_mod.get_or_fetch("L", "exalted", fetcher=fetcher)
    assert calls["n"] == 1               # old-version cache ignored -> refetched
    assert res.table == {"New Orb": 2.0}


def test_cache_raises_when_no_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_mod, "cache_dir", lambda: tmp_path)

    def failing(*a, **k):
        raise PricingError("down")

    with pytest.raises(PricingError):
        cache_mod.get_or_fetch("L", "exalted", fetcher=failing)


# --- valuation ---------------------------------------------------------------


def test_evaluate_ranks_and_marks_best():
    prices = {"Divine Orb": 165.0, "Chaos Orb": 13.76}
    # 1 divine = 165 ex > 5 chaos = 68.8 ex -> divine wins
    result = evaluate([(5, "Chaos Orb"), (1, "Divine Orb")], prices)

    assert result.items[0].name == "Divine Orb"
    assert result.items[0].is_best is True
    assert result.items[0].total == 165.0
    assert result.items[1].total == pytest.approx(68.8)  # 5 * 13.76
    assert result.incomplete is False


def test_evaluate_orders_by_total_not_unit():
    prices = {"Divine Orb": 165.0, "Chaos Orb": 13.76}
    # 20 chaos = 275.2 ex > 1 divine = 165 ex -> chaos wins
    result = evaluate([(1, "Divine Orb"), (20, "Chaos Orb")], prices)
    assert result.best.name == "Chaos Orb"


def test_format_value_switches_to_divine_for_large_totals():
    from aldur_appraiser.pricing.valuation import format_value

    rate = 165.0  # exalted per divine
    # small value stays in the base unit
    assert format_value(50.0, rate) == (50.0, "exalted")
    # >= 1 divine switches to divine
    val, unit = format_value(495.0, rate)
    assert unit == "divine" and val == pytest.approx(3.0)
    # exactly one divine
    assert format_value(165.0, rate) == (1.0, "divine")
    # no rate -> stays base
    assert format_value(999.0, None) == (999.0, "exalted")


def test_bonus_is_valued_but_not_ranked():
    prices = {"Divine Orb": 165.0, "Regal Orb": 0.34}
    # Regal Orb is the bonus (always paid) and far more... no: it's cheap here,
    # but even a valuable bonus must never be BEST or affect the choice ranking.
    result = evaluate([(1, "Divine Orb")], prices, bonus=[(1, "Regal Orb")])

    assert result.best.name == "Divine Orb"            # bonus excluded from ranking
    assert [v.name for v in result.items] == ["Divine Orb"]
    assert len(result.bonus_items) == 1
    assert result.bonus_items[0].name == "Regal Orb"
    assert result.bonus_items[0].is_bonus is True
    assert result.bonus_items[0].total == 0.34


def test_bonus_does_not_trigger_incomplete():
    prices = {"Divine Orb": 165.0}
    # an unknown *bonus* must not make the choice comparison incomplete
    result = evaluate([(1, "Divine Orb")], prices, bonus=[(1, "Mystery Bonus")])
    assert result.incomplete is False
    assert result.bonus_items[0].known is False


def test_evaluate_unknown_currency_is_incomplete():
    prices = {"Divine Orb": 165.0}
    result = evaluate([(1, "Divine Orb"), (1, "Mystery Item")], prices)

    assert result.incomplete is True
    unknown = [v for v in result.items if not v.known]
    assert len(unknown) == 1
    assert unknown[0].total is None
    # known option still gets the best marker
    assert result.best.name == "Divine Orb"

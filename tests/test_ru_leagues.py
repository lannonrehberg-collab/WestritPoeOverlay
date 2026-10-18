"""Регрессия: выбор лиги и раздельное хранение цен по лигам.

Проверяем ровно то, из-за чего предметы прошлых лиг оставались без цены:
у каждой лиги должен быть свой файл кэша, а общий ``prices.json`` не должен
подменять данные другой лиги.
"""

from __future__ import annotations

import json
import os
import time

from aldur_appraiser.leagues import (
    AUTO_LEAGUE_LABEL, league_slug, order_leagues, parse_leagues,
)
from aldur_appraiser.pricing.ru_prices import PriceBook, price_cache_path
from aldur_appraiser.runeshape_catalog import (
    PRICE_SCAN_SCHEMA, catalog_archive_path, catalog_path, market_refresh_is_due,
    refresh_is_due, refresh_state_path,
)
from aldur_appraiser.ru_settings import RuSettings


def test_league_slug_is_safe_for_file_names() -> None:
    assert league_slug("Forbidden Rites") == "forbidden-rites"
    assert league_slug("Runes of Aldur") == "runes-of-aldur"
    assert league_slug("HC Forbidden Rites") == "hc-forbidden-rites"
    assert league_slug("  ") == ""


def test_temporary_leagues_come_before_permanent_ones() -> None:
    ordered = order_leagues(["Standard", "Forbidden Rites", "Hardcore",
                             "Runes of Aldur", "forbidden rites"])
    assert ordered == ["Forbidden Rites", "Runes of Aldur", "Standard", "Hardcore"]


def test_api_league_list_is_parsed_and_ordered() -> None:
    payload = {"result": [{"id": "Forbidden Rites"}, {"id": "HC Forbidden Rites"},
                          {"id": "Runes of Aldur"}, {"id": "Standard"}]}
    assert parse_leagues(payload) == ["Forbidden Rites", "HC Forbidden Rites",
                                      "Runes of Aldur", "Standard"]
    assert parse_leagues({"result": ["Standard", "Runes of Aldur"]}) == [
        "Runes of Aldur", "Standard"]


def test_auto_option_is_offered_by_label(tmp_path, monkeypatch) -> None:
    # Пустая лига = автоопределение; подпись пункта не должна выглядеть лигой.
    assert AUTO_LEAGUE_LABEL.strip() != ""
    assert league_slug(AUTO_LEAGUE_LABEL) == ""


def test_price_cache_is_separate_for_every_league(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(os.environ, "ALDUR_APPRAISER_DATA_DIR", str(tmp_path))
    forbidden = price_cache_path("Forbidden Rites")
    aldur = price_cache_path("Runes of Aldur")
    auto = price_cache_path("")
    assert forbidden.name == "prices-forbidden-rites.json"
    assert aldur.name == "prices-runes-of-aldur.json"
    assert auto.name == "prices.json"
    assert len({forbidden, aldur, auto}) == 3


def test_prices_of_the_previous_league_survive_a_switch(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(os.environ, "ALDUR_APPRAISER_DATA_DIR", str(tmp_path))
    first = PriceBook(league="Runes of Aldur")
    first._book = {"Aldur's Saga": 23037.0}
    first._scout = {"Aldur's Saga": 23037.0}
    first._loaded_at = time.time()
    first._save_cache()
    assert price_cache_path("Runes of Aldur").exists()

    other = PriceBook(league="Forbidden Rites")
    assert other._load_cache() is False        # по этой лиге своего кэша ещё нет
    assert other._book == {}                   # и чужие цены не подставляются

    back = PriceBook(league="Runes of Aldur")
    assert back.use_cache() is True
    assert back._book["Aldur's Saga"] == 23037.0


def test_shared_cache_is_not_reused_by_another_league(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(os.environ, "ALDUR_APPRAISER_DATA_DIR", str(tmp_path))
    aldur = PriceBook(league="Runes of Aldur")
    aldur._book = {"Aldur's Saga": 23037.0}
    aldur._scout = {"Aldur's Saga": 23037.0}
    aldur._save_cache()
    shared = json.loads(price_cache_path("").read_text(encoding="utf-8"))
    assert shared["league"] == "Runes of Aldur"

    forbidden = PriceBook(league="Forbidden Rites")
    assert forbidden._load_cache() is False
    # А режим «Авто» общий файл по-прежнему поднимает.
    auto = PriceBook(league="")
    assert auto._load_cache() is True
    assert auto.league == "Runes of Aldur"


def test_switch_league_clears_and_reloads_cache(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(os.environ, "ALDUR_APPRAISER_DATA_DIR", str(tmp_path))
    aldur = PriceBook(league="Runes of Aldur")
    aldur._book = {"Aldur's Saga": 23037.0}
    aldur._scout = {"Aldur's Saga": 23037.0}
    aldur._save_cache()

    book = PriceBook(league="Forbidden Rites")
    book._book = {"Divine Orb": 666.0}
    assert book.switch_league("Runes of Aldur") is True
    assert book.league == "Runes of Aldur"
    assert "Divine Orb" not in book._book          # цены прежней лиги не остаются
    assert book._book["Aldur's Saga"] == 23037.0

    assert book.switch_league("Forbidden Rites") is False
    assert book._book == {}


def test_catalog_keeps_a_separate_snapshot_for_every_league(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(os.environ, "ALDUR_APPRAISER_DATA_DIR", str(tmp_path))
    assert catalog_archive_path("Forbidden Rites").name == \
        "runeshape_catalog-forbidden-rites.json"
    assert catalog_archive_path("Runes of Aldur").name == \
        "runeshape_catalog-runes-of-aldur.json"
    assert catalog_archive_path("") == catalog_path()


def test_settings_remember_leagues_and_the_prompt_flag(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(os.environ, "ALDUR_APPRAISER_DATA_DIR", str(tmp_path))
    settings = RuSettings()
    assert settings.ask_league is True
    settings.league = "Runes of Aldur"
    settings.known_leagues = order_leagues(["Runes of Aldur", "Forbidden Rites"])
    settings.ask_league = False
    settings.save()

    loaded = RuSettings().load()
    assert loaded.league == "Runes of Aldur"
    assert loaded.known_leagues == ["Runes of Aldur", "Forbidden Rites"]
    assert loaded.ask_league is False


def _ready_state(league: str) -> dict:
    return {
        "price_scan_schema": PRICE_SCAN_SCHEMA,
        "status": "ready",
        "full_scan_verified": True,
        "warm_gems": True,
        "completed": 291,
        "total": 291,
        "last_success_at": time.time(),
        "league": league,
    }


def test_change_of_league_forces_a_new_scan(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(os.environ, "ALDUR_APPRAISER_DATA_DIR", str(tmp_path))
    settings = RuSettings()
    settings.league = "Runes of Aldur"
    settings.save()
    # Прошлый проход был по другой лиге: его цены к выбранной не подходят.
    refresh_state_path().write_text(json.dumps(_ready_state("Forbidden Rites")),
                                    encoding="utf-8")
    assert refresh_is_due() is True

    refresh_state_path().write_text(json.dumps(_ready_state("Runes of Aldur")),
                                    encoding="utf-8")
    assert refresh_is_due() is False


def test_market_freshness_is_measured_for_the_selected_league(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(os.environ, "ALDUR_APPRAISER_DATA_DIR", str(tmp_path))
    settings = RuSettings()
    settings.league = "Runes of Aldur"
    settings.save()
    # Свежий общий рынок другой лиги не считается свежим рынком выбранной лиги.
    price_cache_path("").write_text(
        json.dumps({"league": "Forbidden Rites", "loaded_at": time.time()}),
        encoding="utf-8")
    assert market_refresh_is_due() is True

    price_cache_path("Runes of Aldur").write_text(
        json.dumps({"league": "Runes of Aldur", "loaded_at": time.time()}),
        encoding="utf-8")
    assert market_refresh_is_due() is False

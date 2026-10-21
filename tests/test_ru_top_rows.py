"""Регрессия: верхние строки панели могут прийти с мусором от края свитка."""

from aldur_appraiser.localize import parse_line
from aldur_appraiser.pricing.ru_prices import PriceBook
from aldur_appraiser.ru_app import badge_row_height
from aldur_appraiser.vision.detect import PanelDetector


def test_gem_with_ocr_prefix_is_still_recognised() -> None:
    reward = parse_line("(•zv; Уровень умения 20: Проводящие руны")

    assert reward is not None
    assert reward.kind == "gem"
    assert reward.gem_level == 20
    assert reward.en_name == "Conductive Runes"


def test_gem_with_symbol_prefix_is_still_recognised() -> None:
    reward = parse_line("• Уровень умения 20: Отталкивание")

    assert reward is not None
    assert reward.kind == "gem"
    assert reward.gem_level == 20
    assert reward.en_name == "Repulsion"


def test_verisium_pile_is_marked_as_estimate_not_trade_item() -> None:
    reward = parse_line("Груда веризия")

    assert reward is not None
    assert reward.kind == "estimate"
    assert reward.en_name == "Verisium Pile"


def test_random_currency_is_marked_as_an_estimate() -> None:
    reward = parse_line("5 случайных валют")

    assert reward is not None
    assert reward.kind == "estimate"
    assert reward.en_name == "5x Random Currency"


def test_random_currency_as_the_game_writes_it_is_not_dropped() -> None:
    # Живая строка панели (снимок 1600x900_020): игра пишет «5 шт. случайной
    # валюты». Раньше такая строка отбрасывалась как «не награда» и пропадала
    # с панели вместе со своей оценкой.
    for text in ("5 шт. случайной валюты", "5 шт случайных валют",
                 "5 штук случайной валюты", "5 игг. случайной валюты"):
        reward = parse_line(text)
        assert reward is not None, text
        assert reward.kind == "estimate", text
        assert reward.en_name == "5x Random Currency", text


def test_random_uncut_spirit_gem_is_marked_as_estimate() -> None:
    reward = parse_line("Неогранённый камень духа")

    assert reward is not None
    assert reward.kind == "estimate"
    assert reward.en_name == "Uncut Spirit Gem"


def test_uppercase_currency_is_not_lost_as_an_unknown_fragment() -> None:
    reward = parse_line("Сфера ВОЗВЫШеНИЯ (1)")

    assert reward is not None
    assert reward.en_name == "Exalted Orb"
    assert reward.qty == 1


def test_corrupted_panel_title_is_not_a_reward() -> None:
    assert parse_line("Руножеорческие КОмГДНЦИД") is None


def test_unknown_russian_name_is_not_sent_to_the_trade_site() -> None:
    # Снимки 1600x900_002 и 2560x1440_032: награду «Уникальная бижутерия» каталог
    # не знает. Искать её в трейде нельзя: незнакомое условие движок может
    # проигнорировать и вернуть чужие лоты — это перемешивание цен.
    from aldur_appraiser.pricing.ru_prices import PriceBook

    # Заглушка вместо книги: проверка имени стоит до любого обращения к книге.
    assert PriceBook.price_item_trade(None, "Уникальная бижутерия") is None  # type: ignore[arg-type]


def test_own_badge_filter_does_not_eat_the_game_reward_with_sht() -> None:
    # Живая строка панели: игра пишет количество словами «5 шт. случайной
    # валюты». Проверка своих плашек раньше искала «шт» где угодно и вместе с
    # плашкой выбрасывала саму награду — строка пропадала с панели.
    from aldur_appraiser.ru_app import drop_own_badges
    from aldur_appraiser.vision.ocr import OcrLine

    def line(text: str) -> OcrLine:
        return OcrLine(text=text, conf=0.0, top=0.0)

    assert drop_own_badges([line("5 шт. случайной валюты")]) != []
    assert drop_own_badges([line("(3 шт)")]) == []
    assert drop_own_badges([line("3 шт.")]) == []
    assert drop_own_badges([line("661 ex")]) == []


def test_gem_level_without_ocr_lost_word_is_recognised() -> None:
    reward = parse_line("Умения 20: Фрагменгы прошлого")

    assert reward is not None
    assert reward.kind == "gem"
    assert reward.gem_level == 20
    assert reward.en_name == "Fragments Of The Past"


def test_gem_with_ocr_typo_in_ability_label_is_recognised() -> None:
    reward = parse_line("Учение: Фрагменты прошлого")

    assert reward is not None
    assert reward.kind == "gem"
    assert reward.gem_level is None
    assert reward.en_name == "Fragments Of The Past"


def test_gem_level_with_ocr_typo_in_level_word_is_recognised() -> None:
    """«Уровень утаения 20: …» — та же строка камня, слово прочитано с ошибкой.

    Раньше такая строка не опознавалась: цифры в тексте отбрасывали её как мусор,
    и награда оставалась вообще без плашки.
    """

    reward = parse_line("Уровень утаения 20: Взрывная агония")

    assert reward is not None
    assert reward.kind == "gem"
    assert reward.gem_level == 20
    assert reward.en_name == "Detonate Living"


def test_gem_level_with_heavily_mangled_level_word_is_recognised() -> None:
    reward = parse_line("Уровэ-ъ умеюля 20: Фрагменты прошлого")

    assert reward is not None
    assert reward.kind == "gem"
    assert reward.gem_level == 20
    assert reward.en_name == "Fragments Of The Past"


def test_gem_level_with_letter_swapped_in_level_word_is_recognised() -> None:
    """«Урвень умения 20: …» — реальная строка с панели (2К, полноэкранный режим).

    В слове «Уровень» буква «о» прочитана как «в». Проверка подстроки «уро»
    отбрасывала строку целиком, и награда исчезала с панели без плашки.
    """

    reward = parse_line(". Урвень умения 20: Кольцо моротюго шимени")

    assert reward is not None
    assert reward.kind == "gem"
    assert reward.gem_level == 20
    assert reward.en_name == "Frostflame Nova"


def test_gem_with_ocr_typo_in_ability_word_gets_a_price_instead_of_question() -> None:
    """«Увление: Опалкивание» — раньше это был «неизвестный предмет» и «?»."""
    reward = parse_line("Увление: Опалкивание")

    assert reward is not None
    assert reward.kind == "gem"
    assert reward.gem_level is None
    assert reward.en_name == "Repulsion"


def test_ordinary_reward_with_quantity_is_not_turned_into_a_gem() -> None:
    reward = parse_line("Сфера астромантии (2)")

    assert reward is not None
    assert reward.kind == "item"
    assert reward.gem_level is None
    assert reward.en_name == "Artificer's Orb"
    assert reward.qty == 2


def test_trade_query_places_gem_level_in_the_api_filter() -> None:
    class Response:
        status_code = 200

        def json(self):
            return {"result": []}

    class Client:
        payloads: list = []

    book = PriceBook(league="Forbidden Rites")
    client = Client()
    book._trade_post = lambda _client, _url, payload, **_kwargs: (
        client.payloads.append(payload) or Response()
    )

    assert book._listing_prices(client, "Fragments Of The Past", level=20) == []
    # Лотов нет: тот же запрос повторяется со статусом online (у валютных наград
    # мгновенного выкупа не бывает, и без второго прохода цена уходила в агрегатор).
    assert [payload["query"]["status"]["option"] for payload in client.payloads] == [
        "securable", "online"]
    for payload in client.payloads:
        filters = payload["query"]["filters"]["misc_filters"]["filters"]
        assert filters["gem_level"] == {"min": 20, "max": 20}
        assert filters["corrupted"] == {"option": "false"}
        assert payload["query"]["type"] == "Fragments Of The Past"
        assert "trade_filters" not in payload["query"]["filters"]


def test_currency_lot_search_falls_back_to_online_status() -> None:
    """Сплав: мгновенного выкупа нет, но лоты со статусом online находятся."""

    class Search:
        status_code = 200

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    class Lots:
        status_code = 200

        def json(self):
            return {"result": [
                {"listing": {"price": {"amount": 30, "currency": "exalted"}}},
                {"listing": {"price": {"amount": 1, "currency": "chaos"}}},
            ]}

    book = PriceBook(league="Forbidden Rites")
    asked: list[str] = []

    def post(_client, _url, payload, **_kwargs):
        status = payload["query"]["status"]["option"]
        asked.append(status)
        empty = {"result": [], "id": "empty"}
        found = {"result": ["a", "b"], "id": "found"}
        return Search(empty if status == "securable" else found)

    book._trade_post = post
    book._trade_get = lambda *_args, **_kwargs: Lots()

    prices = book._listing_prices(object(), "Runic Alloy", level=None)

    assert asked == ["securable", "online"]
    assert prices == [("exalted", 30.0), ("chaos", 1.0)]


def test_official_check_reports_zero_lots_and_zero_offers_as_a_proof() -> None:
    """Когда таблицы цену не знают, официальный ответ должен быть записан."""

    class Response:
        status_code = 200

        def json(self):
            return {"result": [], "total": 0}

    class ExchangeResponse:
        status_code = 200

        def json(self):
            return {"result": []}

    book = PriceBook(league="Forbidden Rites")
    book._ids = {"Thaumaturgic Flux (Level 12)": "thaumaturgic-flux-12"}
    book._rates = {"chaos": 50.0, "divine": 700.0}
    book._trade_post = lambda _client, url, _payload, **_kwargs: (
        ExchangeResponse() if "/exchange/" in url else Response()
    )
    book._save_cache = lambda: None

    value, proof = book.official_price_or_proof("Thaumaturgic Flux (Level 12)", client=object())

    assert value is None
    assert proof == "официальный поиск: лотов 0; обменник: заявок 0"


def test_official_check_takes_a_price_from_live_exchange_offers() -> None:
    class Response:
        status_code = 200

        def json(self):
            return {"result": [], "total": 0}

    class ExchangeResponse:
        status_code = 200

        def json(self):
            return {"result": [{"listing": {"offers": [{
                "exchange": {"amount": 7, "currency": "exalted"},
                "item": {"amount": 1, "currency": "sample-tag", "stock": 3},
            }]}}]}

    book = PriceBook(league="Forbidden Rites")
    book._ids = {"Sample Tag": "sample-tag"}
    book._rates = {"chaos": 50.0, "divine": 700.0}
    book._book = {}
    book._trade_post = lambda _client, url, _payload, **_kwargs: (
        ExchangeResponse() if "/exchange/" in url else Response()
    )
    book._save_cache = lambda: None

    value, proof = book.official_price_or_proof("Sample Tag", client=object())

    assert value == 7.0
    assert proof == "официальный поиск: лотов 0; обменник: заявок 1"


def test_fullscreen_detector_includes_high_ui_scale() -> None:
    detector = PanelDetector(threshold=0.74,
                             scales=__import__("numpy").linspace(0.3, 2.4, 43))

    assert max(detector.scales) >= 2.4
    assert detector.threshold == 0.74


def test_fullscreen_badges_are_not_tiny() -> None:
    assert badge_row_height(1440) >= 26


def test_price_display_steps_through_exalts_chaos_and_divines() -> None:
    from aldur_appraiser.ru_app import format_number, format_value

    rates = {"chaos": 64.0, "divine": 715.0}

    assert format_number(63.9, rates) == ("64", "exalted")
    assert format_number(64.0, rates) == ("1.00", "chaos")
    assert format_number(400.0, rates) == ("6.25", "chaos")
    assert format_number(715.0, rates) == ("1.00", "divine")
    assert format_value(64.0, rates) == "1.00 chaos"


def test_catalog_gem_level_is_not_sent_as_part_of_trade_name() -> None:
    from aldur_appraiser.runeshape_catalog import split_item_level

    assert split_item_level("Rain of Blades (Level 20)") == ("Rain of Blades", 20)
    assert split_item_level("Eternal March") == ("Eternal March", None)


def test_catalog_russian_name_survives_the_level_suffix() -> None:
    from aldur_appraiser.runeshape_catalog import ru_display_name

    names = {"Rain of Blades": "Дождь клинков",
             "Greater Exalted Orb": "Большая сфера возвышения"}

    # Точного ключа «... (Level 20)» в словаре нет — имя берём без уровня.
    assert ru_display_name(names, "Rain of Blades (Level 20)") == "Дождь клинков"
    assert ru_display_name(names, "Greater Exalted Orb (Level 20)") == \
        "Большая сфера возвышения"
    # Точное имя важнее: у него может быть своя строка в словаре.
    assert ru_display_name(names, "Rain of Blades") == "Дождь клинков"
    # Награда без предмета остаётся без имени, а не получает чужое.
    assert ru_display_name(names, "5x Random Currency") is None
    assert ru_display_name(names, "Verisium Pile") is None


def test_catalog_routes_currency_even_when_it_is_on_the_gems_tab() -> None:
    from aldur_appraiser.runeshape_catalog import _pricing_route

    route, name, level = _pricing_route(
        "Gemcutter's Prism", gem_names={"Gemcutter's Prism"},
        tradeable_types={"Gemcutter's Prism": "Currency"},
        currency_ids={"Gemcutter's Prism": "gcp"})

    assert (route, name, level) == ("exchange", "Gemcutter's Prism", None)


def test_catalog_routes_uncut_gems_by_currency_exchange_id() -> None:
    from aldur_appraiser.runeshape_catalog import _pricing_route

    route, name, level = _pricing_route(
        "Uncut Skill Gem (Level 20)", gem_names={"Uncut Skill Gem (Level 20)"},
        tradeable_types={"Uncut Skill Gem": "Gems"},
        currency_ids={"Uncut Skill Gem (Level 20)": "uncut-skill-gem-20"})

    assert (route, name, level) == ("exchange", "Uncut Skill Gem", 20)


def test_catalog_takes_base_currency_when_level_is_a_tab_artifact() -> None:
    from aldur_appraiser.runeshape_catalog import _pricing_route

    # «Greater Exalted Orb» — валюта, уровня у неё нет: суффикс пришёл со
    # страницы каталога. Биржевой ID подтверждён живым запросом (HTTP 200),
    # поэтому цену базового предмета брать можно.
    route, name, level = _pricing_route(
        "Greater Exalted Orb (Level 20)", gem_names=set(),
        tradeable_types={"Greater Exalted Orb": "Currency"},
        currency_ids={"Greater Exalted Orb": "greater-exalted-orb"})

    assert (route, name, level) == ("exchange", "Greater Exalted Orb", 20)


def test_catalog_does_not_send_levelled_currency_as_exchange_slug() -> None:
    from aldur_appraiser.runeshape_catalog import _pricing_route

    route, name, level = _pricing_route(
        "Thaumaturgic Flux (Level 10)", gem_names=set(),
        tradeable_types={"Thaumaturgic Flux": "Currency"}, currency_ids={})

    assert (route, name, level) == ("levelled-currency", "Thaumaturgic Flux", 10)


def test_divine_outlier_does_not_override_normal_market_price() -> None:
    book = PriceBook(league="Forbidden Rites")
    book._scout = {"Ancient Rune of the Horde": 717.0}
    book._book = {"Ancient Rune of the Horde": 8.0}

    assert book.price_item("Ancient Rune of the Horde", live=False) == 8.0


def test_trade_fallback_uses_cheapest_fixed_buyout_after_currency_conversion() -> None:
    book = PriceBook(league="Forbidden Rites")
    book._rates = {"divine": 700.0}
    book._listing_prices = lambda *_args, **_kwargs: [
        ("divine", 1.0), ("exalted", 10.0), ("exalted", 12.0),
    ]

    # После перевода в exalted цены равны 700, 10 и 12; ценой считается самая
    # низкая — 10 ex. Средние значения (медиана) завышали цену и расходились с
    # тем, что видно на торговом сайте.
    assert book.price_item_trade("Ancient Rune of the Horde") == 10.0


def test_trade_fallback_takes_cheapest_convertible_buyout() -> None:
    from aldur_appraiser.pricing.ru_prices import _representative_buyout

    book = PriceBook(league="Forbidden Rites")
    book._rates = {"divine": 700.0}

    # Единственный пересчитываемый лот (20 ex при divine по 700) — это цена.
    chosen = _representative_buyout([("divine", 1), ("exalted", 20)], book.convert)
    assert chosen is not None and chosen[0] == 20.0
    # Если ни один лот не пересчитывается в exalted, цены нет.
    assert _representative_buyout([("unknown-coin", 3)], book.convert) is None


def test_official_exchange_ratio_divides_by_item_side_and_converts_trade_slug() -> None:
    import httpx

    item_path = "Metadata/Items/Currency/CurrencyUpgradeToMagic"
    ex_path = "Metadata/Items/Currency/CurrencyAddModToRare"
    payload = {"markets": [{
        "league": "Forbidden Rites",
        "market_pair": [item_path, ex_path],
        "volume_traded": {item_path: 432, ex_path: 144},
        "lowest_ratio": {item_path: 3, ex_path: 1},
        "highest_ratio": {item_path: 3, ex_path: 1},
    }]}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    book = PriceBook(league="Forbidden Rites")
    book._base_ids = {"Orb of Transmutation": item_path}
    book._ids = {"Orb of Transmutation": "transmute"}
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert book.load_exchange(client)

    assert book._exchange["Orb of Transmutation"] == (1 / 3, 1 / 3, 432)
    assert book.convert(3, "transmute") == 1.0


def test_currency_conversion_ignores_thin_or_wide_official_market() -> None:
    book = PriceBook(league="Forbidden Rites")
    book._ids = {"Regal Orb": "regal"}
    book._exchange = {"Regal Orb": (0.7, 4.0, 100)}

    assert book.convert(1, "regal") is None


def test_legacy_price_cache_invalidates_old_exchange_ratio_and_rates(tmp_path, monkeypatch) -> None:
    import json

    import aldur_appraiser.pricing.ru_prices as prices_module

    monkeypatch.setattr(prices_module, "_cache_dir", lambda: tmp_path)
    (tmp_path / "prices.json").write_text(json.dumps({
        "league": "Forbidden Rites",
        "book": {"Exalted Orb": 1.0},
        "rates": {"exalted": 1.0, "divine": 700.0, "chaos": 70.0},
        "exchange": {"Orb of Transmutation": [1.0, 1.0, 500]},
        "exchange_at": 1_800_000_000,
    }), encoding="utf-8")

    book = PriceBook(league="Forbidden Rites")

    assert book.use_cache()
    assert book._rates_at == 0.0
    assert book._exchange == {}


def test_currency_exchange_price_uses_all_payment_currencies_and_cheapest() -> None:
    class Response:
        status_code = 200

        def json(self):
            def row(payment: float, currency: str, amount: float = 1,
                    stock: int = 1):
                return {"listing": {"offers": [{
                    "exchange": {"amount": payment, "currency": currency},
                    "item": {"amount": amount, "currency": "sample-rune",
                             "stock": stock},
                }]}}

            return {"result": [
                row(1, "chaos"),       # 50 ex
                row(20, "exalted"),    # 20 ex
                row(2, "divine"),      # 1400 ex
                row(3, "exalted"),     # 3 ex
                row(0.1, "chaos"),     # 5 ex
                row(0.01, "chaos", stock=0),  # no available stock: ignore
            ]}

    book = PriceBook(league="Forbidden Rites")
    book._ids = {"Sample Rune": "sample-rune"}
    book._rates = {"chaos": 50.0, "divine": 700.0}
    book._book = {"Sample Rune": 10_000.0}
    book._trade_post = lambda _client, _url, payload, **_kwargs: (
        setattr(book, "last_payload", payload) or Response()
    )
    book._save_cache = lambda: None

    # Заявки пересчитываются в exalted: 50, 20, 1400, 3 и 5. Самая низкая — 3 ex;
    # заявка без остатка (stock=0) в расчёт не идёт.
    assert book.price_exchange("Sample Rune", force=True) == 3.0
    assert book.last_payload == {
        "query": {
            "status": {"option": "online"},
            "have": ["exalted", "chaos", "divine"],
            "want": ["sample-rune"],
        },
        "sort": {"have": "asc"},
        "engine": "new",
    }
    assert book._gems["item|Sample Rune"][0] == 3.0


def test_full_audit_does_not_reuse_a_recent_gem_cache() -> None:
    import time

    book = PriceBook(league="Forbidden Rites")
    book._gems["Rain of Blades|20"] = (1.0, time.time())
    book._listing_prices = lambda *_args, **_kwargs: [
        ("exalted", 38.0), ("exalted", 40.0), ("exalted", 42.0),
    ]

    assert book.price_gem("Rain of Blades", 20, force=True) == 38.0


def test_scan_state_of_a_dead_process_is_not_shown_as_scanning(
        tmp_path, monkeypatch) -> None:
    """Обновитель убили — состояние не должно висеть «сканирование 73%».

    Замок оставался от умершего процесса, и трей показывал прогресс при том,
    что ни одного запроса уже не шло.
    """
    import json
    import time

    from aldur_appraiser.runeshape_catalog import scan_state_is_stale

    monkeypatch.setenv("ALDUR_APPRAISER_DATA_DIR", str(tmp_path))
    (tmp_path / "runeshape_refresh.lock").write_text("999999", encoding="utf-8")
    state = {"status": "scanning", "progress_percent": 73,
             "last_heartbeat_at": time.time() - 3600}

    assert scan_state_is_stale(state) is True
    assert json.dumps(state)                       # состояние читается как json


def test_scan_state_of_a_live_process_is_not_stale(tmp_path, monkeypatch) -> None:
    """Идёт настоящий проход: замок наш, heartbeat свежий — прогресс показываем."""
    import os
    import time

    from aldur_appraiser.runeshape_catalog import scan_state_is_stale

    monkeypatch.setenv("ALDUR_APPRAISER_DATA_DIR", str(tmp_path))
    (tmp_path / "runeshape_refresh.lock").write_text(str(os.getpid()), encoding="utf-8")
    state = {"status": "scanning", "progress_percent": 40,
             "last_heartbeat_at": time.time()}

    assert scan_state_is_stale(state) is False


def test_finished_scan_is_never_stale(tmp_path, monkeypatch) -> None:
    """Готовый проход не считается зависшим, даже если файла замка уже нет."""
    from aldur_appraiser.runeshape_catalog import scan_state_is_stale

    monkeypatch.setenv("ALDUR_APPRAISER_DATA_DIR", str(tmp_path))
    assert scan_state_is_stale({"status": "ready", "progress_percent": 100}) is False


def _book_with_thin_rune_pair() -> PriceBook:
    """Книга цен с парой снимка «Безупречная руна ледника»: 5 chaos, 5 сделок.

    Взят безупречный тир: у малого, обычного и большого тиров стандартных рун
    есть потолок ``JUNK_RUNE_CAP_EX``, а здесь проверяется выбор источника
    цены, а не потолок.
    """
    book = PriceBook(league="Forbidden Rites")
    book._rates = {"exalted": 1.0, "chaos": 68.68696479543291, "divine": 721.9}
    book._ids = {"Perfect Glacial Rune": "perfect-glacial-rune",
                 "Chaos Orb": "chaos"}
    book._base_ids = {
        "Perfect Glacial Rune": "Metadata/Items/SoulCores/RuneColdPerfect"}
    book._loaded_names = {
        "Metadata/Items/SoulCores/RuneColdPerfect": "Perfect Glacial Rune",
        "Metadata/Items/Currency/CurrencyRerollRare": "Chaos Orb",
    }
    book._exchange_pairs = {"Metadata/Items/SoulCores/RuneColdPerfect": [
        ("Metadata/Items/Currency/CurrencyRerollRare", 5.0, 5.0, 5)]}
    book._save_cache = lambda: None
    return book


def test_thin_snapshot_pair_yields_to_the_cheap_site_listing() -> None:
    """«Руна ледника»: снимок давал 5 chaos, сайт продаёт её за 5 exalted.

    Пара официального снимка стояла ровно на пороге значимости (5 сделок за
    час), и уровень задавала одна нетипичная сделка — 5 chaos за штуку. Самый
    дешёвый лот официального сайта в тот же час стоил 5 exalted, в 68 раз
    дешевле: по цене снимка предмет не продать, ценой должен стать лот сайта.
    """
    book = _book_with_thin_rune_pair()
    book.price_item_trade = lambda _name, **_kw: 5.0

    assert round(book.snapshot_unit("Perfect Glacial Rune") or 0) == 343
    assert book.exchange_price("Perfect Glacial Rune") == 5.0


def test_thin_snapshot_pair_survives_when_the_site_agrees() -> None:
    """Сайт рядом со снимком — цену не меняем: расхождение втрое и меньше."""
    book = _book_with_thin_rune_pair()
    book.price_item_trade = lambda _name, **_kw: 200.0

    assert book.exchange_price("Perfect Glacial Rune") == 343.43482397716457


def test_full_snapshot_pair_is_not_rechecked_on_the_site() -> None:
    """За плотной парой стоят сотни сделок: сайт не спрашиваем вообще."""
    book = _book_with_thin_rune_pair()
    book._exchange_pairs["Metadata/Items/SoulCores/RuneColdPerfect"] = [
        ("Metadata/Items/Currency/CurrencyRerollRare", 5.0, 5.0, 400)]
    asked: list[str] = []
    book.price_item_trade = lambda name, **_kw: asked.append(name) or 5.0

    assert book.exchange_price("Perfect Glacial Rune") == 343.43482397716457
    assert asked == []


def test_thin_snapshot_pair_falls_back_to_the_ninja_average() -> None:
    """Сайт молчит (лимит 429) — тонкая пара не должна возвращать цена снимка.

    Средняя poe.ninja по «Малой руне ледника» — 34.09 exalted (0.5 chaos),
    снимок с пятью сделками давал 343 exalted (5 chaos). Когда лоты сайта
    недоступны, ценой становится ориентир ninja, а не одиночная сделка.
    """
    book = _book_with_thin_rune_pair()
    book._book["Perfect Glacial Rune"] = 34.0935
    book.price_item_trade = lambda _name, **_kw: None

    assert book.exchange_price("Perfect Glacial Rune") == 34.0935


def test_ninja_average_is_the_base_and_the_snapshot_only_lowers_it() -> None:
    """Средняя poe.ninja — основа: дорогая пара снимка её не поднимает.

    Пара снимка через Chaos давала по «Малой руне ледника» 343 exalted, средняя
    poe.ninja — 34.09 exalted (0.5 chaos). Ценой становится основа, а снимок
    может её только понизить.
    """
    book = _book_with_thin_rune_pair()
    book._book["Perfect Glacial Rune"] = 34.0935
    book.price_item_trade = lambda _name, **_kw: None

    assert book.exchange_price("Perfect Glacial Rune") == 34.0935


def test_cheap_snapshot_pair_lowers_the_ninja_average() -> None:
    """Плотная пара снимка дешевле основы — берём цену реальной сделки."""
    book = _book_with_thin_rune_pair()
    book._book["Perfect Glacial Rune"] = 500.0
    book._exchange_pairs["Metadata/Items/SoulCores/RuneColdPerfect"] = [
        ("Metadata/Items/Currency/CurrencyAddModToRare", 1.0, 1.0, 400)]
    book.price_item_trade = lambda _name, **_kw: None

    assert book.exchange_price("Perfect Glacial Rune") == 1.0


def test_dense_snapshot_pair_ignores_the_ninja_average() -> None:
    """Плотная пара снимка дороже основы: основу не поднимаем, сайт не спрашиваем."""
    book = _book_with_thin_rune_pair()
    book._book["Perfect Glacial Rune"] = 1.0
    book._exchange_pairs["Metadata/Items/SoulCores/RuneColdPerfect"] = [
        ("Metadata/Items/Currency/CurrencyRerollRare", 5.0, 5.0, 400)]
    asked: list[str] = []
    book.price_item_trade = lambda name, **_kw: asked.append(name) or 5.0

    assert book.exchange_price("Perfect Glacial Rune") == 1.0
    assert asked == []


def test_chaos_pair_of_a_cheap_rune_is_capped_by_the_ninja_average() -> None:
    """Пара «руна | Chaos» не задаёт цену: дороже средней poe.ninja она не берётся.

    Именно эта пара дала 343.4 exalted (5 chaos) за руну, которая в паре с
    Exalted стоила 1 exalted. Основа — средние 34.09 exalted (0.5 chaos).
    """
    book = _book_with_thin_rune_pair()
    book._book["Perfect Glacial Rune"] = 34.0935
    book.price_item_trade = lambda _name, **_kw: None

    assert book.snapshot_unit("Perfect Glacial Rune") == 343.43482397716457
    assert book.exchange_price("Perfect Glacial Rune") == 34.0935


def test_thin_scout_aggregate_yields_to_the_site_listing() -> None:
    """Тонкий агрегат poe2scout не задаёт цену: «Малая руна заряда» — 40155 ex.

    Одна сделка в агрегаторе дала 55 divine за руну, у которой вся родня стоит
    3–50 exalted. Ценой становится лот сайта.
    """
    book = _book_with_thin_rune_pair()
    book._scout["Perfect Glacial Rune"] = 40155.87
    book._scout_volume["Perfect Glacial Rune"] = 1
    book.price_item_trade = lambda _name, **_kw: 5.0

    assert book.exchange_price("Perfect Glacial Rune") == 5.0


def test_dense_scout_aggregate_is_taken_as_is() -> None:
    """Плотный агрегат (объём выше порога) цену задаёт сам, сайт не спрашиваем."""
    book = _book_with_thin_rune_pair()
    book._exchange_pairs = {}          # снимок цену не знает — остаётся агрегат
    book._scout["Perfect Glacial Rune"] = 40.0
    book._scout_volume["Perfect Glacial Rune"] = 50
    asked: list[str] = []
    book.price_item_trade = lambda name, **_kw: asked.append(name) or 5.0

    assert book.exchange_price("Perfect Glacial Rune") == 40.0
    assert asked == []


def test_thin_scout_aggregate_without_site_lots_has_no_price() -> None:
    """Лотов на сайте нет — тонкий агрегат в 55 divine ценой не становится.

    «Малой руны заряда» нет ни у poe.ninja, ни в списке торгового сайта: одна
    сделка агрегатора дала 40155 exalted. Лучше показать «?», чем 55 divine.
    """
    book = _book_with_thin_rune_pair()
    book._exchange_pairs = {}
    book._scout["Perfect Glacial Rune"] = 40155.87
    book._scout_volume["Perfect Glacial Rune"] = 1
    book.price_item_trade = lambda _name, **_kw: None

    assert book.exchange_price("Perfect Glacial Rune") is None


def test_thin_scout_aggregate_survives_a_trade_site_outage() -> None:
    """Лимит сайта (429) — не «лотов нет»: ориентир остаётся, цена не пропадает."""
    book = _book_with_thin_rune_pair()
    book._exchange_pairs = {}
    book._scout["Perfect Glacial Rune"] = 30.0
    book._scout_volume["Perfect Glacial Rune"] = 1

    def blocked(_name, **_kw):
        book._last_trade_error = "поиск вернул HTTP 429"
        return None

    book.price_item_trade = blocked

    assert book.exchange_price("Perfect Glacial Rune") == 30.0


def test_exchange_offer_cache_is_not_used_as_a_site_listing() -> None:
    """Цена обменника (``item|``) не должна выдаваться за лот сайта (``listing|``).

    Ключи делили одну запись: заявка обменника (5 chaos) читалась как цена
    лота сайта, из-за чего цена руны вообще не проверялась на сайте.
    """
    import time

    book = _book_with_thin_rune_pair()
    book._gems["item|Perfect Glacial Rune"] = (343.0, time.time())
    book._listing_prices = lambda *_args, **_kwargs: [("exalted", 5.0)]

    assert book.price_item_trade("Perfect Glacial Rune") == 5.0
    assert book._gems["listing|Perfect Glacial Rune"][0] == 5.0


def _junk_cap_book(book_prices: dict[str, float]) -> PriceBook:
    book = PriceBook(league="Forbidden Rites")
    book._book = dict(book_prices)
    book._save_cache = lambda: None
    return book


def test_junk_rune_cap_limits_standard_tiers_only() -> None:
    """Потолок 30 exalted: мусорные тиры стандартных рун, но не безупречные.

    «Руна разума» показывалась за 375 exalted (0.54 divine), потому что к концу
    лиги у неё осталась одна сделка «1 divine». Безупречные руны, сплавы и
    особые руны в потолок не входят: у них есть подтверждённый объём сделок.
    """
    from aldur_appraiser.pricing.ru_prices import JUNK_RUNE_CAP_EX

    book = _junk_cap_book({
        "Mind Rune": 375.7,
        "Greater Mind Rune": 900.0,
        "Perfect Mind Rune": 1973.9,
        "Masterwork Rune": 727.2,
        "Greater Rune of Leadership": 2174.8,
        "Runic Alloy": 295.9,
    })

    assert JUNK_RUNE_CAP_EX == 30.0
    assert book.exchange_price("Mind Rune", live=False) == 30.0
    assert book.price_item("Mind Rune", live=False) == 30.0
    assert book.exchange_price("Greater Mind Rune", live=False) == 30.0
    assert book.exchange_price("Perfect Mind Rune", live=False) == 1973.9
    assert book.exchange_price("Masterwork Rune", live=False) == 727.2
    assert book.exchange_price("Greater Rune of Leadership", live=False) == 2174.8
    assert book.exchange_price("Runic Alloy", live=False) == 295.9


def test_junk_rune_cap_leaves_cheaper_prices_alone() -> None:
    """Ниже потолка цена не меняется: режется только завышенная."""
    book = _junk_cap_book({"Lesser Mind Rune": 0.4, "Body Rune": 37.6})

    assert book.exchange_price("Lesser Mind Rune", live=False) == 0.4
    assert book.exchange_price("Body Rune", live=False) == 30.0

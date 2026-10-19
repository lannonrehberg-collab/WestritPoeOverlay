"""Цены для русского оверлея.

Две дороги к цене:

* предметы из торгового обмена (валюта, руны, эссенции, реликвии …) — таблица
  poe.ninja: один запрос отдаёт ~650 позиций сразу, значение приводится к exalted;
* камни умений — официальный трейд-сайт: у камня цена зависит от уровня,
  а в обменной таблице их нет.

Трейд-сайт жёстко ограничивает частоту запросов (HTTP 429 «ждите N секунд»),
поэтому здесь: пауза между запросами, повтор по подсказке сервера и кэш на диск.
"""

from __future__ import annotations

import json
import os
import re
import statistics
import sys
import threading
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import httpx

from aldur_appraiser import netenv
from aldur_appraiser.leagues import league_slug
from aldur_appraiser.localize import ru_to_en
from aldur_appraiser.resources import resource_path

NINJA_OVERVIEW = "https://poe.ninja/poe2/api/economy/exchange/current/overview"
NINJA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://poe.ninja/poe2/economy",
}
# Цены игрового обменника. Сайт poe.ninja отдаёт осреднённые значения, которые
# отстают от игры; обменник в игре живёт своей жизнью, и его данные (вместе с
# курсами) публикует poe2scout — свой worker у них читает официальный снапшот
# обменника web.poecdn.com/api/currency-exchange.
SCOUT = "https://api.poe2scout.com/poe2"
SCOUT_HEADERS = {
    "User-Agent": "AldurAppraiserRU/0.2 (PoE2 reward overlay; personal use)",
}
# Категории «пачечных» предметов: именно они торгуются в игровом обменнике.
SCOUT_CATEGORIES = (
    "currency", "runes", "essences", "fragments", "expedition", "delirium",
    "breach", "abyss", "incursion", "ultimatum", "vaal", "uncutgems",
    "ritual", "legion", "gems", "waystones", "verisium",
)
# Справочник названий и внутренних путей меняется редко — обходим его раз в сутки,
# а цены берём часовыми снимками официального обменника.
SCOUT_TTL = 24 * 60 * 60
# Официальный снапшот игрового обменника (документирован GGG). Для PoE2 нужен
# realm «poe2», а метка времени — начало ПРОШЛОГО часа: текущий час ещё не закрыт.
# Один запрос отдаёт все рынки лиги: цены в exalted, объёмы и границы сделок.
EXCHANGE_CDN = "https://web.poecdn.com/api/currency-exchange/poe2"
EXALTED_PATH = "Metadata/Items/Currency/CurrencyAddModToRare"
# Имя предмета в трейде всегда латиницей. Русское имя без словарной пары —
# признак сырой строки OCR, а не предмета: такой запрос не отправляем.
_LATIN_RE = re.compile(r"[A-Za-z]")
EXCHANGE_TTL = 60 * 60
# 3 — в кэш добавлены все пары официального снимка (не только пара с Exalted):
# без них предметы вида «Зеркало Каландры» и «Прядь Хинекоры» оставались без цены.
EXCHANGE_SCHEMA = 3
TRADE = "https://www.pathofexile.com/api/trade2"
# Те же заголовки, что шлёт сам торговый сайт. Без Origin/Referer Cloudflare
# отвечает 403 «Just a moment» заметно чаще (проверено живыми запросами).
TRADE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Content-Type": "application/json",
    "Origin": "https://www.pathofexile.com",
    "Referer": "https://www.pathofexile.com/trade2/search/poe2",
    "X-Requested-With": "XMLHttpRequest",
}
# Короткие коды валют, которыми трейд-сайт подписывает цену лота. Обычно они
# приходят из справочника обменника, но если справочник ещё не загружен,
# цена лота в «vaal»/«alch»/«regal» оставалась непересчитанной.
TRADE_CURRENCY_ALIASES = {
    "alch": "Orb of Alchemy",
    "alchemy": "Orb of Alchemy",
    "vaal": "Vaal Orb",
    "regal": "Regal Orb",
    "aug": "Orb of Augmentation",
    "transmute": "Orb of Transmutation",
    "chance": "Orb of Chance",
    "mirror": "Mirror of Kalandra",
    "exalt": "Exalted Orb",
    "ex": "Exalted Orb",
}

CATEGORIES = [
    "Currency", "Fragments", "Runes", "Essences", "SoulCores", "Idols",
    "Expedition", "Verisium", "Ritual", "Abyss", "Breach", "Delirium",
    "UncutGems", "LineageSupportGems",
]

# Результат полного сканирования — снимок на сутки. Его нельзя игнорировать
# через полчаса и снова обращаться к trade API от нажатия Shift+D.
TRADE_CACHE_TTL = 26 * 60 * 60
RATE_TTL = 60 * 60         # курс валют — час
BOOK_TTL = 60 * 60         # обменная таблица — час
# Текущая цена агрегатора годится только когда за ней есть не единичная сделка.
# Иначе одно случайное предложение меняет цену руны в десятки раз.
MIN_SCOUT_VOLUME = 5
MIN_OFFICIAL_TRADES = 5
MAX_OFFICIAL_SPREAD = 2.0
# Разброс сделок внутри пары официального снимка. У пары с Exalted он узкий
# (обычная цена предмета), у «мусорных» часов бывает 39× (Divine 20..779 ex) —
# такую пару нельзя выдавать за цену.
MAX_SNAPSHOT_PAIR_SPREAD = 2.5
# Живая заявка трейд-сайта не может быть дороже рынка больше чем втрое: у
# ходовых валют и рун в обменнике попадаются предложения вида «1 divine за одну
# сферу усиления», и медиана трёх самых дешёвых заявок делает такую цену
# итоговой. Официальный снимок и агрегаторы показывают настоящий уровень.
MAX_LIVE_DEVIATION = 3.0
# Пара официального снимка, за которой почти нет сделок: за час их столько,
# что одну нетипичную сделку нельзя отличить от рынка. Живой замер 14.10.2026:
# «Малая руна ледника» — снимок дал 5 chaos за штуку (ровно 5 сделок), а самый
# дешёвый лот официального сайта в тот же час стоил 5 exalted, в 68 раз дешевле.
# Такую пару нельзя показывать как готовую цену: её сверяют с сайтом.
THIN_PAIR_TRADES = 10
# Сколько помнить, что у тонкой пары сайт не дал ни одного лота: иначе панель
# спрашивала бы сайт на каждой перерисовке.
THIN_MISS_TTL = 30 * 60
# Сколько раз пробовать один запрос к трейд-сайту и сколько ждать ответа.
# 403 «Just a moment» от Cloudflare и таймауты — временные сбои: живой замер
# показал, что повтор проходит (2 отказа на 53 камня, 5 отказов на обменник).
# Общий таймаут 20 с: через прокси ответ приходит и за 11–12 с, при прежних
# 8 с такой ответ терялся и предмет оставался без цены.
TRADE_ATTEMPTS = 4
TRADE_TIMEOUT = 20.0
# Liquid Verisium покупается у Farrow за 600 обычного Verisium. Наблюдаемый
# выход одной «Груды» — 200–400; это диапазон оценки, а не биржевой предмет.
VERISIUM_PILE_RANGE = (200, 400)
# У «5 случайных валют» нет опубликованных весов выпадения. Для полезной,
# но честно консервативной оценки берём медиану обычной валютной корзины и не
# включаем джекпоты вроде Mirror.
RANDOM_CURRENCY_BASKET = (
    "Orb of Transmutation", "Orb of Augmentation", "Regal Orb", "Exalted Orb",
    "Orb of Alchemy", "Chaos Orb", "Orb of Chance", "Artificer's Orb",
    "Lesser Jeweller's Orb", "Gemcutter's Prism",
)

# Запасные правила, если сервер не прислал заголовки. Взяты из ответов PoE2:
# поиск — 5/10 с, 15/60 с, 30/300 с, 600/6 ч; обмен — 5/15 с, 10/90 с, 30/300 с.
DEFAULT_RULES: dict[str, list[tuple[int, int, int]]] = {
    "search": [(5, 10, 60), (15, 60, 300), (30, 300, 1800), (600, 21600, 3600)],
    "exchange": [(5, 15, 60), (10, 90, 300), (30, 300, 1800)],
    "fetch": [(5, 4, 10), (15, 20, 60)],
    "other": [(5, 10, 60)],
}
# Если сайт просит подождать дольше этого — не ждём в процессе, а отдаём «нет цены».
MAX_INLINE_WAIT = 20.0
# Сколько вообще готовы ждать внутри запроса (цены считаются в фоновом потоке).
INLINE_WAIT_LIMIT = 120.0


def _slugify(name: str) -> str:
    """«Perfect Jeweller's Orb» -> «perfect-jewellers-orb» (id в обменнике).

    Апостроф именно убирается, а не заменяется дефисом: иначе получается
    «perfect-jeweller-s-orb», и обменник отвечает ошибкой.
    """
    cleaned = (name or "").lower().replace("'", "").replace("\u2019", "")
    return re.sub(r"[^a-z0-9]+", "-", cleaned).strip("-")


@lru_cache(maxsize=1)
def bundled_exchange_ids() -> dict[str, str]:
    """Проверенные биржевые ID обменника для наград каталога Runeshape.

    Справочник собран из словаря торгового сайта и подтверждён живым запросом
    к обменнику: для этих ID он отвечает 200, а для предметов, которые ищутся
    только обычным поиском, — 400 «No valid exchange want tags were found».
    """
    path = resource_path("assets/lang/exchange_ids.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in (data.get("by_name") or {}).items()}


@lru_cache(maxsize=1)
def bundled_exchange_families() -> dict[str, dict]:
    """Камни без огранки: торгуются в обменнике по уровням.

    У награды уровня нет (он случайный), поэтому своего ID у записи тоже нет —
    цену берём по уровням семейства.
    """
    path = resource_path("assets/lang/exchange_ids.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    families = {}
    for name, info in (data.get("gem_families") or {}).items():
        levels = [int(level) for level in (info.get("levels") or [])]
        if levels:
            families[str(name)] = {"id_template": str(info.get("id_template") or ""),
                                   "levels": levels}
    return families


def _is_transient(status: int) -> bool:
    """Временный ли сбой у ответа: 403 Cloudflare или 5xx сервера.

    Это не «предмета нет» и не «лимит исчерпан» — такой ответ надо повторить,
    иначе одна случайная страница проверки бота оставляет награду без цены.
    """
    return status == 403 or 500 <= status < 600


def _robust_price(values: list[float]) -> float | None:
    """Устойчивая цена: медиана нескольких самых дешёвых лотов.

    Самый дешёвый лот часто мусорный («1 регал» за сферу, «3 exalted» за сплав),
    поэтому одиночная минимальная цена врёт. Медиана первых пяти отражает
    реальный рынок и не скачет от одного странного лота.
    """
    if not values:
        return None
    cheapest = sorted(value for value in values if value > 0)[:5]
    if not cheapest:
        return None
    return cheapest[len(cheapest) // 2]


BUYOUT_SAMPLE_SIZE = 5


def _representative_buyout(prices: list[tuple[str, float]], convert
                           ) -> tuple[float, tuple[str, float]] | None:
    """Самая низкая цена среди десяти самых дешёвых лотов мгновенного выкупа.

    Лоты сравниваются только после перевода в exalted: сортировка сайта не
    учитывает курс между валютами, поэтому «самый дешёвый» лот по мнению сайта
    может оказаться дороже другого. Ценой предмета считается минимум — это то,
    что реально можно купить прямо сейчас.
    Средние значения (медиана трёх/пяти) завышали цену и не совпадали с тем,
    что видно на сайте.
    """
    normalized: list[tuple[float, str, float]] = []
    for unit, amount in prices:
        value = convert(amount, unit)
        if value is not None and value > 0:
            normalized.append((value, unit, amount))
    if not normalized:
        return None
    sample = sorted(normalized, key=lambda entry: entry[0])[:BUYOUT_SAMPLE_SIZE]
    chosen = sample[0]
    return chosen[0], (chosen[1], chosen[2])


def _instant_buyout_price(prices: list[tuple[str, float]], convert) -> float | None:
    """Совместимая обёртка для репрезентативной цены мгновенного выкупа."""
    chosen = _representative_buyout(prices, convert)
    return chosen[0] if chosen else None


def _exchange_offer_prices(payload: dict, api_id: str) -> list[tuple[str, float]]:
    """Разбирает ответ обменника: [(валюта оплаты, цена за штуку), …].

    Отдельной функцией, потому что один и тот же разбор нужен и обычной цене
    обменника, и официальной проверке предмета, у которого таблицы цены не знают.
    """
    result = (payload or {}).get("result") or {}
    entries = list(result.values()) if isinstance(result, dict) else result
    out: list[tuple[str, float]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for offer in (entry.get("listing", {}) or {}).get("offers", []) or []:
            ex = offer.get("exchange") or {}
            it = offer.get("item") or {}
            unit = str(ex.get("currency", "")).lower()
            item_currency = str(it.get("currency", "")).lower()
            payment = float(ex.get("amount") or 0)
            item_amount = float(it.get("amount") or 0)
            stock = float(it.get("stock") or 0)
            if (unit in {"exalted", "chaos", "divine"}
                    and item_currency == api_id.lower()
                    and payment > 0 and item_amount > 0 and stock > 0):
                out.append((unit, payment / item_amount))
    return out


def _listing_query(type_name: str, level: int | None, *,
                   status: str = "securable") -> dict:
    """Тело поискового запроса торгового сайта для предмета или камня.

    Один запрос отдаёт лоты во всех валютах; ``securable`` — это переключатель
    «Instant Buyout»: только фиксированные лоты, без «~price» и торга в чате.
    У валютных предметов (сплавы, руны, сферы) мгновенного выкупа не бывает:
    со статусом ``securable`` официальный поиск отдаёт пустой список, поэтому
    тот же запрос повторяется со статусом ``online``.
    """
    misc_filters: dict = {}
    if level is not None:
        # Награда «Уровень умения 20» — именно камень 20 уровня, не 21+.
        misc_filters["gem_level"] = {"min": level, "max": level}
        # Награда — обычный камень, без порчи: испорченные стоят иначе.
        misc_filters["corrupted"] = {"option": "false"}
    return {
        "query": {
            "status": {"option": status},
            "filters": {"misc_filters": {"filters": misc_filters}},
            "stats": [{"type": "and", "filters": []}],
            "type": type_name,
        },
        "sort": {"price": "asc"},
    }



class TradeLimiter:
    """Держит темп обращений к трейд-сайту по его же правилам.

    Сайт присылает заголовки вида
        x-rate-limit-ip: 5:10:60,15:60:300,30:300:1800,600:21600:3600
        x-rate-limit-ip-state: 1:10:0,9:60:0,31:300:1800,81:21600:0
    то есть «не больше N запросов за P секунд, иначе блокировка на R секунд».
    Важно соблюдать ВСЕ окна сразу: короткое задаёт темп, длинное — сколько можно
    всего за 5 минут и за 6 часов. Поэтому считаем не паузу, а скользящие окна.
    """

    def __init__(self) -> None:
        self._rules: dict[str, list[tuple[int, int, int]]] = {}
        self._history: dict[str, list[float]] = {}
        self._blocked_until: dict[str, float] = {}
        # search и fetch могут принадлежать одной политике сервера. Их нельзя
        # считать раздельно: GGG прямо указывает, что одинаковая policy общая
        # между endpoint-ами.
        self._kind_bucket: dict[str, str] = {}
        self._active_bucket: str | None = None
        # Первый State в окне включает обращения браузера/других программ с того
        # же IP. Храним его как базу до конца именно этого окна, чтобы не
        # превысить лимит уже занятого серверного счётчика.
        self._observed: dict[str, dict[int, tuple[float, int]]] = {}
        self._lock = threading.Lock()

    def _bucket(self, kind: str) -> str:
        return self._kind_bucket.get(kind) or self._active_bucket or kind

    def _kind_rules(self, kind: str) -> list[tuple[int, int, int]]:
        bucket = self._bucket(kind)
        return self._rules.get(bucket) or DEFAULT_RULES.get(kind, DEFAULT_RULES["other"])

    def blocked_for(self, kind: str) -> float:
        """Сколько секунд ещё нельзя обращаться (0 — можно)."""
        with self._lock:
            return max(0.0, self._blocked_until.get(self._bucket(kind), 0.0) - time.time())

    @staticmethod
    def _sleep_with_heartbeat(seconds: float,
                              on_wait: Callable[[float], None] | None) -> None:
        """Ждёт предписанную сервером паузу, не превращая её в «зависание»."""
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if on_wait:
                on_wait(remaining)
            time.sleep(min(1.0, remaining))

    def wait(self, kind: str, *, max_wait: float | None = INLINE_WAIT_LIMIT,
             on_wait: Callable[[float], None] | None = None) -> bool:
        """Разрешает запрос, выдержав паузу. False — сейчас нельзя (лимит исчерпан).

        Ждать больше INLINE_WAIT_LIMIT не имеет смысла: цены считаются в фоновом
        потоке, поэтому лучше вернуть отказ и показать «?», чем копить очередь.
        """
        with self._lock:
            bucket = self._bucket(kind)
            now = time.time()
            until_block = self._blocked_until.get(bucket, 0.0)
            if until_block > now:
                if max_wait is not None and until_block - now > max_wait:
                    if on_wait:
                        on_wait(until_block - now)
                    return False
                self._sleep_with_heartbeat(until_block - now, on_wait)
                now = time.time()

            rules = self._kind_rules(kind)
            longest = max(period for _, period, _ in rules)
            history = [t for t in self._history.get(bucket, []) if now - t < longest]

            pause = 0.0
            for hits, period, _ in rules:
                in_window = [t for t in history if now - t < period]
                used_own = len(in_window)
                baseline = self._observed.get(bucket, {}).get(period)
                used_server = 0
                baseline_at = 0.0
                if baseline is not None:
                    baseline_at, baseline_hits = baseline
                    if now - baseline_at < period:
                        # Собственные вызовы после снимка сервер уже считает,
                        # поэтому добавляем только их.
                        used_server = baseline_hits + len(
                            [t for t in history if t >= baseline_at])
                used = max(used_own, used_server)
                if used < hits:
                    continue
                # По своим запросам знаем время; по серверному снимку — только
                # конец окна. В обоих случаях выдерживаем официальное окно.
                if used_own >= hits:
                    pause = max(pause, min(in_window) + period + 0.2 - now)
                if used_server >= hits:
                    pause = max(pause, baseline_at + period + 0.2 - now)
            if max_wait is not None and pause > max_wait:
                if on_wait:
                    on_wait(pause)
                return False
            if pause > 0:
                self._sleep_with_heartbeat(pause, on_wait)
                now = time.time()
                history = [t for t in history if now - t < longest]

            history.append(now)
            self._history[bucket] = history
            return True

    def observe(self, kind: str, headers) -> None:
        """Обновляет правила по заголовкам лимитов и учитывает Retry-After."""
        try:
            policy = str(headers.get("x-rate-limit-policy") or "trade")
            rule_name = next((name.strip() for name in
                              str(headers.get("x-rate-limit-rules") or "ip").split(",")
                              if name.strip()), "ip")
            bucket = f"{policy}:{rule_name}"
            rules = _parse_limit(headers.get(f"x-rate-limit-{rule_name}"))
            # Старый trade endpoint иногда не присылает Rules, но использует ip.
            if not rules:
                rules = _parse_limit(headers.get("x-rate-limit-ip"))
            with self._lock:
                previous = self._bucket(kind)
                if previous != bucket:
                    self._history[bucket] = sorted(
                        self._history.get(bucket, []) + self._history.get(previous, []))
                self._kind_bucket[kind] = bucket
                self._active_bucket = bucket
                if rules:
                    self._rules[bucket] = rules

            retry_after = headers.get("retry-after")
            if retry_after:
                with self._lock:
                    self._blocked_until[bucket] = time.time() + float(retry_after) + 0.2

            # State: третье число — единственный признак уже активного запрета.
            # Сам счётчик (первое число) не является паузой и нужен только для
            # диагностики; наш собственный скользящий журнал выше не превышает
            # правила до отправки запроса.
            now = time.time()
            for current, period, restricted in _parse_limit(
                    headers.get(f"x-rate-limit-{rule_name}-state")
                    or headers.get("x-rate-limit-ip-state")):
                with self._lock:
                    saved = self._observed.setdefault(bucket, {}).get(period)
                    # Не перезаписываем исходный снимок после каждого нашего
                    # запроса: иначе окно искусственно продлевалось бы вечно.
                    if saved is None or now - saved[0] >= period:
                        self._observed[bucket][period] = (now, current)
                if restricted:
                    with self._lock:
                        self._blocked_until[bucket] = max(
                            self._blocked_until.get(bucket, 0.0),
                            now + float(restricted) + 0.2)
        except Exception:  # noqa: BLE001 — заголовки не критичны, работаем на запасных правилах
            pass


def _parse_limit(raw: str | None) -> list[tuple[int, int, int]]:
    """'5:10:60,15:60:300' -> [(5, 10, 60), (15, 60, 300)]."""
    out: list[tuple[int, int, int]] = []
    for chunk in (raw or "").split(","):
        parts = chunk.split(":")
        if len(parts) >= 2 and all(p.strip().isdigit() for p in parts[:2]):
            hits, period = int(parts[0]), int(parts[1])
            restriction = int(parts[2]) if len(parts) > 2 and parts[2].strip().isdigit() else 0
            if hits > 0 and period > 0:
                out.append((hits, period, restriction))
    return out


def _retry_delay(resp: httpx.Response) -> float:
    """Сколько ждать после 429: сначала заголовок Retry-After, затем текст ошибки."""
    try:
        retry_after = resp.headers.get("retry-after")
        if retry_after:
            return float(retry_after) + 1.0
    except (TypeError, ValueError):
        pass
    try:
        message = resp.json().get("error", {}).get("message", "")
        digits = "".join(ch for ch in message if ch.isdigit())
        if digits:
            return min(float(digits) + 2.0, 70.0)
    except ValueError:
        pass
    return 30.0


def _cache_dir() -> Path:
    # Переносимая копия задаёт этот путь в launchers, чтобы кэш цен не
    # оставался разбросанным в общем AppData.
    override = os.environ.get("ALDUR_APPRAISER_DATA_DIR")
    if override:
        path = Path(override)
    elif getattr(sys, "frozen", False):
        path = Path(sys.executable).resolve().parent / "runtime"
    else:
        base = os.environ.get("APPDATA") or str(Path.home())
        path = Path(base) / "aldur-appraiser-ru"
    path.mkdir(parents=True, exist_ok=True)
    return path


def price_cache_path(league: str = "") -> Path:
    """Файл кэша цен для лиги.

    Цены разных лиг несовместимы (в Runes of Aldur Aldur's Saga стоит около
    50 divine, а в Forbidden Rites её просто нет). Общий файл затирал бы
    прошлую лигу при каждом переключении, поэтому у каждой лиги свой файл.
    Пустая лига (режим «Авто») остаётся на прежнем ``prices.json``.
    """
    slug = league_slug(league)
    return _cache_dir() / (f"prices-{slug}.json" if slug else "prices.json")


@dataclass
class PriceBook:
    """Цены в exalted + курс валют; сам решает, когда обновляться."""

    league: str = ""
    base: str = "exalted"
    _book: dict[str, float] = field(default_factory=dict)      # en_name -> exalted
    _rates: dict[str, float] = field(default_factory=dict)     # currency -> exalted за 1
    _gems: dict[str, tuple[float, float]] = field(default_factory=dict)  # key -> (цена, время)
    _gem_source: dict[str, tuple[str, float]] = field(default_factory=dict)  # валюта лота
    _ninja_rates: dict[str, float] = field(default_factory=dict)  # курс из агрегата
    _rates_at: float = 0.0
    icons: dict[str, str] = field(default_factory=dict)   # id валюты -> ссылка на иконку
    # Живые цены игрового обменника: имя предмета -> exalted и объём торговли.
    _scout: dict[str, float] = field(default_factory=dict)
    _scout_volume: dict[str, int] = field(default_factory=dict)
    _ids: dict[str, str] = field(default_factory=dict)   # имя -> id в обменнике
    _base_ids: dict[str, str] = field(default_factory=dict)  # имя -> внутренний путь
    # Цены официального обменника: имя -> (низ, верх, объём за час) в exalted.
    _exchange: dict[str, tuple[float, float, int]] = field(default_factory=dict)
    # Внутренний путь предмета -> [(путь второй валюты, низ, верх, объём)]:
    # цена штуки в единицах второй валюты. Нужна для пар без Exalted.
    _exchange_pairs: dict[str, list[tuple[str, float, float, int]]] = field(
        default_factory=dict)
    # Обратный справочник «внутренний путь -> имя предмета».
    _loaded_names: dict[str, str] = field(default_factory=dict)
    _exchange_at: float = 0.0
    _scout_at: float = 0.0
    _loaded_at: float = 0.0
    _background_refresh: bool = False
    _progress_callback: Callable[[float], None] | None = None
    # Причина последнего неуспешного запроса трейда: сканер не имеет права
    # тихо превращать такую ошибку в «предмет без цены».
    _last_trade_error: str | None = None
    # Имя -> время, когда у тонкой пары снимка сайт не дал ни одного лота.
    _thin_miss: dict[str, float] = field(default_factory=dict)
    _limiter: TradeLimiter = field(default_factory=TradeLimiter)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # --- служебное ---------------------------------------------------------

    def _refresh_name_index(self) -> None:
        """Обратный справочник «внутренний путь -> имя» для пар обменника."""
        self._loaded_names = {path: name for name, path in self._base_ids.items()}

    def _merge_bundled_exchange_ids(self) -> None:
        """Добавляет проверенные биржевые ID наград каталога.

        Справочник poe2scout знает не все уровневые предметы (например,
        «Thaumaturgic Flux (Level 8)» или «Lesser Charging Rune»), из-за чего
        скан писал «цены нет» предметам, которые в обменнике торгуются. Наши
        ID подтверждены живым запросом, поэтому догадку ``_slugify`` они
        заменяют, а уже известные значения не трогают.
        """
        for name, api_id in bundled_exchange_ids().items():
            self._ids.setdefault(name, api_id)

    @property
    def _cache_file(self) -> Path:
        return price_cache_path(self.league)

    @property
    def _shared_cache_file(self) -> Path:
        """Общий файл рынка: его читают инструменты и проверка свежести рынка."""
        return price_cache_path("")

    def _save_cache(self) -> None:
        # Пустота не должна затирать рабочие цены: иначе один неудачный запуск
        # (например, без сети) обнуляет кэш для всех следующих.
        if not self._book and not self._scout:
            return
        try:
            payload = json.dumps({
                "league": self.league,
                "loaded_at": self._loaded_at,
                "book": self._book,
                "rates": self._rates,
                "rates_at": self._rates_at,
                "gem_cache_version": 3,
                "gems": self._gems,
                "gem_source": self._gem_source,
                "icons": self.icons,
                "scout": self._scout,
                "scout_volume": self._scout_volume,
                "ids": self._ids,
                "base_ids": self._base_ids,
                "exchange_schema": EXCHANGE_SCHEMA,
                "exchange": self._exchange,
                "exchange_pairs": self._exchange_pairs,
                "exchange_at": self._exchange_at,
                "scout_at": self._scout_at,
            }, ensure_ascii=False)
            self._write_cache_file(self._cache_file, payload)
            # Общий prices.json остаётся «текущим рынком»: на него смотрят
            # проверка свежести и внешние инструменты.
            shared = self._shared_cache_file
            if shared != self._cache_file:
                self._write_cache_file(shared, payload)
        except OSError:
            pass

    @staticmethod
    def _read_cache_json(path: Path) -> dict | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _write_cache_file(path: Path, payload: str) -> None:
        # Пишем через временный файл: обрыв записи не испортит кэш.
        # В имени — номер процесса: оверлей и суточный сканер пишут один и тот
        # же файл лиги, и общий временный файл давал обрубок кэша.
        tmp = path.with_suffix(f".{os.getpid()}.json.tmp")
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)

    def _load_cache(self) -> bool:
        data = self._read_cache_json(self._cache_file)
        # Первый запуск после разделения кэшей по лигам: своего файла ещё нет,
        # но общий prices.json содержит цены этой же лиги — забираем их.
        if data is None:
            shared = self._shared_cache_file
            if shared != self._cache_file:
                candidate = self._read_cache_json(shared)
                cached = str((candidate or {}).get("league") or "")
                if candidate and (not self.league or cached == self.league):
                    data = candidate
        if data is None:
            return False
        cached_league = str(data.get("league") or "")
        # При обычном запуске лига ещё не определена. Раньше из-за строгого
        # сравнения такой кэш отвергался, таблица и курсы загружались заново в
        # фоне, а запросы цен камней уходили без лиги и без курса валют.
        if not self.league and cached_league:
            self.league = cached_league
        if cached_league != self.league:
            return False
        self._book = {k: float(v) for k, v in data.get("book", {}).items()}
        self._rates = {k: float(v) for k, v in data.get("rates", {}).items()}
        self._rates_at = float(data.get("rates_at", 0.0))
        # Версии до 3 были записаны до двух исправлений запроса камней: уровень
        # был «не ниже указанного», а в выборку попадали торгуемые (~price) лоты.
        # Такие оценки нельзя смешивать с точными fixed-buyout ценами.
        if data.get("gem_cache_version") == 3:
            self._gems = {k: (float(v[0]), float(v[1]))
                          for k, v in data.get("gems", {}).items()
                          if float(v[0]) > 0}
            self._gem_source = {k: (str(v[0]), float(v[1]))
                                for k, v in data.get("gem_source", {}).items()}
        else:
            self._gems = {}
            self._gem_source = {}
        self.icons = {str(k): str(v) for k, v in data.get("icons", {}).items()}
        self._scout = {str(k): float(v) for k, v in data.get("scout", {}).items()}
        self._scout_volume = {str(k): int(v)
                             for k, v in data.get("scout_volume", {}).items()}
        self._ids = {str(k): str(v) for k, v in data.get("ids", {}).items()}
        self._base_ids = {str(k): str(v) for k, v in data.get("base_ids", {}).items()}
        self._refresh_name_index()
        self._merge_bundled_exchange_ids()
        # До schema 2 хранилась только сторона Exalted из ratio, как будто
        # она была ценой одной штуки. Такой официальный снимок надо обновить.
        if data.get("exchange_schema") == EXCHANGE_SCHEMA:
            self._exchange = {str(k): (float(v[0]), float(v[1]), int(v[2]))
                              for k, v in data.get("exchange", {}).items()}
            self._exchange_pairs = {
                str(k): [(str(row[0]), float(row[1]), float(row[2]), int(row[3]))
                         for row in rows]
                for k, rows in (data.get("exchange_pairs") or {}).items()
            }
            self._exchange_at = float(data.get("exchange_at", 0.0))
        else:
            self._exchange = {}
            self._exchange_pairs = {}
            self._exchange_at = 0.0
        self._scout_at = float(data.get("scout_at", 0.0))
        self._loaded_at = float(data.get("loaded_at", 0.0))
        return bool(self._book) or bool(self._scout)

    # --- лига и таблица ------------------------------------------------------

    def detect_league(self, client: httpx.Client) -> str:
        """Актуальная мягкая лига по официальному трейду (авторитетный источник)."""
        try:
            resp = client.get(f"{TRADE}/data/leagues", headers=TRADE_HEADERS, timeout=20)
            resp.raise_for_status()
            payload = resp.json()
            leagues = payload.get("result", payload) if isinstance(payload, dict) else payload
            if not isinstance(leagues, list):
                return self.league or "Standard"
            for lg in leagues:
                lid = lg.get("id") if isinstance(lg, dict) else str(lg)
                if not lid:
                    continue
                low = lid.lower()
                if low.startswith("hc") or low in ("standard", "hardcore"):
                    continue
                return lid
        except (httpx.HTTPError, ValueError):
            pass
        return self.league or "Standard"

    def _scout_league_info(self, client: httpx.Client) -> dict | None:
        """Данные нашей лиги у poe2scout: курс divine и обменный курс хаоса."""
        try:
            resp = client.get(f"{SCOUT}/Leagues", headers=SCOUT_HEADERS, timeout=30)
            resp.raise_for_status()
            leagues = resp.json()
        except (httpx.HTTPError, ValueError):
            return None
        if not isinstance(leagues, list) or not leagues:
            return None
        want = (self.league or "").strip().lower()
        for league in leagues:
            value = str(league.get("Value") or "").lower()
            short = str(league.get("ShortName") or "").lower()
            if want and want in (value, short):
                return league
        return next((lg for lg in leagues if lg.get("IsCurrent")), leagues[0])

    def load_scout(self, client: httpx.Client) -> bool:
        """Забирает цены игрового обменника (то, что игрок видит в игре).

        poe.ninja даёт осреднённое значение, которое отстаёт от игры; здесь —
        свежие данные обменника и вместе с ними честные курсы divine и chaos.
        """
        info = self._scout_league_info(client)
        if info is None:
            return False
        league_name = str(info.get("Value") or self.league)
        prices: dict[str, float] = {}
        volumes: dict[str, int] = {}
        for category in SCOUT_CATEGORIES:
            page, pages = 1, 1
            while page <= pages:
                try:
                    resp = client.get(
                        f"{SCOUT}/Leagues/{quote(league_name)}/Currencies/ByCategory",
                        params={"category": category, "perPage": 100, "page": page},
                        headers=SCOUT_HEADERS, timeout=45)
                    resp.raise_for_status()
                    data = resp.json()
                except (httpx.HTTPError, ValueError):
                    break
                pages = int(data.get("Pages") or 1)
                for item in data.get("Items") or []:
                    name = item.get("Text")
                    price = item.get("CurrentPrice")
                    api_id = item.get("ApiId")
                    if name and api_id:
                        self._ids[str(name)] = str(api_id)
                    if name and item.get("BaseItemTypeId"):
                        self._base_ids[str(name)] = str(item["BaseItemTypeId"])
                    if name and price is not None:
                        prices[str(name)] = float(price)
                        volumes[str(name)] = int(item.get("CurrentQuantity") or 0)
                page += 1
                time.sleep(0.25)          # не частим: сервис общественный
        if not prices:
            return False
        self._scout = prices
        self._scout_volume = volumes
        self._scout_at = time.time()
        self._refresh_name_index()

        divine = float(info.get("DivinePrice") or 0)
        chaos_per_divine = float(info.get("ChaosDivinePrice") or 0)
        if divine > 0:
            self._rates["exalted"] = 1.0
            self._rates["divine"] = divine
            if chaos_per_divine > 0:
                self._rates["chaos"] = divine / chaos_per_divine
        return True

    def use_cache(self, *, reload: bool = False) -> bool:
        """Поднимает сохранённые цены с диска, не трогая сеть.

        Нужно, чтобы оверлей начал показывать цены сразу после запуска, а
        обновление из обменника шло фоном (полный обход занимает минуты).
        """
        if not reload and (self._book or self._scout):
            return True
        return self._load_cache()

    def switch_league(self, league: str) -> bool:
        """Переключает книгу на другую лигу, не смешивая цены разных лиг.

        Цены лиг несовместимы, поэтому память очищается полностью и заново
        поднимается кэш именно этой лиги (``prices-<лига>.json``). Возвращает
        True, если для новой лиги нашёлся сохранённый кэш.
        """
        with self._lock:
            self.league = str(league or "").strip()
            self._book = {}
            self._rates = {}
            # Курс из агрегата тоже относится к лиге: без сброса после смены
            # лиги пересчёт divine/chaos шёл по курсу прежней лиги.
            self._ninja_rates = {}
            self._gems = {}
            self._gem_source = {}
            self._scout = {}
            self._scout_volume = {}
            self._exchange = {}
            self._exchange_pairs = {}
            self._ids = {}
            self._base_ids = {}
            self._loaded_names = {}
            self._exchange_at = 0.0
            self._scout_at = 0.0
            self._loaded_at = 0.0
            self._rates_at = 0.0
            loaded = self._load_cache()
            if not loaded:
                self._merge_bundled_exchange_ids()
            return loaded

    def load_exchange(self, client: httpx.Client) -> bool:
        """Официальный часовой снапшот игрового обменника: цены и объёмы.

        Документация GGG: `GET /api/currency-exchange/poe2/<метка часа>`, где
        метка — начало прошлого часа (текущий час ещё не закрыт; без метки
        отдаётся самый первый час истории — данные 2024 года).

        Для каждого рынка (пара предметов) указаны границы сделок и объём.
        Цену предмета в exalted даёт пара с Exalted Orb: `ratio` со стороны
        exalted — сколько exalted дают за одну штуку предмета.
        """
        stamp = int(time.time()) - (int(time.time()) % 3600) - 3600
        resp = client.get(f"{EXCHANGE_CDN}/{stamp}", headers=SCOUT_HEADERS, timeout=90)
        if resp.status_code != 200:
            return False
        markets = resp.json().get("markets") or []
        return self.apply_exchange_snapshot(markets)

    def apply_exchange_snapshot(self, markets: list) -> bool:
        """Разбирает рынки официального снимка (без сети — удобно проверять)."""
        by_path = {path: name for name, path in self._base_ids.items()}
        prices: dict[str, tuple[float, float, int]] = {}
        # Все пары снимка, а не только пара с Exalted. Зеркало Каландры, Прядь
        # Хинекоры, совершенные сферы хаоса торгуются за Divine, и в паре с
        # Exalted их просто нет — без этой таблицы они оставались без цены.
        pairs: dict[str, list[tuple[str, float, float, int]]] = {}
        for market in markets:
            if str(market.get("league")) != self.league:
                continue
            pair = market.get("market_pair") or []
            low = market.get("lowest_ratio") or {}
            high = market.get("highest_ratio") or {}
            volume = market.get("volume_traded") or {}
            for item_path in pair:
                other_path = pair[1] if item_path == pair[0] else pair[0]
                try:
                    item_low = float(low.get(item_path))
                    item_high = float(high.get(item_path))
                    other_low = float(low.get(other_path))
                    other_high = float(high.get(other_path))
                except (TypeError, ValueError):
                    continue
                if min(item_low, item_high, other_low, other_high) <= 0:
                    continue
                # Цена штуки item в единицах other: сколько other дают за item.
                endpoints = sorted((other_low / item_low, other_high / item_high))
                pairs.setdefault(item_path, []).append(
                    (other_path, endpoints[0], endpoints[1],
                     int(volume.get(item_path) or 0)))
            if EXALTED_PATH not in pair:
                continue
            for path in pair:
                if path == EXALTED_PATH:
                    continue
                name = by_path.get(path)
                if not name:
                    continue
                # Ratio — это количество КАЖДОЙ стороны пары на одну сделку,
                # а не цена предмета в exalted сама по себе. Например, 3
                # transmute : 1 exalted означает 1/3 exalted за transmute.
                item_low, item_high = low.get(path), high.get(path)
                ex_low, ex_high = low.get(EXALTED_PATH), high.get(EXALTED_PATH)
                if not item_low or not item_high or not ex_low or not ex_high:
                    continue
                endpoints = (float(ex_low) / float(item_low),
                             float(ex_high) / float(item_high))
                bottom, top = sorted(endpoints)
                traded = int(volume.get(path) or 0)
                # Если по предмету несколько пар, берём ту, где больше сделок.
                old = prices.get(name)
                if old is None or traded > old[2]:
                    prices[name] = (bottom, top, traded)
        if not prices and not pairs:
            return False
        self._exchange = prices
        self._exchange_pairs = pairs
        self._exchange_at = time.time()
        self._loaded_names = by_path
        return True

    def load(self, *, force: bool = False, client: httpx.Client | None = None,
             detect_league: bool = False) -> None:
        """Обновляет таблицу цен и курс, если кэш устарел."""
        owns = client is None
        client = client or netenv.client(timeout=30, headers=NINJA_HEADERS)
        try:
            # Авто-режим определяет текущую лигу при каждом полном проходе.
            # Явно выбранную пользователем лигу не перезаписываем.
            if detect_league or not self.league:
                self.league = self.detect_league(client)
            fresh = (time.time() - self._loaded_at) < BOOK_TTL
            rates_fresh = (time.time() - self._rates_at) < RATE_TTL
            scout_fresh = (time.time() - self._scout_at) < SCOUT_TTL
            if (not force and (self._book or self._scout) and fresh and scout_fresh
                    and rates_fresh):
                return
            if not force and not self._book and not self._scout and self._load_cache():
                return

            # 1. Справочник названий и внутренних путей (редко меняется).
            if force or not self._scout or not scout_fresh:
                try:
                    self.load_scout(client)
                except Exception as exc:  # noqa: BLE001 — без обменника работаем по агрегату
                    # Причину обязательно в лог: молчаливый пропуск источника
                    # выглядит как «цены нет, а почему — неизвестно».
                    print(f"[цены] обменник poe2scout недоступен: "
                          f"{type(exc).__name__}: {exc}", flush=True)
            # 2. Официальный часовой снапшот обменника: цены в exalted с объёмами.
            exchange_fresh = (time.time() - self._exchange_at) < EXCHANGE_TTL
            if (force or not self._exchange or not exchange_fresh) and self._base_ids:
                try:
                    self.load_exchange(client)
                except Exception as exc:  # noqa: BLE001 — снапшот недоступен: цена из таблиц
                    print(f"[цены] официальный снапшот обменника недоступен: "
                          f"{type(exc).__name__}: {exc}", flush=True)

            book: dict[str, float] = {}
            for cat in CATEGORIES:
                try:
                    resp = client.get(NINJA_OVERVIEW,
                                      params={"league": self.league, "type": cat})
                    resp.raise_for_status()
                    data = resp.json()
                except (httpx.HTTPError, ValueError):
                    continue
                rate = float((data.get("core", {}).get("rates") or {}).get("exalted", 1.0))
                if not self._ninja_rates:
                    self._ninja_rates = {
                        k: float(v) for k, v in (data.get("core", {}).get("rates") or {}).items()
                    }
                names = {i["id"]: i["name"] for i in data.get("items", [])}
                # Ссылки на иконки: их отдаёт та же таблица цен.
                for item in data.get("items", []):
                    ident, image = item.get("id"), item.get("image")
                    if ident and image:
                        self.icons.setdefault(ident, image)
                for line in data.get("lines", []):
                    name = names.get(line.get("id"))
                    value = line.get("primaryValue")
                    if name and value is not None:
                        book[name] = float(value) * rate
            if book:
                self._book = book
                self._loaded_at = time.time()
            # Курсы: живые из игрового обменника важнее. Если их нет (обменник
            # недоступен) — считаем по трейду, иначе берём из агрегата.
            if force or not rates_fresh or "divine" not in self._rates \
                    or "chaos" not in self._rates:
                new_rates = self._trade_rates(client)
                if new_rates.get("divine") and new_rates.get("chaos"):
                    self._rates = new_rates
                    self._rates_at = time.time()
            self._save_cache()
        finally:
            # Проверенные биржевые ID нужны и после сетевого обновления, и после
            # раннего выхода: без них уровневые награды уходили в «цену не
            # нашли», хотя в обменнике они есть.
            self._merge_bundled_exchange_ids()
            if owns:
                client.close()

    # --- курс валют ----------------------------------------------------------

    def _throttle(self) -> None:
        """Оставлено для совместимости: темп теперь держит TradeLimiter."""
        self._limiter.wait("other")

    def _trade_get(self, client: httpx.Client, url: str, kind: str = "fetch",
                   **kw) -> httpx.Response | None:
        """Запрос к трейду с соблюдением лимитов. None — обращаться сейчас нельзя."""
        background = self._background_refresh
        if not background and self._limiter.blocked_for(kind) > MAX_INLINE_WAIT:
            return None
        resp = None
        for attempt in range(TRADE_ATTEMPTS):
            if not self._limiter.wait(kind,
                                      max_wait=None if background else INLINE_WAIT_LIMIT,
                                      on_wait=self._progress_callback if background else None):
                return None
            try:
                resp = client.get(url, headers=TRADE_HEADERS,
                                  timeout=httpx.Timeout(TRADE_TIMEOUT, connect=5.0), **kw)
            except httpx.HTTPError as exc:
                self._last_trade_error = f"сеть: {type(exc).__name__}"
                time.sleep(1.5 * (attempt + 1))
                continue
            self._limiter.observe(kind, resp.headers)
            if resp.status_code == 429:
                if not background and self._limiter.blocked_for(kind) > MAX_INLINE_WAIT:
                    return resp
                # observe() уже записал Retry-After. Следующая итерация пройдёт
                # через wait(), который показывает обратный отсчёт и паузу.
                continue
            if _is_transient(resp.status_code):
                # 403 Cloudflare / 5xx — сбой на стороне сайта. Это не отказ по
                # существу: повтор обычно проходит. Раньше такой ответ сразу
                # ронял весь проход.
                time.sleep(2.0 * (attempt + 1))
                continue
            return resp
        return resp

    def _trade_post(self, client: httpx.Client, url: str, payload: dict,
                    kind: str = "search") -> httpx.Response | None:
        background = self._background_refresh
        if not background and self._limiter.blocked_for(kind) > MAX_INLINE_WAIT:
            return None
        resp = None
        for attempt in range(TRADE_ATTEMPTS):
            if not self._limiter.wait(kind,
                                      max_wait=None if background else INLINE_WAIT_LIMIT,
                                      on_wait=self._progress_callback if background else None):
                return None
            try:
                resp = client.post(url, content=json.dumps(payload), headers=TRADE_HEADERS,
                                   timeout=httpx.Timeout(TRADE_TIMEOUT, connect=5.0))
            except httpx.HTTPError as exc:
                self._last_trade_error = f"сеть: {type(exc).__name__}"
                time.sleep(1.5 * (attempt + 1))
                continue
            self._limiter.observe(kind, resp.headers)
            if resp.status_code == 429:
                if not background and self._limiter.blocked_for(kind) > MAX_INLINE_WAIT:
                    return resp
                # observe() уже записал Retry-After; см. аналогичный GET выше.
                continue
            if _is_transient(resp.status_code):
                time.sleep(2.0 * (attempt + 1))
                continue
            return resp
        return resp

    def _ratios(self, client: httpx.Client, have: str, want: str) -> list[float]:
        """Все соотношения want/have из обменных лотов (включая мусорные заявки)."""
        body = {
            "query": {
                "status": {"option": "online"},
                "have": [have],
                "want": [want],
            },
            "sort": {"have": "asc"},
            "engine": "new",
        }
        resp = self._trade_post(client, f"{TRADE}/exchange/poe2/{self.league}", body,
                                kind="exchange")
        if resp is None or resp.status_code != 200:
            return []
        try:
            entries = resp.json().get("result") or []
        except ValueError:
            return []
        if isinstance(entries, dict):
            entries = list(entries.values())
        out: list[float] = []
        for entry in entries:
            for offer in (entry.get("listing", {}) or {}).get("offers", []) or []:
                ex = offer.get("exchange") or {}
                it = offer.get("item") or {}
                if str(ex.get("currency", "")).lower() != have:
                    continue
                if str(it.get("currency", "")).lower() != want:
                    continue
                amount_have = float(ex.get("amount") or 0)
                amount_want = float(it.get("amount") or 0)
                if amount_have > 0 and amount_want > 0:
                    out.append(amount_want / amount_have)
        return out

    def _exchange_rate(self, client: httpx.Client, cur: str) -> tuple[float | None, bool]:
        """Сколько exalted стоит 1 единица валюты и подтверждён ли курс с двух сторон.

        Среди обменных лотов есть откровенный мусор (1:1000 и наоборот), поэтому
        смотрим с двух сторон: одна даёт верхнюю оценку, другая — нижнюю. Если
        оценки близки, курс надёжен; если расходятся в разы — доверять нельзя.
        """
        forward = [r for r in self._ratios(client, cur, "exalted") if 0 < r < 1e6]
        backward = [r for r in self._ratios(client, "exalted", cur) if 0 < r < 1e6]
        top = max(forward) if forward else None
        bottom = (1.0 / min(backward)) if backward else None
        if top and bottom:
            consistent = max(top, bottom) / max(min(top, bottom), 1e-9) <= 1.6
            return statistics.median([top, bottom]), consistent
        value = top or bottom
        return value, False

    def _fallback_rates(self) -> dict[str, float]:
        """Курс из обменной таблицы poe.ninja — устойчивый, хоть и осреднённый."""
        rates: dict[str, float] = {"exalted": 1.0}
        raw = self._ninja_rates or {}
        ex_per_divine = float(raw.get("exalted") or 0)
        chaos_per_divine = float(raw.get("chaos") or 0)
        if ex_per_divine > 0:
            rates["divine"] = ex_per_divine
            if chaos_per_divine > 0:
                rates["chaos"] = ex_per_divine / chaos_per_divine
        return rates

    def _trade_rates(self, client: httpx.Client) -> dict[str, float]:
        """Курс в exalted: живые лоты, если они согласованы, иначе агрегат."""
        fallback = self._fallback_rates()
        rates: dict[str, float] = {"exalted": 1.0}
        for cur in ("divine", "chaos"):
            value, consistent = self._exchange_rate(client, cur)
            if value and consistent:
                rates[cur] = value
            elif cur in fallback:
                rates[cur] = fallback[cur]
            elif value:
                rates[cur] = value
        return rates

    def convert(self, amount: float, unit: str) -> float | None:
        """Приводит цену лота к exalted по текущему известному курсу.

        Поиск предметов может вернуть цену в любой игровой валюте (например,
        ``transmute`` или ``regal``). Для них используем только свежий часовой
        официальный курс с достаточным объёмом и узким спредом; неизвестные
        или ненадёжные валюты не превращаем в выдуманную цену.
        """
        unit = (unit or "").lower()
        if unit in ("exalted", "exalt", "ex"):
            return amount
        rate = self._rates.get(unit)
        if rate:
            return amount * rate

        # У Trade Search валютные коды короче ID предметов: ``transmute`` →
        # ``Orb of Transmutation``, ``regal`` → ``Regal Orb``.
        item_name = next((name for name, item_id in self._ids.items()
                          if str(item_id).lower() == unit), None)
        if not item_name:
            # Справочник обменника грузится раз в сутки; если его ещё нет,
            # короткий код всё равно должен опознаваться, иначе лот в «vaal»
            # или «alch» выпадал из расчёта цены камня.
            item_name = TRADE_CURRENCY_ALIASES.get(unit)
        if not item_name:
            return None
        quote = self._exchange.get(item_name)
        if quote:
            low, high, traded = quote
            if (traded >= MIN_OFFICIAL_TRADES and low > 0 and high >= low
                    and high / low <= MAX_OFFICIAL_SPREAD):
                return amount * ((low + high) / 2.0)
        # У алхимии и сферы царей официальная пара слишком широкая (2.25..6.5),
        # но это не повод считать лот в такой валюте неизвестным: у агрегаторов
        # по этим валютам огромный объём, значит цена устойчивая. Без этого
        # «Пустая оболочка» и «Проводящие руны» оставались без цены, хотя лотов
        # сотни.
        volume = self._scout_volume.get(item_name, 0)
        aggregate = self._scout.get(item_name)
        if aggregate and aggregate > 0 and volume >= MIN_SCOUT_VOLUME:
            return amount * float(aggregate)
        baseline = self._book.get(item_name)
        if baseline and baseline > 0:
            return amount * float(baseline)
        return None

    # --- камни ---------------------------------------------------------------

    def _listing_prices(self, client: httpx.Client, type_name: str, *,
                        level: int | None, limit: int = 10) -> list[tuple[str, float]] | None:
        """Первые лоты предмета: [(валюта, количество), …].

        Один запрос уже содержит лоты во всех валютах. Берём первые десять и
        сравниваем их только после перевода каждой цены в exalted.

        Десять, а не пять: сайт сортирует лоты по цене внутри своей валюты, а
        не по курсу, поэтому дешёвый лот в divine мог оказаться за пределами
        первой пятёрки и цена выходила завышенной (например, 13 chaos вместо
        1 divine за тот же камень).

        Больше десяти идентификаторов нельзя передавать в один запрос `fetch`:
        API отвечает `400 Invalid query`, из-за чего камень ошибочно оставался
        без цены.

        None — запрос не удался (лимит/ошибка); [] — запрос прошёл, но лотов нет.
        Разница важна: при ошибке не нужно повторять поиск без фильтра уровня.
        """
        self._last_trade_error = None
        # Структура API: misc_filters -> filters -> gem_level. Раньше здесь
        # получалось лишнее вложение `filters`, поэтому сервер молча игнорировал
        # уровень и отдавал цену дешёвого камня другого уровня.
        data = None
        ids: list[str] = []
        # Сначала мгновенный выкуп. Если лотов нет, повторяем тот же поиск со
        # статусом online: у валютных наград (сплавы, руны, сферы) фиксированных
        # лотов не бывает, а раньше пустой ответ считался «лотов нет» — цена
        # уходила в агрегатор, который по сплавам завышал её в 4–15 раз.
        for status in ("securable", "online"):
            resp = self._trade_post(client, f"{TRADE}/search/poe2/{self.league}",
                                    _listing_query(type_name, level, status=status),
                                    kind="search")
            if resp is None:
                self._last_trade_error = (self._last_trade_error
                                          or "поисковый запрос не выполнен")
                return None
            if resp.status_code != 200:
                self._last_trade_error = f"поиск вернул HTTP {resp.status_code}"
                return None
            try:
                data = resp.json()
            except ValueError:
                self._last_trade_error = "поиск вернул не-JSON ответ"
                return None
            ids = data.get("result", [])[:limit]
            if ids:
                break
        if not ids:
            return []
        got = self._trade_get(client, f"{TRADE}/fetch/{','.join(ids)}",
                              kind="fetch", params={"query": data.get("id")})
        if got is None:
            self._last_trade_error = self._last_trade_error or "запрос лотов не выполнен"
            return None
        if got.status_code != 200:
            self._last_trade_error = f"лоты вернули HTTP {got.status_code}"
            return None
        out: list[tuple[str, float]] = []
        try:
            results = got.json().get("result", [])
        except ValueError:
            self._last_trade_error = "лоты вернули не-JSON ответ"
            return None
        for entry in results:
            price = (entry.get("listing", {}) or {}).get("price") or {}
            if price.get("amount"):
                out.append((str(price.get("currency", "")).lower(), float(price["amount"])))
        return out

    def price_gem(self, en_name: str, level: int | None, *,
                  client: httpx.Client | None = None, force: bool = False) -> float | None:
        """Цена камня умения в exalted (медиана первых лотов), с кэшем."""
        key = f"{en_name}|{level or 0}"
        with self._lock:
            cached = self._gems.get(key)
            if not force and cached and (time.time() - cached[1]) < TRADE_CACHE_TTL:
                return cached[0]

        owns = client is None
        client = client or netenv.client(timeout=30)
        try:
            # Уровень — свойство самого товара. Если для точного уровня нет
            # fixed-buyout лотов, честный ответ — «цены нет», а не стоимость
            # другого уровня того же камня.
            prices = self._listing_prices(client, en_name, level=level)
        finally:
            if owns:
                client.close()
        if not prices:
            return None
        chosen = _representative_buyout(prices, self.convert)
        if chosen is None:
            return None
        value, source = chosen
        with self._lock:
            self._gems[key] = (value, time.time())
            self._gem_source[key] = source
            self._save_cache()
        return value

    def gem_source(self, en_name: str, level: int | None) -> tuple[str, float] | None:
        """Валюта и количество самого дешёвого лота (для понятного показа)."""
        with self._lock:
            return self._gem_source.get(f"{en_name}|{level or 0}")

    @property
    def last_trade_error(self) -> str:
        """Последняя ошибка торгового сайта (например «поиск вернул HTTP 429»).

        Оверлею нужна, чтобы отличить «цены нет» от «лимит запросов исчерпан»
        и не повторять поиск, пока лимит не отпустит.
        """
        return self._last_trade_error or ""

    def cached_gem_price(self, en_name: str, level: int | None) -> float | None:
        """Свежая сохранённая цена камня без сетевого запроса.

        Нужна оверлею при Shift+D: нажатие должно мгновенно показывать готовый
        суточный снимок, а не создавать новую очередь trade API.

        Строго по ключу «камень + уровень». Цену другого уровня или другого
        предмета подставлять нельзя: у каждого уровня свой лот и своя цена.
        Нет точной записи — честно возвращаем None (в панели это «?»).
        """
        key = f"{en_name}|{level or 0}"
        with self._lock:
            cached = self._gems.get(key)
            if not cached or (time.time() - cached[1]) >= TRADE_CACHE_TTL:
                return None
            return cached[0] or None

    # --- поиск цены для награды ---------------------------------------------

    def price_exchange(self, en_name: str, *,
                       client: httpx.Client | None = None,
                       force: bool = False) -> float | None:
        """Самая низкая активная заявка Currency Exchange из пяти дешёвых.

        Один запрос охватывает exalted, chaos и divine. Суммы нормализуются к
        цене за штуку и переводятся в exalted; агрегаторы не отбрасывают заявки.
        """
        if not en_name:
            return None
        self._last_trade_error = None
        key = f"item|{en_name}"
        with self._lock:
            cached = self._gems.get(key)
            if not force and cached and (time.time() - cached[1]) < TRADE_CACHE_TTL:
                return cached[0] or None

        api_id = self._ids.get(en_name) or _slugify(en_name)
        if not api_id or len(api_id) < 3:
            return None
        owns = client is None
        client = client or netenv.client(timeout=30)
        prices: list[tuple[str, float]] = []
        try:
            body = {
                "query": {
                    "status": {"option": "online"},
                    "have": ["exalted", "chaos", "divine"],
                    "want": [api_id],
                },
                "sort": {"have": "asc"},
                "engine": "new",
            }
            resp = self._trade_post(client, f"{TRADE}/exchange/poe2/{self.league}", body,
                                    kind="exchange")
            if resp is None:
                if not self._last_trade_error:
                    self._last_trade_error = "exchange request not sent (API limiter)"
                return None
            if resp.status_code != 200:
                self._last_trade_error = f"Currency Exchange HTTP {resp.status_code}"
                return None
            prices.extend(_exchange_offer_prices(resp.json(), api_id))
            chosen = _representative_buyout(prices, self.convert)
            value = chosen[0] if chosen else None
            # Самая низкая из пяти дешёвых заявок — это и есть цена покупки. Но
            # у ходовых валют и рун рядом стоят заявки «1 divine за одну сферу»,
            # поэтому заявка, которая дороже рыночных таблиц больше чем втрое,
            # ценой не считается.
            if value is not None:
                reference = self.reference_price(en_name)
                if reference and value > reference * MAX_LIVE_DEVIATION:
                    value = None
                    chosen = None

            if chosen:
                self._gem_source[f"{en_name}|0"] = chosen[1]
        except (httpx.HTTPError, ValueError, TypeError):
            self._last_trade_error = "не удалось прочитать ответ Currency Exchange"
            return None
        finally:
            if owns:
                client.close()
        with self._lock:
            if value:
                self._gems[key] = (value, time.time())
            elif force:
                self._gems.pop(key, None)
                self._gem_source.pop(f"{en_name}|0", None)
            self._save_cache()
        return value

    def official_price_or_proof(self, en_name: str, *,
                                client: httpx.Client | None = None
                                ) -> tuple[float | None, str]:
        """Официальная проверка предмета, который таблицы цен не знают.

        Правило проекта: источники спрашиваются по порядку, и «цены нет»
        говорится только после официального ответа. Здесь два официальных
        источника подряд — торговый поиск с мгновенным выкупом и заявки игрового
        обменника. Возвращает (цену или None, что именно ответили источники):
        строку-доказательство записываем в каталог рядом с наградой, чтобы
        «нет цены» не было отпиской.

        Ничего не подставляет и не додумывает: предмет спрашивается ровно под
        своим именем и своим уровнем.
        """
        owns = client is None
        client = client or netenv.client(timeout=30)
        notes: list[str] = []
        value: float | None = None
        try:
            # 1. Официальный торговый поиск: лоты с мгновенным выкупом.
            prices = self._listing_prices(client, en_name, level=None)
            if prices:
                chosen = _representative_buyout(prices, self.convert)
                if chosen is not None:
                    value = chosen[0]
                    notes.append(f"официальный поиск: лотов {len(prices)}")
            elif prices == []:
                notes.append("официальный поиск: лотов 0")
            else:
                notes.append("официальный поиск: "
                             + (self._last_trade_error or "нет ответа"))
            # 2. Официальный обменник: заявки по тегу предмета.
            if value is None:
                api_id = self._ids.get(en_name) or _slugify(en_name)
                body = {
                    "query": {"status": {"option": "online"},
                              "have": ["exalted", "chaos", "divine"],
                              "want": [api_id]},
                    "sort": {"have": "asc"},
                    "engine": "new",
                }
                resp = self._trade_post(client, f"{TRADE}/exchange/poe2/{self.league}",
                                        body, kind="exchange")
                if resp is None:
                    notes.append("обменник: запрос не отправлен")
                elif resp.status_code != 200:
                    notes.append(f"обменник: HTTP {resp.status_code}")
                else:
                    offers = _exchange_offer_prices(resp.json(), api_id)
                    if offers:
                        chosen = _representative_buyout(offers, self.convert)
                        if chosen is not None:
                            reference = self.reference_price(en_name)
                            if reference and chosen[0] > reference * MAX_LIVE_DEVIATION:
                                notes.append(f"обменник: заявок {len(offers)}, "
                                             "все дороже рынка больше чем втрое")
                            else:
                                value = chosen[0]
                                notes.append(f"обменник: заявок {len(offers)}")
                    else:
                        notes.append("обменник: заявок 0")
        except (httpx.HTTPError, ValueError, TypeError):
            notes.append("официальный запрос не удался: "
                         + (self._last_trade_error or "нечитаемый ответ"))
        finally:
            if owns:
                client.close()
        return value, "; ".join(notes)

    def has_bulk_price(self, en_name: str) -> bool:
        """Есть ли у предмета цена «пачечного» рынка (обменник или таблицы)."""
        return bool(en_name) and (en_name in self._exchange or en_name in self._scout
                                  or en_name in self._book)

    def uncut_gem_range(self, family: str) -> tuple[float, float] | None:
        """Диапазон цены камня без огранки случайного уровня.

        На награде уровень не написан, а уровни стоят очень по-разному. Берём
        минимум и максимум известных уровней из уже загруженных таблиц, не делая
        запросов на трейд для несуществующих уровней.
        """
        info = bundled_exchange_families().get(family)
        levels = info["levels"] if info else None
        if not levels:
            return None
        names = set(self._exchange) | set(self._scout) | set(self._book)
        values: list[float] = []
        for level in levels:
            name = f"{family} (Level {level})"
            if name not in names:
                continue
            value = self.price_item(name, live=False)
            if value is not None and value > 0:
                values.append(value)
        return (min(values), max(values)) if values else None

    def uncut_spirit_gem_range(self) -> tuple[float, float] | None:
        """Диапазон цены неогранённого камня духа случайного уровня."""
        return self.uncut_gem_range("Uncut Spirit Gem")

    def trades_in_exchange(self, en_name: str) -> bool:
        """Торгуется ли предмет «пачками» в игровом обменнике."""
        return bool(en_name) and (en_name in self._ids or en_name in self._scout
                                  or en_name in self._book)

    def random_estimate(self, en_name: str) -> tuple[float, float, str] | None:
        """Диапазон для награды со случайным содержимым в exalted.

        Возвращает ``(низ, верх, пояснение)``. Сами цены в формуле берутся из
        обновляемой базы, поэтому оценка меняется вместе с рынком.
        """
        if en_name == "Verisium Pile":
            liquid = self.price_item("Liquid Verisium", live=False)
            if not liquid or liquid <= 0:
                return None
            low_qty, high_qty = VERISIUM_PILE_RANGE
            return liquid * low_qty / 600, liquid * high_qty / 600, "200–400 Verisium"
        if en_name == "5x Random Currency":
            values = [self.price_item(name, live=False) for name in RANDOM_CURRENCY_BASKET]
            values = [value for value in values if value and value > 0]
            if not values:
                return None
            typical = statistics.median(values) * 5
            # Коридор вокруг базовой оценки: это не обещание результата, а
            # читаемый ориентир для обычных исходов.
            return typical * 0.6, typical * 1.4, "5 случайных валют"
        if en_name in bundled_exchange_families():
            # Камень без огранки выдаётся случайного уровня, поэтому цена — это
            # диапазон уровней из обменника, а не одна цифра.
            span = self.uncut_gem_range(en_name)
            if span is None:
                return None
            return span[0], span[1], f"{en_name}: случайный уровень"
        return None

    def price_item(self, en_name: str, *, live: bool = True) -> float | None:
        """Цена предмета: заявки игрового обменника, затем таблицы.

        live=False — только таблицы, без обращения к обменнику: так первый показ
        получается мгновенным, а живую цену фоном догружает отдельный поток.
        """
        if not en_name:
            return None
        # Пачечные предметы (валюта, руны, сплавы) торгуются в обменнике, и там
        # цена — это снимок сделок и агрегаторы. Живая книга заявок обменника
        # («1 exalted за сплав», «1 divine за сферу усиления») ценой не является,
        # поэтому для таких предметов она вообще не спрашивается.
        bulk = en_name in self._ids or en_name in self._base_ids
        if not bulk:
            direct_listing = self._market_price(en_name)
            if direct_listing is not None:
                return direct_listing
        # Таблицы: официальный снимок обменника и агрегаторы, взятые по
        # согласию источников. Раньше здесь первым шёл один часовой срез, и у
        # дешёвых валют его грубая пара (1..2 exalted за сферу) давала цену
        # ниже рынка, а у дорогих рун — выше.
        consensus = self.exchange_price(en_name)
        if consensus is not None:
            return consensus
        base = ru_to_en().get(en_name)
        if base:
            consensus = self.exchange_price(base)
            if consensus is not None:
                return consensus
        if not bulk:
            direct_listing = self._market_price(en_name)
            if direct_listing is not None:
                return direct_listing
        # «Только кэш» используется для первого показа и прогрева каталога.
        # Он не должен превращать сотни неизвестных строк в запросы к трейду.
        if not live:
            return None
        return self.price_item_trade(base or en_name)

    # --- официальный снимок обменника ---------------------------------------

    def _currency_rate(self, path: str) -> float | None:
        """Курс валюты (внутренний путь) в exalted: 1 divine = ~759 ex и т. п."""
        # Exalted — базовая валюта: её курс не зависит от справочника названий.
        if path == EXALTED_PATH:
            return 1.0
        name = self._loaded_names.get(path)
        if not name:
            return None
        code = self._ids.get(name)
        if code:
            rate = self._rates.get(str(code).lower())
            if rate and rate > 0:
                return float(rate)
        # Курса в «ставках» нет: берём надёжный агрегат самой валюты.
        volume = self._scout_volume.get(name, 0)
        aggregate = self._scout.get(name)
        if aggregate and aggregate > 0 and volume >= MIN_SCOUT_VOLUME:
            return float(aggregate)
        baseline = self._book.get(name)
        if baseline and baseline > 0:
            return float(baseline)
        quote = self._exchange.get(name)
        if quote:
            low, high, traded = quote
            if (traded >= MIN_OFFICIAL_TRADES and low > 0 and high >= low
                    and high / low <= MAX_OFFICIAL_SPREAD):
                # Курс тоже берём по низу сделок: пересчёт чужой цены не должен
                # завышать итог.
                return low
        return None

    def snapshot_price(self, en_name: str) -> tuple[float, float, int, str] | None:
        """Официальный часовой снимок обменника: (низ, верх, сделок, валюта).

        Снимок — это агрегат реально состоявшихся сделок за час, а не список
        заявок. Пара с Exalted даёт цену напрямую; для предметов, которые за
        Exalted не торгуются (Зеркало, Прядь Хинекоры), цена берётся из пары с
        Divine или Chaos и переводится по курсу. Возвращаются границы сделок в
        exalted за штуку.
        """
        path = self._base_ids.get(en_name)
        if not path:
            return None
        best: tuple[float, float, int, str] | None = None
        for other_path, low, high, traded in self._exchange_pairs.get(path, []):
            if traded < MIN_OFFICIAL_TRADES:
                continue
            other = self._currency_rate(other_path)
            if not other or other <= 0:
                continue
            ex_low, ex_high = low * other, high * other
            if ex_low <= 0:
                continue
            if other_path == EXALTED_PATH:
                return (ex_low, ex_high, traded, "exalted")
            # Пары без Exalted грубее (3 совершенные сферы за 1 divine), но это
            # единственная официальная цена таких предметов.
            if best is None or traded > best[2]:
                best = (ex_low, ex_high, traded,
                        str(self._ids.get(self._loaded_names.get(other_path) or "") or ""))
        return best

    def snapshot_unit(self, en_name: str) -> float | None:
        """Самая низкая цена сделки по официальному снимку обменника.

        Берём именно НИЖНЮЮ границу сделок, а не середину: по правилу проекта
        ценой предмета считается самая низкая цена, по которой он реально ушёл,
        а среднее между границами завышает цифру.
        """
        snap = self.snapshot_price(en_name)
        if snap is None:
            return None
        low, _high, _traded, _unit = snap
        return low

    def reference_price(self, en_name: str) -> float | None:
        """Надёжная рыночная цена из таблиц, без живых заявок трейд-сайта.

        Порядок: официальный снимок обменника → poe2scout (с объёмом) →
        poe.ninja. Живые заявки сюда не входят: их проверяют по этому ориентиру.
        """
        if not en_name:
            return None
        snap = self.snapshot_unit(en_name)
        if snap is not None:
            return snap
        baseline = self._book.get(en_name)
        if baseline and baseline > 0:
            return float(baseline)
        volume = self._scout_volume.get(en_name, 0)
        aggregate = self._scout.get(en_name)
        if aggregate and aggregate > 0 and volume >= MIN_SCOUT_VOLUME:
            return float(aggregate)
        if aggregate and aggregate > 0:
            return float(aggregate)
        return None

    def exchange_price(self, en_name: str, *, live_conflict: bool = False) -> float | None:
        """Цена обменного предмета — самая низкая цена, по которой он уходит.

        Основа — средняя poe.ninja (``_book``): она считается по настоящим
        продажам, покрывает почти весь список и по замерам ближе к рынку, чем
        poe2scout. Всё остальное может её только ПОНИЗИТЬ, потому что покупают
        всегда по самой низкой цене:

          1. poe.ninja — основа;
          2. официальный снимок обменника — цена реальных сделок; берётся,
             только если он ниже основы;
          3. poe2scout — если таблицы цену не знают;
          4. живые лоты сайта — по тонкой паре, если они втрое дешевле.

        Пара снимка, пересчитанная через чужую валюту (Chaos, Divine), выше
        основы не берётся: 14.10.2026 пара «Малая руна ледника | Chaos» дала
        5 chaos (343 exalted) за руну, которая в паре с Exalted стоила 1 exalted,
        а средняя poe.ninja — 34 exalted (0.5 chaos). Пятихаосовая пара в цену
        не попала.

        ``live_conflict=True`` — крайний случай: источники цену не знают, тогда
        берётся самая низкая из пяти дешёвых живых заявок обменника.
        """
        if not en_name:
            return None
        # 1. Основа — средняя poe.ninja.
        baseline = self._book.get(en_name)
        base = float(baseline) if baseline and baseline > 0 else None
        # 2. Официальный снимок: пара с Exalted измеряет цену напрямую в exalted,
        #    остальные пары (Divine, Chaos) грубее пересчитываются по курсу.
        snap: float | None = None
        thin = False
        snap_pair = self.snapshot_price(en_name)
        if snap_pair is not None:
            low, high, traded, _unit = snap_pair
            if (traded >= MIN_OFFICIAL_TRADES and low > 0 and high >= low
                    and high / low <= MAX_SNAPSHOT_PAIR_SPREAD):
                snap = low
                # Тонкая пара стоит на нескольких сделках за час: одну
                # нетипичную сделку в ней от рынка не отличить. Такую цену
                # проверяем по самому дешёвому лоту сайта, а не берём как есть.
                thin = traded <= THIN_PAIR_TRADES
        trusted: float | None = base
        if snap is not None and (trusted is None or snap < trusted):
            trusted = snap
        if trusted is not None:
            if thin:
                live = self._cheaper_live_listing(en_name, trusted)
                if live is not None:
                    return live
            return trusted
        # 3. poe2scout — только с подтверждённым объёмом сделок.
        volume = self._scout_volume.get(en_name, 0)
        aggregate = self._scout.get(en_name)
        if aggregate and aggregate > 0:
            if volume >= MIN_SCOUT_VOLUME:
                return float(aggregate)
            # Тонкий агрегат держится на одной-двух сделках и врёт на порядки:
            # «Малая руна заряда» — 40155 exalted (55 divine) при всей родне в
            # 3–50 exalted. Такую цифру подтверждаем лотом сайта: он и есть
            # цена, по которой предмет покупают. Лотов нет вовсе — цены нет.
            live = self._site_listing_price(en_name)
            if live is not None:
                return live
            return float(aggregate) if self._last_trade_error else None
        if live_conflict:
            return self.price_exchange(en_name, force=True)
        return None

    def _site_listing_price(self, en_name: str) -> float | None:
        """Самый дешёвый лот сайта. None — лотов нет или сайт недоступен.

        Успешная цена кэшируется на ``TRADE_CACHE_TTL``, а отсутствие лотов —
        на ``THIN_MISS_TTL``, чтобы не спрашивать их на каждой панели. Лимит
        сайта (429) и обрыв сети не запоминаются: это не «лотов нет».
        """
        miss_at = self._thin_miss.get(en_name, 0.0)
        if miss_at and (time.time() - miss_at) < THIN_MISS_TTL:
            return None
        live = self.price_item_trade(en_name)
        if not live or live <= 0:
            if not self._last_trade_error:
                self._thin_miss[en_name] = time.time()
            return None
        return float(live)

    def _cheaper_live_listing(self, en_name: str, price: float) -> float | None:
        """Дешёвый лот официального сайта, если он явно ниже ориентира.

        Нужен там, где ориентир тонкий: пара снимка на нескольких сделках.
        Сайт показывает то, что реально можно купить и продать сейчас, поэтому
        при расхождении больше чем втрое (тот же порог ``MAX_LIVE_DEVIATION``,
        что и для завышенных заявок) ценой становится лот сайта, а не одинокая
        сделка таблицы.
        """
        live = self._site_listing_price(en_name)
        if live is not None and live * MAX_LIVE_DEVIATION <= price:
            return live
        return None

    def _market_price(self, en_name: str) -> float | None:
        """Цена по живым лотам (устойчивая), без записи в кэш вызовов.

        Значение сверяется с таблицами: если лот дороже рынка больше чем
        втрое, он не выдаётся за цену предмета.

        Ключ лота сайта отдельный от ключа заявок обменника (``item|``):
        это разные рынки, и значение одного нельзя выдавать за другое. Ключ
        ``item|`` читаем только как запасной — в нём лежат цены прошлых версий.
        """
        with self._lock:
            cached = (self._gems.get(f"listing|{en_name}")
                      or self._gems.get(f"item|{en_name}"))
            if not cached or (time.time() - cached[1]) >= TRADE_CACHE_TTL:
                return None
            value = cached[0] or None
        if value is None:
            return None
        reference = self.reference_price(en_name)
        if reference and value > reference * MAX_LIVE_DEVIATION:
            return None
        return value

    def price_item_trade(self, en_name: str, *,
                         client: httpx.Client | None = None, force: bool = False) -> float | None:
        """Цена по живым лотам трейда (когда обменная таблица предмет не знает)."""
        if not en_name or len(en_name) < 3:
            return None
        if not _LATIN_RE.search(en_name):
            # Имя без единой латинской буквы в словаре не нашлось: это сырая
            # строка OCR (например «Уникальная бижутерия» — награда, которой
            # каталог не знает). В трейд такое имя отправлять нельзя: движок
            # поиска может проигнорировать незнакомое условие и вернуть чужие
            # лоты — а это уже перемешивание цен. Показываем «?», без цены.
            return None
        # Отдельный ключ от заявок обменника (``item|``): раньше они делили
        # одну запись, и цена обменника выдавалась за лот сайта (и наоборот).
        key = f"listing|{en_name}"
        with self._lock:
            cached = self._gems.get(key)
            if not force and cached and (time.time() - cached[1]) < TRADE_CACHE_TTL:
                return cached[0] or None

        owns = client is None
        client = client or netenv.client(timeout=30)
        try:
            prices = self._listing_prices(client, en_name, level=None)
        finally:
            if owns:
                client.close()
        # Только текущие лоты с фиксированным выкупом. Выбираем медиану трёх
        # самых дешёвых после пересчёта валют — без отдельных запросов по ex,
        # chaos или divine.
        chosen = _representative_buyout(prices or [], self.convert)
        if chosen:
            value, _source = chosen
            with self._lock:
                self._gems[key] = (value, time.time())
                self._save_cache()
            return value
        return None

    @property
    def size(self) -> int:
        return len(self._book) + len(self._scout)

"""Каталог фиксированных наград рунных комбинаций и ежедневный прогрев цен.

Источник списка: PoE2DB. Страница содержит список всех комбинаций и отдельную
вкладку Gems; мы сохраняем только награды с определённым предметом/количеством.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import math
import threading
from pathlib import Path

import httpx

from aldur_appraiser import netenv
from aldur_appraiser.leagues import league_slug
from aldur_appraiser.localize import ru_to_en
from aldur_appraiser.pricing.ru_prices import (
    PriceBook, bundled_exchange_families, price_cache_path,
)
from aldur_appraiser.ru_settings import RuSettings, config_dir
from aldur_appraiser.winproc import process_alive

CATALOG_URL = "https://poe2db.tw/Runeshape_Combinations"
TRADE_ITEM_DATA_URL = "https://www.pathofexile.com/api/trade2/data/items"
CATALOG_TTL = 24 * 60 * 60
REFRESH_INTERVAL = 24 * 60 * 60
# Меняем версию, когда меняется смысл торгового запроса. Иначе старый кэш,
# рассчитанный по другим правилам, ошибочно считается «полным сканированием».
PRICE_SCAN_SCHEMA = 7
# Рынок обменника меняется заметно быстрее, чем список комбинаций. Полный
# каталог с камнями не стоит греть при каждом старте (у трейда есть лимиты),
# но цены всех предметов обменника должны быть свежими.
MARKET_REFRESH_INTERVAL = 15 * 60
# Предел ожидания ответа «подожди» (HTTP 429) за один проход. Дольше ждать
# бессмысленно: это уже не лимит на частоту запросов, а блокировка.
MAX_RATE_LIMIT_WAIT = 10 * 60
_ROW_RE = re.compile(r'<div class="d-flex justify-content-between"><div>(.*?)</div><div>', re.S)
_TAGS_RE = re.compile(r"<[^>]+>")
_LEVEL_RE = re.compile(r"\s+Lv\d+(?:-\d+|\+)$")
_ITEM_LEVEL_RE = re.compile(r"\s+\(Level (\d+)\)$")
_QTY_RE = re.compile(r"^(.*?)\s+x(\d+)$")
# «Уникальное кольцо», «Rare Unique Item», «Unique Body Armour» — это не предмет,
# а описание награды: конкретного имени и цены у неё нет.
# Uncut-камни, наоборот, оставляем: они выдаются случайного уровня (1–10) и
# продаются — тип «Uncut Skill/Spirit/Support Gem» есть в справочнике трейда.
_GENERIC_RE = re.compile(r"^(?:(?:Very )?Rare )?Unique(?: item| .+)?$", re.I)

# Награды, которые нельзя продать другому игроку. Проверено 12.10.2026 по
# четырём независимым источникам: класс предмета на PoE2DB, официальный
# справочник типов trade API, словарь торгового сайта и снимок обменника.
#   * «Boon» четырёх боссов Экспедиции — квестовые предметы (QuestItem,
#     Metadata/Items/QuestItems/…): применяются правым кликом, не торгуются;
#   * Krillson's Bay Key — пасхалка (10 Bait Rune), страницы предмета нет;
#   * Thaumaturgic Flux (Level 5–7) — в торговой базе есть только уровни 8–20,
#     ни типа для поиска, ни пары в обменнике у этих уровней нет.
# «5x Random Currency» и «Verisium Pile» здесь намеренно НЕ перечислены: это
# случайные награды, их содержимое продаётся, цену даёт random_estimate().
NOT_FOR_SALE = frozenset({
    "Medved's Boon", "Uhtred's Boon", "Vorana's Boon", "Olroth's Boon",
    "Krillson's Bay Key",
    "Thaumaturgic Flux (Level 5)", "Thaumaturgic Flux (Level 6)",
    "Thaumaturgic Flux (Level 7)",
})
_STATE_LOCK = threading.RLock()
_SCAN_FAILED = threading.Event()


class ScanRequestError(RuntimeError):
    """Торговый запрос не дал валидного ответа; кэш нельзя помечать готовым."""


def catalog_path() -> Path:
    return config_dir() / "runeshape_catalog.json"


def catalog_archive_path(league: str) -> Path:
    """Каталог отдельной лиги: прошлые лиги не затираются новым проходом.

    Список наград у лиг общий (он берётся со страницы PoE2DB), но цены разные:
    Aldur's Saga стоит около 50 divine в Runes of Aldur и отсутствует в
    Forbidden Rites. Поэтому для каждой лиги держим свой снимок.
    """
    slug = league_slug(league)
    return config_dir() / (f"runeshape_catalog-{slug}.json" if slug
                           else "runeshape_catalog.json")


def refresh_state_path() -> Path:
    return config_dir() / "runeshape_refresh_state.json"


def _read_refresh_state() -> dict:
    try:
        data = json.loads(refresh_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_text_atomically(path: Path, payload: str) -> None:
    """Пишет файл через временный: обрыв записи не оставит обрезанный JSON.

    В имени временного файла — номер процесса: если файл пишут два процесса,
    они не затирают заготовку друг друга. Тем же способом пишется состояние
    прогресса: оверлей читает оба файла в любой момент.
    """
    tmp = path.with_suffix(f".{os.getpid()}{path.suffix}.tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _save_refresh_state(state: dict) -> None:
    """Атомарно сохраняет прогресс, чтобы трей мог читать его в любой момент."""
    try:
        _write_text_atomically(refresh_state_path(),
                               json.dumps(state, ensure_ascii=False, indent=2))
    except OSError:
        pass


# Метка жизни сканера для стража. Считается по монотонным часам: перевод
# системного времени не должен выглядеть как зависание.
_PROGRESS_AT = {"at": time.monotonic()}


def _set_progress(percent: int, phase: str, *, completed: int = 0,
                  total: int = 0, current_reward: str | None = None) -> None:
    _PROGRESS_AT["at"] = time.monotonic()
    with _STATE_LOCK:
        state = _read_refresh_state()
        if _SCAN_FAILED.is_set() or state.get("status") == "failed":
            return
        # Во время серверной паузы callback не всегда знает total. Не затираем
        # уже известные «все награды пройдены» значением 0/0 — это выглядело
        # как сбой сканера.
        known_total = int(state.get("total", 0) or 0)
        known_completed = int(state.get("completed", 0) or 0)
        if total <= 0 and known_total > 0:
            total = known_total
        if completed <= 0 and known_completed > 0:
            completed = known_completed
        state.update({
            "status": "scanning",
            "progress_percent": max(0, min(99, int(percent))),
            "phase": phase,
            "completed": completed,
            "total": total,
            "last_heartbeat_at": time.time(),
        })
        if current_reward is not None:
            state["current_reward"] = current_reward
        _save_refresh_state(state)


# Сколько секунд без единой отметки прогресса считаем зависанием. Заметно
# больше самого долгого одиночного запроса (45 с — страница каталога), чтобы
# медленный, но живой проход не был принят за зависший.
WATCHDOG_TIMEOUT_SECONDS = 120.0


def _start_watchdog(stop: threading.Event,
                    *, timeout: float = WATCHDOG_TIMEOUT_SECONDS) -> threading.Thread:
    """Останавливает проход, если он перестал подтверждать жизнь.

    Нормальная пауза лимитера вызывает _set_progress каждую секунду, поэтому
    сюда попадает только зависший запрос/сокет, а не разрешённое API ожидание.

    Раньше эта защита была в коде, но её никто не запускал: зависший обход
    оставлял статус «сканирование» навсегда, трей вечно показывал проценты,
    а замок мешал новому запуску. Часы берём монотонные: перевод системного
    времени не должен завершать исправный проход.
    """
    def watch() -> None:
        while not stop.wait(1.0):
            if time.monotonic() - _PROGRESS_AT["at"] <= timeout:
                continue
            _SCAN_FAILED.set()
            with _STATE_LOCK:
                state = _read_refresh_state()
                state.update({
                    "status": "failed",
                    "phase": "ошибка сканирования",
                    "error": f"HeartbeatTimeout: нет отметок прогресса {int(timeout)} с",
                    "last_heartbeat_at": time.time(),
                })
                _save_refresh_state(state)
            print(f"проход остановлен: нет отметок прогресса {int(timeout)} с", flush=True)
            # Данный процесс — отдельный обновитель; выходим, чтобы следующий
            # запуск не получил ложный lock от зависшего сетевого вызова.
            os._exit(70)

    thread = threading.Thread(target=watch, name="scan-watchdog", daemon=True)
    thread.start()
    return thread


def refresh_is_due(now: float | None = None) -> bool:
    """Нужно ли запускать суточное обновление.

    Старый timestamp сам по себе не доказывает, что обход завершился: нужен
    явный флаг, который ставится только после прохода всех наград и записи
    каталога.
    """
    try:
        state = json.loads(refresh_state_path().read_text(encoding="utf-8"))
        last_success = float(state.get("last_success_at", 0))
    except (OSError, ValueError, TypeError):
        return True
    if (state.get("price_scan_schema") != PRICE_SCAN_SCHEMA
            or state.get("status") != "ready" or not state.get("full_scan_verified")
            or int(state.get("completed", 0) or 0) < int(state.get("total", 1) or 1)):
        return True
    # Обновление без прогрева камней полезно для быстрой проверки, но не считаем
    # его полным суточным проходом: при следующем запуске надо догреть кэш.
    if not bool(state.get("warm_gems")):
        return True
    # Лига сменилась: цены прошлой лиги к новой не подходят. Например, в Runes
    # of Aldur Aldur's Saga торгуется, а в Forbidden Rites её не существует —
    # значит нужен новый проход именно по выбранной лиге.
    selected = RuSettings().load().league.strip()
    recorded = str(state.get("league") or "").strip()
    if selected and recorded and selected != recorded:
        return True
    return (now if now is not None else time.time()) - last_success >= REFRESH_INTERVAL


def market_refresh_is_due(now: float | None = None) -> bool:
    """Нужен ли новый снимок рынка валют/рун/материалов.

    Это отдельная проверка от суточного прогрева камней: ``prices.json``
    содержит время именно полного снимка рынка. Нет файла или времени —
    обновляем сразу при запуске.
    """
    moment = now if now is not None else time.time()
    # Свежесть рынка смотрим по файлу выбранной лиги: у каждой лиги свой снимок,
    # и обновление Runes of Aldur не должно выглядеть как свежий рынок
    # Forbidden Rites (и наоборот). Общий prices.json подходит только тогда,
    # когда в нём лежит рынок той же самой лиги: это первый запуск после
    # разделения кэшей, когда своего файла у лиги ещё нет.
    league = RuSettings().load().league.strip()
    candidates = [price_cache_path(league)]
    if league:
        candidates.append(price_cache_path(""))
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            updated = float(data.get("loaded_at", 0))
        except (OSError, ValueError, TypeError):
            continue
        cached = str(data.get("league") or "")
        if league and cached and cached != league:
            continue
        return moment - updated >= MARKET_REFRESH_INTERVAL
    return True


def _lock_path() -> Path:
    return config_dir() / "runeshape_refresh.lock"


def _lock_owner_is_alive(lock: Path) -> bool:
    """Возвращает True, только если PID из lock-файла ещё существует.

    После принудительного завершения/сбоя старый lock не должен блокировать
    следующий запуск шесть часов: это оставляло сканер внешне «зависшим».

    ВАЖНО про Windows: ``os.kill(pid, 0)`` там не проверяет процесс, а
    ЗАВЕРШАЕТ его (TerminateProcess). Такой «проверкой» живой проход можно
    было убить, а при отказе в доступе — наоборот, счесть живого мёртвым.
    Поэтому проверка живёт в одном месте — в winproc.process_alive.
    """
    try:
        pid = int(lock.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return process_alive(pid)


def _acquire_lock() -> bool:
    """Не даёт ночной задаче и запуску приложения обновлять рынок параллельно."""
    lock = _lock_path()
    try:
        if lock.exists() and (not _lock_owner_is_alive(lock)
                              or time.time() - lock.stat().st_mtime > 6 * 60 * 60):
            lock.unlink()  # остаток от аварийно завершившегося старого процесса
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
        return True
    except FileExistsError:
        return False


def _release_lock() -> None:
    """Снимает замок, только если он наш.

    Раньше файл замка удалялся без проверки владельца: завершение старого
    процесса снимало замок нового, и два сканера могли идти одновременно.
    """
    lock = _lock_path()
    try:
        if int(lock.read_text(encoding="utf-8").strip()) != os.getpid():
            return
    except (OSError, ValueError):
        return
    try:
        lock.unlink()
    except OSError:
        pass


SCAN_STALE_SECONDS = 120.0


def scan_state_is_stale(state: dict, *, timeout: float = SCAN_STALE_SECONDS) -> bool:
    """True, если запись «сканирование» осталась от умершего процесса.

    Обновитель мог быть закрыт принудительно (перезапуск компьютера, снятая
    задача). Тогда файл состояния навсегда остаётся в статусе scanning, и трей
    показывает зависший прогресс («73%») при том, что запросов уже нет.

    Признак живого прохода — свежий heartbeat: лимитер отмечается каждую
    секунду, поэтому свежее время означает, что проход идёт, и прогресс надо
    показывать. Замок спрашиваем только когда heartbeat уже затих: проверка
    «процесс жив» не должна влиять на показ живого прогресса.
    """
    if not isinstance(state, dict) or state.get("status") != "scanning":
        return False
    try:
        heartbeat = float(state.get("last_heartbeat_at") or 0)
    except (TypeError, ValueError):
        heartbeat = 0.0
    if not heartbeat:
        return True
    if time.time() - heartbeat <= timeout:
        return False
    lock = _lock_path()
    return not (lock.exists() and _lock_owner_is_alive(lock))


def _clean_row(fragment: str) -> tuple[str, int] | None:
    text = html.unescape(_TAGS_RE.sub("", fragment)).replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    text = _LEVEL_RE.sub("", text).strip()
    if not text or _GENERIC_RE.match(text):
        return None
    quantity = 1
    match = _QTY_RE.match(text)
    if match:
        text, quantity = match.group(1).strip(), int(match.group(2))
    return text, quantity


def fetch_tradeable_types(*, on_wait=None) -> dict[str, str]:
    """Список допустимых ``type`` из trade API.

    Не каждый текст награды является торговым предметом. Проверка по этому
    справочнику предотвращает invalid-query (HTTP 400) и лишние запросы.
    """
    try:
        with netenv.client(headers={"User-Agent": "AldurAppraiserRU/0.2"},
                           follow_redirects=True,
                           timeout=httpx.Timeout(8.0, connect=4.0)) as client:
            # Сервер может отвечать 429 и просить подождать: ждём его подсказку,
            # но не бесконечно. Без этого ограничения постоянный 429 (например,
            # временная блокировка адреса) крутил обновитель вечно: лок оставался
            # занят, а новые обновления молча пропускались.
            waited = 0.0
            while True:
                response = client.get(TRADE_ITEM_DATA_URL)
                if response.status_code != 429:
                    break
                retry = float(response.headers.get("retry-after") or 60)
                if waited + retry > MAX_RATE_LIMIT_WAIT:
                    raise ScanRequestError(
                        f"справочник типов trade API: сервер просит подождать "
                        f"{math.ceil(waited + retry)} с — это больше предела "
                        f"{int(MAX_RATE_LIMIT_WAIT)} с, проход отменён")
                deadline = time.monotonic() + retry
                waited += retry
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    if on_wait:
                        on_wait(remaining)
                    time.sleep(min(1.0, remaining))
            if response.status_code != 200:
                retry = response.headers.get("retry-after")
                suffix = f"; Retry-After {retry} с" if retry else ""
                raise ScanRequestError(
                    f"справочник типов trade API: HTTP {response.status_code}{suffix}")
            groups = response.json().get("result", [])
    except ScanRequestError:
        raise
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        raise ScanRequestError(f"справочник типов trade API: {type(exc).__name__}") from exc
    return {str(entry["type"]): str(group.get("label") or "")
            for group in groups if isinstance(group, dict)
            for entry in group.get("entries", []) if isinstance(entry, dict)
            and entry.get("type")}


def _rows(fragment: str) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for raw in _ROW_RE.findall(fragment):
        item = _clean_row(raw)
        if item and item not in seen:
            seen.add(item)
            found.append(item)
    return found


def split_item_level(name: str) -> tuple[str, int | None]:
    """Разделяет имя камня в каталоге и его уровень для Trade API.

    В каталоге это одна строка: ``Rain of Blades (Level 20)``. В трейде
    название и уровень — два разных поля; передача суффикса в названии даёт
    пустой результат и делала «прогрев» камней фиктивным.
    """
    match = _ITEM_LEVEL_RE.search(name)
    if not match:
        return name, None
    return name[:match.start()].strip(), int(match.group(1))


def ru_display_name(reverse_names: dict[str, str], en_name: str) -> str | None:
    """Русское имя награды для каталога.

    У камней имя в каталоге идёт с уровнем — ``Rain of Blades (Level 20)``.
    Такого ключа в словаре нет, поэтому русское имя терялось и оставалось
    пустым у 26 записей. Сначала ищем точное имя, затем — имя без уровня.
    """
    exact = reverse_names.get(en_name)
    if exact:
        return exact
    base_name, level = split_item_level(en_name)
    if level is None:
        return None
    return reverse_names.get(base_name)


def _pricing_route(en_name: str, *, gem_names: set[str],
                   tradeable_types: dict[str, str], currency_ids: dict[str, str]
                   ) -> tuple[str, str, int | None]:
    """Определяет тип поиска; раздел PoE2DB не всегда равен типу предмета."""
    base_name, level = split_item_level(en_name)
    trade_type = tradeable_types.get(en_name) or tradeable_types.get(base_name)
    if (trade_type == "Currency" and level is not None
            and en_name not in currency_ids and base_name not in currency_ids):
        # Не отправляем несуществующий ID вида "...-level-10" в Currency
        # Exchange: это уже проверено на 400 (Invalid query). Но если базовый
        # предмет в обменнике есть (Greater Exalted Orb), уровень — артефакт
        # вкладки каталога, и цену базового предмета брать можно.
        return "levelled-currency", base_name, level
    if trade_type == "Currency" or en_name in currency_ids:
        return "exchange", base_name, level
    if en_name in bundled_exchange_families():
        # «Uncut Skill Gem» без уровня: сам камень торгуется в обменнике, но
        # ID у него уровневый. Цену берём диапазоном по уровням семейства.
        return "exchange-range", en_name, None
    if en_name in gem_names or trade_type == "Gems":
        return "gem", base_name, level
    if trade_type:
        return "trade", base_name, level
    return "unsupported", base_name, level


def fetch_catalog(client: httpx.Client) -> tuple[list[tuple[str, int]], set[str]]:
    """Возвращает фиксированные награды и имена наград-камней."""
    response = client.get(CATALOG_URL, timeout=45)
    response.raise_for_status()
    page = response.text
    # Первая вкладка — 322 комбинации. Следующая вкладка уже начинается после
    # первого tab-pane. Берём всё до неё, чтобы не дублировать категории.
    combinations = page.split('id="Alloys"', 1)[0]
    # В каталог идут только продаваемые награды: квестовые предметы и описания
    # («Rare Unique Item») отсеиваются, иначе скан ищет цену тому, чего на
    # рынке не существует.
    all_rewards = [item for item in _rows(combinations) if item[0] not in NOT_FOR_SALE]

    gems_match = re.search(r'id="Gems".*?(?=<div id="[A-Za-z]+" class="tab-pane|$)', page, re.S)
    gem_names = {name for name, _qty in _rows(gems_match.group(0))} if gems_match else set()
    return all_rewards, gem_names


def build_catalog(*, warm_gems: bool, started_at: float | None = None,
                  watchdog_stop: threading.Event | None = None) -> dict:
    """Обновляет рынок, каталог и сохранённые цены. Возвращает сводку."""
    if watchdog_stop is not None:
        # Страж жизни прохода. Он был в коде, но не запускался: зависший
        # запрос оставлял статус «сканирование» и замок навсегда.
        _set_progress(1, "подготовка сканирования", total=0)
        _start_watchdog(watchdog_stop)
    with netenv.client(headers={"User-Agent": "AldurAppraiserRU/0.2"},
                       follow_redirects=True) as client:
        rewards, gem_names = fetch_catalog(client)
    _set_progress(5, "получение рыночных данных", total=len(rewards))

    # Настройка лиги общая с оверлеем. Пусто = «Авто», то есть определить
    # текущую активную лигу только для этого запуска.
    selected_league = RuSettings().load().league.strip()
    book = PriceBook(league=selected_league)
    state = _read_refresh_state()
    state["league_selection"] = selected_league or "auto"
    _save_refresh_state(state)
    # Полный аудит может спокойно ждать окно лимитера, в отличие от обработки
    # нажатия Shift+D, где задержка недопустима.
    book._background_refresh = True

    def report_api_wait(seconds: float) -> None:
        state = _read_refresh_state()
        _set_progress(int(state.get("progress_percent", 0)),
                      f"ожидание лимита API ({math.ceil(seconds)} с)",
                      completed=int(state.get("completed", 0)),
                      total=int(state.get("total", 0)))

    book._progress_callback = report_api_wait
    book.use_cache()
    book.load(force=True, detect_league=not bool(selected_league))
    base_league = book.league
    state = _read_refresh_state()
    state["league"] = base_league
    _save_refresh_state(state)
    _set_progress(18, "проверка справочника trade API", total=len(rewards))
    tradeable_types = fetch_tradeable_types(on_wait=report_api_wait)
    _set_progress(20, "проверка фиксированных наград", total=len(rewards))
    # Каждый trade-запрос имеет собственный сетевой таймаут и записывает HTTP
    # ошибку сразу. Не используем отдельный watchdog по wall-clock: системное
    # время может прыгнуть и ошибочно завершить корректный проход до запроса.
    reverse_names = {en: ru for ru, en in ru_to_en().items()}
    entries: list[dict] = []
    gem_requests = 0
    item_trade_requests = 0
    currency_exchange_requests = 0
    request_errors: list[str] = []
    for index, (en_name, quantity) in enumerate(rewards, start=1):
        _set_progress(20 + int((index - 1) * 79 / len(rewards)),
                      "проверка награды", completed=index - 1,
                      total=len(rewards), current_reward=en_name)
        # Рыночный снимок — только запасной вариант. Для обычного трейда точной
        # ценой служит самый дешёвый лот мгновенного выкупа после сопоставления
        # exalted/chaos/divine в одной единице (exalted).
        market_unit = book.price_item(en_name, live=False)
        unit = market_unit
        price_kind = "market"
        price_reason = None
        estimate = book.random_estimate(en_name)
        if unit is None and estimate is not None:
            low, high, detail = estimate
            unit = (low + high) / 2.0
            price_kind = "estimate"
        # Наличие агрегированной цены не означает, что она выгоднее живого
        # лота. Полный суточный проход проверяет обычные предметы через trade,
        # валютные награды — через официальный Currency Exchange.
        if warm_gems:
            route, base_name, item_level = _pricing_route(
                en_name, gem_names=gem_names, tradeable_types=tradeable_types,
                currency_ids=book._ids)
            # Вкладка Gems на PoE2DB содержит и валютные награды (например,
            # Gemcutter's Prism и Uncut Gems); маршрут берём из trade API.
            if route == "exchange":
                _set_progress(20 + int((index - 1) * 79 / len(rewards)),
                              "поиск заявок Currency Exchange", completed=index - 1,
                              total=len(rewards), current_reward=en_name)
                # Уровневые записи без отдельного ID иногда являются вариантом
                # базового Currency ID; иначе проверяем торговый поиск с уровнем.
                exchange_name = en_name if en_name in book._ids else base_name
                # Правило цены: официальный снимок обменника → poe.ninja →
                # poe2scout. Живая книга заявок обменника (POST /exchange) для
                # ОБЫЧНОЙ цены не спрашивается: её минимум — это мусорные заявки
                # («1 divine за сферу»), а сами запросы дают 403/429 и лишний
                # риск бана. Она идёт только последней инстанцией — когда ни один
                # из трёх источников цены не знает, и тогда её ответ записывается
                # в каталог как доказательство вместо отписки «цены нет».
                trade_unit = book.exchange_price(exchange_name)
                if trade_unit is None:
                    trade_unit = book.exchange_price(base_name)
                official_note = ""
                if trade_unit is None:
                    # Таблицы цену не знают — спрашиваем официальные источники.
                    # «Цены нет» без официального ответа было отпиской: у части
                    # уровневых валют лотов нет в таблицах, но обменник отвечает
                    # по своему тегу, и только после его ответа видно, что рынка
                    # действительно нет.
                    _set_progress(20 + int((index - 1) * 79 / len(rewards)),
                                  "официальная проверка награды", completed=index - 1,
                                  total=len(rewards), current_reward=en_name)
                    trade_unit, official_note = book.official_price_or_proof(en_name)
                if trade_unit is not None:
                    unit = trade_unit
                    price_kind = "trade-exchange"
                elif estimate is not None:
                    unit = (estimate[0] + estimate[1]) / 2.0
                    price_kind = "estimate"
                    price_reason = "нет цены ни в обменнике, ни у агрегаторов; оценка"
                else:
                    unit = None
                    price_kind = "unpriced"
                    price_reason = "нет цены ни в обменнике, ни у агрегаторов"
                    if official_note:
                        price_reason += f" (проверено официально: {official_note})"
            elif route == "gem":
                _set_progress(20 + int((index - 1) * 79 / len(rewards)),
                              "проверка цен камней", completed=index - 1,
                              total=len(rewards))
                unit = book.price_gem(base_name, item_level, force=True)
                gem_requests += 1
                if unit is not None:
                    price_kind = "trade-gem"
                elif book._last_trade_error:
                    price_reason = f"trade-error: {book._last_trade_error}"
                    unit = None
                    price_kind = "unpriced"
                elif estimate is not None:
                    unit = (estimate[0] + estimate[1]) / 2.0
                    price_kind = "estimate"
                    price_reason = "no-active-fixed-buyout; estimate-fallback"
                else:
                    unit = None
                    price_kind = "unpriced"
                    price_reason = "no-active-fixed-buyout"
            elif route == "trade":
                _set_progress(20 + int((index - 1) * 79 / len(rewards)),
                              "поиск фиксированных лотов", completed=index - 1,
                              total=len(rewards), current_reward=en_name)
                trade_unit = book.price_item_trade(base_name, force=True)
                item_trade_requests += 1
                if trade_unit is not None:
                    unit = trade_unit
                    price_kind = "trade-item"
                elif estimate is not None:
                    unit = (estimate[0] + estimate[1]) / 2.0
                    price_kind = "estimate"
                    price_reason = "no-active-fixed-buyout; estimate-fallback"
                else:
                    # Для обычного предмета агрегатор не является заменой
                    # реального fixed-buyout лота. Оставлять старую среднюю
                    # цифру здесь означало бы отметить скан успешным с
                    # неактуальной ценой.
                    unit = None
                    price_kind = "unpriced"
                    price_reason = (f"trade-error: {book._last_trade_error}"
                                    if book._last_trade_error
                                    else "no-active-fixed-buyout")
            elif route == "exchange-range":
                # Камень без огранки: уровень случаен, поэтому точной цифры нет.
                # В каталог идёт диапазон уровней обменника, а не одно число.
                _set_progress(20 + int((index - 1) * 79 / len(rewards)),
                              "проверка уровней камня в обменнике", completed=index - 1,
                              total=len(rewards), current_reward=en_name)
                if estimate is not None:
                    low, high, detail = estimate
                    unit = (low + high) / 2.0
                    price_kind = "estimate"
                    price_reason = f"{detail}: диапазон обменника"
                else:
                    unit = None
                    price_kind = "unpriced"
                    price_reason = "no-exchange-levels-for-random-gem"
            elif route == "levelled-currency":
                unit = None
                price_kind = "unpriced"
                price_reason = "no-exact-currency-exchange-id-for-levelled-item"
            elif unit is None and estimate is None:
                # Отсутствие точного типа в справочнике не доказывает, что
                # награду нельзя продать; фиксируем её как непроверенную.
                price_kind = "unpriced"
                price_reason = "no-matching-trade-api-type"
            else:
                # Агрегаторная цена без соответствующего trade API типа не
                # является доказанной ценой мгновенного выкупа.
                unit = None
                if estimate is not None:
                    unit = (estimate[0] + estimate[1]) / 2.0
                    price_kind = "estimate"
                    price_reason = "no-matching-trade-api-type; estimate-fallback"
                else:
                    price_kind = "unpriced"
                    price_reason = "no-matching-trade-api-type"
            if unit is None and book._last_trade_error:
                # Сбой запроса — это не «предмета нет». Помечаем строку и идём
                # дальше: раньше здесь падал весь проход, и один ответ
                # Cloudflare (403) выбрасывал уже собранные цены всех наград.
                request_errors.append(f"{en_name}: {book._last_trade_error}")
                price_reason = f"request-error: {book._last_trade_error}"
                price_kind = "unpriced"
                unit = None
        entries.append({
            "en_name": en_name,
            "ru_name": ru_display_name(reverse_names, en_name),
            "quantity": quantity,
            "price_ex": None if unit is None else round(unit * quantity, 4),
            "source": (price_kind if unit is not None or price_kind == "not-tradeable"
                       else "unpriced"),
            "price_reason": price_reason,
            "estimate": (None if estimate is None else {
                "low_ex": round(estimate[0] * quantity, 4),
                "high_ex": round(estimate[1] * quantity, 4),
                "detail": estimate[2],
            }),
        })
        phase = "проверка цен камней" if warm_gems and en_name in gem_names else "проверка наград"
        _set_progress(20 + int(index * 79 / len(rewards)), phase,
                      completed=index, total=len(rewards), current_reward=en_name)

    payload = {
        "source": CATALOG_URL,
        "league": book.league,
        "updated_at": time.time(),
        "fixed_rewards": len(entries),
        "base_rewards": len(entries),
        "rewards_total": len(rewards),
        "gem_rewards": len(gem_names),
        "warm_gems": warm_gems,
        "entries": entries,
    }
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    _write_text_atomically(catalog_path(), serialized)
    # Снимок лиги: если завтра искать по другой лиге, прошлый результат
    # останется на диске, а не будет затёрт.
    archive = catalog_archive_path(book.league)
    if archive != catalog_path():
        _write_text_atomically(archive, serialized)
    # Быстрый рыночный проход не должен помечаться как суточный прогрев камней,
    # иначе следующие сутки останутся со старыми ценами камней.
    previous = _read_refresh_state()
    completed_at = time.time()
    priced_rewards = sum(entry["price_ex"] is not None for entry in entries)
    # «Цены нет» бывает двух видов: источник честно не знает предмета (это
    # нормальный итог, у камней и случайных наград так и должно быть) и сбой
    # запроса (это уже ошибка прохода). Успехом считается только первый вид —
    # иначе состояние навсегда оставалось «failed» из-за наград без рынка.
    unpriced = [entry for entry in entries if entry["price_ex"] is None]
    unexplained = [entry for entry in unpriced
                   if not str(entry.get("price_reason") or "").strip()
                   and entry.get("source") != "estimate"]
    all_prices_verified = (len(entries) == len(rewards)
                           and not request_errors and not unexplained)
    full_scan_success = warm_gems and all_prices_verified
    previous.update({
        "last_market_success_at": completed_at,
        "last_started_at": started_at if started_at is not None else completed_at,
        "source": CATALOG_URL,
        "fixed_rewards": len(entries),
        "priced_rewards": priced_rewards,
        # «100%» означает не обход строк, а полученную проверяемую оценку для
        # каждой награды. Нельзя сообщать об успехе с пустыми ценами.
        "status": "ready" if (not warm_gems or full_scan_success) else "failed",
        "progress_percent": 100,
        "phase": ("сканирование завершено" if (not warm_gems or full_scan_success)
                  else "сканирование неполное: есть награды без цены"),
        "completed": len(entries),
        "total": len(entries),
    })
    if warm_gems:
        previous["price_scan_schema"] = PRICE_SCAN_SCHEMA
        previous["full_scan_verified"] = full_scan_success
        previous["warm_gems"] = True
        previous["unpriced_rewards"] = len(unpriced)
        if request_errors:
            previous["error"] = (f"Сбои запросов: {len(request_errors)} "
                                 f"(например, {request_errors[0][:120]})")
        elif unexplained:
            previous["error"] = f"Без цены и без причины: {len(unexplained)}"
        else:
            previous.pop("error", None)
        if full_scan_success:
            previous["last_success_at"] = completed_at
        elif not request_errors and not unexplained:
            # Все награды обработаны: у части просто нет рынка ни в одном из
            # трёх источников. Это не сбой, повторять проход незачем.
            previous["last_success_at"] = completed_at
    _save_refresh_state(previous)
    return {"fixed_rewards": len(entries), "gem_rewards": len(gem_names),
            "priced": priced_rewards, "unpriced": len(unpriced),
            "request_errors": len(request_errors),
            "gem_requests": gem_requests, "item_trade_requests": item_trade_requests,
            "currency_exchange_requests": currency_exchange_requests,
            "path": str(catalog_path())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Обновить каталог рунных комбинаций и цены")
    parser.add_argument("--warm-gems", action="store_true",
                        help="заранее запросить цены фиксированных камней с трейда")
    parser.add_argument("--market-only", action="store_true",
                        help="обновить весь рынок без запросов цен камней")
    args = parser.parse_args(argv)
    # Сканер запускается оверлеем без консоли: у такого процесса sys.stdout
    # равен None, и print просто ничего не печатает. Снаружи это выглядело как
    # «сканер молча ничего не сделал» — по журналу нельзя было понять, дошёл он
    # до сети или умер на первом шаге. Уводим вывод в тот же overlay.log.
    if sys.stdout is None or sys.stderr is None:
        log = None
        try:
            log = open(config_dir() / "overlay.log", "a", encoding="utf-8", buffering=1)
        except OSError:
            try:
                log = open(os.devnull, "w", encoding="utf-8")
            except OSError:
                log = None
        if log is not None:
            sys.stdout = sys.stdout or log
            sys.stderr = sys.stderr or log
    # Прокси в проекте не используется: чужая переменная окружения не должна
    # ломать загрузку цен (однажды NO_PROXY со значением [::1] давал
    # httpx.InvalidURL: Invalid port на всех запросах сразу).
    removed = netenv.drop_proxy_env()
    if removed:
        print(f"убрал из окружения переменные прокси: {', '.join(removed)}", flush=True)
    if not _acquire_lock():
        print(json.dumps({"skipped": "already running"}, ensure_ascii=False))
        return 0
    watchdog_stop = threading.Event()
    try:
        _SCAN_FAILED.clear()
        started_at = time.time()
        # Записываем попытку сразу: даже если сеть недоступна, по состоянию
        # понятно, когда программа последний раз пыталась проверить базу.
        previous = _read_refresh_state()
        previous.pop("error", None)
        previous.pop("current_reward", None)
        previous.update({
            "last_started_at": started_at,
            "status": "scanning",
            "progress_percent": 0,
            "phase": "подготовка сканирования",
            "completed": 0,
            "total": 0,
        })
        _save_refresh_state(previous)
        result = build_catalog(warm_gems=args.warm_gems and not args.market_only,
                               started_at=started_at, watchdog_stop=watchdog_stop)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        failed = _read_refresh_state()
        failed.update({"status": "failed", "phase": "ошибка сканирования",
                       "error": f"{type(exc).__name__}: {str(exc)[:180]}",
                       "last_heartbeat_at": time.time()})
        _save_refresh_state(failed)
        raise
    finally:
        watchdog_stop.set()
        _release_lock()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

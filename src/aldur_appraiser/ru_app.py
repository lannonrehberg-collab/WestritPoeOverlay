"""Русский оверлей цен для Path of Exile 2.

Что делает: по горячей клавише снимает экран, находит панель наград, читает
названия русским OCR, берёт цены (обменная таблица + трейд для камней) и рисует
прозрачное окно поверх игры: у каждой награды — её цена, лучший вариант отмечен.

Запуск:
    py -m aldur_appraiser.ru_app [--hotkey F8] [--monitor 1]

Проверка без игры (рисует результат в файл):
    py -m aldur_appraiser.ru_app --render <скриншот> --out <картинка.png>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np

from aldur_appraiser.localize import BONUS_RE, QTY_SUFFIX_RE, Reward, parse_lines
from aldur_appraiser.netenv import drop_proxy_env
from aldur_appraiser.pricing.ru_prices import PriceBook, bundled_exchange_families
from aldur_appraiser.resources import resource_path
from aldur_appraiser.ru_cli import ocr_rewards
from aldur_appraiser.ru_settings import RuSettings, config_dir
from aldur_appraiser.runeshape_catalog import (
    market_refresh_is_due, refresh_is_due, scan_state_is_stale,
)
from aldur_appraiser.game_window import (
    find_game_window, foreground_is_game, foreground_process,
)
from aldur_appraiser.leagues import (
    AUTO_LEAGUE_LABEL, available_leagues, fetch_leagues, order_leagues,
)
from aldur_appraiser.vision import read_image
from aldur_appraiser.vision.detect import PanelBox, PanelDetector, _clamp_roi
from aldur_appraiser.vision.ocr import OcrLine, get_engine
from aldur_appraiser.win_hotkey import start_global_hotkey
from aldur_appraiser.winproc import image_name, process_alive

RU_TEMPLATE = "assets/templates/header_runeshape_ru.png"
# Крестик закрытия панели: по нему за пару миллисекунд понятно, открыта панель
# или её уже закрыли — не нужно каждый раз искать заголовок по всему экрану.
CLOSE_TEMPLATE = "assets/templates/close_ru.png"
DEFAULT_HOTKEY = "<Shift>d"
# Насколько должна измениться область наград, чтобы считать список прокрученным.
# Сколько раз перечитывать кадр, если раскладка вышла неровной, и пауза между
# попытками: панель могла ещё доезжать после анимации открытия.
MAX_LAYOUT_ATTEMPTS = 4
RETRY_DELAY_MS = 150
SCROLL_THRESHOLD = 5.0
# Проверка UI локальная: ни сеть, ни API здесь не участвуют. 300 мс дают
# отзывчивую прокрутку и закрытие, а два тика защищают от краткого перекрытия
# крестика курсором.
UI_WATCH_INTERVAL_MS = 300
SCROLL_CHECK_EVERY_TICKS = 2

# Цвета интерфейса
C_BG = (0, 0, 0, 170)
C_BEST = (255, 205, 90, 255)
C_TEXT = (240, 240, 240, 255)
C_UNKNOWN = (170, 170, 170, 255)
C_BAR = (255, 205, 90, 255)

# Плашка цены: полупрозрачная тёмно-синяя заливка и светлая окантовка.
BADGE_FILL = (18, 28, 52, 210)          # обычная цена
BADGE_BORDER = (108, 160, 255, 235)     # обычная рамка
BADGE_FILL_BEST = (58, 44, 14, 220)     # лучший выбор
BADGE_BORDER_BEST = (255, 205, 90, 255)
BADGE_FILL_UNKNOWN = (20, 22, 28, 185)  # цена неизвестна
BADGE_BORDER_UNKNOWN = (150, 150, 150, 175)
BADGE_FILL_PENDING = (18, 28, 52, 175)  # цена ещё грузится
BADGE_BORDER_PENDING = (108, 160, 255, 140)

# Это не котировка трейда: "Груда веризия" не выставляется отдельным лотом, а
# количество ресурса в награде не раскрывается интерфейсом. Диапазон намеренно
# широкий и только помогает выбрать комбинацию, пока нет точных данных.
VERISIUM_PILE_ESTIMATE_LABEL = "≈ 10–21 ex · 200–400 Verisium"
UNCUT_SPIRIT_GEM_FALLBACK_LABEL = "≈ 30–4 800 ex"


class Prices:
    """Загрузка цен в фоне, чтобы окно не подвисало на старте."""

    def __init__(self, league: str = "") -> None:
        self.book = PriceBook(league=league)
        self.ready = threading.Event()
        self.error = ""
        self._cache_mtime_ns = 0
        self._price_errors: set[tuple[str, str]] = set()
        self.thread = threading.Thread(target=self._load, daemon=True)
        self.thread.start()

    def _note_price_error(self, name: str, exc: Exception) -> None:
        """Записывает в лог, почему цена не получилась: один раз на предмет.

        Раньше ошибка цены молча превращалась в «нет цены» — по логу нельзя
        было понять, это рынок такой или сломался запрос.
        """
        key = (name, type(exc).__name__)
        if key in self._price_errors:
            return
        self._price_errors.add(key)
        print(f"[{time.strftime('%H:%M:%S')}] [цены] {name}: "
              f"{type(exc).__name__}: {exc}", flush=True)

    def _remember_cache_mtime(self) -> None:
        try:
            self._cache_mtime_ns = self.book._cache_file.stat().st_mtime_ns
        except OSError:
            pass

    def _load(self) -> None:
        had_cache = False
        try:
            had_cache = self.book.use_cache()   # сразу работаем на сохранённых ценах
            self._remember_cache_mtime()
        except Exception:  # noqa: BLE001
            pass
        # Пока идёт первая загрузка рынка, плашки держатся в состоянии «…»,
        # а не «?»: иначе 13 секунд ожидания выглядят как «цен нет вообще».
        if had_cache and self.book.size:
            self.ready.set()             # показывать цены можно уже сейчас
        # Первый запуск (файла цен ещё нет): тянем рынок прямо здесь, в этом же
        # фоновом потоке. Раньше цены зависели только от отдельного процесса-
        # сканера, и если тот молча не стартовал, оверлей навсегда оставался с
        # «?» у каждой награды — именно это видели на чистой установке.
        if not had_cache or not self.book.size:
            try:
                print(f"[{time.strftime('%H:%M:%S')}] [цены] сохранённого кэша нет — "
                      f"качаю рынок", flush=True)
                started = time.time()
                self.book.load(force=True)
                print(f"[{time.strftime('%H:%M:%S')}] [цены] рынок загружен: "
                      f"{self.book.size} предметов за {time.time() - started:.0f} c",
                      flush=True)
            except Exception as exc:  # noqa: BLE001 — без сети живём на пустом рынке
                self._note_price_error("рынок", exc)
            self.ready.set()
        # Полный проход дорог для официального trade API. Запускаем его только
        # если полного успешного прохода нет или ему уже сутки; Shift+D всегда
        # работает из локального кэша и не создаёт сетевых запросов.
        # Полный проход (включая fixed-buyout лоты камней/предметов) идёт раз
        # в сутки. Рынок материалов обновляем отдельно и чаще: это один общий
        # снимок, без сотен запросов к trade API.
        refresh_args: list[str] | None = None
        if refresh_is_due():
            refresh_args = ["--warm-gems"]
        elif market_refresh_is_due():
            refresh_args = ["--market-only"]
        if refresh_args is not None:
            try:
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if getattr(sys, "frozen", False):
                    command = [sys.executable, "--refresh-catalog", *refresh_args]
                    root = Path(sys.executable).resolve().parent
                else:
                    command = [sys.executable, "-u", "-m",
                               "aldur_appraiser.runeshape_catalog", *refresh_args]
                    root = Path(__file__).resolve().parents[2]
                subprocess.Popen(command, cwd=str(root), creationflags=flags, close_fds=True)
                print(f"[{time.strftime('%H:%M:%S')}] [цены] запущен полный проход "
                      f"цен: {' '.join(refresh_args)}", flush=True)
            except Exception as exc:  # noqa: BLE001 — приложение должно жить и без сети
                self.error = f"{type(exc).__name__}: {exc}"
                # Молчаливый отказ запуска выглядел как «цены просто не пришли».
                print(f"[{time.strftime('%H:%M:%S')}] [цены] не смог запустить "
                      f"полный проход: {self.error}", flush=True)
        self._remember_cache_mtime()
        self.ready.set()

    def reload_cache_if_changed(self) -> bool:
        """Подхватывает ночное обновление с диска, не обращаясь к сети.

        Нажатие Shift+D никогда не вызывает этот метод: он запускается только
        редким таймером приложения и лишь когда ежедневная задача уже заменила
        файл кэша атомарной записью.
        """
        try:
            mtime_ns = self.book._cache_file.stat().st_mtime_ns
        except OSError:
            return False
        if mtime_ns <= self._cache_mtime_ns:
            return False
        try:
            with self.book._lock:
                loaded = self.book.use_cache(reload=True)
            if loaded:
                self._cache_mtime_ns = mtime_ns
            return loaded
        except Exception:  # noqa: BLE001 — оставляем рабочие цены в памяти
            return False

    def price(self, reward: Reward, *, live: bool = True) -> float | None:
        if reward.kind == "estimate":
            return None
        if not reward.en_name:
            return None
        try:
            if reward.kind == "gem":
                # Неогранённые камни торгуются «пачками» в игровом обменнике, и там
                # цена точнее, чем самый дешёвый лот трейд-сайта (по лотам такие
                # камни выходили в divine, хотя стоят десятки exalted).
                if self.book.has_bulk_price(reward.en_name):
                    bulk = self.book.price_item(reward.en_name, live=live)
                    if bulk:
                        return bulk
                return self.book.price_gem(reward.en_name, reward.gem_level)
            return self.book.price_item(reward.en_name, live=live)
        except Exception as exc:  # noqa: BLE001 — одна неудачная цена не должна ломать показ
            self._note_price_error(reward.en_name or "?", exc)
            return None

    def cached_gem_price(self, reward: Reward) -> float | None:
        """Цена камня только из локального снимка, без обращения к трейду."""
        if reward.kind != "gem" or not reward.en_name:
            return None
        try:
            return self.book.cached_gem_price(reward.en_name, reward.gem_level)
        except Exception:  # noqa: BLE001
            return None

    def estimate_label(self, reward: Reward) -> str:
        """Подпись для награды с заведомо неизвестным точным вариантом."""
        name = reward.en_name or ""
        if name in bundled_exchange_families():
            # Камни без огранки выдаются случайного уровня. Диапазон уровней
            # берём из обменника: округляем до десятков/сотен, чтобы не
            # создавать видимость точности у случайного уровня.
            span = self.book.uncut_gem_range(name)
            if span is None:
                return UNCUT_SPIRIT_GEM_FALLBACK_LABEL
            low, high = span
            low = round(low, -1) if low >= 10 else round(low)
            high = round(high, -2) if high >= 100 else round(high, -1)
            return f"≈ {low:,.0f}–{high:,.0f} ex".replace(",", " ")
        random_price = self.book.random_estimate(name)
        if random_price is not None:
            low, high, detail = random_price
            # Оценка всегда остаётся диапазоном: не изображаем точную цену там,
            # где итог определяется случайным количеством/предметом.
            low, high = round(low, 1), round(high, 1)
            return f"≈ {low:g}–{high:g} ex · {detail}"
        if reward.en_name == "Verisium Pile":
            return VERISIUM_PILE_ESTIMATE_LABEL
        return "≈ цена неизвестного варианта"


class Appraiser:
    """Кадр -> награды с ценами и координатами на экране."""

    def __init__(self, prices: Prices, *, language: str = "ru") -> None:
        self.prices = prices
        self.engine = get_engine("windows", language=language)
        # Второй проход чтения с большим увеличением: мелкий шрифт читается лучше.
        try:
            from aldur_appraiser.vision.ocr import WindowsOcrEngine

            self.engine_hi = WindowsOcrEngine(language=language, scale=4.0)
        except Exception:  # noqa: BLE001 — обойдёмся одним проходом
            self.engine_hi = None
        self.bonus: set[int] = set()          # строки-бонусы: их не выбираем как лучшие
        self.panel = None                     # найденная панель (для размера плашек)
        template = resource_path(RU_TEMPLATE)
        self.detector = (
            # Полноэкранный интерфейс на высоком разрешении даёт масштаб 1.65
            # относительно старого шаблона. Верхняя граница 1.6 из-за этого
            # исключала правильный вариант ещё до сопоставления.
            # На полноэкранном кадре с текущим масштабом интерфейса шаблон
            # совпадает на 0.789: старый глобальный порог 0.80 отбрасывал уже
            # найденную панель. Для русского шаблона 0.74 достаточно; ниже по
            # цепочке всё равно проверяются OCR-строки и ровная раскладка.
            PanelDetector(template_path=template, threshold=0.74,
                          scales=np.linspace(0.3, 2.4, 43))
            if template.exists() else None
        )

    def read_lines(self, image) -> list:
        """Чтение области наград: два увеличения, лучшее совпадение по словарю.

        Вынесено отдельным методом, чтобы это можно было подменить в тестах.
        """
        engines = [self.engine] + ([self.engine_hi] if self.engine_hi else [])
        return read_lines_multi(image, engines)

    def run(self, frame: np.ndarray, *, price_gems: bool = True,
            panel: PanelBox | None = None, detect: bool = True, live: bool = False
            ) -> tuple[list[tuple[Reward, float | None, tuple | None]], str,
                       np.ndarray | None]:
        """Возвращает (строки, статус, отпечаток области наград).

        price_gems=False — камни остаются без цены: их считает трейд-сайт по одному,
        и ждать все сразу нельзя (лимит запросов), поэтому камень догружается отдельно.
        detect=False — панель уже найдена вызывающим (не ищем повторно).
        live=False — первый показ по таблицам, живые цены обменника догружаются фоном.
        """
        signature = None
        if detect:
            panel = self.detector.find_panel(frame) if self.detector is not None else None

        if panel is not None:
            roi = self.detector.reward_image(frame, panel)
            origin = (panel.reward_roi().left, panel.reward_roi().top)
            ocr_lines = merge_quantity_lines(self.read_lines(roi))
            ocr_lines = drop_own_badges(ocr_lines)   # страховка от своих же плашек
            ocr_lines = dedupe_overlapping(ocr_lines)
            ocr_lines = drop_detached_tail(ocr_lines)
            status = f"панель найдена (совпадение {panel.confidence:.2f})"
            signature = roi_signature(self.detector, frame, panel)
        else:
            origin = (0, 0)
            ocr_lines = merge_quantity_lines(self.read_lines(frame))
            ocr_lines = drop_own_badges(ocr_lines)
            ocr_lines = dedupe_overlapping(ocr_lines)
            status = "панель не найдена — читаю весь кадр"

        # Разбираем каждую строку отдельно и СРАЗУ запоминаем её рамку: раньше
        # рамка искалась по точному тексту, и при малейшем отличии строка
        # оставалась без неё — плашка на этой награде пропадала.
        allow_unknown = panel is not None
        triples: list[tuple[Reward, tuple | None, str]] = []
        for line in ocr_lines:
            for parsed in parse_lines([line.text], allow_unknown=allow_unknown):
                triples.append((parsed, line.box, line.text))
        kept = drop_fragments([reward for reward, _box, _text in triples])
        keep_ids = {id(reward) for reward in kept}
        triples = [item for item in triples if id(item[0]) in keep_ids]
        pairs = [(reward, box) for reward, box, _text in triples]
        kept_texts = {text for _reward, _box, text in triples}
        # Диагностика: строка ВНУТРИ списка наград, из которой награда не
        # получилась. Раньше она молча исчезала с панели — без плашки и без
        # единой строчки в логе, и причину приходилось искать по дампу
        # (так пропало «Кольцо морозного пламени»: OCR прочитал «Урвень»).
        if panel is not None:
            tops = [box[1] for _reward, box in pairs if box]
            if tops:
                low, high = min(tops), max(tops)
                for line in ocr_lines:
                    if line.box is None or line.text in kept_texts:
                        continue
                    if low <= line.box[1] <= high:
                        print(f"[{time.strftime('%H:%M:%S')}] строка панели осталась "
                              f"без награды: {line.text!r}", flush=True)
        # Строка без рамки иначе остаётся без плашки: цену ей некуда поставить.
        pairs = fill_missing_boxes(pairs)
        rewards = self.stabilize([reward for reward, _box in pairs])
        # Разделитель «Бонусная награда» надёжнее любых догадок по промежуткам.
        divider = None
        for line in ocr_lines:
            if BONUS_RE.search(line.text or "") and line.box:
                divider = line.box[3] if divider is None else max(divider, line.box[3])

        rows: list[tuple[Reward, float | None, tuple | None, str | None]] = []
        for reward, box in pairs:
            note: str | None = None
            if reward.kind == "estimate":
                value = None
                note = self.prices.estimate_label(reward)
            elif reward.kind == "gem" and not price_gems:
                # Shift+D читает исключительно локальный кэш. Раньше этот
                # участок намеренно игнорировал уже сохранённые цены, рисовал
                # «…» и запускал новые запросы к trade API.
                unit = self.prices.cached_gem_price(reward)
                value = None if unit is None else unit * max(1, reward.qty)
            else:
                unit = self.prices.price(reward, live=live)
                # В панели рядом с названием стоит количество: «(2)», «(3)».
                # Показываем стоимость всего лота, а не одной штуки.
                value = None if unit is None else unit * max(1, reward.qty)
                # Цена уже нормализована и показана в подходящей валюте. Не
                # дублируем рядом исходную валюту лота: «8 chaos · 8 chaos»
                # выглядит как две разные цены, хотя это один и тот же лот.
            screen_box = None
            if box is not None:
                screen_box = (origin[0] + box[0], origin[1] + box[1],
                              origin[0] + box[2], origin[1] + box[3])
                # Рамка от OCR — не то же самое, что строка текста: у двойных
                # наград она выше и с отступами. Находим саму строку на картинке.
                band = measure_text_band(frame, screen_box)
                if band is not None:
                    screen_box = (screen_box[0], band[0], screen_box[2], band[1])
            rows.append((reward, value, screen_box, note))

        self.bonus = bonus_indices(rows, divider)
        self.panel = panel
        if panel is not None:
            dump_debug(roi, ocr_lines, rewards)   # снимок и блоки — для разбора проблем
        return rows, status, signature

    def stabilize(self, rewards: list[Reward]) -> list[Reward]:
        """Не даёт результату «дрожать» между нажатиями на одной и той же панели.

        Распознавание каждый раз читает шрифт чуть иначе, и от этого могли
        меняться названия и подписи. Если набор наград совпал с прошлым разом,
        берём прежнее прочтение — цены при этом всё равно пересчитываются.
        """
        previous = getattr(self, "_last_rewards", None)
        self._last_rewards = list(rewards)
        if not previous or len(previous) != len(rewards):
            return rewards
        for old, new in zip(previous, rewards):
            if old.en_name != new.en_name or old.qty != new.qty or old.kind != new.kind:
                return rewards
        return previous

    @staticmethod
    def gem_indices(rows) -> list[int]:
        """Камни без цены в локальном снимке.

        Их не запрашиваем от Shift+D: полный скан заполнит кэш по расписанию,
        а пока плашка честно покажет «?». 
        """
        return [i for i, (reward, value, _, _n) in enumerate(rows)
                if reward.kind == "gem" and reward.en_name and value is None]

    @staticmethod
    def bulk_indices(rows, book) -> list[int]:
        """Строки «пачечных» предметов: их живую цену тянем из игрового обменника.

        Первый показ идёт по таблице (мгновенно), а точная цена обменника
        подставляется фоном — иначе показ тормозил бы на запросах.
        """
        out: list[int] = []
        for index, (reward, _value, _box, _note) in enumerate(rows):
            if reward.kind == "gem" or not reward.en_name:
                continue
            if book.trades_in_exchange(reward.en_name):
                out.append(index)
        return out


def _monitors() -> list[dict]:
    """Список экранов для окна настроек (индекс 0 — все экраны сразу)."""
    try:
        import mss

        factory = getattr(mss, "MSS", None) or mss.mss
        with factory() as sct:
            return [dict(m) for m in sct.monitors]
    except Exception:  # noqa: BLE001 — без списка показываем хотя бы один вариант
        return [{"left": 0, "top": 0, "width": 0, "height": 0}]


_instance_mutex = None


def quit_flag_path() -> Path:
    """Файл-просьба «закройся»: им новая копия вежливо гасит прежнюю."""
    return config_dir() / "quit.flag"


def request_previous_exit(pid: int, timeout: float = 3.0) -> bool:
    """Просит живую копию закрыться и ждёт её ухода.

    Важно: принудительное завершение (taskkill /F) не даёт процессу убрать
    значок из трея — Windows оставляет «мёртвый» значок, который выглядит
    живым, но ни на что не реагирует. Поэтому сначала просим закрыться
    по-хорошему: копия сама прячет значок и выходит.
    """
    flag = quit_flag_path()
    try:
        flag.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_alive(pid):
            return True
        time.sleep(0.2)
    return False


def close_instance(pid: int, timeout: float = 1.5) -> bool:
    """Закрывает чужую копию оверлея: сначала по-хорошему, потом принудительно.

    Возвращает True, только если процесса действительно больше нет. Проверка
    обязательна: завершить копию с другими правами (например, запущенную от
    администратора, когда сама запущена обычно) нельзя — taskkill молча
    откажет, и раньше в логе появлялось «закрыл прежнюю копию», хотя она
    продолжала работать.
    """
    import subprocess

    if not process_alive(pid):
        return True
    request_previous_exit(pid, timeout=timeout)
    if not process_alive(pid):
        return True                      # ушла сама и убрала значок из трея
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True,
                       timeout=15,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:  # noqa: BLE001 — ниже всё равно проверим результат
        pass
    time.sleep(0.5)
    return not process_alive(pid)


def stop_previous_instance() -> int | None:
    """Завершает ранее запущенную копию оверлея.

    Одновременно должна работать ровно одна копия: две рисовали бы плашки друг
    поверх друга — получалась «пляска» размеров и цены поверх названий.
    Номер процесса храним в файле, а перед завершением проверяем, что это
    действительно python/pythonw (номер мог достаться другой программе).
    """
    path = config_dir() / "overlay.pid"
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if pid <= 0 or pid == os.getpid():
        return None
    if not image_name(pid).lower().endswith(("python.exe", "pythonw.exe")):
        return None          # номер переиспользован другой программой — не трогаем
    closed = close_instance(pid, timeout=3.0)
    quit_flag_path().unlink(missing_ok=True)
    if not closed:
        print(f"не удалось закрыть прежнюю копию (номер {pid}): она запущена "
              f"с другими правами — закрой её из трея пунктом «Выход»", flush=True)
        return None
    return pid


def remember_pid() -> None:
    """Запоминает номер текущего процесса (для следующего запуска)."""
    try:
        (config_dir() / "overlay.pid").write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass


def instances_dir() -> Path:
    """Папка с метками живых копий оверлея."""
    path = config_dir() / "instances"
    path.mkdir(parents=True, exist_ok=True)
    return path


def register_instance() -> None:
    """Оставляет метку «эта копия работает» со своим номером процесса."""
    try:
        (instances_dir() / f"{os.getpid()}.pid").write_text(str(os.getpid()),
                                                            encoding="utf-8")
    except OSError:
        pass


def forget_instance() -> None:
    """Снимает свою метку при выходе."""
    try:
        (instances_dir() / f"{os.getpid()}.pid").unlink(missing_ok=True)
    except OSError:
        pass


def registered_instances() -> list[int]:
    """Номера живых копий оверлея по их собственным меткам.

    Поиск по командной строке через WMI здесь не работает: у оверлея,
    запущенного с правами администратора, командная строка не видна снаружи
    (проверено — у работающей копии она пустая), поэтому прежняя «подчистка»
    лишних копий молча ничего не делала. Свои метки видит сам оверлей.
    """
    alive: list[int] = []
    for file in instances_dir().glob("*.pid"):
        try:
            pid = int(file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            file.unlink(missing_ok=True)
            continue
        if pid == os.getpid():
            continue
        if process_alive(pid):
            alive.append(pid)
        else:
            file.unlink(missing_ok=True)   # метка от уже закрытой копии
    return alive


def stop_other_instances() -> list[int]:
    """Гасит прочие копии оверлея — работать должна ровно одна.

    Сначала просим закрыться по-хорошему: тогда копия сама уберёт свой значок
    из трея. Принудительное завершение оставляет значок «мёртвым»: он виден,
    но не отвечает на нажатия.
    """
    stopped: list[int] = []
    for pid in registered_instances():
        if not image_name(pid).lower().endswith(("python.exe", "pythonw.exe")):
            continue          # номер переиспользован другой программой — не трогаем
        if close_instance(pid, timeout=1.5):
            stopped.append(pid)
            (instances_dir() / f"{pid}.pid").unlink(missing_ok=True)
        else:
            print(f"копия оверлея (номер {pid}) не закрылась: у неё другие права",
                  flush=True)
    quit_flag_path().unlink(missing_ok=True)
    return stopped


def already_running() -> bool:
    """Защита от второго запуска: два экземпляра рисовали бы плашки друг поверх друга.

    Важно брать WinDLL с use_last_error=True и читать ctypes.get_last_error():
    у обычного ctypes.windll код ошибки не сохраняется, и проверка всегда
    возвращала «не запущен» — из-за этого второй экземпляр спокойно запускался.
    """
    global _instance_mutex
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                          wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        _instance_mutex = kernel32.CreateMutexW(None, False, "PoE2RUOverlay_SingleInstance")
        return ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS
    except Exception:  # noqa: BLE001 — не Windows или нет прав: просто продолжаем
        return False


def capture(monitor: int = 0) -> tuple[np.ndarray, dict]:
    """Снимок монитора (0 — все экраны сразу). -> (кадр BGR, геометрия)."""
    import mss

    factory = getattr(mss, "MSS", None) or mss.mss  # mss.mss устарел в новых версиях
    with factory() as sct:
        mon = sct.monitors[monitor]
        shot = np.array(sct.grab(mon))
    return shot[:, :, :3].copy(), mon


# --- отрисовка ---------------------------------------------------------------

ICON_BASE = "https://web.poecdn.com"


def icon_dir() -> Path:
    path = config_dir() / "icons"
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_icons(urls: dict[str, str]) -> None:
    """Скачивает иконки валют в локальный кэш (один раз на валюту)."""
    from aldur_appraiser import netenv

    folder = icon_dir()
    for ident, path in urls.items():
        target = folder / f"{ident}.png"
        if target.exists() and target.stat().st_size > 0:
            continue
        try:
            response = netenv.get(ICON_BASE + path, timeout=20, follow_redirects=True,
                                  headers={"User-Agent": "AldurAppraiserRU/0.2"})
            if response.status_code == 200 and "image" in response.headers.get(
                    "content-type", ""):
                target.write_bytes(response.content)
        except Exception:  # noqa: BLE001 — без иконки цена всё равно покажется
            continue


def load_icon_pixmaps() -> dict:
    """Читает сохранённые иконки. Требует созданного QApplication."""
    from PySide6.QtGui import QPixmap

    out: dict = {}
    for file in icon_dir().glob("*.png"):
        pixmap = QPixmap(str(file))
        if not pixmap.isNull():
            out[file.stem] = pixmap
    return out


def format_number(value: float, rates: dict[str, float]) -> tuple[str, str]:
    """Число и валюта для показа, от дешёвой валюты к дорогой.

    Все внутренние оценки хранятся в exalted.  Валюта меняется только когда
    сумма достигла стоимости *одной* следующей валюты по свежему курсу:
    exalted → chaos → divine.
    """
    chaos = rates.get("chaos") or 0
    divine = rates.get("divine") or 0
    if divine and value >= divine:
        return f"{value / divine:.2f}", "divine"
    if chaos and value >= chaos:
        return f"{value / chaos:.2f}", "chaos"
    if value >= 1000:
        return f"{value:,.0f}".replace(",", " "), "exalted"
    if value >= 10:
        return f"{value:.0f}", "exalted"
    return f"{value:.1f}", "exalted"


def format_value(value: float | None, rates: dict[str, float]) -> str:
    """Текстовая цена (для логов и консоли)."""
    if value is None:
        return "?"
    number, currency = format_number(value, rates)
    suffix = {"exalted": "ex", "chaos": "chaos", "divine": "div"}[currency]
    return f"{number} {suffix}"


def roi_signature(detector, frame, panel) -> np.ndarray | None:
    """Сжатый «отпечаток» области наград — по нему видно, что список прокрутили."""
    try:
        import cv2

        roi = detector.reward_image(frame, panel)
        if roi is None or roi.size == 0:
            return None
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        return cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA).astype(np.int16)
    except Exception:  # noqa: BLE001 — отпечаток не критичен
        return None


QTY_ONLY_RE = re.compile(r"^[\(\[]?\s*(\d{1,3})\s*[\)\]]?$|^(\d{1,3})\s*[xX×]$")


# Текст, который бывает только у наших плашек («661 ex», «1 chaos · 55.9 exalted»,
# «(3 шт)»). Нужен как страховка: если плашки всё же попали в снимок, строки
# отбрасываются и не считаются наградами.
#
# «шт» узнаётся только в конце строки или в скобках: у плашки вся строка — это
# «(3 шт)», а у живой награды «5 шт. случайной валюты» слово стоит в середине.
# Раньше проверка была «где угодно» и вместе с плашкой выбрасывала саму награду
# (снимки 1600x900_019 и 1600x900_020: строка пропадала с панели целиком).
OWN_BADGE_RE = re.compile(
    r"(\bex\b|\bdiv\b|·|…|\d+\s*шт[а-яё]*\.?\s*\)?\s*$)", re.IGNORECASE)


def drop_own_badges(lines: list) -> list:
    """Убирает из распознанного строки, похожие на наши плашки с ценами."""
    return [line for line in lines if not OWN_BADGE_RE.search(line.text or "")]


def merge_quantity_lines(lines: list) -> list:
    """Приклеивает отдельно распознанное количество к строке награды.

    Windows OCR иногда читает «(3)» не в строке названия, а отдельным блоком
    справа. Тогда количество терялось, а мусорная строка попадала в список.

    Склеиваем осторожно: только если у строки ещё нет количества, блок стоит
    рядом (а не в другом углу панели) и на той же высоте. Иначе к награде
    приклеивалось постороннее число и цена считалась за «19 штук».
    """
    out: list = []
    for line in sorted(lines, key=lambda ln: ln.top):
        match = QTY_ONLY_RE.match((line.text or "").strip())
        if match and out and line.box is not None:
            previous = out[-1]
            if previous.box is not None and not QTY_SUFFIX_RE.search(previous.text):
                row_height = max(8.0, previous.box[3] - previous.box[1])
                same_row = abs(line.box[1] - previous.box[1]) < row_height * 0.9
                gap = line.box[0] - previous.box[2]
                near = -row_height <= gap <= row_height * 2.5
                if same_row and near:
                    digits = match.group(1) or match.group(2) or "1"
                    merged = OcrLine(
                        text=f"{previous.text} ({digits})",
                        conf=previous.conf,
                        top=previous.top,
                        box=(previous.box[0], previous.box[1], line.box[2],
                             max(previous.box[3], line.box[3])),
                    )
                    out[-1] = merged
                    continue
        out.append(line)
    return out


def capture_settled(monitor: int, detector=None, *, attempts: int = 6,
                    delay: float = 0.12, tolerance: float = 2.0
                    ) -> tuple[np.ndarray, dict, PanelBox | None]:
    """Снимок экрана после того, как панель наград перестала двигаться.

    Панель открывается с анимацией: если снять её в этот момент, текст в кадре
    крупнее обычного, а рамки строк смещены — плашки выходят разного размера и
    встают неровно. Поэтому делаем несколько быстрых снимков и ждём, пока
    область панели перестанет меняться (до ~0.6 секунды).
    """
    frame, mon = capture(monitor)
    panel = detector.find_panel(frame) if detector is not None else None
    if panel is None:
        return frame, mon, None   # панели нет — ждать нечего, не тратим время

    for _ in range(max(0, attempts - 1)):
        time.sleep(delay)
        again, _geom = capture(monitor)
        roi = _clamp_roi(panel.reward_roi(), frame.shape)
        before = frame[roi.top:roi.top + roi.height, roi.left:roi.left + roi.width]
        after = again[roi.top:roi.top + roi.height, roi.left:roi.left + roi.width]
        if before.shape != after.shape or before.size == 0:
            settled = False
        else:
            diff = np.mean(np.abs(before.astype(np.int16) - after.astype(np.int16)))
            settled = float(diff) < tolerance
        frame = again
        if settled:
            break
    return frame, mon, panel


def drop_fragments(rewards: list) -> list:
    """Убирает обрывки строк: «Сфера» от «Сфера возвышения (2)».

    Распознавание иногда режет длинное название на две строки. Вторая попадала
    в список наград, и на её месте рисовалась плашка «?» прямо поверх названия.
    """
    out = []
    for reward in rewards:
        if reward.ru_name is not None:
            out.append(reward)
            continue
        text = (reward.en_name or "").strip().lower()
        if len(text) < 3:
            continue
        fragment = False
        for other in rewards:
            if other is reward or other.ru_name is None:
                continue
            if other.raw and text in other.raw.lower() and len(other.raw) > len(text) + 2:
                fragment = True
                break
        if not fragment:
            out.append(reward)
    return out


def fill_missing_boxes(pairs: list) -> list:
    """Достраивает рамку строкам, для которых распознавание её не вернуло.

    Плашка рисуется только по рамке строки. Если движок прочитал текст, но не
    отдал блок, строка оставалась вообще без цены — так пропала
    «Уровень умения 20: Кольцо морозного пламени» на панели. Высоту и шаг строк
    берём у соседей, а место достраиваем от ближайшей строки с рамкой.
    """
    boxes = [box for _reward, box in pairs if box]
    if not boxes:
        return pairs
    heights = sorted(box[3] - box[1] for box in boxes)
    height = heights[len(heights) // 2]
    steps = sorted(b[1] - a[1] for a, b in zip(boxes, boxes[1:]) if b[1] > a[1])
    # Именно МИНИМАЛЬНЫЙ шаг: пропущенная строка как раз и раздувает часть
    # промежутков, а плотный шаг между строками с рамками остаётся верным.
    step = steps[0] if steps else height * 1.6
    gap = max(0.0, step - height)
    out: list = []
    for index, (reward, box) in enumerate(pairs):
        if box is None:
            previous = next((b for _r, b in reversed(out) if b), None)
            following = next((b for _r, b in pairs[index + 1:] if b), None)
            reference = previous or following
            if reference is None:
                out.append((reward, box))
                continue
            top = (previous[3] + gap if previous is not None
                   else following[1] - gap - height)
            box = (reference[0], top, reference[2], top + height)
        out.append((reward, box))
    return out


def read_lines_multi(image, engines: list) -> list:
    """Читает область несколькими движками (разное увеличение) и сливает результаты.

    Мелкий стилизованный шрифт читается по-разному при разном увеличении: строка
    «Неогранённый камень умения (уровень 19)» при x2 распознавалась мусором
    («ЬЊ•ень утеения»), а при x4 — почти правильно. Поэтому читаем дважды и для
    каждой строки берём вариант, который лучше совпал со словарём названий.
    """
    from aldur_appraiser.localize import match_name

    collected: list = []
    for engine in engines:
        try:
            collected.extend(line for line in engine.lines(image) if line.text.strip())
        except Exception:  # noqa: BLE001 — одно увеличение может не сработать
            continue
    if len(engines) < 2 or not collected:
        return sorted(collected, key=lambda ln: ln.top)

    # Группируем строки разных проходов, попавшие на одну высоту.
    collected.sort(key=lambda ln: (ln.top, ln.box[0] if ln.box else 0.0))
    groups: list[list] = []
    for line in collected:
        placed = False
        for group in groups:
            first = group[0]
            if first.box and line.box:
                overlap = (min(first.box[3], line.box[3]) - max(first.box[1], line.box[1]))
                height = min(first.box[3] - first.box[1], line.box[3] - line.box[1])
                if height > 0 and overlap > 0.5 * height:
                    group.append(line)
                    placed = True
                    break
        if not placed:
            groups.append([line])

    merged: list = []
    for group in groups:
        best = None
        best_score = -1.0
        for line in group:
            found = match_name(line.text)
            score = found[2] if found else 0.0
            if score > best_score or (score == best_score and best is not None
                                      and len(line.text) > len(best.text)):
                best, best_score = line, score
        if best is not None:
            text = best.text
            if len({ln.text for ln in group}) > 1:
                longest = max((ln.text for ln in group), key=len)
                if best_score <= 0.0:
                    text = longest
            merged.append(OcrLine(text=text, conf=best.conf, top=best.top, box=best.box))
    merged.sort(key=lambda ln: ln.top)
    return merged


def dedupe_overlapping(lines: list) -> list:
    """Склеивает повторные прочтения одной строки.

    Распознавание иногда возвращает одну и ту же строку дважды с чуть разными
    рамками (например, текст игрового интерфейса внизу панели). Две такие записи
    давали две строки наград и две плашки «?».
    """
    out: list = []
    for line in sorted(lines, key=lambda ln: ln.top):
        duplicate = False
        for other in out:
            if not (line.box and other.box):
                continue
            overlap = min(line.box[3], other.box[3]) - max(line.box[1], other.box[1])
            height = min(line.box[3] - line.box[1], other.box[3] - other.box[1])
            if height <= 0 or overlap <= 0.5 * height:
                continue
            # Та же строка: выбираем более длинное прочтение.
            if len(line.text) > len(other.text):
                out[out.index(other)] = line
            duplicate = True
            break
        if not duplicate:
            out.append(line)
    return out


def drop_detached_tail(lines: list, *, factor: float = 4.0) -> list:
    """Убирает строки, оторванные от списка наград.

    Область чтения захватывает и нижнюю часть экрана, где игра рисует свои
    надписи. Награды идут подряд с одинаковым шагом, поэтому всё, что стоит
    заметно ниже последней строки списка, наградой не является.

    Порог намеренно большой (вчетверо больше обычного шага): бонусная награда
    отделена от списка небольшим разрывом, и её терять нельзя.
    """
    ordered = sorted(lines, key=lambda ln: ln.top)
    if len(ordered) < 3:
        return ordered
    gap = [ordered[i + 1].top - ordered[i].top for i in range(len(ordered) - 1)]
    positive = [value for value in gap if value > 1.0]
    if not positive:
        return ordered
    typical = sorted(positive)[len(positive) // 2]
    if typical <= 0:
        return ordered
    out = [ordered[0]]
    for line in ordered[1:]:
        step = line.top - out[-1].top
        if step > typical * factor:
            break          # список закончился: дальше только интерфейс игры
        out.append(line)
    return out


def capture_region(left: int, top: int, width: int, height: int) -> np.ndarray | None:
    """Снимок маленькой области экрана — мгновенный (несколько миллисекунд)."""
    import mss

    try:
        factory = getattr(mss, "MSS", None) or mss.mss
        with factory() as sct:
            shot = np.array(sct.grab({"left": int(left), "top": int(top),
                                      "width": int(width), "height": int(height)}))
        return shot[:, :, :3].copy()
    except Exception:  # noqa: BLE001 — сбой захвата не должен ломать слежение
        return None


# Где искать крестик закрытия относительно найденного заголовка панели.
# Проверено на снимках панели: крестик всегда в этой точке, совпадение 1.000.
CLOSE_DX_FRACTION = 310.0 / 201.0     # от ширины заголовка
CLOSE_DY_FRACTION = -58.0 / 48.0      # от высоты заголовка
CLOSE_MARGIN = 6                      # запас вокруг крестика при поиске


def close_region(panel, crest_shape: tuple[int, int],
                 margin: int = CLOSE_MARGIN) -> tuple[int, int, int, int]:
    """Область экрана, где должен быть крестик закрытия, по найденной панели."""
    height, width = crest_shape[:2]
    left = int(panel.x + panel.w * CLOSE_DX_FRACTION) - margin
    top = int(panel.y + panel.h * CLOSE_DY_FRACTION) - margin
    return left, top, width + margin * 2, height + margin * 2


def close_present(region: np.ndarray, crest: np.ndarray,
                  threshold: float = 0.7) -> bool:
    """Виден ли крестик закрытия в снятой области."""
    if region is None or crest is None:
        return False
    if region.shape[0] < crest.shape[0] or region.shape[1] < crest.shape[1]:
        return False
    result = cv2.matchTemplate(region, crest, cv2.TM_CCOEFF_NORMED)
    return bool(result.max() >= threshold)


def layout_is_sane(rows) -> tuple[bool, str]:
    """Проверка, что строки наград прочитаны ровно.

    Распознавание иногда склеивает соседние строки, разрезает одну на две или
    возвращает дубли — тогда плашки встают неровно. Такую раскладку не показываем:
    лучше перечитать кадр ещё раз (это и есть авто-перепроверка).
    """
    boxes = [box for _reward, _value, box, _note in rows if box]
    if len(boxes) < 2:
        return True, ""
    heights = sorted(box[3] - box[1] for box in boxes)
    median_height = heights[len(heights) // 2]
    if median_height <= 0:
        return False, "нулевая высота строк"

    # 1. Слишком разные высоты: строки склеены или разрезаны.
    if max(heights) > median_height * 1.6:
        return False, (f"высота строк различается "
                       f"({heights[0]:.0f}..{heights[-1]:.0f} при типичной "
                       f"{median_height:.0f})")

    tops = sorted(box[1] for box in boxes)
    gaps = [tops[i + 1] - tops[i] for i in range(len(tops) - 1)
            if tops[i + 1] - tops[i] > 1.0]
    # 2. Рваный шаг между строками — часть строк потерялась или задвоилась.
    # Последний промежуток не проверяем: там законно стоит бонусная награда,
    # отделённая от списка.
    body = gaps[:-1] if len(gaps) >= 2 else gaps
    if len(body) >= 2:
        typical = sorted(body)[len(body) // 2]
        if max(body) > typical * 1.8:
            return False, f"рваный шаг строк ({typical:.0f} и {max(body):.0f})"
    # 3. Строки налезают друг на друга.
    for index in range(len(tops) - 1):
        if tops[index + 1] - tops[index] < median_height * 0.5:
            return False, "строки налезают друг на друга"
    return True, ""


def panel_border_x(panel) -> float | None:
    """X границы текстовой части панели: левее неё — названия и количество."""
    if panel is None:
        return None
    try:
        return float(panel.x + panel.w * PANEL_RIGHT_FRACTION)
    except (AttributeError, TypeError):
        return None


def blank_rects(frame: np.ndarray, rects: list, *, pad: int = 2) -> np.ndarray:
    """Затирает места, где нарисованы наши плашки, перед распознаванием.

    На повторном нажатии плашки уже висят на экране, попадают в снимок, и
    распознавание приклеивает наши цифры к названию — из-за этого рамки строк
    «уезжали» и цена вставала на название или на количество. Затираем ровно те
    прямоугольники, где мы рисовали, цветом фона рядом с ними.
    """
    if not rects:
        return frame
    height, width = frame.shape[:2]
    for rect in rects:
        try:
            x, y, w, h = (float(v) for v in rect)
        except (TypeError, ValueError):
            continue
        x0, y0 = max(0, int(x) - pad), max(0, int(y) - pad)
        x1, y1 = min(width, int(x + w) + pad), min(height, int(y + h) + pad)
        if x1 <= x0 or y1 <= y0:
            continue
        # Цвет берём слева от плашки: там фон панели, а наших цифр нет.
        sample_x = max(1, x0 - 5)
        strip = frame[y0:y1, max(0, sample_x - 4):sample_x]
        if strip.size:
            colour = np.median(strip.reshape(-1, 3), axis=0).astype(frame.dtype)
        else:
            colour = np.zeros(3, dtype=frame.dtype)
        frame[y0:y1, x0:x1] = colour
    return frame


def measure_text_band(frame: np.ndarray, box: tuple) -> tuple[float, float] | None:
    """Находит строку ТЕКСТА внутри рамки распознавания.

    Рамка от OCR часто выше самой строки: у наград бывает двойная рамка
    (сверху руна, снизу длинное имя) плюс отступы. Поэтому ищем сами буквы —
    «чернила» на фоне — и берём НИЖНЮЮ полосу текста (там длинное имя).

    Возвращает (верх, низ) полосы текста в координатах кадра или None.
    """
    try:
        x0, y0, x1, y1 = (int(round(float(v))) for v in box)
    except (TypeError, ValueError):
        return None
    height, width = frame.shape[:2]
    x0 = max(0, min(x0, width - 2))
    x1 = max(x0 + 2, min(x1, width))
    y0 = max(0, min(y0, height - 2))
    y1 = max(y0 + 2, min(y1, height))
    patch = frame[y0:y1, x0:x1]
    if patch.size == 0:
        return None
    gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY).astype(np.float32)
    # Фон — медиана строки; текст — заметное отклонение от неё.
    ink = np.abs(gray - np.median(gray, axis=1, keepdims=True)).mean(axis=1)
    if float(ink.max()) < 6.0:
        return None
    active = ink > max(3.0, float(ink.max()) * 0.35)

    # Разбиваем на полосы и берём последнюю (нижнюю) — там длинное имя.
    bands: list[tuple[int, int]] = []
    start = None
    for index, flag in enumerate(active):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            bands.append((start, index))
            start = None
    if start is not None:
        bands.append((start, len(active)))
    bands = [band for band in bands if band[1] - band[0] >= 3]
    if not bands:
        return None
    top, bottom = bands[-1]
    return y0 + float(top), y0 + float(bottom)


def dump_debug(roi, lines, rewards) -> None:
    """Сохраняет область наград и распознанные блоки — для разбора проблемных панелей.

    Файлы кладутся рядом с настройками: last_panel.png и last_panel.txt.
    Перезаписываются при каждом разборе, места занимают мало.
    """
    try:
        import cv2

        folder = config_dir()
        if roi is not None and getattr(roi, "size", 0):
            cv2.imwrite(str(folder / "last_panel.png"), roi)
        rows = ["текст | x0,y0 - x1,y1"]
        for line in lines:
            box = line.box or (0, 0, 0, 0)
            rows.append(f"{line.text} | {int(box[0])},{int(box[1])} - {int(box[2])},{int(box[3])}")
        rows.append("")
        rows.append("разобрано:")
        for reward in rewards:
            rows.append(f"  количество={reward.qty} вид={reward.kind} "
                        f"имя={reward.ru_name!r} -> {reward.en_name!r} "
                        f"совпадение={reward.score:.0f}%")
        (folder / "last_panel.txt").write_text("\n".join(rows), encoding="utf-8")
    except Exception:  # noqa: BLE001 — отладочный дамп не должен мешать работе
        pass


def gem_note(book, en_name: str, level: int | None) -> str | None:
    """Подпись с исходной валютой лота: «1 chaos» понятнее, чем «56 exalted»."""
    try:
        source = book.gem_source(en_name, level)
    except Exception:  # noqa: BLE001
        return None
    if not source:
        return None
    currency, amount = source
    if currency in ("exalted", "exalt", "ex", "divine", "div"):
        return None  # цена и так показывается в этих единицах, подпись была бы повтором
    return f"{amount:g} {currency}"


def bonus_indices(rows, divider_top: float | None = None) -> set[int]:
    """Номера строк-бонусов: они выдаются всегда и не участвуют в выборе лучшего.

    Определяем двумя способами:

    * по подписи-разделителю в панели («Бонусная награда») — всё, что ниже неё;
    * иначе по разрыву между строками, причём разрыв должен быть заметно больше
      ЛЮБОГО другого промежутка. Раньше порог считался от медианы, и из-за
      дрожания распознанных рамок подпись «бонус» прыгала при каждом нажатии.
    """
    positioned = [(i, box[1]) for i, (_, _, box, _n) in enumerate(rows) if box]
    if not positioned:
        return set()
    if divider_top is not None:
        below = {i for i, top in positioned if top > divider_top}
        if below:
            return below
    if len(positioned) < 3:
        return set()
    tops = [top for _, top in positioned]
    gaps = [tops[i + 1] - tops[i] for i in range(len(tops) - 1)]
    others = gaps[:-1]
    if not others:
        return set()
    typical = statistics.median(others)
    if typical <= 0:
        return set()
    if gaps[-1] > max(max(others) * 1.25, typical * 1.8):
        return {positioned[-1][0]}
    return set()


def bonus_index(rows) -> int | None:
    """Обратная совместимость: единственная бонусная строка или None."""
    found = sorted(bonus_indices(rows))
    return found[-1] if found else None


def badge_pad(row_height: float) -> float:
    """Внутренний отступ плашки по горизонтали."""
    return max(8.0, row_height * 0.45)


def badge_geometry(text: str, row_height: float, metrics) -> tuple[float, float, float]:
    """Размер плашки под текст: ширина, высота и внутренний отступ.

    Отступы считаем ДО ширины и добавляем их дважды — иначе текст не влезает
    и последние буквы («ex», «chaos») обрезаются рамкой.
    """
    pad = badge_pad(row_height)
    width = metrics.horizontalAdvance(text) + pad * 2.0
    height = max(row_height + 8.0, metrics.height() + 6.0)
    return width, height, pad


UNIT_SHORT = {"exalted": "ex", "divine": "div", "chaos": "chaos", "mirror": "mirror"}


def add_unit(segments: list[tuple[str, str | None]], number: str, currency: str,
             icons: dict, *, prefix: str = "") -> None:
    """Добавляет число и единицу цены: иконкой, а если её нет — текстом.

    Без текстового запаса при отсутствии иконки цена осталась бы «голым» числом
    без указания валюты.
    """
    if prefix:
        segments.append((prefix, None))
    if currency and currency in icons:
        segments.append((number + " ", None))
        segments.append(("", currency))
    else:
        unit = UNIT_SHORT.get(currency, currency)
        segments.append((f"{number} {unit}".strip(), None))


# Правая граница ТЕКСТОВОЙ части панели наград относительно её заголовка:
# измерена по снимкам и совпадает с крестиком закрытия. Левее этой линии
# начинаются названия и количество, поэтому плашка цены левее неё не заходит
# никогда. Вправо (где свободный экран) ограничений нет.
PANEL_RIGHT_FRACTION = 1.55


# Размер плашек ФИКСИРОВАННЫЙ и не зависит ни от панели, ни от распознавания,
# ни от нажатия. Значение подобрано под игру (высота строки награды ~16 px) и
# пересчитывается только при смене разрешения экрана — один раз при запуске.
BADGE_ROW_HEIGHT = 16.0
BADGE_MIN_ROW_HEIGHT = 12.0
BADGE_MAX_ROW_HEIGHT = 34.0


def badge_row_height(screen_height: float | None = None) -> float:
    """Высота плашки: при запуске считается от высоты экрана и дальше не меняется."""
    if not screen_height:
        return BADGE_ROW_HEIGHT
    return max(BADGE_MIN_ROW_HEIGHT,
               # На полноэкранной панели строки заметно выше, чем подсказывает
               # старое деление на 90: плашки выглядели мелкими. 55 даёт 26 px
               # на 1440p (и ограничивается 34 px на более высоких экранах).
               min(screen_height / 55.0, BADGE_MAX_ROW_HEIGHT))


def draw_rows(painter, rows, *, rates: dict[str, float], status: str, size,
              bonus: int | None = None, opacity: float = 0.85,
              pending: set[int] | None = None, icons: dict | None = None,
              panel_height: float | None = None,
              panel_right: float | None = None,
              row_height: float | None = None,
              row_centers: dict[str, float] | None = None
              ) -> list[tuple[float, float, float, float]]:
    """Рисует плашки с ценами и возвращает их прямоугольники на экране.

    Прямоугольники нужны, чтобы перед следующим чтением затереть свои плашки в
    кадре: иначе распознавание приклеивает их к названию и цена «уезжает».
    """
    """Рисует плашки с ценами справа от каждой строки награды."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QFontMetrics, QPen

    base_font = QFont("Segoe UI", 12, QFont.Bold)
    badge_rects: list[tuple[float, float, float, float]] = []
    align = int(Qt.AlignVCenter | Qt.AlignLeft)
    icons = icons or {}
    if bonus is None:
        bonus_set: set[int] = set()
    elif isinstance(bonus, (set, frozenset, list, tuple)):
        bonus_set = {int(v) for v in bonus}
    else:
        bonus_set = {int(bonus)}

    candidates = [(i, v) for i, (_, v, _, _n) in enumerate(rows)
                  if v and i not in bonus_set]
    best_index = max(candidates, key=lambda item: item[1])[0] if candidates else -1
    pending = pending or set()

    # Колонка цены — одна на всю панель. Считаем, где заканчивается текст каждой
    # строки, и берём самый дальний край: распознавание часто отдаёт рамку КОРОЧЕ
    # названия (не захватывает конец и количество), поэтому доверять рамке нельзя.
    # Левее этой колонки — названия и количество; туда плашка не заходит никогда.
    column = max((box[2] for _r, _v, box, _n in rows if box), default=0.0)
    for reward, _value, box, _note in rows:
        if box is None:
            continue
        shown_name = reward.ru_name or reward.raw or ""
        if reward.ru_name:
            shown_name = f"{shown_name} ({reward.qty})"
        ocr_text = reward.raw or ""
        if box[2] > box[0] and ocr_text:
            ratio = min(1.6, len(shown_name) / max(1, len(ocr_text)))
            width = (box[2] - box[0]) * ratio
        else:
            width = 0.0
        column = max(column, box[0] + width)
    if panel_right:
        column = max(column, panel_right + 2.0)

    def row_column(box) -> float:
        return max(box[2], column) + max(4.0, (box[3] - box[1]) * 0.28)

    # Размер плашек — ФИКСИРОВАННЫЙ: он не зависит ни от рамок распознавания, ни
    # от найденной панели, ни от кадра. Раньше он считался из изображения, из-за
    # чего «плясал» между нажатиями — этого быть не должно.
    if row_height:
        fixed_height = float(row_height)
    elif panel_height:
        fixed_height = max(BADGE_MIN_ROW_HEIGHT, min(panel_height * 0.33,
                                                     BADGE_MAX_ROW_HEIGHT))
    else:
        fixed_height = BADGE_ROW_HEIGHT

    for index, (reward, value, box, note) in enumerate(rows):
        if box is None:
            continue
        # Содержимое плашки: кусочки текста и иконки валют, как на сайте торговли.
        segments: list[tuple[str, str | None]] = []
        if value is not None:
            number, currency = format_number(value, rates)
            add_unit(segments, number, currency, icons)
            if reward.qty > 1:
                segments.append((f" ({reward.qty} шт)", None))
            if note:
                amount, _, note_currency = note.partition(" ")
                add_unit(segments, amount, note_currency or "", icons, prefix=" · ")
        elif reward.kind == "estimate":
            segments.append((note or VERISIUM_PILE_ESTIMATE_LABEL, None))
        elif index in pending:
            segments.append(("…", None))   # цена ещё грузится с трейд-сайта
        else:
            segments.append(("?", None))   # цены нет ни в таблице, ни в лотах
        is_bonus = index in bonus_set
        if is_bonus:
            segments.insert(0, ("бонус ", None))

        # Высота плашки — ФИКСИРОВАННАЯ, как была: строка названия плюс запас на
        # рамку и отступы. Значение берётся из фиксированного размера, поэтому
        # при каждом нажатии оно одинаковое.
        height = fixed_height + 8.0
        # Центр плашки = ЦЕНТР СТРОКИ текста (её измеренной полосы). Выше и ниже
        # уходит одинаково — ровно по строке. Положение запоминаем: пока строка та
        # же, оно не меняется, сколько бы раз ни нажимали клавишу.
        # Ключ включает и номер строки: две ОДИНАКОВЫЕ награды в панели не должны
        # делить одно положение — иначе вторая вставала бы на место первой.
        key = f"{index}|{reward.en_name or reward.raw}|{reward.qty}"
        if row_centers is not None and key in row_centers:
            centre_y = row_centers[key]
        else:
            centre_y = (box[1] + box[3]) / 2.0
            if row_centers is not None:
                row_centers[key] = centre_y
        top = max(0.0, centre_y - height / 2.0)

        # Размер шрифта и иконки — от той же фиксированной высоты.
        font = QFont(base_font)
        font.setPointSizeF(min(max(10.0, height * 0.5), 30.0))
        painter.setFont(font)
        metrics = QFontMetrics(font)
        pad = badge_pad(fixed_height)
        icon_size = max(12.0, height * 0.62)

        widths: list[float] = []
        for text, code in segments:
            if code and code in icons:
                widths.append(icon_size + 2.0)
            else:
                widths.append(float(metrics.horizontalAdvance(text)))
        width = sum(widths) + pad * 2.0
        centre_y = top + height / 2.0
        x_left = row_column(box)
        if x_left + width > size[0] - 4:          # не вылезаем за экран
            x_left = max(4.0, size[0] - width - 4)
        rect = QRectF(x_left, centre_y - height / 2.0, width, height)
        badge_rects.append((rect.left(), rect.top(), rect.width(), rect.height()))
        is_best = index == best_index and value is not None

        # Оформление плашки: закруглённая окантовка и полупрозрачная заливка.
        if is_bonus or value is None:
            fill, border = BADGE_FILL_UNKNOWN, BADGE_BORDER_UNKNOWN
        elif is_best:
            fill, border = BADGE_FILL_BEST, BADGE_BORDER_BEST
        elif index in pending:
            fill, border = BADGE_FILL_PENDING, BADGE_BORDER_PENDING
        else:
            fill, border = BADGE_FILL, BADGE_BORDER
        strength = max(0.3, min(1.0, opacity))
        radius = min(rect.height() / 2.2, max(6.0, fixed_height * 0.45))
        pen = QPen(QColor(border[0], border[1], border[2], int(border[3] * strength)))
        pen.setWidthF(max(1.3, fixed_height * 0.07))
        painter.setPen(pen)
        painter.setBrush(QColor(fill[0], fill[1], fill[2], int(fill[3] * strength)))
        painter.drawRoundedRect(rect.adjusted(pen.widthF() / 2, pen.widthF() / 2,
                                              -pen.widthF() / 2, -pen.widthF() / 2),
                                radius, radius)

        if is_bonus:
            color = C_UNKNOWN
        elif is_best:
            color = C_BEST
        elif value is not None or index in pending:
            color = C_TEXT
        else:
            color = C_UNKNOWN
        painter.setPen(QPen(QColor(*color)))
        cursor = rect.left() + pad
        for (text, code), segment_width in zip(segments, widths):
            if code and code in icons:
                pixmap = icons[code].scaled(int(icon_size), int(icon_size),
                                            Qt.KeepAspectRatio, Qt.SmoothTransformation)
                painter.drawPixmap(int(cursor), int(rect.center().y() - pixmap.height() / 2),
                                   pixmap)
            else:
                painter.drawText(QRectF(cursor, rect.top(), segment_width + 4.0, rect.height()),
                                 align, text)
            cursor += segment_width

    if status:
        # Строка состояния — тоже в аккуратной плашке, чтобы не терялась на экране.
        status_font = QFont("Segoe UI", max(10, int(size[1] / 120)))
        painter.setFont(status_font)
        metrics = QFontMetrics(status_font)
        width = metrics.horizontalAdvance(status) + 22
        height = metrics.height() + 10
        rect = QRectF(12, 10, width, height)
        pen = QPen(QColor(120, 170, 255, 150))
        pen.setWidthF(1.3)
        painter.setPen(pen)
        painter.setBrush(QColor(14, 20, 36, 165))
        painter.drawRoundedRect(rect, height / 2.2, height / 2.2)
        painter.setPen(QPen(QColor(226, 232, 244, 235)))
        painter.drawText(rect.adjusted(11, 0, -11, 0), align, status)

    return badge_rects


def render_to_file(image_path: str, out_path: str, *, league: str = "", monitor: int = 0) -> int:
    """Проверка без игры: обрабатывает картинку и рисует результат в файл."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtWidgets import QApplication

    frame = read_image(image_path)
    if frame is None:
        print(f"не удалось прочитать: {image_path}")
        return 2
    prices = Prices(league=league)
    prices.ready.wait(90)
    appraiser = Appraiser(prices)
    rows, status, _ = appraiser.run(frame)
    for reward, value, _, note in rows:
        shown = f"{value:,.1f} exalted" if value else "?"
        if note:
            shown = f"{note} · {shown}"
        print(f"  {reward.raw:44s} -> {shown}")

    app = QApplication.instance() or QApplication(sys.argv[:1])
    h, w = frame.shape[:2]
    qimg = QImage(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB).data, w, h, 3 * w,
                  QImage.Format_RGB888)
    canvas = qimg.copy()
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.Antialiasing)
    draw_rows(painter, rows, rates=prices.book._rates, status=status, size=(w, h),
              bonus=appraiser.bonus, icons=load_icon_pixmaps(),
              panel_height=appraiser.panel.h if appraiser.panel else None,
              panel_right=panel_border_x(appraiser.panel))
    painter.end()
    canvas.save(out_path)
    print(f"картинка с ценами: {out_path}")
    return 0


# --- живой режим -------------------------------------------------------------

def ask_league_dialog(current: str, ask_next_time: bool):
    """Спрашивает лигу для поиска по базе. None — пользователь отказался.

    Спрашиваем ДО загрузки цен: лига определяет и запросы к трейду, и то,
    какой файл кэша поднимать. Иначе выбор из окна не успевал повлиять на
    фоновое сканирование, которое стартует вместе с загрузкой цен.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog,
                                   QDialogButtonBox, QLabel, QPushButton,
                                   QVBoxLayout)

    dialog = QDialog()
    dialog.setWindowTitle("Лига для поиска по базе")
    dialog.setWindowFlag(Qt.WindowStaysOnTopHint, True)
    combo = QComboBox()
    combo.setEditable(True)
    auto_prefix = AUTO_LEAGUE_LABEL.split(" ")[0]

    def fill(names) -> None:
        combo.clear()
        combo.addItem(AUTO_LEAGUE_LABEL, "")
        for name in names:
            combo.addItem(name, name)
        if current:
            index = combo.findData(current)
            if index >= 0:
                combo.setCurrentIndex(index)
            else:
                combo.setEditText(current)

    fill(available_leagues())
    layout = QVBoxLayout(dialog)
    layout.addWidget(QLabel(
        "В какой лиге искать цены?\n"
        "Награды прошлых лиг (например, Aldur's Saga) имеют цену\n"
        "только в своей лиге — в текущей их просто нет."))
    layout.addWidget(combo)
    again = QCheckBox("спрашивать при каждом запуске")
    again.setChecked(ask_next_time)
    layout.addWidget(again)
    refresh = QPushButton("Обновить список лиг из игры")

    def do_refresh() -> None:
        refresh.setEnabled(False)
        refresh.setText("запрашиваю список лиг…")
        QApplication.processEvents()
        names = fetch_leagues()
        fill(names or available_leagues())
        refresh.setText("Обновить список лиг из игры")
        refresh.setEnabled(True)

    refresh.clicked.connect(do_refresh)
    layout.addWidget(refresh)
    buttons = QDialogButtonBox()
    buttons.addButton("Искать по этой лиге", QDialogButtonBox.AcceptRole)
    buttons.addButton("Отмена", QDialogButtonBox.RejectRole)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    if dialog.exec() != QDialog.Accepted:
        return None
    text = combo.currentText().strip()
    data = combo.currentData()
    if data is None:
        chosen = text
    elif str(data).strip():
        chosen = str(data).strip()
    else:
        chosen = "" if text.startswith(auto_prefix) else text
    return chosen, again.isChecked()


def run_live(*, hotkey: str, league: str, monitor: int, selftest: float = 0.0,
             settings: RuSettings | None = None, settingstest: bool = False,
             ask_league: bool | None = None) -> int:
    from PySide6.QtCore import Qt, QTimer, Signal
    from PySide6.QtGui import QAction, QIcon, QPainter
    from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget

    settings = settings or RuSettings().load()
    if hotkey:
        settings.hotkey = hotkey
    if league:
        settings.league = league
    if monitor:
        settings.monitor = monitor
    monitor = settings.monitor

    # Вопрос о лиге — только по явному ключу --ask-league. Автоматический вопрос
    # нельзя показывать при обычном запуске: оверлей стартует с правами
    # администратора (exe с манифестом requireAdministrator), а модальное окно
    # оставалось невидимым — процесс висел на нём, и в трее ничего не появлялось.
    # Поиск по умолчанию идёт по всем лигам, выбор доступен в настройках.
    want_prompt = bool(ask_league) if ask_league is not None else False
    if league and ask_league is None:
        want_prompt = False          # лига уже задана ключом запуска
    if want_prompt and not (selftest or settingstest):
        QApplication.instance() or QApplication(sys.argv[:1])
        answer = ask_league_dialog(settings.league, settings.ask_league)
        if answer is not None:
            chosen, ask_next = answer
            settings.ask_league = ask_next
            settings.league = chosen
            if chosen:
                settings.known_leagues = order_leagues(
                    list(settings.known_leagues) + [chosen])
            settings.save()
            print(f"[лига] выбрана для поиска по базе: {chosen or 'авто'}", flush=True)

    prices = Prices(league=settings.league)

    class Overlay(QWidget):
        triggered = Signal()
        gemPriced = Signal(int, int, float, str)   # поколение, строка, цена, валюта лота
        pricesDone = Signal(int)                   # цены догружены (или не удалось)

        def __init__(self) -> None:
            super().__init__()
            self.setWindowFlags(
                Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus
            )
            self.setAttribute(Qt.WA_TranslucentBackground, True)
            self.setAttribute(Qt.WA_ShowWithoutActivating, True)
            self.rows: list = []
            self.pending: set[int] = set()
            self.signature = None
            self.icons: dict | None = None
            self.bonus: set[int] = set()
            self.status = "загрузка цен…"
            self.appraiser: Appraiser | None = None
            self.panel_box = None          # где панель была в прошлый раз
            self.crest: np.ndarray | None = None   # шаблон крестика закрытия
            self._tick = 0
            self._misses = 0
            self.attempts = 0              # попытки перечитать ровную раскладку
            self.badge_rects: list = []    # где нарисованы плашки (затираем перед чтением)
            # Размер плашек задаётся ОДИН раз при запуске (от высоты экрана) и
            # больше никогда не меняется — ни от кадра, ни от панели, ни от нажатия.
            try:
                geometry = _monitors()[monitor] if monitor < len(_monitors()) else \
                    _monitors()[0]
                screen_height = float(geometry.get("height") or 0)
            except Exception:  # noqa: BLE001
                screen_height = 0.0
            self.row_height = badge_row_height(screen_height)
            # Центры строк (по названию): посчитанные один раз, дальше не меняются.
            self.row_centers: dict[str, float] = {}
            try:
                crest_path = resource_path(CLOSE_TEMPLATE)
                if Path(crest_path).exists():
                    self.crest = read_image(crest_path)
            except Exception:  # noqa: BLE001 — без крестика работаем как раньше
                self.crest = None
            self.busy = False
            self.gem_busy = False
            self.generation = 0
            self.triggered.connect(self.on_hotkey)
            self.gemPriced.connect(self.apply_gem_price)
            self.pricesDone.connect(self.finish_prices)
            # Следим, открыта ли ещё панель: закрыл панель — цены исчезают сами.
            # Крестик проверяется крошечной областью каждые 300 мс, а полная
            # проверка прокрутки идёт раз в 600 мс.
            self.watcher = QTimer(self)
            self.watcher.setInterval(UI_WATCH_INTERVAL_MS)
            self.watcher.timeout.connect(self.check_panel_open)

        def on_hotkey(self) -> None:
            self.toggle()

        def toggle(self) -> None:
            """Нажатие клавиши: перечитать панель и показать уже готовые цены.

            Окно показывается ТОЛЬКО с готовым результатом. Раньше оно появлялось
            до чтения панели (со старым содержимым), затем пряталось на время
            снимка и появлялось снова — это и было мерцание.
            """
            if self.busy:
                return
            self.busy = True
            self.status = "читаю панель…" if prices.ready.is_set() else "загрузка цен…"
            QTimer.singleShot(10, self.analyze)

        def hide_prices(self) -> None:
            self.hide()
            self.watcher.stop()

        @staticmethod
        def roi_signature(detector, frame, panel) -> np.ndarray | None:
            return roi_signature(detector, frame, panel)

        def check_panel_open(self) -> None:
            """Слежение: панель закрыли — сразу убрать цены; список прокрутили — перечитать.

            Закрытие определяем по крестику в углу панели: снимаем крошечную
            область (пара миллисекунд) и сравниваем с шаблоном. Полную проверку со
            поиском заголовка делаем редко — она дорогая.
            """
            if not self.isVisible() or self.busy or self.appraiser is None:
                return
            detector = self.appraiser.detector
            if detector is None:
                return

            self._tick += 1
            # 1. Быстрая проверка крестика (каждый тик).
            # Шаблон крестика рассчитан на прежний масштаб. При совпадении
            # заголовка ниже 0.8 он даёт ложное «панель закрыта», поэтому для
            # такого режима используем надёжную полную проверку ниже.
            if (self.panel_box is not None and self.crest is not None
                    and self.panel_box.confidence >= 0.8):
                left, top, width, height = close_region(self.panel_box, self.crest.shape)
                region = capture_region(left, top, width, height)
                if not close_present(region, self.crest):
                    self._misses += 1
                    # Одного промаха мало: крестик мог перекрыть курсор или подсказка.
                    if self._misses >= 2:
                        self.hide()
                        self.watcher.stop()
                        self._misses = 0
                        print(f"[{time.strftime('%H:%M:%S')}] панель закрыта — плашки убраны",
                              flush=True)
                    return
                self._misses = 0
                # Крестик на месте — проверяем прокрутку раз в 600 мс.
                if self._tick % SCROLL_CHECK_EVERY_TICKS != 0:
                    return

            try:
                frame, _ = capture(monitor)
                panel = detector.find_panel(frame)
            except Exception:  # noqa: BLE001 — сбой захвата не должен ронять показ
                return
            if panel is None:
                self.hide()
                self.watcher.stop()
                print(f"[{time.strftime('%H:%M:%S')}] панель закрыта — плашки убраны",
                      flush=True)
                return

            signature = self.roi_signature(detector, frame, panel)
            if signature is None or self.signature is None:
                self.signature = signature
                return
            changed = float(np.abs(signature - self.signature).mean())
            if changed > SCROLL_THRESHOLD:
                self.signature = signature
                print(f"[{time.strftime('%H:%M:%S')}] список изменился "
                      f"(разница {changed:.0f}) — перечитываю", flush=True)
                self.busy = True
                QTimer.singleShot(10, self.analyze)

        @staticmethod
        def panel_is_open(frame, detector) -> bool:
            """Есть ли панель на кадре (вынесено отдельно, чтобы это можно было тестировать)."""
            return detector is not None and detector.find_panel(frame) is not None

        def analyze(self) -> None:
            # Свои плашки на время снимка НЕ прячем: окно не должно мигать.
            # Их текст отсекается фильтром (drop_own_badges), а «голые» числа —
            # проверкой, похоже ли прочитанное на название предмета.
            keep_hidden = False
            retry = False
            try:
                if self.appraiser is None:
                    self.appraiser = Appraiser(prices)   # нужен детектор для ожидания
                frame, mon, panel = capture_settled(monitor, self.appraiser.detector)
                # Свои плашки уже висят на экране — затираем их в кадре, иначе
                # распознавание приклеит наши цифры к названию и цена «уедет».
                frame = blank_rects(frame, self.badge_rects)
                rows, status, signature = self.appraiser.run(
                    frame, price_gems=False, panel=panel, detect=False)
                self.generation += 1
                self.rows = list(rows)
                self.signature = signature
                self.bonus = set(self.appraiser.bonus)
                # Если набор строк изменился (прокрутка, другая панель) — забываем
                # прежние центры: у новых строк они свои. Если строки те же, центры
                # остаются прежними, и плашки стоят на месте.
                current = {f"{i}|{r.en_name or r.raw}|{r.qty}"
                           for i, (r, _v, _b, _n) in enumerate(self.rows)}
                if current != set(self.row_centers):
                    self.row_centers.clear()
                # Запоминаем, где панель: по этому месту следим за крестиком.
                if panel is not None:
                    self.panel_box = panel
                pending = Appraiser.gem_indices(self.rows)
                live_extra = Appraiser.bulk_indices(self.rows, prices.book)
                self.status = status
                if pending:
                    self.status += f" | камней без свежей цены: {len(pending)}"
                if not prices.ready.is_set():
                    self.status += " (цены ещё грузятся)"
                self.setGeometry(mon["left"], mon["top"], mon["width"], mon["height"])
                if live_extra and prices.ready.is_set() and settings.price_gems:
                    # Камни не догружаем от Shift+D: это была причина очереди
                    # «…» и лишних запросов. Их обновляет только полный скан.
                    # Для пачечных предметов сохраняем фоновую проверку
                    # официального обменника.
                    self.pending = set(live_extra)
                    self.start_gem_worker(live_extra, self.generation)
                else:
                    self.pending = set()
                priced = sum(1 for _, v, _b, _n in self.rows if v)
                heights = sorted(b[3] - b[1] for _r, _v, b, _n in self.rows if b)
                hint = heights[len(heights) // 2] if heights else 0
                # Самопроверка: раскладка должна быть ровной. Если нет — молча
                # перечитываем кадр, а не показываем кривые плашки.
                sane, reason = layout_is_sane(self.rows)
                if not sane and self.attempts < MAX_LAYOUT_ATTEMPTS:
                    self.attempts += 1
                    retry = True
                    print(f"[{time.strftime('%H:%M:%S')}] раскладка неровная "
                          f"({reason}) — перечитываю ({self.attempts})", flush=True)
                    return
                self.attempts = 0
                print(f"[{time.strftime('%H:%M:%S')}] {self.status} | строк {len(self.rows)}, "
                      f"с ценой {priced}, высота строки {hint:.0f}", flush=True)
                # Панель найдена и что-то прочитано — включаем слежение за закрытием.
                if "панель найдена" not in status and self.appraiser.detector is not None:
                    # Панели на экране нет: показывать нечего, случайные совпадения
                    # со всего экрана только путали бы. Окно не показываем вовсе,
                    # чтобы не было пустой вспышки.
                    self.rows = []
                    self.watcher.stop()
                    keep_hidden = True
                    print(f"[{time.strftime('%H:%M:%S')}] панель не найдена — плашки скрыты",
                          flush=True)
                    return
                if settings.auto_hide and self.rows and "панель найдена" in self.status:
                    if not self.watcher.isActive():
                        self.watcher.start()
                else:
                    self.watcher.stop()
            except Exception as exc:  # noqa: BLE001
                # Ошибку обязательно в лог: молчаливое падение обработки выглядит
                # как «нажимаю клавишу — ничего не происходит».
                import traceback

                traceback.print_exc()
                print(f"[{time.strftime('%H:%M:%S')}] ошибка обработки: {exc}", flush=True)
                self.rows = []
                self.status = f"ошибка: {exc}"
            finally:
                self.busy = False
                if retry:
                    # Неровную раскладку не показываем: короткая пауза и повтор.
                    QTimer.singleShot(RETRY_DELAY_MS, self.analyze)
                elif keep_hidden or not self.rows:
                    # Показываем только готовый результат: ни пустых вспышек,
                    # ни промежуточных состояний.
                    self.hide()
                else:
                    self.show()
                    self.repaint()

        def start_gem_worker(self, indices: list[int], generation: int) -> None:
            """Цены камней тянет трейд-сайт по одному — в фоне, с показом по мере готовности."""
            if self.gem_busy:
                return
            self.gem_busy = True
            snapshot = list(self.rows)

            def work() -> None:
                try:
                    for index in indices:
                        reward = snapshot[index][0]
                        value = prices.price(reward)
                        if value:
                            value *= max(1, reward.qty)   # стоимость всего лота
                            self.gemPriced.emit(generation, index, float(value), "")
                finally:
                    self.gem_busy = False
                    # Цены догружены: если что-то получить не удалось, «…» больше
                    # не висит — такие строки честно показывают «?».
                    self.pricesDone.emit(generation)

            threading.Thread(target=work, daemon=True).start()

        def finish_prices(self, generation: int) -> None:
            """Догрузка цен закончилась: убираем «…», оставляя честные «?»."""
            if generation != self.generation:
                return
            if self.pending:
                self.pending.clear()
                self.status = self.status.split(" | камней в очереди")[0]
                self.repaint()

        def apply_gem_price(self, generation: int, index: int, value: float,
                            note: str = "") -> None:
            if generation != self.generation or not (0 <= index < len(self.rows)):
                return
            reward, _, box, old_note = self.rows[index]
            self.rows[index] = (reward, value, box, note or old_note)
            self.pending.discard(index)
            self.status = self.status.split(" | камней в очереди")[0]
            self.repaint()

        def paintEvent(self, event) -> None:  # noqa: N802 (Qt API)
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing)
            rates = prices.book._rates if prices.ready.is_set() else {}
            if self.icons is None and prices.ready.is_set():
                self.icons = load_icon_pixmaps()   # иконки валют, когда они уже скачаны
            self.badge_rects = draw_rows(painter, self.rows, rates=rates,
                                         status=self.status if settings.show_status else "",
                                         size=(self.width(), self.height()), bonus=self.bonus,
                                         opacity=settings.opacity, pending=self.pending,
                                         icons=self.icons or {},
                                         panel_height=self.panel_box.h if self.panel_box else None,
                                         panel_right=panel_border_x(self.panel_box),
                                         row_height=self.row_height,
                                         row_centers=self.row_centers)
            painter.end()

    app = QApplication.instance() or QApplication(sys.argv)
    # Оверлей живёт в системном трее без обычного главного окна.
    app.setQuitOnLastWindowClosed(False)
    overlay = Overlay()

    # Сканер обновляет runtime/prices.json в фоне. Раз в секунду смотрим только
    # время изменения локального файла (без API), чтобы новые цены попадали в
    # следующий Shift+D сразу после записи, а не через пять минут.
    # Пока идёт сканирование, файл переписывается почти каждую секунду, поэтому
    # в лог пишем не чаще раза в 30 секунд: сам подхват дешёвый (единицы
    # миллисекунд), а сотни одинаковых строк только мешали читать лог.
    reload_log_at = {"time": 0.0}

    def reload_daily_price_cache() -> None:
        if not prices.reload_cache_if_changed():
            return
        now = time.monotonic()
        if now - reload_log_at["time"] >= 30.0:
            reload_log_at["time"] = now
            print(f"[{time.strftime('%H:%M:%S')}] подхвачен ежедневный кэш цен", flush=True)

    cache_watch = QTimer(app)
    cache_watch.setInterval(1000)
    cache_watch.timeout.connect(reload_daily_price_cache)
    cache_watch.start()

    def on_hotkey() -> None:
        # Из потока горячей клавиши — только сигнал в поток интерфейса.
        if not foreground_is_game():
            # Нажатие в чужой программе: Shift+D там должна работать как
            # обычная, поэтому оверлей молчит.
            _pid, exe = foreground_process()
            print(f"[{time.strftime('%H:%M:%S')}] {settings.hotkey} нажата не в окне "
                  f"игры (в фокусе {exe or 'неизвестная программа'}) — пропускаю",
                  flush=True)
            return
        overlay.triggered.emit()

    handle = start_global_hotkey(on_hotkey, accelerator=settings.hotkey,
                                 gate=foreground_is_game)
    hotkey_state = {"handle": handle}

    def apply_settings(new: RuSettings) -> None:
        """Применяет настройки: перевешивает клавишу, перезагружает цены при смене лиги."""
        old = RuSettings().load()
        settings.__dict__.update(new.__dict__)
        settings.save()
        if new.hotkey != old.hotkey or hotkey_state["handle"] is None:
            if hotkey_state["handle"] is not None:
                hotkey_state["handle"].stop()
            hotkey_state["handle"] = start_global_hotkey(
                on_hotkey, accelerator=new.hotkey, gate=foreground_is_game)
        if new.league != old.league:
            # Цены разных лиг несмешиваемы (Aldur's Saga есть в Runes of Aldur и
            # отсутствует в Forbidden Rites), поэтому сначала полностью меняем
            # книгу на кэш новой лиги, и только потом уточняем рынок в фоне.
            prices.book.switch_league(new.league)
            prices._remember_cache_mtime()
            threading.Thread(target=prices.book.load, kwargs={"force": True},
                             daemon=True).start()
        tray.setToolTip(f"PoE2 RU Overlay — клавиша {settings.hotkey}")
        tray.showMessage("PoE2 RU Overlay",
                         "Настройки сохранены. Клавиша показа: " + settings.hotkey, QIcon(), 4000)

    def screen_for_dialog():
        """Экран под курсором: окно должно появляться там, где смотрит человек."""
        from PySide6.QtGui import QCursor
        screen = QApplication.screenAt(QCursor.pos())
        return screen or QApplication.primaryScreen()

    def bring_to_front(dialog) -> None:
        """Поднимает окно поверх игры и отдаёт ему фокус.

        Без этого окно могло оказаться ЗА окном игры на весь экран — снаружи
        это выглядит так, будто нажатие на «Настройки…» ничего не сделало.
        """
        dialog.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        screen = screen_for_dialog()
        if screen is not None:
            area = screen.availableGeometry()
            size = dialog.sizeHint()
            dialog.move(area.center().x() - size.width() // 2,
                        area.center().y() - size.height() // 2)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def build_settings_dialog():
        """Собирает окно настроек. Возвращает диалог и словарь его полей."""
        from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                                       QDoubleSpinBox, QFormLayout, QLabel, QLineEdit,
                                       QVBoxLayout)

        dialog = QDialog()
        dialog.setWindowTitle("Настройки PoE2 RU Overlay")
        fields: dict = {}
        form = QFormLayout()
        fields["hotkey"] = QLineEdit(settings.hotkey)
        fields["hotkey"].setPlaceholderText("<Control><Alt>d")
        fields["league"] = QComboBox()
        fields["league"].setEditable(True)
        fields["league"].addItem(AUTO_LEAGUE_LABEL, "")
        for league_name in available_leagues():
            fields["league"].addItem(league_name, league_name)
        if settings.league:
            existing = fields["league"].findData(settings.league)
            if existing >= 0:
                fields["league"].setCurrentIndex(existing)
            else:
                fields["league"].setEditText(settings.league)
        fields["opacity"] = QDoubleSpinBox()
        fields["opacity"].setRange(0.3, 1.0)
        fields["opacity"].setSingleStep(0.05)
        fields["opacity"].setValue(settings.opacity)
        fields["monitor"] = QComboBox()
        for index, mon in enumerate(_monitors()):
            fields["monitor"].addItem(f"экран {index} — {mon['width']}x{mon['height']}", index)
        fields["monitor"].setCurrentIndex(min(settings.monitor, fields["monitor"].count() - 1))
        fields["auto_hide"] = QCheckBox("убирать цены, когда панель в игре закрыта")
        fields["auto_hide"].setChecked(settings.auto_hide)
        fields["status"] = QCheckBox("показывать строку состояния сверху")
        fields["status"].setChecked(settings.show_status)
        fields["gems"] = QCheckBox("оценивать камни умений через трейд-сайт")
        fields["gems"].setChecked(settings.price_gems)

        form.addRow("Горячая клавиша:", fields["hotkey"])
        form.addRow("Лига:", fields["league"])
        form.addRow("Прозрачность плашек:", fields["opacity"])
        form.addRow("Экран:", fields["monitor"])
        form.addRow("", fields["auto_hide"])
        form.addRow("", fields["status"])
        form.addRow("", fields["gems"])
        layout = QVBoxLayout(dialog)
        layout.addLayout(form)
        layout.addWidget(QLabel("Клавиши: <Control><Alt>d, <Control><Shift>r, <Alt>q …"))
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        return dialog, fields

    def open_settings() -> None:
        """Открывает окно настроек и записывает в лог, что оно действительно открылось.

        Лог нужен потому, что из трея не видно, открылось окно или нет: раньше
        нажатие «Настройки…» не оставляло вообще никакого следа.
        """
        import traceback

        stamp = time.strftime("%H:%M:%S")
        print(f"[{stamp}] [настройки] открываю окно", flush=True)
        try:
            dialog, fields = build_settings_dialog()
            bring_to_front(dialog)
        except Exception as exc:  # noqa: BLE001 — нажатие не должно молча пропадать
            traceback.print_exc()
            print(f"[{stamp}] [настройки] окно не открылось: "
                  f"{type(exc).__name__}: {exc}", flush=True)
            tray.showMessage("PoE2 RU Overlay",
                             f"Окно настроек не открылось: {exc}", QIcon(), 6000)
            return
        if dialog.exec():
            chosen_league = (
                "" if str(fields["league"].currentData() or "") == ""
                and fields["league"].currentText().startswith("Авто")
                else fields["league"].currentText().strip())
            apply_settings(RuSettings(
                hotkey=fields["hotkey"].text().strip() or settings.hotkey,
                league=chosen_league,
                monitor=int(fields["monitor"].currentData() or 0),
                opacity=float(fields["opacity"].value()),
                auto_hide=fields["auto_hide"].isChecked(),
                watch_interval_ms=settings.watch_interval_ms,
                price_gems=fields["gems"].isChecked(),
                show_status=fields["status"].isChecked(),
                # Не сбрасываем выбор лиги при каждом сохранении настроек:
                # иначе вписанная вручную лига исчезала бы из списка.
                ask_league=settings.ask_league,
                known_leagues=order_leagues(
                    list(settings.known_leagues) + ([chosen_league] if chosen_league else [])),
            ))
            print(f"[{time.strftime('%H:%M:%S')}] [настройки] сохранены: "
                  f"клавиша {settings.hotkey}, лига {settings.league or 'авто'}", flush=True)
        else:
            print(f"[{time.strftime('%H:%M:%S')}] [настройки] закрыты без изменений",
                  flush=True)

    def settings_selftest() -> None:
        """Проверка: окно настроек строится, поднимается поверх и открывается.

        Проверяем ровно тот путь, которым идёт нажатие «Настройки…»: сборка
        окна, принудительный подъём поверх остальных окон и модальный цикл.
        Окно закрывается само через 0.4 секунды — настройки не меняются.
        """
        import traceback

        try:
            dialog, fields = build_settings_dialog()
            bring_to_front(dialog)
            topmost = bool(dialog.windowFlags() & Qt.WindowStaysOnTopHint)
            visible = dialog.isVisible()
            QTimer.singleShot(400, dialog.reject)
            result = dialog.exec()
            checks = [
                dialog.windowTitle() != "",
                len(fields) == 7,
                fields["hotkey"].text() == settings.hotkey,
                topmost,
                visible,
                result == 0,
            ]
            print("[самотест] окно настроек: "
                  f"{'построено, поверх всех, открылось и закрылось' if all(checks) else 'НЕПОЛНОЕ'}"
                  f" (поверх={topmost}, показано={visible}, цикл окна вернул {result})",
                  flush=True)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            print(f"[самотест] окно настроек: ОШИБКА {type(exc).__name__}: {exc}", flush=True)
        app.quit()

    tray_icon = QIcon(str(resource_path("assets/icon.ico")))
    if tray_icon.isNull():
        # В сборке .ico мог не попасть в комплект — тогда берём .png.
        # Без значка в трее оверлей выглядит «не запустившимся».
        tray_icon = QIcon(str(resource_path("assets/icon.png")))
    tray = QSystemTrayIcon()
    tray.setIcon(tray_icon)
    menu = QMenu()
    act_show = QAction("Показать / обновить цены", menu)
    act_show.triggered.connect(overlay.toggle)
    act_hide = QAction("Скрыть цены", menu)
    act_hide.triggered.connect(overlay.hide_prices)
    act_settings = QAction("Настройки…", menu)

    def open_settings_from_menu() -> None:
        """Закрывает меню и лишь потом открывает окно.

        Так пункт меню не спорит за фокус с ещё открытым всплывающим меню —
        из-за этого нажатие иногда пропадало без следа.
        """
        menu.close()
        QTimer.singleShot(0, open_settings)

    act_settings.triggered.connect(open_settings_from_menu)
    act_log = QAction("Открыть папку с логом", menu)
    act_log.triggered.connect(lambda: __import__("os").startfile(str(settings.log_file().parent)))
    act_hide_auto = QAction("Автоскрытие при закрытии панели", menu)
    act_hide_auto.setCheckable(True)
    act_hide_auto.setChecked(settings.auto_hide)

    def toggle_auto_hide(checked: bool) -> None:
        settings.auto_hide = checked
        settings.save()
        if not checked:
            overlay.watcher.stop()

    act_hide_auto.toggled.connect(toggle_auto_hide)
    act_quit = QAction("Выход", menu)
    act_quit.triggered.connect(app.quit)
    menu.addAction(act_show)
    menu.addAction(act_hide)
    menu.addAction(act_settings)
    menu.addAction(act_hide_auto)
    menu.addAction(act_log)
    menu.addSeparator()
    menu.addAction(act_quit)
    tray.setContextMenu(menu)
    tray.setToolTip(f"PoE2 RU Overlay — клавиша {settings.hotkey}")
    tray.show()
    # Храним ссылки на уровне QApplication: для Qt это явно закрепляет значок
    # до завершения приложения, даже если внутренности функции будут очищены.
    app._aldur_tray = tray
    app._aldur_tray_icon = tray_icon
    print(f"[tray] available={QSystemTrayIcon.isSystemTrayAvailable()} "
          f"icon_null={tray_icon.isNull()} visible={tray.isVisible()}", flush=True)

    # Выход всегда убирает значок из трея и снимает метку копии. Если значок не
    # убрать, Windows оставляет его «мёртвым»: он выглядит живым, но не
    # отвечает на нажатия.
    app.aboutToQuit.connect(tray.hide)
    app.aboutToQuit.connect(forget_instance)

    def retry_hotkey() -> None:
        """Занимает горячую клавишу позже, если она была занята другой копией.

        Без этого оверлей оставался живым, но не реагировал на клавишу до
        следующего запуска — со стороны это выглядит как «оверлей не работает».
        """
        if hotkey_state["handle"] is not None:
            return
        handle = start_global_hotkey(on_hotkey, accelerator=settings.hotkey,
                                     gate=foreground_is_game)
        if handle is None:
            return
        hotkey_state["handle"] = handle
        overlay.status = (f"готово. Жми {settings.hotkey} при активном окне игры")
        print(f"[{time.strftime('%H:%M:%S')}] клавиша {settings.hotkey} освободилась — "
              f"оверлей снова её слушает", flush=True)

    hotkey_watch = QTimer(app)
    hotkey_watch.setInterval(5000)
    hotkey_watch.timeout.connect(retry_hotkey)
    hotkey_watch.start()

    if hotkey_state["handle"] is None:
        # Сразу объясняем, почему клавиша не работает: чаще всего мешает
        # прежняя копия оверлея, которую не удалось закрыть по правам.
        tray.showMessage(
            "PoE2 RU Overlay",
            f"Клавиша {settings.hotkey} занята другой копией оверлея. "
            f"Закрой лишний значок в трее («Выход») — этот оверлей подхватит "
            f"клавишу сам.", QIcon(), 9000)

    def watch_quit_request() -> None:
        """Следит за просьбой новой копии закрыться — и уходит по-хорошему."""
        flag = quit_flag_path()
        try:
            asked = int(flag.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return
        if asked == os.getpid():
            return
        print(f"[{time.strftime('%H:%M:%S')}] новая копия просит закрыться — "
              f"убираю значок и выхожу", flush=True)
        quit_flag_path().unlink(missing_ok=True)
        tray.hide()
        app.quit()

    quit_watch = QTimer(app)
    quit_watch.setInterval(500)
    quit_watch.timeout.connect(watch_quit_request)
    quit_watch.start()

    # Обновитель пишет прогресс в runtime. Читаем только локальный JSON, без
    # сетевых вызовов, и показываем его прямо при наведении на значок в трее.
    scan_state = {"seen": False, "last": None}

    def update_scan_tooltip() -> None:
        try:
            state = json.loads((config_dir() / "runeshape_refresh_state.json").read_text(
                encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            state = {}
        status = state.get("status")
        if scan_state_is_stale(state):
            # Проход прервали (закрыли обновитель, сняли задачу) — иначе трей
            # показывал бы «сканирование 73%» бесконечно, без единого запроса.
            state = dict(state, status="interrupted")
            status = "interrupted"
        if status == "scanning":
            percent = max(0, min(99, int(state.get("progress_percent", 0))))
            phase = str(state.get("phase") or "сканирование")
            completed, total = int(state.get("completed", 0)), int(state.get("total", 0))
            detail = f" ({completed}/{total})" if total else ""
            text = f"PoE2 RU Overlay — сканирование цен: {percent}%\n{phase}{detail}"
            if scan_state["last"] != text:
                tray.setToolTip(text)
                scan_state["last"] = text
            scan_state["seen"] = True
        elif scan_state["seen"]:
            tray.setToolTip(f"PoE2 RU Overlay — клавиша {settings.hotkey}")
            completed = int(state.get("completed", 0) or 0)
            total = int(state.get("total", 0) or 0)
            started = float(state.get("last_started_at", 0) or 0)
            succeeded = float(state.get("last_success_at", 0) or 0)
            # Число наград в каталоге меняется (аудит продаваемости убирает
            # квестовые предметы), поэтому сверяем не с константой 296, а с
            # фактическим объёмом прохода.
            full_success = (status == "ready" and total > 0 and completed >= total
                            and succeeded >= started)
            if full_success:
                tray.showMessage("PoE2 RU Overlay", "Сканирование цен завершено.",
                                 QIcon(), 3500)
            elif status == "failed":
                tray.showMessage("PoE2 RU Overlay", "Сканирование цен завершилось ошибкой.",
                                 QIcon(), 5000)
            elif status == "interrupted":
                tray.showMessage("PoE2 RU Overlay", "Сканирование цен прервано.",
                                 QIcon(), 4000)
            scan_state.update(seen=False, last=None)

    scan_watch = QTimer(app)
    scan_watch.setInterval(1000)
    scan_watch.timeout.connect(update_scan_tooltip)
    scan_watch.start()
    update_scan_tooltip()

    overlay.status = (f"готово. Жми {settings.hotkey} при активном окне игры"
                      if hotkey_state["handle"]
                      else f"горячая клавиша {settings.hotkey} занята — управляй из трея")
    print(f"{overlay.status} (запущен один экземпляр, номер {os.getpid()})", flush=True)
    game = find_game_window()
    print(f"[{time.strftime('%H:%M:%S')}] окно игры: "
          + (f"{game[1] or 'без названия'} ({game[0]}) — клавиша {settings.hotkey} "
             f"слушается только в нём" if game else
             f"не найдено — клавиша {settings.hotkey} сработает, когда игра "
             f"будет в фокусе"), flush=True)

    if selftest > 0:
        def dump() -> None:
            priced = [(r, v) for r, v, _b, _n in overlay.rows if v]
            print(f"[самотест] строк: {len(overlay.rows)}, с ценой: {len(priced)}, "
                  f"статус: {overlay.status}")
            for reward, value in priced:
                print(f"   {value:>10,.1f} ex  {reward.ru_name or reward.en_name}")
            print(f"[самотест] горячая клавиша: "
                  f"{'зарегистрирована' if hotkey_state['handle'] else 'НЕ зарегистрирована'}")
            print(f"[самотест] после двух нажатий плашки видны: "
                  f"{'ДА' if overlay.isVisible() and overlay.rows else 'НЕТ'}", flush=True)
            app.quit()

        QTimer.singleShot(2000, overlay.toggle)          # как будто нажали клавишу
        QTimer.singleShot(9000, overlay.toggle)          # второе нажатие: должно обновить
        QTimer.singleShot(int(selftest * 1000), dump)    # отчёт и выход
        QTimer.singleShot(int(selftest * 1000) + 4000, app.quit)

    if settingstest:
        QTimer.singleShot(1500, settings_selftest)

    return app.exec()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ru_app", description="PoE2 RU Overlay — цены наград")
    parser.add_argument("--hotkey", default="", help=f"горячая клавиша (по умолчанию {DEFAULT_HOTKEY})")
    parser.add_argument("--league", default="", help="лига (по умолчанию автоопределение)")
    parser.add_argument("--ask-league", dest="ask_league", action="store_true", default=None,
                        help="спросить лигу при запуске (по умолчанию — как в настройках)")
    parser.add_argument("--no-ask-league", dest="ask_league", action="store_false",
                        help="не спрашивать лигу при запуске")
    parser.add_argument("--monitor", type=int, default=None, help="0 — все экраны, 1 — первый")
    parser.add_argument("--render", help="проверка без игры: обработать картинку")
    parser.add_argument("--out", default="overlay_preview.png", help="куда сохранить картинку")
    parser.add_argument("--selftest", type=float, default=0.0,
                        help="автотест живого режима: через N секунд отчёт и выход")
    parser.add_argument("--settingstest", action="store_true",
                        help="автотест: построить окно настроек и выйти")
    parser.add_argument("--refresh-catalog", action="store_true",
                        help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.refresh_catalog:
        from aldur_appraiser.runeshape_catalog import main as refresh_catalog
        return refresh_catalog(["--warm-gems"])

    settings = RuSettings().load()
    # Проверка комплекта: в собранном EXE забытые файлы словаря дают «пустые»
    # строки без единой ошибки на экране — поэтому ругаемся громко в журнал.
    for _res in ("assets/lang/ru_items.json", "assets/lang/exchange_ids.json",
                 "assets/templates/header_runeshape_ru.png",
                 "assets/templates/close_ru.png"):
        if not resource_path(_res).exists():
            print(f"ВНИМАНИЕ: в комплекте нет {_res} — распознавание не заработает",
                  flush=True)
    # Прокси в оверлее не используется: чужая переменная окружения (например,
    # NO_PROXY со значением вида [::1]) однажды уже валила все запросы
    # (httpx.InvalidURL: Invalid port). Убираем их сразу, чтобы их не получили
    # и дочерние процессы — обновитель каталога запускается отдельно.
    removed_proxy = drop_proxy_env()
    # Запуск без консоли (pythonw): вывод некуда писать — уводим его в лог-файл.
    if sys.stdout is None or sys.stderr is None:
        log = open(settings.log_file(), "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log
    if removed_proxy:
        print(f"[{time.strftime('%H:%M:%S')}] убрал из окружения переменные прокси: "
              f"{', '.join(removed_proxy)}", flush=True)

    if args.render:
        return render_to_file(args.render, args.out, league=args.league,
                              monitor=args.monitor or 0)
    # Одна копия за раз: сначала по сохранённому номеру, затем — по меткам
    # живых копий (свой поиск, не зависящий от прав и WMI).
    # Новая копия всегда заменяет старую, поэтому «пляски» от двух оверлеев
    # одновременно невозможны.
    stopped = stop_previous_instance()
    if stopped:
        print(f"закрыл прежнюю копию оверлея (номер {stopped})", flush=True)
    extra = stop_other_instances()
    if extra:
        print("закрыл лишние копии оверлея: " + ", ".join(str(pid) for pid in extra),
              flush=True)
    register_instance()
    remember_pid()
    return run_live(hotkey=args.hotkey, league=args.league,
                    monitor=args.monitor if args.monitor is not None else 0,
                    selftest=args.selftest, settings=settings,
                    settingstest=args.settingstest, ask_league=args.ask_league)


if __name__ == "__main__":
    sys.exit(main())

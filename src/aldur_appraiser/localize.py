"""Русский слой: строки OCR с панели наград -> канонические награды.

Русский клиент показывает награды иначе, чем английский:

    «Совершенная сфера превращения (1)»   — количество в скобках, а не «1x ...»
    «Уровень умения 20: Дождь клинков»    — камень умения с указанием уровня

Модуль приводит обе формы к одному виду: количество, русское имя (для показа)
и английское имя (по нему берётся цена). Словарь RU->EN взят из данных
Exiled Exchange 2 (MIT, Kvan7 / Awakened PoE Trade).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache

from rapidfuzz import fuzz, process

from aldur_appraiser.resources import resource_path

# Игровой шрифт путает цифры с похожими буквами: «3» читается как «З», «0» как «О»,
# «1» как «І» или «|». Поэтому количество чиним отдельно от остального текста.
DIGIT_LOOKALIKE = str.maketrans({
    "З": "3", "з": "3", "Э": "3", "э": "3",
    "О": "0", "о": "0", "O": "0", "o": "0", "Q": "0",
    "І": "1", "і": "1", "I": "1", "l": "1", "|": "1", "!": "1",
    "S": "5", "s": "5", "Б": "6", "б": "6", "Ч": "4", "ч": "4", "У": "4",
})
_QTY_CHARS = "0-9A-Za-zА-Яа-яЁё|!Іі"


def qty_from_token(token: str) -> int | None:
    """Превращает прочитанный «токен количества» в число или None.

    «З» -> 3, «О» -> 0, «І» -> 1: без этого количество (3) терялось, и цена
    считалась за одну штуку.
    """
    fixed = token.translate(DIGIT_LOOKALIKE)
    return int(fixed) if fixed.isdigit() else None


# «... (1)» в конце строки — так русский клиент показывает количество.
# Точка после скобки — обычное дело для OCR: без неё «(2).» не распознавалось,
# цифра оставалась в названии, и награда пропадала целиком.
QTY_SUFFIX_RE = re.compile(
    rf"[\(\[]\s*([{_QTY_CHARS}]{{1,4}})\s*[\)\]]\s*[.,;:]?\s*$")
# «1x ...» — запасная форма на случай другой вёрстки.
QTY_PREFIX_RE = re.compile(r"^\s*([0-9A-Za-zА-Яа-яЁё]{1,4})\s*[xX×]\s*")
# Символы, которые OCR выдаёт вместо цифр уровня: «2О», «2З», «І9».
_LEVEL_CHARS = "0-9OoОоЗзІіIl|!SБб"
# «Уровень умения 20: …», «Уровень камня поддержки 20: …»
GEM_RE = re.compile(
    rf"уровень\s+(умения|камня|камней|поддержки|камня\s+поддержки)\s*"
    rf"([{_LEVEL_CHARS}]{{1,3}})?\s*[:\-—]?\s*",
    re.IGNORECASE,
)
# OCR иногда теряет «Уровень», оставляя «Умения 20: …» в начале строки.
GEM_SHORT_LEVEL_RE = re.compile(
    rf"^\s*умени[ея]\s*([{_LEVEL_CHARS}]{{1,3}})\s*[:\-—]?\s*", re.IGNORECASE)
# «Умение: Дождь клинков», «Поддержка: Рунное извлечение» — так камни подписаны
# в панели торговца. Уровень там не указывают, поэтому цену ищем без фильтра
# уровня (иначе у части камней лотов вообще нет и цена не находится).
GEM_WORD_RE = re.compile(
    r"^\s*(умение|умения|учение|поддержка|подцержка|поддержка|поддрежка|support|skill)"
    r"\s*[:\-—.]?\s*",
    re.IGNORECASE,
)
# Слова-маркеры камней. Распознавание читает их по-разному («Увление»,
# «Уровень утиения»), поэтому сравнение идёт похожестью, а не точным списком.
_GEM_WORD_HINTS = ("умение", "умения", "поддержка", "support", "skill")
_LEVEL_HINTS = ("уровень умения", "уровень камня", "уровень камня поддержки",
                "уровень камней")
# «<слово> <слово> 20: …» — строка уровня камня. Голова ограничена 40 знаками
# без цифр и двоеточия, поэтому обычные названия наград сюда не попадают.
_LEVEL_LINE_RE = re.compile(r"^\W*([^\d:]{2,40}?)\s*(\d{1,2})\s*[:\-—]\s*(.+)$")
# «<слово>: остаток» — подпись камня без уровня.
_WORD_TAIL_RE = re.compile(r"^\W*([A-Za-zА-Яа-яЁё]{3,14})\s*[:\-—.]\s*(\S.*)$")


def _letters(text: str) -> str:
    return "".join(ch for ch in (text or "").casefold() if ch.isalpha())


def _like(text: str, hints: tuple[str, ...]) -> float:
    """Насколько прочитанное слово похоже на один из эталонов (0..100)."""
    norm = _letters(text)
    if not norm:
        return 0.0
    return max(fuzz.ratio(norm, hint) for hint in hints)


def level_prefix(text: str, *, cutoff: float = 60.0) -> tuple[int, str] | None:
    """«Уровень умения 20: …» с допуском на ошибки распознавания.

    Игровой шрифт читается как «Уровэ-ъ умеюля 20», «Уровень утиения 20». Раньше
    такая строка не опознавалась камнем: с уровнем она отбрасывалась целиком и
    награда оставалась БЕЗ плашки вообще, а без уровня превращалась в
    «неизвестный предмет» и показывала «?». Уровень здесь берётся из самих цифр
    строки, без догадок, поэтому цена по-прежнему ищется строго по своему уровню.
    """
    match = _LEVEL_LINE_RE.match(text)
    if match is None:
        return None
    head, level, body = match.group(1), match.group(2), match.group(3)
    letters = _letters(head)
    if not letters:
        return None
    # Строгая проверка подстроки «уро» отбрасывала строку целиком, если OCR
    # прочитал слово с ошибкой: «. Урвень умения 20: Кольцо моротюго шимени» —
    # награда ИСЧЕЗАЛА с панели, плашки на ней не было вообще (пропадала
    # «Кольцо морозного пламени»). Смотрим похожесть на «уровень», а не буквы.
    if fuzz.partial_ratio(letters, "уровень") < 70:
        return None
    if _like(head, _LEVEL_HINTS) < cutoff:
        return None
    return int(level), body.strip()


def word_prefix(text: str, *, cutoff: float = 70.0) -> str | None:
    """«Умение: …» / «Поддержка: …» с допуском на ошибки («Увление: …»)."""
    match = _WORD_TAIL_RE.match(text)
    if match is None:
        return None
    if _like(match.group(1), _GEM_WORD_HINTS) < cutoff:
        return None
    return match.group(2).strip()


def word_tail(text: str) -> str | None:
    """Остаток строки вида «<слово>: …» — даже если слово прочитано мусором.

    Нужен как последняя попытка: если после непонятного слова стоит известное
    название камня, это камень, а не безымянный предмет.
    """
    match = _WORD_TAIL_RE.match(text)
    return match.group(2).strip() if match else None
# Заголовок панели и служебные подписи — не награды.
UI_LABEL_RE = re.compile(r"(комбинаци|комлнаци|combination|награда|reward)", re.IGNORECASE)
BONUS_RE = re.compile(r"бонус", re.IGNORECASE)
# Мусорные символы, которые OCR цепляет сбоку от текста.
NOISE_EDGE_RE = re.compile(r"^[^0-9A-Za-zА-Яа-яЁё]+|[^0-9A-Za-zА-Яа-яЁё\)\]]+$")


@dataclass(frozen=True)
class Reward:
    """Одна награда с панели."""

    kind: str                 # "item" | "gem" | "estimate"
    qty: int
    raw: str                  # исходная строка OCR
    ru_name: str | None = None   # совпавшее русское имя из словаря
    en_name: str | None = None   # английское имя (ключ цены)
    gem_level: int | None = None
    score: float = 0.0           # качество совпадения, 0..100


@lru_cache(maxsize=1)
def ru_to_en() -> dict[str, str]:
    """Словарь русских названий предметов -> английских."""
    path = resource_path("assets/lang/ru_items.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache(maxsize=1)
def _names() -> tuple[str, ...]:
    return tuple(ru_to_en().keys())


def clean_name(text: str) -> str:
    """Убирает боковой мусор OCR, скобки количества и лишние точки/двоеточия."""
    text = QTY_SUFFIX_RE.sub("", text)
    text = text.strip().strip(".,:;«»\"'`-—–_ ")
    return NOISE_EDGE_RE.sub("", text).strip()


def looks_like_item_name(text: str) -> bool:
    """Похоже ли прочитанное на название предмета, а не на мусор распознавания.

    Игровой шрифт иногда даёт случайные наборы вроде «l.iF4j» или «ЬЊ•ень».
    Такие строки нельзя показывать наградой: на их месте появлялась плашка «?»
    прямо посреди панели, поверх названий соседних наград.
    """
    letters = [ch for ch in text if ch.isalpha()]
    if len(letters) < 5:
        return False
    if any(ch.isdigit() for ch in text):
        return False                       # в названиях предметов цифр не бывает
    if any(ch in text for ch in "•|\\/<>*_=+\"'`~^"):
        return False
    vowels = sum(1 for ch in text.lower() if ch in "аеёиоуыэюяaeiouy")
    if vowels < 2:
        return False                       # без гласных это не слово
    stripped = text.lstrip()
    return bool(stripped) and stripped[0].isupper()


def match_name(text: str, cutoff: float = 70.0) -> tuple[str, str, float] | None:
    """Нечётко находит русское название в словаре. -> (ru, en, score)."""
    cleaned = clean_name(text)
    if len(cleaned) < 3:
        return None

    # Windows OCR может вернуть одну строку целиком заглавными буквами
    # («Сфера ВОЗВЫШеНИЯ»). Названия в словаре написаны обычным регистром;
    # точное совпадение не должно от него зависеть.
    folded = cleaned.casefold()
    for name in _names():
        if name.casefold() == folded:
            return name, ru_to_en()[name], 100.0

    # Распознавание часто теряет начало длинного названия: вместо
    # «Неогранённый камень умения (уровень 19)» приходит «камень умения (уровень 19)».
    # Если прочитанное — точный хвост ровно одного названия из словаря, берём его.
    if len(cleaned) >= 10:
        tails = [name for name in _names() if name.endswith(cleaned) and len(name) > len(cleaned)]
        if len(tails) == 1:
            ru = tails[0]
            return ru, ru_to_en()[ru], 100.0

    hit = process.extractOne(cleaned, _names(), scorer=fuzz.ratio,
                             processor=str.casefold, score_cutoff=cutoff)
    if not hit:
        return None
    ru, score, _ = hit
    return ru, ru_to_en()[ru], float(score)


def parse_line(raw: str, *, cutoff: float = 70.0, allow_unknown: bool = True) -> Reward | None:
    """Разбирает одну строку OCR. None — если это не награда.

    allow_unknown=True сохраняет строку с ненайденным именем (покажем «?»);
    allow_unknown=False отбрасывает всё, что не совпало со словарём.
    """
    text = (raw or "").strip()
    if not text:
        return None
    if UI_LABEL_RE.search(text) and not re.search(r"\d", text):
        return None
    # Заголовок панели сам попадает в ROI, а OCR часто искажает в нём слово
    # «комбинации» сильнее, чем позволяет обычная регулярка. Без фильтра он
    # становился ложной первой «наградой» и ломал проверку шага строк.
    if not re.search(r"\d", text) and fuzz.ratio(
            text.casefold().strip(" .,:;"), "рунотворческие комбинации") >= 70:
        return None
    if BONUS_RE.search(text):
        return None  # строка-разделитель «Бонусная награда», не сама награда

    qty = 1
    suffix = QTY_SUFFIX_RE.search(text)
    if suffix:
        parsed = qty_from_token(suffix.group(1))
        if parsed is not None:
            qty = parsed
        text = QTY_SUFFIX_RE.sub("", text)
        # Если количество встретилось дважды («(3) (19)»), верным считаем первое:
        # второе обычно прилипло из другого места панели.
        second = QTY_SUFFIX_RE.search(text)
        if second:
            parsed_second = qty_from_token(second.group(1))
            if parsed_second is not None:
                qty = parsed_second
            text = QTY_SUFFIX_RE.sub("", text)
    else:
        prefix = QTY_PREFIX_RE.match(text)
        if prefix:
            parsed = qty_from_token(prefix.group(1))
            if parsed is not None:
                qty = parsed
            text = QTY_PREFIX_RE.sub("", text)

    # Первые строки панели OCR чаще всего читает вместе с краем свитка:
    # «• Уровень умения 20: …». Ищем маркер не только в нулевой позиции,
    # иначе такой камень ошибочно становится безымянным предметом и отпадает.
    gem = GEM_RE.search(text)
    if gem:
        level = qty_from_token(gem.group(2) or "")
        body = text[gem.end():].strip()
        kind = "gem"
    else:
        short_level = GEM_SHORT_LEVEL_RE.match(text)
        tolerant_level = None if short_level else level_prefix(text)
        if short_level:
            level = qty_from_token(short_level.group(1))
            body = text[short_level.end():].strip()
            kind = "gem"
        elif tolerant_level is not None:
            # «Уровэ-ъ умеюля 20: …» — тот же камень с уровнем, только слово
            # распознано с ошибкой. Уровень берём из цифр строки как есть.
            level = tolerant_level[0]
            body = tolerant_level[1]
            kind = "gem"
        else:
            word = GEM_WORD_RE.match(text)
            tolerant_word = None if word else word_prefix(text)
            if word and len(text[word.end():].strip()) >= 3:
                # «Умение: …» / «Поддержка: …» — это камень, а не предмет.
                level = None
                body = text[word.end():].strip()
                kind = "gem"
            elif tolerant_word is not None and len(tolerant_word) >= 3:
                # «Увление: …» — то же самое, но слово распознано с ошибкой.
                level = None
                body = tolerant_word
                kind = "gem"
            else:
                tail = word_tail(text)
                if (tail is not None and len(tail) >= 3
                        and match_name(tail, cutoff=cutoff) is not None):
                    # Слово-маркер прочитано совсем неразборчиво, но название
                    # после него известно — значит это камень без уровня.
                    level = None
                    body = tail
                    kind = "gem"
                else:
                    level = None
                    body = text.strip()
                    kind = "item"

    # "Груда веризия" — награда из рунной комбинации, а не предмет с
    # самостоятельным лотом на трейде. Выделяем её до нечёткого словаря:
    # иначе OCR может спутать её с произвольным предметом и отправить на
    # бессмысленный запрос цены.
    if kind == "item" and re.search(r"груд[аы]\s+вериз", body, re.IGNORECASE):
        return Reward(kind="estimate", qty=qty, raw=raw,
                      ru_name="Груда веризия", en_name="Verisium Pile", score=100.0)
    # Игра пишет эту награду и как «5 случайных валют», и как
    # «5 шт. случайной валюты»: между числом и словами стоит сокращение «шт.»,
    # а само название в родительном падеже. Прежняя проверка ждала только
    # первый вид, поэтому живая строка панели отбрасывалась целиком — награда
    # с известной оценкой (≈9,5 ex по каталогу) не показывалась вовсе.
    # Между числом и «случайной» допускаем любое короткое слово: OCR иногда
    # читает «шт.» мусором («игг.»), а название вокруг остаётся верным.
    if kind == "item" and re.search(
            r"\b\d+\s*(?:шт\w*\.?|[xх×]|\S{1,6})?\s*случайн\w*\s+валют",
            body, re.IGNORECASE):
        return Reward(kind="estimate", qty=qty, raw=raw,
                      ru_name="5 случайных валют", en_name="5x Random Currency", score=100.0)

    found = match_name(body, cutoff=cutoff)
    if found is None:
        cleaned = clean_name(body)
        # Строки без единой буквы (числа, полоски HUD) наградой не являются.
        if not re.search(r"[A-Za-zА-Яа-яЁё]", cleaned):
            return None
        if not allow_unknown or len(cleaned) < 3 or not looks_like_item_name(cleaned):
            return None
        return Reward(kind=kind, qty=qty, raw=raw, ru_name=None,
                      en_name=cleaned, gem_level=level)
    ru, en, score = found
    # На панели рунной комбинации уровень неогранённого камня духа не указан:
    # выпадает один из разных уровней. Это не точный предметный лот, а диапазон.
    if en == "Uncut Spirit Gem":
        return Reward(kind="estimate", qty=qty, raw=raw, ru_name=ru, en_name=en,
                      gem_level=None, score=score)
    return Reward(kind=kind, qty=qty, raw=raw, ru_name=ru, en_name=en,
                  gem_level=level, score=score)


def parse_lines(lines: Iterable[str], *, cutoff: float = 70.0,
                allow_unknown: bool = True) -> list[Reward]:
    out: list[Reward] = []
    for raw in lines:
        reward = parse_line(raw, cutoff=cutoff, allow_unknown=allow_unknown)
        if reward is not None:
            out.append(reward)
    return out

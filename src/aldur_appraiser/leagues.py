"""Выбор лиги, по которой оверлей ищет цены в базе.

Зачем отдельный модуль: часть наград каталога существует не во всех лигах.
Например, Aldur's Saga и низкие уровни Thaumaturgic Flux (8–11) есть только в
Runes of Aldur, а в текущей лиге их получить нельзя — там цену взять неоткуда.
Поэтому лигу нужно выбирать до сканирования, а список лиг брать у официального
трейда: тогда следующая лига появится в выборе сама, без правки кода.

Прошлые лиги не забываются: список известных лиг лежит в настройках оверлея,
а цены и каталог хранятся отдельным файлом на каждую лигу.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from aldur_appraiser.ru_settings import RuSettings

TRADE_LEAGUES_URL = "https://www.pathofexile.com/api/trade2/data/leagues"
# Список лиг меняется раз в несколько месяцев; чаще спрашивать трейд незачем.
LEAGUES_TTL = 12 * 60 * 60
# Постоянные лиги всегда в конце списка: играть и торговать ими будут реже.
PERMANENT = ("standard", "hardcore", "ssf standard", "ssf hardcore")
# Если трейд недоступен, а памяти о лигах нет — показываем хотя бы эти.
FALLBACK_LEAGUES = ("Forbidden Rites", "Runes of Aldur", "Standard")
# Пункт «не выбирать лигу вручную»: пустая лига = определить при сканировании.
AUTO_LEAGUE_LABEL = "Авто — определить самой"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "application/json",
    "Referer": "https://www.pathofexile.com/trade2/search/poe2",
}


def leagues_cache_path() -> Path:
    return RuSettings().path.parent / "leagues.json"


def league_slug(name: str) -> str:
    """Имя лиги в вид, пригодный для имени файла: ``Forbidden Rites`` -> ``forbidden-rites``."""
    return re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower()).strip("-")


def order_leagues(names) -> list[str]:
    """Временные лиги в исходном порядке, постоянные — в конце, без повторов."""
    seen: set[str] = set()
    temporary: list[str] = []
    permanent: list[str] = []
    for raw in names or ():
        name = str(raw or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        (permanent if name.lower() in PERMANENT else temporary).append(name)
    return temporary + permanent


def read_cache() -> dict:
    try:
        data = json.loads(leagues_cache_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_leagues_cache(leagues: list[str]) -> None:
    """Пишет список лиг через временный файл.

    Обычная запись поверх давала момент, когда файл пуст: одновременное
    чтение (окно настроек) теряло список лиг до запасного набора.
    """
    path = leagues_cache_path()
    tmp = path.with_suffix(f".{os.getpid()}.json.tmp")
    try:
        tmp.write_text(json.dumps({"fetched_at": time.time(), "leagues": leagues},
                                  ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def cached_leagues() -> list[str]:
    data = read_cache()
    leagues = data.get("leagues")
    return [str(name) for name in leagues] if isinstance(leagues, list) else []


def cache_is_stale(now: float | None = None) -> bool:
    data = read_cache()
    try:
        fetched = float(data.get("fetched_at") or 0)
    except (TypeError, ValueError):
        return True
    return (now if now is not None else time.time()) - fetched >= LEAGUES_TTL


def known_leagues() -> list[str]:
    """Лиги, которые пользователь уже видел или вписал вручную."""
    saved = RuSettings().load().known_leagues
    return [str(name) for name in saved] if isinstance(saved, list) else []


def remember_leagues(names) -> list[str]:
    """Запоминает лиги в настройках, чтобы прошлые лиги не терялись."""
    merged = order_leagues(list(known_leagues()) + list(names or ()))
    if merged:
        settings = RuSettings().load()
        settings.known_leagues = merged
        settings.save()
    return merged


def parse_leagues(payload) -> list[str]:
    """Достаёт имена лиг из ответа ``/data/leagues`` (список строк или словарей)."""
    if isinstance(payload, dict):
        payload = payload.get("result", payload)
    if not isinstance(payload, list):
        return []
    names: list[str] = []
    for item in payload:
        if isinstance(item, dict):
            name = item.get("id") or item.get("text")
        else:
            name = item
        if name:
            names.append(str(name))
    return order_leagues(names)


def fetch_leagues(timeout: float = 15.0, client=None) -> list[str]:
    """Спрашивает список лиг у официального трейда и кладёт его в кэш.

    При любой сетевой ошибке возвращает то, что уже известно — выбор лиги не
    должен ломаться из-за недоступного сайта.
    """

    from aldur_appraiser import netenv

    owns = client is None
    client = client or netenv.client(timeout=timeout)
    try:
        response = client.get(TRADE_LEAGUES_URL, headers=HEADERS, timeout=timeout)
        response.raise_for_status()
        leagues = parse_leagues(response.json())
    except Exception:  # noqa: BLE001 — без сети работаем на памяти о лигах
        return available_leagues()
    finally:
        if owns:
            client.close()
    if not leagues:
        return available_leagues()
    try:
        write_leagues_cache(leagues)
    except OSError:
        pass
    remember_leagues(leagues)
    return leagues


def available_leagues(refresh: bool = False, client=None) -> list[str]:
    """Все лиги для окна выбора: известные + последний ответ трейда + запасные."""
    if refresh:
        return fetch_leagues(client=client)
    names = known_leagues() + cached_leagues() + list(FALLBACK_LEAGUES)
    return order_leagues(names)

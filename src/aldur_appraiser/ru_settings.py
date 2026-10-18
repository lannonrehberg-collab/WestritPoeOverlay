"""Настройки русского оверлея: сохранение в файл рядом с кэшем цен."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path


def config_dir() -> Path:
    # Портативная копия задаёт эту переменную в launchers и держит все
    # изменяемые данные рядом с исходниками, а не в общем AppData.
    override = os.environ.get("ALDUR_APPRAISER_DATA_DIR")
    if override:
        path = Path(override)
    elif getattr(sys, "frozen", False):
        # Однофайловый EXE лежит в переносимой папке проекта: данные должны
        # оставаться рядом с ним, а не уходить в общий AppData.
        path = Path(sys.executable).resolve().parent / "runtime"
    else:
        base = os.environ.get("APPDATA") or str(Path.home())
        path = Path(base) / "aldur-appraiser-ru"
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class RuSettings:
    """Что можно менять из меню настроек."""

    hotkey: str = "<Shift>d"          # горячая клавиша показа
    league: str = ""                  # пусто = определять автоматически
    monitor: int = 0                  # 0 — все экраны
    opacity: float = 0.85             # прозрачность плашек, 0.3..1.0
    auto_hide: bool = True            # скрывать, когда панель в игре закрыта
    watch_interval_ms: int = 1500     # как часто проверять, открыта ли панель
    price_gems: bool = True           # оценивать камни умений через трейд-сайт
    show_status: bool = True          # показывать строку состояния сверху
    # Лигу выбираем перед первым сканированием: награды прошлых лиг (например,
    # Aldur's Saga) в текущей лиге цены не имеют, а искать их надо в своей лиге.
    ask_league: bool = True           # спрашивать лигу при запуске оверлея
    known_leagues: list[str] = field(default_factory=list)  # виденные лиги

    @property
    def path(self) -> Path:
        return config_dir() / "settings.json"

    def load(self) -> RuSettings:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self
        for key, value in data.items():
            if hasattr(self, key):
                setattr(self, key, value)
        return self

    def save(self) -> None:
        try:
            self.path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        except OSError:
            pass

    def log_file(self) -> Path:
        return config_dir() / "overlay.log"

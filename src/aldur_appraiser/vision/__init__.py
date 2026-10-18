"""Vision half — OS/optional-dependency specific. Imported lazily."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np


def read_image(path: str | Path, flags: int = cv2.IMREAD_COLOR) -> np.ndarray | None:
    """Читает картинку так, чтобы путь с кириллицей её не ломал.

    ``cv2.imread`` на Windows открывает файл ANSI-функциями: если в пути есть
    буквы вне ASCII (например, ``C:\\Users\\Иван\\...``), он молча возвращает
    None — это выглядело как «шаблон не найден», хотя файл на месте. Поэтому
    читаем файл сами и разбираем картинку из памяти.
    """
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
    except OSError:
        return None
    if data.size == 0:
        return None
    image = cv2.imdecode(data, flags)
    return image


def write_image(path: str | Path, image: np.ndarray) -> bool:
    """Сохраняет картинку так же надёжно, как читает (кириллица в пути).

    ``cv2.imwrite`` молча возвращает False на таком пути — сохраняем через
    буфер в памяти.
    """
    target = Path(path)
    try:
        ok, buffer = cv2.imencode(target.suffix or ".png", image)
    except cv2.error:
        return False
    if not ok:
        return False
    try:
        buffer.tofile(str(target))
    except OSError:
        return False
    return True

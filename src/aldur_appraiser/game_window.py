"""Окно самой игры: поиск, «активно ли оно сейчас», область клиента.

Нужно затем, чтобы оверлей реагировал на свою клавишу **только у игры**: в
другой программе та же комбинация должна работать как обычная (в игре
Shift+D не занята ничем, а в браузере это переключение режима).

Здесь одна реализация на весь проект: и оверлей, и кнопка снимков берут окно
игры отсюда, а не ищут его каждый по-своему.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path

from aldur_appraiser.winproc import image_name

# Имена программ игры: обычный клиент, Steam, старый клиент.
GAME_EXE = ("pathofexile", "pathofexile_x64", "pathofexilesteam", "pathofexile2")


def _process_stem(pid: int) -> str:
    """Имя программы без пути и расширения, строчными буквами."""
    path = image_name(pid)
    return Path(path).stem.lower() if path else ""


def is_game_process(pid: int) -> bool:
    """Программа с таким номером — игра?"""
    exe = _process_stem(pid)
    return bool(exe) and any(exe.startswith(name) for name in GAME_EXE)


def _root(hwnd: int) -> int:
    """Самое верхнее окно цепочки: у игры главное окно может быть владельцем."""
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        parent = user32.GetAncestor(wintypes.HWND(hwnd), 2)  # GA_ROOT
        return int(parent) if parent else int(hwnd)
    except Exception:  # noqa: BLE001
        return int(hwnd)


def foreground_is_game() -> bool:
    """Окно игры сейчас в фокусе (человек смотрит в игру)?

    Только в этом случае оверлей считает, что нажатие его клавиши относится к
    игре. Ошибка в любую сторону — «нет»: лучше не сработать, чем сработать
    поверх чужой программы.
    """
    return bool(_pid := _foreground_pid()) and is_game_process(_pid)


def foreground_process() -> tuple[int, str]:
    """(номер, имя программы) окна в фокусе — чтобы объяснить, где нажали."""
    pid = _foreground_pid()
    return pid, _process_stem(pid) if pid else ""


def _foreground_pid() -> int:
    if not hasattr(ctypes, "WinDLL"):
        return 0
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetForegroundWindow.restype = wintypes.HWND
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return 0
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(wintypes.HWND(_root(int(hwnd))),
                                        ctypes.byref(pid))
        return int(pid.value)
    except Exception:  # noqa: BLE001
        return 0


def game_window_title(hwnd: int) -> str:
    """Заголовок окна игры (пусто, если прочитать не вышло)."""
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        length = user32.GetWindowTextLengthW(wintypes.HWND(hwnd))
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(wintypes.HWND(hwnd), buffer, length + 1)
        return buffer.value
    except Exception:  # noqa: BLE001
        return ""


def find_game_window() -> tuple[int, str] | None:
    """Окно игры: (дескриптор, заголовок). None, если игра не запущена."""
    if not hasattr(ctypes, "WinDLL"):
        return None
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    found: list[tuple[int, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _param):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if is_game_process(pid.value):
            found.append((int(hwnd), game_window_title(int(hwnd))))
            # Заголовок нужен для надписи «окно подключено», поэтому ищем
            # дальше: у игры есть и служебные окна без названия.
            return len(found) < 8
        return True

    try:
        user32.EnumWindows(visit, 0)
    except Exception:  # noqa: BLE001
        return None
    if not found:
        return None
    titled = [item for item in found if item[1]]
    return titled[0] if titled else found[0]


def client_region(hwnd: int) -> tuple[int, int, int, int]:
    """Клиентская область окна в координатах экрана: (left, top, width, height)."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    rect = wintypes.RECT()
    if not user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        raise RuntimeError("не удалось получить размер окна игры")
    point = wintypes.POINT(0, 0)
    user32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(point))
    return point.x, point.y, rect.right - rect.left, rect.bottom - rect.top

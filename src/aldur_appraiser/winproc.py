"""Работа с процессами Windows: жив ли процесс и как он называется.

Здесь одна реализация на весь проект: проверка «жив ли процесс по номеру»
нужна и оверлею (замена прежней копии), и обновителю каталога (замок
сканирования). Раньше она была скопирована в двух местах.

ВАЖНО: ``os.kill(pid, 0)`` на Windows НЕ проверка, а завершение процесса
(вызывает TerminateProcess). Для проверки используется OpenProcess.
"""

from __future__ import annotations

import ctypes


def process_alive(pid: int) -> bool:
    """Жив ли процесс с таким номером. На Windows — через OpenProcess."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if not hasattr(ctypes, "WinDLL"):
        # Не Windows: сигнал 0 в POSIX действительно только проверяет процесс.
        import os

        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # Отказано в доступе (5) — процесс есть, но открыть его нельзя.
        return ctypes.get_last_error() == 5
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def image_name(pid: int) -> str:
    """Полный путь к программе процесса (пусто, если узнать не вышло)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return ""
    if not hasattr(ctypes, "WinDLL"):
        return ""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            size = ctypes.c_uint(1024)
            buffer = ctypes.create_unicode_buffer(1024)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return buffer.value
            return ""
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return ""

"""Global keyboard hotkey on Windows.

Два способа, по порядку:

1. ``HookHotkey`` — низкоуровневый перехват клавиатуры (``WH_KEYBOARD_LL``).
   Он только подсматривает комбинацию и всегда пропускает её дальше, поэтому
   Shift+D продолжает работать в браузере и других играх. У него есть условие
   ``gate``: срабатывает только когда в фокусе окно игры.
2. ``WinHotkey`` — ``RegisterHotKey``. Забирает комбинацию у системы целиком,
   но работает всегда; остаётся запасным, если перехват не поставился.

Read-only: оба способа только сообщают о *нашем* нажатии, чужой ввод не читают.

Best-effort and isolated: any failure (non-Windows, combo already taken, parse
error) returns None so the caller falls back to the tray trigger.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from collections.abc import Callable


def _debug(msg: str) -> None:
    if os.environ.get("ALDUR_HOTKEY_DEBUG"):
        print(f"[win-hotkey] {msg}", file=sys.stderr)

# Win32 modifier flags for RegisterHotKey.
_MOD_ALT = 0x0001
_MOD_CONTROL = 0x0002
_MOD_SHIFT = 0x0004
_MOD_WIN = 0x0008
_MOD_NOREPEAT = 0x4000

_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012

_MOD_NAMES = {
    "control": _MOD_CONTROL, "primary": _MOD_CONTROL, "ctrl": _MOD_CONTROL,
    "alt": _MOD_ALT, "shift": _MOD_SHIFT, "super": _MOD_WIN, "logo": _MOD_WIN,
    "win": _MOD_WIN, "meta": _MOD_WIN,
}

# Virtual-key codes for non-printable keys we want to allow.
_VK_NAMED = {
    "space": 0x20, "tab": 0x09, "return": 0x0D, "enter": 0x0D, "escape": 0x1B,
    "esc": 0x1B, "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "up": 0x26, "down": 0x28, "left": 0x25,
    "right": 0x27,
}


def parse_accelerator(accel: str) -> tuple[int, int] | None:
    """Convert a GTK accelerator ("<Control><Alt>p") to (modifiers, vk) for
    RegisterHotKey, or None if it can't be parsed."""
    import re

    mods = 0
    for name in re.findall(r"<([^>]+)>", accel):
        flag = _MOD_NAMES.get(name.lower())
        if flag is None:
            return None
        mods |= flag
    key = re.sub(r"<[^>]+>", "", accel).strip()
    if not key:
        return None
    low = key.lower()
    if low in _VK_NAMED:
        vk = _VK_NAMED[low]
    elif len(low) == 2 and low[0] == "f" and low[1:].isdigit():
        vk = 0x70 + int(low[1:]) - 1  # F1..F12 -> 0x70..0x7B
    elif low[0] == "f" and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x70 + int(key[1:]) - 1
    elif len(key) == 1 and (key.isalnum()):
        vk = ord(key.upper())  # A-Z / 0-9 share their ASCII as the VK code
    else:
        return None
    # A bare key (no modifier) is allowed — the user may want e.g. just "0".
    return mods | _MOD_NOREPEAT, vk


class WinHotkey:
    """Owns a thread running a Win32 message loop that fires ``callback`` when
    the registered combo is pressed."""

    def __init__(self, callback: Callable[[], None], mods: int, vk: int):
        self._callback = callback
        self._mods = mods
        self._vk = vk
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._ok = False
        self._error = ""
        self._lock = threading.Lock()
        self._abandoned = False

    @property
    def error(self) -> str:
        """Empty if the hotkey registered, else a human-readable reason."""
        return self._error

    def start(self, *, timeout_s: float = 4.0) -> bool:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if self._ready.wait(timeout_s):
            with self._lock:
                self._abandoned = False
            return self._ok
        # Не дождались: поток может дорегистрировать клавишу уже после отказа,
        # и тогда клавиша оказалась бы занята без обработчика. Помечаем — поток
        # снимет регистрацию сам.
        with self._lock:
            self._abandoned = True
        return False

    def stop(self) -> None:
        if self._thread_id:
            import ctypes

            ctypes.windll.user32.PostThreadMessageW(self._thread_id, _WM_QUIT, 0, 0)

    def _run(self) -> None:
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

            # Pin the signatures so pointer/UINT args are the right width on x64.
            user32.RegisterHotKey.argtypes = [
                wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT,
            ]
            user32.RegisterHotKey.restype = wintypes.BOOL
            user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.GetMessageW.argtypes = [
                ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
            ]
            user32.GetMessageW.restype = ctypes.c_int

            self._thread_id = kernel32.GetCurrentThreadId()
            # id 1 is arbitrary but unique within this thread.
            if not user32.RegisterHotKey(None, 1, self._mods, self._vk):
                err = ctypes.get_last_error()
                # ERROR_HOTKEY_ALREADY_REGISTERED: такую клавишу уже занял кто-то
                # другой. Текст был на немецком — в русском оверлее это выглядело
                # как чужая ошибка, поэтому здесь по-русски.
                if err == 1409:
                    self._error = "клавиша уже занята другой программой"
                else:
                    self._error = (f"Windows не разрешила клавишу "
                                   f"(код ошибки {err})")
                _debug(self._error)
                self._ok = False
                self._ready.set()
                return
            _debug(f"registered mods=0x{self._mods:x} vk=0x{self._vk:x}")
            with self._lock:
                self._ok = True
                abandoned = self._abandoned
                self._ready.set()
            if abandoned:
                # Владелец уже отказался от клавиши — сразу освобождаем её.
                user32.UnregisterHotKey(None, 1)
                self._ok = False
                _debug("hotkey registered too late -> released")
                return

            msg = wintypes.MSG()
            while True:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret == 0 or ret == -1:  # WM_QUIT or error
                    break
                if msg.message == _WM_HOTKEY:
                    _debug("WM_HOTKEY -> firing callback")
                    try:
                        self._callback()
                    except Exception as exc:  # noqa: BLE001 - never kill the loop
                        _debug(f"callback raised: {exc!r}")
            user32.UnregisterHotKey(None, 1)
        except Exception as exc:  # noqa: BLE001 - any failure -> caller uses the tray
            self._error = f"{type(exc).__name__}: {exc}"
            _debug(self._error)
            self._ok = False
            self._ready.set()


# --- перехват, который НЕ отбирает клавишу у других программ --------------------
#
# RegisterHotKey выше забирает комбинацию у системы целиком: пока оверлей жив,
# Shift+D перестаёт работать в браузере и в других играх. Низкоуровневый
# перехват клавиатуры только подсматривает событие и всегда пропускает его
# дальше (CallNextHookEx), поэтому чужие программы получают комбинацию как
# обычно. Плюс у перехвата есть «условие» (gate): нажатие считается только
# тогда, когда в фокусе окно игры.

_WH_KEYBOARD_LL = 13
_HC_ACTION = 0
_WM_KEYDOWN = 0x0100
_WM_SYSKEYDOWN = 0x0104
_WM_KEYUP = 0x0101
_WM_SYSKEYUP = 0x0105

_VK_SHIFT = 0x10
_VK_CONTROL = 0x11
_VK_ALT = 0x12
_VK_LWIN = 0x5B
_VK_RWIN = 0x5C


class _KbdHookStruct(ctypes.Structure):
    # c_ulong вместо wintypes: модуль должен читаться и не на Windows, а
    # ctypes.wintypes там недоступен.
    _fields_ = [
        ("vkCode", ctypes.c_ulong),
        ("scanCode", ctypes.c_ulong),
        ("flags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class HookHotkey:
    """Слушает комбинацию системным перехватом и пропускает её дальше.

    ``gate`` вызывается перед срабатыванием: если вернёт False, нажатие
    игнорируется (но всё равно уходит в активное окно).
    """

    def __init__(self, callback: Callable[[], None], mods: int, vk: int,
                 gate: Callable[[], bool] | None = None):
        self._callback = callback
        self._mods = mods & 0x000F  # без _MOD_NOREPEAT: повтор гасим сами
        self._vk = vk
        self._gate = gate
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._ok = False
        self._error = ""
        self._lock = threading.Lock()
        self._abandoned = False
        self._fired = False

    @property
    def error(self) -> str:
        return self._error

    def start(self, *, timeout_s: float = 4.0) -> bool:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if self._ready.wait(timeout_s):
            with self._lock:
                self._abandoned = False
            return self._ok
        with self._lock:
            self._abandoned = True
        return False

    def stop(self) -> None:
        if self._thread_id:
            import ctypes

            ctypes.windll.user32.PostThreadMessageW(self._thread_id, _WM_QUIT, 0, 0)

    def _mods_now(self, user32) -> int:
        mods = 0
        if user32.GetAsyncKeyState(_VK_SHIFT) & 0x8000:
            mods |= _MOD_SHIFT
        if user32.GetAsyncKeyState(_VK_CONTROL) & 0x8000:
            mods |= _MOD_CONTROL
        if user32.GetAsyncKeyState(_VK_ALT) & 0x8000:
            mods |= _MOD_ALT
        if (user32.GetAsyncKeyState(_VK_LWIN) | user32.GetAsyncKeyState(_VK_RWIN)) & 0x8000:
            mods |= _MOD_WIN
        return mods

    def _run(self) -> None:
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

            user32.SetWindowsHookExW.argtypes = [
                ctypes.c_int, ctypes.c_void_p, wintypes.HINSTANCE, wintypes.DWORD,
            ]
            user32.SetWindowsHookExW.restype = wintypes.HHOOK
            user32.CallNextHookEx.argtypes = [
                wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM,
            ]
            user32.CallNextHookEx.restype = wintypes.LPARAM
            user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
            user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
            user32.GetAsyncKeyState.restype = ctypes.c_short
            user32.GetMessageW.argtypes = [
                ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
            ]
            user32.GetMessageW.restype = ctypes.c_int

            hookproc = ctypes.WINFUNCTYPE(
                wintypes.LPARAM, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

            def handler(code, wparam, lparam):
                try:
                    if code == _HC_ACTION:
                        info = ctypes.cast(
                            lparam, ctypes.POINTER(_KbdHookStruct)).contents
                        if info.vkCode == self._vk:
                            if wparam in (_WM_KEYDOWN, _WM_SYSKEYDOWN):
                                if not self._fired:
                                    self._fired = True
                                    if self._mods_now(user32) == self._mods:
                                        if self._gate is None or self._gate():
                                            self._callback()
                            elif wparam in (_WM_KEYUP, _WM_SYSKEYUP):
                                self._fired = False
                except Exception as exc:  # noqa: BLE001 — перехват не должен падать
                    _debug(f"hook handler: {exc!r}")
                # Нажатие всегда идёт дальше: чужие программы его получают.
                return user32.CallNextHookEx(None, code, wparam, lparam)

            self._proc = hookproc(handler)  # держим ссылку: иначе сборщик мусора
            self._thread_id = kernel32.GetCurrentThreadId()
            hook = user32.SetWindowsHookExW(_WH_KEYBOARD_LL, self._proc, None, 0)
            if not hook:
                err = ctypes.get_last_error()
                self._error = f"SetWindowsHookExW не вышло (Win32-ошибка {err})"
                _debug(self._error)
                self._ok = False
                self._ready.set()
                return
            _debug(f"перехват поставлен mods=0x{self._mods:x} vk=0x{self._vk:x}")
            with self._lock:
                self._ok = True
                abandoned = self._abandoned
                self._ready.set()
            if abandoned:
                user32.UnhookWindowsHookEx(hook)
                self._ok = False
                _debug("перехват поставлен слишком поздно -> снят")
                return

            msg = wintypes.MSG()
            while True:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret == 0 or ret == -1:  # WM_QUIT или ошибка
                    break
            user32.UnhookWindowsHookEx(hook)
        except Exception as exc:  # noqa: BLE001
            self._error = f"{type(exc).__name__}: {exc}"
            _debug(self._error)
            self._ok = False
            self._ready.set()


def start_global_hotkey(
    callback: Callable[[], None], *, accelerator: str,
    gate: Callable[[], bool] | None = None,
) -> WinHotkey | HookHotkey | None:
    """Слушать ``accelerator``; вернуть ручку, пока она нужна, или None.

    Сначала пробуем перехват (клавиша остаётся доступной другим программам),
    и только если он не поставился — обычную системную регистрацию.
    """
    if not sys.platform.startswith("win"):
        return None
    parsed = parse_accelerator(accelerator)
    if parsed is None:
        return None
    try:
        hook = HookHotkey(callback, *parsed, gate=gate)
        if hook.start():
            return hook
        _debug(f"перехват не вышел: {hook.error} — пробую RegisterHotKey")
    except Exception as exc:  # noqa: BLE001
        _debug(f"перехват упал: {exc!r}")
    try:
        hk = WinHotkey(callback, *parsed)
        return hk if hk.start() else None
    except Exception:  # noqa: BLE001
        return None

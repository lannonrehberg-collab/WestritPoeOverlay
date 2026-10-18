"""Global keyboard hotkey via the xdg-desktop-portal GlobalShortcuts interface.

On Wayland an app can't grab a global key while another window (the game) is
focused, so we register a shortcut with the compositor through the portal; KDE
then delivers an `Activated` signal when the user presses it (the key itself is
assigned once in System Settings → Shortcuts, or via the portal dialog).

Best-effort and isolated: any failure (no portal, old compositor, D-Bus error)
returns None so the caller falls back to a tray trigger. Read-only — we never
read raw input, the compositor only tells us our own shortcut fired.
"""

from __future__ import annotations

import os
import secrets
import threading
from collections.abc import Callable


def _debug(msg: str) -> None:
    if os.environ.get("ALDUR_HOTKEY_DEBUG"):
        import sys

        print(f"[hotkey] {msg}", file=sys.stderr)


_PORTAL = "org.freedesktop.portal.Desktop"
_PATH = "/org/freedesktop/portal/desktop"
_IFACE = "org.freedesktop.portal.GlobalShortcuts"
_SHORTCUT_ID = "appraise"


class GlobalHotkey:
    """Owns a GLib main loop (own thread) that keeps the portal session alive and
    fires `callback` whenever the registered shortcut is activated."""

    def __init__(self, callback: Callable[[], None], *, description: str, trigger: str):
        self._callback = callback
        self._description = description
        self._trigger = trigger
        self._loop = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._ok = False

    # --- public -------------------------------------------------------------

    def start(self, *, timeout_s: float = 8.0) -> bool:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(timeout_s)
        return self._ok

    def stop(self) -> None:
        if self._loop is not None:
            self._loop.quit()

    # --- worker thread ------------------------------------------------------

    def _run(self) -> None:
        try:
            # The venv has no PyGObject; reuse the capture backend's shim that
            # puts the distro's gi on sys.path.
            from aldur_appraiser.vision.portal_capture import _ensure_gi

            _ensure_gi()
            import gi

            gi.require_version("Gio", "2.0")
            from gi.repository import Gio, GLib

            self._Gio = Gio
            self._GLib = GLib
            self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            self._sender = self._bus.get_unique_name()[1:].replace(".", "_")
            self._loop = GLib.MainLoop()

            _debug("CreateSession")
            session = self._create_session()
            _debug(f"session={session}; BindShortcuts (trigger={self._trigger})")
            self._bind_shortcuts(session)
            self._bus.signal_subscribe(
                _PORTAL, _IFACE, "Activated", _PATH, None,
                Gio.DBusSignalFlags.NONE, self._on_activated,
            )
            _debug("registered; waiting for Activated")
            self._ok = True
        except Exception as exc:  # noqa: BLE001 - any failure -> caller uses the tray
            _debug(f"failed: {type(exc).__name__}: {exc}")
            self._ok = False
            self._ready.set()
            return
        self._ready.set()
        self._loop.run()  # block here until stop()

    def _on_activated(self, _conn, _sender, _path, _iface, _signal, params):
        # Activated(session_handle:o, shortcut_id:s, timestamp:t, options:a{sv})
        _debug("Activated signal received")
        try:
            shortcut_id = params.unpack()[1]
        except Exception:  # noqa: BLE001
            shortcut_id = _SHORTCUT_ID
        if shortcut_id == _SHORTCUT_ID:
            self._callback()

    # --- portal request/response -------------------------------------------

    def _request(self, method: str, body, timeout_s: int = 60) -> dict:
        GLib, Gio = self._GLib, self._Gio
        token = "aldur_" + secrets.token_hex(8)
        req_path = f"/org/freedesktop/portal/desktop/request/{self._sender}/{token}"
        loop = GLib.MainLoop()
        out: dict = {}

        def on_response(_c, _s, _p, _i, _sig, params):
            code, results = params.unpack()
            out["code"], out["results"] = code, results
            loop.quit()

        sub = self._bus.signal_subscribe(
            _PORTAL, "org.freedesktop.portal.Request", "Response", req_path, None,
            Gio.DBusSignalFlags.NONE, on_response,
        )
        try:
            self._bus.call_sync(
                _PORTAL, _PATH, _IFACE, method, body(token),
                None, Gio.DBusCallFlags.NONE, -1, None,
            )
            GLib.timeout_add_seconds(timeout_s, loop.quit)
            loop.run()
        finally:
            self._bus.signal_unsubscribe(sub)
        if out.get("code") != 0:
            raise RuntimeError(f"{method}: portal response {out.get('code')}")
        return out["results"]

    def _create_session(self) -> str:
        GLib = self._GLib

        def body(handle_token: str):
            opts = {
                "handle_token": GLib.Variant("s", handle_token),
                "session_handle_token": GLib.Variant("s", "aldur_" + secrets.token_hex(8)),
            }
            return GLib.Variant("(a{sv})", (opts,))

        return self._request("CreateSession", body)["session_handle"]

    def _bind_shortcuts(self, session_handle: str) -> None:
        GLib = self._GLib

        def body(handle_token: str):
            shortcut = (
                _SHORTCUT_ID,
                {
                    "description": GLib.Variant("s", self._description),
                    "preferred_trigger": GLib.Variant("s", self._trigger),
                },
            )
            opts = {"handle_token": GLib.Variant("s", handle_token)}
            return GLib.Variant(
                "(oa(sa{sv})sa{sv})", (session_handle, [shortcut], "", opts)
            )

        self._request("BindShortcuts", body)


def start_global_hotkey(
    callback: Callable[[], None],
    *,
    description: str = "Aldur: appraise the reward panel",
    trigger: str = "CTRL+ALT+p",
) -> GlobalHotkey | None:
    """Register the shortcut; returns a handle to keep alive, or None if the
    portal isn't usable (caller should fall back to a tray trigger)."""
    try:
        hk = GlobalHotkey(callback, description=description, trigger=trigger)
        return hk if hk.start() else None
    except Exception:  # noqa: BLE001
        return None

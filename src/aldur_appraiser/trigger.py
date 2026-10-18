"""Tiny IPC so a desktop keyboard shortcut can poke a running `appraiser run`.

`appraiser run` listens on a Unix socket; `appraiser trigger` connects to it to
request one on-demand appraisal. This is the GNOME-friendly path — bind a custom
keyboard shortcut to `appraiser trigger` — and avoids the GlobalShortcuts portal,
which GNOME/Mutter only supports unreliably. stdlib only, no heavy imports.
"""

from __future__ import annotations

import os
import socket
import tempfile
import threading
from collections.abc import Callable


def socket_path() -> str:
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return os.path.join(base, "aldur-appraiser-trigger.sock")


def serve(on_trigger: Callable[[], None]) -> socket.socket | None:
    """Listen for trigger pokes in a daemon thread; each connection fires
    `on_trigger`. Returns the server socket (keep it referenced), or None if it
    couldn't bind."""
    path = socket_path()
    try:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen()
    except OSError:
        return None

    def loop() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                break
            conn.close()
            on_trigger()

    threading.Thread(target=loop, daemon=True).start()
    return srv


def send() -> bool:
    """Poke a running listener. True if delivered, False if nothing is listening."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(socket_path())
        return True
    except OSError:
        return False

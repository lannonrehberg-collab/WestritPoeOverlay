"""Bind the appraise hotkey at the desktop level (GNOME).

GNOME/Mutter doesn't let an app grab a global key, but it can write a custom
keyboard shortcut into GNOME's settings (via gsettings) that runs
`appraiser trigger` on the chosen key. Best-effort: returns False on non-GNOME or
any failure, so the caller can fall back to manual setup.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import sys

_GNOME_ID = "org.gnome.settings-daemon.plugins.media-keys"
_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/aldur-appraiser/"


def trigger_command() -> str:
    """Shell command a desktop shortcut should run to poke a running overlay."""
    if os.path.exists("/.flatpak-info") or os.environ.get("FLATPAK_ID"):
        return "flatpak run --command=appraiser io.github.t0nd3.AldurAppraiser trigger"
    exe = sys.argv[0] or ""
    if exe and os.path.basename(exe).startswith("appraiser"):
        exe = os.path.abspath(exe)
        if os.path.exists(exe):
            return f"{shlex.quote(exe)} trigger"
    found = shutil.which("appraiser")
    if found:
        return f"{shlex.quote(found)} trigger"
    return f"{shlex.quote(sys.executable)} -m aldur_appraiser trigger"


def accel_to_portal(accel: str) -> str:
    """Convert a GTK accelerator ("<Control><Alt>p") to the GlobalShortcuts portal
    trigger syntax ("CTRL+ALT+p")."""
    mods = re.findall(r"<([^>]+)>", accel)
    key = re.sub(r"<[^>]+>", "", accel).strip()
    table = {"control": "CTRL", "primary": "CTRL", "ctrl": "CTRL",
             "alt": "ALT", "shift": "SHIFT", "super": "LOGO"}
    return "+".join([table.get(m.lower(), m.upper()) for m in mods] + [key])


def bind_gnome_shortcut(accelerator: str, *, command: str | None = None) -> bool:
    """Register/refresh a GNOME custom keybinding for `accelerator`. Returns True
    on success, False if GNOME's schema isn't present or anything fails."""
    try:
        from aldur_appraiser.vision.portal_capture import _ensure_gi

        _ensure_gi()
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio
    except Exception:  # noqa: BLE001 - no PyGObject -> not GNOME-bindable here
        return False

    src = Gio.SettingsSchemaSource.get_default()
    if src is None or src.lookup(_GNOME_ID, True) is None or src.lookup(
        _GNOME_ID + ".custom-keybinding", True
    ) is None:
        return False  # not GNOME (schema absent)
    try:
        base = Gio.Settings.new(_GNOME_ID)
        paths = list(base.get_strv("custom-keybindings"))
        if _PATH not in paths:
            paths.append(_PATH)
            base.set_strv("custom-keybindings", paths)
        cb = Gio.Settings.new_with_path(_GNOME_ID + ".custom-keybinding", _PATH)
        cb.set_string("name", "Aldur: appraise reward panel")
        cb.set_string("command", command or trigger_command())
        cb.set_string("binding", accelerator)
        Gio.Settings.sync()
        return True
    except Exception:  # noqa: BLE001 - best effort
        return False

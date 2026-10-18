"""Tiny JSON settings store (user preferences like the appraise hotkey)."""

from __future__ import annotations

import json

from aldur_appraiser.config import config_dir

DEFAULT_HOTKEY = "<Control><Alt>p"  # GNOME/GTK accelerator syntax


def _path():
    return config_dir() / "settings.json"


def load() -> dict:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def get(key: str, default=None):
    return load().get(key, default)


def set(key: str, value) -> None:  # noqa: A001 - mirror dict.set naming
    data = load()
    data[key] = value
    try:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass

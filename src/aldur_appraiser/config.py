"""Configuration loading and cross-platform paths.

The pricing/OCR halves are platform-independent; only capture/overlay are
OS-specific. Paths go through platformdirs so the tool behaves on both
Windows and Linux (Bazzite) without hard-coded locations.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from platformdirs import user_cache_dir, user_config_dir

from aldur_appraiser.resources import resource_path

APP_NAME = "aldur-appraiser"

# Bundled default config (repo tree, or the PyInstaller bundle when frozen).
_REPO_CONFIG = resource_path("config.toml")


def cache_dir() -> Path:
    d = Path(user_cache_dir(APP_NAME))
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_dir() -> Path:
    d = Path(user_config_dir(APP_NAME))
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass(frozen=True)
class PricingConfig:
    realm: str = "poe2"
    league: str = "Runes of Aldur"
    base: str = "exalted"
    categories: tuple[str, ...] = ("currency",)
    cache_ttl_minutes: int = 20
    source: str = "poe2scout"


@dataclass(frozen=True)
class AppConfig:
    pricing: PricingConfig = field(default_factory=PricingConfig)
    raw: dict = field(default_factory=dict)


def _config_path() -> Path:
    """Prefer a user config file, fall back to the repo-bundled default."""
    user = config_dir() / "config.toml"
    return user if user.exists() else _REPO_CONFIG


def load_config(path: Path | None = None) -> AppConfig:
    cfg_path = path or _config_path()
    data: dict = {}
    if cfg_path.exists():
        with cfg_path.open("rb") as fh:
            data = tomllib.load(fh)

    p = data.get("pricing", {})
    pricing = PricingConfig(
        realm=p.get("realm", "poe2"),
        league=p.get("league", "Runes of Aldur"),
        base=p.get("base", "exalted"),
        categories=tuple(p.get("categories", [])),  # empty = all currency categories
        cache_ttl_minutes=int(p.get("cache_ttl_minutes", 20)),
        source=p.get("source", "poe2scout"),
    )
    return AppConfig(pricing=pricing, raw=data)

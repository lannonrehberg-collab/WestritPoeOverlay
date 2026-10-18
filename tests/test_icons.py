"""Tests for the base-currency icon download/cache (inline overlay)."""

from __future__ import annotations

import httpx

from aldur_appraiser import icons


def test_base_icon_downloads_once_and_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(icons, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(icons, "fetch_base_icon_url", lambda realm, league: "http://x/icon.png")
    calls = {"n": 0}

    def fake_get(url, **kw):
        calls["n"] += 1
        return httpx.Response(
            200, content=b"\x89PNG fake image bytes", request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(icons.httpx, "get", fake_get)

    p1 = icons.base_icon_path("poe2", "L")
    p2 = icons.base_icon_path("poe2", "L")

    assert p1 is not None and p1.exists()
    assert p1.read_bytes().startswith(b"\x89PNG")
    assert p1 == p2 and calls["n"] == 1  # second call served from disk


def test_base_icon_none_when_no_url(tmp_path, monkeypatch):
    monkeypatch.setattr(icons, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(icons, "fetch_base_icon_url", lambda realm, league: None)
    assert icons.base_icon_path("poe2", "L") is None


def test_base_icon_none_on_download_error(tmp_path, monkeypatch):
    monkeypatch.setattr(icons, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(icons, "fetch_base_icon_url", lambda realm, league: "http://x/icon.png")

    def boom(url, **kw):
        raise httpx.ConnectError("no net")

    monkeypatch.setattr(icons.httpx, "get", boom)
    assert icons.base_icon_path("poe2", "L") is None

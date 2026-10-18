"""Tests for the GitHub release update check (network mocked)."""

from __future__ import annotations

import httpx

from aldur_appraiser import updates


def test_parse_and_is_newer():
    assert updates.is_newer("0.1.1", "0.1.0")
    assert updates.is_newer("v0.2.0", "0.1.9")
    assert not updates.is_newer("0.1.0", "0.1.0")
    assert not updates.is_newer("0.1.0", "0.2.0")
    # tolerant of prefixes/suffixes
    assert updates.is_newer("v1.0.0-beta", "0.9.0")


def _patch_get(monkeypatch, *, tag=None, fail=False):
    def fake_get(url, **kw):
        if fail:
            raise httpx.ConnectError("no net")
        return httpx.Response(200, json={"tag_name": tag}, request=httpx.Request("GET", url))

    monkeypatch.setattr(updates.httpx, "get", fake_get)


def test_latest_version_parses_tag(monkeypatch):
    _patch_get(monkeypatch, tag="v0.3.0")
    assert updates.latest_version() == "0.3.0"


def test_latest_version_none_on_error(monkeypatch):
    _patch_get(monkeypatch, fail=True)
    assert updates.latest_version() is None


def test_newer_release(monkeypatch):
    _patch_get(monkeypatch, tag="v9.9.9")
    assert updates.newer_release("0.1.0") == "9.9.9"
    _patch_get(monkeypatch, tag="v0.0.1")
    assert updates.newer_release("0.1.0") is None

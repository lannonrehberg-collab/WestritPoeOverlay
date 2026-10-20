"""The on-demand trigger IPC (a desktop shortcut pokes a running overlay)."""

from __future__ import annotations

import socket
import time

import pytest

from aldur_appraiser import trigger

# Сокет AF_UNIX есть не в каждой сборке Windows: без него проверка IPC
# невозможна, и раньше весь набор падал с AttributeError вместо пропуска.
pytestmark = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"),
                                reason="в этой сборке Python нет AF_UNIX")


def test_serve_and_send_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert trigger.send() is False              # nothing is listening yet
    hits: list[int] = []
    srv = trigger.serve(lambda: hits.append(1))
    assert srv is not None
    try:
        assert trigger.send() is True
        for _ in range(50):                     # the callback runs on a worker thread
            if hits:
                break
            time.sleep(0.01)
        assert hits == [1]
    finally:
        srv.close()

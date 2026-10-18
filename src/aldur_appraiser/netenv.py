"""Сеть оверлея: никаких прокси из окружения.

Зачем отдельный модуль: однажды запросы уже падали с
``httpx.InvalidURL: Invalid port: ':1]'`` — виновата была переменная NO_PROXY
в окружении (там оказалась запись вида ``[::1]``). Приложение не должно
зависеть от чужих настроек прокси: их в оверлее нет, а сломанная переменная
окружения не должна ломать загрузку цен.

Поэтому все сетевые обращения идут через этот модуль: у каждого клиента
жёстко выключено чтение окружения (``trust_env=False``), а на старте процесса
переменные прокси ещё и удаляются из окружения, чтобы их не унаследовали
дочерние процессы (обновитель каталога запускается как отдельный процесс).
"""

from __future__ import annotations

import os

import httpx

# Полный список переменных, которыми httpx (и не только) узнаёт про прокси.
PROXY_ENV_VARS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "ftp_proxy",
    "NO_PROXY", "no_proxy",
)


def drop_proxy_env() -> list[str]:
    """Убирает переменные прокси из окружения процесса. -> что убрали."""
    removed = [name for name in PROXY_ENV_VARS if name in os.environ]
    for name in removed:
        os.environ.pop(name, None)
    return removed


def client(**kwargs) -> httpx.Client:
    """Клиент без опоры на окружение: прокси и прочие настройки среды не читаются."""
    kwargs.setdefault("follow_redirects", True)
    return httpx.Client(trust_env=False, **kwargs)


def get(url: str, **kwargs) -> httpx.Response:
    """Одиночный запрос без опоры на окружение."""
    return httpx.get(url, trust_env=False, **kwargs)

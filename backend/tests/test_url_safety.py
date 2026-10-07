import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from librarysync.core import url_safety
from librarysync.core.url_safety import UnsafeUrlError, ensure_public_host, ensure_public_url


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "10.1.2.3", "192.168.1.10", "169.254.169.254", "100.100.1.1", "::1", "localhost", "db.localhost"],
)
def test_internal_hosts_are_rejected(host: str) -> None:
    with pytest.raises(UnsafeUrlError):
        asyncio.run(ensure_public_host(host))


def test_hostname_resolving_to_private_address_is_rejected() -> None:
    async def _getaddrinfo(*_args, **_kwargs):
        return [(None, None, None, None, ("10.0.0.5", 0))]

    async def _check():
        with patch.object(asyncio.get_running_loop(), "getaddrinfo", _getaddrinfo):
            await ensure_public_host("internal.example")

    with pytest.raises(UnsafeUrlError, match="not allowed"):
        asyncio.run(_check())


def test_unresolvable_host_fails_closed() -> None:
    with pytest.raises(UnsafeUrlError, match="could not be resolved"):
        asyncio.run(ensure_public_host("does-not-exist.invalid"))


def test_public_address_is_allowed() -> None:
    asyncio.run(ensure_public_host("93.184.216.34"))


def test_private_urls_can_be_allowed_explicitly(monkeypatch) -> None:
    monkeypatch.setattr(url_safety, "settings", SimpleNamespace(allow_private_urls=True))
    asyncio.run(ensure_public_host("192.168.1.10"))


@pytest.mark.parametrize(
    "url", ["ftp://93.184.216.34/x", "https://user:pass@93.184.216.34/", "http://93.184.216.34/"]
)
def test_url_scheme_and_credentials_are_checked(url: str) -> None:
    with pytest.raises(UnsafeUrlError):
        asyncio.run(ensure_public_url(url, schemes=("https",)))

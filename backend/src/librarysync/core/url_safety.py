"""Guards for server-side requests to user-supplied URLs (SSRF)."""

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

from librarysync.config import settings


class UnsafeUrlError(ValueError):
    pass


def _is_public_address(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    # is_global excludes private, loopback, link-local, reserved and shared
    # (100.64.0.0/10, used by CGNAT and Tailscale) ranges.
    return ip.is_global and not ip.is_multicast


async def ensure_public_host(hostname: str | None, *, label: str = "URL") -> None:
    """Reject hosts that are, or resolve to, non-public addresses.

    Self-hosters who deliberately point at LAN services can opt out with
    LIBRARYSYNC_ALLOW_PRIVATE_URLS=true.
    """
    if not hostname:
        raise UnsafeUrlError(f"{label} host is required")
    if settings.allow_private_urls:
        return
    host = hostname.strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise UnsafeUrlError(f"{label} host is not allowed")
    try:
        if not _is_public_address(host):
            raise UnsafeUrlError(f"{label} host is not allowed")
        return
    except ValueError as exc:
        if isinstance(exc, UnsafeUrlError):
            raise
    try:
        resolved = await asyncio.get_running_loop().getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"{label} host could not be resolved") from exc
    if not resolved:
        raise UnsafeUrlError(f"{label} host could not be resolved")
    for entry in resolved:
        if not _is_public_address(str(entry[4][0])):
            raise UnsafeUrlError(f"{label} host is not allowed")


async def ensure_public_url(url: str, *, label: str = "URL", schemes: tuple[str, ...] = ("https", "http")) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in schemes:
        raise UnsafeUrlError(f"{label} must use {' or '.join(schemes)}")
    if parsed.username or parsed.password:
        raise UnsafeUrlError(f"{label} must not contain credentials")
    await ensure_public_host(parsed.hostname, label=label)

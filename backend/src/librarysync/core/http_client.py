"""
Centralized HTTP client utilities for librarySync.

Provides a consistent HTTP client factory that automatically sets
the User-Agent header to "librarySync Version/<version>" for all
outbound HTTP requests.
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
from importlib import metadata

import httpx

# Daily quotas reset within a day. The bound only stops a nonsense header from parking work for good.
MAX_RETRY_AFTER = timedelta(days=1)

_SECRET_QUERY_RE = re.compile(
    r"(?i)\b(api_key|apikey|access_token|refresh_token|client_secret|token|password)=([^&\s'\"]+)"
)


def redact_secrets(text: str) -> str:
    """Mask credentials passed as URL query parameters (e.g. in httpx error messages)."""
    return _SECRET_QUERY_RE.sub(r"\1=REDACTED", text)


def retry_after_seconds(response: httpx.Response) -> float | None:
    """The wait a response asks for in Retry-After, given as seconds or as an HTTP date, up to MAX_RETRY_AFTER."""
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
    # Bounded here, so callers can add it to a timestamp without overflowing.
    return min(max(seconds, 0.0), MAX_RETRY_AFTER.total_seconds())


def quiet_http_request_logging() -> None:
    """httpx logs every request URL at INFO, including keys some APIs require in the query."""
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


@lru_cache(maxsize=1)
def get_app_version() -> str:
    """Get the application version from package metadata."""
    try:
        return metadata.version("librarysync")
    except metadata.PackageNotFoundError:
        return "unknown"


def get_http_client(
    *,
    base_url: str | None = None,
    timeout: float = 15.0,
    headers: dict[str, str] | None = None,
    **kwargs,
) -> httpx.AsyncClient:
    """
    Create an HTTP client with librarySync User-Agent.

    Args:
        base_url: Optional base URL for all requests
        timeout: Request timeout in seconds (default: 15.0)
        headers: Additional headers to include (merged with default headers)
        **kwargs: Additional arguments passed to httpx.AsyncClient

    Returns:
        Configured httpx.AsyncClient with User-Agent header set
    """
    version = get_app_version()
    user_agent = f"librarySync Version/{version}"

    default_headers = {"User-Agent": user_agent}

    # Merge additional headers if provided
    if headers:
        default_headers.update(headers)

    client_kwargs = {"timeout": timeout, "headers": default_headers, **kwargs}
    if base_url is not None:
        client_kwargs["base_url"] = base_url

    return httpx.AsyncClient(**client_kwargs)

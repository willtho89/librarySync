"""
Centralized HTTP client utilities for librarySync.

Provides a consistent HTTP client factory that automatically sets
the User-Agent header to "librarySync Version/<version>" for all
outbound HTTP requests.
"""

from importlib import metadata

import httpx


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

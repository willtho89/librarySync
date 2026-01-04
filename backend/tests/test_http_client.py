"""Tests for centralized HTTP client with User-Agent header."""

import asyncio
import re
from importlib import metadata
from unittest.mock import patch

import httpx

from librarysync.core.http_client import get_app_version, get_http_client


def test_get_app_version_returns_version():
    """Test that get_app_version returns a valid version string."""
    version = get_app_version()
    assert version is not None
    # Should be either a valid version (X.Y.Z) or "unknown"
    assert version == "unknown" or re.match(r"\d+\.\d+\.\d+", version)


def test_get_app_version_handles_missing_package():
    """Test that get_app_version returns 'unknown' when package is not found."""
    with patch("librarysync.core.http_client.metadata.version") as mock_version:
        mock_version.side_effect = metadata.PackageNotFoundError
        version = get_app_version()
        assert version == "unknown"


def test_get_http_client_sets_user_agent() -> None:
    """Test that get_http_client sets the correct User-Agent header."""
    version = get_app_version()
    expected_user_agent = f"librarySync Version/{version}"

    async def run_test():
        async with get_http_client() as client:
            assert "User-Agent" in client.headers
            assert client.headers["User-Agent"] == expected_user_agent

    asyncio.run(run_test())


def test_get_http_client_with_base_url() -> None:
    """Test that get_http_client correctly sets base_url."""
    base_url = "https://api.example.com"

    async def run_test():
        async with get_http_client(base_url=base_url) as client:
            assert client.base_url == httpx.URL(base_url)

    asyncio.run(run_test())


def test_get_http_client_with_timeout() -> None:
    """Test that get_http_client correctly sets timeout."""
    timeout = 30.0

    async def run_test():
        async with get_http_client(timeout=timeout) as client:
            assert client.timeout.connect == timeout

    asyncio.run(run_test())


def test_get_http_client_merges_headers() -> None:
    """Test that get_http_client merges additional headers with User-Agent."""
    version = get_app_version()
    expected_user_agent = f"librarySync Version/{version}"
    additional_headers = {
        "Authorization": "Bearer token123",
        "Content-Type": "application/json",
    }

    async def run_test():
        async with get_http_client(headers=additional_headers) as client:
            # User-Agent should be set
            assert client.headers["User-Agent"] == expected_user_agent
            # Additional headers should also be present
            assert client.headers["Authorization"] == "Bearer token123"
            assert client.headers["Content-Type"] == "application/json"

    asyncio.run(run_test())


def test_get_http_client_additional_headers_override_user_agent() -> None:
    """Test that explicitly provided User-Agent overrides the default."""
    custom_user_agent = "CustomAgent/1.0"
    headers = {"User-Agent": custom_user_agent}

    async def run_test():
        async with get_http_client(headers=headers) as client:
            # Custom User-Agent should override the default
            assert client.headers["User-Agent"] == custom_user_agent

    asyncio.run(run_test())


def test_get_http_client_user_agent_format() -> None:
    """Test that User-Agent follows the expected format."""

    async def run_test():
        async with get_http_client() as client:
            user_agent = client.headers["User-Agent"]
            # Should match "librarySync Version/X.Y.Z" or "librarySync Version/unknown"
            assert user_agent.startswith("librarySync Version/")
            version_part = user_agent.replace("librarySync Version/", "")
            assert version_part == "unknown" or re.match(r"\d+\.\d+\.\d+", version_part)

    asyncio.run(run_test())

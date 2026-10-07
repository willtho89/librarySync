"""Guard the service worker precache list against stale or missing entries.

``cache.addAll``-style precaching aborts the whole install on a single 404, so every
asset listed in ``CORE_ASSETS`` must exist and every page route must render.
"""

import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

STATIC_DIR = PROJECT_ROOT / "src" / "librarysync" / "static"
SERVICE_WORKER = STATIC_DIR / "service-worker.js"


def _parse_string_array(name: str) -> list[str]:
    source = SERVICE_WORKER.read_text(encoding="utf-8")
    match = re.search(rf"const {name} = \[(.*?)\];", source, re.DOTALL)
    assert match, f"{name} array not found in service-worker.js"
    body = match.group(1)
    entries = re.findall(r'"([^"]+)"', body)
    leftovers = re.sub(r'"[^"]+"', "", body)
    assert not re.search(r"[A-Za-z_]", leftovers), f"{name} must only contain string literals"
    return entries


CORE_ASSETS = _parse_string_array("CORE_ASSETS")
PAGE_ROUTES = _parse_string_array("PAGE_ROUTES")
STATIC_ASSETS = [path for path in CORE_ASSETS if path.startswith("/static/")]
ROUTED_ASSETS = [path for path in CORE_ASSETS if not path.startswith("/static/")]


@pytest.fixture(autouse=True)
def mock_lifespan():
    with (
        patch("librarysync.main.run_migrations"),
        patch("librarysync.main.validate_security_settings"),
        patch("librarysync.main.init_session_factory"),
    ):
        yield


@pytest.fixture
def client():
    from librarysync.api import deps
    from librarysync.main import create_app

    app = create_app()

    mock_result = MagicMock()
    mock_result.scalar_one = MagicMock(return_value=0)

    async def mock_get_db():
        mock_session = MagicMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        yield mock_session

    async def mock_get_optional_user(db=None):
        return None

    app.dependency_overrides[deps.get_db] = mock_get_db
    app.dependency_overrides[deps.get_optional_user] = mock_get_optional_user

    with TestClient(app, raise_server_exceptions=True) as test_client:
        yield test_client


def test_core_assets_are_unique():
    assert len(CORE_ASSETS) == len(set(CORE_ASSETS))


def test_page_routes_are_precached():
    assert set(PAGE_ROUTES) <= set(CORE_ASSETS)
    assert "/offline" in PAGE_ROUTES


@pytest.mark.parametrize("path", STATIC_ASSETS)
def test_static_asset_exists_on_disk(path):
    relative = path.removeprefix("/static/")
    assert (STATIC_DIR / relative).is_file(), f"{path} is precached but missing from static/"


@pytest.mark.parametrize("path", ROUTED_ASSETS)
def test_routed_asset_resolves(client, path):
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 200, f"{path} returned {response.status_code}"


@pytest.mark.parametrize("path", PAGE_ROUTES)
def test_page_route_renders_html(client, path):
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")


def test_page_scripts_are_precached():
    page_scripts = {f"/static/{script.name}" for script in STATIC_DIR.glob("page-*.js")}
    assert page_scripts <= set(STATIC_ASSETS), (
        f"page scripts missing from CORE_ASSETS: {sorted(page_scripts - set(STATIC_ASSETS))}"
    )


def test_service_worker_served_from_root(client):
    response = client.get("/service-worker.js?v=test")
    assert response.status_code == 200
    assert "javascript" in response.headers.get("content-type", "")
    assert response.headers.get("cache-control") == "no-cache"
    assert "CORE_ASSETS" in response.text

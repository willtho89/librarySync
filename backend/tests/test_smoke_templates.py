import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from starlette.requests import Request
from starlette.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


@pytest.fixture(autouse=True)
def mock_lifespan():
    with (
        patch("librarysync.main.run_migrations"),
        patch("librarysync.main.init_session_factory"),
    ):
        yield


def create_test_app():
    from librarysync.main import create_app
    from librarysync.api import deps
    from unittest.mock import MagicMock

    app = create_app()

    mock_result = MagicMock()
    mock_result.scalar_one = MagicMock(return_value=0)

    async def mock_get_db():
        mock_session = MagicMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.commit = AsyncMock()
        mock_session.rollback = AsyncMock()
        yield mock_session

    async def mock_get_optional_user(db=None):
        return None

    async def mock_get_current_user(db=None):
        from fastapi import HTTPException, status

        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    app.dependency_overrides[deps.get_db] = mock_get_db
    app.dependency_overrides[deps.get_optional_user] = mock_get_optional_user
    app.dependency_overrides[deps.get_current_user] = mock_get_current_user

    return app


@pytest.fixture
def client():
    app = create_test_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        yield client


class TestAppInitialization:
    def test_app_creates_successfully(self):
        from librarysync.main import create_app

        app = create_app()
        assert app is not None
        assert len(app.routes) > 0


class TestWebPageRoutes:
    @pytest.mark.parametrize(
        "path",
        [
            "/",
            "/history",
            "/watchlist",
            "/add-watched",
            "/settings",
            "/login",
            "/offline",
            "/stremio-addon",
        ],
    )
    def test_page_renders(self, client, path):
        response = client.get(path)
        assert response.status_code == 200, f"Page {path} failed: {response.text}"
        assert "text/html" in response.headers.get("content-type", "")

    def test_favicon_routes(self, client):
        for icon_path in ["/favicon.ico", "/favicon.svg", "/apple-touch-icon.png"]:
            response = client.get(icon_path)
            assert response.status_code == 200


class TestTemplateResponseSignature:
    def test_render_page_signature(self, client, monkeypatch):
        from starlette.templating import Jinja2Templates

        original_response = Jinja2Templates.TemplateResponse
        call_args = []

        def capture_signature(self, request, name, context=None, **kwargs):
            call_args.append((request, name, context))
            return original_response(self, request, name, context, **kwargs)

        monkeypatch.setattr(Jinja2Templates, "TemplateResponse", capture_signature)

        response = client.get("/")
        assert response.status_code == 200

        assert len(call_args) > 0, "TemplateResponse was never called"
        request, name, context = call_args[0]

        assert isinstance(request, Request), f"First arg should be Request, got {type(request)}"
        assert isinstance(name, str), f"Second arg should be template name (str), got {type(name)}"
        assert isinstance(context, dict) or context is None, (
            f"Third arg should be context (dict), got {type(context)}"
        )

    def test_no_type_error_from_dict_key(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert "cannot use 'tuple' as a dict key" not in response.text
        assert "TypeError" not in response.text or "401" not in response.text


class TestStaticAssets:
    def test_static_assets_accessible(self, client):
        response = client.get("/static/styles.css")
        assert response.status_code == 200


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

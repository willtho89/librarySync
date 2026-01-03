from importlib import metadata
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from librarysync.api import (
    routes_activity,
    routes_admin,
    routes_auth,
    routes_history,
    routes_integrations,
    routes_metadata,
    routes_settings,
)
from librarysync.api.deps import get_optional_user
from librarysync.config import settings
from librarysync.db.migrate import run_migrations
from librarysync.db.models import User
from librarysync.db.session import init_session_factory

STATIC_DIR = Path(__file__).resolve().parent / "static"
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_CACHE_LONG = 60 * 60 * 24 * 30
STATIC_CACHE_MEDIUM = 60 * 60 * 24 * 7
STATIC_CACHE_DEFAULT = 60 * 60

OPENAPI_TAGS = [
    {"name": "auth", "description": "User registration and authentication."},
    {
        "name": "integrations",
        "description": "Configure player and downstream service integrations.",
    },
    {"name": "metadata", "description": "Metadata providers and lookup workflows."},
    {"name": "history", "description": "Manual watched history operations."},
    {"name": "activity", "description": "Progress events, outbox, and status feeds."},
    {"name": "settings", "description": "Per-user search settings."},
    {"name": "admin", "description": "Administrative operations."},
    {"name": "health", "description": "Service health check."},
]


def get_app_version() -> str:
    """Get the application version from package metadata."""
    try:
        return metadata.version("librarysync")
    except metadata.PackageNotFoundError:
        return "unknown"


def make_static_url(version: str) -> callable:
    """Create a static_url function with the given version."""

    def static_url(path: str) -> str:
        """Generate a versioned static URL for cache busting."""
        return f"{path}?v={version}"

    return static_url


def create_app() -> FastAPI:
    app = FastAPI(
        title="librarySync",
        description=(
            "Authenticate with `Authorization: Bearer <token>` or the "
            "`access_token` cookie set by `/api/auth/login`."
        ),
        openapi_tags=OPENAPI_TAGS,
    )
    if settings.gzip_enabled:
        app.add_middleware(GZipMiddleware, minimum_size=settings.gzip_min_size)

    app.include_router(routes_auth.router)
    app.include_router(routes_integrations.router)
    app.include_router(routes_metadata.router)
    app.include_router(routes_history.router)
    app.include_router(routes_activity.router)
    app.include_router(routes_settings.router)
    app.include_router(routes_admin.router)

    app_version = get_app_version()
    templates = Jinja2Templates(directory=TEMPLATES_DIR)
    templates.env.globals["static_url"] = make_static_url(app_version)

    class CachedStaticFiles(StaticFiles):
        async def get_response(self, path: str, scope):
            response = await super().get_response(path, scope)
            if response.status_code != 200 or "cache-control" in response.headers:
                return response
            suffix = Path(path).suffix.lower()
            if path.endswith("service-worker.js"):
                response.headers["Cache-Control"] = "no-cache"
                return response
            if suffix in {
                ".woff2",
                ".woff",
                ".ttf",
                ".otf",
                ".png",
                ".jpg",
                ".jpeg",
                ".gif",
                ".webp",
                ".svg",
                ".ico",
            }:
                response.headers["Cache-Control"] = f"public, max-age={STATIC_CACHE_LONG}"
                return response
            if suffix in {".css", ".js", ".webmanifest"}:
                response.headers["Cache-Control"] = f"public, max-age={STATIC_CACHE_MEDIUM}"
                return response
            response.headers["Cache-Control"] = f"public, max-age={STATIC_CACHE_DEFAULT}"
            return response

    app.mount("/static", CachedStaticFiles(directory=STATIC_DIR), name="static")

    def _static_asset(filename: str) -> FileResponse:
        response = FileResponse(STATIC_DIR / filename)
        suffix = Path(filename).suffix.lower()
        if suffix in {
            ".woff2",
            ".woff",
            ".ttf",
            ".otf",
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".webp",
            ".svg",
            ".ico",
        }:
            response.headers["Cache-Control"] = f"public, max-age={STATIC_CACHE_LONG}"
        elif suffix in {".css", ".js", ".webmanifest"}:
            response.headers["Cache-Control"] = f"public, max-age={STATIC_CACHE_MEDIUM}"
        else:
            response.headers["Cache-Control"] = f"public, max-age={STATIC_CACHE_DEFAULT}"
        return response

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon_ico() -> FileResponse:
        return _static_asset("favicon.ico")

    @app.get("/favicon.svg", include_in_schema=False)
    async def favicon_svg() -> FileResponse:
        return _static_asset("favicon.svg")

    @app.get("/favicon-96x96.png", include_in_schema=False)
    async def favicon_png() -> FileResponse:
        return _static_asset("favicon-96x96.png")

    @app.get("/apple-touch-icon.png", include_in_schema=False)
    async def apple_touch_icon() -> FileResponse:
        return _static_asset("apple-touch-icon.png")

    @app.get("/site.webmanifest", include_in_schema=False)
    async def webmanifest() -> FileResponse:
        return _static_asset("site.webmanifest")

    @app.get("/web-app-manifest-192x192.png", include_in_schema=False)
    async def webmanifest_192() -> FileResponse:
        return _static_asset("web-app-manifest-192x192.png")

    @app.get("/web-app-manifest-512x512.png", include_in_schema=False)
    async def webmanifest_512() -> FileResponse:
        return _static_asset("web-app-manifest-512x512.png")

    @app.on_event("startup")
    def _startup() -> None:
        run_migrations()
        init_session_factory()

    def _render_page(
        request: Request,
        template_name: str,
        current_user: User | None = None,
        **context: object,
    ):
        auth_state = "auth" if current_user else "guest"
        return templates.TemplateResponse(
            template_name,
            {
                "request": request,
                "app_version": app_version,
                "auth_state": auth_state,
                "current_user": current_user,
                **context,
            },
        )

    @app.get("/", include_in_schema=False)
    async def index(
        request: Request,
        current_user: User | None = Depends(get_optional_user),
    ):
        return _render_page(
            request,
            "index.html",
            page_title="Home",
            active_page="home",
            current_user=current_user,
        )

    @app.get("/login", include_in_schema=False)
    async def login(
        request: Request,
        current_user: User | None = Depends(get_optional_user),
    ):
        return _render_page(
            request,
            "login.html",
            page_title="Login",
            active_page="login",
            guest_only=True,
            allow_registration=settings.allow_registration,
            current_user=current_user,
        )

    @app.get("/add-watched", include_in_schema=False)
    async def add_watched(
        request: Request,
        current_user: User | None = Depends(get_optional_user),
    ):
        return _render_page(
            request,
            "add-watched.html",
            page_title="Add Watched",
            active_page="add-watched",
            requires_auth=True,
            current_user=current_user,
        )

    @app.get("/history", include_in_schema=False)
    async def history(
        request: Request,
        current_user: User | None = Depends(get_optional_user),
    ):
        return _render_page(
            request,
            "history.html",
            page_title="History",
            active_page="history",
            requires_auth=True,
            current_user=current_user,
        )

    @app.get("/integrations", include_in_schema=False)
    async def integrations(
        request: Request,
        current_user: User | None = Depends(get_optional_user),
    ):
        # Redirect to settings page (integrations merged into settings)
        return RedirectResponse(url="/settings", status_code=301)

    @app.get("/activity", include_in_schema=False)
    async def activity(
        request: Request,
        current_user: User | None = Depends(get_optional_user),
    ):
        return _render_page(
            request,
            "activity.html",
            page_title="Activity",
            active_page="activity",
            requires_auth=True,
            current_user=current_user,
        )

    @app.get("/settings", include_in_schema=False)
    async def settings_page(
        request: Request,
        current_user: User | None = Depends(get_optional_user),
    ):
        return _render_page(
            request,
            "settings.html",
            page_title="Settings",
            active_page="settings",
            requires_auth=True,
            current_user=current_user,
        )

    @app.get("/offline", include_in_schema=False)
    async def offline(
        request: Request,
        current_user: User | None = Depends(get_optional_user),
    ):
        return _render_page(
            request,
            "offline.html",
            page_title="Offline",
            current_user=current_user,
        )

    @app.get(
        "/health",
        tags=["health"],
        summary="Health check",
        description="Simple liveness check for the API.",
    )
    async def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
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
from librarysync.config import settings
from librarysync.db.migrate import run_migrations
from librarysync.db.session import init_session_factory

STATIC_DIR = Path(__file__).resolve().parent / "static"
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

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


def create_app() -> FastAPI:
    app = FastAPI(
        title="librarySync",
        description=(
            "Authenticate with `Authorization: Bearer <token>` or the "
            "`access_token` cookie set by `/api/auth/login`."
        ),
        openapi_tags=OPENAPI_TAGS,
    )

    app.include_router(routes_auth.router)
    app.include_router(routes_integrations.router)
    app.include_router(routes_metadata.router)
    app.include_router(routes_history.router)
    app.include_router(routes_activity.router)
    app.include_router(routes_settings.router)
    app.include_router(routes_admin.router)

    templates = Jinja2Templates(directory=TEMPLATES_DIR)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    def _static_asset(filename: str) -> FileResponse:
        return FileResponse(STATIC_DIR / filename)

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

    def _render_page(request: Request, template_name: str, **context: object):
        return templates.TemplateResponse(template_name, {"request": request, **context})

    @app.get("/", include_in_schema=False)
    async def index(request: Request):
        return _render_page(request, "index.html", page_title="Home", active_page="home")

    @app.get("/login", include_in_schema=False)
    async def login(request: Request):
        return _render_page(
            request,
            "login.html",
            page_title="Login",
            active_page="login",
            guest_only=True,
            allow_registration=settings.allow_registration,
        )

    @app.get("/add-watched", include_in_schema=False)
    async def add_watched(request: Request):
        return _render_page(
            request,
            "add-watched.html",
            page_title="Add Watched",
            active_page="add-watched",
            requires_auth=True,
        )

    @app.get("/history", include_in_schema=False)
    async def history(request: Request):
        return _render_page(
            request,
            "history.html",
            page_title="History",
            active_page="history",
            requires_auth=True,
        )

    @app.get("/integrations", include_in_schema=False)
    async def integrations(request: Request):
        return _render_page(
            request,
            "integrations.html",
            page_title="Integrations",
            active_page="integrations",
            requires_auth=True,
        )

    @app.get("/activity", include_in_schema=False)
    async def activity(request: Request):
        return _render_page(
            request,
            "activity.html",
            page_title="Activity",
            active_page="activity",
            requires_auth=True,
        )

    @app.get("/settings", include_in_schema=False)
    async def settings_page(request: Request):
        return _render_page(
            request,
            "settings.html",
            page_title="Settings",
            active_page="settings",
            requires_auth=True,
        )

    @app.get("/offline", include_in_schema=False)
    async def offline(request: Request):
        return _render_page(request, "offline.html", page_title="Offline")

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

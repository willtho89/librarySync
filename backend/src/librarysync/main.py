from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from librarysync.api import (
    routes_activity,
    routes_auth,
    routes_history,
    routes_integrations,
    routes_metadata,
    routes_settings,
)
from librarysync.db.migrate import run_migrations
from librarysync.db.session import init_session_factory

STATIC_DIR = Path(__file__).resolve().parent / "static"

OPENAPI_TAGS = [
    {"name": "auth", "description": "User registration and authentication."},
    {
        "name": "integrations",
        "description": "Configure player and downstream service integrations.",
    },
    {"name": "metadata", "description": "Metadata providers and lookup workflows."},
    {"name": "history", "description": "Manual watched history operations."},
    {"name": "activity", "description": "Progress events, outbox, and status feeds."},
    {"name": "settings", "description": "Per-user polling and completion settings."},
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

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.on_event("startup")
    def _startup() -> None:
        run_migrations()
        init_session_factory()

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

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

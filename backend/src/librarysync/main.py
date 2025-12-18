from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from librarysync.api import (
    routes_activity,
    routes_auth,
    routes_integrations,
    routes_settings,
)
from librarysync.db.migrate import run_migrations
from librarysync.db.session import init_session_factory

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app() -> FastAPI:
    app = FastAPI(title="librarySync")

    app.include_router(routes_auth.router)
    app.include_router(routes_integrations.router)
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

    @app.get("/health", tags=["health"])
    async def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()

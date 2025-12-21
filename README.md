# librarySync

Self-hosted, Docker-compose-deployable multi-user sync hub for watch progress. MVP target: ingest from Stremio (AIOStreams) and sync to Trakt + SIMKL.

## Goals (MVP)
- [x] Integrate MetaData Provider (TVDB, TMDB, IMDB)
- [x] Add Items to Watch History
- [x] Show Watch History
- [x] Ratings
- [x] Trakt movie/tv sync (incl ratings)
- [x] Trakt movie/tv import
- [x] Simkl movie/tv sync
- [x] Simkl movie/tv import
- [x] Letterbox movie sync (incl ratings)
- [x] Letterbox movie import
- [x] Merge History Items and sync to all upstream integrations
- [x] Metadata update for imported Items (TV Shows Episode Poster)
- [x] Add Watch API
- [ ] Ingest playback progress from AIOStreams proxy.
- [ ] minimal web UI (plain JS + static HTML).
- [ ] worker logs
- [ ] Docker as Non Root User
- [ ] CI/CD Pipelines
- [ ] Delete history -> delete in integrations.
- [ ] 

## TODOs (Not MVP)
- [ ] Anime integrations
- [ ] Delete history -> delete in integrations.
- [ ] Setup wizard after register.
- [ ] scalable & configurable worker system
- [ ] Overhaul UI/UX (PWA, Mobile Friendly)
- [ ] Stremio API (Watch History; see Stremthru/dash)

## Ideas
- [ ] Catalogs (most watched movies/TV this <intervall>).

## Repository Layout
```
librarySync/
  agent.md
  README.md
  docker-compose.yml
  .env.example

  backend/
    Dockerfile
    pyproject.toml
    src/librarysync/
      main.py
      config.py
      db/
        session.py
        models.py
        migrations/
      api/
        routes_auth.py
        routes_integrations.py
        routes_activity.py
        routes_settings.py
      core/
        canonical.py
        security.py
        dedupe.py
        outbox.py
        matching.py
      connectors/
        players/
          base.py
          aiostreams.py
        services/
          base.py
          trakt.py
          simkl.py
      jobs/
        poll_aiostreams.py
        process_outbox.py
        drift_daily.py
      static/
        index.html
        login.html
        integrations.html
        activity.html
        settings.html
        app.js
        styles.css
      templates/

  worker/
    Dockerfile
    pyproject.toml
    src/librarysync_worker/
      main.py
```

## Getting Started (Local)
1. Copy env example: `cp .env.example .env` and adjust values.
2. Build and run: `docker compose up --build`.
3. Open `http://localhost:8000`.

## Development (uv + Ruff)
1. Install Python 3.14 and `uv` (see `.python-version`).
2. Sync dependencies:
   - `cd backend && uv sync`
   - `cd worker && uv sync`
3. Run the API: `cd backend && uv run uvicorn librarysync.main:app --reload`.
4. Run the worker: `cd worker && uv run python -m librarysync_worker.main`.
5. Lint: `cd backend && uv run ruff check ../backend/src ../worker/src`.

## Database Migrations
- Ensure Postgres is running: `docker compose up -d db`
- Generate a revision (uses the Docker DB + .env):
  `docker compose run --rm -v "$PWD:/app" -w /app api alembic -c backend/alembic.ini revision --autogenerate -m "init"`
- Apply migrations: `docker compose run --rm -v "$PWD:/app" -w /app api alembic -c backend/alembic.ini upgrade head`

## Auth Bootstrap
- Default auth is username + password with JWT cookies.
- Registration is enabled by default via `POST /api/auth/register`.
- Disable registration by setting `LIBRARYSYNC_ALLOW_REGISTRATION=false`.

## Environment Variables
The following are required for the MVP (see `.env.example` for defaults):
- `DATABASE_URL`
- `LIBRARYSYNC_SECRET_KEY`
- `LIBRARYSYNC_BASE_URL`
- `TRAKT_CLIENT_ID`, `TRAKT_CLIENT_SECRET`
- `SIMKL_CLIENT_ID`, `SIMKL_CLIENT_SECRET`
- `POLL_INTERVAL_SECONDS`
- `COMPLETION_THRESHOLD_PERCENT`
- `LOG_LEVEL`
- `LIBRARYSYNC_JWT_ACCESS_TOKEN_MINUTES`
- `LIBRARYSYNC_JWT_ALGORITHM`
- `LIBRARYSYNC_ALLOW_REGISTRATION`

## Notes
- The backend and worker share the `librarysync` package.
- Connectors are kept pure (no DB writes inside connectors).
- Outbox and canonical models are the backbone for syncing.
- Ruff handles linting and import sorting with a 100-character line length.
- The root `pyproject.toml` defines the uv workspace.
- Letterboxd credentials: thanks to https://github.com/dado3212/letterboxd-scripts/tree/main
  for guidance on retrieving the `client_id` and `client_secret`.

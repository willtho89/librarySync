# librarySync - Copilot Instructions

## Project Overview

librarySync is a self-hosted, Docker-compose-deployable, multi-user hub for watch history and ratings that syncs across Trakt, SIMKL, Letterboxd, and Stremio.

## Tech Stack

- **Backend**: Python 3.13+ with FastAPI
- **Database**: PostgreSQL with SQLAlchemy 2.0+ and Alembic migrations
- **Frontend**: Static HTML + vanilla JavaScript with Tailwind CSS
- **Architecture**: Async worker pattern with outbox-based delivery
- **Package Manager**: uv (preferred) with pyproject.toml
- **Linter**: Ruff with 120 character line length

## Code Style & Conventions

### Python
- Line length: 120 characters (configured in pyproject.toml)
- Use async/await patterns throughout the codebase
- Follow existing patterns for database models and SQLAlchemy queries
- Use type hints consistently

### HTTP Requests
- **ALWAYS** use `get_http_client()` from `core/http_client.py` for all HTTP requests
- This ensures consistent User-Agent headers (`librarySync Version/<version>`)
- Never use httpx directly; always use the centralized HTTP client

### Connectors
- Keep connectors pure: **NO database writes inside connector modules**
- Connectors live in `backend/src/librarysync/connectors/`
- Services: `services/` (Trakt, SIMKL, Letterboxd, Stremio)
- Metadata: `metadata/` (TMDB, TVDB, IMDb, TVMaze, Kitsu, MyAnimeList)

### Security
- Store secrets in `integration_secrets` table (encrypted), NOT in `integrations.config`
- Use `LIBRARYSYNC_SECRET_KEY` for encryption (see `core/security.py`)
- Hash passwords with bcrypt; enforce 8+ chars and 72-byte max
- Never log raw secrets or tokens
- Sanitize provider responses before storage/logging

### Styling
- Do NOT edit `backend/src/librarysync/static/styles.css` directly
- Update `frontend/input.css` instead
- After updating Tailwind styles, rebuild with: `npm run build` (from frontend directory)

## Architecture Patterns

### Data Flow (Canonical)
1. Manual add or import → `MediaItem`/`EpisodeItem` + `WatchedItem`
2. Append `WatchEvent` for auditing
3. Enqueue internal outbox job → provider sync jobs + `WatchSync` rows
4. Worker delivers jobs and records `SyncAttempt` + `WatchSync` status
5. Metadata enrichment fills missing IDs/posters when possible

### Pipeline Helpers
- Use `watch_pipeline.py` helpers to enqueue sync jobs
- Follow the outbox pattern for all async operations
- Use `metadata_lookup_engine.py` for metadata resolution

## Database Models

Key tables:
- `users`: Authentication and per-user settings
- `integrations` + `integration_secrets`: Provider configs (secrets are encrypted)
- `media_items` + `episode_items`: Canonical media catalog
- `watched_items`: Per-user watch history with ratings
- `watch_events`: Append-only event log
- `watch_syncs`: Per-provider sync status
- `outbox` + `sync_attempts`: Delivery queue and history
- `metadata_lookup_requests` + `metadata_lookup_candidates`: Async lookup pipeline

## Testing

- Tests located in `backend/tests/`
- Follow existing test patterns (see `test_routes_history.py`, `test_http_client.py`)
- Focus on outbox transitions, import scheduling, and metadata lookups
- Run tests before committing changes

## Worker Modes

Worker modes (configurable via `LIBRARYSYNC_WORKER_MODES`):
- `outbox`: Process delivery queue
- `metadata`: Resolve metadata lookups
- `metadata_cache`: Seed media_items for faster searches
- `quick_import`: 7-day import window
- `import_all`: Full history import per user
- `watchlist`: Watchlist sync operations
- `merge_all_history`: Dedupe watched history entries

## API Structure

- Auth routes: `api/routes_auth.py`
- Integration routes: `api/routes_integrations.py`
- History routes: `api/routes_history.py`
- Metadata routes: `api/routes_metadata.py`
- Activity/status: `api/routes_activity.py`
- Settings: `api/routes_settings.py`
- Admin: `api/routes_admin.py` (requires `X-API-Key` header)

## Development Setup

1. Install Python 3.13+ and uv
2. Copy `.env.example` to `.env` and configure
3. Run with Docker: `docker compose up --build`
4. Access at `http://localhost:8000`

## Common Tasks

### Adding a new integration
1. Create connector in `connectors/services/`
2. Add OAuth flow in `api/routes_integrations.py`
3. Update `integration_secrets` encryption
4. Add worker job in `jobs/`
5. Configure rate limiting in config

### Adding a metadata provider
1. Create provider in `connectors/metadata/`
2. Use `get_http_client()` for all requests
3. Register in `metadata_providers.py`
4. Add configuration in `api/routes_metadata.py`

### Modifying the watch pipeline
1. Update `core/watch_pipeline.py` for enqueue logic
2. Modify `jobs/process_outbox.py` for delivery
3. Update `db/models.py` if schema changes needed
4. Create Alembic migration for DB changes

## Important Notes

- Multi-user: All operations are per-user scoped
- Rate limiting: Per-user, per-provider token buckets
- Batch sizes: Configurable per provider (Trakt: 750, SIMKL: 750)
- OAuth: Trakt and SIMKL use standard OAuth2 flows
- Letterboxd: Requires client credentials extracted from mobile app
- Stremio: Uses auth key authentication

## Documentation

- `AGENTS.md`: Comprehensive architecture and API documentation
- `README.md`: Quick start and configuration guide
- `.env.example`: All environment variables with defaults

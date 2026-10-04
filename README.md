# librarySync

![librarySync logo](.img/logo_w_text.png)

Self-hosted, Docker-compose-deployable sync hub for watch history and ratings across
multiple services. This project exists because I use multiple services and want to keep
my watch history in sync.

## What It Does

- Multi-user auth with JWT cookies and optional registration.
- Manual watch history (add/update/delete) with optional downstream deletion.
- Ratings support synced where supported.
- Imports from Trakt, SIMKL, Letterboxd, and Stremio (quick import + import all).
- Sync providers include Trakt, SIMKL, Letterboxd, Stremio, AniList, and PublicMetaDB.
- AIOStreams Watch State v2: watched marks, resume positions, Continue Watching, watchlists, dropped shows, independent ratings, bulk marks, and household viewers.
- Outbox-based delivery with retries and per-user rate limiting.
- Metadata lookup and enrichment with TMDB, TVDB, IMDb, TVMaze, Kitsu, MyAnimeList, PublicMetaDB.
- Minimal web UI (static HTML + JS).

## Quick Start (Docker Compose)

1. Copy the env example:
   `cp .env.example .env`
2. Edit `.env` with your credentials and secrets.
3. Start:
   `docker compose up --build`
4. Open `http://localhost:8000`.

By default, `docker-compose.override.yml` is loaded and builds local images. To pull
the published images instead, run:
`docker compose -f docker-compose.yml up --pull=always`

## Environment Variables (Defaults)

All defaults below are from `.env.example`.

### Database
- `POSTGRES_DB` (default `librarysync`): database name for the Postgres container.
- `POSTGRES_USER` (default `librarysync`): database user for the Postgres container.
- `POSTGRES_PASSWORD` (default `librarysync`): database password for the Postgres container.
- `DATABASE_URL`
  (default `postgresql+psycopg://librarysync:librarysync@db:5432/librarysync`):
  SQLAlchemy connection string used by API/worker.

### App
- `LIBRARYSYNC_SECRET_KEY` (default `change_me`): encryption key for stored secrets.
  Change this in production.
- `LIBRARYSYNC_ADMIN_API_KEY` (default `your_admin_api_key`): required for admin endpoints.
- `LIBRARYSYNC_BASE_URL` (default `http://localhost:8000`): base URL for OAuth callbacks.
- `LOG_LEVEL` (default `INFO`): logging level.
- `HISTORY_LOOKBACK_DAYS` (default `30`): import-all lookback window (set `-1` for full history).
- `LIBRARYSYNC_JWT_ACCESS_TOKEN_MINUTES` (default `60`): access token lifetime.
- `LIBRARYSYNC_JWT_ALGORITHM` (default `HS256`): JWT signing algorithm.
- `LIBRARYSYNC_ALLOW_REGISTRATION` (default `true`): enable `/api/auth/register`.
- `LIBRARYSYNC_MAX_USERS` (default `1`): max number of registered users (`-1` for unlimited).

### OAuth
- `TRAKT_CLIENT_ID` (default `your_trakt_client_id`): Trakt OAuth app client ID.
- `TRAKT_CLIENT_SECRET` (default `your_trakt_client_secret`): Trakt OAuth app secret.
- `SIMKL_CLIENT_ID` (default `your_simkl_client_id`): SIMKL OAuth app client ID.
- `SIMKL_CLIENT_SECRET` (default `your_simkl_client_secret`): SIMKL OAuth app secret.

### Worker Modes
- `LIBRARYSYNC_WORKER_MODES` (default `all`): comma-separated list of worker loops.
  Options: `outbox`, `metadata`, `metadata_backfill`, `metadata_cache`, `quick_import`, `import_all`, `watch_state`, `watchlist`, `merge_history`, `merge_all_history`.
- `LIBRARYSYNC_WORKER_OUTBOX_CONCURRENCY` (default `1`): outbox loop concurrency.
- `LIBRARYSYNC_WORKER_METADATA_CONCURRENCY` (default `1`): metadata loop concurrency.
- `LIBRARYSYNC_WORKER_METADATA_CACHE_CONCURRENCY` (default `1`): metadata cache loop concurrency.
- `LIBRARYSYNC_WORKER_QUICK_IMPORT_CONCURRENCY` (default `1`): quick import loop concurrency.
- `LIBRARYSYNC_WORKER_IMPORT_ALL_CONCURRENCY` (default `1`): import-all loop concurrency.

### Rate Limits (per user, per provider, per minute)
- `LIBRARYSYNC_TRAKT_RATE_LIMIT_PER_MINUTE` (default `60`).
- `LIBRARYSYNC_SIMKL_RATE_LIMIT_PER_MINUTE` (default `60`).
- `LIBRARYSYNC_LETTERBOXD_RATE_LIMIT_PER_MINUTE` (default `30`).
- `LIBRARYSYNC_STREMIO_RATE_LIMIT_PER_MINUTE` (default `120`).
- `LIBRARYSYNC_TMDB_RATE_LIMIT_PER_MINUTE` (default `150`).
- `LIBRARYSYNC_TVDB_RATE_LIMIT_PER_MINUTE` (default `150`).

### PublicMetaDB Rate Limits (per user, request-window based)
- `LIBRARYSYNC_PUBLICMETADB_RATE_LIMIT_MAX_REQUESTS` (default `300`).
- `LIBRARYSYNC_PUBLICMETADB_RATE_LIMIT_INTERVAL_SECONDS` (default `10`).
- `LIBRARYSYNC_PUBLICMETADB_BATCH_RATE_LIMIT_MAX_REQUESTS` (default `3`, reserved for `/api/batch` support).
- `LIBRARYSYNC_PUBLICMETADB_BATCH_RATE_LIMIT_INTERVAL_SECONDS` (default `1`, reserved for `/api/batch` support).

### Batch Sizes (for batch-capable providers)
- `LIBRARYSYNC_TRAKT_MAX_BATCH_SIZE` (default `750`): Maximum number of items per Trakt batch request.
- `LIBRARYSYNC_SIMKL_MAX_BATCH_SIZE` (default `750`): Maximum number of items per SIMKL batch request (limited by 20MB POST size).

## Integrations

### AIOStreams Watch State

LibrarySync supports AIOStreams' [Watch State v2 protocol](https://docs.aiostreams.viren070.me/reference/addon-protocol/watch-state/).

1. Open **Stremio Addon** in LibrarySync.
2. Enable **Sync Watch State with AIOStreams** and save.
3. Add the manifest URL to AIOStreams, or refresh an existing install.
4. Enable the addon's **Watch State** resource and select it as the user's tracker.

The setting is off by default. The manifest URL grants read and write access to that user's
state, so keep it private. Disabling Watch State or the addon immediately disables its endpoints.
AIOStreams' instance settings `WATCH_STATE_REPORT_ENABLED` and `WATCH_STATE_PULL_ENABLED`
must permit the directions you use. The normal `all` worker includes `watch_state`; custom
worker configurations need that mode to process accepted bulk marks and mapping retries.

Supported state:

- **Watched marks:** `played` and `unplayed` override older playback and imported history.
  Completed stops create history through the existing metadata and provider sync pipeline.
  Unfinished stops preserve progress without creating watched history. Explicit clears remove
  watches up to the event timestamp and queue supported provider deletions.
- **Resume and Continue Watching:** pauses and unfinished stops retain positions, including
  positions with unknown duration. Starts clear paused state. Pulls include released next episodes,
  excluding dropped shows. Resume points can be removed from the addon page.
- **Watchlists and dropped shows:** favourites sync through personal watchlist sources. Removing
  an AIOStreams favourite preserves manual and other-source membership. Drop/undrop changes use
  supported provider watchlist and dropped-list operations. Starting or completing playback
  resumes a dropped show.
- **Independent ratings:** movie, show, season and episode scores retain the protocol's 0–10 scale,
  including zero and decimals, without creating watches. The addon page can add, edit and clear them.
- **Bulk marks:** season/show marks are acknowledged after durable receipt and processed by the
  worker, with per-episode outcomes and idempotent retries. Newer single-episode marks take precedence
  over delayed bulk marks. Specials retain season 0.
- **Household viewers:** invite a viewer on the addon page; the target LibrarySync user accepts
  the invitation while signed in. AIOStreams personas use the matching viewer slug and their own
  history. Unknown viewers are rejected. Either user can revoke access.
- **Diagnostics:** the addon page shows receipt and pull times, duplicates, pending/unresolved
  events, retries, episode mapping repair, and downstream delivery failures.

Pulls supply complete watched, watchlist and rating snapshots. A matching `since` version avoids
history and episode scans; current playback positions are still returned. Database changes from
imports, manual edits and bulk operations invalidate the cached snapshot.

Absolute-numbered episodes retain their original metadata/video identities. LibrarySync does not
guess a broadcast season. An unresolved episode can be mapped to an existing show and canonical
season/episode in diagnostics; subsequent events reuse the mapping for history and provider delivery.
The broader shared episode-identity refactor remains deferred.

Standalone rating delivery follows provider capabilities:

| Provider | Supported rating scopes | Conversion |
| --- | --- | --- |
| Trakt | Movie, show, season, episode | Integer 1–10, rounded half up |
| SIMKL | Movie, show | Integer 1–10, rounded half up |
| PublicMetaDB | Movie, show, episode | Integer 1–10, rounded half up, then provider scale |
| AniList | Show with an AniList ID | Protocol score, preserves existing status/progress |
| Letterboxd, Stremio | No standalone rating operation | Reported as unsupported in diagnostics |

Unsupported scopes are visible failures, not silently discarded. Local protocol scores keep their
original precision even when a downstream provider needs a different scale.

Playback reporting requires AIOStreams or another Jellyfin-compatible client. Native Stremio
playback still uses the Stremio import integration. The old AIOStreams proxy-state importer has
been removed. The upgrade retires its configuration and deletes its stored credentials and queue
entries while preserving previously imported history. Downgrading does not restore those credentials.

See [0.21.0 release notes](docs/releases/0.21.0.md) and
[local conformance testing](docs/watch-state-testing.md).

### Letterboxd
Letterboxd unfortunately does not have a devleoper program to request API access. For personal use it is possible to extract the required information from the app. Special thanks to @dado3212 with https://github.com/dado3212/letterboxd-scripts/ for guidance on retrieving the `client_id` and `client_secret`.
> Letterboxd tip: users can paste an intercepted request as `curl` or `httpie` to extract the `client_id` and `client_secret`.

### Trakt/SIMKL

When configuring connected apps for Trakt and SIMKL, add your domain and callback URLs.
Example values (replace `example.com` with your domain):
- Trakt app URL: `https://example.com`
- Trakt redirect URI: `https://example.com/api/integrations/trakt/callback`
- SIMKL app URL: `https://example.com`
- SIMKL redirect URI: `https://example.com/api/integrations/simkl`


## Development (uv + Ruff)

1. Install Python 3.13 or 3.14 and `uv` (`python3 -m pip install --upgrade pip && python3 -m pip install uv`).
2. Sync deps:
   - `cd backend && uv sync --extra tests`
3. Run API:
   `cd backend && uv run uvicorn librarysync.main:app --reload`
4. Run worker:
   `cd backend && uv run python -m librarysync.worker`
5. Lint:
   `cd backend && uv run ruff check src tests`
6. Run tests:
   `cd backend && uv run pytest -q tests`

If you’re using `docker compose` with the `scaled-workers` profile, `worker-metadata-cache`
is now available and will run the metadata cache loop (`LIBRARYSYNC_WORKER_MODES=metadata_cache`).

## Release

Use the release helper to bump the version in `backend/pyproject.toml`, tag,
and create a GitHub release.

Examples:
- `python scripts/release.py --patch`
- `python scripts/release.py 0.9.0 --no-release`

The release helper runs `ruff` and the unit tests before it commits.

`gh` must be installed and authenticated if you are creating a GitHub release.

## Static File Cache Invalidation

librarySync automatically handles browser cache invalidation for static assets (CSS and JavaScript) by appending version query parameters to their URLs.

- Static files are cached for 7 days by default
- When you update the version in `backend/pyproject.toml`, browsers will automatically fetch new files
- Example: `/static/core.js?v=0.4.3` becomes `/static/core.js?v=0.4.5` on version bump

No manual cache busting or build-time hash generation is needed.

## Credits

Thanks to @MunifTanjim with [https://github.com/MunifTanjim/stremthru.git](Stremthru) for the inspiration behind the
Stremio sync workflow.

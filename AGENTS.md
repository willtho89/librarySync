# librarySync — AGENTS.md

## 1) Mission

**librarySync** is a self-hosted, Docker-deployable, multi-user hub for watch history and ratings. It syncs watch history to **Trakt**, **SIMKL**, **Letterboxd**, and **Stremio**, powered by an async metadata lookup and enrichment pipeline.

---

## 2) Feature Set

- **Authentication**: Multi-user JWT cookies with optional registration toggle
- **History Management**: Manual add, update, delete, bulk delete; optional deletion in integrations; mark next released episode of a show as watched
- **Ratings**: 0.5–5.0 star ratings synced to supported providers
- **Metadata Providers**: TMDB, TVDB, IMDb, TVMaze, Kitsu, MyAnimeList (per-user configuration)
- **Metadata Pipeline**: Async lookup and enrichment (posters/IDs) with local cache reuse
- **Import Sources**: Trakt, SIMKL, Letterboxd, Stremio (quick import and full import)
- **Import Queue**: Priority-ordered per-user queue with post-import deduplication
- **Sync Engine**: Outbox-based delivery with retries, per-user rate limiting, and configurable batch sizes
- **UI**: Minimal static HTML/JS interface (login, integrations, settings, activity, history, dashboard with up-next shows)

---

## 3) Architecture

### Components
- **API**: FastAPI serving JSON endpoints and static UI at `/static`
- **Worker**: Async loops for outbox processing, metadata lookups, quick import, and full import

### Data Flow
1. Manual add or import → `MediaItem`/`EpisodeItem` + `WatchedItem`
2. Append `WatchEvent` for auditing
3. Enqueue internal outbox job → provider sync jobs + `WatchSync` rows
4. Worker delivers jobs and records `SyncAttempt` + `WatchSync` status
5. Metadata enrichment fills missing IDs/posters when available

---

## 4) Repository Layout

```
librarySync/
  AGENTS.md
  README.md
  docker-compose.yml
  .env.example

  backend/
    Dockerfile
    alembic.ini
    pyproject.toml
    src/librarysync/
      main.py                  # FastAPI app entry
      worker.py                # Async worker entrypoint
      config.py                # Environment/config handling
      db/
        models.py
        session.py
        migrations/
      api/
        routes_auth.py
        routes_integrations.py
        routes_history.py
        routes_metadata.py
        routes_activity.py
        routes_settings.py
        routes_admin.py
      core/
        auth.py
        watch_pipeline.py
        import_schedule.py
        import_control.py
        import_state.py
        import_all.py
        import_history.py
        metadata_lookup_engine.py
        metadata_enrichment.py
        metadata_providers.py
        next_episode.py
        rate_limiter.py
        ratings.py
        security.py
        watchlist.py
        watchlist_sync.py
      connectors/
        services/
          trakt.py
          simkl.py
          letterboxd.py
          stremio.py
          stremio_watched_bitfield.py
        metadata/
          tmdb.py
          tvdb.py
          imdb.py
          tvmaze.py
          kitsu.py
          myanimelist.py
      jobs/
        process_outbox.py
        metadata_lookup.py
        imports.py
        trakt_import.py
        simkl_import.py
        letterboxd_import.py
        stremio_import.py
        merge_history.py
      static/
        app.js
        styles.css
      templates/
        index.html
        login.html
        integrations.html
        settings.html
        activity.html
        history.html
        add-watched.html
        settings/
          providers.html
          metadata.html
          watchlists.html
          blacklist.html
          activity.html
          history.html
          imports.html
          preferences.html
          modals.html
        base.html
        watchlist.html
        offline.html
```

---

## 5) Data Model

### Core Tables
- **`users`**: Authentication and per-user settings (e.g., include adult content in search)
- **`integrations`**: Per-user provider configurations
- **`integration_secrets`**: Encrypted provider credentials
- **`media_items`**: Canonical movie/show catalog
- **`episode_items`**: Canonical episode catalog
- **`watched_items`**: Per-user watch history (watched_at, rating, source)

### Audit & Sync
- **`watch_events`**: Append-only event log for imports and manual changes
- **`watch_syncs`**: Per-provider sync status with external IDs and error details
- **`outbox`**: Delivery queue for sync jobs
- **`sync_attempts`**: Attempt history for deliveries

### Metadata & Jobs
- **`metadata_lookup_requests`**: Async lookup pipeline requests
- **`metadata_lookup_candidates`**: Async lookup pipeline candidates
- **`scheduled_jobs`**: Job leases for recurring worker tasks
- **`rate_limit_buckets`**: Per-user/provider token buckets

### Legacy
- **`progress_events`**: Progress model (not yet wired to outbox)

---

## 6) Integrations & Metadata Providers

### Service Integrations (Sync + Import)
- **Trakt**: OAuth authentication
- **SIMKL**: OAuth authentication
- **Letterboxd**: Client credentials with refresh token
- **Stremio**: Auth key

### Metadata Providers (Lookup Only)
- **TMDB**: API key required
- **TVDB**: API key required, optional PIN
- **IMDb**: No authentication required
- **TVMaze**: No authentication required
- **Kitsu**: No authentication required
- **MyAnimeList**: No authentication required

**Storage**: Provider configurations in `integrations` table; sensitive credentials in `integration_secrets` (encrypted).

---

## 7) Worker Modes & Jobs

### Worker Modes
Configured via `LIBRARYSYNC_WORKER_MODES`: `outbox`, `metadata`, `metadata_backfill`, `metadata_cache`, `quick_import`, `import_all`, `watchlist`, `merge_history`, `merge_all_history`

### Job Types

#### Outbox Processing (`process_outbox`)
Handles job types: `push_watched`, `push_rating`, `update_history`, `remove_history`, `update_log_entry`, `delete_log_entry`, `remove_watched`, and internal `new_item_added`

#### Metadata Jobs
- **`metadata_lookup`**: Resolves lookup requests into candidates
- **`metadata_cache`**: Scans recent candidates and seeds `media_items` to accelerate search
- **`metadata_backfill`**: Periodically refreshes metadata/enriches watched history and episode lists that are missing posters or identifiers

#### Import Jobs
- **`quick_import`**: Runs 7-day import window on the user's configured schedule (30 min to 7 days). Per-user runs are single-flight via a lease stored in the integration config (10-minute expiry, refreshed at each claim); an expired lease lets any worker resume a stuck run from its saved queue index
- **`import_all`**: Sequences providers per user for full import
- **Dropped ingestion**: Trakt (`GET /users/hidden/dropped?type=show`) and SIMKL (`status: "dropped"` in `/sync/all-items`) dropped shows are imported into the terminal `dropped` watchlist status, tracked via a per-provider `WatchlistSource` (`external_id="dropped"`); reconcile un-drops shows that leave the provider's dropped list. Import upserts never resurrect dropped items (`restore_dropped=False`)
- **`merge_history`**: Post-import deduplication (same-day movie entries) and repoints sync/outbox rows
- **`merge_all_history`**: Periodic deduplication of all user history in database (API merges on-the-fly until DB is clean)

---

## 8) API Surface

### Auth
```
POST   /api/auth/register       # If registration enabled
POST   /api/auth/login
POST   /api/auth/logout
GET    /api/auth/me
```

### Settings
```
GET    /api/settings
POST   /api/settings
```

### Integrations
```
GET    /api/integrations
POST   /api/integrations/letterboxd
POST   /api/integrations/letterboxd/test
POST   /api/integrations/letterboxd/disconnect
GET    /api/integrations/trakt/start
GET    /api/integrations/trakt/callback
POST   /api/integrations/trakt/disconnect
GET    /api/integrations/simkl/start
GET    /api/integrations/simkl/callback
POST   /api/integrations/simkl/disconnect
POST   /api/integrations/stremio/login
POST   /api/integrations/stremio/disconnect
POST   /api/integrations/import/quick/schedule
POST   /api/integrations/import/quick
POST   /api/integrations/import/all
```

### Metadata
```
GET    /api/metadata/providers
POST   /api/metadata/providers/{tmdb|tvdb|kitsu|tvmaze|imdb|myanimelist}
POST   /api/metadata/providers/{provider}/test
POST   /api/metadata/lookup
GET    /api/metadata/lookup/{lookup_id}
GET    /api/metadata/tv/{provider}/{provider_item_id}/seasons
GET    /api/metadata/tv/{provider}/{provider_item_id}/seasons/{season_number}/episodes
```

### History
```
POST   /api/history/items
GET    /api/history/items
PATCH  /api/history/items/{watched_id}
DELETE /api/history/items
DELETE /api/history/items/{watched_id}
POST   /api/history/items/bulk-delete
POST   /api/history/items/sync
POST   /api/history/shows/{media_item_id}/mark-next-episode
```

### Dashboard
```
GET    /api/dashboard/stats
GET    /api/dashboard/up-next
```

### Activity / Status
```
GET    /api/activity/events
GET    /api/activity/sessions
GET    /api/outbox
GET    /api/status
```

### Admin (requires `X-API-Key`)
```
POST   /api/admin/reset-outbox-jobs
DELETE /api/admin/purge-jobs
POST   /api/admin/merge-history
```

---

## 9) Configuration

### Core Settings
- `DATABASE_URL`
- `LIBRARYSYNC_SECRET_KEY`
- `LIBRARYSYNC_ADMIN_API_KEY`
- `LIBRARYSYNC_BASE_URL`
- `LOG_LEVEL`

### Authentication & Authorization
- `LIBRARYSYNC_JWT_ACCESS_TOKEN_MINUTES`
- `LIBRARYSYNC_JWT_ALGORITHM`
- `LIBRARYSYNC_ALLOW_REGISTRATION`

### Import & History
- `HISTORY_LOOKBACK_DAYS`

### OAuth Credentials
- `TRAKT_CLIENT_ID`
- `TRAKT_CLIENT_SECRET`
- `SIMKL_CLIENT_ID`
- `SIMKL_CLIENT_SECRET`

### Worker Configuration
- `LIBRARYSYNC_WORKER_MODES`
- `LIBRARYSYNC_WORKER_OUTBOX_CONCURRENCY`
- `LIBRARYSYNC_WORKER_METADATA_CONCURRENCY`
- `LIBRARYSYNC_WORKER_METADATA_CACHE_CONCURRENCY`
- `LIBRARYSYNC_WORKER_QUICK_IMPORT_CONCURRENCY`
- `LIBRARYSYNC_WORKER_IMPORT_ALL_CONCURRENCY`

### Rate Limiting
- `LIBRARYSYNC_TRAKT_RATE_LIMIT_PER_MINUTE`
- `LIBRARYSYNC_SIMKL_RATE_LIMIT_PER_MINUTE`
- `LIBRARYSYNC_LETTERBOXD_RATE_LIMIT_PER_MINUTE`
- `LIBRARYSYNC_STREMIO_RATE_LIMIT_PER_MINUTE`

### Batch Sizes
- `LIBRARYSYNC_TRAKT_MAX_BATCH_SIZE` (default: 750)
- `LIBRARYSYNC_SIMKL_MAX_BATCH_SIZE` (default: 750)

---

## 10) Security Requirements

- **Secrets**: Encrypt at rest using `LIBRARYSYNC_SECRET_KEY` (see `core/security.py`)
- **Passwords**: Bcrypt hashing with 8+ character minimum and 72-byte maximum (no truncation)
- **OAuth**: State validation for Trakt and SIMKL flows
- **Logging**: Never log raw secrets or tokens

---

## 11) Observability

### Audit Trail
Primary audit sources: `watch_events`, `outbox`, `sync_attempts`, `watch_syncs`

### Monitoring
- **`/api/status`**: Exposes schedule and queue state for UI
- **Provider responses**: Sanitized before storage and logging

---

## 12) Developer Guidance

### Code Practices
- **Connectors**: Keep pure—no database writes inside connectors
- **Sync Jobs**: Use `watch_pipeline.py` helpers to enqueue sync jobs
- **Secrets**: Store in `integration_secrets` (encrypted), never in `integrations.config`
- **HTTP Requests**: Always use `get_http_client()` from `core/http_client.py` to ensure consistent User-Agent headers (`librarySync Version/<version>`)

### Tooling
- **Linter**: Ruff with 120-character line length (see `pyproject.toml`)
- **Styles**: Edit `frontend/input.css` (not `backend/src/librarysync/static/styles.css` directly)
- **Build**: Rebuild `backend/src/librarysync/static/styles.css` after Tailwind changes

---

## 13) Tests

### Existing Tests
- `backend/tests/test_routes_history.py`
- `backend/tests/test_stremio_watched_bitfield.py`
- `backend/tests/test_http_client.py`

### Testing Guidance
Add tests for outbox transitions, import scheduling, and metadata lookups when modifying those areas.

---

## 14) Known Gaps

- CI/CD pipeline (see README TODO)
- UI/UX polish
- Progress/scrobble ingestion (exists in models but not wired to outbox)

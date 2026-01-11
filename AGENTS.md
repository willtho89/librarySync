# librarySync — AGENTS.md

## 1) Mission / Current Snapshot

Build **librarySync**, a self-hosted, Docker-compose-deployable, **multi-user** hub for
watch history + ratings. The current code base focuses on **watched history** (manual and
imported) and syncs that history to **Trakt, SIMKL, Letterboxd, and Stremio**, backed by
an async metadata lookup/enrichment pipeline.

---

## 2) Current Feature Set (Implemented)

- Multi-user auth with JWT cookies; optional registration toggle.
- Manual history management: add, update, delete, bulk delete; optional delete in integrations.
- Ratings support (0.5–5.0 stars) synced to providers where supported.
- Metadata providers (per user): TMDB, TVDB, IMDb, TVMaze, Kitsu, MyAnimeList.
- Async metadata lookup and enrichment (posters/IDs), with local cache reuse.
- Imports from Trakt, SIMKL, Letterboxd, Stremio (quick import + import all).
- Import-all queue (priority-ordered per user) + post-import history merge dedupe.
- Outbox-based delivery with retries + per-user rate limiting + configurable batch sizes.
- Configurable per-provider batch sizes for efficient large library syncing.
- Minimal UI (static HTML + JS): login, integrations, settings, activity, history, add-watched.

---

## 3) Architecture / Data Flow

- **API**: FastAPI serves JSON endpoints and static UI under `/static`.
- **Worker**: async loops for outbox, metadata lookups, quick import, and import-all.
- **Canonical flow**:
  1. Manual add or import -> `MediaItem`/`EpisodeItem` + `WatchedItem`
  2. Append `WatchEvent` for auditing.
  3. Enqueue internal outbox job -> provider sync jobs + `WatchSync` rows.
  4. Worker delivers jobs and records `SyncAttempt` + `WatchSync` status.
  5. Metadata enrichment fills missing IDs/posters when possible.

---

## 4) Repository Layout (Actual)

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
      config.py                # env/config handling
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
        import_all.py
        metadata_lookup_engine.py
        metadata_enrichment.py
        metadata_providers.py
        rate_limiter.py
        ratings.py
        security.py
        outbox.py
        dedupe.py
        matching.py
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

  worker/
    Dockerfile
    pyproject.toml
    src/librarysync_worker/
      main.py
```

---

## 5) Data Model (DB Highlights)

- `users`: auth + per-user settings (e.g., include adult in search).
- `integrations` + `integration_secrets`: per-user provider config + encrypted secrets.
- `media_items` + `episode_items`: canonical media catalog.
- `watched_items`: per-user watch history (watched_at, rating, source).
- `watch_events`: append-only event log for imports/manual changes.
- `watch_syncs`: per-provider sync status + external IDs/errors.
- `outbox` + `sync_attempts`: delivery queue and attempt history.
- `metadata_lookup_requests` + `metadata_lookup_candidates`: async lookup pipeline.
- `scheduled_jobs`: leases for recurring jobs.
- `rate_limit_buckets`: per-user/provider token buckets.
- `progress_events`: legacy progress model (not wired to outbox yet).

---

## 6) Integrations & Metadata Providers

- **Downstream + import**: Trakt (OAuth), SIMKL (OAuth), Letterboxd (client + refresh token),
  Stremio (auth key).
- **Metadata providers**: TMDB (API key), TVDB (API key + optional PIN), IMDb, TVMaze,
  Kitsu, MyAnimeList (no secrets).
- Provider configs live in `integrations`; sensitive fields in `integration_secrets`.

---

## 7) Worker Modes & Jobs

- `LIBRARYSYNC_WORKER_MODES`: `outbox`, `metadata`, `metadata_cache`, `quick_import`, `import_all`, `watchlist`.
- `process_outbox` handles `push_watched`, `push_rating`, `update_history`,
  `remove_history`, `update_log_entry`, `delete_log_entry`, `remove_watched`,
  and internal `new_item_added`.
- `metadata_lookup` resolves lookup requests into candidates.
- `metadata_cache` scans recent lookup candidates and seeds `media_items` to speed up add-menu searches.
- `quick_import` runs the 7-day import window; `import_all` sequences providers per user.
  `merge_history` runs after quick/import-all to dedupe same-day movie entries and repoint sync/outbox rows.

---

## 8) API Surface (Current)

### Auth
- `POST /api/auth/register` (if enabled)
- `POST /api/auth/login`
- `POST /api/auth/logout`
- `GET /api/auth/me`

### Settings
- `GET /api/settings`
- `POST /api/settings`

### Integrations
- `GET /api/integrations`
- `POST /api/integrations/letterboxd`
- `POST /api/integrations/letterboxd/test`
- `POST /api/integrations/letterboxd/disconnect`
- `GET /api/integrations/trakt/start`
- `GET /api/integrations/trakt/callback`
- `POST /api/integrations/trakt/disconnect`
- `GET /api/integrations/simkl/start`
- `GET /api/integrations/simkl/callback`
- `POST /api/integrations/simkl/disconnect`
- `POST /api/integrations/stremio/login`
- `POST /api/integrations/stremio/disconnect`
- `POST /api/integrations/import/quick/schedule`
- `POST /api/integrations/import/quick`
- `POST /api/integrations/import/all`

### Metadata
- `GET /api/metadata/providers`
- `POST /api/metadata/providers/{tmdb|tvdb|kitsu|tvmaze|imdb|myanimelist}`
- `POST /api/metadata/providers/{provider}/test`
- `POST /api/metadata/lookup`
- `GET /api/metadata/lookup/{lookup_id}`
- `GET /api/metadata/tv/{provider}/{provider_item_id}/seasons`
- `GET /api/metadata/tv/{provider}/{provider_item_id}/seasons/{season_number}/episodes`

### History
- `POST /api/history/items`
- `GET /api/history/items`
- `PATCH /api/history/items/{watched_id}`
- `DELETE /api/history/items`
- `DELETE /api/history/items/{watched_id}`
- `POST /api/history/items/bulk-delete`
- `POST /api/history/items/sync`

### Activity / Status
- `GET /api/activity/events`
- `GET /api/activity/sessions`
- `GET /api/outbox`
- `GET /api/status`

### Admin (requires `X-API-Key`)
- `POST /api/admin/reset-outbox-jobs`
- `DELETE /api/admin/purge-jobs`

---

## 9) Configuration (Env Vars)

- `DATABASE_URL`
- `LIBRARYSYNC_SECRET_KEY`
- `LIBRARYSYNC_ADMIN_API_KEY`
- `LIBRARYSYNC_BASE_URL`
- `LOG_LEVEL`
- `HISTORY_LOOKBACK_DAYS`
- `LIBRARYSYNC_JWT_ACCESS_TOKEN_MINUTES`
- `LIBRARYSYNC_JWT_ALGORITHM`
- `LIBRARYSYNC_ALLOW_REGISTRATION`
- `TRAKT_CLIENT_ID`, `TRAKT_CLIENT_SECRET`
- `SIMKL_CLIENT_ID`, `SIMKL_CLIENT_SECRET`
- `LIBRARYSYNC_WORKER_MODES`
- `LIBRARYSYNC_WORKER_OUTBOX_CONCURRENCY`
- `LIBRARYSYNC_WORKER_METADATA_CONCURRENCY`
- `LIBRARYSYNC_WORKER_METADATA_CACHE_CONCURRENCY`
- `LIBRARYSYNC_WORKER_QUICK_IMPORT_CONCURRENCY`
- `LIBRARYSYNC_WORKER_IMPORT_ALL_CONCURRENCY`
- `LIBRARYSYNC_TRAKT_RATE_LIMIT_PER_MINUTE`
- `LIBRARYSYNC_SIMKL_RATE_LIMIT_PER_MINUTE`
- `LIBRARYSYNC_LETTERBOXD_RATE_LIMIT_PER_MINUTE`
- `LIBRARYSYNC_STREMIO_RATE_LIMIT_PER_MINUTE`
- `LIBRARYSYNC_TRAKT_MAX_BATCH_SIZE` (default 750): Maximum items per Trakt batch request
- `LIBRARYSYNC_SIMKL_MAX_BATCH_SIZE` (default 750): Maximum items per SIMKL batch request

---

## 10) Security Requirements

- Encrypt secrets at rest using `LIBRARYSYNC_SECRET_KEY` (see `core/security.py`).
- Passwords hashed with bcrypt; enforce 8+ chars and 72-byte max (no truncation).
- OAuth state validation for Trakt/SIMKL.
- Never log raw secrets or tokens.

---

## 11) Observability / Debuggability

- `watch_events`, `outbox`, `sync_attempts`, and `watch_syncs` are the primary audit trail.
- `/api/status` surfaces schedule/queue state for UI.
- Provider responses and payloads are sanitized before storage/logging.

---

## 12) Developer Guidance

- Keep connectors pure: **no DB writes inside connectors**.
- Use `watch_pipeline.py` helpers to enqueue sync jobs.
- Store secrets in `integration_secrets` (encrypted), not `integrations.config`.
- **Use `get_http_client()` from `core/http_client.py` for all HTTP requests**. This ensures consistent User-Agent headers (`librarySync Version/<version>`) across all outbound requests to metadata providers and service integrations.
- Ruff is the linter; line length is 100 (see `pyproject.toml`).
- Do not edit `backend/src/librarysync/static/styles.css` directly; update `frontend/input.css`.
- When updating Tailwind styles in `frontend/input.css`, rebuild `backend/src/librarysync/static/styles.css`.

---

## 13) Tests

- `backend/tests/test_routes_history.py`
- `backend/tests/test_stremio_watched_bitfield.py`
- `backend/tests/test_http_client.py`
- Add tests around outbox transitions, import scheduling, and metadata lookups when changing those.

---

## 14) Known Gaps / Open Items

- CI/CD pipeline still missing (see README TODO).
- UI/UX polish is still pending.
- Progress/scrobble ingestion exists in canonical models but is not wired to the outbox yet.

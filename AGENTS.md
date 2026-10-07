# librarySync — AGENTS.md

## 1) Mission

**librarySync** is a self-hosted, Docker-deployable, multi-user hub for watch history and ratings. It syncs watch history to **Trakt**, **SIMKL**, **Letterboxd**, **Stremio**, **AniList** and **PublicMetaDB**, powered by an async metadata lookup and enrichment pipeline.

---

## 2) Feature Set

- **Authentication**: Multi-user JWT cookies with optional registration toggle and failed-login throttling
- **History Management**: Manual add, update, delete, bulk delete; optional deletion in integrations; mark next released episode of a show as watched
- **Ratings**: History uses 0.5–5.0 stars; independent Watch State ratings retain 0–10 scores and decimals.
- **AIOStreams Watch State v2**: Explicit watched marks, resume, watchlist/drop state, independent ratings, durable bulk marks, consent-based viewers, and diagnostics. The proxy importer is retired; historical records remain readable.
- **Watchlists**: Personal, provider, dropped and list-URL sources with reconciliation
- **Stremio Addon**: Per-user manifest with custom and external catalogs
- **Metadata Providers**: TMDB, TVDB, IMDb, TVMaze, Kitsu, MyAnimeList, AniList, PublicMetaDB (per-user configuration)
- **Metadata Pipeline**: Async lookup and enrichment (posters/IDs) with local cache reuse
- **Import Sources**: Trakt, SIMKL, Letterboxd, Stremio, AniList, PublicMetaDB (quick import and full import)
- **Import Queue**: Priority-ordered per-user queue with post-import deduplication
- **Sync Engine**: Outbox-based delivery with retries, per-user rate limiting, Retry-After handling and configurable batch sizes
- **UI**: Minimal static HTML/JS interface (login, settings, activity, history, watchlist, Stremio addon, dashboard with up-next shows)

---

## 3) Architecture

### Components
- **API**: FastAPI serving JSON endpoints, Jinja page shells and static assets at `/static`
- **Worker**: Async polling loops (see section 7); one process can run all modes or a subset

### Data Flow
1. Manual add, import or Watch State event → `MediaItem`/`EpisodeItem` + `WatchedItem`
2. Append `WatchEvent` for auditing (also used to recognise already-imported entries)
3. Enqueue internal outbox job → provider sync jobs + `WatchSync` rows
4. Worker delivers jobs and records `SyncAttempt` + `WatchSync` status
5. Metadata enrichment fills missing IDs/posters when available

---

## 4) Repository Layout

```
librarySync/
  AGENTS.md / README.md / .env.example / docker-compose*.yml / justfile
  .github/workflows/          # ci (lint, tests incl. Postgres, migrations, CSS, Docker), codeql, publish
  docs/                       # release notes, Watch State testing
  frontend/                   # Tailwind source (input.css) and build (npm run build:css)
  scripts/release.py          # version bump, tag, GitHub release (requires green CI)

  backend/
    Dockerfile                # multi-stage; installs from uv.lock
    alembic.ini
    pyproject.toml
    tests/                    # pytest; Postgres-only tests need WATCH_STATE_TEST_DATABASE_URL
    src/librarysync/
      main.py                 # FastAPI app, page routes, lifespan (secret check, migrations, key rotation)
      worker.py               # worker modes, graceful shutdown, failure backoff
      config.py               # environment settings
      api/                    # routes_<area>.py routers + deps.py (auth/admin dependencies)
      core/                   # domain logic (watch pipeline, imports, watchlists, metadata, Watch State,
                              # auth, security, integration_tokens, url_safety, scheduler, retention helpers)
      connectors/
        services/             # trakt, simkl, letterboxd, stremio, anilist, publicmetadb (+ pagination)
        metadata/             # tmdb, tvdb, imdb, tvmaze, kitsu, myanimelist, anilist, publicmetadb
      jobs/                   # worker job implementations (process_outbox, *_import, metadata_*, retention, ...)
      db/                     # models, session, migrations/, watch_state_triggers
      static/                 # core.js + page-*.js (classic scripts), service-worker.js, built styles.css
      templates/              # base.html + page templates, settings/ partials
```

---

## 5) Data Model

### Core Tables
- **`users`**: Authentication and per-user settings (e.g., include adult content in search)
- **`integrations`**: Per-user provider configurations (the pseudo-provider `system` row holds import scheduling state)
- **`integration_secrets`**: Encrypted provider credentials
- **`media_items`**: Canonical movie/show catalog
- **`episode_items`**: Canonical episode catalog
- **`watched_items`**: Per-user watch history (watched_at, rating, source)
- **`watchlist_items`**, **`watchlist_sources`**, **`watchlist_source_items`**: Watchlists and where items came from
- **`blacklist_items`**: Items excluded from imports

### Audit & Sync
- **`watch_events`**: Append-only event log for imports and manual changes
- **`watch_syncs`**: Per-provider sync status with external IDs and error details
- **`outbox`**: Delivery queue for sync jobs
- **`sync_attempts`**: Attempt history for deliveries

### Watch State & Addon
- **`watch_state_*`** (receipts, entries, viewers, revisions, snapshots): AIOStreams Watch State inbox and projections
- **`stremio_addon_configs`**, **`stremio_custom_catalogs`**, **`stremio_external_catalogs`** (+ items)

### Metadata & Jobs
- **`metadata_lookup_requests`** / **`metadata_lookup_candidates`**: Async lookup pipeline
- **`scheduled_jobs`**: Job leases for recurring worker tasks
- **`rate_limit_buckets`**: Per-user/provider token buckets

### Legacy
- **`progress_events`**: Progress model (not yet wired to outbox)

---

## 6) Integrations & Metadata Providers

### Service Integrations (Sync + Import)
- **Trakt**, **SIMKL**, **AniList**: OAuth authentication
- **Letterboxd**: Client credentials with a rotating refresh token
- **Stremio**: Auth key
- **PublicMetaDB**: API key

### Metadata Providers (Lookup Only)
- **TMDB**: API key (v3) or read access token (v4, sent as a Bearer header)
- **TVDB**: API key required, optional PIN
- **IMDb**, **TVMaze**, **Kitsu**, **MyAnimeList**: No authentication required

**Storage**: Provider configurations in `integrations` table; sensitive credentials in `integration_secrets` (encrypted).

---

## 7) Worker Modes & Jobs

### Worker Modes
Configured via `LIBRARYSYNC_WORKER_MODES` (`all` or a comma-separated subset): `outbox`, `metadata`, `metadata_backfill`, `metadata_cache`, `quick_import`, `import_all`, `watch_state`, `external_catalog_refresh`, `watchlist`, `merge_history`, `merge_all_history`, `retention`.

On SIGTERM/SIGINT loops finish their current iteration and release unprocessed claimed outbox jobs and metadata lookups. A failing loop backs off exponentially (max five minutes).

### Outbox Processing (`process_outbox`)
- Job types: `push_watched`, `push_rating`, `remove_rating`, `update_history`, `remove_history`, `update_log_entry`, `delete_log_entry`, `remove_watched`, `push_watchlist`, `remove_watchlist`, and internal `new_item_added` / `watchlist_update`
- Claims interleave users round-robin; Trakt/SIMKL `push_watched`/`push_rating` are batched (one rate-limit token per batch)
- `dedupe_key` is unique only among waiting jobs (`pending`, `failed_retryable`). A changed payload for a target already in flight becomes a successor that runs after it; a failed predecessor becomes `superseded`. Finished jobs never keep a key.
- Retryable failures back off up to an hour and give up after `LIBRARYSYNC_OUTBOX_MAX_ATTEMPTS`; HTTP 429 honours `Retry-After` without using an attempt; a 401 from an OAuth provider expires the stored token and retries
- Jobs stuck `in_progress` longer than `LIBRARYSYNC_OUTBOX_STALE_MINUTES` are requeued
- Deleting watches cancels their queued pushes (`cancel_queued_pushes`)

#### Watch State Inbox
- `watch_state` drains durable bulk receipts and mapping retries.
- Public addon routes serialize changes per user and enforce addon opt-in and viewer consent.
- `WatchStateEntry` separates watched, playback, watchlist, drop and rating projections.
- Database revision triggers invalidate complete snapshots for imports and direct SQL changes.

#### Metadata Jobs
- **`metadata_lookup`**: Resolves lookup requests into candidates (stale in-progress lookups are reclaimed after five minutes)
- **`metadata_cache`**: Scans recent candidates and seeds `media_items` to accelerate search
- **`metadata_backfill`**: Periodically refreshes metadata/enriches watched history and episode lists that are missing posters or identifiers

#### Import Jobs
- **`quick_import`**: Runs 7-day import window on the user's configured schedule (30 min to 7 days). Due runs are selected from an unlocked scan, then locked. Per-user runs are single-flight via a lease stored in the integration config (10-minute expiry, refreshed at each claim); an expired lease lets any worker resume a stuck run from its saved queue index
- **`import_all`**: Sequences providers per user for full import, single-flight via a two-hour lease renewed at each provider step
- **Watchlist reconciliation**: removals are only reconciled against complete listings. A failed fetch, a failed SIMKL category or a page-capped listing (`PagedEntries.truncated`) never deletes items
- **Dropped ingestion**: Trakt (`GET /users/hidden/dropped?type=show`) and SIMKL (`status: "dropped"` in `/sync/all-items`) dropped shows are imported into the terminal `dropped` watchlist status, tracked via a per-provider `WatchlistSource` (`external_id="dropped"`); reconcile un-drops shows that leave the provider's dropped list. Import upserts never resurrect dropped items (`restore_dropped=False`)
- **`merge_history`**: Post-import deduplication (same-day movie entries) and repoints sync/outbox rows
- **`merge_all_history`**: Periodic deduplication of all user history in database (API merges on-the-fly until DB is clean)

#### Maintenance
- **`retention`**: Daily deletion of finished outbox jobs (`LIBRARYSYNC_OUTBOX_RETENTION_DAYS`) and metadata lookups (`LIBRARYSYNC_LOOKUP_RETENTION_DAYS`). `watch_events` and Watch State receipts are kept because they deduplicate imports and events.

---

## 8) API Surface

Routers live in `api/routes_<area>.py`; the OpenAPI schema at `/docs` is the complete reference. Main groups:

| Prefix | Router | Purpose |
| --- | --- | --- |
| `/api/auth` | `routes_auth` | register, login (throttled), logout, me |
| `/api/settings` | `routes_settings` | per-user settings |
| `/api/integrations` | `routes_integrations` | connect/disconnect providers (OAuth start/callback for Trakt, SIMKL, AniList), quick import schedule and triggers, import-all |
| `/api/metadata` | `routes_metadata` | provider configuration/tests, lookups, season/episode listings |
| `/api/history` | `routes_history` | list, add, update, delete, bulk delete, sync, mark next episode |
| `/api/watchlist` | `routes_watchlist` | items (drop, rewatch, mark watched) and sources |
| `/api/blacklist` | `routes_blacklist` | import blacklist |
| `/api/dashboard` | `routes_dashboard` | stats and up-next |
| `/api/activity`, `/api/outbox`, `/api/status` | `routes_activity` | events, sessions, queue state |
| `/api/stremio-addon` | `routes_stremio_addon`, `routes_addon_watch_state` | addon config, custom/external catalogs, Watch State status, viewers, ratings, diagnostics |
| `/stremio-addon/{addon_id}/…` | `routes_stremio_addon_public`, `routes_addon_watch_state` | public manifest, catalogs, Watch State pull/push (authenticated by the addon id in the URL) |
| `/api/admin` | `routes_admin` (requires `X-API-Key`) | reset/purge outbox jobs, merge history, metadata backfill/cache, watchlist refresh, media id fixes |

---

## 9) Configuration

See `.env.example` and the README for every variable and default. Highlights:

- **Core**: `DATABASE_URL`, `LIBRARYSYNC_SECRET_KEY` (required, no placeholders), `LIBRARYSYNC_SECRET_KEY_PREVIOUS` (rotation), `LIBRARYSYNC_ADMIN_API_KEY`, `LIBRARYSYNC_BASE_URL`, `LOG_LEVEL`
- **Auth**: `LIBRARYSYNC_JWT_ACCESS_TOKEN_MINUTES`, `LIBRARYSYNC_JWT_ALGORITHM`, `LIBRARYSYNC_ALLOW_REGISTRATION`, `LIBRARYSYNC_MAX_USERS`
- **Network safety**: `LIBRARYSYNC_ALLOW_PRIVATE_URLS` (opt out of the SSRF guard for LAN addons)
- **OAuth**: `TRAKT_*`, `SIMKL_*`, `ANILIST_*` client credentials
- **Worker**: `LIBRARYSYNC_WORKER_MODES`, `LIBRARYSYNC_WORKER_<MODE>_CONCURRENCY`, `LIBRARYSYNC_WORKER_ID`
- **Outbox**: `LIBRARYSYNC_OUTBOX_MAX_ATTEMPTS`, `LIBRARYSYNC_OUTBOX_STALE_MINUTES`, `LIBRARYSYNC_OUTBOX_RETENTION_DAYS`, `LIBRARYSYNC_LOOKUP_RETENTION_DAYS`
- **Rate limits / batches**: `LIBRARYSYNC_<PROVIDER>_RATE_LIMIT_PER_MINUTE`, `LIBRARYSYNC_TRAKT_MAX_BATCH_SIZE`, `LIBRARYSYNC_SIMKL_MAX_BATCH_SIZE`

---

## 10) Security Requirements

- **Secrets**: Encrypt at rest using `LIBRARYSYNC_SECRET_KEY` (see `core/security.py`; MultiFernet with previous keys for rotation). Startup refuses empty/placeholder keys.
- **Passwords**: Bcrypt hashing (in a worker thread) with 8+ character minimum and 72-byte maximum (rejected, not truncated)
- **Sessions**: HttpOnly SameSite=Lax cookie (Secure on https); CORS never allows credentials
- **OAuth**: State validation (constant-time) for Trakt, SIMKL and AniList flows
- **OAuth tokens**: Refresh through `core/integration_tokens.py` only (row-locked, committed immediately); never refresh and discard a rotating token
- **Outbound requests to user-supplied URLs**: Validate with `core/url_safety.py` (`ensure_public_url` / `ensure_public_host`)
- **Responses**: Security headers and a CSP for pages (`core/security_headers.py`); inline scripts must be plain `<script>` blocks so their hashes are allowed
- **Logging**: Never log raw secrets or tokens; redact credentials in URLs (`redact_secrets`); httpx request logging stays at WARNING

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
- **Sync Jobs**: Use `watch_pipeline.py` helpers to enqueue sync jobs; use `cancel_queued_pushes` when deleting watches
- **Secrets**: Store in `integration_secrets` (encrypted), never in `integrations.config`
- **HTTP Requests**: Always use `get_http_client()` from `core/http_client.py` to ensure consistent User-Agent headers (`librarySync Version/<version>`)
- **Pagination feeding reconciliation**: Return `PagedEntries` and skip removals when `truncated`
- **Closures in loops**: Bind loop variables explicitly (Ruff `B023` is enabled)

### Common Tasks
- **New integration**: connector in `connectors/services/`, OAuth/credential routes in `api/routes_integrations.py`, token refresh via `core/integration_tokens.py`, sync strategy in `core/watch_pipeline.py`, delivery in `jobs/process_outbox.py`, import strategy in `jobs/`, rate limit in `config.py`
- **New metadata provider**: provider in `connectors/metadata/`, register in `core/metadata_providers.py`, configuration in `api/routes_metadata.py`
- **Schema change**: update `db/models.py` and add an Alembic migration; CI runs `alembic upgrade head`, `alembic check` and a downgrade/upgrade round trip

### Tooling
- **Linter/formatter**: Ruff with 120-character lines (`E, F, I, B, UP, ASYNC, DTZ, RUF, S`); `ruff format` is enforced in CI
- **Styles**: Edit `frontend/input.css` (not `backend/src/librarysync/static/styles.css` directly), then `npm run build:css` and commit the output; CI checks it is current
- **Shortcuts**: `just sync|test|lint|fmt|css|dev-api|dev-worker`; pre-commit runs Ruff

---

## 13) Tests

- `backend/tests/` covers routes, outbox lifecycle and providers, imports and claims, watchlists, Watch State, metadata, security and templates (57 files)
- Postgres-only tests (Watch State triggers, migrations) run when `WATCH_STATE_TEST_DATABASE_URL` points at a disposable database; CI provides one
- Add tests for outbox transitions, import scheduling, and metadata lookups when modifying those areas; prefer SQLite-backed tests over mocks for claim/lease logic

---

## 14) Known Gaps

- Long database transactions around provider HTTP calls (imports, outbox) — split read/HTTP/write phases
- `process_outbox.py` and the import jobs are large and share duplicated item-resolution code; a shared episode identity/resolver is deferred
- Provider connectors have no HTTP-level (respx/MockTransport) test suite yet
- No metrics endpoint or worker heartbeat; migrations at API startup are not guarded by an advisory lock
- Progress/scrobble ingestion (exists in models but not wired to outbox)

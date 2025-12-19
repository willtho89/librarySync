# librarySync — AGENTS.md

## 1) Mission / Summary

Build **librarySync**, a self-hosted, Docker-compose-deployable, **multi-user** synchronization hub that:

- **Ingests watch progress** from media players (MVP: **Stremio via AIOStreams add-on**).
- Normalizes events into a **canonical progress/watched model**.
- **Syncs watched/progress** to downstream tracking services (MVP: **Trakt + SIMKL**).
- Runs background jobs for polling, syncing, retries, and (later) drift reconciliation.
- Provides a **minimal web UI** (no frontend frameworks; plain JS + server-served static HTML).

Scope for MVP: **watched + progress only** (no collection/library syncing, no deletes).

Core requirements:
- **Pluggable connectors** for players and services.
- **OAuth** for downstream services where possible (Trakt, SIMKL).
- Credentials stored in DB, **encrypted at rest**.
- Observable and debuggable: event log, outbox status, last errors.

Repository style: **monorepo** named `librarySync`.

---

## 2) MVP Feature Set (Must Have)

### Player ingest (MVP)
- Poll AIOStreams "active sessions" endpoint on a configurable interval (e.g. 10–30s).
- Parse AIOStreams JSON similar to:

https://aiostreams.saladprecedestretch123.uk/api/v1/proxy/stats |jq .users.username.active
    ```json
    [
        {
            "ip": "80.187.74.90",
            "url": "https://addon.debridio.com/play/serie/torbox/506386038130ecedba49e71e8cd3f4d0/4b6d8076-4e66-472d-928a-e5928ba6c2f3/33238ed6676afb691a6ae3554f1ffc59b08bf72a/Gen.V.S02E04.Bags.2160p.AMZN.WEB-DL.DDP5.1.Atmos.DV.HDR.H.265-G66.mkv",
            "filename": "Gen.V.S02E04.Bags.2160p.AMZN.WEB-DL.DDP5.1.Atmos.DV.HDR.H.265-G66.mkv",
            "timestamp": "2025-12-16T17:23:37.344Z",
            "lastSeen": "2025-12-16T17:45:19.002Z",
            "count": 5,
            "requestIds": [
            "rz8c6e"
            ],
            "metaId": "tt13159924:2:4",
            "relativeTimestamp": "21m 50s ago",
            "relativeLastSeen": "8.88s ago",
            "progress": 51.9
        },
        //…
    ]
    ```
- Interpret `imdbId`:
  - `tt1234567` => movie
  - `tt1234567:season:episode` => episode (show/anime treated as episode for MVP)
- Generate canonical progress events with dedupe/coalescing to avoid spamming downstream APIs.
- Determine completion using a configurable threshold (default 85%).

### Downstream sync (MVP)
- Trakt OAuth connect + token refresh + push:
  - progress/scrobble if supported reasonably
  - watched/completed marking on completion
- SIMKL OAuth connect + push:
  - watched/completed marking
  - progress where supported (fallback to watched-only if needed)
- Outbox-based delivery with retries and per-provider rate limiting.

### Multi-user (MVP)
- Multi-user data model from day one.
- Local authentication for the web UI (simple username + password for MVP).
- Each user configures their own integrations (AIOStreams endpoint + OAuth connections).

### UI (MVP)
- Very small set of pages; plain JS (no frameworks):
  - Login
  - Integrations (connect/disconnect, test connection)
  - Settings (poll interval, completion threshold, source-of-truth placeholders)
  - Activity (recent events + sync status)

---

## 3) Non-Goals (MVP)

- No deletions/unwatch actions pushed to services.
- No full "collection/library" sync (only watched/progress).
- No fancy UI frameworks.
- No mobile app.
- No universal metadata perfection. Use pragmatic ID mapping (IMDb-first) and cache results.

---

## 4) Architecture Overview

### Components
- **Backend API**: Python (FastAPI recommended)
- **Worker**: background processing loop(s) for:
  - polling AIOStreams
  - processing outbox jobs
  - retries/backoff
  - (later) daily drift reconciliation
- **DB**: PostgreSQL
- **Web UI**: static HTML + plain JS served by backend (or via lightweight static route)
- **Deployment**: Docker + Docker Compose

### Core patterns
- **Connector plugin interfaces**:
  - Players: "read sessions/progress"
  - Services: "OAuth + push watched/progress + optional pull later"
- **Canonical models** between connectors and orchestration code.
- **Outbox pattern**:
  - Ingest creates canonical `ProgressEvent`
  - Orchestrator enqueues outbox jobs
  - Worker delivers jobs to Trakt/SIMKL, records attempts

---

## 5) Canonical Domain Model (Conceptual)

### PlaybackSession (from player connector)
- `session_key`: stable identifier for a session (prefer requestId; otherwise hash of key fields)
- `user_id`
- `provider`: `aiostreams`
- `imdb_id_raw`: e.g. `tt0472954:10:7`
- `imdb_id`: base IMDb ID e.g. `tt0472954`
- `media_type`: `movie` | `episode`
- `season`: int? (nullable)
- `episode`: int? (nullable)
- `progress_percent`: float (0–100)
- `first_seen_at`: datetime
- `last_seen_at`: datetime
- `url`, `filename`, `raw`

### ProgressEvent (append-only)
- `event_id`
- `user_id`
- `source_provider`: `aiostreams`
- `item_key`: canonical key (e.g. `imdb:tt123` or `imdb:tt123:s10e7`)
- `event_type`: `progress` | `completed`
- `progress_percent`: float?
- `occurred_at`: datetime (prefer lastSeen)
- `session_key`
- `raw` json

### ItemState (derived state)
- `user_id`
- `item_key`
- `last_progress_percent`
- `completed_at` (nullable)
- `last_seen_at`

### OutboxJob
- `job_id`
- `user_id`
- `target_provider`: `trakt` | `simkl`
- `job_type`: `push_progress` | `push_completed`
- `payload` (canonical)
- `status`: `pending` | `in_progress` | `succeeded` | `failed_permanent` | `failed_retryable`
- `run_after`, `attempts`, `last_error`

### ItemMapping (provider ID cache)
- `item_key`
- `provider` (`trakt`/`simkl`)
- provider-specific IDs needed to perform updates
- `raw` json
- `updated_at`

---

## 6) Sync Rules (MVP)

### Dedupe/coalescing rules
- Progress events should not be emitted on every poll.
- Emit `progress` only if:
  - progress increased by at least `Δp` (default 1.0%), OR
  - time since last emitted progress >= `T` seconds (default 60s)
- Never decrease stored progress: `effective_progress = max(previous, current)`.

### Completion rule
- When `progress_percent >= completion_threshold` (default 85%):
  - emit a `completed` event once per item/user
  - enqueue watched marking for Trakt and SIMKL
- If session disappears:
  - optional grace completion if last progress was near threshold (configurable later)

### Add-only guarantee
- Never unwatch or delete on downstream services in MVP.

---

## 7) Connectors (Pluggable Interfaces)

### PlayerConnector interface (conceptual)
- `fetch_active_sessions(user) -> list[PlaybackSession]`
- `validate_config(config) -> bool` (test endpoint / credentials)

MVP player connector: `AIOStreamsConnector`

### ServiceConnector interface (conceptual)
- OAuth:
  - `oauth_start(user) -> redirect_url`
  - `oauth_callback(user, code/state) -> store tokens`
  - `refresh_token_if_needed(user)`
- Push:
  - `push_progress(user, event)`
  - `push_completed(user, event)`
- Optional later:
  - `pull_watched_state(user, since)`

MVP service connectors: `TraktConnector`, `SimklConnector`

---

## 7b) Metadata Providers (Sprint 2)

Add a new connector category: metadata providers.

### MetadataProvider interface (conceptual)
- `search_movie(query, user) -> list[MovieCandidate]`
- `find_movie_by_external_id(external_id, user) -> list[MovieCandidate]`
- `get_movie_details(provider_id, user) -> MovieCandidate`
- `normalize_candidate(raw) -> MovieCandidate`

MVP metadata provider: `TmdbMetadataProvider`
Optional stub (no functionality required in Sprint 2): `TvdbMetadataProvider`

---

## 8) Repository Layout (Monorepo)

```
librarySync/
  AGENTS.md
  README.md
  docker-compose.yml
  .env.example

  backend/
    pyproject.toml
    src/librarysync/
      main.py                  # FastAPI app entry
      config.py                # env/config handling
      db/
        session.py
        models.py
        migrations/            # Alembic
      api/
        routes_auth.py
        routes_integrations.py
        routes_activity.py
        routes_settings.py
      core/
        canonical.py           # canonical dataclasses/types
        security.py            # encryption helpers for tokens
        dedupe.py              # progress dedupe/coalesce logic
        outbox.py              # enqueue helpers
        matching.py            # ID mapping cache and lookup
      connectors/
        players/
          base.py
          aiostreams.py
        services/
          base.py
          trakt.py
          simkl.py
        metadata/
          base.py
          tmdb.py
          tvdb.py
      jobs/
        poll_aiostreams.py
        process_outbox.py
        drift_daily.py         # stub for MVP, can be disabled initially
      static/
        index.html
        login.html
        integrations.html
        activity.html
        settings.html
        app.js
        styles.css
      templates/               # optional (if server-rendered pages are used)

  worker/
    pyproject.toml             # can share backend package or separate
    src/librarysync_worker/
      main.py                  # worker entrypoint that imports backend modules
```

Notes:
- Prefer a single Python package shared by API + worker to avoid duplication.
- Worker can be a separate entrypoint that reuses `librarysync.*`.

---

## 9) Tech Stack Decisions (Default)

- Python 3.14+ (managed with uv)
- FastAPI + Uvicorn
- Async-first: use async/await in API endpoints, worker loops, and connectors; avoid
  blocking I/O in request handlers and jobs.
- SQLAlchemy 2.x + Alembic
- PostgreSQL 16+
- Plain JS frontend (served from `/static`)
- Docker + Docker Compose

Queue/backing store:
- MVP can do DB-backed outbox polling (simpler).
- Optional later: Redis-based queue if needed, but not required for MVP.

---

## 10) Security Requirements

- Store OAuth tokens/API keys **encrypted at rest**.
  - Encryption key provided via environment variable (e.g., `LIBRARYSYNC_SECRET_KEY`).
- Passwords hashed (bcrypt/argon2).
- CSRF/State protection for OAuth flows.
- Do not log sensitive tokens.
- Ensure endpoints are authenticated (except OAuth callbacks and health).

### Password Handling (Required)
- Enforce minimum length (>= 8 characters) and reject empty/too-short passwords.
- Enforce bcrypt input limit: reject passwords > 72 bytes when UTF-8 encoded; **do not truncate**.
- Hash with a modern KDF (bcrypt for MVP; prefer Argon2id when feasible).
- Verify with constant-time helpers (passlib) and treat any verify errors as auth failures.
- Never log, echo, or return raw passwords; return clear validation errors only on registration.

---

## 11) Observability / Debuggability Requirements

- Store raw payload snapshots for:
  - AIOStreams session entries (sanitized)
  - downstream API responses (sanitized)
- Expose in UI:
  - last poll time per user
  - outbox queue size
  - last error per integration
- Provide structured logs (JSON preferred) for container logs.

---

## 12) Configuration (Env Vars)

MVP env vars (example names; finalize during implementation):
- `DATABASE_URL`
- `LIBRARYSYNC_SECRET_KEY` (encryption)
- `LIBRARYSYNC_BASE_URL` (for OAuth callback URLs)
- `TRAKT_CLIENT_ID`, `TRAKT_CLIENT_SECRET`
- `SIMKL_CLIENT_ID`, `SIMKL_CLIENT_SECRET`
- `POLL_INTERVAL_SECONDS` (default 60)
- `COMPLETION_THRESHOLD_PERCENT` (default 85)
- `LOG_LEVEL`

Per-user AIOStreams config stored in DB:
- `aiostreams_base_url`
- `aiostreams_api_key` (if used)

---

## 13) API Endpoints (MVP Target)

### Auth
- `POST /api/auth/login`
- `POST /api/auth/logout`
- `GET /api/me`

### Integrations
- `GET /api/integrations`
- `POST /api/integrations/aiostreams` (save config)
- `POST /api/integrations/aiostreams/test`
- `GET /api/integrations/trakt/start`
- `GET /api/integrations/trakt/callback`
- `POST /api/integrations/trakt/disconnect`
- `GET /api/integrations/simkl/start`
- `GET /api/integrations/simkl/callback`
- `POST /api/integrations/simkl/disconnect`

### Activity / Status
- `GET /api/activity/events?limit=100`
- `GET /api/activity/sessions`
- `GET /api/outbox?status=pending|failed|succeeded`
- `GET /api/status` (poller stats, last run times)

---

## 14) Implementation Plan (Execution Order)

1. **Repo bootstrap**: docker compose, backend skeleton, DB connectivity, migrations.
2. **Multi-user auth**: login, user model, session/JWT.
3. **Integrations framework**: `integrations` table, encryption helpers, UI wiring.
4. **Trakt OAuth**: connect + test call.
5. **SIMKL OAuth**: connect + test call.
6. **AIOStreams poller**:
   - fetch active sessions
   - session tracking
   - dedupe + generate `ProgressEvent`
7. **Outbox + delivery worker**:
   - enqueue jobs per event per provider
   - implement Trakt push (completed first; progress second)
   - implement SIMKL push (completed first; progress second)
8. **UI activity + status**: show what’s happening; show failures.
9. **Hardening**: retries/backoff, rate limiting, mapping cache, logging.
10. (Optional MVP+) **Daily drift job** (watched-only add-only) once push pipeline is stable.

---

## 15) Coding Standards / Agent Guidance

- Keep connectors pure: **no DB writes inside connectors**.
- Canonicalize early:
  - parse AIOStreams -> canonical session
  - canonical session -> canonical event
  - canonical event -> outbox jobs
- Use Ruff for linting/import sorting with a 100-character line length.
- Prefer deterministic keys:
  - `item_key` format:
    - movie: `imdb:tt1234567`
    - episode: `imdb:tt1234567:s10e07` (zero-pad episode)
- Never spam downstream:
  - coalesce progress updates
  - prefer completion accuracy over frequent progress writes
- Always record:
  - what was attempted
  - provider response status
  - sanitized error body (if safe)
- Write integration tests for:
  - dedupe logic
  - item_key parsing
  - outbox retry transitions

---

## 16) Known Risks / Open Items

- AIOStreams progress units must be confirmed (assume percent 0–100 until verified).
- Trakt/SIMKL episode mapping may require search calls and caching.
- Some services have different semantics for "scrobble" vs "watched". Implement watched first, then progress.

---

## 17) Definition of Done (MVP)

A user can:

- Deploy with `docker compose up`.
- Log in to the web UI.
- Configure AIOStreams endpoint and connect Trakt + SIMKL via OAuth.
- Start playback in Stremio; librarySync detects progress and emits events.
- On completion threshold, librarySync marks the item watched in Trakt and SIMKL.
- UI shows recent events and whether sync succeeded or failed.
- Failures are retried and visible; tokens are stored encrypted.

---

## 18) Sprint 2 - Manual Watched Movies + Single Lookup Flow

### Sprint 2 outcome
Logged-in users can manually add a watched movie through a single, consistent flow:

1. User input: user enters either a title (free text) or a known ID (IMDb `tt...`, TMDB ID).
2. Get info: system performs an asynchronous metadata lookup against the user's enabled
   metadata providers (start with TMDB).
3. User selects + confirms: user picks the correct match from a candidate list and confirms
   a watch date.
   - DateTime input optional.
   - If None: default to "started watching now" (use server time).
4. System stores the result locally as the user's watched history (future sprint: sync out).

### Scope now vs. later
Now (Sprint 2):
- Local source of truth for watched movies inside the app.
- Metadata lookup pipeline (async, provider-pluggable).
- UI and API stable enough that future downstream syncing is another consumer.

Later (future sprints):
- Add Trakt/SIMKL connectors + sync worker that pushes local watched events.
- Add player ingestion (AIOStreams) as another source of watch events.
- Add drift detection and reconciliation.

### User-facing features (Sprint 2)
1) Metadata provider settings (per user)
- Enable/disable TMDB metadata provider.
- Provide credentials per user (no instance-wide keys).
- Optional: provider preferences like language/region (minimal in MVP).

2) Manual "Add watched movie" page
- Input field: "Title or ID".
- Search button.
- Results area: shows candidates (title/year/poster if available).
- User selects one candidate.
- Confirmation step:
  - Watch date input optional.
  - If empty: default to "now" as started watching time.
  - Confirm button.

3) History page
- Shows the user's watched movies added manually (most recent first).

### System behavior (Sprint 2)
Single flow, multiple query strategies:
- Looks like IMDb ID (`tt\\d+`) -> use provider "find by external ID" where available (TMDB).
- Otherwise treat as title query -> use provider search (TMDB movie search).

TMDB workflow (MVP-friendly):
- Search first, then fetch details for the selected candidate (or top N candidates).

Async lookup (required):
- Create lookup request -> poll status endpoint until ready -> render candidates.
- Keep the UI flow consistent across providers.

### Minimal API surface (Sprint 2)
Settings:
- List enabled metadata providers for current user.
- Enable/disable provider + save credentials (per user).
- Test provider (optional but helpful).

Lookup flow:
- Create lookup request from user input (title or ID).
- Get lookup status + candidates.
- Confirm selection + watch date.

History:
- List watched movies for the logged-in user.

### Persistence goals (Sprint 2)
Store:
- Canonical representation of a movie (with common IDs like IMDb/TMDB).
- Per-user watched record (including optional watch datetime; default to now).
- Append-only log/event representing "user marked movie watched manually".
- Lookup requests + candidate results (for async polling and debugging).

### Worker responsibilities (Sprint 2)
- Pick up pending lookup requests.
- Call enabled metadata providers for that user.
- Store candidates.
- Mark lookup request as complete or failed.

### Acceptance criteria (Sprint 2)
- A user can enable TMDB provider and store their credentials (per user).
- A user can input a movie title, get candidates, pick one, confirm watch datetime (or leave blank).
- The watched movie appears in the user's history.
- The lookup flow is asynchronous (request -> poll -> results).
- Provider design is extensible (adding TVDB later doesn't change the UI flow).

### TMDB notes (Sprint 2)
- Use TMDB movie search endpoint for title-based searches.
- Use TMDB external-ID find capability when user supplies an IMDb ID.
- Prefer the "search then query details" workflow for better confirmation data.

# librarySync: overview and improvement plan

*Review date: 2026-10-06. Reviewed version: v0.21.0 (`0e734e8`).*

This plan comes from a read-only review of six areas: security, the sync engine and worker, connectors and metadata, the frontend, tests and CI, and docs and hygiene. The items marked **✔ verified** were checked directly against the code during the review. Every other item was traced to file:line by a reviewer but not checked a second time. Effort estimates are **S** (under half a day), **M** (one to three days) and **L** (a week or more).

---

## 1. What the project is

librarySync is a self-hosted, multi-user hub for watch history. It keeps watch history, ratings and watchlists in sync between **Trakt, SIMKL, Letterboxd, Stremio, AniList and PublicMetaDB**. It also implements the **AIOStreams Watch State v2** protocol (an addon endpoint for watched marks, resume points and ratings), so Stremio and AIOStreams can use librarySync as their tracker.

| Part | What it is |
| --- | --- |
| API | FastAPI app (`main.py`) with 13 routers: auth, history, watchlist, metadata, integrations, dashboard, settings, blacklist, admin, Stremio addon (private and public), and AIOStreams watch-state |
| Worker | `worker.py` runs 11 async polling loops: outbox, metadata lookup, backfill and cache, quick import, import-all, watch state, watchlist refresh, external catalogs, and history merging |
| Storage | Postgres through async SQLAlchemy 2 and Alembic, with 29 ORM models and 23 migrations. Provider credentials are encrypted with Fernet in `integration_secrets` |
| Delivery | Transactional outbox: `outbox` jobs, then `sync_attempts` and `watch_syncs`, with retries and per-user token-bucket rate limits |
| Metadata | Lookup and enrichment from TMDB, TVDB, IMDb, TVMaze, Kitsu, MAL, AniList and PublicMetaDB |
| UI | Jinja shells plus about 10k lines of vanilla JS (`static/page-*.js`) and Tailwind v4 CSS, with a PWA manifest and service worker |
| Ops | Docker images on GHCR, Compose with Postgres and split worker profiles, and GitHub Actions for CI, CodeQL, dependency review and publishing |

**Size and health snapshot**
- About 41k lines of Python (excluding migrations), about 10k lines of JS, and about 9.5k lines of tests in 43 files.
- 393 tests pass and 6 are skipped (the Postgres-only and live end-to-end tests), in 12 s.
- `ruff check` is clean. `ruff format --check` would reformat 87 files. mypy reports 271 errors, and no type checker is configured.
- There is one maintainer and 53 release tags. Activity comes in bursts (Jan, Apr, Jul and Oct 2026).
- Open issues: [#97 Up Next ordering](https://github.com/willtho89/librarySync/issues/97), [#99 SIMKL calendar v2](https://github.com/willtho89/librarySync/issues/99) (the old calendar files stop updating on **1 Feb 2027**), and [#14 AniList gaps](https://github.com/willtho89/librarySync/issues/14). Copilot has opened draft PRs [#98](https://github.com/willtho89/librarySync/pull/98) and [#100](https://github.com/willtho89/librarySync/pull/100).

**Strengths to keep:**
- Data is consistently scoped per user, and no IDOR was found.
- No SQL is built from strings.
- OAuth state handling and viewer-invite tokens are solid.
- Secrets are encrypted at rest.
- The outbox pattern gives an audit trail.
- The container runs as a non-root user.
- The test suite is fast.
- Release notes are thorough for recent versions.

---

## 2. P0: correctness and data-loss bugs (fix first)

| # | Issue | Where | Effort |
| --- | --- | --- | --- |
| 1 | **Stremio show import attaches every episode to the last episode.** The `_build_items` closure captures the loop variable `episode_item`, and `process_import_candidates` runs only after the loop ends. Ruff B023 flags this. ✔ verified | `jobs/stremio_import.py:620` | S |
| 2 | **A failed Trakt watchlist fetch wipes the local Trakt watchlist.** `TraktError` produces `entries=[]`, and the code then calls `reconcile_watchlist_source(seen_item_ids=[])`. If only one of the two types (movies or shows) fails, items of that type are dropped. ✔ verified | `jobs/trakt_import.py:266-303` | S |
| 3 | **Page caps combined with reconcile drop the end of large lists.** The cap is 10×50 for Trakt quick import and always 10×50 for Letterboxd. Items after number 500 are never marked as seen, so each run reconciles them away. | `trakt_import.py:62`, `letterboxd_import.py:70`, `letterboxd.py:626` | S |
| 4 | **Batched outbox jobs keep their `dedupe_key`.** The next enqueue for the same item inserts a duplicate key and raises `IntegrityError`. The single-job path clears the key; the batch path does not. ✔ verified | `jobs/process_outbox.py:618-651` vs `:786` | S |
| 5 | **A rating change made while a job is in flight is silently lost.** Enqueue returns the existing `in_progress` job and discards the new payload. Jobs are claimed 50 at a time, so this window can last minutes. ✔ verified | `core/watch_pipeline.py:102-110` | M |
| 6 | **Outbox jobs stuck in `in_progress` are never recovered.** There is no lease or reaper. There is no SIGTERM handler, and no init process in Compose, so `docker stop` kills the worker and strands up to 50 claimed jobs. Enqueue then dedupes against those stranded jobs, so the affected items never sync again. A swallowed DB error also poisons the session for the rest of the batch. | `process_outbox.py:97,702,774`, `worker.py:91-117` | M |
| 7 | **Removing a show from the SIMKL watchlist calls `/sync/history/remove`.** This can erase SIMKL watch history for a partly watched show. SIMKL may have no endpoint that removes from a list only, so if this is deliberate, guard it when local watches exist and rename the misleading `remove_from_watchlist`. ✔ verified | `process_outbox.py:1523`, `connectors/services/simkl.py:231-238` | S |
| 8 | **OAuth token refresh can race.** The refresh logic is copied six times and no copy locks the secret row. Two loops can spend the same single-use Trakt refresh token. A 401 or 400 is treated as `failed_permanent`, and there is no "needs re-authentication" state. | `process_outbox.py:1033,3134,3156`, `trakt_import.py:1279`, `simkl_import.py:1956`, `letterboxd_import.py:384` | M |
| 9 | **Letterboxd discards rotated refresh tokens** during catalog refresh. | `letterboxd.py:86-115`, `core/external_catalog.py:1047` | S |
| 10 | **Quick-import claiming can starve users.** It selects the 5 oldest rows and filters them in Python. Users whose schedule is disabled stay at the front of the queue forever. | `jobs/imports.py:155-176` | S |
| 11 | **`import_all` has no lease.** Running the `all` worker together with the `worker-import-all` profile duplicates imports. | `jobs/imports.py:198-212` | S |
| 12 | **Deleting history: Cancel in the second `confirm()` means "delete locally only"**, so pressing Cancel still deletes. | `static/page-history.js:851-858` | S |
| 13 | **The service worker never installs.** It pre-caches `/activity` and `page-activity.js`, which no longer exist, so `addAll` fails. It is also registered under `/static/`, so it could never control pages anyway. ✔ verified | `static/service-worker.js:8,20`, `core.js:390` | S |

---

## 3. P1: security hardening

| # | Issue | Where | Effort |
| --- | --- | --- | --- |
| 1 | **Placeholder secrets are accepted.** With `change_me` or `your_admin_api_key`, anyone can forge JWTs and decrypt every provider secret. Refuse to start with a known placeholder or a key shorter than 32 bytes. | `core/auth.py:35`, `core/security.py:12`, `api/deps.py:79` | S |
| 2 | **Postgres is published on `0.0.0.0:5432`** with a default password, and Docker port publishing bypasses ufw. ✔ verified | `docker-compose.yml:29-33` | S |
| 3 | **CORS is `allow_origins=["*"]` with `allow_credentials=True`.** Starlette then echoes back any origin. Remove CORS from `/api`, and allow `*` without credentials only on `/stremio-addon/*`. ✔ verified | `main.py:84-90` | S |
| 4 | **SSRF through the user-supplied Stremio `api_base_url`.** The URL is passed to the client unvalidated (✔ verified), and the upstream error body is reflected in the response. Letterboxd's override has the same unvalidated URL. | `routes_integrations.py:1183`, `connectors/services/stremio.py:117` | S |
| 5 | **The external-catalog SSRF filter has gaps.** It allows CGNAT (100.64/10, which Tailscale uses), is vulnerable to DNS rebinding because the address is resolved twice, and fails open on `gaierror`. Use `ip.is_global` and pin the resolved IP. | `core/external_catalog.py:76-115` | M |
| 6 | **Login runs bcrypt synchronously on the event loop**, so parallel login attempts stall the whole API. There is no rate limiting or lockout, and response timing and the register 409 reveal which usernames exist. | `routes_auth.py:48,77`, `core/auth.py:29` | S |
| 7 | **The TMDB API key leaks.** It is sent as a query parameter, so it appears in httpx INFO logs, `HTTPStatusError` text, the lookup `error` column, and API responses. Use the v4 bearer header and redact URLs. | `connectors/metadata/tmdb.py:237`, `metadata_enrichment.py:459,466`, `routes_metadata.py:1375` | S |
| 8 | **Dependencies.** Replace `python-jose`, which pulls in `ecdsa` with the unfixed CVE-2024-23342, with PyJWT. Remove `passlib`, which is never imported, and declare `bcrypt>=5` directly. The Docker image also ignores `uv.lock`; see item 4.2. | `backend/pyproject.toml:19-20` | S |
| 9 | **Smaller items.** These are each low severity: <br>• Add constant-time comparisons for the admin key and OAuth state. <br>• Add a way to regenerate the addon URL token, and use a separate watch-state token. <br>• Revoke JWTs on logout with a `token_version` column, and add a password-change flow. <br>• Derive separate keys with HKDF and support rotation with MultiFernet. <br>• Add security headers (CSP, frame-ancestors, nosniff, HSTS). <br>• Stop writing to `innerHTML` in `showToast`. <br>• Default registration to off, and fix the race in the `max_users` check. <br>• Stop the dashboard from showing other users' activity and rating distribution. | `deps.py:83`, `routes_integrations.py:272`, `routes_auth.py:38,100`, `core/security.py:14`, `core.js:437`, `routes_dashboard.py:298-335` | M |

---

## 4. P2: reliability, performance and operations

### 4.1 Sync engine

1. **Do not hold DB transactions or row locks across HTTP calls.** **M–L**
   - The outbox holds a `FOR UPDATE` lock on `WatchStateEntry` during provider calls, which blocks the addon API.
   - The rate-limiter bucket stays locked through TMDB calls.
   - Imports keep one transaction open while paging the entire history.

   Change each job to three steps: read and commit, make the HTTP call with no transaction open, then reopen and write. Set the pool size explicitly and set `idle_in_transaction_session_timeout`. See `process_outbox.py:147,184,815`, `core/rate_limiter.py:45`, `trakt_import.py:137` and `db/session.py:34`.
2. **Retry policy.** **M**
   - Add a maximum number of attempts and a `dead` status.
   - Honor `429` and `Retry-After`; the code has no 429 handling anywhere today.
   - Charge rate-limit tokens for batch deliveries too.
   - Add backoff to the worker loop.
3. **Outbox fairness and ordering.** **M**
   - Claims are ordered by `user_id`, so one user's backlog blocks everyone else.
   - A delete doesn't cancel pending pushes for the same item, so a retried push can bring a deleted item back.
4. **Retention.** `outbox`, `sync_attempts`, `watch_events`, the metadata lookup tables and `watch_state_receipts` grow without limit. Add a scheduled pruning job. **S–M**
5. **Schema and indexes.** **M**
   - Switch `JSON` columns to `JSONB`, or promote the 18 hot `raw["trakt_id"]` lookups to real columns.
   - Add indexes on `watched_items(user_id, watched_at)` and `(user_id, media_item_id)`.
   - Add a partial index for outbox claims, and a pg_trgm index for `ilike '%q%'` search.
   - Drop the duplicate indexes on columns that already have unique constraints.
   - Move the import state machine out of the pseudo-provider `"system"` integration row into an `import_runs` table.
6. **Make lease completion check `lease_owner`.** `core/scheduler.py:43-80`. **S**
7. **HTTP clients.** **S**
   - Use one pooled `AsyncClient` per provider instead of a new client per request (25 call sites).
   - Cache the version string.
   - Keep the TVDB token for its full lifetime instead of logging in again for each item.
8. **Reduce external calls per item.** **M**
   - Metadata currently takes 2–4 calls per watched episode. Cache providers per run, and cache season listings by `(tmdb_id, season, lang)`.
   - Check local IDs before making per-item `external_ids` calls.
   - Add negative caching to the backfill.

### 4.2 Deployment and operations

1. **Make the Docker image use `uv.lock`.** Replace `uv pip install --system .` with `uv sync --frozen --no-dev`, using a multi-stage build that copies only the virtualenv. Remove pip and uv from the runtime image, which is about 350 MB today. `backend/Dockerfile:30`. **S**
2. **Health checks.** **M**
   - The worker's healthcheck probes the API, so a hung worker still reports healthy. Add a per-loop heartbeat.
   - `/health` is static.
   - The `api` service has no restart policy.
   - Add `init: true`.
   - `docker-compose.override.yml` doesn't list `worker-watch-state`.
3. **Observability.** **M**
   - Add structured JSON logs that include `job_id` and `user_id`.
   - Add a Prometheus `/metrics` endpoint with queue depth, age of the oldest pending job, per-provider latency and 429 counts.
   - Lower the httpx logger to WARNING.
4. **Run migrations under an advisory lock** so that several API replicas don't race. `main.py:207`. **S**
5. **Shrink the Docker build context.** `.dockerignore` misses `.uv-cache` (146 MB), `external-api-docs`, `worker/` and `docs/`. **S**

---

## 5. P3: tests and CI

1. **Add Postgres to CI.**
   - Add a `postgres:17` service and set `WATCH_STATE_TEST_DATABASE_URL`.
   - Run `alembic upgrade head`, `alembic check`, and a downgrade-then-upgrade round trip.
   - Production is Postgres-only, and SKIP LOCKED, the triggers and locking are not tested today. **M**
2. **Add HTTP-level connector tests.** Use respx or `httpx.MockTransport` with fixtures from `external-api-docs/`. These modules have no tests at all: **L**
   - `LetterboxdClient` (1,352 lines)
   - All metadata providers
   - `MetadataLookupEngine`
   - `stremio_import`, `letterboxd_import`, `anilist_import` and `merge_history`
   - `routes_integrations` (including the OAuth flow) and `routes_auth`
   - `worker.py`
3. **Add a regression test for each P0 bug** as it is fixed. **S each**
4. **Strengthen CI.** **S–M**
   - Add `ruff format --check`, after one formatting commit plus a `.git-blame-ignore-revs` file.
   - Add pyright in basic mode, or mypy with a baseline. First reword the comment at `db/models.py:580`, which mypy misreads as a type comment.
   - Use `uv sync --locked`, set `permissions: contents: read`, add concurrency cancellation, and pin actions by SHA.
   - Add `javascript-typescript` to CodeQL.
   - Build the Docker image on pull requests without pushing it, and scan it with Trivy or Grype.
   - Add an SBOM and provenance attestation on publish.
   - Rebuild `styles.css` and check it with `git diff --exit-code`.
   - Have the release script wait for CI.
5. **Widen the ruff rules.** Add `B` (with FastAPI `Depends` marked as immutable, which leaves 12 hits including the P0 bug), `UP` (25, all auto-fixable), `ASYNC`, `DTZ` (3 naive `strptime` calls), `RUF`, and `S` (ignoring S101 in tests). Add `SIM` and `BLE` later. **S**
6. **Update Dependabot.**
   - Switch from the `pip` ecosystem to `uv` at the repo root; the `pip` setup never updates `uv.lock`.
   - Add `npm` for `/frontend` and `docker` for `/backend`, with grouping. **S**
7. **Tidy the test setup.**
   - Add a `conftest.py` and a `[tool.pytest.ini_options]` section.
   - Remove the 14 `sys.path` hacks and the cross-test imports.
   - Replace the per-file SQLite engines with shared fixtures.
   - Add frontend checks: ESLint (`no-undef`, `no-redeclare`) and a small Playwright smoke test. **M**

---

## 6. P4: maintainability and refactoring

1. **Split `process_outbox.py` (3,477 lines).**
   - Move the claim, process, batch, retry and classify logic into `jobs/outbox/runner.py`.
   - Give each provider its own `handlers/<provider>.py`.
   - Add a shared `ProviderError(status, body)` hierarchy (Auth, RateLimit with `retry_after`, NotFound, Transient). It replaces the 2×6 `except` chains and the six error formatters. **L**
2. **Share the import item-resolution code.** About 2,770 lines are duplicated across 7 import jobs, including `_find_media_item` ×6, `_get_or_create_episode_item` ×5, `_can_assign_media_id` ×5 and `_save_integration_secret` ×4. Move them into `jobs/import_items.py`, and stream pages instead of loading whole histories into memory. **L**
3. **Add a `core/integration_tokens.py` TokenManager.** It should lock the secret row, re-read it after locking, commit separately, retry once on 401, and set `reauth_required`. It replaces the 6 copies of the refresh logic and fixes P0-8. **M**
4. **Add a `BaseHttpConnector`.** It provides a pooled client, error mapping with redacted bodies, and a `paginate()` helper. Trakt alone has 7 identical pagination loops, and `_safe_body`, `parse_expires_at` and the error formatters are duplicated. Drop the `ServiceConnector` stubs that only raise `NotImplementedError`. **M**
5. **Shared episode identity.** This is the refactor the README defers.
   - Add an `EpisodeIdentity` value type and a single resolver.
   - Add an `episode_external_ids` table to replace lookups in `raw` JSON.
   - Map absolute and season/episode numbering using TVDB absolute order or anime-lists.

   This unblocks anime, absolute numbering and new import sources. **L**
6. **Split the other large modules.**
   - `watch_pipeline.py`: separate the queue, the strategies and the payload builders.
   - `routes_history.py`: move the list query into `core/history_query.py`, and share the merge-on-read logic with `merge_history`.
   - `routes_metadata.py`: collapse the 15 near-identical save and test routes into one parametrized route.
   - `simkl_import.py` (1,995 lines). **M each**
7. **Squash migrations into a new baseline** at a release boundary; there are 23 revisions, including a merge-heads revision and hand-typed IDs. **S**

---

## 7. P5: frontend, UX and accessibility

1. **Stale results on History and Watchlist.** There is no request sequencing or `AbortController`, so an older, slower response can overwrite a newer one. Reuse the `requestVersion` pattern from `page-add-watched.js:242`. **S**
2. **Lookup polling never stops.** The polling code is copied 3 times with no timeout or backoff, so if the metadata worker is down it polls forever. Move it into one shared helper with a timeout. **S**
3. **Error handling.**
   - FastAPI 422 errors display as `[object Object]`.
   - There is no global handling for 401 or an expired session.
   - Home and Settings failures only go to `console.error`.
   - Settings polls every 30 s even when the tab is hidden. **S**
4. **Accessibility.** **M**
   - Use native `<dialog>.showModal()` so the metadata drawer has focus management and a focus trap.
   - Put `inert` on the closed mobile menu; today keyboard users tab through 8 invisible links.
   - Give the import-queue drag-and-drop a keyboard alternative.
   - Add `aria-live` to the toast container.
   - Remove `user-scalable=no`.
   - Fix contrast: the dark-mode primary button is 3.42:1.
   - Restore the focus ring on the search box.
   - Add text alternatives for the charts.
5. **Code structure.** **M–L**
   - Move to native ES modules without a bundler.
   - Create shared `api.js`, `lookup.js`, `list-controller.js` and `dialog.js`.
   - Split `page-settings.js` (3,449 lines, 112 functions) by tab.
   - Remove the duplicated globals; `formatWatchlistSourceLabel` has two different versions.
   - Add `// @ts-check` with JSDoc.
   - Optionally adopt lit-html for the long render functions. A full SPA rewrite is not recommended.
6. **Build and UX polish.** **S–M**
   - Minify the CSS.
   - Fix the flash of the wrong theme or unstyled content.
   - Keep filters and pagination state in the URL.
   - Replace native `confirm()` with proper dialogs.

---

## 8. P6: documentation and repo hygiene (all S)

1. **AGENTS.md is out of date.**
   - It lists `static/app.js` and `templates/integrations.html`, which don't exist.
   - It omits about 50 modules: the AniList and PublicMetaDB code, the addon and watch-state routes, the watchlist, dashboard and blacklist routes, `external_catalog` and the newer jobs.
   - The worker-mode list is missing `external_catalog_refresh`.
   - It lists 3 test files; there are 43.
   - `.github/copilot-instructions.md` duplicates AGENTS.md. Make one of them point to the other.
2. **README errors.**
   - `uv sync --extra tests` doesn't exist; use `--group dev`.
   - The SIMKL redirect URI should be `/api/integrations/simkl/callback`.
   - Undocumented variables: the AniList settings, GZIP, dashboard and external-catalog settings, the PublicMetaDB per-minute rate limit, and `LIBRARYSYNC_WORKER_ID`.
   - The Tailwind build and local Postgres setup aren't explained.
3. **Sync `.env.example` with `config.py`.** Recommend generating secrets with `openssl rand -hex 32`.
4. **Clean up repo hygiene.**
   - Delete the leftover `worker/` directory, which holds only caches.
   - Delete the root `__pycache__` and the stray `logo.png` (272 KB).
   - Update `.vscode/*`, which still points at `worker/src`.
   - Add `.idea/` to `.gitignore`.
5. **Developer experience.**
   - Add a `justfile` or Makefile with `dev`, `test`, `lint` and `css` recipes.
   - Add a pre-commit config with ruff.
   - Add a Compose dev profile and seed data.
   - Add a CHANGELOG; `docs/releases/` has only 2 of 53 releases.

---

## 9. P7: features and roadmap

1. **[#99](https://github.com/willtho89/librarySync/issues/99) SIMKL calendar v2.** The old calendar files stop on **1 Feb 2027**. Draft PR [#100](https://github.com/willtho89/librarySync/pull/100) exists, so review it and merge it before the deadline.
2. **[#97](https://github.com/willtho89/librarySync/issues/97) Up Next should continue from the last watched season.** Draft PR [#98](https://github.com/willtho89/librarySync/pull/98) exists.
3. **[#14](https://github.com/willtho89/librarySync/issues/14) AniList gaps:**
   - rating push
   - full history sync
   - progress-only semantics
   - clearing the activity feed on delete
4. **Scrobble and progress delivery.** `ProgressEvent`, `push_progress` and Watch State playback data already exist. Add Trakt and SIMKL `/scrobble` as a new outbox job type. **M**
5. **Media-server sources (Jellyfin, Plex, Emby, Kodi webhooks).** Each one costs 1,000–2,000 lines today, but only about 300 after the shared resolver (P4-2 and P4-5) is in place. Do it after that refactor.
6. **Account management:**
   - password change
   - session revocation
   - addon-token rotation
   - an admin user list

---

## 10. Suggested order

| Sprint | Focus | Items |
| --- | --- | --- |
| 1 (about 1 week) | Stop data loss, close obvious holes | All of P0 except items 5, 6 and 8 · P1 items 1–4, 6 and 7 · the dependency swap (P1-8) · Docker on `uv.lock` (4.2-1) · regression tests for each fix |
| 2 | Outbox robustness | P0 items 5, 6 and 8 through the TokenManager (P4-3) · retry policy, 429 and dead-letter (4.1-2) · leases and SIGTERM handling · Postgres in CI (P3-1) |
| 3 | Scale and observability | Short transactions (4.1-1) · indexes and JSONB (4.1-5) · retention · pooled HTTP clients · metrics and heartbeats · merge #100 before the SIMKL deadline |
| 4+ | Structure | Split `process_outbox` · shared import resolver · episode identity · frontend ES modules · connector test suite · docs refresh |

---

## Status on branch `fix/review-improvements` (2026-10-07)

| Area | Done | Still open |
| --- | --- | --- |
| P0 bugs | All 13. Includes a migration that repairs watches already affected by the Stremio bug. | — |
| P1 security | Items 1 and 3–8, plus parts of 9: constant-time comparisons, security headers and CSP, toast text-only, `max_users` race, dashboard privacy, key rotation. | Item 2 (Postgres port, local-only, intentionally skipped). Addon-token rotation, logout revocation, registration default, DNS-rebinding pinning. |
| P2 sync engine | Retry cap, 429 `Retry-After` handling, batch rate limiting, user fairness, cancel pushes on delete, retention job, lease fencing, `watched_items` indexes, version cache. | Short transactions around HTTP calls, JSONB/trigram indexes, `import_runs` table, pooled HTTP clients, metadata call reduction. |
| P2 ops | Docker installs from `uv.lock` (multi-stage), stricter `.dockerignore`, quiet httpx logging, graceful worker shutdown. | Worker heartbeat and metrics, migration advisory lock, compose health checks (local). |
| P3 tests/CI | Postgres and migration jobs in CI, Docker build and Trivy scan on PRs, CSS check, CodeQL for JS, SBOM/provenance, Dependabot (uv/npm/docker), wider Ruff rules and format check, pytest config, about 120 new tests. | HTTP-level connector tests, type checker (pyright/mypy), ESLint/Playwright. |
| P4 refactors | Shared OAuth token manager, unified outbox failure handling. | Split `process_outbox`, shared import resolver, episode identity. |
| P5 frontend | Service worker, stale responses, delete dialog, lookup timeout, error handling, accessibility, contrast, theme flash, CSS minification. | ES modules split, URL-persisted filters. |
| P6 docs | AGENTS.md, README, `.env.example`, Copilot pointer, `justfile`, pre-commit, `.vscode`, `.idea`. | CHANGELOG, untracked `worker/` and root `__pycache__` cleanup, unused `logo.png`. |
| P7 features | — | All (#99 SIMKL calendar before 1 Feb 2027, #97, #14, scrobbling, media-server sources). |

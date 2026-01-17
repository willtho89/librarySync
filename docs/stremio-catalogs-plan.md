# Stremio Catalogs Plan

## Goals
- Provide a Stremio addon that exposes each user's watchlist as catalogs.
- Support user-defined filters (unwatched, released, combinations) and ordering (date added, release date, random, etc.).
- Add a TV-only "In progress" catalog for shows with unwatched released episodes.
- Enable custom catalogs with fixed, user-curated media items.
- V2: Support catalog-only external watchlists (provider watchlists or list URLs), imported
  separately and refreshed regularly, with the same filters applied before ordering.
- Offer install options: direct install link and copyable manifest URL.
- Ship a responsive frontend for configuration + management.

## Non-goals (for this phase)
- Streaming sources, meta enrichers, or providers beyond the local catalog.
- Replacing existing watchlist UI or watchlist data model.

## Reference/Research
- Review Stremio addon manifest/catalog spec and best practices.
- Inspect any local Stremio-related code and the aiometadata/aiostreams addon patterns for response structure and pagination.

## High-level UX
- Add a new burger-menu entry below Settings: "Stremio Addon".
- New page `/stremio-addon` with:
  - Install section (manifest URL + stremio:// link).
  - Addon key management (show + rotate).
  - Built-in catalogs (Watchlist, In progress) with filter + order controls.
  - Custom catalogs (CRUD + add/remove items).

## Backend Design

### Data Model
Add new tables (or extend existing ones) to capture per-user addon config and custom catalogs.

Suggested tables:
- `stremio_addon_configs`
  - `user_id` (FK)
  - `is_enabled` (bool)
  - `addon_key_hash` (string) + `addon_key_last_rotated_at`
  - `default_catalogs` (JSON): list of catalog definitions (id, name, media_type, filters, ordering, enabled)
  - `created_at`, `updated_at`

- `stremio_custom_catalogs`
  - `id`, `user_id` (FK)
  - `name`, `slug`
  - `media_type` (movie/tv/anime/all)
  - `order_by`, `order_dir`
  - `created_at`, `updated_at`

- `stremio_custom_catalog_items`
  - `catalog_id` (FK)
  - `media_item_id` (FK)
  - `position` (int) for manual ordering
  - `created_at`

Notes:
- Addon access is key-based (no JWT). Store addon keys hashed; keep plaintext only at creation/rotation time.
- If you prefer fewer tables, we can encode the built-in catalogs in JSON on the config table and only create a table for custom catalogs/items.
- V2 catalog-only watchlists:
  - Add tables mirroring watchlist sources/items but scoped to the addon:
    - `stremio_watchlist_sources` (user_id, provider, source_type, external_id, url, name, enabled)
    - `stremio_watchlist_items` (user_id, media_item_id, status, dates, source metadata)
    - `stremio_watchlist_source_items` (source_id, watchlist_item_id, last_seen_at)
  - Keep these separate from `watchlist_items` so catalog-only imports do not modify the main
    watchlist UI.

### Internal API (auth-required)
Create endpoints under `/api/stremio-addon`:
- `GET /api/stremio-addon/config`
  - Returns manifest URL, install link, enabled state, catalogs config, and custom catalogs.
- `POST /api/stremio-addon/config`
  - Updates addon enablement and built-in catalog filters/order.
- `POST /api/stremio-addon/token/rotate`
  - Rotates addon key and returns new manifest URL.
- Custom catalogs CRUD:
  - `POST /api/stremio-addon/custom-catalogs`
  - `PATCH /api/stremio-addon/custom-catalogs/{catalog_id}`
  - `DELETE /api/stremio-addon/custom-catalogs/{catalog_id}`
  - `POST /api/stremio-addon/custom-catalogs/{catalog_id}/items`
  - `DELETE /api/stremio-addon/custom-catalogs/{catalog_id}/items/{media_item_id}`
  - Optional: `POST /api/stremio-addon/custom-catalogs/{catalog_id}/reorder`
- V2 catalog-only watchlists:
  - `GET /api/stremio-addon/watchlists`
  - `POST /api/stremio-addon/watchlists`
  - `PATCH /api/stremio-addon/watchlists/{watchlist_id}`
  - `DELETE /api/stremio-addon/watchlists/{watchlist_id}`
  - Optional: `POST /api/stremio-addon/watchlists/{watchlist_id}/refresh`

### Addon (public) Routes
Add a new router with public endpoints; all access is gated by the addon key in the URL.

Suggested URL scheme:
- `GET /stremio-addon/{addon_key}/manifest.json`
- `GET /stremio-addon/{addon_key}/catalog/{type}/{id}.json`
- (Optional) `GET /stremio-addon/{addon_key}/meta/{type}/{id}.json`

Notes:
- Use Stremio types: `movie` and `series`. Map `tv` and `anime` to `series`.
- Manifest should include multiple catalogs (one addon, many catalogs).
- Catalog endpoint should support `extra` params (`skip`, `limit`, `search`) per Stremio spec.
- Random ordering should be fully random per request (no caching for now; add later if needed).
- Support Stremio pagination via `skip` + `limit` on every catalog response.

### Catalog Definitions
Built-in catalogs (user-configurable):
- `watchlist_movies`
- `watchlist_shows`
- `watchlist_anime` (optional, map to series)
- `in_progress_shows`

Custom catalogs:
- Each catalog has a stable `id` (slug or UUID) and `name`.
- Items are explicit `media_item_id` entries.
- Custom catalogs are separate from the watchlist.

V2: Catalog-only watchlist catalogs:
- Each catalog maps to a `stremio_watchlist_source` (provider + source_type + external_id).
- Treated like watchlist-backed catalogs but scoped to the catalog-only dataset.
- Apply the same filters (e.g. exclude `watched` statuses) before ordering.

### Filters and Ordering
Filters should align with existing watchlist status logic:
- Unwatched: statuses in `added`, `in_progress`, `not_released` (exclude `watched`, `waiting`, `removed`).
- Released: user-selectable basis for shows:
  - by show air date (`first_air_date`), or
  - by per-episode air dates (released episodes only).
- Status combinations: map to existing watchlist status rules in `core/watchlist.py`.

Ordering:
- Reuse `core/catalog_ordering.apply_catalog_ordering` for date added, release date, last watched, progress.
- Extend ordering to include `random` and optional `title` or `manual` (custom catalogs).

### Query Strategy
- Base query: join `watchlist_items` -> `media_items` for watchlist-based catalogs.
- In-progress TV query:
  - Use existing progress subqueries to detect shows with released episodes left.
  - Filter where `watched_count > 0` and `watched_count < total_released`.
- Custom catalogs:
  - Join `stremio_custom_catalog_items` -> `media_items`.
  - Support manual ordering by `position`, fallback to created_at.
- V2 catalog-only watchlists:
  - Join `stremio_watchlist_source_items` -> `stremio_watchlist_items` -> `media_items`.
  - Filter by source scope, then apply the same watchlist filters.

## Frontend Plan
- Add nav entry in `backend/src/librarysync/templates/base.html`.
- Create `backend/src/librarysync/templates/stremio-addon.html`.
- Create `backend/src/librarysync/static/page-stremio-addon.js`.
- Update `frontend/input.css` (do not edit `backend/src/librarysync/static/styles.css` directly).

UI sections:
1. Install
   - Manifest URL (copy button).
   - Direct install link (stremio://... per spec).
   - Rotate key button.
2. Built-in catalogs
   - Toggle enable/disable.
   - Filters: unwatched, released, include watched, media type.
   - Ordering: date added, release date, last watched, progress, random.
3. In progress (TV)
   - Dedicated block with ordering controls and enable toggle.
4. Custom catalogs
   - Create/edit/delete catalogs.
   - Add items via search (reuse blacklist-style search UX).
   - Reorder items (drag/drop or up/down).
5. V2: Catalog-only watchlists
   - Add/manage external watchlist sources for catalog-only use.
   - Apply the same filters/order options as built-in watchlist catalogs.

## Security + Access
- Addon access uses a per-user addon key (random, rotatable); no auth cookies.
- Store addon key hashed; verify using constant-time compare.
- Enforce `is_enabled` flag; return 404/401 for disabled or invalid keys.

## Testing
- Unit tests for:
  - Manifest generation includes expected catalogs.
  - Catalog filters (unwatched, released, in-progress) and ordering.
  - Custom catalog CRUD and item ordering.
  - Addon key rotation and access control.
- Integration tests for Stremio endpoints (FastAPI test client).

## Rollout Steps
- Add migrations for new tables/columns.
- Deploy backend endpoints and UI.
- Verify with a test Stremio client install link.
- Monitor catalog performance and adjust cache TTL or indexes.

## Open Questions
None.

## Known Bugs (needs fixing)
  - [P2] Persist default catalog updates when editing JSON config — /Users/twillems/Development/stremio/librarySync/backend/src/librarysync/api/routes_stremio_addon.py:60-70
    The catalog update path mutates the existing default_catalogs list/dicts in place and then reassigns the list. Because default_catalogs is a plain JSON column (not a MutableList), SQLAlchemy doesn’t track in-place JSON mutations; if the new list
    compares equal to the old one, the UPDATE won’t include default_catalogs. This means toggling catalog enabled state or changing filters/order can appear to succeed in the response but revert on the next load. Consider deep-copying before mutation or
    explicitly flagging the JSON column as modified.
  - [P2] Apply catalog status filters to in-progress catalog — /Users/twillems/Development/stremio/librarySync/backend/src/librarysync/api/routes_stremio_addon_public.py:270-277
    The in-progress catalog query ignores the configured filters.statuses and only excludes removed. If a user disables certain statuses in the addon config (e.g., wants to exclude watched or not_released items), those settings are silently ignored for
    the in_progress_shows catalog. This causes the in-progress catalog to return items the user explicitly filtered out. Consider applying the same status filter logic as _build_watchlist_query or pass the catalog’s filters into this helper.

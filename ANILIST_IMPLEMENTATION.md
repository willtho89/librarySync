# AniList Integration Implementation Summary

## Overview
This implementation adds comprehensive anime detection and AniList integration to librarySync, allowing users to sync their anime watch history and ratings to AniList alongside existing providers.

## What Has Been Implemented ✅

### 1. Database Schema & Models
- **Added `anilist_id` column** to `media_items` table
- Created Alembic migration `6e9a1b2c3d4f_add_anilist_id_to_media_items.py`
- Added unique constraint on `(media_type, anilist_id)` pair
- Added index for `anilist_id` lookups
- Updated all API models to include `anilist_id` field

### 2. Anime Detection System
- **Created `core/anime.py`** with helper functions:
  - `is_anime()`: Detects anime via multiple signals (media_type, provider IDs, raw metadata)
  - `get_anime_provider_ids()`: Extracts anime-specific provider IDs
- Anime is detected when any of these conditions are met:
  - `media_type == "anime"`
  - Has `kitsu_id`, `myanimelist_id`, or `anilist_id`
  - Raw metadata contains `type: "anime"`

### 3. AniList Metadata Provider
- **Created `connectors/metadata/anilist.py`**
- GraphQL-based provider using AniList's public API
- Features:
  - Search anime by title
  - Get anime details by AniList ID
  - Maps to MAL ID when available
  - No authentication required for metadata lookups
- Registered in `METADATA_PROVIDER_REGISTRY`
- API endpoints: `POST /api/metadata/providers/anilist` and `/test`

### 4. AniList Service Connector
- **Created `connectors/services/anilist.py`**
- OAuth2 integration with AniList
- Key features:
  - Token exchange and management
  - Viewer information retrieval
  - Media list entry management (add/update/delete)
  - Rating conversion (0.5-5.0 stars → 0-10 AniList scale)
  - Fuzzy date handling
  - GraphQL client with error handling

### 5. Configuration & Rate Limiting
- Added AniList config settings:
  - `ANILIST_CLIENT_ID`
  - `ANILIST_CLIENT_SECRET`
  - `LIBRARYSYNC_ANILIST_RATE_LIMIT_PER_MINUTE` (default: 90)
- Integrated AniList into rate limiter system
- Updated `.env.example` with AniList configuration

### 6. API Routes
- Updated `routes_history.py`:
  - Added `anilist_id` to all request/response models
  - Added AniList sync status fields to `WatchedItemOut`
  - Updated search to include `anilist_id`
  - Added AniList to provider fields and metadata fields
- Updated `routes_metadata.py`:
  - Added `anilist_id` to candidate extraction
  - Added `anilist` provider mapping
  - Created AniList provider endpoints

### 7. Testing
- **Created `tests/test_anime_detection.py`**
- Comprehensive tests for `is_anime()` function
- Tests for `get_anime_provider_ids()` extraction
- Covers all detection methods and edge cases

## What Remains To Be Implemented 🔨

### 1. Integration Routes (High Priority)
File: `backend/src/librarysync/api/routes_integrations.py`

Need to add:
```python
# OAuth start endpoint
@router.get("/anilist/start")
async def anilist_start(...)

# OAuth callback endpoint
@router.get("/anilist/callback")
async def anilist_callback(...)

# Disconnect endpoint
@router.post("/anilist/disconnect")
async def anilist_disconnect(...)
```

Pattern to follow: Similar to existing SIMKL/Trakt OAuth flows

### 2. Sync Strategy (High Priority)
File: `backend/src/librarysync/core/watch_pipeline.py`

Need to create:
```python
class AniListSyncStrategy(SyncStrategy):
    provider = "anilist"
    
    async def enqueue_new(...):
        # Check if anime using is_anime()
        # Create push_watched jobs for anime items
        
    async def enqueue_update(...):
        # Handle rating and watched_at updates
        
    async def enqueue_delete(...):
        # Handle removal from AniList
```

Add to `SYNC_STRATEGY_REGISTRY`

### 3. Outbox Handler (High Priority)
File: `backend/src/librarysync/jobs/process_outbox.py`

Need to create:
```python
class AniListOutboxHandler(OutboxHandler):
    provider = "anilist"
    
    async def deliver(self, db: AsyncSession, job: OutboxJob):
        # Handle push_watched
        # Handle push_rating
        # Handle remove_history
        # Use AniListClient for API calls
```

Add to `OUTBOX_HANDLER_REGISTRY`

### 4. Anime Mapping for Other Providers (Medium Priority)
Files: 
- `backend/src/librarysync/jobs/process_outbox.py`
- Individual handler functions for Trakt, SIMKL, Letterboxd, Stremio

Need to ensure:
- Anime items are mapped to "tv" or appropriate type for non-anime providers
- Don't fail sync when anime detection is positive
- Graceful fallback for providers that don't support anime explicitly

### 5. Import Support (Medium Priority)
Files:
- `backend/src/librarysync/jobs/anilist_import.py` (NEW)
- `backend/src/librarysync/api/routes_integrations.py`

Need to add:
- AniList import job implementation
- Quick import endpoint
- Import all endpoint
- Map AniList history to librarySync format

### 6. Frontend Integration (Medium Priority)
Files:
- `backend/src/librarysync/templates/integrations.html`
- `backend/src/librarysync/static/app.js`

Need to add:
- AniList integration card in UI
- OAuth connect/disconnect buttons
- Status display
- Sync status indicators in history view

### 7. Enhanced Testing (Low Priority)
Files:
- `backend/tests/test_anilist_outbox.py` (NEW)
- `backend/tests/test_anilist_metadata.py` (NEW)

Should add:
- Outbox job transitions for AniList
- Payload generation tests
- OAuth flow tests
- Error handling tests

## Integration Checklist

When implementing the remaining pieces, follow this order:

1. ✅ **Foundation** - Database, models, detection (COMPLETE)
2. ✅ **Metadata** - AniList metadata provider (COMPLETE)
3. ✅ **Configuration** - Settings and rate limiting (COMPLETE)
4. 🔨 **OAuth Routes** - Integration API endpoints (IN PROGRESS)
5. 🔨 **Sync Strategy** - Watch pipeline integration
6. 🔨 **Outbox Handler** - Job processing and delivery
7. 🔨 **Provider Mapping** - Ensure other providers handle anime
8. 📋 **Import** - AniList history import
9. 📋 **Frontend** - UI components
10. 📋 **Testing** - Additional test coverage

## Key Design Decisions

### Anime Detection
- Multi-signal approach ensures anime is detected regardless of entry point
- `is_anime()` is the single source of truth for anime classification
- Raw metadata preserves original source type information

### AniList Integration
- Metadata provider requires no authentication (public API)
- Service connector uses OAuth2 for user-specific operations
- Ratings converted from 0.5-5.0 to 0-10 scale
- GraphQL API used throughout for efficiency

### Rate Limiting
- Default 90 requests per minute (AniList's documented rate limit)
- Token bucket algorithm shared with other providers
- Per-user, per-provider isolation

## Testing Strategy

### Unit Tests ✅
- Anime detection logic
- Provider ID extraction
- Token parsing and validation

### Integration Tests 🔨
- OAuth flow end-to-end
- Sync job processing
- Import functionality

### Manual Testing 📋
- Full user flow: connect → manual add → auto-sync
- Import from AniList
- Rating sync
- Error handling

## Documentation Updates Needed

1. Update `README.md` with AniList setup instructions
2. Add AniList to supported providers list
3. Document anime detection behavior
4. Add OAuth app setup guide for AniList
5. Update API documentation with new endpoints

## Migration Guide

For existing installations:

1. Run database migration: `alembic upgrade head`
2. Set environment variables:
   ```
   ANILIST_CLIENT_ID=your_client_id
   ANILIST_CLIENT_SECRET=your_client_secret
   LIBRARYSYNC_ANILIST_RATE_LIMIT_PER_MINUTE=90
   ```
3. Restart services
4. Users can connect AniList via integrations page

## Performance Considerations

- AniList GraphQL API is efficient (single request for complex queries)
- Rate limiting prevents API abuse
- Metadata caching reduces redundant API calls
- Batch operations not supported by AniList (sequential processing required)

## Security Notes

- OAuth tokens encrypted at rest
- Client secret stored server-side only
- State parameter validates OAuth callbacks
- Rate limiting prevents abuse
- GraphQL errors sanitized before logging

## Future Enhancements

- Bidirectional sync (import updates from AniList)
- Episode progress tracking
- Season watching status
- Anime recommendations based on AniList data
- Advanced rating options (AniList supports 0-100 scale)
- Watch time tracking
- Rewatch count support

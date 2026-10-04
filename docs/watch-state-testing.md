# Local Watch State conformance testing

The contract tests run with the normal backend suite:

```sh
uv run pytest backend/tests -q
```

PostgreSQL concurrency and migration tests need a disposable database. They create and remove
isolated schemas; the migration test upgrades the previous schema, checks proxy retirement and
preserved history, then downgrades.

```sh
WATCH_STATE_TEST_DATABASE_URL=postgresql+psycopg://postgres:password@127.0.0.1:55439/librarysync_tests \
  uv run pytest backend/tests/test_watch_state_postgres.py backend/tests/test_watch_state_migration_postgres.py -q
```

Live tests use an unmodified AIOStreams server and an isolated LibrarySync server. Both URLs
must be loopback addresses. Never use a production tracker configuration. Enable LibrarySync
registration with enough capacity for an owner and a household test account. Run both the
`watch_state` and `outbox` worker modes.

Create an isolated AIOStreams configuration with metadata/catalog providers, stream providers
restricted to `stream`, and only the local LibrarySync addon enabled for `watch_state`. Filter
old LibrarySync instances from any nested AIOStreams addon. Select the local addon as the primary
tracker. Authenticate through Jellyfin and save the response to a private temporary JSON file.
Also save the isolated AIOStreams account and config JSON outside the repository.

```sh
AIOSTREAMS_TEST_BASE_URL=http://127.0.0.1:3006 \
LIBRARYSYNC_TEST_BASE_URL=http://127.0.0.1:8767 \
AIOSTREAMS_TEST_AUTH_FILE=/tmp/test-jellyfin-auth.json \
AIOSTREAMS_TEST_ACCOUNT_FILE=/tmp/test-aiostreams-account.json \
AIOSTREAMS_TEST_CONFIG_FILE=/tmp/test-aiostreams-config.json \
AIOSTREAMS_TEST_PASSWORD=test-account-password \
LIBRARYSYNC_TEST_USERNAME=test-owner \
LIBRARYSYNC_TEST_PASSWORD=test-owner-password \
  uv run pytest backend/tests/test_watch_state_aiostreams_e2e.py -q -s
```

The live tests cover playback start/pause/resume/stop, completed/unfinished stops, explicit marks,
favourites, drops, decimal ratings and clears, show/season/episode rating scopes, bulk season/show
marks, LibrarySync-to-Jellyfin rating pulls, household isolation and consent revocation. They use
real metadata but do not fetch or play media streams. AIOStreams retains delivered position IDs,
so reruns choose fresh positions. Account configuration updates are confined to the disposable server.

For faster local tests, AIOStreams can use a 10-second delivery interval, a 60-second pull interval,
a one-second pull TTL and a one-second echo window. The test waits for delivery rather than assuming
that a Jellyfin acknowledgement means the tracker has already received the event.

Remove the disposable containers, browser session and temporary credential/config files afterward.

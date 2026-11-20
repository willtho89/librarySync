# Private LibrarySync MCP

## Release notes / compatibility

This release adds an opt-in MCP listener and an optional Hermes skill. Existing API, worker, provider credentials, provider jobs and default Compose services are unchanged. There is **no new database migration**. Roll back by pausing the Hermes routine, removing its two MCP connections, and stopping the MCP listener. If rolling the stack back, restore both API and LibrarySync workers to the previous verified image digest; stopping MCP does not undo approved Watchlist entries or cancel the regular outbox. Never downgrade or restore a production database as part of this feature rollback without a separate recovery decision.

### Transport and exposure

The same release image can run a dedicated, private ASGI listener for `/mcp`. It is not mounted on the normal web/API listener: deployments that already route all API paths publicly do not accidentally publish MCP. The normal API continues to own migrations. The dedicated listener speaks classic **stateful Streamable HTTP**, with SSE on the tool request for live form elicitation. It does not expose legacy `/sse`, stdio, browser REST credentials, or the newer stateless MCP protocol. Run one process/worker per listener; sessions and history cursors do not survive a restart.

Use the release image at its verified digest. On a host with a private or loopback bind, a typical **listener command inside that image** is:

```sh
uvicorn librarysync.mcp_app:create_app --factory --host 127.0.0.1 --port 8765 \
  --workers 1 --no-proxy-headers --no-access-log
```

When running it in a separate container, put it on a private network restricted to the database and Hermes, or bind its published port to host loopback only and use a private tunnel. In the latter case the container needs to listen on its container interface; **do not** publish on all host interfaces. Use trusted TLS or an authenticated, encrypted tunnel for cross-host access. Do not attach it to the ingress proxy's network, add any public router, or depend on the `Host` header for network isolation. Verify that the public web API has no MCP route and that only the intended Hermes route can reach the private listener. Use the applicable exact `Host` header including the port when sent. Browser `Origin` is always rejected.

### Account grants and secrets

Set these values **only for the private listener** (e.g. protected environment / secret store):

| Environment variable | Meaning |
| --- | --- |
| `LIBRARYSYNC_MCP_ENABLED` | Exactly `true` (case-insensitive) opts in; unset/default is off. |
| `LIBRARYSYNC_MCP_ALLOWED_HOSTS` | JSON array such as `["127.0.0.1:8765"]`; no wildcards or URL schemes. |
| `LIBRARYSYNC_MCP_GRANTS` | JSON array of `{ "user_id": "<canonical account UUID>", "token_sha256": "<64 hex chars>", "allow_watchlist_add": false }` entries. An empty list grants no access. |
| `DATABASE_URL` | Existing migrated LibrarySync database over its private route. |
| `LIBRARYSYNC_SECRET_KEY` / `LIBRARYSYNC_SECRET_KEY_PREVIOUS` | Same protected encryption configuration as the API if write/provider sync is used. Do not copy any admin token or OAuth login credentials into the bot or provision new provider access. |

An operator maps grants to the intended active User IDs from the existing account database, never from Hermes arguments. Generate **distinct** read/write credentials using a CSPRNG (`secrets.token_urlsafe(32)`); store the raw 43-character strings in the Hermes profile's secret store, and only their SHA-256 hashes of the raw ASCII token (no newline or `Bearer ` prefix) on the service. Do not print the strings or put them on a shell command line, in a Compose manifest, Git, approval prompt, logs or task. The read entry has `allow_watchlist_add: false`, the optional write entry `true`. Omit the write grant initially. Removing/replacing a grant and restarting the listener revokes it; deactivating the account also prevents further use. Credentials are pre-provisioned bearer tokens for private infrastructure, not OAuth discovery grants or cookie/JWT logins. The read grant cannot add even if a client ignores the tool allowlist.

Default deployment does not enable the listener. No access/request-body/debug tracing on the listener, proxy or Hermes. A user may see personal data in the authorised Bot Chat, and the configured Hermes inference provider processes data passed to the model. Hermes's private session storage may retain tool results. Review the Bot profile's model and retention settings with the operator before enabling access; don't export sessions, enable raw traces, or use a shared Bot Chat. Keep the account out of group/peer workflows and restrict access to its profile. Never use production titles or tokens in smoke logs or diagnostics.

## Tools and provider effects

| Tool | Behaviour |
| --- | --- |
| `list_history` | Authenticated account's stored watches, 20 by default, maximum 50 per page. Follow `next_cursor` until `null`. Stable descending time/ID order with a credential-scoped, process-lifetime cursor; not a frozen snapshot as the library changes. Re-watches and duplicate import rows are retained. Episode watches carry the parent series and episode numbers. No provider calls, raw metadata, paths, credentials or sync logs. |
| `find_title` | Exact, case-sensitive local catalog title (optionally year and library type `movie`/`tv`/`anime`) **or** exact canonical IMDb title ID. No aliases or remote lookup. The existing shared catalog contains title identity only (no other account's activity); `watched` and `watchlist_status` refer only to the caller. A maximum of five candidates is returned; `ambiguous` and `truncated` are explicit. A miss is not evidence a title has never been watched. The normal LibrarySync UI can populate the local catalog through its existing lookup if necessary. |
| `add_to_watchlist` | Requires the local canonical `media_id` of exactly one film or series and a new affirmative client form response for that call. No un-drop, removal, bulk operation, arbitrary user, account settings or watched/history mutation. Existing rows are not changed or requeued. A created result is not a provider delivery receipt. |

Approved first additions follow LibrarySync's existing personal Watchlist sync. The selected title **may** be queued to Trakt, SIMKL, PublicMetaDB, and/or Letterboxd, only with the caller's existing integration credentials, an enabled source, and suitable media identifiers. PublicMetaDB also retains its sync configuration check. No new receiver or new sync path; no provider call happens in the request. Reads, recommendations, declines, and duplicate no-ops do not enqueue work. The approval message discloses this possible forwarding. No write API is exposed for user IDs or integration changes.

## Install the Hermes skill in the dedicated private Bot

1. After release and private-path verification, create a **new** Hermes Bot called **LibrarySync** (profile slug `librarysync`) with its own canonical **Bot Chat**. Do not clone an existing profile's .env, memory, GitHub credentials or tool configuration. Don't add group membership, peer access, messaging transports, sampling, or admin tools. Restrict the bot's interactive toolsets to the two LibrarySync connections and the minimum needed for the skill and title disambiguation. Leave GitHub, mail, calendar, shell, code, and infrastructure tooling disabled; have the operator create the cron job from the local Bot Chat at acceptance, not via ongoing bot permissions. Leave Ava/default and other profiles unchanged. Use [Hermes Bot Mode](https://hermes-agent.nousresearch.com/docs/user-guide/bot-mode) and [Profiles](https://hermes-agent.nousresearch.com/docs/user-guide/profiles).
2. Copy [`contrib/hermes/librarysync/SKILL.md`](../contrib/hermes/librarysync/SKILL.md) to `skills/librarysync/SKILL.md` under that new profile's actual Hermes home. Apply only the [`mcp.example.yaml`](../contrib/hermes/librarysync/mcp.example.yaml) entries in this profile via the Hermes configuration UI/API. Set `LIBRARYSYNC_MCP_URL` to the **private** full `/mcp` URL and supply the read and optional write tokens via the profile's secret store/environment. Start read-only; enable the write entry only when one-time form elicitation works on the intended private UI. There are separate allowlists: the read toolset carries only history/lookup; the write entries carry only add. No sampling/resources/prompts. `trust: full` on the private write connection avoids the generic trust prompt (which offers remembered grants); it does not bypass the mandatory Server → Hermes form. Never use a client or adapter that auto-answers elicitation.
3. Make the skill part of this Bot's role, including the explicit-human-request rule. Test a real read without echoing titles. When the operator approves an isolated test account and test item, verify in the actual UI that denial and absent approval leave Watchlist and History unchanged, approved add has a readback, and replay requires new approval. Do not perform a write smoke test on the real owner's account.
4. Once the operator has verified the live link, create the [example weekly job](../contrib/hermes/librarysync/weekly-recommendation.example.json) **from the LibrarySync profile's canonical local Desktop Bot Chat** via `cronjob_manage`, initially paused. It uses `0 18 * * 0` in `Europe/Berlin` (wall-clock time, including DST), only `mcp-librarysync_read`, and no catch-up. `deliver: origin` must capture that exact local chat: before resuming, verify the stored origin has `platform: local` and the canonical Bot Chat's session ID, that the read-only toolset and schedule were retained, and that the profile has no fallback home channels. Do not create this job from a separate CLI, cron, or messaging session. Verify in a test run that a current Hermes gateway persists the result as a passive completion note in that private chat, with no second turn or approval. Do **not** use `deliver: bot-chat`: that starts a new turn with the interactive tools. Keep the job paused if strict routing or passive delivery cannot be verified, and recheck before resuming after any chat move or gateway upgrade.

## Deployment verification checklist

- Identify the live API and LibrarySync worker services and current image IDs/digests; verify the release, matching source and immutable architecture-appropriate digest. Check database version, current Alembic head, backup and restore path, and record the previous digest. Don't pull `latest` or change the database/AIOStreams/AIOMetadata services.
- Upgrade API and LibrarySync worker(s) together only under the existing operator approval. Check health, worker logs/status without exposing private content, and existing sync operation. Verify the private listener separately with an unauthorized denial and an authenticated bounded read. Confirm public ingress remains closed to MCP.
- Enable the private Bot and the routine only when their acceptance checks pass. Rollback remains possible without touching the database schema.

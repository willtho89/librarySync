"""Private, opt-in Streamable HTTP listener; deliberately independent of the web API."""

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import unicodedata
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

from mcp import types
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from librarysync.core.mcp_library import (
    AddToWatchlistArgs,
    ApprovalTarget,
    FindTitleArgs,
    HistoryArgs,
    InvalidCursor,
    add_approved_watchlist_item,
    find_title,
    list_history,
    load_approval_target,
)
from librarysync.db import session as db_session
from librarysync.db.models import User

logger = logging.getLogger(__name__)
_HOST = re.compile(rb"(?:[a-zA-Z0-9.-]+|\[[a-fA-F0-9:.]+\])(?::[0-9]{1,5})?")
_BEARER = re.compile(rb"(?i:Bearer) ([a-zA-Z0-9_-]{42}[AEIMQUYcgkosw048])")
_SESSION_ID = re.compile(rb"[a-f0-9]{32}")
_LEGACY_VERSIONS = frozenset(version.encode("ascii") for version in HANDSHAKE_PROTOCOL_VERSIONS)
_TRANSPORT_HEADERS = frozenset(
    {b"host", b"accept", b"content-type", b"content-length", b"mcp-session-id", b"mcp-protocol-version"}
)
_APPROVAL_TIMEOUT_SECONDS = 300
_GRANT_SCOPE_KEY = "librarysync.mcp.grant"
_DISCONNECT_SCOPE_KEY = "librarysync.mcp.disconnected"
_ADD_PROMPT = (
    "Approve this one LibrarySync watchlist addition? It may sync to Trakt, SIMKL, PublicMetaDB, "
    "or Letterboxd where already configured and enabled."
)


class _GrantConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    user_id: str
    token_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$", min_length=64, max_length=64)
    allow_watchlist_add: bool

    @field_validator("user_id")
    @classmethod
    def validate_user_id(cls, value: str) -> str:
        if len(value) != 36 or str(UUID(value)) != value:
            raise ValueError("Expected a canonical UUID")
        return value


@dataclass(frozen=True, slots=True)
class Grant:
    user_id: str
    fingerprint: str
    allow_watchlist_add: bool


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate JSON fields")
    return result


def _json_list(name: str) -> list:
    try:
        value = json.loads(os.environ.get(name, "[]"), object_pairs_hook=_unique_fields)
        if type(value) is list:
            return value
    except (ValueError, RecursionError):
        pass
    raise ValueError(f"Invalid {name}: expected a JSON list")


def _configured_hosts() -> frozenset[bytes]:
    name = "LIBRARYSYNC_MCP_ALLOWED_HOSTS"
    hosts = _json_list(name)
    if not hosts or any(
        type(host) is not str or not host.isascii() or not _HOST.fullmatch(host.encode()) for host in hosts
    ):
        raise ValueError(f"Invalid {name}: expected exact Host header values")
    return frozenset(host.encode("ascii") for host in hosts)


def _configured_grants() -> dict[str, Grant]:
    name = "LIBRARYSYNC_MCP_GRANTS"
    grants = {}
    for value in _json_list(name):
        try:
            grant = _GrantConfig.model_validate(value)
        except ValidationError:
            raise ValueError(f"Invalid {name}: expected valid credential grants") from None
        digest = grant.token_sha256.lower()
        if digest in grants:
            raise ValueError(f"Invalid {name}: duplicate credential digest")
        fingerprint = hashlib.sha256(b"librarysync-mcp:" + bytes.fromhex(digest)).hexdigest()
        grants[digest] = Grant(grant.user_id, fingerprint, grant.allow_watchlist_add)
    return grants


async def _active_account(user_id: str) -> bool:
    db_session.init_session_factory()
    async with db_session.SessionLocal() as db:
        active = await db.scalar(select(User.is_active).where(User.id == user_id))
        return active is True


class _NoStore:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_no_store(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [(key, value) for key, value in message.get("headers", []) if key.lower() != b"cache-control"]
                message = {**message, "headers": [*headers, (b"cache-control", b"no-store")]}
            await send(message)

        await self.app(scope, receive, send_no_store)


class _Endpoint:
    def __init__(self, manager: StreamableHTTPSessionManager, hosts: frozenset[bytes], grants: dict[str, Grant]):
        self.manager = manager
        self.hosts = hosts
        self.grants = grants

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def reject(status: int) -> None:
            headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
            if status == 405:
                headers = {"Allow": "POST, GET, DELETE"}
            await Response(status_code=status, headers=headers)(scope, receive, send)

        if scope["path"] != "/mcp" or scope.get("raw_path", b"/mcp") != b"/mcp":
            return await reject(404)
        headers = scope["headers"]
        hosts = [value for key, value in headers if key == b"host"]
        if len(hosts) != 1 or hosts[0] not in self.hosts or any(key == b"origin" for key, _ in headers):
            return await reject(403)
        auth = [value for key, value in headers if key == b"authorization"]
        match = _BEARER.fullmatch(auth[0]) if len(auth) == 1 else None
        grant = self.grants.get(hashlib.sha256(match[1]).hexdigest()) if match else None
        try:
            active = grant is not None and await _active_account(grant.user_id)
        except Exception:
            logger.error("LibrarySync MCP account verification failed")
            return await reject(503)
        if grant is None or not active:
            return await reject(401)
        if scope["method"] not in {"POST", "GET", "DELETE"}:
            return await reject(405)

        versions = [value for key, value in headers if key == b"mcp-protocol-version"]
        if len(versions) > 1 or (versions and versions[0] not in _LEGACY_VERSIONS):
            return await reject(400)  # No stateless/modern protocol path or alternate approval channel.
        session_ids = [value for key, value in headers if key == b"mcp-session-id"]
        if len(session_ids) > 1 or (session_ids and not _SESSION_ID.fullmatch(session_ids[0])):
            return await reject(404)
        # Use the SDK's session-ownership check, but never pass it the raw bearer or other credentials.
        fingerprint = grant.fingerprint
        user = AuthenticatedUser(
            AccessToken(token=fingerprint, client_id=fingerprint, subject=grant.user_id, scopes=[])
        )
        disconnected = asyncio.Event()

        async def track_disconnect() -> Message:
            message = await receive()
            if message["type"] == "http.disconnect":
                disconnected.set()
            return message

        sdk_scope = {
            **scope,
            "headers": [(key, value) for key, value in headers if key in _TRANSPORT_HEADERS],
            "query_string": b"",
            "user": user,
            _GRANT_SCOPE_KEY: grant,
            _DISCONNECT_SCOPE_KEY: disconnected,
        }
        await self.manager.handle_request(sdk_scope, track_disconnect, send)


def _tool_error(message: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=message)], is_error=True)


def _target_description(target: ApprovalTarget) -> str:
    title = "".join(char if not unicodedata.category(char).startswith("C") else " " for char in target.title)
    title = " ".join(title.split()) or "Untitled"
    if len(title) > 120:
        title = title[:119] + "…"
    year = target.year if target.year is not None else "unknown year"
    return f"Add {title} ({year}; {target.media_type}; local ID {target.id})"


async def _request_watchlist_approval(ctx: ServerRequestContext, target: ApprovalTarget) -> bool:
    schema = {
        "type": "object",
        "properties": {"approve": {"type": "boolean", "description": _target_description(target)}},
        "required": ["approve"],
    }
    try:
        async with asyncio.timeout(_APPROVAL_TIMEOUT_SECONDS):
            response = await ctx.session.elicit_form(_ADD_PROMPT, schema, related_request_id=ctx.request_id)
    except Exception:
        # A malformed, late, or failed elicitation is never consent. Don't log client result contents.
        return False
    content = response.content
    return (
        response.action == "accept"
        and content is not None
        and set(content) == {"approve"}
        and content["approve"] is True
    )


async def _add_to_watchlist(
    ctx: ServerRequestContext, grant: Grant, args: AddToWatchlistArgs, disconnected: asyncio.Event
) -> dict | types.CallToolResult:
    if not grant.allow_watchlist_add:
        return _tool_error("This credential cannot add to the watchlist")
    capabilities = ctx.session.client_capabilities
    elicitation = capabilities.elicitation if capabilities is not None else None
    if (
        elicitation is None
        or (elicitation.form is None and elicitation.url is not None)
        or not ctx.session.can_send_request
        or ctx.request_id is None
    ):
        return _tool_error("Client form elicitation is required")
    async with db_session.SessionLocal() as db:
        target = await load_approval_target(db, args.media_id)
    if target is None:
        return _tool_error("No supported local catalog title for this ID")
    if disconnected.is_set() or not await _request_watchlist_approval(ctx, target) or disconnected.is_set():
        return _tool_error("Watchlist addition not approved")
    # The lookup session has already closed. Start a fresh transaction after consent.
    async with db_session.SessionLocal() as db:
        result = await add_approved_watchlist_item(db, grant.user_id, target)
        # The SDK can still receive the form reply on another POST after this
        # call's SSE stream disconnected. There is no resumption on this listener.
        if disconnected.is_set():
            return _tool_error("Watchlist addition not approved")
        if result["status"] == "created":
            await db.commit()
    if result["status"] == "account_inactive":
        return _tool_error("Account is no longer active")
    if result["status"] == "target_changed":
        return _tool_error("Catalog title changed; resolve it again before adding")
    return result


def _build_server() -> Server:
    cursor_key = secrets.token_bytes(32)
    read_only = types.ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )
    history = types.Tool(
        name="list_history",
        description="Page through this account's stored watches, newest first. Re-watches and duplicate rows are kept.",
        input_schema=HistoryArgs.model_json_schema(),
        annotations=read_only,
    )
    title = types.Tool(
        name="find_title",
        description=(
            "Match an exact case-sensitive local title (optionally year/type), or IMDb ID alone. "
            "No aliases or provider lookup. A miss says nothing about history outside the local catalog."
        ),
        input_schema=FindTitleArgs.model_json_schema(),
        annotations=read_only,
    )
    watchlist = types.Tool(
        name="add_to_watchlist",
        description=(
            "Add one local movie, TV, or anime catalog ID to this account's watchlist after one-time approval. "
            "Existing rows (including dropped or removed rows) are left untouched."
        ),
        input_schema=AddToWatchlistArgs.model_json_schema(),
        annotations=types.ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True
        ),
    )

    async def list_tools(
        _ctx: ServerRequestContext, _params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[history, title, watchlist])

    async def call_tool(ctx: ServerRequestContext, params: types.CallToolRequestParams) -> types.CallToolResult:
        scope = ctx.request.scope if isinstance(ctx.request, Request) else {}
        grant = scope.get(_GRANT_SCOPE_KEY)
        disconnected = scope.get(_DISCONNECT_SCOPE_KEY)
        if not isinstance(grant, Grant) or not isinstance(disconnected, asyncio.Event):
            return _tool_error("Authentication required")
        try:
            arguments = params.arguments if params.arguments is not None else {}
            if params.name == history.name:
                async with db_session.SessionLocal() as db:
                    result = await list_history(
                        db, grant.user_id, grant.fingerprint, cursor_key, HistoryArgs.model_validate(arguments)
                    )
            elif params.name == title.name:
                async with db_session.SessionLocal() as db:
                    result = await find_title(db, grant.user_id, FindTitleArgs.model_validate(arguments))
            elif params.name == watchlist.name:
                result = await _add_to_watchlist(ctx, grant, AddToWatchlistArgs.model_validate(arguments), disconnected)
            else:
                return _tool_error("Unknown tool")
        except ValidationError:
            return _tool_error("Invalid tool arguments")
        except InvalidCursor:
            return _tool_error("Invalid or expired history cursor")
        except Exception:
            logger.error("LibrarySync MCP tool failed")
            return _tool_error("Tool operation failed")
        if isinstance(result, types.CallToolResult):
            return result
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
            structured_content=result,
        )

    return Server("LibrarySync", on_list_tools=list_tools, on_call_tool=call_tool)


def create_app() -> Starlette:
    """Uvicorn ASGI factory for the separate MCP listener, disabled by default."""
    middleware = [Middleware(_NoStore)]
    if os.environ.get("LIBRARYSYNC_MCP_ENABLED", "false").lower() != "true":
        return Starlette(middleware=middleware)

    manager = StreamableHTTPSessionManager(_build_server(), max_request_body_size=32 * 1024, max_sessions=64)
    endpoint = _Endpoint(manager, _configured_hosts(), _configured_grants())

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            yield

    app = Starlette(routes=[Route("/mcp", endpoint)], lifespan=lifespan, middleware=middleware)
    app.router.redirect_slashes = False
    return app

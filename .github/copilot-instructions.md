# librarySync - Copilot Instructions

Follow [`AGENTS.md`](../AGENTS.md) at the repository root. It is the single source of truth for the
architecture, worker modes, API surface, security requirements, developer guidance and tests, so
keep project guidance there instead of duplicating it here.

Quick reminders:

- Python 3.13+, FastAPI, async SQLAlchemy 2, PostgreSQL, Alembic; vanilla JS + Tailwind UI.
- `uv sync --group dev`, `uv run ruff check backend scripts`, `uv run ruff format backend scripts`,
  `uv run --directory backend pytest -q`.
- Use `get_http_client()` for HTTP, `core/integration_tokens.py` for OAuth refreshes and
  `core/url_safety.py` for user-supplied URLs. Never log secrets.

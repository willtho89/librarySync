FROM node:24-alpine AS frontend-build

WORKDIR /app/frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/input.css ./
COPY backend/src/librarysync/templates /app/backend/src/librarysync/templates
COPY backend/src/librarysync/static/*.js /app/backend/src/librarysync/static/

RUN npm run build

FROM ghcr.io/astral-sh/uv:0.12.23 AS uv

FROM python:3.14-slim AS builder

COPY --from=uv /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /src

# Install the locked third-party dependencies first so this layer is reused
# until uv.lock or a pyproject.toml changes.
COPY pyproject.toml uv.lock ./
COPY backend/pyproject.toml backend/pyproject.toml
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --package librarysync

COPY backend/src backend/src
COPY --from=frontend-build /app/backend/src/librarysync/static/styles.css backend/src/librarysync/static/styles.css
COPY --from=frontend-build /app/backend/src/librarysync/static/chart.min.js backend/src/librarysync/static/chart.min.js
COPY --from=frontend-build /app/backend/src/librarysync/static/chart.umd.min.js.map backend/src/librarysync/static/chart.umd.min.js.map

# Install librarysync itself as a regular (non-editable) package so the
# runtime stage only needs the virtualenv.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package librarysync

FROM python:3.14-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

RUN groupadd --system --gid 10001 app && \
    useradd --system --uid 10001 --gid app --create-home --home-dir /home/app app

COPY --from=builder /app/.venv /app/.venv

USER app

EXPOSE 8000

# No HEALTHCHECK: the same image runs the API (uvicorn on :8000) and the
# worker (`python -m librarysync.worker`, no HTTP listener), so a single
# image-level check cannot be correct for both. docker-compose.yml defines
# role-specific health checks instead.

CMD ["uvicorn", "librarysync.main:app", "--host", "0.0.0.0", "--port", "8000"]

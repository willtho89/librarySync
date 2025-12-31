FROM python:3.14-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir -U pip uv
RUN groupadd --system --gid 10001 app && \
    useradd --system --uid 10001 --gid app --create-home --home-dir /home/app app

COPY --chown=app:app backend/pyproject.toml /app/backend/pyproject.toml
COPY --chown=app:app backend/src /app/backend/src
RUN uv pip install --system /app/backend

COPY --chown=app:app worker/pyproject.toml /app/pyproject.toml
COPY --chown=app:app worker/src /app/src

RUN uv pip install --system .

USER app

CMD ["python", "-m", "librarysync_worker.main"]

FROM python:3.14-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir -U pip uv

COPY backend/pyproject.toml /app/backend/pyproject.toml
COPY backend/src /app/backend/src
RUN uv pip install --system /app/backend

COPY worker/pyproject.toml /app/pyproject.toml
COPY worker/src /app/src

RUN uv pip install --system .

CMD ["python", "-m", "librarysync_worker.main"]

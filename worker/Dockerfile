FROM python:3.14-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir -U pip uv

COPY worker/pyproject.toml /app/pyproject.toml
COPY worker/src /app/src
COPY backend/src /app/src

RUN uv pip install --system .

CMD ["python", "-m", "librarysync_worker.main"]

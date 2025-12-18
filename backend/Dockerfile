FROM python:3.14-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir -U pip uv

COPY backend/pyproject.toml /app/pyproject.toml
COPY backend/src /app/src

RUN uv pip install --system .

CMD ["uvicorn", "librarysync.main:app", "--host", "0.0.0.0", "--port", "8000"]

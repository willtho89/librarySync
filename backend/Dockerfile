FROM node:20-alpine AS frontend-build

WORKDIR /app/frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/input.css ./
COPY backend/src/librarysync/templates /app/backend/src/librarysync/templates
COPY backend/src/librarysync/static/*.js /app/backend/src/librarysync/static/

RUN npm run build

FROM python:3.14-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir -U pip uv
RUN groupadd --system --gid 10001 app && \
    useradd --system --uid 10001 --gid app --create-home --home-dir /home/app app

COPY --chown=app:app backend/pyproject.toml /app/pyproject.toml
COPY --chown=app:app backend/src /app/src
COPY --from=frontend-build /app/backend/src/librarysync/static/styles.css /app/src/librarysync/static/styles.css
COPY --from=frontend-build /app/backend/src/librarysync/static/chart.min.js /app/src/librarysync/static/chart.min.js
COPY --from=frontend-build /app/backend/src/librarysync/static/chart.umd.min.js.map /app/src/librarysync/static/chart.umd.min.js.map

RUN uv pip install --system .

USER app

CMD ["uvicorn", "librarysync.main:app", "--host", "0.0.0.0", "--port", "8000"]

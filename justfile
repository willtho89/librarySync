# Developer shortcuts. Run `just` to list recipes.

# Pass the repo-root .env to the app when it exists (the app itself does not load .env).
env_file := if path_exists(join(justfile_directory(), ".env")) == "true" { "--env-file .env" } else { "" }

# List recipes
default:
    @{{ just_executable() }} --list

# Install Python (uv workspace, incl. dev group) and frontend dependencies
sync:
    uv sync
    npm --prefix frontend ci

# Run the backend test suite; extra args go to pytest (e.g. `just test -k ratings`)
test *args:
    uv run --directory backend pytest {{ args }}

# Lint with ruff
lint:
    uv run ruff check .

# Format with ruff
fmt:
    uv run ruff format .

# Build Tailwind CSS into backend/src/librarysync/static/styles.css
css:
    npm --prefix frontend run build:css

# Run the API with auto-reload on http://localhost:8000
dev-api:
    uv run {{ env_file }} uvicorn librarysync.main:app --reload --reload-dir backend/src --host 0.0.0.0 --port 8000

# Run the worker (modes from LIBRARYSYNC_WORKER_MODES, default: all)
dev-worker:
    uv run {{ env_file }} python -m librarysync.worker

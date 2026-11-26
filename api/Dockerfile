# ── Frontend builder ─────────────────────────────────────────────────────────
# BuildKit skips this stage entirely when target=test (docker-compose dev mode).
FROM node:20-slim AS frontend-builder
WORKDIR /frontend
# Vite bakes VITE_* env vars into the JS bundle at build time.
ARG VITE_CLERK_PUBLISHABLE_KEY
ENV VITE_CLERK_PUBLISHABLE_KEY=$VITE_CLERK_PUBLISHABLE_KEY
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ── Python API base ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS base

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY api/pyproject.toml api/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY api/app/ app/
COPY api/migrations/ migrations/
COPY api/alembic.ini .
COPY api/scripts/ scripts/

FROM base AS test
RUN uv sync --frozen --no-install-project --extra dev
COPY api/tests/ tests/

FROM base AS production
COPY --from=frontend-builder /frontend/dist frontend/dist
# Installs the project itself once, at build time (its version is /health's).
RUN uv sync --frozen --no-dev
RUN adduser --disabled-password --gecos "" --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser
EXPOSE 8000
# The venv's python as PID 1, not `uv run` (BUG-021).
CMD ["/app/.venv/bin/python", "-m", "app.serve"]

FROM python:3.12-slim AS base

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app/ app/
COPY migrations/ migrations/
COPY alembic.ini .
COPY scripts/ scripts/

FROM base AS test
RUN uv sync --frozen --no-install-project --extra dev
COPY tests/ tests/

FROM base AS production
RUN adduser --disabled-password --gecos "" --uid 1000 appuser
USER appuser
EXPOSE 8000
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

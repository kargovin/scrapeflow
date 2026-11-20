FROM python:3.12-slim AS builder

WORKDIR /build
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
# The venv is copied to the runtime stage, so it must link the image's own interpreter.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON_DOWNLOADS=never

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

FROM python:3.12-slim

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1

WORKDIR /app
COPY worker/ ./worker/

RUN useradd --no-create-home --uid 1000 appuser
USER appuser

# The venv's python as PID 1 (not `uv run`, BUG-021): SIGTERM reaches worker.main's handler.
CMD ["python", "-m", "worker.main"]

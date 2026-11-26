FROM python:3.12-slim AS builder

WORKDIR /build
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
# The venv is copied to the runtime stage, so it must link the image's own interpreter.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON_DOWNLOADS=never

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM builder AS dev-builder
RUN uv sync --frozen --no-install-project

FROM python:3.12-slim AS runtime-base
ENV PATH="/opt/venv/bin:$PATH"
WORKDIR /app
RUN useradd --no-create-home --uid 1001 appuser

# docker build --target test -t scrapeflow-mcp-test mcp/
FROM runtime-base AS test
COPY --from=dev-builder /opt/venv /opt/venv
COPY . .
USER appuser
CMD ["python", "-m", "pytest", "tests/", "-q", "-p", "no:cacheprovider"]

FROM runtime-base
COPY --from=builder /opt/venv /opt/venv
COPY . .
USER appuser
CMD ["python", "server.py"]

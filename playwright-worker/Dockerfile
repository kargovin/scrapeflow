FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy AS builder

WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends python3.10-venv && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml .
RUN pip install --no-cache-dir .

FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY worker/ ./worker/

RUN useradd --no-create-home --uid 1001 appuser
USER appuser

CMD ["python", "-m", "worker.main"]

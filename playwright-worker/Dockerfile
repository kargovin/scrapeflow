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
COPY tests/ ./tests/
COPY pyproject.toml .

# Install real Google Chrome for Patchright's channel="chrome" (stealth): the UA
# then reports a genuine Chrome instead of bundled Chromium. Run as root, before
# dropping privileges, so the binary lands in a world-readable system path.
RUN patchright install chrome

# --create-home: headed Chrome writes its profile/cache under $HOME, so the
# runtime user needs a writable home directory.
RUN useradd --create-home --uid 1001 appuser
ENV HOME=/home/appuser
USER appuser

# Run under Xvfb: Chrome must be truly headed (not --headless) to report a clean,
# non-"HeadlessChrome" UA, which requires a virtual display in a container.
CMD ["xvfb-run", "-a", "--server-args=-screen 0 1920x1080x24", "python", "-m", "worker.main"]

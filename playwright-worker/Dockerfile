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
# Flush stdout/stderr immediately so worker logs (and any crash traceback) reach
# `kubectl logs` instead of dying in a buffer when the process exits.
ENV PYTHONUNBUFFERED=1

WORKDIR /app
COPY worker/ ./worker/
COPY tests/ ./tests/
COPY pyproject.toml .
COPY entrypoint.sh .
RUN chmod +x /app/entrypoint.sh

# Install real Google Chrome for Patchright's channel="chrome" (stealth): the UA
# then reports a genuine Chrome instead of bundled Chromium. Run as root, before
# dropping privileges, so the binary lands in a world-readable system path.
RUN patchright install chrome

# Xvfb refuses to create /tmp/.X11-unix as a non-root user, so create it here
# (sticky, world-writable) while we are still root.
RUN mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix

# --create-home: headed Chrome writes its profile/cache under $HOME, so the
# runtime user needs a writable home directory.
RUN useradd --create-home --uid 1001 appuser
ENV HOME=/home/appuser
USER appuser

# entrypoint.sh starts Xvfb (Chrome must be truly headed for a clean UA, which
# needs a virtual display), waits for it, then exec's the worker as pid 1 so a
# crash surfaces to k8s instead of being masked by an xvfb-run wrapper.
CMD ["/app/entrypoint.sh"]

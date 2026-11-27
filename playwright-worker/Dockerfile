FROM mcr.microsoft.com/playwright/python:v1.63.0-noble AS builder

WORKDIR /build
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
# The venv is copied to the runtime stage, so it must link the image's own interpreter.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON_DOWNLOADS=never UV_PYTHON=python3

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM builder AS dev-builder
RUN uv sync --frozen --no-install-project

FROM mcr.microsoft.com/playwright/python:v1.63.0-noble AS runtime

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
# Pinned: bump both values together from google-chrome-stable in
# https://dl.google.com/linux/chrome/deb/dists/stable/main/binary-amd64/Packages
ARG CHROME_VERSION=158.0.8127.88
ARG CHROME_SHA256=a656b2db00a8cb7f13a1d754f6aad91f84014906452c6dfb7f3ead93035da03b
RUN curl -fsSL -o /tmp/chrome.deb \
        "https://dl.google.com/linux/chrome/deb/pool/main/g/google-chrome-stable/google-chrome-stable_${CHROME_VERSION}-1_amd64.deb" \
    && echo "${CHROME_SHA256}  /tmp/chrome.deb" | sha256sum -c - \
    && apt-get update \
    && apt-get install -y /tmp/chrome.deb \
    && rm -rf /tmp/chrome.deb /var/lib/apt/lists/* /etc/apt/sources.list.d/google-chrome.* \
    && google-chrome --version | grep -qF "Google Chrome ${CHROME_VERSION}"

# Xvfb refuses to create /tmp/.X11-unix as a non-root user, so create it here
# (sticky, world-writable) while we are still root.
RUN mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix

# --create-home: headed Chrome writes its profile/cache under $HOME, so the
# runtime user needs a writable home directory.
# noble's base image already holds uid 1001 (pwuser).
RUN userdel --remove pwuser && useradd --create-home --uid 1001 appuser
ENV HOME=/home/appuser
USER appuser

# entrypoint.sh starts Xvfb (Chrome must be truly headed for a clean UA, which
# needs a virtual display), waits for it, then exec's the worker as pid 1 so a
# crash surfaces to k8s instead of being masked by an xvfb-run wrapper.
CMD ["/app/entrypoint.sh"]

# Tests: docker build --target test -t playwright-worker-test playwright-worker/
FROM runtime AS test
COPY --from=dev-builder /opt/venv /opt/venv

FROM runtime

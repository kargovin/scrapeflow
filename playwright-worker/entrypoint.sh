#!/bin/sh
# Start a virtual X display, then hand off to the worker as the container's main
# process. Replaces `xvfb-run`, which (a) masked worker crashes — as pid 1 it kept
# the container "Running" with a dead Python child, so k8s never restarted it — and
# (b) raced Chrome against Xvfb on cold start. Here we wait for Xvfb to be ready and
# `exec` Python so a crash propagates to k8s (CrashLoopBackOff) and SIGTERM reaches
# the worker's graceful-shutdown handler directly.
set -e

SERVERNUM="${XVFB_SERVERNUM:-99}"
SCREEN="${XVFB_SCREEN:-1920x1080x24}"

Xvfb ":${SERVERNUM}" -screen 0 "${SCREEN}" -nolisten tcp &
export DISPLAY=":${SERVERNUM}"

# Wait for the X socket to appear before launching Chrome (fixes the cold-start race).
i=0
while [ ! -S "/tmp/.X11-unix/X${SERVERNUM}" ]; do
    i=$((i + 1))
    if [ "$i" -gt 100 ]; then
        echo "entrypoint: Xvfb failed to start on :${SERVERNUM} within 10s" >&2
        exit 1
    fi
    sleep 0.1
done

exec python -m worker.main

#!/bin/sh
# The delivery worker's MinIO user: read-only on scrapeflow-results (ADR-015 §4). Idempotent; re-run
# on every `up`. k8s: the MinIO chart's `users` and `policies` values.
set -eu

mc alias set local http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
mc admin policy create local delivery-read /scripts/delivery-read.json
mc admin user add local "$DELIVERY_MINIO_USER" "$DELIVERY_MINIO_PASSWORD"
# Attaching an attached policy is an error; the image has no grep.
case "$(mc admin user info local "$DELIVERY_MINIO_USER")" in
    *delivery-read*) ;;
    *) mc admin policy attach local delivery-read --user "$DELIVERY_MINIO_USER" ;;
esac
mc admin user info local "$DELIVERY_MINIO_USER"

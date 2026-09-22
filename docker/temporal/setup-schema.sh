#!/bin/sh
# Temporal schema step — runs before the server on every start (compose: the `temporal-schema`
# one-shot the server depends on; k8s: an init container on the server Deployment). Idempotent:
# `setup-schema -v 0.0` only stamps a database that has no version yet, and `update-schema`
# applies the versioned diffs from the stamped version up to the one baked into this image —
# a no-op once they agree. Bumping the server therefore means bumping this image's tag too.
#
# Deliberately no `temporal-sql-tool create`: the databases exist already — the Postgres
# instance creates them on first boot (docker/temporal-postgres/init.sql). This script owns
# the schema inside them and nothing else. Adapted from temporalio/samples-server
# compose/scripts/setup-postgres.sh.
set -eu

: "${POSTGRES_SEEDS:?POSTGRES_SEEDS is required (the temporal-postgres host)}"
: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${SQL_PASSWORD:?SQL_PASSWORD is required (temporal-sql-tool reads the password from it)}"
DB_PORT="${DB_PORT:-5432}"
SCHEMA_ROOT=/etc/temporal/schema/postgresql/v12

echo "Waiting for ${POSTGRES_SEEDS}:${DB_PORT}..."
until nc -z -w 5 "${POSTGRES_SEEDS}" "${DB_PORT}"; do
  sleep 2
done

# Usage: apply_schema <database> <versioned schema dir>
apply_schema() {
  echo "== ${1}: setup-schema (stamps version 0.0 if unstamped)"
  temporal-sql-tool --plugin postgres12 --ep "${POSTGRES_SEEDS}" -p "${DB_PORT}" \
    -u "${POSTGRES_USER}" --db "${1}" setup-schema -v 0.0
  echo "== ${1}: update-schema to the image's version"
  temporal-sql-tool --plugin postgres12 --ep "${POSTGRES_SEEDS}" -p "${DB_PORT}" \
    -u "${POSTGRES_USER}" --db "${1}" update-schema -d "${SCHEMA_ROOT}/${2}/versioned"
}

apply_schema "${DBNAME:-temporal}" temporal
apply_schema "${VISIBILITY_DBNAME:-temporal_visibility}" visibility

echo "Temporal schema is current in ${DBNAME:-temporal} and ${VISIBILITY_DBNAME:-temporal_visibility}"

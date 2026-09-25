#!/bin/sh
# Temporal namespace registration — ADR-009 §2 (one namespace, §12) and §2c (retention). Runs
# after the server is up (compose: the `temporal-namespace` one-shot; k8s: the
# scrapeflow-temporal-init Job). Converges rather than only creates, like nats-init-job.yaml's
# `stream add` / `stream edit`: a missing namespace is created, an existing one has its retention
# set to the value here — so this file, not a past `temporal operator` command, is the source of
# truth for retention. Safe to re-run; re-running it is also the recovery after the Temporal
# database is wiped.
#
# Written for this project, not copied from temporalio/samples-server's create-namespace.sh:
# that one sets no retention (the CLI default is 72h, not 30d), and its retry branch references
# a misspelled variable, which `set -u` turns into a failure on exactly the path it guards.
set -eu

: "${TEMPORAL_ADDRESS:?TEMPORAL_ADDRESS is required (host:port of the Temporal frontend)}"
NAMESPACE="${TEMPORAL_NAMESPACE:-scrapeflow}"
RETENTION="${TEMPORAL_NAMESPACE_RETENTION:-720h}" # 30 days (ADR-009 §2c) — the CLI wants hours
MAX_ATTEMPTS="${MAX_ATTEMPTS:-60}"                 # × 5 s = 5 min for the server to come up

attempt=1
echo "Waiting for the Temporal frontend at ${TEMPORAL_ADDRESS}..."
until temporal operator cluster health --address "${TEMPORAL_ADDRESS}" >/dev/null 2>&1; do
  if [ "${attempt}" -ge "${MAX_ATTEMPTS}" ]; then
    echo "Temporal not healthy after ${MAX_ATTEMPTS} attempts; giving up" >&2
    exit 1
  fi
  attempt=$((attempt + 1))
  sleep 5
done

# `describe` failing is read as "does not exist". Any other failure falls through to `create`,
# which then fails loudly (already exists / unreachable) — a non-zero exit, never a silent skip.
if temporal operator namespace describe --namespace "${NAMESPACE}" \
     --address "${TEMPORAL_ADDRESS}" >/dev/null 2>&1; then
  echo "Namespace ${NAMESPACE} exists; setting retention to ${RETENTION}"
  temporal operator namespace update --namespace "${NAMESPACE}" \
    --retention "${RETENTION}" --address "${TEMPORAL_ADDRESS}"
else
  echo "Creating namespace ${NAMESPACE} with retention ${RETENTION}"
  temporal operator namespace create --namespace "${NAMESPACE}" \
    --retention "${RETENTION}" --address "${TEMPORAL_ADDRESS}"
fi

temporal operator namespace describe --namespace "${NAMESPACE}" --address "${TEMPORAL_ADDRESS}"

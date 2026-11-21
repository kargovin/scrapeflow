#!/usr/bin/env bash
# delivery-gate.sh — the M.12 compose gate (PRD-017 slice 1, PRD-019's bucket criterion, ADR-015 §8's
# rebinding and known-secret checks). Run from anywhere, with the compose stack up:
#
#   scripts/delivery-gate.sh [--no-regression] [scenario …]
#
# 1. brings up the gate fixtures and recreates the delivery and workflow workers with the gate CA
#    (docker/delivery-gate.yml);
# 2. makes the gate's MinIO buckets and users, their secret keys built from a fresh known secret;
# 3. runs api/scripts/delivery_gate.py (API responses and workflow histories searched for the secret);
# 4. unless --no-regression: the pipeline and monitor gates, unchanged (pipelines saved before M);
# 5. searches the containers' logs since step 1 for the secret, checks the delivery worker was not
#    restarted, and runs the rebinding test against both egress copies;
# 6. always: removes the MinIO users and buckets and puts both workers back on the plain config.
#
# Exit status 1 on any mismatch. The workflow worker must run this tree's app/ (it is recreated here).
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO/docker"
GATE=(docker compose -f docker-compose.yml -f delivery-gate.yml --profile gate)
MC=("${GATE[@]}" run --rm --no-deps -v "$REPO/contracts/fixtures/delivery-gate:/gate:ro" --entrypoint sh)
LOGGED=(api workflow-worker delivery-worker llm-worker-temporal http-worker-temporal
        playwright-worker-temporal temporal postgres)

regression=1
if [ "${1:-}" = "--no-regression" ]; then regression=0; shift; fi

SECRET="gks$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')"
STARTED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
failed=0

restore() {
    "${MC[@]}" minio-users /gate/minio.sh teardown >/dev/null 2>&1
    docker compose up -d delivery-worker workflow-worker >/dev/null 2>&1
    echo "workers back on the plain config; gate buckets and users removed"
}
trap restore EXIT

"${GATE[@]}" up -d --wait delivery-fixture pipeline-fixture || exit 1
"${GATE[@]}" up -d --wait --force-recreate delivery-worker workflow-worker || exit 1
GATE_SECRET="$SECRET" "${MC[@]}" -e GATE_SECRET minio-users /gate/minio.sh setup >/dev/null || exit 1
worker="$("${GATE[@]}" ps -q delivery-worker)"
life="$(docker inspect -f '{{.State.StartedAt}}' "$worker")"

GATE_SECRET="$SECRET" GATE_STARTED="$STARTED" "${GATE[@]}" exec -T -e GATE_SECRET -e GATE_STARTED -e DEBUG=false api \
    uv run python -m scripts.delivery_gate "$@" || failed=1

if [ "$regression" = 1 ]; then
    for g in pipeline_gate monitor_gate; do
        echo "== $g (pipelines saved before M, unchanged)"
        "${GATE[@]}" exec -T -e DEBUG=false api uv run python -m "scripts.$g" | grep -E '^(MISMATCH|GATE)' || failed=1
    done
fi

echo "== the known secret in the containers' logs since $STARTED"
for service in "${LOGGED[@]}"; do
    logs="$("${GATE[@]}" logs --since "$STARTED" --no-log-prefix "$service" 2>&1)"
    hits="$(grep -c -- "$SECRET" <<<"$logs")"
    echo "  $service: $(wc -l <<<"$logs") lines, $hits with the secret"
    [ "$hits" = 0 ] || failed=1
done

if [ "$(docker inspect -f '{{.State.StartedAt}}' "$worker")" = "$life" ]; then
    echo "== one delivery-worker life for the whole gate (started $life)"
else
    echo "MISMATCH  the delivery worker restarted during the gate"
    failed=1
fi

echo "== the rebinding test against both egress copies"
docker run --rm -v "$REPO:/repo" -w /repo docker-api /app/.venv/bin/python -m pytest \
    contracts/test_egress.py -q -p no:cacheprovider -k rebind | tail -1 || failed=1

[ "$failed" = 0 ] && echo "DELIVERY GATE PASSED" || echo "DELIVERY GATE FAILED"
exit "$failed"

#!/usr/bin/env bash
# approval-gate.sh — the N.5 compose gate (PRD-018's approval criteria). Run from anywhere, with the
# compose stack up:
#
#   scripts/approval-gate.sh [--no-waits] [--no-regression] [scenario …]
#
# 1. brings up the gate fixtures and recreates the delivery and workflow workers with the gate CA
#    (docker/delivery-gate.yml), as the delivery gate does;
# 2. makes the gate's MinIO buckets and users, their secret keys built from a fresh secret;
# 3. runs api/scripts/approval_gate.py (≈ 65 min: three scenarios wait out a 1-hour deadline and a
#    3-hour approval's reminder point; --no-waits skips them);
# 4. puts the workers back on the plain config and removes the MinIO users and buckets;
# 5. unless --no-regression: scripts/delivery-gate.sh — the M.12 gate with the J.10 and L.12 gates
#    (pipelines saved before N, unchanged).
#
# Exit status 1 on any mismatch. The workflow worker must run this tree's app/ (it is recreated here).
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO/docker"
GATE=(docker compose -f docker-compose.yml -f delivery-gate.yml --profile gate)
MC=("${GATE[@]}" run --rm --no-deps -v "$REPO/contracts/fixtures/delivery-gate:/gate:ro" --entrypoint sh)

args=()
regression=1
for a in "$@"; do
    case "$a" in
        --no-regression) regression=0 ;;
        *) args+=("$a") ;;
    esac
done

SECRET="gks$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')"
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

GATE_SECRET="$SECRET" "${GATE[@]}" exec -T -e GATE_SECRET -e DEBUG=false -e PYTHONUNBUFFERED=1 api \
    uv run python -m scripts.approval_gate "${args[@]}" || failed=1
restore
trap - EXIT

if [ "$regression" = 1 ]; then
    echo "== the delivery gate, with the pipeline and monitor gates (saved before N, unchanged)"
    out="$("$REPO/scripts/delivery-gate.sh" 2>&1)" || failed=1
    grep -E '^(MISMATCH|GATE|DELIVERY GATE|== )' <<<"$out"
fi

[ "$failed" = 0 ] && echo "APPROVAL GATE PASSED" || echo "APPROVAL GATE FAILED"
exit "$failed"

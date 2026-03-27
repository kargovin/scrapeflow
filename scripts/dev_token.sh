#!/usr/bin/env bash
# dev_token.sh — call the local ScrapeFlow API using either an API key or Clerk JWT.
#
# Usage (API key — preferred):
#   ./scripts/dev_token.sh --api-key sf_<key>
#   ./scripts/dev_token.sh --api-key sf_<key> /jobs
#   ./scripts/dev_token.sh --api-key sf_<key> /jobs POST '{"url":"https://example.com","output_format":"html"}'
#
# Usage (Clerk JWT — for bootstrapping):
#   ./scripts/dev_token.sh --clerk sk_test_<key>
#   ./scripts/dev_token.sh --clerk sk_test_<key> /jobs POST '{"url":"https://example.com","output_format":"html"}'

set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 --api-key <sf_key> [endpoint] [method] [body]"
  echo "       $0 --clerk <clerk_secret_key> [endpoint] [method] [body]"
  exit 1
fi

MODE="$1"
KEY="$2"
API_BASE="http://localhost:8000"
ENDPOINT="${3:-/users/me}"
METHOD="${4:-GET}"
BODY="${5:-}"

# --- Resolve auth header ---
if [[ "$MODE" == "--api-key" ]]; then
  AUTH_HEADER="X-API-Key: ${KEY}"

elif [[ "$MODE" == "--clerk" ]]; then
  echo "► Fetching Clerk user..."
  USER_ID=$(curl -s "https://api.clerk.com/v1/users" \
    -H "Authorization: ${KEY}" \
    | python3 -c "import sys,json; users=json.load(sys.stdin); print(users[0]['id'])")
  echo "  User ID: ${USER_ID}"

  echo "► Fetching active session..."
  SESSION_ID=$(curl -s "https://api.clerk.com/v1/sessions?user_id=${USER_ID}&status=active" \
    -H "Authorization: ${KEY}" \
    | python3 -c "import sys,json; sessions=json.load(sys.stdin); print(sessions[0]['id'])")
  echo "  Session ID: ${SESSION_ID}"

  echo "► Fetching fresh JWT..."
  JWT=$(curl -s -X POST "https://api.clerk.com/v1/sessions/${SESSION_ID}/tokens" \
    -H "Authorization: ${KEY}" \
    -H "Content-Type: application/json" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['jwt'])")
  echo "  JWT obtained (expires in ~60s)"
  AUTH_HEADER="Authorization: Bearer ${JWT}"

else
  echo "Error: first argument must be --api-key or --clerk"
  exit 1
fi

# --- Call the local API ---
echo "► ${METHOD} ${API_BASE}${ENDPOINT}"
if [[ -n "$BODY" ]]; then
  curl -s -X "${METHOD}" "${API_BASE}${ENDPOINT}" \
    -H "${AUTH_HEADER}" \
    -H "Content-Type: application/json" \
    -d "${BODY}" \
    | python3 -m json.tool
else
  curl -s -X "${METHOD}" "${API_BASE}${ENDPOINT}" \
    -H "${AUTH_HEADER}" \
    | python3 -m json.tool
fi

#!/usr/bin/env bash

set -Eeuo pipefail

IMAGE="${1:-openkyrozen-test}"
BASE_PORT="${DOCKER_SMOKE_BASE_PORT:-18080}"
PORT_A="${BASE_PORT}"
PORT_B="$((BASE_PORT + 1))"
RUN_SUFFIX="${GITHUB_RUN_ID:-local}-${RANDOM}-$$"
VOLUME_NAME="openkyrozen-smoke-${RUN_SUFFIX}"
CONTAINER_A="openkyrozen-smoke-a-${RUN_SUFFIX}"
CONTAINER_B="openkyrozen-smoke-b-${RUN_SUFFIX}"
TOKEN="openkyrozen-smoke-token"

require_command() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "docker persistence smoke test requires '$1'" >&2
        exit 2
    }
}

cleanup() {
    docker rm -f "$CONTAINER_A" "$CONTAINER_B" >/dev/null 2>&1 || true
    docker volume rm "$VOLUME_NAME" >/dev/null 2>&1 || true
}

wait_for_server() {
    local container="$1"
    local port="$2"
    local response

    for _ in $(seq 1 90); do
        if response="$(curl --silent --show-error --fail --max-time 3 \
            -H "Authorization: Bearer ${TOKEN}" \
            "http://127.0.0.1:${port}/api/health" 2>/dev/null)" \
            && python3 -c 'import json, sys; data = json.load(sys.stdin); raise SystemExit(0 if data.get("status") == "ok" and data.get("provider") == "ollama" else 1)' <<<"$response"; then
            return 0
        fi
        sleep 1
    done

    echo "container did not become healthy: ${container}" >&2
    docker logs "$container" >&2 || true
    return 1
}

run_server() {
    local container="$1"
    local port="$2"

    docker run --detach --name "$container" \
        --publish "127.0.0.1:${port}:8000" \
        --env KYROZEN_PROVIDER=ollama \
        --env KYROZEN_SERVER_TOKEN="$TOKEN" \
        --env KYROZEN_DB_PATH=/data/openkyrozen.sqlite3 \
        --env KYROZEN_DISABLE_VECTOR_INDEX=1 \
        --mount "type=volume,src=${VOLUME_NAME},dst=/data" \
        "$IMAGE" >/dev/null
}

require_command docker
require_command curl
require_command python3
docker image inspect "$IMAGE" >/dev/null

trap cleanup EXIT INT TERM
docker volume create "$VOLUME_NAME" >/dev/null

run_server "$CONTAINER_A" "$PORT_A"
wait_for_server "$CONTAINER_A" "$PORT_A"

test "$(docker exec "$CONTAINER_A" id -u)" = "10001"
test "$(docker exec "$CONTAINER_A" sh -c 'test -w /data && stat -c %u /data')" = "10001"

claim_payload='{"key":"docker_persistence_smoke","value":"survives container replacement","kind":"fact","authority":"owner","scope":"global","claim_type":"general","visibility":"public"}'
created="$(curl --silent --show-error --fail \
    -X POST "http://127.0.0.1:${PORT_A}/api/v2/memory/claims" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H 'Content-Type: application/json' \
    --data "$claim_payload")"
memory_id="$(python3 -c 'import json, sys; value = json.load(sys.stdin).get("memory_id"); assert value; print(value)' <<<"$created")"
test -n "$memory_id"

docker exec "$CONTAINER_A" sh -c 'test -f /data/openkyrozen.sqlite3'
docker rm -f "$CONTAINER_A" >/dev/null

run_server "$CONTAINER_B" "$PORT_B"
wait_for_server "$CONTAINER_B" "$PORT_B"
recovered="$(curl --silent --show-error --fail \
    "http://127.0.0.1:${PORT_B}/api/v2/memory/claims" \
    -H "Authorization: Bearer ${TOKEN}")"
python3 -c 'import json, sys; data = json.load(sys.stdin); expected = sys.argv[1]; assert any(item.get("id") == expected for item in data.get("claims", []))' "$memory_id" <<<"$recovered"

echo "Docker persistence smoke passed: memory ${memory_id} survived container replacement as uid 10001."

#!/usr/bin/env bash
# Bring the standalone OpenFGA (project pcsfga) up and wait until it answers.
# Idempotent: safe to call from every setup/scratch run; never removes data.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
docker compose -p pcsfga -f "$HERE/docker-compose.openfga.yml" up -d
for _ in $(seq 1 60); do
  if curl -sf http://localhost:8199/healthz >/dev/null 2>&1; then
    echo "openfga: up (http://localhost:8199, network pcsfga, $(curl -s http://localhost:8199/stores | python3 -c 'import sys,json; print(len(json.load(sys.stdin).get("stores",[])))') store(s))"
    exit 0
  fi
  sleep 2
done
echo "openfga did not become healthy on http://localhost:8199" >&2
exit 1

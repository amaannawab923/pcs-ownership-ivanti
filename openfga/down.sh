#!/usr/bin/env bash
# The ONLY command that deletes the standalone OpenFGA and every store in it.
# Not called by setup.sh, scratch/ or any teardown -- run it by hand, on purpose.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
read -r -p "This deletes the pcsfga OpenFGA instance AND its Postgres volume (all stores). Type 'delete' to continue: " ans
[ "$ans" = "delete" ] || { echo "aborted"; exit 1; }
docker compose -p pcsfga -f "$HERE/docker-compose.openfga.yml" down -v

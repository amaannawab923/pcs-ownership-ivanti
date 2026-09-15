#!/usr/bin/env bash
# Builds and seeds the "scratch" brownfield multi-tenant instance for
# PCS-10243 (see scratch/README.md). Re-runnable any number of times and
# always lands on the same world: every run starts from EMPTY scratch
# databases and a NEW scratch OpenFGA store (step 1 drops the two
# databases, step 2 deletes the previous run's store) and rebuilds
# everything from there. Nothing outside this shell's scratch world is
# touched: the demo `superset` database, other OpenFGA stores and other
# compose projects stay as they are. The one thing a re-run reuses is the
# built image (`setup.sh --up` builds it).
#
# ORDERING MATTERS. A real brownfield customer instance has content,
# tenants, users and row-level data FIRST, and only THEN gets the
# ownership migration applied to it -- the migration is something you
# point at an existing database, not something an existing database grows
# up around. Getting this backwards is a real trap:
# `superset_ownership.backfill.run()` never overwrites a row that already
# has an owner (see backfill.py's own docstring, and entrypoint-
# ownership.sh's comment on why re-running it is safe) -- so if the
# ownership migration ran BEFORE any tenant user existed, `superset
# load_examples`' own admin-attributed content (its created_by_fk defaults
# to the first/only user, i.e. admin, at that point) gets backfilled to
# admin-owned rows, permanently; re-attributing created_by_fk afterwards
# would then change nothing, because the rows are already "owned".
#
# So this script keeps OWNERSHIP_ENABLED=false (entrypoint-ownership.sh's
# migration/backfill/check are gated on it) for every step through
# attribution, and only flips it true and restarts `superset` ONCE, at the
# very end, so the ownership migration's backfill runs exactly once, over
# already-attributed data. The low-level superset_ownership modules the
# seed steps use directly (`fga`, `identity`) are NOT gated by
# OWNERSHIP_ENABLED (grep the package: only flags.py/cli.py/lifecycle.py/
# settings.py reference it) -- they work fine against the scratch OpenFGA
# store with the switch off, and neither does `superset.subjects`/the
# dataset/RLS/dashboard ORM work scratch/seed_tenant_data.py and
# scratch/assign_examples.py do (all core Superset tables).
#
# Order:
#   1. scratch Postgres databases (superset_scratch, examples_scratch -- see
#      scratch/docker-compose.scratch.yml's header for why examples needs
#      its own Postgres database rather than the stock per-container
#      SQLite file) + a NEW OpenFGA store (the pcssetup instance's existing
#      demo store is never touched)
#   2. stock init on that database, OWNERSHIP_ENABLED=false (`superset db
#      upgrade`, admin, `superset init`, `superset load_examples` -- the
#      content; the entrypoint's ownership migration/backfill/check does
#      NOT run yet -- the switch is off)
#   3. superset + worker up, OWNERSHIP_ENABLED=false
#   4. Ivanti-shaped identities + a realistic directory (overlay/qa/
#      seed_identity.py, overlay/qa/seed_directory.py, both run VERBATIM --
#      neither hardcodes FGA/STORE/PYTHONPATH, both resolve the connection
#      from the running app's own config, which already points at the
#      scratch store) plus this repo's own scratch/seed_fga_extra.py
#      (group `tenant` relation tuples, tenant_administrator_<tenant>
#      groups, nested membership -- additive, NOT a change to the
#      overlay/qa scripts; see that file's header)
#   5. scratch/seed_tenant_data.py: real row-level tenancy. THREE shared
#      example datasets get a genuine `tenant_id` column + RLS per tenant
#      role (both tenants can read them, different rows); every other
#      example dataset is bound exclusively to one tenant (this REPLACES
#      overlay/qa/seed_tenant_env.py in this pipeline -- that script binds
#      EVERY dataset exclusively and would fight the three shared ones;
#      see seed_tenant_data.py's own header for the reasoning); plus a
#      Neurons-shaped synthetic dataset (~400 rows) with one dashboard per
#      tenant, created by that tenant's administrator.
#   6. deterministic, DATASET-ACCESS-AWARE ownership attribution of the
#      example dashboards/charts (scratch/assign_examples.py) -- reads
#      step 5's datasource_access grants so a dashboard is never attributed
#      to a tenant that cannot read one of its own charts; violations are
#      resolved (chart unlinked from the dashboard, not deleted) and
#      printed.
#   7. flip OWNERSHIP_ENABLED=true, restart `superset` (+ `worker`): THE
#      ownership migration runs now -- `superset ownership db upgrade` ->
#      backfill (every dashboard/chart -> public, owner = the tenant user
#      steps 4-6 gave it) -> `backfill-tenants` -> `check` -- over data
#      that already looks like an existing customer's, not a fresh
#      install.
#   8. `superset ownership plugin verify`
#   9. proof queries (scratch/proof-queries.sh)
#
# Usage:
#   scratch/seed-multitenant.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

TAG="${PCS_OWNERSHIP_TAG:-dev}"
export PCS_OWNERSHIP_TAG="$TAG"
BASE_COMPOSE="docker compose -p pcssetup -f docker-compose.pcs-setup.yml"
COMPOSE="$BASE_COMPOSE -f scratch/docker-compose.scratch.yml"
OUT_DIR="scratch/.scratch-out"
mkdir -p "$OUT_DIR"

TENANT_A="a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40"
TENANT_B="b7f28e5a-9c14-4d6b-a2f0-5e3d8c1a6b92"
ADA="3f0a91c7-2d84-4e63-9b15-7c4e8a2f6d31"
CLEO="9d7e35a1-8c62-4b04-a7f1-3d5e9b2c8a76"

log() { echo; echo "############################################################"; echo "# $*"; echo "############################################################"; }

wait_openfga() {
  for _ in $(seq 1 30); do
    curl -sf http://localhost:8199/healthz >/dev/null 2>&1 && return 0
    sleep 2
  done
  echo "openfga did not become healthy" >&2; exit 1
}

wait_superset_healthy() {
  for _ in $(seq 1 60); do
    cid=$($COMPOSE ps -q superset 2>/dev/null || true)
    [ -n "$cid" ] || { sleep 5; continue; }
    status=$(docker inspect -f '{{.State.Health.Status}}' "$cid" 2>/dev/null || echo starting)
    [ "$status" = "healthy" ] && return 0
    sleep 5
  done
  echo "superset did not become healthy in time" >&2
  $COMPOSE logs --tail=80 superset >&2 || true
  exit 1
}

# ---------------------------------------------------------------------------
log "[1/9] base services up, scratch Postgres databases"
# ---------------------------------------------------------------------------
"$HERE/openfga/up.sh"          # standalone OpenFGA (project pcsfga); idempotent, never deletes
$BASE_COMPOSE up -d postgres redis
wait_openfga
# Always from scratch: a database left by an earlier run (a `down` without
# `-v`) would carry its ownership rows into the new seed, and the backfill
# never overwrites an owner it finds -- the world would then disagree with
# what the attribution step prints. Dropped HERE, once; the `scratch-db`
# one-shot only creates, because compose re-runs it whenever superset
# comes up.
for db in superset_scratch examples_scratch; do
  $BASE_COMPOSE exec -T postgres psql -U superset -c "DROP DATABASE IF EXISTS $db WITH (FORCE)" >/dev/null
done
$COMPOSE run --rm scratch-db
echo "  superset_scratch + examples_scratch databases ready (fresh)"

# ---------------------------------------------------------------------------
log "[2/9] scratch OpenFGA store (NEW -- the pcssetup demo store is untouched)"
# ---------------------------------------------------------------------------
if [ -f "$OUT_DIR/store.env" ]; then
  # A re-run starts over: the databases were just dropped (step 1), so the
  # previous run's store -- tuples keyed on object uuids that no longer
  # exist -- goes with them. Only THIS shell's scratch store is deleted;
  # every other store on the OpenFGA instance is untouched.
  # shellcheck disable=SC1091
  set -a; source "$OUT_DIR/store.env"; set +a
  if curl -sf "http://localhost:8199/stores/${SCRATCH_OPENFGA_STORE_ID:-missing}" >/dev/null 2>&1; then
    curl -sf -X DELETE "http://localhost:8199/stores/$SCRATCH_OPENFGA_STORE_ID" >/dev/null 2>&1 || true
    echo "  deleted the previous run's scratch store $SCRATCH_OPENFGA_STORE_ID; creating a new one"
  fi
  rm -f "$OUT_DIR/store.env"
fi
if [ ! -f "$OUT_DIR/store.env" ]; then
  STORE_ID=$(curl -sf -X POST http://localhost:8199/stores -H 'Content-Type: application/json' \
    -d '{"name":"pcs-scratch"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
  echo "  created store: $STORE_ID"

  # Bootstrapping create_app() needs SOME non-blank OPENFGA_STORE_ID to
  # survive docker/demo-superset-config-docker.py's blank-store guard (see
  # README's "a trap worth knowing"); it need not be the store this command
  # writes to -- `--store` below explicitly redirects the actual write.
  # Reuse the pcssetup instance's already-working demo store/model for that
  # boot if this shell has them (.seed-out/seed.env), else fall back to the
  # brand new store id itself (install-model tolerates an empty store, it
  # just has nothing to compare against).
  BOOT_STORE=""; BOOT_MODEL=""
  if [ -f .seed-out/seed.env ]; then
    BOOT_STORE=$(grep '^OPENFGA_STORE_ID=' .seed-out/seed.env | cut -d= -f2)
    BOOT_MODEL=$(grep '^OPENFGA_MODEL_ID=' .seed-out/seed.env | cut -d= -f2)
  fi
  [ -n "$BOOT_STORE" ] || BOOT_STORE="$STORE_ID"

  INSTALL_OUT=$($BASE_COMPOSE run --rm --no-deps --entrypoint superset \
    -e OPENFGA_STORE_ID="$BOOT_STORE" -e OPENFGA_MODEL_ID="$BOOT_MODEL" \
    init ownership fga install-model --store "$STORE_ID" --api-url http://openfga:8080 --pin)
  echo "$INSTALL_OUT" | sed 's/^/  /'
  MODEL_ID=$(echo "$INSTALL_OUT" | python3 -c "
import json, sys
buf = sys.stdin.read()
start = buf.index('{')
end = buf.rindex('}') + 1
print(json.loads(buf[start:end])['model_id'])
")
  echo "  installed model: $MODEL_ID"
  {
    echo "SCRATCH_OPENFGA_STORE_ID=$STORE_ID"
    echo "SCRATCH_OPENFGA_MODEL_ID=$MODEL_ID"
  } > "$OUT_DIR/store.env"
fi
# shellcheck disable=SC1091
set -a; source "$OUT_DIR/store.env"; set +a
echo "  $OUT_DIR/store.env: SCRATCH_OPENFGA_STORE_ID=$SCRATCH_OPENFGA_STORE_ID SCRATCH_OPENFGA_MODEL_ID=$SCRATCH_OPENFGA_MODEL_ID"

# ---------------------------------------------------------------------------
log "[3/9] stock init on the scratch DB, OWNERSHIP_ENABLED=false (content only, no migration yet)"
# ---------------------------------------------------------------------------
export SCRATCH_OWNERSHIP_ENABLED=false
$COMPOSE run --rm init
echo "  init complete -- content loaded, ownership migration deliberately NOT run yet (see header comment)"

# ---------------------------------------------------------------------------
log "[4/9] superset + worker up, OWNERSHIP_ENABLED=false"
# ---------------------------------------------------------------------------
$COMPOSE up -d --force-recreate superset worker
wait_superset_healthy
CID=$($COMPOSE ps -q superset)
echo "  superset is healthy (ownership switch off): $CID"

# ---------------------------------------------------------------------------
log "[5/9] identities + directory + FGA vocabulary extras"
# ---------------------------------------------------------------------------
docker cp overlay/qa/seed_identity.py "$CID:/tmp/seed_identity.py"
docker cp overlay/qa/seed_directory.py "$CID:/tmp/seed_directory.py"
docker cp scratch/seed_fga_extra.py "$CID:/tmp/seed_fga_extra.py"
docker exec "$CID" python /tmp/seed_identity.py
docker exec "$CID" python /tmp/seed_directory.py
docker exec "$CID" python /tmp/seed_fga_extra.py

# ---------------------------------------------------------------------------
log "[6/9] real row-level tenancy: shared datasets, exclusive datasets, Neurons-shaped dataset + dashboards"
# ---------------------------------------------------------------------------
docker cp scratch/seed_tenant_data.py "$CID:/tmp/seed_tenant_data.py"
docker exec "$CID" python /tmp/seed_tenant_data.py

# ---------------------------------------------------------------------------
log "[7/9] dataset-access-aware ownership attribution of the example dashboards/charts"
# ---------------------------------------------------------------------------
docker cp scratch/assign_examples.py "$CID:/tmp/assign_examples.py"
docker exec "$CID" python /tmp/assign_examples.py

# ---------------------------------------------------------------------------
log "[8/9] flip OWNERSHIP_ENABLED=true, restart: migrate -> backfill -> backfill-tenants -> check runs ONCE, over attributed data"
# ---------------------------------------------------------------------------
export SCRATCH_OWNERSHIP_ENABLED=true
$COMPOSE up -d --force-recreate superset worker
wait_superset_healthy
CID=$($COMPOSE ps -q superset)
echo "  --- entrypoint log (migration/backfill/check) ---"
$COMPOSE logs --no-color superset 2>/dev/null | grep -A200 'superset ownership db upgrade' | tail -200

echo
echo "  --- superset ownership status ---"
docker exec "$CID" superset ownership status

echo
echo "  --- superset ownership plugin verify --tenant $TENANT_A ---"
docker exec "$CID" superset ownership plugin verify --tenant "$TENANT_A" --sample-users 10 || true

echo
echo "  --- proof queries ---"
scratch/proof-queries.sh

echo
echo "scratch stack is up: http://localhost:8097  (superset_scratch DB, store $SCRATCH_OPENFGA_STORE_ID)"
echo "logins: admin/admin, Ada $ADA, Cleo $CLEO -- password test1234 for every seeded tenant user."

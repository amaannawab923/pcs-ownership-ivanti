#!/usr/bin/env bash
# Demo-only: creates a scratch OpenFGA store, installs the ownership model
# (pinned), and writes a tiny tenant vocabulary -- two tenants, four users,
# two groups -- through raw HTTP writes (the shape any real directory sync
# must produce; see overlay/pythonpath/ivanti_pcs_example/README.md §2).
#
# Runs as the compose `seed` one-off job (profile "seed"):
#   docker compose -p pcssetup -f docker-compose.pcs-setup.yml run --rm seed
#
# Writes the resolved store/model id to /seed-out/seed.env (a bind-mounted
# host path, ./.seed-out/seed.env, gitignored) so setup.sh's --up flow can
# export them and restart superset+worker pointed at the real store --
# `superset ownership fga install-model` prints the model id but nothing in
# this stack persists it into the running services' environment on its own.
set -euo pipefail

API="${OPENFGA_API_URL:-http://openfga:8080}"
OUT_DIR="/seed-out"
mkdir -p "$OUT_DIR"

echo "==> waiting for $API"
for i in $(seq 1 30); do
  curl -sf "$API/healthz" >/dev/null 2>&1 && break
  sleep 2
done

echo "==> creating demo OpenFGA store"
STORE_ID=$(curl -sf -X POST "$API/stores" -H 'Content-Type: application/json' \
  -d '{"name":"pcs-setup-demo"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "    store: $STORE_ID"
# The demo config refuses to load with OWNERSHIP_ENABLED=true and no store
# id (it must never fall through to a shared default); the CLI below loads
# that config, so export the id of the store just created before calling it.
export OPENFGA_STORE_ID="$STORE_ID"

# Stock schema first: this job is the first thing to load the app on a
# fresh database, and Flask-AppBuilder's own bootstrap (roles, subject sync)
# logs UndefinedTable errors if Superset's migrations have not run yet.
# Idempotent; the init job's own `superset db upgrade` then finds nothing to do.
echo "==> stock schema: superset db upgrade (idempotent)"
superset db upgrade >/dev/null

echo "==> installing the ownership model (pinned)"
MODEL_ID=$(superset ownership fga install-model --api-url "$API" --store "$STORE_ID" --pin \
  | tee /dev/stderr | grep -oE '[0-9A-HJKMNP-TV-Z]{26}' | tail -1)
echo "    model: $MODEL_ID"

write_tuples() {
  # $1 = JSON array of {user,relation,object}
  curl -sf -X POST "$API/stores/$STORE_ID/write" \
    -H 'Content-Type: application/json' \
    -d "{\"writes\":{\"tuple_keys\":$1}}" >/dev/null
}

echo "==> writing tenant vocabulary: 2 tenants, 4 users, 2 groups"
# Ivanti's authorization facts are keyed on GUID v4 for BOTH tenant and
# member (identity.py's GUID_RE; `plugin verify`'s administrator_group_exists
# and group_ids_parse checks enforce this) -- readable slugs like "tenant-a"
# or "alice" fail those checks. Fixed, documented UUIDs so a re-run of this
# script (or a human reading the output) can always identify who's who.
TENANT_A="0a92c264-2c1d-4b1c-b7e1-072d99726c64"   # tenant A
TENANT_B="ef538b7e-b953-458f-a4c0-cfba19450433"   # tenant B
ALICE="67bbd96d-955d-445e-9a17-c48724d268e3"      # tenant A administrator
BOB="6e550f85-8a38-411a-9d68-99a4bc7de046"        # tenant A editor
CAROL="a02d52f3-88da-452f-b80d-58c44d63fada"      # tenant B administrator
DAVE="0fbce894-82d8-4f3f-9711-3c1bcee07214"       # tenant B editor
GROUP_EDITORS_A="editors_${TENANT_A}"             # OWNERSHIP_GROUP_ID_FORMAT={name}_{tenant}
GROUP_EDITORS_B="editors_${TENANT_B}"
GROUP_ADMIN_A="tenant_administrator_${TENANT_A}"
GROUP_ADMIN_B="tenant_administrator_${TENANT_B}"

write_tuples "[
  {\"user\":\"user:${ALICE}\",\"relation\":\"member\",\"object\":\"tenant:${TENANT_A}\"},
  {\"user\":\"user:${BOB}\",\"relation\":\"member\",\"object\":\"tenant:${TENANT_A}\"},
  {\"user\":\"user:${CAROL}\",\"relation\":\"member\",\"object\":\"tenant:${TENANT_B}\"},
  {\"user\":\"user:${DAVE}\",\"relation\":\"member\",\"object\":\"tenant:${TENANT_B}\"},

  {\"user\":\"user:${ALICE}\",\"relation\":\"member\",\"object\":\"group:${GROUP_ADMIN_A}\"},
  {\"user\":\"user:${CAROL}\",\"relation\":\"member\",\"object\":\"group:${GROUP_ADMIN_B}\"},
  {\"user\":\"tenant:${TENANT_A}#member\",\"relation\":\"tenant\",\"object\":\"group:${GROUP_ADMIN_A}\"},
  {\"user\":\"tenant:${TENANT_B}#member\",\"relation\":\"tenant\",\"object\":\"group:${GROUP_ADMIN_B}\"},

  {\"user\":\"user:${BOB}\",\"relation\":\"member\",\"object\":\"group:${GROUP_EDITORS_A}\"},
  {\"user\":\"user:${DAVE}\",\"relation\":\"member\",\"object\":\"group:${GROUP_EDITORS_B}\"},
  {\"user\":\"tenant:${TENANT_A}#member\",\"relation\":\"tenant\",\"object\":\"group:${GROUP_EDITORS_A}\"},
  {\"user\":\"tenant:${TENANT_B}#member\",\"relation\":\"tenant\",\"object\":\"group:${GROUP_EDITORS_B}\"}
]"
echo "    tenant A ($TENANT_A): alice=$ALICE (admin), bob=$BOB ($GROUP_EDITORS_A)"
echo "    tenant B ($TENANT_B): carol=$CAROL (admin), dave=$DAVE ($GROUP_EDITORS_B)"

{
  echo "OPENFGA_STORE_ID=$STORE_ID"
  echo "OPENFGA_MODEL_ID=$MODEL_ID"
  echo "DEMO_TENANT_A=$TENANT_A"
  echo "DEMO_TENANT_B=$TENANT_B"
  echo "DEMO_ALICE=$ALICE"
  echo "DEMO_BOB=$BOB"
  echo "DEMO_CAROL=$CAROL"
  echo "DEMO_DAVE=$DAVE"
} > "$OUT_DIR/seed.env"

echo "==> wrote $OUT_DIR/seed.env:"
cat "$OUT_DIR/seed.env"

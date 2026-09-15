#!/usr/bin/env bash
# ONE command: stock PCS source -> overlay applied -> pcs-ownership image,
# verified, optionally running.
#
#   ./setup.sh --pcs-ref v6.1.0.7 --up
#       (us: build the stock image ourselves, then the overlay, bring up
#        the demo stack)
#
#   ./setup.sh --pcs-image <your-pcs-image> --skip-sync
#       (the client: you already have your own initialized PCS image; use
#        the overlay exactly as committed on this branch, no re-sync)
#
# Flags:
#   --pcs-ref <ref>     stock PCS ref/tag to build step 1 from (default v6.1.0.7)
#   --main-ref <remote/ref>  client-test ref to sync overlay/ from (default client-test/main)
#   --pcs-image <img>   use this already-built PCS image instead of building
#                        step 1 -- skips fetch-pcs.sh and the stock build
#   --tag <tag>         tag for the final image (default: dev)
#   --skip-sync         use the committed overlay/, MANIFEST, UPSTREAM_SHA
#                        as-is; do not regenerate from --main-ref
#   --up                after building, `docker compose -p pcssetup up -d`
#                        and run the seed job
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PCS_REF="v6.1.0.7"
MAIN_REF="client-test/main"
PCS_IMAGE=""
TAG="dev"
DO_UP=false
SKIP_SYNC=false

while [ $# -gt 0 ]; do
  case "$1" in
    --pcs-ref) PCS_REF="$2"; shift 2 ;;
    --main-ref) MAIN_REF="$2"; shift 2 ;;
    --pcs-image) PCS_IMAGE="$2"; shift 2 ;;
    --tag) TAG="$2"; shift 2 ;;
    --up) DO_UP=true; shift ;;
    --skip-sync) SKIP_SYNC=true; shift ;;
    -h|--help)
      sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "unknown flag: $1 (see --help)"; exit 1 ;;
  esac
done

echo "############################################################"
echo "# [1/7] stock PCS source"
echo "############################################################"
if [ -n "$PCS_IMAGE" ]; then
  echo "--pcs-image given ($PCS_IMAGE) -- still fetching .pcs-src, needed for"
  echo "the frontend rebuild stage (webpack cannot run against an image, only a tree)."
fi
if [ -d .pcs-src ] && [ "$(cat .pcs-src/.pcs-ref 2>/dev/null || true)" = "$PCS_REF" ]; then
  echo "  .pcs-src already at $PCS_REF, skipping fetch (delete .pcs-src to force)"
else
  scripts/fetch-pcs.sh "$PCS_REF"
  echo "$PCS_REF" > .pcs-src/.pcs-ref
fi

echo
echo "############################################################"
echo "# [2/7] sync overlay/ + MANIFEST + UPSTREAM_SHA"
echo "############################################################"
if [ "$SKIP_SYNC" = true ]; then
  echo "  --skip-sync: using the committed overlay/ as-is (this is the client's path)"
else
  REMOTE="${MAIN_REF%%/*}"
  REF="${MAIN_REF#*/}"
  PCS_SOURCE_REMOTE="$REMOTE" scripts/sync-from-main.sh "$REF"
fi

echo
echo "############################################################"
echo "# [3/7] drift guard (overlay vs .pcs-src, before any image is built)"
echo "############################################################"
scripts/apply-overlay.sh --check-only .pcs-src

echo
echo "############################################################"
echo "# [4-5/7] docker build (stage 1 stock lean image unless --pcs-image, stage 2 pcs-ownership:$TAG)"
echo "############################################################"
PCS_REF="$PCS_REF" docker/build.sh .pcs-src "$TAG" "$PCS_IMAGE"

echo
echo "############################################################"
echo "# [6/7] verify-image.sh"
echo "############################################################"
scripts/verify-image.sh "pcs-ownership:${TAG}"

if [ "$DO_UP" = true ]; then
  echo
  echo "############################################################"
  echo "# [7/7] docker compose up + seed"
  echo "############################################################"
  export PCS_OWNERSHIP_TAG="$TAG"
  COMPOSE="docker compose -p pcssetup -f docker-compose.pcs-setup.yml"
  mkdir -p .seed-out
  "$HERE/openfga/up.sh"          # standalone OpenFGA (project pcsfga); idempotent, never deletes
  $COMPOSE up -d postgres redis
  echo "waiting for openfga (host port 8199; the image is distroless -- no in-container healthcheck)..."
  for i in $(seq 1 30); do
    curl -sf http://localhost:8199/healthz >/dev/null 2>&1 && break
    sleep 3
  done

  echo "running seed job (creates the demo store + model + tenant vocabulary)"
  $COMPOSE --profile seed run --rm seed

  # seed.sh wrote .seed-out/seed.env with the resolved OPENFGA_STORE_ID /
  # OPENFGA_MODEL_ID -- export them so every later `docker compose` call in
  # this shell (init, then superset/worker) picks them up via the compose
  # file's ${OPENFGA_STORE_ID:-} substitution, instead of an empty store.
  set -a
  # shellcheck disable=SC1091
  source .seed-out/seed.env
  set +a

  echo "running init job (db upgrade, admin user, ownership migration)"
  $COMPOSE --profile init run --rm init

  $COMPOSE up -d superset worker
  echo "waiting for superset to report healthy..."
  for i in $(seq 1 60); do
    status=$(docker inspect -f '{{.State.Health.Status}}' "$($COMPOSE ps -q superset)" 2>/dev/null || echo "starting")
    [ "$status" = "healthy" ] && break
    sleep 5
  done
fi

echo
echo "############################################################"
echo "# summary"
echo "############################################################"
echo "image:        pcs-ownership:${TAG}"
docker image inspect "pcs-ownership:${TAG}" --format 'size:        {{.Size}}' 2>/dev/null | \
  awk -F': ' '{printf "%s %.0fMB\n", $1, $2/1024/1024}'
echo "overlay buckets (from the last sync):"
grep -E '^# --- ' MANIFEST | sed 's/^# /  /'
echo "ownership chain head (module):"
grep -o "^module superset_ownership/migrations/versions/.*\.py$" MANIFEST | tail -1 | sed 's/^module /  /'
if [ "$DO_UP" = true ]; then
  echo
  echo "stack is up: http://localhost:8097  (see README for demo credentials)"
fi

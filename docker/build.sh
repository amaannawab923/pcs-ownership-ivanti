#!/usr/bin/env bash
# Internal two-stage build helper. Not usually invoked directly -- ./setup.sh
# at the branch root is the one-command entry point; this is what it calls
# for the actual `docker build`s.
#
#   docker/build.sh <pcs-src-dir> <tag> [existing-pcs-image]
#
# Step 1: build the stock lean image from <pcs-src-dir> (skipped if
# [existing-pcs-image] is given -- that is the client's path: they already
# have their own PCS image and never need this repo's stock build at all).
# Step 2: build pcs-ownership:<tag> on top of it, with <pcs-src-dir> passed
# as the "pcs-src" additional build context so this shell's own git history
# never contains a copy of the PCS source tree.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PCS_SRC="${1:?usage: build.sh <pcs-src-dir> <tag> [existing-pcs-image]}"
TAG="${2:?usage: build.sh <pcs-src-dir> <tag> [existing-pcs-image]}"
PCS_IMAGE="${3:-}"
PCS_REF="${PCS_REF:-local}"

export DOCKER_BUILDKIT=1

STEP1_SECONDS=0
if [ -z "$PCS_IMAGE" ]; then
  PCS_IMAGE="pcs-stock:${PCS_REF}"
  echo "==> step 1: stock lean image -> $PCS_IMAGE (from $PCS_SRC)"
  t0=$(date +%s)
  docker build -f "$PCS_SRC/Dockerfile" --target lean -t "$PCS_IMAGE" "$PCS_SRC"
  STEP1_SECONDS=$(( $(date +%s) - t0 ))
  echo "==> step 1 done in ${STEP1_SECONDS}s"
else
  echo "==> step 1 skipped: using provided PCS_IMAGE=$PCS_IMAGE"
fi

echo "==> step 2: pcs-ownership:$TAG (overlay applied on top of $PCS_IMAGE)"
t0=$(date +%s)
docker build \
  -f "$HERE/docker/Dockerfile.pcs-ownership" \
  --build-arg PCS_IMAGE="$PCS_IMAGE" \
  --build-context pcs-src="$PCS_SRC" \
  -t "pcs-ownership:${TAG}" \
  "$HERE"
STEP2_SECONDS=$(( $(date +%s) - t0 ))
echo "==> step 2 done in ${STEP2_SECONDS}s"

STOCK_SIZE="n/a"
if docker image inspect "$PCS_IMAGE" >/dev/null 2>&1; then
  STOCK_SIZE=$(docker image inspect "$PCS_IMAGE" --format '{{.Size}}' | awk '{printf "%.0fMB", $1/1024/1024}')
fi
FINAL_SIZE=$(docker image inspect "pcs-ownership:${TAG}" --format '{{.Size}}' | awk '{printf "%.0fMB", $1/1024/1024}')

echo
echo "==> build.sh summary"
echo "    PCS_IMAGE:          $PCS_IMAGE ($STOCK_SIZE) -- step 1: ${STEP1_SECONDS}s"
echo "    pcs-ownership:$TAG  ($FINAL_SIZE) -- step 2: ${STEP2_SECONDS}s"

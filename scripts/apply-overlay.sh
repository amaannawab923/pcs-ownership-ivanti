#!/usr/bin/env bash
# Applies this shell's overlay onto an extracted stock PCS source tree, for
# the from-source build path (docker/Dockerfile.pcs-ownership's `frontend`
# stage does the equivalent inline; use this script directly when building
# outside that Dockerfile, e.g. inside pcs-ivanti's own FIPS build).
#
#   scripts/apply-overlay.sh [--check-only] <pcs-src-dir>
#
# --check-only runs just the drift guard (step 1) and exits -- no copy. This
# is what setup.sh runs before either docker build stage, against the same
# .pcs-src tree stage 1 builds from, so a drift failure is caught before any
# image is built rather than partway through one.
#
# Deliberately boring: no webpack config, no monkeypatching. Two guards
# before anything is copied:
#   1. drift guard against UPSTREAM_SHA -- HARD FAILS if a file the overlay
#      is about to overwrite (frontend "replace" entries; the backend is a
#      wheel and overwrites nothing under superset/) has moved upstream
#      since this overlay was cut; WARNS ONLY for the two reference-only
#      config/entrypoint sources (they inform the config layer / entrypoint
#      but are never overwritten by this script).
#   2. the pythonpath-shadowing trap (found in the pcs-ivanti spike): never
#      bind-mount or copy a WHOLE pythonpath directory over the target's --
#      copy the individual overlay/pythonpath/* entries so nothing already
#      on the target's PYTHONPATH is hidden.
set -euo pipefail

CHECK_ONLY=false
if [ "${1:-}" = "--check-only" ]; then
  CHECK_ONLY=true
  shift
fi

SRC="${1:?usage: apply-overlay.sh [--check-only] <pcs-src-dir>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFEST="$HERE/UPSTREAM_SHA"

[ -d "$SRC" ] || { echo "ERROR: no such tree: $SRC"; exit 1; }

echo "==> applying PCS-10243 ownership overlay to $SRC"

# --- 1. drift guard ---------------------------------------------------------
# UPSTREAM_SHA lists every M file at the upstream base, repo-root relative.
# The subset this script is about to OVERWRITE (everything under
# superset-frontend/; nothing under superset/ -- the backend is a wheel) is
# hard-checked; the two reference-only config/entrypoint sources are not
# overwritten here, so a mismatch there is a warning, not a blocker.
drift=0
while read -r sha rel; do
  [ -z "${sha:-}" ] && continue
  cur="$SRC/$rel"
  is_replaced=0
  case "$rel" in
    superset-frontend/*) is_replaced=1 ;;
  esac

  if [ ! -f "$cur" ]; then
    if [ "$is_replaced" -eq 1 ]; then
      echo "  MISSING (would-overwrite) upstream file: $rel"; drift=1
    else
      echo "  NOTE: reference file not present in target tree (ok): $rel"
    fi
    continue
  fi
  now=$(shasum -a 256 "$cur" | cut -d' ' -f1)
  if [ "$now" != "$sha" ]; then
    if [ "$is_replaced" -eq 1 ]; then
      echo "  DRIFT (would overwrite changed upstream file): $rel"; drift=1
    else
      echo "  WARN: reference-only file changed upstream since this overlay was cut: $rel"
      echo "        (superset_config_ownership.py / entrypoint-ownership.sh were extracted from"
      echo "         this file's earlier content -- re-diff it before trusting the extraction)"
    fi
  fi
done < "$MANIFEST"

if [ "$drift" -ne 0 ]; then
  echo
  echo "ERROR: upstream moved under a file this overlay REPLACES."
  echo "Re-diff those files against the new PCS release, refresh UPSTREAM_SHA"
  echo "(scripts/sync-from-main.sh), then retry. Applying blindly would drop"
  echo "upstream's changes."
  exit 1
fi
echo "  drift guard: clean (replaced files unchanged upstream)"

if [ "$CHECK_ONLY" = true ]; then
  echo "==> --check-only: drift guard passed, not copying anything"
  exit 0
fi

# --- 2. frontend -------------------------------------------------------------
echo "==> frontend"
cp -R "$HERE/overlay/frontend/." "$SRC/superset-frontend/"
echo "  copied $(find "$HERE/overlay/frontend" -type f | wc -l | tr -d ' ') files"

# --- 3. pythonpath: the config shim (+ the reference package) only ----------
# The module itself is NOT copied: it ships as the superset-ownership wheel
# (packaging/build-wheel.sh), installed into the image's venv by your
# Dockerfile. A copy of it on PYTHONPATH would shadow the wheel -- exactly
# what scripts/verify-image.sh's "no stray superset_ownership" check rejects.
echo "==> pythonpath (config shim + reference package; the module is the wheel)"
mkdir -p "$SRC/docker/pythonpath_dev"
# A module copy left behind by an older overlay would shadow the wheel: remove it.
if [ -e "$SRC/docker/pythonpath_dev/superset_ownership" ]; then
  rm -rf "${SRC:?}/docker/pythonpath_dev/superset_ownership"
  echo "  - removed stale docker/pythonpath_dev/superset_ownership (the module is the wheel)"
fi
for name in superset_config_ownership.py ivanti_pcs_example; do
  entry="$HERE/overlay/pythonpath/$name"
  [ -e "$entry" ] || continue
  rm -rf "${SRC:?}/docker/pythonpath_dev/${name:?}"
  cp -R "$entry" "$SRC/docker/pythonpath_dev/$name"
  echo "  + docker/pythonpath_dev/$name"
done

echo
echo "==> build the wheel next:  packaging/build-wheel.sh --outdir <dir>"
echo "    then in your Dockerfile: uv pip install --python /app/.venv/bin/python --no-deps <dir>/superset_ownership-*.whl"
echo "==> done. Nothing under superset/ was touched; build your frontend normally."
echo "    (docker/Dockerfile.pcs-ownership is the two-step, already-built-stock-image"
echo "    path this shell's own build.sh uses.)"

#!/usr/bin/env bash
# Regenerates overlay/, MANIFEST and UPSTREAM_SHA from a client-test ref,
# by diffing that ref against the upstream base this shell targets
# (preset-pcs v6.1.0.7 == 76151beade) and sorting every changed path into a
# known bucket.
#
#   scripts/sync-from-main.sh [ref]        # ref defaults to "main"
#
# MANIFEST and UPSTREAM_SHA are GENERATED here -- never hand-edit them.
# A changed path that does not fall into a known bucket below FAILS the
# sync loudly, rather than being silently dropped or silently miscounted:
# that is the guard against a future PR quietly growing the overlay's real
# surface without this script (and the README's counts) knowing about it.
#
# To re-sync after a change lands on client-test main (e.g. PR #92,
# feat/ownership-hooks): run this script again with the new ref/sha, review
# the diff summary it prints, commit.
set -euo pipefail

REF="${1:-main}"
REMOTE="${PCS_SOURCE_REMOTE:-client-test}"
UPSTREAM="${PCS_UPSTREAM_REF:-76151beade}"   # preset-pcs v6.1.0.7

cd "$(git rev-parse --show-toplevel)"

git fetch "$REMOTE" "$REF" -q
RESOLVED_REF=$(git rev-parse --short=10 "$REMOTE/$REF")
echo "==> syncing overlay from $REMOTE/$REF ($RESOLVED_REF), diffed against upstream $UPSTREAM"

# --- deliberately excluded: research/process docs and CI, not delivered ----
EXCLUDE_EXACT=(
  ".github/workflows/ownership-tests.yml"
  "demo-ownership.sh"
  "UPDATING.md"
  "docker-compose-light.override.yml"
  "docker/pythonpath_dev/.gitignore"
)
EXCLUDE_PREFIX=(
  "qa/reviews/"
  "qa/design/"
)

# --- the 11 qa operator scripts shipped as tooling (overlay/qa/) ----------
QA_SCRIPTS=(
  "qa/seed_fga.sh" "qa/seed_directory.py" "qa/seed_identity.py"
  "qa/seed_tenant_env.py" "qa/setup_fga.sh" "qa/setup_scenarios.py"
  "qa/pcs617_setup.py" "qa/reset-demo-objects.sh" "qa/access_matrix.sh"
  "qa/parity.sh" "qa/lib.sh"
)

# --- the 2 files that are not copied into overlay/ verbatim but that the
# config layer / entrypoint were extracted from or must track (drift only,
# see UPSTREAM_SHA below and README's "upgrade story") --------------------
CONFIG_SOURCE_FILES=(
  "docker/pythonpath_dev/superset_config_docker_light.py"
  "docker/entrypoints/run-server.sh"
)

is_excluded() {
  local f="$1"
  for e in "${EXCLUDE_EXACT[@]}"; do [ "$f" = "$e" ] && return 0; done
  for p in "${EXCLUDE_PREFIX[@]}"; do [[ "$f" == "$p"* ]] && return 0; done
  return 1
}
in_list() {
  local f="$1"; shift
  for x in "$@"; do [ "$f" = "$x" ] && return 0; done
  return 1
}

DIFF="$(git diff --name-status "$UPSTREAM" "$REMOTE/$REF")"

MODULE=() FRONTEND=() BACKEND=() REFERENCE=() QA=() CONFIGSRC=() UNKNOWN=()

while IFS=$'\t' read -r status path; do
  [ -z "${path:-}" ] && continue
  is_excluded "$path" && continue
  case "$path" in
    docker/pythonpath_dev/superset_ownership/*) MODULE+=("$status:$path") ;;
    qa/ivanti_pcs_example/*) REFERENCE+=("$status:$path") ;;
    superset-frontend/*) FRONTEND+=("$status:$path") ;;
    superset/*)
      BACKEND+=("$status:$path") ;;
    docker/pythonpath_dev/superset_config_docker_light.py|docker/entrypoints/run-server.sh)
      CONFIGSRC+=("$status:$path") ;;
    qa/*)
      if in_list "$path" "${QA_SCRIPTS[@]}"; then
        QA+=("$status:$path")
      else
        UNKNOWN+=("$status:$path")
      fi
      ;;
    *) UNKNOWN+=("$status:$path") ;;
  esac
done <<< "$DIFF"

if [ "${#UNKNOWN[@]}" -gt 0 ]; then
  echo
  echo "ERROR: ${#UNKNOWN[@]} changed path(s) do not fall into a known overlay bucket:"
  printf '  %s\n' "${UNKNOWN[@]}"
  echo
  echo "Add each to an EXCLUDE list (if it is process/research, not delivered)"
  echo "or a bucket (if it is overlay-relevant) in this script. Never hand-edit"
  echo "MANIFEST to paper over an unrecognized path."
  exit 1
fi

# --- the backend ships as a wheel: main may not fork stock backend files ---
# Anything under superset/ that differs from upstream cannot be delivered
# (nothing copies it onto the image any more). Since #105 the three files
# this bucket used to carry are byte-identical to upstream; a non-empty
# bucket here means main regressed on that and the change has to move into
# the package (a runtime seam) before it can ship.
if [ "${#BACKEND[@]}" -gt 0 ]; then
  echo
  echo "ERROR: $REMOTE/$REF forks stock backend file(s) the wheel delivery cannot carry:"
  printf '  %s\n' "${BACKEND[@]}"
  echo "Move the change into superset_ownership (see dashboard_patch.py for the"
  echo "runtime-wrap pattern) or upstream it; the overlay copies nothing under superset/."
  exit 1
fi

# --- materialize buckets ----------------------------------------------------
rm -rf overlay/pythonpath/superset_ownership overlay/pythonpath/ivanti_pcs_example \
       overlay/frontend overlay/backend overlay/qa
mkdir -p overlay/pythonpath overlay/frontend overlay/qa

if [ "${#MODULE[@]}" -gt 0 ]; then
  git archive "$REMOTE/$REF" -- docker/pythonpath_dev/superset_ownership | tar -x -C overlay/pythonpath
  mv overlay/pythonpath/docker/pythonpath_dev/superset_ownership overlay/pythonpath/superset_ownership
  rm -rf overlay/pythonpath/docker
fi

if [ "${#REFERENCE[@]}" -gt 0 ]; then
  git archive "$REMOTE/$REF" -- qa/ivanti_pcs_example | tar -x -C overlay/pythonpath
  mv overlay/pythonpath/qa/ivanti_pcs_example overlay/pythonpath/ivanti_pcs_example
  rm -rf overlay/pythonpath/qa
fi

if [ "${#QA[@]}" -gt 0 ]; then
  git archive "$REMOTE/$REF" -- "${QA_SCRIPTS[@]}" | tar -x -C overlay/qa
  for f in "${QA_SCRIPTS[@]}"; do
    mv "overlay/qa/$f" "overlay/qa/$(basename "$f")"
  done
  rm -rf overlay/qa/qa
fi

for entry in "${FRONTEND[@]}"; do
  path="${entry#*:}"
  rel="${path#superset-frontend/}"
  mkdir -p "overlay/frontend/$(dirname "$rel")"
  git show "$REMOTE/$REF:$path" > "overlay/frontend/$rel"
done

# --- UPSTREAM_SHA: sha256 of every M (modified) file at the upstream base --
# Covers files the overlay overwrites (backend, frontend core) AND the 2
# reference-only config/entrypoint sources -- apply-overlay.sh's drift guard
# hard-fails on the former (they are about to be overwritten) and warns on
# the latter (they inform superset_config_ownership.py / entrypoint-
# ownership.sh but are never copied onto the tree).
{
  for entry in "${MODULE[@]}" "${FRONTEND[@]}" "${BACKEND[@]}" "${REFERENCE[@]}" "${QA[@]}" "${CONFIGSRC[@]}"; do
    status="${entry%%:*}"; path="${entry#*:}"
    [ "$status" = "M" ] || continue
    sha=$(git show "$UPSTREAM:$path" | shasum -a 256 | cut -d' ' -f1)
    echo "$sha  $path"
  done
} > UPSTREAM_SHA

# --- MANIFEST: one line per overlay file, generated ------------------------
{
  echo "# PCS-10243 object ownership overlay -- MANIFEST (generated, do not hand-edit)"
  echo "# generated by scripts/sync-from-main.sh from $REMOTE/$REF ($RESOLVED_REF) vs upstream $UPSTREAM"
  echo "# <kind> <path in this overlay tree>"
  echo "# kind: new = additive, no upstream equivalent"
  echo "#       replace = overwrites a stock PCS file at the same relative path"
  echo "#       module = standalone package (own PYTHONPATH entry, or operator tooling)"
  echo "#       config = the customer-facing config layer"
  echo
  echo "# --- backend core: none (the backend is the superset-ownership wheel; nothing under superset/ is replaced) ---"
  echo
  echo "# --- frontend ($(printf '%s\n' "${FRONTEND[@]}" | wc -l | tr -d ' ') files) ---"
  for entry in "${FRONTEND[@]}"; do
    status="${entry%%:*}"; path="${entry#*:}"; rel="${path#superset-frontend/}"
    kind="replace"; [ "$status" = "A" ] && kind="new"
    echo "$kind $rel"
  done
  echo
  echo "# --- superset_ownership module ($(printf '%s\n' "${MODULE[@]}" | wc -l | tr -d ' ') files) ---"
  for entry in "${MODULE[@]}"; do
    path="${entry#*:}"; rel="${path#docker/pythonpath_dev/}"
    echo "module $rel"
  done
  echo
  echo "# --- ivanti_pcs_example reference plug-in package ($(printf '%s\n' "${REFERENCE[@]}" | wc -l | tr -d ' ') files, NOT wired by default) ---"
  for entry in "${REFERENCE[@]}"; do
    path="${entry#*:}"; rel="${path#qa/}"
    echo "module $rel"
  done
  echo
  echo "# --- qa/ operator tooling ($(printf '%s\n' "${QA[@]}" | wc -l | tr -d ' ') scripts) ---"
  for entry in "${QA[@]}"; do
    path="${entry#*:}"
    echo "module qa/$(basename "$path")"
  done
  echo
  echo "# --- config layer (generated files, not part of the diff above) ---"
  echo "config pythonpath/superset_config_ownership.py"
  echo "config config/superset_config_docker.example.py"
  echo
  echo "# --- reference-only, tracked in UPSTREAM_SHA, never copied onto the tree ---"
  for entry in "${CONFIGSRC[@]}"; do
    path="${entry#*:}"
    echo "# reference $path"
  done
} > MANIFEST

TOTAL=$(( ${#MODULE[@]} + ${#FRONTEND[@]} + ${#BACKEND[@]} + ${#REFERENCE[@]} + ${#QA[@]} ))
echo
echo "==> sync complete."
echo "    module (superset_ownership):     ${#MODULE[@]}"
echo "    frontend:                        ${#FRONTEND[@]}"
echo "    backend core:                    0 (wheel delivery; enforced above)"
echo "    reference (ivanti_pcs_example):  ${#REFERENCE[@]}"
echo "    qa operator tooling:             ${#QA[@]}"
echo "    ---------------------------------------"
echo "    overlay total:                   $TOTAL"
echo "    config/entrypoint (reference-only, not copied): ${#CONFIGSRC[@]}"
echo "    UPSTREAM_SHA entries (all M files): $(grep -vc '^#' UPSTREAM_SHA || true)"

#!/usr/bin/env bash
# Post-build sanity check for a pcs-ownership image: does it actually carry
# the feature, without needing a running compose stack.
#
#   scripts/verify-image.sh <image>
set -euo pipefail

IMAGE="${1:?usage: verify-image.sh <image>}"
FAIL=0

check() {
  local name="$1"; shift
  if "$@" >/tmp/verify-image.out 2>&1; then
    echo "  PASS  $name"
  else
    echo "  FAIL  $name"
    sed 's/^/        /' /tmp/verify-image.out
    FAIL=1
  fi
}

echo "==> verifying $IMAGE"

echo "--- the backend is the wheel, and only the wheel ---"
# importlib.metadata, not pip: the PCS venv ships uv and no pip module.
check "superset-ownership wheel installed in the venv" \
  docker run --rm "$IMAGE" python -c "
import importlib.metadata as m, superset_ownership, sys
v = m.version('superset-ownership')
path = superset_ownership.__file__
assert 'site-packages' in path, f'not a wheel install: {path}'
print(f'superset-ownership {v} at {path}')"

# A stale pythonpath copy would shadow the wheel (PYTHONPATH precedes
# site-packages) and is exactly what a half-migrated image looks like.
check "no stray superset_ownership on /app/pythonpath" \
  docker run --rm "$IMAGE" sh -c \
  '! [ -e /app/pythonpath/superset_ownership ] && echo "clean"'

# Nothing under superset/ is replaced: the three files the pre-wheel overlay
# used to overwrite must be byte-identical to the upstream base.
UPSTREAM="${PCS_UPSTREAM_REF:-76151beade}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
for rel in superset/config.py superset/dashboards/api.py superset/security/manager.py; do
  # Guarded: outside a checkout that has the upstream object (a client
  # running this against a shipped tarball) this becomes a SKIP, never an
  # abort under set -e.
  if want=$(git -C "$REPO_DIR" show "$UPSTREAM:$rel" 2>/dev/null | shasum -a 256 | cut -d" " -f1) \
     && git -C "$REPO_DIR" cat-file -e "$UPSTREAM:$rel" 2>/dev/null; then
    check "$rel matches upstream $UPSTREAM" \
      docker run --rm "$IMAGE" sh -c "
        have=\$(sha256sum /app/$rel | cut -d' ' -f1)
        [ \"\$have\" = \"$want\" ] && echo \"\$have\" || { echo \"image \$have != upstream $want\"; exit 1; }"
  else
    echo "  SKIP  $rel matches upstream (git object $UPSTREAM:$rel not available here)"
  fi
done

# The wrap's pins against the image's own dashboards/api.py, from the file
# (no app is created), the same check the Dockerfile runs at build time.
check "dashboard patch pins match the installed superset" \
  docker run --rm "$IMAGE" python -c "
from superset_ownership.dashboard_patch import assert_stock_file
assert_stock_file(); print('pins ok')"

echo "--- module importable ---"
check "superset_ownership importable" \
  docker run --rm "$IMAGE" python -c "import superset_ownership; print(superset_ownership.__file__)"

check "superset_config_ownership importable" \
  docker run --rm "$IMAGE" python -c "import superset_config_ownership; print(superset_config_ownership.configure)"

echo "--- CLI ---"
# The `ownership` CLI group only exists once a loaded config calls
# superset_config_ownership.configure() with the switch on -- nothing in
# the image itself turns the feature on (that's the whole point: the
# customer's one config file does). Supply the minimal config a bare `docker
# run` needs to prove the CLI group registers, independent of any external
# OpenFGA (authorizer "local", no network dependency) -- the openfga-backed
# path is proven separately, against the real demo stack, in step 7.
VERIFY_CONFIG_DIR=$(mktemp -d)
trap 'rm -rf "$VERIFY_CONFIG_DIR"' EXIT
cat > "$VERIFY_CONFIG_DIR/superset_config_docker.py" <<'EOF'
OWNERSHIP_ENABLED = True
OWNERSHIP_AUTHORIZER = "local"
from superset_config_ownership import configure  # noqa: E402
configure(globals())
EOF

# Superset also refuses to even build the Flask app (so `--help` included)
# with the well-known default SECRET_KEY -- any non-default value satisfies it.
check "'superset ownership --help' works" \
  docker run --rm \
  -e SUPERSET_SECRET_KEY="verify-image-$(date +%s)-not-for-production" \
  -e SUPERSET_CONFIG_PATH=/app/pythonpath/superset_config_docker.py \
  -v "$VERIFY_CONFIG_DIR/superset_config_docker.py:/app/pythonpath/superset_config_docker.py:ro" \
  "$IMAGE" superset ownership --help

echo "--- bundle contains the ownership strings ---"
BUNDLE_GREP=$(docker run --rm "$IMAGE" sh -c \
  'grep -rl "Shared with selected users or groups" /app/superset/static/assets 2>/dev/null | head -1')
if [ -n "$BUNDLE_GREP" ]; then
  echo "  PASS  bundle has 'Shared with selected users or groups' ($BUNDLE_GREP)"
else
  echo "  FAIL  bundle missing 'Shared with selected users or groups'"
  FAIL=1
fi

API_STRING_COUNT=$(docker run --rm "$IMAGE" sh -c \
  'grep -rl "api/v1/ownership" /app/superset/static/assets 2>/dev/null | wc -l' | tr -d ' ')
if [ "${API_STRING_COUNT:-0}" -gt 0 ]; then
  echo "  PASS  bundle references api/v1/ownership ($API_STRING_COUNT file(s))"
else
  echo "  FAIL  bundle does not reference api/v1/ownership"
  FAIL=1
fi

echo
if [ "$FAIL" -eq 0 ]; then
  echo "==> verify-image: ALL CHECKS PASSED"
else
  echo "==> verify-image: FAILURES ABOVE"
fi
exit "$FAIL"

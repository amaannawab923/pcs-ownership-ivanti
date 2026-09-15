#!/usr/bin/env bash
# Builds the superset-ownership wheel from the overlay module.
#
#   packaging/build-wheel.sh [--outdir DIR] [--python PY]
#
# Assembles a throwaway build root ({pyproject.toml, README.md} from
# packaging/superset-ownership/ + overlay/pythonpath/superset_ownership/)
# and runs `python -m build --wheel` in it. The overlay tree is never
# modified, so scripts/sync-from-main.sh can keep regenerating it from
# client-test/main without ever seeing packaging files inside the module.
#
# Needs `build` and `hatchling` importable by PY (default: python3). The
# Dockerfile's wheel-builder stage does the same steps inline.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTDIR="$HERE/dist"
PY="python3"
while [ $# -gt 0 ]; do
  case "$1" in
    --outdir) [ $# -ge 2 ] || { echo "usage: $0 [--outdir DIR] [--python PY]"; exit 2; }; OUTDIR="$2"; shift 2 ;;
    --python) [ $# -ge 2 ] || { echo "usage: $0 [--outdir DIR] [--python PY]"; exit 2; }; PY="$2"; shift 2 ;;
    *) echo "usage: $0 [--outdir DIR] [--python PY]"; exit 2 ;;
  esac
done

SRC="$HERE/overlay/pythonpath/superset_ownership"
META="$HERE/packaging/superset-ownership"
[ -f "$SRC/__init__.py" ] || { echo "ERROR: module not found at $SRC (run scripts/sync-from-main.sh first)"; exit 1; }
"$PY" -c "import build, hatchling" 2>/dev/null || { echo "ERROR: $PY needs 'build' and 'hatchling' (pip install build hatchling)"; exit 1; }

ROOT="$(mktemp -d)"
trap 'rm -rf "$ROOT"' EXIT
cp "$META/pyproject.toml" "$META/README.md" "$ROOT/"
# rsync-free copy that drops caches so they can never end up in the wheel
mkdir -p "$ROOT/superset_ownership"
( cd "$SRC" && find . -type f ! -path '*/__pycache__/*' ! -name '*.pyc' -print0 \
  | while IFS= read -r -d '' f; do mkdir -p "$ROOT/superset_ownership/$(dirname "$f")"; cp "$f" "$ROOT/superset_ownership/$f"; done )

mkdir -p "$OUTDIR"
rm -f "$OUTDIR"/superset_ownership-*.whl   # one wheel per build; no stale siblings
( cd "$ROOT" && "$PY" -m build --wheel --no-isolation --outdir "$OUTDIR" )
WHEEL="$(ls -t "$OUTDIR"/superset_ownership-*.whl | head -1)"
echo "==> built $WHEEL"
# Sanity: the three runtime data files are present, tests are not.
for want in superset_ownership/migrations/alembic.ini superset_ownership/migrations/script.py.mako superset_ownership/model/ownership.fga; do
  "$PY" - "$WHEEL" "$want" <<'PYEOF'
import sys, zipfile
whl, want = sys.argv[1], sys.argv[2]
names = zipfile.ZipFile(whl).namelist()
assert want in names, f"missing from wheel: {want}"
PYEOF
done
"$PY" - "$WHEEL" <<'PYEOF'
import sys, zipfile
names = zipfile.ZipFile(sys.argv[1]).namelist()
bad = [n for n in names if "/tests/" in n or n.endswith("UPDATING.md") or "__pycache__" in n]
assert not bad, f"unexpected files in wheel: {bad[:5]}"
print(f"    {len(names)} files; tests/UPDATING.md/__pycache__ excluded; data files present")
PYEOF

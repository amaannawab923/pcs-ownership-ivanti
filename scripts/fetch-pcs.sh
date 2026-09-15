#!/usr/bin/env bash
# Materializes the stock PCS source tree into ./.pcs-src (gitignored) WITHOUT
# ever committing it to this branch. Three ways to get the same tree, tried
# in this order:
#
#   1. PCS_TARBALL_URL=<url or file://path>   a release tarball (GITHUB_TOKEN
#      is sent as a bearer token if set, for a private release asset)
#   2. PCS_LOCAL_CHECKOUT=<path>              `git archive <ref>` from a clone
#   3. (default) a shallow git clone of the tag from PCS_GIT_URL
#      (https://github.com/preset-io/preset-pcs.git) -- uses whatever git
#      credentials you already have for GitHub (credential helper, SSH via
#      PCS_GIT_URL=git@github.com:preset-io/preset-pcs.git, or `gh auth
#      setup-git`). Nothing to download or clone by hand.
#
#   scripts/fetch-pcs.sh [ref]      # default v6.1.0.7
#
# Either way the result is the same: ./.pcs-src/ holds the stock PCS tree at
# that ref, ready for docker/build.sh's step 1 (the stock lean image) and
# scripts/apply-overlay.sh (the from-source path).
set -euo pipefail
REF="${1:-v6.1.0.7}"
DEST=".pcs-src"
PCS_LOCAL_CHECKOUT="${PCS_LOCAL_CHECKOUT:-}"
PCS_GIT_URL="${PCS_GIT_URL:-https://github.com/preset-io/preset-pcs.git}"

rm -rf "$DEST"
mkdir -p "$DEST"

if [ -n "${PCS_TARBALL_URL:-}" ]; then
  echo "==> fetching $PCS_TARBALL_URL"
  auth=()
  [ -n "${GITHUB_TOKEN:-}" ] && auth=(-H "Authorization: Bearer $GITHUB_TOKEN")
  curl -fsSL "${auth[@]}" "$PCS_TARBALL_URL" -o /tmp/pcs-src.tar.gz
  # GitHub tarballs have a single top-level dir; strip it.
  tar -xzf /tmp/pcs-src.tar.gz -C "$DEST" --strip-components=1
  rm -f /tmp/pcs-src.tar.gz
elif [ -n "$PCS_LOCAL_CHECKOUT" ] && [ -d "$PCS_LOCAL_CHECKOUT" ]; then
  echo "==> archiving $REF from local checkout: $PCS_LOCAL_CHECKOUT"
  git -C "$PCS_LOCAL_CHECKOUT" archive "$REF" | tar -x -C "$DEST"
else
  echo "==> cloning $REF from $PCS_GIT_URL (shallow, with your git credentials)"
  tmp="$(mktemp -d)"
  if ! git clone -q --depth 1 --branch "$REF" "$PCS_GIT_URL" "$tmp/src" 2>"$tmp/err"; then
    cat "$tmp/err" >&2
    echo "ERROR: could not clone $REF from $PCS_GIT_URL." >&2
    echo "  preset-pcs is private: make sure git can authenticate to GitHub as a" >&2
    echo "  user with access (\`gh auth setup-git\`, a credential helper, or SSH via" >&2
    echo "  PCS_GIT_URL=git@github.com:preset-io/preset-pcs.git). Alternatives:" >&2
    echo "  PCS_LOCAL_CHECKOUT=<clone> or PCS_TARBALL_URL=<tarball> (see header)." >&2
    rm -rf "$tmp"; exit 1
  fi
  # the tree only -- no .git, no history, exactly like `git archive`
  ( cd "$tmp/src" && git archive HEAD ) | tar -x -C "$DEST"
  rm -rf "$tmp"
fi
echo "$REF" > "$DEST/.pcs-ref"   # what setup.sh checks to skip a re-fetch
echo "==> done: $DEST ($(du -sh "$DEST" | cut -f1))"

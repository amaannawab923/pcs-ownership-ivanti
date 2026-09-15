#!/usr/bin/env bash
# Entrypoint for the pcs-ownership image. Wraps the stock PCS lean image's
# own entrypoint (/app/docker/entrypoints/run-server.sh) with the ownership
# module's parallel migration chain. Idempotent: every step it runs is
# already idempotent on its own (alembic upgrade to head is a no-op when
# already there; the backfill only touches objects it has not already
# attributed; `superset ownership check` is read-only), so re-running this
# entrypoint (a container restart, a redeployed image) is always safe.
#
# Applying the ownership migration backfills every pre-existing object:
# `superset ownership db upgrade` is immediately followed by the backfill
# (every existing chart/dashboard gets an explicit ownership_object row,
# visibility "public", owner resolved from its own creator) and
# `backfill-tenants` (stamps a tenant on whatever the sweep could not). A
# brownfield instance is never left with ungoverned objects between "the
# migration ran" and "someone remembered to run the backfill" -- there is no
# such gap. Nobody loses access: visibility is always "public" here, so the
# Sharing column reads "Public" for every pre-existing object, exactly as it
# behaved before the feature was enabled.
#
# Dispatch, mirroring the stock image's own convention
# (docker/docker-bootstrap.sh) closely enough to be familiar:
#
#   entrypoint-ownership.sh init     one-off: stock `superset db upgrade` +
#                                    admin user + `superset ownership db
#                                    upgrade` + backfill + `backfill-tenants`
#                                    + `superset ownership check`, then exit
#                                    0. For a compose `init` service /
#                                    Kubernetes Job.
#   entrypoint-ownership.sh worker   exec a Celery worker (+ beat, if
#                                    OWNERSHIP_WORKER_BEAT=true) for the
#                                    ownership outbox drain.
#   entrypoint-ownership.sh app      (default) run the ownership migration
#                                    + backfill + check, THEN exec the stock
#                                    command (gunicorn via run-server.sh, or
#                                    whatever args follow).
#   entrypoint-ownership.sh <cmd...> anything else: run the same pre-flight
#                                    (migration + backfill + check) then
#                                    exec "$@" verbatim -- lets an operator
#                                    run `superset shell`, a one-off CLI
#                                    command, etc. through the same image
#                                    without skipping the migration guard.
set -euo pipefail

STOCK_RUN_SERVER="/app/docker/entrypoints/run-server.sh"

# The stock v6.1.0.7 base image's run-server.sh does not yet export
# SERVER_WORKER_AMOUNT before invoking gunicorn (client-test main fixed this
# --see docker/entrypoints/run-server.sh in UPSTREAM_SHA-- but that fix is
# not part of this overlay's replaced-file set). Without it,
# superset_ownership.service cannot tell how many worker processes it is
# racing and refuses its per-process shared cache under gunicorn. Export it
# here so the module sees the real count regardless of which run-server.sh
# the base image shipped.
export SERVER_WORKER_AMOUNT="${SERVER_WORKER_AMOUNT:-1}"

# stderr, deliberately: stdout must stay clean for whatever command this
# entrypoint execs (verify-image.sh and any operator running `docker run
# <image> <cli-command>` through it both capture stdout).
log() { echo "[entrypoint-ownership] $*" >&2; }

# openfga/openfga's own image is distroless (no shell, no curl) so it can't
# carry a compose HEALTHCHECK; wait for it here instead, with curl (present
# on this image), before anything that needs the store. A missing/unset
# OPENFGA_API_URL (e.g. OWNERSHIP_AUTHORIZER=local) skips this entirely.
wait_for_openfga() {
  [ -n "${OPENFGA_API_URL:-}" ] || return 0
  log "waiting for OpenFGA at $OPENFGA_API_URL"
  for _ in $(seq 1 30); do
    curl -sf "${OPENFGA_API_URL}/healthz" >/dev/null 2>&1 && { log "OpenFGA is up"; return 0; }
    sleep 2
  done
  log "WARNING: OpenFGA did not answer healthz after 60s; continuing anyway" \
      "(the ownership plug-in loader runs DEGRADED-safe for maintenance commands," \
      " strict for web/worker -- see plugins.maintenance_invocation())"
}

ownership_migrate_and_check() {
  if [ "${OWNERSHIP_ENABLED:-false}" != "true" ]; then
    log "OWNERSHIP_ENABLED is not 'true' -- skipping ownership migration/check"
    return 0
  fi
  wait_for_openfga
  log "superset ownership db upgrade (parallel chain, alembic_version_ownership)"
  superset ownership db upgrade

  # The backfill is part of APPLYING the migration, not a separate operator
  # step: a brownfield instance's pre-existing charts/dashboards must never
  # be left ungoverned (no ownership_object row) between "db upgrade ran"
  # and "someone remembered to backfill". Idempotent both ways -- it only
  # creates a row for an object that has none (every already-migrated object
  # is skipped) and it RE-attributes a row that exists but is still unowned
  # (e.g. this ran once before identities/history existed), so it is safe to
  # run on every container start, not just the first. Owner fallback:
  # created_by_fk -> changed_by_fk -> OWNERSHIP_DEFAULT_OWNER (unset here) ->
  # the object's tenant administrator; visibility is always "public", so
  # nobody loses access -- see backfill.py's own docstring and
  # scratch/README.md's proof for what this looks like end to end.
  log "superset ownership backfill (every pre-existing chart/dashboard -> public, owner=creator; idempotent)"
  # backfill.run() (unlike the `superset ownership ...` subcommands) is a
  # bare function, not a Flask CLI command -- it needs an app context pushed
  # around it explicitly (the same pattern overlay/qa/*.py's own __main__
  # blocks use), so this is `python -c` PLUS that wrapping, not a plain
  # `from ...backfill import run; run()` (that form has no app context and
  # fails before it reaches a single database row -- confirmed the hard way
  # building scratch/; see scratch/scratch-run.md).
  set +e
  BACKFILL_OUT=$(python -c "
from superset.app import create_app
app = create_app()
with app.app_context():
    from superset_ownership.backfill import run
    run()
" 2>&1)
  BACKFILL_STATUS=$?
  set -e
  echo "$BACKFILL_OUT"
  if [ "$BACKFILL_STATUS" -ne 0 ]; then
    log "superset ownership backfill FAILED (exit $BACKFILL_STATUS) -- see output above"
    exit "$BACKFILL_STATUS"
  fi
  BACKFILL_SUMMARY=$( (echo "$BACKFILL_OUT" \
    | grep -E '^superset_ownership backfill \[(dashboard|chart)\]:' \
    | sed -e 's/^superset_ownership backfill //' \
    | paste -sd';' -) || true)
  log "backfill summary: ${BACKFILL_SUMMARY:-no dashboards/charts in this database}"

  log "superset ownership backfill-tenants (stamp a tenant on any object the sweep above could not)"
  superset ownership backfill-tenants

  log "superset ownership check"
  superset ownership check
}

case "${1:-app}" in
  init)
    log "stock init: superset db upgrade"
    superset db upgrade
    if [ "${SUPERSET_LOAD_EXAMPLES:-no}" = "yes" ]; then
      log "loading examples (SUPERSET_LOAD_EXAMPLES=yes)"
      superset load_examples
    else
      log "not loading examples (default)"
    fi
    log "creating admin user (idempotent: fab create-admin no-ops if present)"
    superset fab create-admin \
      --username "${ADMIN_USERNAME:-admin}" \
      --firstname "${ADMIN_FIRSTNAME:-Superset}" \
      --lastname "${ADMIN_LASTNAME:-Admin}" \
      --email "${ADMIN_EMAIL:-admin@superset.com}" \
      --password "${ADMIN_PASSWORD:-admin}" \
      || log "  (admin user already exists, or fab reported a benign error -- continuing)"
    superset init
    ownership_migrate_and_check
    log "init complete"
    exit 0
    ;;

  worker)
    ownership_migrate_and_check
    if [ "${OWNERSHIP_WORKER_BEAT:-false}" = "true" ]; then
      log "starting Celery worker + beat (ownership outbox drain)"
      # Beat persists its schedule file in the working directory, which the
      # stock image's non-root user cannot write; a silent gdbm permission
      # error there kills beat and nothing drains the outbox. Keep it under
      # SUPERSET_HOME, the one directory the image guarantees writable.
      exec celery --app=superset.tasks.celery_app:app worker --beat \
        --schedule "${SUPERSET_HOME:-/app/superset_home}/celerybeat-schedule" \
        -O fair -l INFO --concurrency="${CELERYD_CONCURRENCY:-2}"
    else
      log "starting Celery worker"
      exec celery --app=superset.tasks.celery_app:app worker \
        -O fair -l INFO --concurrency="${CELERYD_CONCURRENCY:-2}"
    fi
    ;;

  app)
    ownership_migrate_and_check
    log "exec $STOCK_RUN_SERVER"
    exec "$STOCK_RUN_SERVER"
    ;;

  *)
    ownership_migrate_and_check
    log "exec $*"
    exec "$@"
    ;;
esac

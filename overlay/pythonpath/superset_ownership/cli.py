# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""Operator commands: `superset ownership ...`.

The rollback procedure used to be a Python one-liner executed inside the
application image:

    docker exec ... python -c "from superset_ownership.lifecycle import disable; disable()"

That is not something to hand an SRE. These are the same operations as
Superset's own CLI commands, with `--dry-run` where the operation is
destructive, so the runbook is a documented command rather than an import
path someone has to get right under pressure.

    superset ownership status                     what state is the feature in
    superset ownership check                      report inconsistencies
    superset ownership enable   [--dry-run]       re-arm denial from the rows
    superset ownership disable  [--remove-rows]   strip denial (before flag-off)
    superset ownership reconcile [--write]        realign the store's owner tuples
    superset ownership backfill-tenants           give old objects a tenant
    superset ownership purge-tenant <guid> [--yes] [--force-open]
                                                  offboarding; --force-open also
                                                  strips denial from survivors
    superset ownership teardown                   uninstall completely

Every command prints JSON on stdout, so a runbook step can be asserted on.

This group is registered by the feature-ON mutator and needs the backend.
With OWNERSHIP_ENABLED off the OFF mutator registers a group of the same
name carrying `status` and `check` only (superset_ownership.flags
.install_cli): the switch report, so "backend off, UI on" is reportable and
`check` gates a deployment in either state. Everything else here is absent
with the switch off.
"""

from __future__ import annotations

import json  # noqa: TID251 - keeps this module import-light, like its other superset.* reads
import logging

import click
from flask.cli import with_appcontext

from superset_ownership.cli_fga import fga_group

logger = logging.getLogger(__name__)


def _emit(payload) -> None:
    click.echo(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _tenant_guid_argument(ctx, param, value: str) -> str:
    """Refuse a tenant that is not a GUID v4 before anything runs.

    The same check `lifecycle.purge_tenant` makes (and the REST route
    answers 400 for); here it is a click usage error, so a runbook step
    gets exit 2 and the argument named instead of a traceback.
    """
    from superset_ownership.lifecycle import normalize_tenant_guid

    try:
        return normalize_tenant_guid(value)
    except ValueError as exc:
        raise click.BadParameter(str(exc)) from exc


@click.group()
def ownership() -> None:
    """Object ownership and sharing (PCS-10243)."""


ownership.add_command(fga_group)


@ownership.command()
@with_appcontext
def status() -> None:
    """Is the feature on, which authorizer, and how many objects are governed.

    Reports the switches: `enabled_backend` (OWNERSHIP_ENABLED, whether this
    module is loaded), `enabled_ui` (FEATURE_FLAGS["OBJECT_OWNERSHIP"] as the
    config layers left it) and `enabled_ui_runtime` (the flag as Superset's
    feature-flag manager answers it, hooks applied -- what the UI reads),
    with `ui_flag_hooked` when GET_FEATURE_FLAGS_FUNC / IS_FEATURE_ENABLED_FUNC
    is set. The config derives the flag from the setting; `flags_agree` is
    false when a later config layer or a hook moved it, and `check` fails on
    that. Available in both states of the switch (see the module docstring).

    B2: `status` never touches the connection seam the way the `fga` CLI
    group does -- reading `fga.FGA_API_URL`/`FGA_STORE_ID`/`FGA_MODEL_ID`
    resolves the connection under the hood, so on a registry whose
    connection seam degraded it would otherwise traceback past the DEGRADED
    banner instead of answering JSON at all. Those three fields are read
    inside a `try`/`except PluginError` and replaced by
    `"fga": {"degraded": <reason>}` on failure -- `status` always finishes
    and always emits JSON, whichever state the connection is in.
    """
    from flask import current_app
    from superset_ownership import fga, flags, plugins, service
    from superset_ownership.db import ownership_object, ownership_share
    from superset_ownership.identity import group_id_format

    from superset import db

    rows = db.session.execute(ownership_object.select()).mappings().all()
    by_visibility: dict[str, int] = {}
    for r in rows:
        by_visibility[r["visibility"]] = by_visibility.get(r["visibility"], 0) + 1
    try:
        fga_report: dict = {
            "fga_api_url": fga.FGA_API_URL,
            "fga_store": fga.FGA_STORE_ID,
            "fga_model": fga.FGA_MODEL_ID or "(unpinned: the store's latest)",
        }
    except plugins.PluginError as exc:
        fga_report = {"fga": {"degraded": str(exc)}}
    _emit(
        {
            **flags.report(current_app),
            "backend_loaded": True,
            "plugins": plugins.describe(),
            "group_id_format": group_id_format(),
            "public_scope": service.public_scope(),
            **fga_report,
            "audit_sink": bool(current_app.config.get("OWNERSHIP_AUDIT_SINK")),
            "objects": len(rows),
            "by_visibility": by_visibility,
            "shares": len(
                db.session.execute(ownership_share.select()).mappings().all()
            ),
        }
    )


@ownership.command()
@with_appcontext
def check() -> None:
    """Report objects whose stored visibility and actual enforcement disagree.

    Also lists `group_id_mismatch`: tenanted group subjects in the share
    mirror that do not parse under OWNERSHIP_GROUP_ID_FORMAT, which is what
    a format switch on an instance with existing group shares looks like;
    and, without failing, `group_untenanted`: shares to groups outside the
    tenant convention (no tenant in the id), which the store honours
    regardless of the format.

    Also reports the switches (`enabled_backend`, `enabled_ui`,
    `enabled_ui_runtime`, `ui_flag_hooked`, as `status` does) and fails when
    they disagree -- in the static config or at runtime through a
    feature-flag hook: the backend enforcing with no UI to manage it, or the
    UI offering controls against a backend that is not loaded, are both
    misconfigurations. Also fails, with `runtime_unknown` saying why, when a
    hook is configured and its runtime answer could not be obtained (a
    per-user hook that raises without a request): unknown is not a pass for
    a gate. Available in both states of the switch.

    Exits non-zero when anything is wrong, so it can gate a deployment step.
    """
    from superset_ownership import flags
    from superset_ownership.lifecycle import check_consistency

    report = flags.gate(check_consistency())
    _emit({**report, "backend_loaded": True})
    if not report.get("ok"):
        raise SystemExit(1)


@ownership.command()
@click.option("--dry-run", is_flag=True, help="Report what would be armed, write nothing.")
@with_appcontext
def enable(dry_run: bool) -> None:
    """Re-arm denial for every non-public object. Run AFTER setting the flag true."""
    from superset_ownership.lifecycle import enable as do_enable

    _emit(do_enable(dry_run=dry_run))


@ownership.command()
@click.option("--remove-rows", is_flag=True, help="Also delete the ownership and share rows.")
@click.confirmation_option(prompt="Strip denial from every object in this instance?")
@with_appcontext
def disable(remove_rows: bool) -> None:
    """Strip denial. Run BEFORE setting the flag false, or objects brick."""
    from superset_ownership.lifecycle import disable as do_disable

    _emit(do_disable(remove_rows=remove_rows))


@ownership.command()
@click.option("--write", is_flag=True, help="Apply the changes (default reports only).")
@with_appcontext
def reconcile(write: bool) -> None:
    """Realign the authorization store's owner tuples with the ownership rows."""
    from superset_ownership.lifecycle import reconcile as do_reconcile

    _emit(do_reconcile(dry_run=not write))


@ownership.command("backfill-tenants")
@with_appcontext
def backfill_tenants() -> None:
    """Give pre-existing objects the tenant tuple new objects get at creation."""
    from superset_ownership.lifecycle import backfill_object_tenants

    _emit(backfill_object_tenants())


@ownership.command("purge-tenant")
@click.argument("tenant_guid", callback=_tenant_guid_argument)
@click.option("--yes", is_flag=True, help="Actually delete. Without it, this is a dry run.")
@click.option(
    "--force-open",
    is_flag=True,
    help="Also strip denial from objects that still exist (they become dataset-gated).",
)
@with_appcontext
def purge_tenant(tenant_guid: str, yes: bool, force_open: bool) -> None:
    """Remove a tenant's ownership rows, sentinels and authorization facts.

    Defaults to a dry run. Objects that still exist in Superset are REPORTED
    under `retained` and left untouched -- neither opened up nor stranded.
    Pass --force-open to strip their denial deliberately.
    """
    from superset_ownership.lifecycle import purge_tenant as do_purge

    if not yes:
        click.echo("# dry run -- pass --yes to delete", err=True)
    _emit(do_purge(tenant_guid, dry_run=not yes, force_open=force_open))


@ownership.command()
@click.confirmation_option(
    prompt="Irreversibly remove all ownership data, both tables and the sentinel role?"
)
@with_appcontext
def teardown() -> None:
    """Uninstall: strip denial, drop the tables, remove the sentinel role."""
    from superset_ownership.lifecycle import teardown as do_teardown

    _emit(do_teardown(confirm=True))


@ownership.group("db")
def db_group() -> None:
    """The module's own Alembic chain, independent of `superset db`.

    Run `superset ownership db upgrade` right after `superset db upgrade` on
    every deploy. Tracked in `alembic_version_ownership`; never touches
    Superset's `alembic_version`.
    """


@db_group.command("upgrade")
@click.argument("revision", default="head")
@with_appcontext
def db_upgrade(revision: str) -> None:
    """Apply the ownership chain up to REVISION (default: head). Idempotent."""
    from superset_ownership import migrate

    migrate.upgrade(revision)
    _emit({"chain": "ownership", "upgraded_to": revision, "current": migrate.current()})


@db_group.command("downgrade")
@click.argument("revision")
@click.confirmation_option(prompt="Downgrade the ownership schema?")
@with_appcontext
def db_downgrade(revision: str) -> None:
    """Revert the ownership chain to REVISION ('base' drops the tables)."""
    from superset_ownership import migrate

    migrate.downgrade(revision)
    _emit({"chain": "ownership", "downgraded_to": revision, "current": migrate.current()})


@db_group.command("current")
@with_appcontext
def db_current() -> None:
    """Which ownership revision this database is at."""
    from superset_ownership import migrate

    _emit({"chain": "ownership", "current": migrate.current(), "heads": migrate.heads()})


@db_group.command("stamp")
@click.argument("revision", default="head")
@with_appcontext
def db_stamp(revision: str) -> None:
    """Record REVISION without running migrations (adopt an existing install)."""
    from superset_ownership import migrate

    migrate.stamp(revision)
    _emit({"chain": "ownership", "stamped": revision, "current": migrate.current()})


@ownership.group("outbox")
def outbox_group() -> None:
    """The transactional outbox that syncs sharing changes to OpenFGA.

    In production the Celery beat task `superset_ownership.outbox.drain`
    delivers rows automatically. These commands are for operators and tests.
    """


@outbox_group.command("status")
@click.option("--verbose", "-v", is_flag=True, help="Also list every dead row with its error.")
@with_appcontext
def outbox_status(verbose: bool) -> None:
    """Counts by status, rows stuck behind dead objects, and the oldest problem.

    A non-zero `dead` or `blocked`, or an `oldest_pending` that keeps ageing,
    means the store is not receiving changes: look at --verbose, fix the
    cause, then `superset ownership outbox replay`.
    """
    from superset_ownership import outbox

    payload = outbox.status()
    # The one place the probe belongs: an operator looking at a backlog
    # wants to know whether the store is answering at all right now.
    payload["store_reachable"] = outbox.get_authorizer().reachable()
    if verbose:
        payload["dead_rows"] = outbox.dead_rows()
    _emit(payload)


@outbox_group.command("drain")
@click.option("--limit", default=100, show_default=True, help="Max rows per pass.")
@with_appcontext
def outbox_drain(limit: int) -> None:
    """Deliver pending rows to the authorization store now (one pass)."""
    from superset_ownership import outbox

    _emit(outbox.drain(limit=limit))


@outbox_group.command("replay")
@click.argument("row_ids", nargs=-1, type=int)
@with_appcontext
def outbox_replay(row_ids: tuple[int, ...]) -> None:
    """Return dead rows to pending (all of them, or the given ROW_IDS)."""
    from superset_ownership import outbox

    _emit({"replayed": outbox.replay_dead(list(row_ids) or None)})


@outbox_group.command("prune")
@click.option("--older-than-days", default=30, show_default=True, type=int,
              help="Delete `done` rows settled more than this many days ago.")
@with_appcontext
def outbox_prune(older_than_days: int) -> None:
    """Housekeeping: delete delivered rows. Pending, claimed and dead rows are
    never touched."""
    from datetime import timedelta

    from superset_ownership import outbox

    _emit({"pruned": outbox.prune_done(timedelta(days=older_than_days))})


def install(app) -> None:
    """Register the command group on the Flask app."""
    from superset_ownership.cli_plugin import plugin as plugin_group

    ownership.add_command(plugin_group)
    app.cli.add_command(ownership)
    logger.info("superset_ownership: CLI registered (superset ownership --help)")

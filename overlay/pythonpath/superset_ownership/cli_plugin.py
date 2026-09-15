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
"""`superset ownership plugin ...` -- the spec §10 conformance kit CLI.

    superset ownership plugin verify [--tenant GUID] [--json] [--force-walk]
                                      [--sample-users N] [--object REF]
                                      [--as USER] [--latency-ms N]
        Run the 30-check kit (plugin_verify.py) against the CONFIGURED
        plug-ins, in the current application context -- the spec §10 table's
        22 checks, the 7-check `hooks` section (spec §4.5), and one more
        check added for issue #83 (`vocabulary/manage_permission_template`).
        Exit 0 = conformant, 1 = a check failed, 2 = the plug-ins did not
        load or the store could not be reached (see plugin_verify.EXIT_*).

        There is no `--fixtures-broken` flag: it was threaded from the CLI
        into `Context.broken` but read by no check (a no-op, PR90 review
        M-1). The red path is `plugin seed-scratch --broken VARIANT` against
        a scratch store, then a normal `verify` run against it -- see the
        manual round §(e)2.

    superset ownership plugin seed-scratch --api-url URL --store ID
                                            [--broken VARIANT]
        Write the verify fixtures (qa/seed_fga.sh's shapes plus one
        `group tenant tenant#member` tuple per group) into a scratch store,
        optionally mutated to fail exactly one invariant.

    superset ownership plugin describe
        `plugins.describe()`: which class each seam resolved to, its
        protocol version, its config source, and any degraded seam.

Like the rest of this package's CLI (cli.py), business logic is imported
inside each command body, never at module scope -- this file is imported
once, at `superset ownership` startup, before an application (or the
plug-in registry) necessarily exists.
"""

from __future__ import annotations

import logging

import click
from flask.cli import with_appcontext

logger = logging.getLogger(__name__)


def _emit(payload) -> None:
    # Mirrors cli._emit exactly (same flags); duplicated rather than
    # imported so this module never depends on import order against cli.py.
    from superset.utils import json

    click.echo(json.dumps(payload, indent=2, sort_keys=True, default=str))


@click.group("plugin")
def plugin() -> None:
    """Conformance checks for the configured authorizer/directory/identity."""


@plugin.command("verify")
@click.option(
    "--tenant", default=None, help="Tenant GUID for the vocabulary/directory sections."
)
@click.option(
    "--sample-users",
    default=5,
    show_default=True,
    type=int,
    help="Sample size for identity/user_in_group checks.",
)
@click.option(
    "--object",
    "object_ref",
    default=None,
    help="Object ref for the invariants section, e.g. chart:56.",
)
@click.option(
    "--as",
    "as_user",
    default=None,
    help="Non-owner username/GUID for the invariants section.",
)
@click.option(
    "--latency-ms",
    default=300,
    show_default=True,
    type=int,
    help="p95 latency budget for the directory section.",
)
@click.option(
    "--force-walk",
    is_flag=True,
    help="Bypass the fast-path cache for the vocabulary comparison check.",
)
@click.option(
    "--max-members",
    default=500,
    show_default=True,
    type=int,
    help=(
        "group_ids_parse SKIPs (instead of walking) a tenant with more "
        "members than this -- one store Read per member has no other bound."
    ),
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit the Report as JSON instead of a table.",
)
@with_appcontext
def verify(
    tenant: str | None,
    sample_users: int,
    object_ref: str | None,
    as_user: str | None,
    latency_ms: int,
    force_walk: bool,
    max_members: int,
    as_json: bool,
) -> None:
    """Run the §10 conformance kit against the configured plug-ins."""
    from superset_ownership import plugin_verify

    report = plugin_verify.run(
        tenant=tenant,
        sample_users=sample_users,
        object_ref=object_ref,
        as_user=as_user,
        latency_ms=latency_ms,
        force_walk=force_walk,
        max_members=max_members,
    )
    if as_json:
        _emit(report.to_json())
    else:
        click.echo(report.render_text())
    raise SystemExit(report.exit_code)


@plugin.command("seed-scratch")
@click.option("--api-url", required=True, help="The scratch store's OpenFGA API URL.")
@click.option("--store", required=True, help="The scratch store id.")
@click.option(
    "--broken",
    default=None,
    type=click.Choice(
        ("group_without_tenant_tuple", "cross_tenant_member", "wrong_format_id")
    ),
    help=(
        "Seed a fixture variant that fails exactly one verify check, "
        "instead of the clean set."
    ),
)
def seed_scratch(api_url: str, store: str, broken: str | None) -> None:
    """Write the verify fixtures into a scratch OpenFGA store."""
    from superset_ownership.plugin_verify import fixtures

    try:
        result = fixtures.seed_scratch(api_url=api_url, store=store, broken=broken)
    except (ValueError, RuntimeError) as exc:
        # The refusal (--store is the configured store) and a rejected
        # write (no model installed, or the wrong one) are operator
        # mistakes with a clean, one-line diagnosis -- a Python traceback
        # is the wrong way to report either (PR90 review N-9).
        raise click.ClickException(str(exc)) from exc
    _emit(result)


@plugin.command("describe")
@with_appcontext
def describe() -> None:
    """Which class each seam (authorizer/directory/identity) resolved to."""
    from superset_ownership import plugins

    _emit(plugins.describe())

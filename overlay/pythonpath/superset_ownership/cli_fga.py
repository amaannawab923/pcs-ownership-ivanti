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
"""`superset ownership fga ...` -- the authorization store's connection and
model artefact (spec §9).

    superset ownership fga status                  connection fields, reachable?
    superset ownership fga reconnect                force a fresh connection, probe it
    superset ownership fga install-model [--pin]    write model.MODEL as a new version
    superset ownership fga show-model [--check]     the store's model, as DSL

Registered onto the `ownership` group from `cli.py` with one line
(`ownership.add_command(fga_group)`); everything else about these commands
lives here.

Degraded-connection handling (B2): these four commands ARE the seam that can
fail -- if the connection could not be built (a malformed
`OWNERSHIP_FGA_CREDENTIALS`, an unresolvable `OWNERSHIP_FGA_CONFIG_PROVIDER`,
or `plugins.PluginError` from a registry whose connection seam degraded at
`load()`), that failure is caught here and reported the same way every
time: the DEGRADED banner `load()` already printed to stderr, then one more
line, `ownership: cannot continue -- connection seam degraded: <reason>`,
exit 2. Never a raw traceback, and never a silent rebuild that answers from
whatever the environment happens to default to while ignoring the
configured store (spec §4.2 "Degraded + store-dependent command"; the
hand-off note in `qa/design/directory-hook/03-spec-review.md` naming these
four commands specifically as needing that same sentence `plugin verify`
already had).
"""

from __future__ import annotations

import json  # noqa: TID251 - must stay importable with no Superset (test_model.py)
import logging
import os

import click

from superset_ownership import fga, fga_connection, plugins
from superset_ownership.fga_connection import FgaConnection
from superset_ownership.model import MODEL, model_signature, render_dsl, REQUIRED

logger = logging.getLogger(__name__)


def _emit(payload: object) -> None:
    click.echo(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _connection_payload(conn: FgaConnection) -> dict:
    return {
        "api_url": conn.api_url,
        "store": conn.store_id,
        "model": conn.model_id or "(unpinned: the store's latest)",
        "auth": "bearer" if conn.headers else "none",
    }


def _degraded(exc: Exception) -> None:
    """B2/spec §4.2: the DEGRADED banner itself was already printed to
    stderr by ``plugins.load()`` at boot -- this is the second, command-
    specific line ("no fourth behaviour": the same wording `plugin verify`
    already used) that tells the operator this particular invocation cannot
    proceed, and the process exits 2, never a raw traceback."""
    click.echo(
        f"ownership: cannot continue — connection seam degraded: {exc}", err=True
    )
    raise SystemExit(2) from None


@click.group("fga")
def fga_group() -> None:
    """The authorization store: connection status, reconnect, and the model artefact."""


@fga_group.command("status")
def fga_status() -> None:
    """Connection fields and reachability, without forcing a refresh."""
    try:
        conn = plugins.get_fga_connection()
    except plugins.PluginError as exc:
        _degraded(exc)
        return
    payload = _connection_payload(conn)
    payload["reachable"] = fga.reachable(timeout=conn.timeout_s)
    _emit(payload)


@fga_group.command("reconnect")
def fga_reconnect() -> None:
    """Force a fresh connection (ignores the 401 cooldown) and probe it.

    Exits 1 when the freshly built connection cannot reach the store.
    """
    try:
        conn = fga.refresh_connection()
    except plugins.PluginError as exc:
        _degraded(exc)
        return
    payload = _connection_payload(conn)
    payload["reachable"] = fga.reachable(timeout=conn.timeout_s)
    _emit(payload)
    if not payload["reachable"]:
        raise SystemExit(1)


def _override_connection(
    conn: FgaConnection,
    *,
    api_url: str | None,
    store: str | None,
    credentials_env: str | None,
) -> FgaConnection:
    """`--api-url`/`--store`/`--credentials-env` build a ONE-OFF connection
    for this command only -- never persisted -- so an operator can repair a
    store whose configured connection is what is broken."""
    if not credentials_env:
        # No credential override: carry the ambient connection's resolved
        # headers through unchanged. (The override path only ever sees
        # resolved headers, not the raw OWNERSHIP_FGA_CREDENTIALS shape
        # that produced them, so there is nothing to re-resolve here.)
        return FgaConnection(
            api_url=api_url or conn.api_url,
            store_id=store or conn.store_id,
            model_id=conn.model_id,
            headers=conn.headers,
            timeout_s=conn.timeout_s,
        )
    raw = os.environ.get(credentials_env)
    if not raw:
        click.echo(
            f"ownership: --credentials-env {credentials_env} is not set", err=True
        )
        raise SystemExit(2)
    try:
        credentials = json.loads(raw)
    except ValueError as exc:
        click.echo(
            f"ownership: --credentials-env {credentials_env} is not valid JSON: {exc}",
            err=True,
        )
        raise SystemExit(2) from None
    try:
        build = fga_connection.StaticProvider(
            api_url=api_url or conn.api_url,
            store_id=store or conn.store_id,
            model_id=conn.model_id,
            credentials=credentials,
            timeout_s=conn.timeout_s,
        )
        return build()
    except fga_connection.CredentialError as exc:
        _degraded(exc)
        raise AssertionError("unreachable") from exc  # _degraded always raises


@fga_group.command("install-model")
@click.option(
    "--store",
    "store_override",
    default=None,
    help="Store id to install into (default: the resolved connection's).",
)
@click.option(
    "--pin",
    is_flag=True,
    help="Also print the OWNERSHIP_FGA_MODEL guidance for the written model id.",
)
@click.option(
    "--api-url",
    "api_url_override",
    default=None,
    help="Override the connection's api_url for this command only.",
)
@click.option(
    "--credentials-env",
    "credentials_env_override",
    default=None,
    help="Env var holding a JSON OWNERSHIP_FGA_CREDENTIALS object (this command only).",
)
def fga_install_model(
    store_override: str | None,
    pin: bool,
    api_url_override: str | None,
    credentials_env_override: str | None,
) -> None:
    """Install this module's types into the store's authorization model.

    An empty store gets MODEL (the DSL's JSON source of truth) whole. A
    store that already has a model -- Ivanti publishes theirs, with the
    user, tenant and group types their platform writes -- gets its latest
    model with our dashboard and chart types added or replaced and
    nothing of theirs rewritten (`model.merge_into`); a referenced type
    they lack is added whole, a relation ours needs on one they have
    (`tenant#admin`) is added beside theirs, never written over. The
    `model.missing_relations` guard after the merge is exactly that -- a
    guard against a merge that left a needed relation out, which the
    merge by construction does not.

    S6: a no-op when the store's latest model already carries our types
    as they are -- two consecutive runs must not accumulate a new model
    version each time, or a pinned deployment's pin silently lags the
    version an unpinned reader already sees.
    """
    try:
        conn = plugins.get_fga_connection()
    except plugins.PluginError as exc:
        _degraded(exc)
        return

    if store_override or api_url_override or credentials_env_override:
        conn = _override_connection(
            conn,
            api_url=api_url_override,
            store=store_override,
            credentials_env=credentials_env_override,
        )

    try:
        latest = fga.get_model(connection=conn)
    except fga.StoreRejected:
        latest = None  # empty store: nothing to compare against, install below
    except fga.StoreError as exc:
        click.echo(f"ownership: install-model failed: {exc}", err=True)
        raise SystemExit(1) from None

    to_write = MODEL
    if latest is not None:
        from superset_ownership.model import merge_into, missing_relations

        to_write = merge_into(latest)
        lacking = missing_relations(to_write)
        if lacking:  # cannot happen after a merge; a guard, not a branch
            click.echo(
                f"ownership: install-model refused: merged model lacks {lacking}",
                err=True,
            )
            raise SystemExit(1)
        if model_signature(latest) == model_signature(to_write):
            _emit(
                {"model_id": latest["id"], "store": conn.store_id, "installed": False}
            )
            click.echo("ownership: install-model: already current", err=True)
            return

    try:
        model_id = fga.write_model(to_write, connection=conn)
    except fga.StoreError as exc:
        click.echo(f"ownership: install-model failed: {exc}", err=True)
        raise SystemExit(1) from None

    _emit({"model_id": model_id, "store": conn.store_id, "installed": True})
    if pin:
        click.echo(f'OWNERSHIP_FGA_MODEL = "{model_id}"')
        click.echo(f"env: OWNERSHIP_FGA_MODEL={model_id}")


@fga_group.command("show-model")
@click.option(
    "--check",
    "do_check",
    is_flag=True,
    help="Compare against the required relations, list what is missing.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="With --check, print the check result as JSON on stdout instead of stderr.",
)
def fga_show_model(do_check: bool, as_json: bool) -> None:
    """The store's model (latest, or the pinned id) rendered as DSL.

    N8: the DSL always goes to stdout. Plain ``--check`` prints its
    ``{"pinned": ..., "missing": [...]}`` verdict to STDERR, so a reader
    piping stdout only ever sees the DSL (the two were previously
    interleaved on stdout, which misled a reader diffing the DSL against
    ``model/ownership.fga`` -- the trailing JSON is not part of that file).
    ``--check --json`` prints the verdict to stdout instead, as JSON only
    (no DSL), for a caller that wants to parse it directly.
    """
    try:
        conn = plugins.get_fga_connection()
    except plugins.PluginError as exc:
        _degraded(exc)
        return

    try:
        model_json = fga.get_model(conn.model_id, connection=conn)
    except fga.StoreError as exc:
        click.echo(f"ownership: show-model failed: {exc}", err=True)
        raise SystemExit(1) from None

    if not (do_check and as_json):
        click.echo(render_dsl(model_json))
    if not do_check:
        return

    missing = _missing_relations(model_json)
    payload = {"pinned": conn.model_id is not None, "missing": missing}
    if as_json:
        _emit(payload)
    else:
        click.echo(json.dumps(payload, sort_keys=True, default=str), err=True)
    if missing:
        raise SystemExit(1)


def _missing_relations(model_json: dict) -> list[str]:
    """`type.relation` entries in :data:`superset_ownership.model.REQUIRED`
    that this model does not declare."""
    present: dict[str, set[str]] = {
        type_def["type"]: set((type_def.get("relations") or {}).keys())
        for type_def in model_json.get("type_definitions", [])
    }
    return [
        f"{type_name}.{relation}"
        for type_name, relations in REQUIRED.items()
        for relation in relations
        if relation not in present.get(type_name, set())
    ]

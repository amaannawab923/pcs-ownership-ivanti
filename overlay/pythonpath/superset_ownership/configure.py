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
"""The object-ownership config layer, as an importable function.

`configure(ns)` applies the ownership feature layer to a Superset config
namespace: it resolves THE ONE SWITCH (`OWNERSHIP_ENABLED`, config layer
first, environment second), derives `FEATURE_FLAGS["OBJECT_OWNERSHIP"]` from
it, and installs the blueprint / `FLASK_APP_MUTATOR` wiring for whichever
state the switch is in. It is meant to be the LAST line of a deployment's
own config file:

    from superset_ownership.configure import configure
    configure(globals())

(A pythonpath shim named `superset_config_ownership` re-exports `configure`
under the spelling the delivery shell documents, so either import works.)

This is the "THE ONE SWITCH" block from `superset_config_docker_light.py`,
with no behaviour change: the original file mutates its own `globals()` as it
executes top to bottom; this function performs the same mutations against an
explicit `ns` mapping. Every precedence rule, default and log message matches
the source block -- only the storage target differs (`globals()[...]` /
bare name -> `ns[...]`, `layer=globals()` -> `layer=ns`).

Preconditions on `ns` (true of any Superset config module by the time this
runs): FEATURE_FLAGS is already a dict, or absent (superset.config's default
is used). BLUEPRINTS and FLASK_APP_MUTATOR need not be present -- the stock
defaults apply when absent, exactly as upstream.

Nothing here imports `superset.*` at module import time: every such import
is deferred into the function body, so importing this module from a config
file that `superset/config.py` is still exec'ing cannot form a cycle.
"""

from __future__ import annotations

import logging
import os
from typing import Any, MutableMapping


def configure(ns: MutableMapping[str, Any]) -> None:
    """Apply the object-ownership feature layer to a config namespace.

    Call it once, as the last line of config loading; a second call on the
    same namespace re-derives the same values but chains the first call's
    mutator as the "prior" one, so it is not idempotent by design. Safe to
    call with OWNERSHIP_ENABLED unset anywhere (the environment default,
    "false", applies) -- the feature then stays fully off and this reduces
    to installing the flags-only `superset ownership status|check` report.

    Deployment-specific settings that are not about ownership (Talisman,
    PUBLIC_ROLE_LIKE for the embedded SDK, caches) are deliberately NOT set
    here: they belong to the deployment's own config file.
    """
    # The source block assumed FEATURE_FLAGS was already in scope, brought
    # in by `from superset_config import *` at the top of the file it lived
    # in. A config module reaches this call however it likes -- some chain
    # an existing superset_config.py the same way, some (e.g. a bare
    # SUPERSET_CONFIG_PATH file with nothing above this call, which
    # `superset/config.py` execs into a genuinely EMPTY fresh namespace) do
    # not. Fall back to Superset's own stock default rather than KeyError,
    # so `configure(globals())` is correct as the very first line of a
    # config file, not just the last.
    if "FEATURE_FLAGS" not in ns:
        from superset.config import FEATURE_FLAGS as _DEFAULT_FEATURE_FLAGS

        ns["FEATURE_FLAGS"] = dict(_DEFAULT_FEATURE_FLAGS)

    # --- THE ONE SWITCH, computed once here; the feature block further down
    # documents what it means. Two spellings, one precedence:
    #
    #   * an earlier config layer (superset_config.py, or the operator's
    #     superset_config_docker.py, BEFORE this call) that set
    #     OWNERSHIP_ENABLED as a Python setting WINS;
    #   * the environment variable is the DEFAULT, read only when no layer
    #     set it. A compose file that always injects the variable (default
    #     "false") means the reverse order would make the Python setting
    #     dead in every such deployment.
    #
    # Either spelling is parsed by the same rule as the SUPERSET_FEATURE_*
    # merge below: only the literal "true" (case-insensitive) or the bool
    # True is on; "1", "yes" and the string "false" are all off.
    _inherited_switch = ns.get("OWNERSHIP_ENABLED")
    ns["OWNERSHIP_ENABLED_SOURCE"] = (
        "environment" if _inherited_switch is None else "config"
    )
    _ownership_enabled = (
        os.environ.get("OWNERSHIP_ENABLED", "false")
        if _inherited_switch is None
        else _inherited_switch
    )
    _ownership_enabled = (
        _ownership_enabled is True or str(_ownership_enabled).strip().lower() == "true"
    )
    ns["OWNERSHIP_ENABLED"] = _ownership_enabled

    # What an earlier layer said about the UI flag, if anything, before the
    # derivation below overwrites it. superset_ownership.flags reports this
    # as ignored at boot when it disagrees with the switch.
    ns["OWNERSHIP_INHERITED_UI_FLAG"] = ns["FEATURE_FLAGS"].get("OBJECT_OWNERSHIP")

    # Honor SUPERSET_FEATURE_<NAME> env vars on top of any flags inherited
    # from an earlier layer. Only the literal string "true" (case-
    # insensitive) is treated as enabled -- "1"/"yes"/"on" are not, matching
    # the strict-string convention used elsewhere in Superset's env parsing.
    ns["FEATURE_FLAGS"] = {
        **ns["FEATURE_FLAGS"],
        **{
            name[len("SUPERSET_FEATURE_") :]: value.strip().lower() == "true"
            for name, value in os.environ.items()
            if name.startswith("SUPERSET_FEATURE_")
        },
    }

    # ONE SWITCH for object ownership. FEATURE_FLAGS["OBJECT_OWNERSHIP"] is
    # DERIVED from OWNERSHIP_ENABLED here, after the SUPERSET_FEATURE_* merge
    # above, so SUPERSET_FEATURE_OBJECT_OWNERSHIP in the environment does not
    # set it on its own and an earlier layer's own
    # FEATURE_FLAGS["OBJECT_OWNERSHIP"] does not survive it (both reported as
    # ignored, if they disagreed, by warn_if_flags_disagree at boot).
    ns["FEATURE_FLAGS"] = {
        **ns["FEATURE_FLAGS"],
        "OBJECT_OWNERSHIP": _ownership_enabled,
    }

    # This module is imported normally (not exec'd into a fresh namespace),
    # so BLUEPRINTS / FLASK_APP_MUTATOR are read off `ns` as the caller left
    # them: an earlier layer's values if any, else superset.config's own
    # defaults -- never this module's own globals, which have neither.
    from superset.config import BLUEPRINTS as _DEFAULT_BLUEPRINTS
    from superset.config import FLASK_APP_MUTATOR as _DEFAULT_FLASK_APP_MUTATOR

    _prior_flask_app_mutator = ns.get("FLASK_APP_MUTATOR") or _DEFAULT_FLASK_APP_MUTATOR
    _prior_blueprints = list(ns.get("BLUEPRINTS") or _DEFAULT_BLUEPRINTS)

    def _flask_app_mutator_off(app: Any) -> None:
        # The feature-OFF mutator: the prior mutator, the flag report and
        # the flags-only `superset ownership status|check` -- nothing else.
        # flags.py is the one ownership module a feature-off process loads.
        # The import is GUARDED: a config shipped without the package is
        # stock Superset and must still boot with the switch off -- it gets
        # a reduced inline report and a WARNING instead of a
        # ModuleNotFoundError from create_app. The same guard catches a
        # package that is present but older than this config (no flags.py).
        # Replaced below when the feature is on.
        if _prior_flask_app_mutator:
            _prior_flask_app_mutator(app)
        try:
            from superset_ownership.flags import install_cli, warn_if_flags_disagree
        except ImportError as exc:
            _log = logging.getLogger("superset_ownership.config")
            _log.warning(
                "superset_ownership: package not importable or incomplete (%s); "
                "the flag report and `superset ownership` are unavailable in "
                "this process",
                exc,
            )
            _log.info(
                "superset_ownership: OWNERSHIP_ENABLED=%s "
                "FEATURE_FLAGS['OBJECT_OWNERSHIP']=%s (one switch; the flag is "
                "derived from the setting; package absent, runtime hooks not "
                "checked)",
                app.config.get("OWNERSHIP_ENABLED"),
                (app.config.get("FEATURE_FLAGS") or {}).get("OBJECT_OWNERSHIP"),
            )
            return
        warn_if_flags_disagree(app)
        install_cli(app)

    ns["FLASK_APP_MUTATOR"] = _flask_app_mutator_off

    # Object ownership ships OFF. With the switch unset nothing below runs:
    # no hooks are registered, no blueprint, no tables, no authorizer is
    # ever constructed -- so the instance is indistinguishable from stock.
    #
    # OPERATOR NOTE -- THE FLAG IS NOT SYMMETRIC. Read this before flipping it.
    #
    # Denial is enforced by a sentinel row written into each object's
    # `viewers`, not by this flag and not by the ownership_object table. So:
    #
    #   TURNING IT OFF      run lifecycle.disable() FIRST, then set the flag
    #                       false. The flag alone leaves the sentinels
    #                       behind with no code left to interpret them, and
    #                       every private or shared object becomes
    #                       unreachable by its own owner.
    #
    #   TURNING IT BACK ON  set the flag true, then run lifecycle.enable().
    #                       disable() stripped the sentinels; the rows
    #                       survived. Until enable() replays them, every
    #                       object the API and UI both call "private" is
    #                       readable by anyone holding the underlying
    #                       dataset grant.
    #
    #     python -c "from superset_ownership.lifecycle import disable; print(disable())"
    #     python -c "from superset_ownership.lifecycle import enable;  print(enable())"
    #
    # A consistency check runs at startup and logs at ERROR if it finds
    # recorded-private objects with no sentinel behind them. Set
    # OWNERSHIP_REPAIR_ON_START=true to re-arm automatically instead of only
    # reporting -- off by default, because a silent bulk write on boot is
    # not something an operator should get without asking.
    if not _ownership_enabled:
        return

    # --- Object ownership prototype ---
    from superset_ownership import settings as _ownership_settings

    # Opt-in: a minimal Celery just for the ownership outbox drain. Off by
    # default. Set OWNERSHIP_OUTBOX_CELERY=true and run a `redis` service to
    # get the production delivery path: beat enqueues a drain every few
    # seconds, the worker delivers pending outbox rows to OpenFGA with
    # retries. Inside the ON block on purpose: the task module registers
    # itself on Superset's Celery app only when the feature is on.
    if _ownership_settings.get(
        "OWNERSHIP_OUTBOX_CELERY",
        False,
        _ownership_settings.as_legacy_bool,
        layer=ns,
    ):
        _drain_every = _ownership_settings.get(
            "OWNERSHIP_OUTBOX_DRAIN_SECONDS",
            10.0,
            _ownership_settings.as_float,
            layer=ns,
        )

        class OwnershipOutboxCeleryConfig:
            broker_url = (
                f"redis://{os.environ.get('REDIS_HOST', 'redis')}:"
                f"{os.environ.get('REDIS_PORT', '6379')}/0"
            )
            # No result_backend: the drain task is ignore_result=True.
            imports = ("superset_ownership.outbox",)
            worker_prefetch_multiplier = 1
            task_acks_late = False
            beat_schedule = {
                "superset_ownership.outbox.drain": {
                    "task": "superset_ownership.outbox.drain",
                    "schedule": _drain_every,
                    # A tick that could not run within one interval is
                    # dropped rather than queued: after a worker outage beat
                    # must not replay hundreds of drains. The drain itself
                    # is safe under overlap (atomic claims), so this is
                    # about waste, not safety.
                    "options": {"expires": _drain_every},
                },
            }

        ns["CELERY_CONFIG"] = OwnershipOutboxCeleryConfig

    from superset_ownership.api import ownership_bp
    from superset_ownership.hooks import (
        after_asset_create,
        chart_query_filter,
        dashboard_query_filter,
        enforce_chart_data_access,
        extra_editors,
        owners_resolver,
        raise_for_access_bypass,
    )

    # Authorization backend for object ownership.
    #   "local"   - relationships in Superset's own metadata DB. No external
    #               service. Tenancy comes from Superset roles.
    #   "openfga" - relationships, tenancy and groups in an OpenFGA store.
    # The two answer the same questions through the same interface.
    ns["OWNERSHIP_AUTHORIZER"] = _ownership_settings.get(
        "OWNERSHIP_AUTHORIZER", "local", layer=ns
    )

    # Where the tenant goes in a group id -- one setting, applied
    # everywhere; never changed on an instance with existing group tuples
    # without re-seeding them. POLICY, DELIBERATE: a bad value RAISES here,
    # at import, and so stops every process that loads this config.
    from superset_ownership.identity import (
        GROUP_ID_FORMAT_DEFAULT,
        parse_group_id_format,
    )

    ns["OWNERSHIP_GROUP_ID_FORMAT"] = _ownership_settings.get(
        "OWNERSHIP_GROUP_ID_FORMAT",
        GROUP_ID_FORMAT_DEFAULT,
        _ownership_settings.as_str,
        layer=ns,
    )
    parse_group_id_format(ns["OWNERSHIP_GROUP_ID_FORMAT"])  # raises on a bad value

    # Optional last-resort owner for backfill when an object has no
    # recorded creator or last-editor. LEFT UNSET here on purpose: an
    # unowned object is not a dead end -- a tenant administrator can see it
    # and assign an owner.
    ns["OWNERSHIP_DEFAULT_OWNER"] = _ownership_settings.get(
        "OWNERSHIP_DEFAULT_OWNER", None, _ownership_settings.as_int, layer=ns
    )

    # Optional "manage sharing" permission, separate from ownership. Unset
    # (the default) changes nothing: the owner, a tenant administrator and a
    # Superset admin manage an object and nobody else. Set it to the name of
    # a dedicated Flask-AppBuilder role to let holders of that role manage
    # sharing (not ownership) of objects in their own tenant. See
    # docker/pythonpath_dev/superset_config_docker_light.py on client-test
    # main for the full matrix of what a holder may and may not do -- this
    # extraction changes none of it.
    ns["OWNERSHIP_MANAGE_PERMISSION"] = _ownership_settings.get(
        "OWNERSHIP_MANAGE_PERMISSION", None, _ownership_settings.as_str, layer=ns
    )

    # What "public" means. "tenant" (default): visible to the members of
    # the object's tenant, its owner and admins -- an object outside every
    # tenant only to its owner and to callers outside every tenant. "instance":
    # the pre-0004 behaviour, where public hands the object back to
    # Superset's own rules and a dashboard on a dataset two tenants share is
    # visible to both. Read through service.public_scope(), which also
    # refuses an unrecognised value (logged, treated as "tenant").
    ns["OWNERSHIP_PUBLIC_SCOPE"] = _ownership_settings.get(
        "OWNERSHIP_PUBLIC_SCOPE", "tenant", _ownership_settings.as_str, layer=ns
    )

    # Seconds an ownership row may be served from the shared cache before
    # it is re-read; default 10. 0 turns the shared layer off (the
    # request-local one stays). See parse_lookup_cache_ttl's docstring for
    # the multi-worker safety fallback (SimpleCache under >1 worker turns
    # the shared layer off automatically, with a warning).
    #
    # THIS IS ALSO THE MAXIMUM REVOCATION LAG for a tenant administrator
    # (issue #130): a "yes, administers <tenant>" is shared for the same
    # window and nothing invalidates it when the platform revokes the
    # relation, so a demoted administrator keeps reading that tenant's
    # objects for up to this long. A GRANT is immediate -- a negative
    # answer is never shared. `superset ownership forget-administrator
    # <user-id>` drops one cached yes without waiting.
    from superset_ownership.service import parse_lookup_cache_ttl

    ns["OWNERSHIP_LOOKUP_CACHE_TTL"] = parse_lookup_cache_ttl(
        _ownership_settings.get(
            "OWNERSHIP_LOOKUP_CACHE_TTL", None, _ownership_settings.as_str, layer=ns
        )
    )

    # FEATURE_FLAGS["OBJECT_OWNERSHIP"] is NOT set here: it was derived from
    # OWNERSHIP_ENABLED above, once, for both states of the switch.

    ns["AFTER_ASSET_CREATE"] = after_asset_create
    ns["EXTRA_RAISE_FOR_ACCESS_BYPASS"] = raise_for_access_bypass
    # An `editor` share confers EDIT through Superset's own is_editor.
    # Without this the role was a viewer share with a different label.
    ns["EXTRA_EDITORS_RESOLVER"] = extra_editors
    # Ownership lives in this plugin's table, not the model's `owners`
    # relationship (Slice has none), so a denial would otherwise be unable
    # to name anyone to ask for access.
    ns["EXTRA_OWNERS_RESOLVER"] = owners_resolver
    ns["EXTRA_ACCESS_QUERY_FILTERS"] = {
        "dashboards": dashboard_query_filter,
        "charts": chart_query_filter,
    }

    ns["BLUEPRINTS"] = [*_prior_blueprints, ownership_bp]

    def _flask_app_mutator_on(app: Any) -> None:
        # Replaces the feature-off mutator defined above; chains the same
        # prior mutator and makes the same flag report first.
        if _prior_flask_app_mutator:
            _prior_flask_app_mutator(app)
        from superset_ownership.flags import warn_if_flags_disagree

        warn_if_flags_disagree(app)

        # Resolve the three plug-in seams (authorizer, directory, identity)
        # once, here, and stash the registry on app.extensions["ownership"].
        # Strict everywhere except the maintenance commands named in
        # plugins.maintenance_invocation() (db upgrade/downgrade/current/
        # stamp, status, fga reconnect/status/install-model/show-model,
        # plugin verify/seed-scratch): those run DEGRADED on a plug-in
        # failure instead of being unable to fix the thing that is broken.
        from superset_ownership import plugins

        plugins.load(app, strict=not plugins.maintenance_invocation())

        from superset_ownership.db import create_tables

        with app.app_context():
            from superset import db as _db

            create_tables(_db.engine)
            # Outbox on but nothing draining it = every share queued forever.
            from superset_ownership.outbox import warn_if_no_drainer

            warn_if_no_drainer(app)

        # Flask-WTF's global CSRFProtect applies to every POST/PUT/DELETE by
        # default; our blueprint isn't a flask_appbuilder API so it isn't
        # covered by WTF_CSRF_EXEMPT_LIST's dotted-path resolution. Exempt
        # it directly -- it's already protected by JWT/session auth plus the
        # owner-or-admin check on every mutating route.
        from superset.extensions import csrf

        csrf.exempt(ownership_bp)

        # Make the denial non-forgeable: re-arm the sentinel inside the
        # same transaction, whatever wrote it. See superset_ownership/guard.py.
        from superset_ownership.guard import install as install_sentinel_guard

        install_sentinel_guard()

        # Operator commands, so the runbook is `superset ownership disable`
        # rather than a python -c import path executed inside the image.
        from superset_ownership.cli import install as install_ownership_cli

        install_ownership_cli(app)

        # Chart-level privacy on the DATA path. A dashboard tile's data
        # request is gated by Superset on the DATASOURCE only, so a private
        # chart on a dashboard the viewer can open used to render its data
        # to anyone holding the dataset grant. The tile is deliberately LEFT
        # IN PLACE and shows a private-object placeholder instead.
        app.before_request(enforce_chart_data_access)

        # Dashboard-tile access marker: a tile the viewer cannot access is
        # returned with `has_access: false` and `owners` (who to ask), its
        # title kept. Installed by wrapping one stock method at runtime, so
        # no file under superset/ is edited; the wrap refuses a stock method
        # that no longer matches this package's pins (see dashboard_patch).
        from superset_ownership import dashboard_patch

        dashboard_patch.install()

        # Consistency check. Cheap, read-only, and the only thing standing
        # between a mis-sequenced flag flip and objects that report as
        # private while opening for everybody. See the operator note above.
        _repair = _ownership_settings.get(
            "OWNERSHIP_REPAIR_ON_START",
            False,
            _ownership_settings.as_legacy_bool,
            layer=app.config,
        )
        with app.app_context():
            from superset_ownership.lifecycle import startup_check

            startup_check(repair=_repair)

    ns["FLASK_APP_MUTATOR"] = _flask_app_mutator_on

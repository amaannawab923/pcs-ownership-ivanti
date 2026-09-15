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
"""A real Superset application, on SQLite, with the ownership hooks wired and
a COUNTING authorizer where OpenFGA would be.

This is the seam the authorization-model suite (test_access_model.py,
test_endpoints.py) is driven through. Nothing in the access path is mocked:

  - `superset.app.create_app` builds the application from a generated config
    module that starts from the environment's own Superset config (in the
    container, superset_config_docker_light.py) and wires every ownership
    extension point exactly as that file does -- AFTER_ASSET_CREATE,
    EXTRA_RAISE_FOR_ACCESS_BYPASS, EXTRA_EDITORS_RESOLVER, EXTRA_OWNERS_RESOLVER,
    EXTRA_ACCESS_QUERY_FILTERS, the blueprint, the flush guard and the
    chart-data before_request. `test_deployed_config_wires_the_same_hooks`
    asserts the deployed config and this one name the same functions, and
    that the deployed FLASK_APP_MUTATOR installs the same guard and gate.
  - Superset's own `raise_for_access`, list filters, create commands, REST
    routes, session and flush events run unchanged against a SQLite file.
  - The authorizer is selected the way a deployment selects one --
    `OWNERSHIP_AUTHORIZER = "counting"` resolved by `authz.get_authorizer()`
    -- so every store call the request path makes goes through
    `CountingAuthorizer` and is recorded: EVERY method of the contract, not
    only `check` and `list_objects`, because `object_tenant` is a store round
    trip on the deployed backend too. The counts the suite asserts are
    therefore what the code under test actually did, not what a mock was
    told to answer.
  - The outbox is on and drained explicitly with the same fake, so "delete
    removes tuples" is observed as tuples disappearing from the store.

Why not the module-level Flask app of test_transfer.py: the call-count and
flag-off guarantees are properties of the whole request path (Superset's
security manager, FAB's base filters, the flush guard), and only the real
application exercises all of them in the order a request does.

Every function here imports Superset lazily, so this module can be imported
where Superset is absent (the fixtures skip before calling anything) and by
the flag-off subprocess, which must build the SAME world WITHOUT the
`superset_ownership` package ever being imported.
"""

from __future__ import annotations

# The standard library, not superset.utils.json: this module is imported by
# the pure job, where Superset is absent.
import json  # noqa: TID251
import logging
import os
import shutil
import sys
import textwrap
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from flask import Flask
    from flask.testing import FlaskClient
    from werkzeug.test import TestResponse

ALLOW = "allow"
DENY = "deny"
ERROR = "error"

# The two methods that answer "may this subject reach this object". The
# suite's assertions are on ALL recorded calls (`Decision.names`,
# `CountingAuthorizer.names`); this tuple only names the relation calls for
# the helpers that filter on them.
RELATION_CALLS = ("check", "list_objects")

# Ivanti keys everything on GUID v4; usernames carry the member GUID.
GROUP = "eng"
GROUP_SUBJECT = f"group:{GROUP}#member"

SENTINEL = "role:__ownership_sentinel__"

Call = tuple[Any, ...]
Tuple = tuple[str, str, str]


def guid(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-000000000000"


# ------------------------------------------------------------------------ the fake


class CountingAuthorizer:
    """An in-memory authorization store that counts.

    Answers the whole `authz.Authorizer` contract from a set of tuples, with
    the relation implications of the deployed OpenFGA model (owner implies
    editor implies viewer; a `group:<g>#member` grant reaches every member;
    a `tenant:<guid>#member` tuple on the `tenant` relation records which
    tenant an object belongs to).

    Every method records itself in `calls`, so an assertion on `names` sees
    the directory reads (`object_tenant`, `user_in_group`, ...) as well as
    the relation calls; on the deployed backend each is an OpenFGA round trip.

    Failure modes, both reachable from a test:
      up=False       the contract the real OpenFGA adapter (fga.py) keeps when
                     the store is unreachable: check -> False, list_objects ->
                     [], writes -> False, strict writes raise StoreUnavailable.
      raising=True   a backend that does not absorb failures: every relation
                     call raises. Fail-closed must hold for this shape too.
    """

    name = "counting"

    def __init__(self) -> None:
        self.tuples: set[Tuple] = set()
        # group name -> {"user:<guid>", ...}; tenant administrators are the
        # group `tenant_administrator_<tenant guid>`, as in the deployment.
        self.groups: dict[str, set[str]] = {}
        self.calls: list[Call] = []
        self.up = True
        self.raising = False

    # -- test-side controls --------------------------------------------------

    def reset(self) -> None:
        self.calls.clear()
        self.up = True
        self.raising = False

    def count(self, *names: str) -> int:
        return sum(1 for c in self.calls if c[0] in names)

    @property
    def names(self) -> tuple[str, ...]:
        """The method names of every call made since the last clear."""
        return tuple(c[0] for c in self.calls)

    @property
    def checks(self) -> list[Call]:
        return [c for c in self.calls if c[0] == "check"]

    @property
    def relation_calls(self) -> list[Call]:
        return [c for c in self.calls if c[0] in RELATION_CALLS]

    def tuples_on(self, obj: str) -> set[Tuple]:
        return {t for t in self.tuples if t[2] == obj}

    # -- internals -----------------------------------------------------------

    def _record(self, *call: Any) -> None:
        self.calls.append(call)
        if self.raising:
            raise RuntimeError(f"authorization store failure during {call[0]}")

    def _members(self, group_ref: str) -> set[str]:
        name = group_ref.split(":", 1)[1].split("#", 1)[0]
        return self.groups.get(name, set())

    def _holders(self, relation: str, obj: str) -> set[str]:
        """Every user reference that holds `relation` on `obj`, implications
        and group expansion applied."""
        implied = {
            "viewer": ("viewer", "editor", "owner"),
            "editor": ("editor", "owner"),
        }.get(relation, (relation,))
        out: set[str] = set()
        for user, rel, o in self.tuples:
            if o != obj or rel not in implied:
                continue
            if user.startswith("group:"):
                out |= self._members(user)
            else:
                out.add(user)
        return out

    # -- relations -----------------------------------------------------------

    def check(self, user: str, relation: str, obj: str) -> bool:
        self._record("check", user, relation, obj)
        if not self.up:
            return False
        return user in self._holders(relation, obj)

    def list_objects(
        self, user: str, relation: str, object_type: str, *, strict: bool = False
    ) -> list[str]:
        self._record("list_objects", user, relation, object_type)
        if not self.up:
            if strict:
                from superset_ownership.fga import StoreUnavailable

                raise StoreUnavailable("store unreachable")
            return []
        prefix = f"{object_type}:"
        objects = {o for (_u, _r, o) in self.tuples if o.startswith(prefix)}
        return sorted(o for o in objects if user in self._holders(relation, o))

    def _write(self, key: Tuple, strict: bool, add: bool) -> bool:
        if not self.up:
            if strict:
                from superset_ownership.fga import StoreUnavailable

                raise StoreUnavailable("store unreachable")
            return False
        if add:
            self.tuples.add(key)
        else:
            self.tuples.discard(key)
        return True

    def write_tuple(
        self, user: str, relation: str, obj: str, *, strict: bool = False
    ) -> bool:
        self._record("write_tuple", user, relation, obj)
        return self._write((user, relation, obj), strict, add=True)

    def delete_tuple(
        self, user: str, relation: str, obj: str, *, strict: bool = False
    ) -> bool:
        self._record("delete_tuple", user, relation, obj)
        return self._write((user, relation, obj), strict, add=False)

    def list_relations(self, user: str, obj: str) -> list[str]:
        self._record("list_relations", user, obj)
        return sorted(
            {
                r
                for (u, r, o) in self.tuples
                if u == user and o == obj and r not in ("owner", "tenant")
            }
        )

    def list_grants(self, obj: str) -> list[dict[str, str]]:
        self._record("list_grants", obj)
        return [
            {"user": u, "relation": r}
            for (u, r, o) in sorted(self.tuples)
            if o == obj and r not in ("owner", "tenant")
        ]

    def purge_object(self, obj: str) -> bool:
        self._record("purge_object", obj)
        if not self.up:
            from superset_ownership.fga import StoreUnavailable

            raise StoreUnavailable("store unreachable")
        self.tuples = {t for t in self.tuples if t[2] != obj}
        return True

    def revoke_subject(self, user: str, obj: str) -> bool:
        self._record("revoke_subject", user, obj)
        if not self.up:
            from superset_ownership.fga import StoreUnavailable

            raise StoreUnavailable("store unreachable")
        self.tuples = {
            t
            for t in self.tuples
            if not (t[0] == user and t[2] == obj and t[1] not in ("owner", "tenant"))
        }
        return True

    def reachable(self) -> bool:
        return self.up

    # -- directory -----------------------------------------------------------
    # `user_in_group` is the one directory-shaped method kept on the
    # RELATION seam (D-1): `CountingDirectory.user_in_group` calls this same
    # method rather than duplicating it. Tenant membership, group listing
    # and group existence are answered by `CountingDirectory` instead,
    # sharing this object's `tuples`/`groups`/`calls`.

    def user_in_group(self, user_guid: str, group: str) -> bool:
        self._record("user_in_group", user_guid, group)
        return f"user:{user_guid}" in self._members(group)

    def object_tenant(
        self, asset_type: str, object_uuid: str, *, strict: bool = False
    ) -> Optional[str]:
        self._record("object_tenant", asset_type, object_uuid)
        for user, rel, obj in self.tuples:
            if rel == "tenant" and obj == f"{asset_type}:{object_uuid}":
                return user.split(":", 1)[1].split("#", 1)[0]
        return None

    def tenant_objects(self, tenant_guid: str, asset_type: str) -> list[str]:
        self._record("tenant_objects", tenant_guid, asset_type)
        return []

    def set_object_tenant(
        self,
        asset_type: str,
        object_uuid: str,
        tenant_guid: str,
        *,
        strict: bool = False,
    ) -> bool:
        self._record("set_object_tenant", asset_type, object_uuid, tenant_guid)
        key = (f"tenant:{tenant_guid}#member", "tenant", f"{asset_type}:{object_uuid}")
        return self._write(key, strict, add=True)


class CountingDirectory:
    """The `Directory` half of the counting fake.

    Shares `tuples`, `groups` and, above all, `calls` with a
    `CountingAuthorizer` -- constructed from one (`CountingDirectory(store)`)
    rather than owning its own state, so a suite asserting on
    `harness.authorizer.calls` sees directory reads too (`test_endpoints.py`'s
    invariant counts: owner/public = 0 calls, non-owner = 1 `check`, now
    measured across BOTH seams the request path can reach). `user_in_group`
    is not implemented here: it delegates to the store's own method (D-1),
    which already records itself.
    """

    name = "counting"
    protocol_version = 1

    def __init__(self, store: "CountingAuthorizer") -> None:
        self._store = store

    def search_users(
        self, tenant: str, query: str, *, limit: int = 100, cursor: Optional[str] = None
    ) -> dict[str, Any]:
        self._store._record("search_users", tenant, query)
        q = (query or "").lower()
        members = sorted(
            u.split(":", 1)[1]
            for (u, r, o) in self._store.tuples
            if r == "member" and o == f"tenant:{tenant}" and u.startswith("user:")
        )
        items = [
            {"guid": m, "display_name": m, "email": None, "superset_id": None}
            for m in members
            if not q or q in m.lower()
        ]
        return {"items": items[:limit], "next_cursor": None}

    def user_in_tenant(self, member_guid: str, tenant: str) -> bool:
        self._store._record("user_in_tenant", member_guid, tenant)
        if not self._store.up:
            return False
        return (f"user:{member_guid}", "member", f"tenant:{tenant}") in self._store.tuples

    def list_groups(self, tenant: str, *, cursor: Optional[str] = None) -> dict[str, Any]:
        self._store._record("list_groups", tenant)
        items = [
            {"id": f"group:{name}", "display_name": name, "tenant": tenant, "members": len(members)}
            for name, members in sorted(self._store.groups.items())
        ]
        return {"items": items, "next_cursor": None}

    def group_members(self, group_id: str, *, cursor: Optional[str] = None) -> dict[str, Any]:
        self._store._record("group_members", group_id)
        ref = group_id if group_id.endswith("#member") else f"{group_id}#member"
        items = [
            {"guid": u.split(":", 1)[1], "display_name": u.split(":", 1)[1], "email": None, "superset_id": None}
            for u in sorted(self._store._members(ref))
            if u.startswith("user:")
        ]
        return {"items": items, "next_cursor": None}

    def user_in_group(self, member_guid: str, group_id: str) -> bool:
        return self._store.user_in_group(member_guid, group_id)

    def group_exists(self, group_id: str) -> bool:
        self._store._record("group_exists", group_id)
        name = group_id.split(":", 1)[-1].split("#", 1)[0]
        return name in self._store.groups

    def tenant_administrators(self, tenant: str) -> list[dict[str, Any]]:
        self._store._record("tenant_administrators", tenant)
        from superset_ownership.identity import tenant_administrator_group

        admin_group = tenant_administrator_group(tenant).split(":", 1)[-1]
        members = self._store.groups.get(admin_group, set())
        return [
            {"guid": u.split(":", 1)[1], "display_name": u.split(":", 1)[1], "email": None, "superset_id": None}
            for u in sorted(members)
            if u.startswith("user:")
        ]

    def health(self) -> Any:
        self._store._record("health")
        from superset_ownership.directory import DirectoryHealth

        return DirectoryHealth(ok=self._store.up, detail="counting fake")


# ------------------------------------------------------------------------- the app

# Everything a test build changes about the environment's config, and nothing
# else. `from superset.config import *` is what the deployed app does too.
_BASE_CONFIG = """
# Generated by superset_ownership/tests/superset_harness.py for one test run.
from sqlalchemy.pool import NullPool

from superset.config import *  # noqa: F401,F403 -- the environment's config
from superset.utils.log import AbstractEventLogger

SQLALCHEMY_DATABASE_URI = {uri!r}
SQLALCHEMY_EXAMPLES_URI = SQLALCHEMY_DATABASE_URI
# Superset's own test config pins NullPool for file SQLite: a pooled connection
# handed to another thread is refused by pysqlite.
SQLALCHEMY_ENGINE_OPTIONS = {{"poolclass": NullPool}}
SECRET_KEY = "ownership-test-suite-secret-key-not-for-any-real-deployment"
TESTING = True
WTF_CSRF_ENABLED = False
RATELIMIT_ENABLED = False
TALISMAN_ENABLED = False
# The dataset the charts under test read is a table in the metadata file.
PREVENT_UNSAFE_DB_CONNECTIONS = False
CELERY_CONFIG = None
RESULTS_BACKEND = None
CACHE_CONFIG = {{"CACHE_TYPE": "SimpleCache"}}
DATA_CACHE_CONFIG = CACHE_CONFIG
THUMBNAIL_CACHE_CONFIG = CACHE_CONFIG
FEATURE_FLAGS = {{**FEATURE_FLAGS, "SOFT_DELETE": True}}  # noqa: F405


class _NoEventLogger(AbstractEventLogger):
    # The DB event logger writes from a timer thread and locks the SQLite file.

    def log(self, *args, **kwargs):
        return None


EVENT_LOGGER = _NoEventLogger()
"""

# The ownership wiring, step for step what superset_config_docker_light.py
# does under OWNERSHIP_ENABLED=true. Kept explicit here rather than inherited
# from the environment so the suite asserts the SAME thing wherever it runs;
# test_deployed_config_wires_the_same_hooks checks the two agree, including
# what the deployed FLASK_APP_MUTATOR installs. The one step left out is
# lifecycle.startup_check (a read-only consistency report that needs the
# schema, which does not exist yet when the mutator runs).
_OWNERSHIP_CONFIG = """
OWNERSHIP_ENABLED = True
# Set here as a Python setting, which is what the deployed config records
# when a config layer (rather than the environment) decided the switch.
OWNERSHIP_ENABLED_SOURCE = "config"
OWNERSHIP_AUTHORIZER = "counting"
# OWNERSHIP_DIRECTORY has no default alias for an authorizer outside
# {local, openfga} (plugins._default_directory_alias) -- "counting" needs
# it named explicitly.
OWNERSHIP_DIRECTORY = "counting"
OWNERSHIP_OUTBOX_ENABLED = True
OWNERSHIP_LOOKUP_CACHE_TTL = 10
# Derived, as the deployed config derives it: one switch, never set by hand.
FEATURE_FLAGS = {**FEATURE_FLAGS, "OBJECT_OWNERSHIP": OWNERSHIP_ENABLED}

from superset_ownership.api import ownership_bp  # noqa: E402
from superset_ownership.hooks import (  # noqa: E402
    after_asset_create,
    chart_query_filter,
    dashboard_query_filter,
    enforce_chart_data_access,
    extra_editors,
    owners_resolver,
    raise_for_access_bypass,
)

AFTER_ASSET_CREATE = after_asset_create
EXTRA_RAISE_FOR_ACCESS_BYPASS = raise_for_access_bypass
EXTRA_EDITORS_RESOLVER = extra_editors
EXTRA_OWNERS_RESOLVER = owners_resolver
EXTRA_ACCESS_QUERY_FILTERS = {
    "dashboards": dashboard_query_filter,
    "charts": chart_query_filter,
}
# The environment's config may already carry the blueprint; register it once.
BLUEPRINTS = [  # noqa: F405
    *(b for b in BLUEPRINTS if b is not ownership_bp),  # noqa: F405
    ownership_bp,
]


def FLASK_APP_MUTATOR(app):  # noqa: N802
    from superset_ownership.flags import warn_if_flags_disagree

    warn_if_flags_disagree(app)
    # M-5: load the registry on THIS path too -- the deployed mutator does
    # (superset_config_docker_light.py's ON block calls plugins.load right
    # after warn_if_flags_disagree), so without this the harness proved
    # every seam-behind-get_authorizer()/get_directory() invariant on the
    # no-app fallback cache, never on app.extensions["ownership"], the path
    # production actually runs. OWNERSHIP_DIRECTORY = "counting" is set
    # above, so this resolves cleanly against the aliases conftest.py
    # registers before create_test_app is called.
    from superset_ownership import plugins

    plugins.load(app, strict=True)
    from superset_ownership.db import create_tables

    with app.app_context():
        from superset import db as _db

        create_tables(_db.engine)
    from superset.extensions import csrf

    csrf.exempt(ownership_bp)
    from superset_ownership.guard import install as install_sentinel_guard

    install_sentinel_guard()
    from superset_ownership.cli import install as install_ownership_cli

    install_ownership_cli(app)
    app.before_request(enforce_chart_data_access)
    # The dashboard-tile access marker, as the deployed mutator installs it:
    # a runtime wrap of one stock method, not a core edit.
    from superset_ownership.dashboard_patch import install as install_dashboard_patch

    install_dashboard_patch()
"""


def _hook_reset_lines() -> str:
    """`OWNERSHIP_<hook> = None` for every setting `plugin_hooks.HOOK_SPECS`
    defines, plus `OWNERSHIP_IDENTITY = "default"` -- generated from the
    spec tuple (M-1), never hand-listed, so this can never drift as hooks
    are added, renamed or removed from the registry.

    Appended AFTER `from superset.config import *` (`_BASE_CONFIG`) pulls in
    the environment's own `superset_config`/`superset_config_docker.py`:
    in the acceptance container that module carries the full reference hook
    block (and, once `install()` runs, is `OWNERSHIP_IDENTITY`-set too), and
    the suite must be hermetic regardless -- a hook that talks to a table
    the harness's throwaway SQLite has never heard of is exactly the
    failure mode this closes (the PR body's recipe against that deployment
    config, unpatched: 37 failed / 1335 passed, one `ERROR ownership: hook
    OWNERSHIP_MEMBER_GUID raised OperationalError` line and then every
    share refused for the life of the process -- worth its own line in
    §4.5.1 item 4, not just a harness footnote).
    """
    from superset_ownership.plugin_hooks import HOOK_SPECS

    lines = [f"{spec.setting} = None" for spec in HOOK_SPECS]
    lines.append('OWNERSHIP_IDENTITY = "default"')
    return "\n" + "\n".join(lines) + "\n"


def write_config(directory: str, uri: str, *, ownership: bool) -> str:
    """Write the generated config module into `directory`; return its name."""
    name = "ownership_test_config" if ownership else "ownership_flag_off_config"
    source = _BASE_CONFIG.format(uri=uri)
    if ownership:
        # M-1: neutralise every OWNERSHIP_* hook (and OWNERSHIP_IDENTITY)
        # right after the environment's config is star-imported -- scoped
        # to the ownership-ON config, same as `_OWNERSHIP_CONFIG` itself:
        # a hook setting is meaningless (and the package is not even
        # imported) when the flag is off, and importing
        # `superset_ownership.plugin_hooks` unconditionally here broke BOTH
        # the "no package on this PYTHONPATH" and the "the module is never
        # loaded when the flag is off" flag-off boot tests.
        source += _hook_reset_lines()
        source += _OWNERSHIP_CONFIG
    else:
        # A deployment config on this PYTHONPATH may hardcode
        # `OWNERSHIP_ENABLED = True` as a Python setting (the acceptance
        # container's `superset_config_docker.py` does): `_BASE_CONFIG`'s
        # `from superset.config import *` above inherits it, and that
        # config layer's OWN `if OWNERSHIP_ENABLED:` block -- evaluated at
        # ITS import time, before this file's code runs at all -- already
        # wired the ownership-specific extension points into the names
        # this star-import just brought in. Assigning `OWNERSHIP_ENABLED =
        # False` afterward corrects the VALUE the app reports, but cannot
        # retroactively undo wiring an earlier import already performed;
        # the ownership-specific extension points and blueprint are reset
        # explicitly here instead. `FLASK_APP_MUTATOR` is deliberately left
        # alone: an operator's own config layer (a real `superset_config_
        # docker.py` earlier on `sys.path`, not this one) may set its OWN,
        # unrelated mutator, which the "operator layer" flag-off scenario
        # depends on this file NOT clobbering.
        #
        # R2-N2 proposed dropping this branch as inert, on the theory that
        # the flag-off test fails (at `_assert_nothing_wired`'s `ownership_
        # modules` check) before these resets would matter. Empirically,
        # for THIS repo's `superset_config_docker_light.py` (whose ON block
        # sets `EXTRA_ACCESS_QUERY_FILTERS` to a dict of real filter
        # functions, not None-shaped values), dropping this branch used to
        # not merely change which assertion fails -- the flag-off
        # subprocess's own report never got built at all:
        # `FLAG_OFF_SCRIPT`'s final `json.dumps(report)` raised `TypeError:
        # Object of type function is not JSON serializable` on
        # `extra_access_query_filters`, so the subprocess exited non-zero
        # and `_flag_off_report` failed at `assert proc.returncode == 0`
        # before `_assert_nothing_wired` (or the `OWNERSHIP_ENABLED_SOURCE
        # == "config"` skip above) ever ran. R3-N1 (`qa/reviews/
        # review-hooks-pr92.md`) closed that specific crash by serialising
        # `extra_access_query_filters` by qualified name instead of raw
        # (below), so this branch is no longer load-bearing for THAT
        # failure mode -- but it is kept anyway, unshrunk, because it is
        # still what keeps `extra_access_query_filters`/the four `HOOKS`
        # entries actually `None`/`{}`-shaped for `_assert_nothing_wired`'s
        # own assertions once the report DOES build; dropping it would swap
        # one failure for another rather than for nothing.
        # `OWNERSHIP_ENABLED_SOURCE` is deliberately NOT reset by it (only
        # the deployed config itself sets that name), so the skip above
        # still fires correctly once the report is built.
        source += textwrap.dedent(
            """
            OWNERSHIP_ENABLED = False
            AFTER_ASSET_CREATE = None
            EXTRA_RAISE_FOR_ACCESS_BYPASS = None
            EXTRA_EDITORS_RESOLVER = None
            EXTRA_OWNERS_RESOLVER = None
            EXTRA_ACCESS_QUERY_FILTERS = {}
            BLUEPRINTS = [  # noqa: F405
                b
                for b in BLUEPRINTS  # noqa: F405
                if getattr(b, "name", None) != "superset_ownership"
            ]
            FEATURE_FLAGS = {**FEATURE_FLAGS, "OBJECT_OWNERSHIP": False}  # noqa: F405
            """
        )
    with open(os.path.join(directory, f"{name}.py"), "w", encoding="utf-8") as fh:
        fh.write(textwrap.dedent(source))
    return name


DB_FILENAME = "superset.db"


def sqlite_uri(directory: str) -> str:
    # check_same_thread=false is Superset's own default for a SQLite metadata
    # database; NullPool (above) is the test suite's addition.
    return f"sqlite:///{os.path.join(directory, DB_FILENAME)}?check_same_thread=false"


def create_test_app(directory: str, *, ownership: bool, quiet: bool = True) -> Flask:
    """`superset.app.create_app` from the generated config, with the schema
    built afterwards. Returns the Flask app. `quiet` silences logging for
    the duration of create_app; the flag-off subprocess turns it off to
    capture what the deployed mutator logs at boot.

    The schema cannot exist before the app does (Superset's models bind to the
    app's encrypted-field adapter at import), so FAB registers its views
    against an empty database and logs that its default roles could not be
    written; the ownership tables, by contrast, are created by the mutator's
    own migration. Permissions are then created for the registered views and
    the roles synced, which is what `superset init` does. Every step is
    idempotent, so the same call opens a directory that already holds a
    database (the seeded flag-off run).
    """
    sys.path.insert(0, directory)
    module = write_config(directory, sqlite_uri(directory), ownership=ownership)
    from superset.app import create_app

    previous = logging.root.manager.disable
    if quiet:
        logging.disable(logging.CRITICAL)
    try:
        app = create_app(superset_config_module=module)
    finally:
        logging.disable(previous)

    with app.app_context():
        from flask_appbuilder import Model
        from superset import db, security_manager
        from superset.extensions import appbuilder

        Model.metadata.create_all(db.engine)
        appbuilder.add_permissions(update_perms=True)
        security_manager.sync_role_definitions()
    return app


# ----------------------------------------------------------------------- the world


@dataclass(frozen=True)
class Person:
    id: int
    username: str
    label: str

    @property
    def ref(self) -> str:
        """The authorization-store reference (identity.member_ref)."""
        return f"user:{self.username}"


@dataclass
class World:
    """Users and one dataset. Built once per session; objects are per test.

      admin   Superset Admin (no GUID username: a local account)
      ada     Gamma + dataset grant; owns everything the tests create
      ben     Gamma + dataset grant; the non-owner every share targets
      cy      Gamma, NO dataset grant; a share must never let them in
      dee     Gamma + dataset grant; member of group `eng` in the store only

    None of them carries a tenant role; the tenant scenario adds its own
    people through `Harness.add_person`.
    """

    admin: Person
    ada: Person
    ben: Person
    cy: Person
    dee: Person
    dataset_id: int
    dataset_perm: str
    people: dict[str, Person] = field(default_factory=dict)

    @property
    def everyone(self) -> list[Person]:
        return [self.admin, self.ada, self.ben, self.cy, self.dee]


PASSWORD = "ownership-tests"  # noqa: S105 - test accounts on a throwaway database
DATASET_ROLE = "sales_readers"
DATASET_TABLE = "sales"

# username, label, roles -- the same people whether built or reloaded.
_PEOPLE: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("admin", "Admin", ("Admin",)),
    (guid(1), "Ada", ("Gamma", DATASET_ROLE)),
    (guid(2), "Ben", ("Gamma", DATASET_ROLE)),
    (guid(3), "Cy", ("Gamma",)),
    (guid(4), "Dee", ("Gamma", DATASET_ROLE)),
)


def populate(app: Flask) -> World:
    """Create the users, the dataset (backed by a real table) and the role
    that grants it. Once per database."""
    import sqlalchemy as sa

    with app.app_context():
        from superset import db, security_manager
        from superset.connectors.sqla.models import SqlaTable, TableColumn
        from superset.models.core import Database

        with db.engine.begin() as conn:
            # A fixed table name from this module, not input (S608).
            conn.execute(sa.text("CREATE TABLE sales (region TEXT, amount INTEGER)"))
            conn.execute(sa.text("INSERT INTO sales VALUES ('north', 1), ('south', 2)"))

        database = Database(
            database_name="ownership_tests",
            sqlalchemy_uri=app.config["SQLALCHEMY_DATABASE_URI"],
        )
        db.session.add(database)
        db.session.commit()
        table = SqlaTable(table_name=DATASET_TABLE, database=database)
        table.columns = [
            TableColumn(column_name="region", type="TEXT"),
            TableColumn(column_name="amount", type="INTEGER"),
        ]
        db.session.add(table)
        db.session.commit()

        perm = table.get_perm()
        pvm = security_manager.find_permission_view_menu("datasource_access", perm)
        assert pvm is not None, "dataset_after_insert did not create the permission"
        readers = security_manager.add_role(DATASET_ROLE)
        security_manager.add_permission_role(readers, pvm)
        db.session.commit()

        people = [
            _add_person(username, label, roles) for username, label, roles in _PEOPLE
        ]
        return _world(people, table.id, perm)


def load_world(app: Flask) -> World:
    """The same World, read back from a database `populate` built (the
    seeded flag-off subprocess opens a copy of the session's database)."""
    with app.app_context():
        from superset import db, security_manager
        from superset.connectors.sqla.models import SqlaTable

        people = []
        for username, label, _roles in _PEOPLE:
            user = security_manager.find_user(username=username)
            assert user is not None, f"{username} missing from the seeded database"
            people.append(Person(user.id, user.username, label))
        table = (
            db.session.query(SqlaTable)
            .filter(SqlaTable.table_name == DATASET_TABLE)
            .one()
        )
        return _world(people, table.id, table.get_perm())


def _world(people: list[Person], dataset_id: int, perm: str) -> World:
    admin, ada, ben, cy, dee = people
    world = World(admin, ada, ben, cy, dee, dataset_id, perm)
    world.people = {p.label: p for p in world.everyone}
    return world


def _add_person(username: str, label: str, roles: tuple[str, ...]) -> Person:
    """Inside an app context: a user with these roles (created if missing)."""
    from superset import security_manager

    role_objs = [
        security_manager.find_role(r) or security_manager.add_role(r) for r in roles
    ]
    user = security_manager.add_user(
        username,
        label,
        "Test",
        f"{label.lower()}@ownership.test",
        role_objs,
        password=PASSWORD,
    )
    assert user is not None, f"could not create {username}"
    return Person(user.id, user.username, label)


# --------------------------------------------------------------------- the harness


@dataclass(frozen=True)
class Ref:
    asset_type: str
    id: int
    uuid: str

    @property
    def obj(self) -> str:
        return f"{self.asset_type}:{self.uuid}"


@dataclass(frozen=True)
class Decision:
    verdict: str
    calls: tuple[Call, ...]
    error: Optional[BaseException] = None

    @property
    def names(self) -> tuple[str, ...]:
        """Every authorizer method the decision called, in order."""
        return tuple(c[0] for c in self.calls)

    @property
    def checks(self) -> list[Call]:
        return [c for c in self.calls if c[0] == "check"]


class Harness:
    """Everything a test does to the world, each operation in its own app
    context (a fresh session and a fresh `flask.g`, as a request would have)
    and REST calls with NO test-side context pushed, so a request cannot
    inherit the caller's per-request caches."""

    def __init__(
        self, app: Flask, authorizer: Optional[CountingAuthorizer], world: World
    ) -> None:
        self.app = app
        # The flag-off subprocess has no authorizer at all; give it something
        # with a `calls` list so the object helpers work unchanged.
        self.authorizer = authorizer if authorizer is not None else CountingAuthorizer()
        self.world = world
        self.client: FlaskClient = app.test_client()
        self._tokens: dict[int, dict[str, str]] = {}
        self.audit: list[dict[str, Any]] = []
        app.config["OWNERSHIP_AUDIT_SINK"] = self.audit.append

    # -- contexts ----------------------------------------------------------------

    @contextmanager
    def ctx(self) -> Iterator[None]:
        with self.app.app_context():
            yield

    @contextmanager
    def acting_as(self, person: Person) -> Iterator[None]:
        """An app context with `g.user` set through Superset's own override_user."""
        from superset.utils.core import override_user

        with self.app.app_context():
            with override_user(self._user(person)):
                yield

    def _user(self, person: Person) -> Any:
        from superset import security_manager

        return security_manager.get_user_by_id(person.id)

    def _load(self, ref: Ref) -> Any:
        from superset import db
        from superset.models.dashboard import Dashboard
        from superset.models.slice import Slice

        model = Dashboard if ref.asset_type == "dashboard" else Slice
        return db.session.get(model, ref.id)

    # -- people ------------------------------------------------------------------

    def add_person(self, username: str, label: str, *roles: str) -> Person:
        """Another user, with these roles (a `tenant_<guid>` role is created
        on demand). For scenarios the session world does not carry."""
        with self.ctx():
            return _add_person(username, label, roles)

    def set_active(self, person: Person, active: bool) -> None:
        """Deactivate (or reactivate) an account, as an administrator would."""
        with self.ctx():
            from superset import db

            user = self._user(person)
            user.active = active
            db.session.commit()

    # -- objects -----------------------------------------------------------------

    def create(self, asset_type: str, owner: Person, name: str) -> Ref:
        if asset_type == "chart":
            return self.create_chart(owner, name)
        return self.create_dashboard(owner, title=name)

    def create_chart(self, owner: Person, name: str = "chart") -> Ref:
        """Through the real create command, so AFTER_ASSET_CREATE governs it."""
        from superset.commands.chart.create import CreateChartCommand

        ds = self.world.dataset_id
        with self.acting_as(owner):
            chart = CreateChartCommand(
                {
                    "slice_name": name,
                    "viz_type": "table",
                    "datasource_id": ds,
                    "datasource_type": "table",
                    "params": json.dumps(
                        {
                            "viz_type": "table",
                            "datasource": f"{ds}__table",
                            "query_mode": "raw",
                            "all_columns": ["region", "amount"],
                            "row_limit": 10,
                        }
                    ),
                    "query_context": json.dumps(self.query_context()),
                }
            ).run()
            return Ref("chart", chart.id, str(chart.uuid))

    def create_dashboard(
        self, owner: Person, *charts: Ref, title: str = "dashboard"
    ) -> Ref:
        from superset.commands.dashboard.create import CreateDashboardCommand

        with self.acting_as(owner):
            from superset import db

            dash = CreateDashboardCommand(
                {"dashboard_title": title, "published": True}
            ).run()
            if charts:
                dash.slices = [self._load(c) for c in charts]
                db.session.commit()
            return Ref("dashboard", dash.id, str(dash.uuid))

    def query_context(
        self, slice_id: Optional[int] = None, *, shape: str = "form_data"
    ) -> dict:
        """The query context a chart tile or explore sends to the bulk data
        route, `POST /api/v1/chart/data`. Clients name the saved chart in one
        of two places: `form_data.slice_id` (dashboards, explore) or
        `queries[].form_data.slice_id`; `shape` picks which."""
        ds = self.world.dataset_id
        query: dict[str, Any] = {"columns": ["region", "amount"], "row_limit": 10}
        body: dict[str, Any] = {
            "datasource": {"id": ds, "type": "table"},
            "queries": [query],
            "result_format": "json",
            "result_type": "full",
        }
        if slice_id is not None:
            if shape == "form_data":
                body["form_data"] = {"slice_id": slice_id}
            else:
                query["form_data"] = {"slice_id": slice_id}
        return body

    # -- the access decision -----------------------------------------------------

    def decide(self, person: Person, ref: Ref) -> Decision:
        """Superset's own `raise_for_access` for this user and object, with
        EVERY authorizer call it caused. The access decision the spec's
        call-count criteria are stated about."""
        from superset import security_manager
        from superset.exceptions import SupersetSecurityException

        self.authorizer.calls.clear()
        with self.acting_as(person):
            obj = self._load(ref)
            try:
                security_manager.raise_for_access(**{ref.asset_type: obj})
                verdict, error = ALLOW, None
            except SupersetSecurityException:
                verdict, error = DENY, None
            except Exception as exc:  # noqa: BLE001 - recorded, asserted on by the test
                verdict, error = ERROR, exc
        return Decision(verdict, tuple(self.authorizer.calls), error)

    def visible_ids(self, person: Person, asset_type: str) -> set[int]:
        """What EXTRA_ACCESS_QUERY_FILTERS adds to this user's list query."""
        from superset_ownership import hooks

        fn = (
            hooks.dashboard_query_filter
            if asset_type == "dashboard"
            else hooks.chart_query_filter
        )
        self.authorizer.calls.clear()
        with self.acting_as(person):
            return set(fn(person.id))

    def editor_subject_ids(self, ref: Ref) -> set[int]:
        from superset_ownership import hooks

        with self.ctx():
            return set(hooks.extra_editors(self._load(ref)))

    def subject_id(self, person: Person) -> int:
        from superset.subjects.utils import get_or_create_user_subject

        with self.ctx():
            from superset import db

            subject = get_or_create_user_subject(person.id)
            db.session.commit()
            return subject.id

    # -- state -------------------------------------------------------------------

    def row(self, ref: Ref) -> Any:
        from superset_ownership import service

        with self.ctx():
            return service.lookup(ref.asset_type, ref.id, fresh=True)

    def viewers(self, ref: Ref) -> list[str]:
        """Names of the subjects in the object's own `viewers` collection."""
        with self.ctx():
            return sorted(_subject_name(s) for s in self._load(ref).viewers)

    def editors(self, ref: Ref) -> list[str]:
        with self.ctx():
            return sorted(_subject_name(s) for s in self._load(ref).editors)

    def outbox(self) -> list[dict[str, Any]]:
        import sqlalchemy as sa
        from superset_ownership.db import ownership_outbox

        with self.ctx():
            from superset import db

            return [
                dict(r)
                for r in db.session.execute(
                    sa.select(ownership_outbox).order_by(ownership_outbox.c.id)
                ).mappings()
            ]

    def drain(self, *, external: bool = False) -> dict[str, int]:
        """Run the outbox drain.

        `external=True` (H-1, PR #100 review round 2) simulates the drain
        running in a SEPARATE process from the one serving reads -- the
        Celery worker or the `superset ownership outbox drain` CLI, the
        production shape, never the harness's default (which drains in the
        reader's own process and memory, the one topology production never
        has). On a per-process cache backend (SimpleCache: this stack's
        flask web + Celery drain shape) `outbox._apply`'s post-delivery
        invalidation reaches only the process that runs it; hiding
        `service._shared_cache()` for the duration of the drain call makes
        every `_shared_cache()` lookup INSIDE the drain answer exactly as a
        genuinely separate process's own (different, empty) SimpleCache
        instance would -- unable to see, let alone touch, the entries a
        prior request published to THIS process's shared cache. Those
        entries are the ones a test asserts against afterwards, unaffected
        by anything the external drain did.
        """
        from superset_ownership import outbox

        with self.ctx():
            if not external:
                return outbox.drain()
            from unittest.mock import patch

            from superset_ownership import service

            with patch.object(service, "_shared_cache", return_value=None):
                return outbox.drain()

    def events(self, name: str) -> list[dict[str, Any]]:
        return [e for e in self.audit if e.get("event") == name]

    def snapshot_database(self, directory: str) -> str:
        """Copy the session's SQLite file into `directory` (created), for a
        subprocess to open as a pre-existing database. Returns the directory."""
        from superset import db

        with self.ctx():
            db.session.remove()
            db.engine.dispose()
        uri = self.app.config["SQLALCHEMY_DATABASE_URI"]
        source = uri[len("sqlite:///") :].split("?", 1)[0]
        os.makedirs(directory, exist_ok=True)
        shutil.copyfile(source, os.path.join(directory, DB_FILENAME))
        return directory

    # -- REST --------------------------------------------------------------------

    def headers(self, person: Person) -> dict[str, str]:
        if person.id not in self._tokens:
            r = self.client.post(
                "/api/v1/security/login",
                json={
                    "username": person.username,
                    "password": PASSWORD,
                    "provider": "db",
                },
            )
            assert r.status_code == 200, (r.status_code, r.get_json())
            token = r.get_json()["access_token"]
            self._tokens[person.id] = {"Authorization": f"Bearer {token}"}
        return self._tokens[person.id]

    def request(
        self, person: Person, method: str, path: str, **kwargs: Any
    ) -> TestResponse:
        from flask import has_app_context

        assert not has_app_context(), (
            "REST calls must run outside a test-side app context; otherwise the "
            "request shares flask.g (and the per-request caches) with the test"
        )
        self.authorizer.calls.clear()
        return getattr(self.client, method)(
            path, headers=self.headers(person), **kwargs
        )

    def get(self, person: Person, path: str) -> TestResponse:
        return self.request(person, "get", path)

    def put(self, person: Person, path: str, body: dict[str, Any]) -> TestResponse:
        return self.request(person, "put", path, json=body)

    def post(
        self, person: Person, path: str, body: Optional[dict[str, Any]] = None
    ) -> TestResponse:
        return self.request(person, "post", path, json=body or {})

    def delete(self, person: Person, path: str) -> TestResponse:
        return self.request(person, "delete", path)

    def listed_ids(self, person: Person, asset_type: str) -> set[int]:
        r = self.get(person, f"/api/v1/{asset_type}/?q=(page_size:100)")
        assert r.status_code == 200, (r.status_code, r.get_json())
        return set(r.get_json()["ids"])

    def detail(self, person: Person, ref: Ref) -> TestResponse:
        """Superset's own detail route for the object."""
        return self.get(person, f"/api/v1/{ref.asset_type}/{ref.id}")

    def rename(self, person: Person, ref: Ref, name: str) -> TestResponse:
        """A native PUT of the object's title: Superset's own write path."""
        return self.put(
            person, f"/api/v1/{ref.asset_type}/{ref.id}", {title_field(ref): name}
        )

    # -- the ownership API -------------------------------------------------------

    def share(
        self,
        actor: Person,
        ref: Ref,
        subject: str,
        role: str = "viewer",
        *,
        drain: bool = True,
    ) -> TestResponse:
        r = self.post(
            actor,
            f"/api/v1/ownership/{ref.asset_type}/{ref.id}/shares",
            {"subject": subject, "role": role},
        )
        assert r.status_code == 200, (r.status_code, r.get_json())
        if drain:
            self.drain()
        return r

    def revoke(
        self, actor: Person, ref: Ref, subject: str, *, drain: bool = True
    ) -> TestResponse:
        # `#` must be percent-encoded in the path: unescaped, the test
        # client (like any URL parser) reads everything from it on as a
        # FRAGMENT and never sends it, so a group reference's `#member`
        # suffix -- the very thing that distinguishes the group from a role
        # change -- would silently vanish before the route ever saw it.
        from urllib.parse import quote

        encoded = quote(subject, safe="")
        r = self.delete(
            actor, f"/api/v1/ownership/{ref.asset_type}/{ref.id}/shares/{encoded}"
        )
        assert r.status_code == 200, (r.status_code, r.get_json())
        if drain:
            self.drain()
        return r

    def set_visibility(self, actor: Person, ref: Ref, visibility: str) -> TestResponse:
        r = self.put(
            actor,
            f"/api/v1/ownership/{ref.asset_type}/{ref.id}/visibility",
            {"visibility": visibility},
        )
        assert r.status_code == 200, (r.status_code, r.get_json())
        return r


def title_field(ref: Ref) -> str:
    return "slice_name" if ref.asset_type == "chart" else "dashboard_title"


def _subject_name(subject: Any) -> str:
    if getattr(subject, "role", None) is not None:
        return f"role:{subject.role.name}"
    if getattr(subject, "user", None) is not None:
        return f"user:{subject.user.username}"
    return f"subject:{subject.id}"


# ---------------------------------------------------------------- flag-off world

# Run in a SUBPROCESS with OWNERSHIP_ENABLED=false, so the assertion "the
# module is never imported" is about a fresh interpreter, not about whatever
# the test process already has in sys.modules. Prints one JSON line.
#
# Two ways in, chosen by the environment:
#   fresh    OWNERSHIP_FLAG_OFF_DIR is empty: build the world, create a chart,
#            report stock behaviour and the absence of every extension point.
#   seeded   OWNERSHIP_FLAG_OFF_SEED names a chart id: the directory holds a
#            copy of a database the feature governed (sentinel rows, ownership
#            tables); the world is loaded, not built, and the report carries
#            what stock Superset makes of that pre-existing object.
#
# Either way the directory is first on sys.path, so a `superset_config_docker
# .py` the test writes there is the operator's override layer (the deployed
# superset_config.py star-imports it), and the caller's PYTHONPATH and cwd
# decide whether the package is importable at all -- `package_importable` is
# read before anything could import it. Every `superset_ownership.*` log
# record made during the boot is captured (the harness's logging silence is
# lifted for this process) so the boot-log contract is asserted against the
# deployed file, and the `superset ownership` group, where the mutator
# registered one, is listed and its `status` / `check` invoked.
FLAG_OFF_SCRIPT = r"""
import importlib.util, json, logging, os, sys
sys.path.insert(0, os.environ["OWNERSHIP_HARNESS_DIR"])
import superset_harness as h

package_importable = importlib.util.find_spec("superset_ownership") is not None


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.records = []

    def emit(self, record):
        self.records.append([record.levelname, record.name, record.getMessage()])


capture = _Capture()
_ownership_logger = logging.getLogger("superset_ownership")
_ownership_logger.setLevel(logging.INFO)
_ownership_logger.addHandler(capture)

directory = os.environ["OWNERSHIP_FLAG_OFF_DIR"]
seed = json.loads(os.environ.get("OWNERSHIP_FLAG_OFF_SEED") or "null")
app = h.create_test_app(directory, ownership=False, quiet=False)
world = h.load_world(app) if seed else h.populate(app)
from sqlalchemy import inspect

import superset.config as deployed
from superset import db, security_manager
from superset.exceptions import SupersetSecurityException
from superset.extensions import feature_flag_manager
from superset.models.slice import Slice
from superset.utils.core import override_user

def decide(person, chart_id):
    with app.app_context():
        with override_user(security_manager.get_user_by_id(person.id)):
            try:
                security_manager.raise_for_access(chart=db.session.get(Slice, chart_id))
                return "allow"
            except SupersetSecurityException:
                return "deny"

ref = h.Harness(app, None, world).create_chart(world.ada)
with app.app_context():
    tables = set(inspect(db.engine).get_table_names())
client = app.test_client()


def names(chart_id, collection):
    with app.app_context():
        subjects = getattr(db.session.get(Slice, chart_id), collection)
        return sorted(h._subject_name(s) for s in subjects)


def status(person, path):
    login = {"username": person.username, "password": h.PASSWORD, "provider": "db"}
    token = client.post("/api/v1/security/login", json=login).get_json()["access_token"]
    return client.get(path, headers={"Authorization": f"Bearer {token}"}).status_code


def observe(chart_id):
    route = f"/api/v1/chart/{chart_id}"
    return {
        "viewers": names(chart_id, "viewers"),
        "editors": names(chart_id, "editors"),
        "decisions": {p.label: decide(p, chart_id) for p in world.everyone},
        "get_status": {p.label: status(p, route) for p in world.everyone},
        "data_status": {p.label: status(p, f"{route}/data/") for p in world.everyone},
    }


def qualified(fn):
    return f"{getattr(fn, '__module__', '?')}.{getattr(fn, '__name__', repr(fn))}"


def listeners(name):
    # Session events are registered at class level; a session instance's
    # dispatch lists what would fire for it.
    with app.app_context():
        return [qualified(fn) for fn in getattr(db.session().dispatch, name)]


HOOKS = (
    "EXTRA_RAISE_FOR_ACCESS_BYPASS",
    "EXTRA_EDITORS_RESOLVER",
    "AFTER_ASSET_CREATE",
    "EXTRA_OWNERS_RESOLVER",
)
EVENTS = ("before_flush", "before_commit", "after_commit", "after_soft_rollback")
FLAG_HOOKS = ("GET_FEATURE_FLAGS_FUNC", "IS_FEATURE_ENABLED_FUNC")


def cli(*args):
    # The registered group through Flask's own runner, as `superset ownership
    # ...` would reach it; exit code and the JSON it printed.
    result = app.test_cli_runner().invoke(app.cli, list(args))
    try:
        payload = json.loads(result.output)
    except ValueError:
        payload = result.output
    return [result.exit_code, payload]


ownership_group = app.cli.commands.get("ownership")
with app.app_context():
    # What common_bootstrap_payload() hands the frontend: the hooked value.
    runtime_flag = bool(
        feature_flag_manager.get_feature_flags().get("OBJECT_OWNERSHIP")
    )
# The modules list is read before the CLI is invoked: the OFF-state commands
# load only what the mutator already loaded, but the parity claim is about
# the boot, so the boot is what is measured.
ownership_modules = sorted(m for m in sys.modules if m.startswith("superset_ownership"))
report = {
    "package_importable": package_importable,
    "ownership_modules": ownership_modules,
    "log": capture.records,
    "operator_mutator_ran": app.config.get("OPERATOR_MUTATOR_RAN"),
    "switch_source": app.config.get("OWNERSHIP_ENABLED_SOURCE"),
    "inherited_ui_flag": app.config.get("OWNERSHIP_INHERITED_UI_FLAG"),
    "flag_hooks": {name: bool(app.config.get(name)) for name in FLAG_HOOKS},
    "runtime_bootstrap_flag": runtime_flag,
    "ownership_cli": (
        None if ownership_group is None else sorted(ownership_group.commands)
    ),
    "cli": (
        None
        if ownership_group is None
        else {"status": cli("ownership", "status"), "check": cli("ownership", "check")}
    ),
    "config": {
        k: (None if app.config.get(k) is None else repr(app.config.get(k)))
        for k in HOOKS
    },
    # R3-N1 (`qa/reviews/review-hooks-pr92.md`): serialised by QUALIFIED NAME,
    # like `before_request` below -- these values are real filter functions,
    # not JSON-shaped, so `dict(... or {})` raw here used to make the final
    # `json.dumps(report)` raise `TypeError: Object of type function is not
    # JSON serializable` whenever the deployment wired any (the ON-state
    # report never reached the assertions at all). Reporting what the
    # deployment actually wired, the same way every other function-valued
    # field in this report already does, is also more useful than a raw,
    # unprintable repr would have been.
    "extra_access_query_filters": {
        k: qualified(v)
        for k, v in (app.config.get("EXTRA_ACCESS_QUERY_FILTERS") or {}).items()
    },
    "object_ownership_flag": bool(
        (app.config.get("FEATURE_FLAGS") or {}).get("OBJECT_OWNERSHIP")
    ),
    # The two switches as the deployed config computed them (the module
    # namespace superset.config exec'd it into) and as the app sees them.
    "deployed_switches": {
        "OWNERSHIP_ENABLED": getattr(deployed, "OWNERSHIP_ENABLED", None),
        "OBJECT_OWNERSHIP": (getattr(deployed, "FEATURE_FLAGS", None) or {}).get(
            "OBJECT_OWNERSHIP"
        ),
    },
    "effective_switches": {
        "OWNERSHIP_ENABLED": app.config.get("OWNERSHIP_ENABLED"),
        "OBJECT_OWNERSHIP": (app.config.get("FEATURE_FLAGS") or {}).get(
            "OBJECT_OWNERSHIP"
        ),
    },
    "blueprints": sorted(app.blueprints),
    "before_request": [qualified(fn) for fn in app.before_request_funcs.get(None, [])],
    "session_listeners": {name: listeners(name) for name in EVENTS},
    "ownership_tables": sorted(t for t in tables if t.startswith("ownership_")),
    "fresh": observe(ref.id),
    "seeded": observe(seed["chart_id"]) if seed else None,
}
print("OWNERSHIP_FLAG_OFF_REPORT " + json.dumps(report))
"""

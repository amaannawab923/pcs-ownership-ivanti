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
"""
Shared CRUD/query logic for the ownership_object / ownership_share tables.
Used by both the extension hooks (hooks.py) and the REST API (api.py) so
there's exactly one place that knows the table shapes.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import asdict, dataclass
from numbers import Integral
from typing import TYPE_CHECKING, Any, Iterable, Optional, Protocol, runtime_checkable

from sqlalchemy import select, insert, update, delete, text

from superset_ownership import fga, outbox
from superset_ownership.authz import get_authorizer
from superset_ownership.db import ownership_object, ownership_share

if TYPE_CHECKING:
    from flask_sqlalchemy import SQLAlchemy

logger = logging.getLogger(__name__)


# The visibilities sharing governs: a sentinel is armed, the read gate decides,
# shares apply. `public` joins them under OWNERSHIP_PUBLIC_SCOPE="tenant" (the
# default): see `is_enforced` / `enforced_visibilities` below -- the code
# paths that arm and check sentinels ask those, not this tuple, so the tuple
# keeps its narrower meaning ("the states with shares") for the share logic.
GOVERNED_VISIBILITIES = ("private", "shared")

# OWNERSHIP_PUBLIC_SCOPE
#   "tenant"   (default) a public object is visible to the members of ITS
#              tenant (the mirrored `tenant_guid`), the owner and admins. An
#              object outside every tenant (no tenant stamped: created by an
#              admin with no tenant role, or not yet backfilled) is public
#              to everyone with dataset access, as before. Decided from the
#              ownership row alone -- zero store calls, readable through a
#              store outage, same cost as before.
#   "instance" the pre-0004 behaviour: public means this module says
#              nothing and Superset's own rules (dataset grant) decide, so a
#              public object built on a dataset two tenants share is visible
#              to both tenants.
PUBLIC_SCOPES = ("tenant", "instance")


def public_scope() -> str:
    """The effective OWNERSHIP_PUBLIC_SCOPE; an unrecognised value is logged
    once per process and treated as the default ("tenant"), never as the
    weaker "instance"."""
    from superset_ownership import settings

    raw = settings.get("OWNERSHIP_PUBLIC_SCOPE", None, settings.as_str)
    if raw is None:
        return "tenant"
    value = str(raw).strip().lower()
    if value in PUBLIC_SCOPES:
        return value
    if value not in _warned_public_scope:
        _warned_public_scope.add(value)
        logger.warning(
            "superset_ownership: OWNERSHIP_PUBLIC_SCOPE=%r is not one of %s; "
            "using 'tenant'",
            raw,
            PUBLIC_SCOPES,
        )
    return "tenant"


_warned_public_scope: set[str] = set()


def public_is_tenant_scoped() -> bool:
    return public_scope() == "tenant"


def enforced_visibilities() -> tuple[str, ...]:
    """The visibilities whose objects carry a sentinel and are decided by the
    read gate: private and shared always; public too under tenant scope."""
    if public_is_tenant_scoped():
        return GOVERNED_VISIBILITIES + ("public",)
    return GOVERNED_VISIBILITIES


def is_enforced(row: Optional["OwnershipRow"]) -> bool:
    """Does the read gate (and the sentinel) govern this row?"""
    return row is not None and row.visibility in enforced_visibilities()


def normalize_tenant(tenant_guid: Optional[str]) -> Optional[str]:
    """The tenant GUID as the row stores and compares it: lower-cased,
    stripped, and None for blank. The default resolver lower-cases a
    GUID-shaped value; an OWNERSHIP_TENANT_GUID hook need not, and a stamp
    from one source must still match a caller resolved by the other (N2)."""
    if tenant_guid is None:
        return None
    value = str(tenant_guid).strip().lower()
    return value or None


def public_row_visible_to(row: "OwnershipRow", user: Any) -> bool:
    """The "public within the tenant" rule, from the row alone.

    The caller's tenant -- resolved by the Identity seam (the `tenant_<guid>`
    role by default, or the OWNERSHIP_TENANT_GUID hook), never the store --
    must be the object's tenant. Both sides may be None, and only then do
    they match: an object with no tenant stamped (created by an account
    outside every tenant, or not yet backfilled) is visible to callers who
    are themselves outside every tenant -- operators, service accounts --
    and to its owner, and to NOBODY in any tenant until it is stamped. Fail
    closed: an untenanted row is a gap to repair (`check` says so), not a
    licence to read across tenants. The dataset grant is the caller's to
    check first; this only answers the tenancy half.
    """
    user_id = getattr(user, "id", None)
    if row.owner_user_id is not None and row.owner_user_id == user_id:
        return True
    if user is None:
        return row.tenant_guid is None
    from superset_ownership.identity import resolve_tenant_guid

    return normalize_tenant(resolve_tenant_guid(user)) == normalize_tenant(
        row.tenant_guid
    )


_ADMINISTERED_TENANT_SETTING = "superset_ownership.administered_tenant"
_NO_TENANT = ""  # cached "administers nothing": distinct from a cache miss


def administered_tenant(user: Any) -> Optional[str]:
    """The tenant this caller administers (normalised GUID), or None.

    The read gate's tenant-administrator ground: an administrator reads
    every object of their own tenant (`tenant_admin_reads`). Answered once
    per user per request, and for OWNERSHIP_LOOKUP_CACHE_TTL seconds across
    requests through the hook cache -- the same cache and TTL
    `OWNERSHIP_IS_TENANT_ADMINISTRATOR` uses -- because the dashboard tile
    marker asks per tile and the default `is_tenant_administrator` is one
    store check per call. Only a "yes" reaches the shared layer: a "no" is
    request-local, so a freshly granted administrator is recognised on their
    next request rather than after the TTL (a revoked one keeps reading for
    at most the TTL, which is what the TTL bounds -- see the setting's
    doc). The flip side, by design: a caller who administers nothing pays
    one administrator check per request on any route that asks (the read
    gate's owner fast path comes first, so an owner's own reads never do);
    a shared negative would save that check at the price of a grant taking
    the TTL to show. Fails closed (None) on an identity or directory
    error, which is logged; a failure is never cached.
    """
    user_id = getattr(user, "id", None)
    if user is None or user_id is None:
        return None
    from superset_ownership import plugin_hooks

    cached = plugin_hooks.cache_get(_ADMINISTERED_TENANT_SETTING, "user", user_id)
    if cached is not plugin_hooks.MISS:
        return cached or None
    from superset_ownership.api import is_tenant_administrator
    from superset_ownership.identity import resolve_tenant_guid

    try:
        tenant = normalize_tenant(resolve_tenant_guid(user))
    except Exception:  # noqa: BLE001 - identity seam (I-2): fail closed, say so
        logger.exception(
            "superset_ownership: identity plug-in raised resolving the tenant of "
            "user %s; treated as administering no tenant for this request",
            user_id,
        )
        return None
    try:
        answer = tenant if tenant and is_tenant_administrator(user) else None
    except Exception:  # noqa: BLE001 - is_tenant_administrator logged it; fail closed
        return None
    plugin_hooks.cache_set(
        _ADMINISTERED_TENANT_SETTING,
        "user",
        user_id,
        answer or _NO_TENANT,
        shared=answer is not None,
    )
    return answer


def forget_administered_tenant(user_id: int) -> bool:
    """Drop the cached "yes, administers <tenant>" for one user.

    `administered_tenant` caches a positive answer in the shared layer for
    `OWNERSHIP_LOOKUP_CACHE_TTL` seconds and nothing invalidates it, so a
    tenant administrator the platform has just demoted keeps reading that
    tenant's private objects until the entry ages out (issue #130). The TTL
    is the bound; this is the way to not wait for it.

    TWO keys, in this order (review round 1). The derived answer is
    recomputed from `is_tenant_administrator`, and where a deployment
    configures `OWNERSHIP_IS_TENANT_ADMINISTRATOR` that hook's own answer is
    cached in the same shared layer under its own key. Dropping only the
    derived one meant the recompute read the stale hook answer and re-cached
    the same "yes" with a fresh full TTL -- the command made the problem
    last longer. The hook key goes first: a request landing between the two
    reads the derived answer it would have read anyway, and cannot
    republish it from a stale hook.
    """
    from superset_ownership import plugin_hooks

    hook_dropped = plugin_hooks.cache_forget(
        "OWNERSHIP_IS_TENANT_ADMINISTRATOR", user_id
    )
    derived_dropped = plugin_hooks.cache_forget(_ADMINISTERED_TENANT_SETTING, user_id)
    return hook_dropped or derived_dropped


def tenant_admin_reads(row: "OwnershipRow", user: Any) -> bool:
    """May this caller read the object because they administer its tenant?

    From the row alone (revision 0004's mirrored tenant), zero store calls
    beyond the cached administrator check: the row's tenant must be set and
    be the caller's administered tenant. Whatever the visibility -- private,
    shared, public, and unowned -- an administrator sees every object of
    their tenant, so they can re-home it (transfer, take ownership, claim);
    only the owner decides who ELSE sees it (`_sharing_ground`). An
    untenanted row is nobody's tenant, so no administrator reads it here
    (the claim path for an unowned untenanted object is `_tenant_admin_
    reason`'s, unchanged). The dataset grant is the caller's to check first.
    """
    tenant = normalize_tenant(getattr(row, "tenant_guid", None))
    if not tenant:
        return False
    return administered_tenant(user) == tenant


def administered_object_ids(asset_type: str, user: Any):
    """A selectable of the `object_id`s of every governed row in the tenant
    this caller administers -- `tenant_admin_reads` stated in SQL for the
    list filter -- or None when the caller administers no tenant."""
    tenant = administered_tenant(user)
    if not tenant:
        return None
    return select(ownership_object.c.object_id).where(
        ownership_object.c.asset_type == asset_type,
        ownership_object.c.tenant_guid == tenant,
        ownership_object.c.visibility.in_(enforced_visibilities()),
    )


def set_row_tenant(
    asset_type: str,
    object_uuid: Optional[str],
    tenant_guid: str,
    *,
    object_id: Optional[int] = None,
) -> None:
    """Mirror the object's tenant onto its ownership row (revision 0004).

    Called by every path that writes the store's `tenant` tuple, so the row
    and the store agree; idempotent. A row that does not exist yet (creation
    order) is stamped by the caller's own upsert instead. Matched by uuid --
    how the store addresses the object -- or, when the caller knows it, by
    `object_id`, so a prototype-era row with no uuid recorded is stamped
    too rather than left untenanted.
    """
    tenant_guid = normalize_tenant(tenant_guid)
    if not tenant_guid or not (object_uuid or object_id is not None):
        return
    db = _db()
    match = [ownership_object.c.asset_type == asset_type]
    if object_id is not None:
        match.append(ownership_object.c.object_id == int(object_id))
    else:
        match.append(ownership_object.c.object_uuid == object_uuid)
    rows = db.session.execute(
        select(ownership_object.c.asset_type, ownership_object.c.object_id).where(
            *match,
            ownership_object.c.tenant_guid.is_distinct_from(tenant_guid),
        )
    ).all()
    if not rows:
        return
    db.session.execute(
        update(ownership_object).where(*match).values(tenant_guid=tenant_guid)
    )
    for r in rows:
        invalidate(r.asset_type, int(r.object_id))


def public_object_ids_visible_to(asset_type: str, user: Any):
    """A selectable of the `object_id`s of the public rows this caller may
    see -- the same rule as `public_row_visible_to`, stated in SQL so the
    list filter can AND it with Superset's own dataset-grant filter without
    loading a model or asking the store: the caller's tenant's rows (NULL
    matches only a caller with no tenant), plus the rows the caller owns.
    Under instance scope every public row (the caller's dataset grant then
    decides, as before)."""
    from superset_ownership.identity import resolve_tenant_guid

    where: list[Any] = [
        ownership_object.c.asset_type == asset_type,
        ownership_object.c.visibility == "public",
    ]
    if public_is_tenant_scoped():
        tenant = (
            normalize_tenant(resolve_tenant_guid(user)) if user is not None else None
        )
        tenancy = (
            ownership_object.c.tenant_guid == tenant
            if tenant
            else ownership_object.c.tenant_guid.is_(None)
        )
        user_id = getattr(user, "id", None)
        if user_id is not None:
            tenancy = tenancy | (ownership_object.c.owner_user_id == user_id)
        where.append(tenancy)
    return select(ownership_object.c.object_id).where(*where)


@dataclass
class OwnershipRow:
    id: int
    asset_type: str
    object_id: int
    object_uuid: Optional[str]
    owner_user_id: Optional[int]
    visibility: str
    # Revision 0004. Optional with a default so a row decoded from a cache
    # entry written before the column existed still constructs.
    tenant_guid: Optional[str] = None


def _db() -> "SQLAlchemy":
    from superset import db

    return db


# --- ownership-row cache ----------------------------------------------------
#
# Spec section 6.2 / the SOW performance criterion: an owner's or a public
# object's request must cost ZERO authorization-store calls and AT MOST ONE
# cached lookup of the ownership row. Zero store calls always held. The one
# lookup did not: `lookup()` was a query every time, and one request calls it
# from the bypass hook, the editors resolver, the owners resolver, the
# chart-data guard and the list filter's candidate pruning -- three to five
# round-trips per request to read one row that did not change in between.
#
# Two layers, both in front of the SELECT:
#
#   request-local   on `flask.g`, keyed (asset_type, object_id) -> row | None.
#                   Negative results are cached too: "no row" is the answer for
#                   every ungoverned object and is asked just as often. Lives
#                   exactly as long as the request. Without an app context
#                   (CLI bootstrap, pure tests) there is no `g` and no cache;
#                   every call queries.
#
#   shared, short   Superset's own Flask-Caching instance (`CACHE_CONFIG`, the
#                   same backend the rest of the app uses), TTL
#                   OWNERSHIP_LOOKUP_CACHE_TTL seconds (default 10; 0 or blank
#                   -> off). This is what makes the SECOND request for the same
#                   dashboard free, not just the second call in the first.
#
# The key is the OBJECT, never the user: the row says who owns the object and
# how visible it is, and that answer is the same whoever asks. So one tenant's
# request populating an entry that another tenant's request reads is correct
# by construction -- there is nothing tenant- or user-specific in the value.
# Access decisions are made on top of the row, per request, by the hooks.
#
# The same TTL also bounds the one per-USER answer this module caches, the
# administered tenant (`administered_tenant`): a "yes" is served from the
# shared layer for up to OWNERSHIP_LOOKUP_CACHE_TTL seconds after the
# administrator relation is revoked, and nothing invalidates it earlier --
# so this setting is the longest a former tenant administrator keeps
# reading the tenant's objects. A "no" is never shared. Size it with that in
# mind (10 s by default); 0 turns the shared layer off for both.
#
# INVALIDATION is explicit and happens TWICE per write, on either side of the
# commit. Every write to `ownership_object` / `ownership_share` in this
# package goes through `invalidate()` / `invalidate_all()` (see the writers
# below, hooks.after_asset_delete, authz.LocalAuthorizer.purge_object,
# backfill and lifecycle). At the time of the write that call:
#
#   - drops the request-local copy and marks the key DIRTY for the rest of
#     the request: later reads of it go to the database (which, in the
#     writer's own session, shows the uncommitted write) and are kept
#     request-locally only, never written back to the shared cache; and
#   - deletes the shared entry, and records the key as PENDING replay.
#
# The pre-commit delete alone is not enough. Between the write and the
# commit, a reader in ANOTHER request or worker sees the shared miss,
# SELECTs the still-committed OLD row (READ COMMITTED) and publishes it --
# and that entry would answer for the object for a full TTL after the commit
# made it wrong. So the shared-layer deletes are REPLAYED once the
# transaction is real: guard._after_commit calls `replay_invalidations()`,
# which deletes the pending keys again (and bumps the generation again when
# the request invalidated everything). SQLAlchemy fires `after_commit` for a
# SAVEPOINT release as well as for the outer commit, and a savepoint release
# makes nothing visible to other connections, so the listener replays only
# when `session.in_nested_transaction()` is False -- otherwise the pending
# set would be consumed while the transaction was still open and the outer
# commit would have nothing left to replay. guard._after_rollback calls
# `discard_invalidations()`: a full rollback drops the pending set without
# touching the shared layer and forgets the request-local rows (they may
# hold the rolled-back value); a savepoint rollback forgets the request-local
# rows only (a row read inside the savepoint may hold what it discarded) and
# keeps the pending set, because the outer transaction and the writes made
# before the savepoint are still live. Dirty marks survive all of these, so
# the writing request itself never republishes. The CLI paths that commit
# explicitly (backfill, lifecycle) call `replay_invalidations()` after their
# commit as well; it is idempotent, so the listener and the explicit call
# cannot double-publish.
#
# What remains is the interval between a concurrent reader's SELECT and its
# SET straddling the writer's commit and replay: on a per-process backend the
# reader's Python between the two, on a network backend (Redis) the reader's
# result decoding plus one SET round trip against the writer's one DELETE
# round trip -- milliseconds -- bounded by the TTL, rather than the whole
# length of the writer's transaction.
#
# Writers read FRESH: a decision to INSERT or UPDATE, or who may write, must
# never be made on a cached row. The mutating routes and upsert_ownership
# read through `lock_object` (below), which is a fresh read that also takes
# the per-object write lock; the flush guard reads `fresh=True` without one.
#
# What is deliberately NOT cached: the outbox read gate
# (outbox.has_pending_revocation) -- it exists to be fresh -- and every share
# query (has_share_row, list_share_rows, uuids_shared_with): those feed the
# non-owner path, which the criterion does not cover, and they change on
# every share write. `lookup_by_uuid` (the local authorizer's key) is likewise
# left as a query. `get_authorizer().list_objects(...)`, the list base
# filter's own cost (issue #82), has its OWN cache -- see "list_objects
# (reverse-index) cache" below, a separate section with the same shape as
# this one but keyed (asset_type, subject) rather than (asset_type, object_id).
#
# Multi-process note: with a shared backend (Redis) the post-commit delete is
# global and the TTL bounds only the straddle window above. With a
# per-process backend (`SimpleCache`, `NullCache`) an invalidation reaches
# the worker that wrote and nothing else, so every OTHER worker serves the
# old row for a full TTL after every write -- a window no replay can close.
# `_shared_layer_usable()` therefore turns the shared layer off, once and
# with a warning, when the backend is per-process and this process is not
# alone: the worker count says so (SERVER_WORKER_AMOUNT, which the docker
# entrypoint and the helm chart export to every worker, or WEB_CONCURRENCY,
# which gunicorn reads), or the count is UNKNOWN and the process is served
# by gunicorn (it exports SERVER_SOFTWARE to its workers and never the
# count), by uwsgi, or is a Celery worker (its pool is not described by the
# web's count at all). An unknown count under a forking server is treated
# as "several", never as "one": the layer is refused and the warning names
# the variable to set. Only a process nothing marks as forked (`flask run`,
# the CLI, the test runner) keeps the layer on an unknown count -- one
# process with threads shares one memory. The TTL is the bound on staleness
# where the layer is on, and it is short on purpose.
#
# The single-worker case is exact only for writes the WEB process makes. On
# a per-process backend the lifecycle CLI (`disable`, `enable`,
# `purge_tenant`, `backfill`, `teardown`), the outbox drain's
# `LocalAuthorizer.purge_object` and the retention purge's
# `after_asset_delete` (Celery) invalidate and bump their OWN process's
# memory; the web worker's copy is untouched and ages out over the TTL. So
# `enable` after a `disable` window, or a purge, is followed by up to one
# TTL of the previous row in the web process. A shared backend has no such
# tail: the CLI's and Celery's replays reach the same Redis.

LOOKUP_CACHE_TTL_DEFAULT = 10
# Bumped with the row shape. "v2": revision 0004 added `tenant_guid`; an
# entry written by older code decodes without it (None -> untenanted), so
# the old entries must not be read by the new code (N1).
_CACHE_VERSION = "v2"
_GEN_KEY = "superset_ownership:row:gen"
# M-2 (review round 2, PR #100): the list_objects (reverse-index) cache's
# own generation, separate from the row cache's `_GEN_KEY` above -- see the
# "GENERATION" paragraph in the list_objects cache's module comment below
# for why. `_read_generation` / `_generation` / `_bump_generation` all take
# which of the two keys to use; `invalidate_all()` bumps both.
_LIST_OBJECTS_GEN_KEY = "superset_ownership:list_objects:gen"
# The generation key must outlive every entry it governs. A finite timeout
# well above the row TTL keeps it from sorting FIRST in a backend that evicts
# by nearest expiry (Flask-Caching's SimpleCache treats timeout=0 as
# expires=0 and prunes it before anything else once over its threshold).
_GEN_KEY_MIN_TIMEOUT = 24 * 60 * 60
# Stored in place of a row for a negative result, so a cache MISS (backend
# returns None) and a cached "no row" can be told apart.
_ABSENT = "absent"


class CacheBackend(Protocol):
    """What the shared layer needs from a cache. Flask-Caching's `Cache` has
    exactly this; the tests inject a dict-backed fake. `get_many` /
    `set_many` (see `BulkCacheBackend`) are used when the backend offers
    them and are not required."""

    def get(self, key: str) -> Any: ...

    def set(self, key: str, value: Any, timeout: Optional[int] = None) -> Any: ...

    def delete(self, key: str) -> Any: ...


@runtime_checkable
class BulkCacheBackend(CacheBackend, Protocol):
    """A `CacheBackend` that can read, write and delete several keys in one
    round trip (Redis). Flask-Caching's `Cache` offers all three."""

    def get_many(self, *keys: str) -> list[Any]: ...

    def set_many(
        self, mapping: dict[str, Any], timeout: Optional[int] = None
    ) -> Any: ...

    def delete_many(self, *keys: str) -> Any: ...


# Test seams. When set, they replace `flask.g` and Superset's cache_manager
# respectively, so the layers can be exercised with a dict and a fake cache
# and no Flask at all. `_ttl_override` replaces the config read.
_request_cache_override: Optional["_RequestCache"] = None
_shared_cache_override: Optional[CacheBackend] = None
_ttl_override: Optional[int] = None
# Decided once per process by `_shared_layer_usable()`: None = not yet looked
# at (or the backend could not be inspected: judged again next time), True =
# the configured backend may be used, False = per-process backend under
# several workers, or under a forking server with an unknown worker count;
# the shared layer is off (and `lookup_cache_ttl()` reports 0).
_shared_layer_verdict: Optional[bool] = None
# Above this many pending keys a replay bumps the generation instead of
# deleting one key at a time: a purge or a backfill of thousands of objects
# is one SET, not thousands of DELETE round trips.
REPLAY_BUMP_THRESHOLD = 256


class _RequestCache:
    """One request's view of the rows it has resolved.

    `rows`: (asset_type, object_id) -> OwnershipRow | None (None = no row).
    `dirty`: keys written during this request; `all_dirty`: a bulk write
    happened. Dirty keys are read from the database and never published to
    the shared cache for the rest of the request. `pending` / `pending_all`:
    the shared-layer invalidations to replay after the transaction commits
    (see the module comment); a rollback drops them. `generation` memoises
    the shared cache's generation so it is read once per request.

    `list_objects` / `list_objects_dirty` / `list_objects_all_dirty` /
    `list_objects_pending` / `list_objects_pending_all` are the same ideas
    for the SEPARATE list_objects (reverse-index) cache below (issue #82):
    (asset_type, subject) -> the raw `shared_tuples` `list_objects`
    answered with. It shares this request's `generation` field and the
    shared layer's ONE generation counter with the row cache -- its shared
    keys are versioned by the same number, so `invalidate_all()` (a bulk
    row write, or a test fixture wanting a clean slate -- see its own
    docstring) turns this cache over too, in the same one write, with
    nothing enumerated. A key in `list_objects_dirty` (or every key, once
    `list_objects_all_dirty`) was invalidated this request and is never
    read from or published to the shared layer again until the request
    ends; `list_objects_pending` / `list_objects_pending_all` are replayed
    (deleted again) after commit, exactly like `pending` / `pending_all`
    above and for the same READ COMMITTED race (see `cached_list_objects`
    and `invalidate_list_objects`).
    """

    __slots__ = (
        "rows",
        "dirty",
        "all_dirty",
        "pending",
        "pending_all",
        "generation",
        "list_objects",
        "list_objects_dirty",
        "list_objects_all_dirty",
        "list_objects_pending",
        "list_objects_pending_all",
        "list_objects_generation",
    )

    def __init__(self) -> None:
        self.rows: dict[tuple[str, int], Optional[OwnershipRow]] = {}
        self.dirty: set[tuple[str, int]] = set()
        self.all_dirty = False
        self.pending: set[tuple[str, int]] = set()
        self.pending_all = False
        self.generation: Optional[int] = None
        self.list_objects: dict[tuple[str, str], list[str]] = {}
        self.list_objects_dirty: set[tuple[str, str]] = set()
        self.list_objects_all_dirty = False
        self.list_objects_pending: set[tuple[str, str]] = set()
        self.list_objects_pending_all = False
        # M-2: the list_objects cache's own generation (see `_LIST_OBJECTS_
        # GEN_KEY`), memoised separately from `generation` (the row cache's)
        # so the two can be bumped independently within one request.
        self.list_objects_generation: Optional[int] = None

    def is_dirty(self, key: tuple[str, int]) -> bool:
        return self.all_dirty or key in self.dirty

    def is_list_objects_dirty(self, key: tuple[str, str]) -> bool:
        return self.list_objects_all_dirty or key in self.list_objects_dirty


def _request_cache() -> Optional[_RequestCache]:
    """The current request's cache, or None when there is no app context."""
    if _request_cache_override is not None:
        return _request_cache_override
    try:
        from flask import g, has_app_context

        if not has_app_context():
            return None
        cache = getattr(g, "_ownership_row_cache", None)
        if cache is None:
            cache = _RequestCache()
            g._ownership_row_cache = cache
        return cache
    except Exception:  # noqa: BLE001 - no Flask, or a torn-down context
        return None


def parse_lookup_cache_ttl(raw: Any, default: int = LOOKUP_CACHE_TTL_DEFAULT) -> int:
    """The one parser for OWNERSHIP_LOOKUP_CACHE_TTL, shared by the config
    module and `lookup_cache_ttl()` so the two cannot disagree.

    None (unset) -> `default`. A blank string or 0 -> 0, the shared layer
    off. A negative number -> 0. Anything that is not a whole number -> a
    warning and `default`: a typo in an environment variable must degrade
    to the documented default, never keep the application from starting.
    """
    if raw is None:
        return default
    if isinstance(raw, bool):
        return default
    if isinstance(raw, Integral):
        return max(0, int(raw))
    text = str(raw).strip()
    if text == "":
        return 0
    try:
        return max(0, int(text))
    except ValueError:
        logger.warning(
            "superset_ownership: OWNERSHIP_LOOKUP_CACHE_TTL=%r is not a whole number "
            "of seconds; using the default of %d",
            raw,
            default,
        )
        return default


def lookup_cache_ttl() -> int:
    """Seconds an ownership row may be served from the shared cache.

    One precedence (settings.get: config layer, then the environment, then
    the default), parsed by `parse_lookup_cache_ttl`. 0 disables the shared
    layer; the request-local layer is unaffected. A blank value at a level
    means "not set here" like every other OWNERSHIP_* key (settings.get's
    one rule, section 3 of the plug-in architecture spec) and falls through
    -- it is no longer a second spelling of 0, only a literal `0` is. 0 is
    also what this reports once `_shared_layer_usable()` has refused the
    configured backend for this process.
    """
    if _ttl_override is not None:
        return _ttl_override
    if _shared_layer_verdict is False:
        return 0
    from superset_ownership import settings

    raw = settings.get("OWNERSHIP_LOOKUP_CACHE_TTL", None, settings.as_str)
    return parse_lookup_cache_ttl(raw)


def _shared_cache() -> Optional[CacheBackend]:
    """The cross-request cache, or None when off or unavailable.

    Outside an app context Superset's cache cannot be used (it is bound to
    the app), and there is no shared cache.
    """
    if _shared_cache_override is not None:
        return _shared_cache_override
    if lookup_cache_ttl() <= 0:
        return None
    try:
        from flask import has_app_context

        if not has_app_context():
            return None
        from superset.extensions import cache_manager

        cache = cache_manager.cache
        # Inside the same boundary as the handle itself: a handle that cannot
        # be inspected (Flask-Caching's `.cache` is a property that raises
        # KeyError for a handle not initialised on the current app) is
        # unusable, not an error out of the access path. The verdict is
        # left undecided, so an initialised handle is judged next time.
        if cache is None or not _shared_layer_usable(cache):
            return None
    except Exception:  # noqa: BLE001 - Superset not importable, cache not initialised
        logger.debug(
            "superset_ownership: shared lookup cache unavailable", exc_info=True
        )
        return None
    return cache


_PER_PROCESS_BACKENDS = ("SimpleCache", "NullCache")


def _is_per_process_backend(cache: Any) -> bool:
    """Is this Flask-Caching handle backed by process memory (or nothing)?

    Flask-Caching's `Cache` keeps the backend on `.cache`; Superset's
    `SupersetCache` subclasses it. Anything else (a bare backend, a fake) is
    judged by its own class.
    """
    backend = getattr(cache, "cache", cache)
    try:
        from flask_caching.backends import NullCache, SimpleCache

        return isinstance(backend, (SimpleCache, NullCache))
    except Exception:  # noqa: BLE001 - flask_caching absent or reshaped
        return type(backend).__name__ in _PER_PROCESS_BACKENDS


def _worker_count() -> Optional[int]:
    """How many gunicorn worker PROCESSES this one is among, from the same
    variables the entrypoint passes to `--workers` (SERVER_WORKER_AMOUNT) or
    gunicorn reads itself (WEB_CONCURRENCY). None when neither is set to a
    positive whole number: the count is UNKNOWN, not one -- gunicorn exports
    nothing about it to its workers, so `gunicorn -w 8` and a
    `gunicorn.conf.py` leave both unset."""
    for name in ("SERVER_WORKER_AMOUNT", "WEB_CONCURRENCY"):
        raw = os.environ.get(name, "").strip()
        if raw:
            try:
                count = int(raw)
            except ValueError:
                continue
            if count >= 1:
                return count
    return None


def _forking_server() -> Optional[str]:
    """The multi-process server this process runs under, when the process
    itself can tell: "gunicorn" (the arbiter exports SERVER_SOFTWARE to
    every worker), "uwsgi" (the `uwsgi` module exists only inside uwsgi),
    "celery" (the worker exports CELERY_LOG_LEVEL / CELERY_LOG_FILE to its
    pool children, and its pool is not described by the web's worker
    count). None for `flask run`, the CLI and the test runner."""
    if os.environ.get("SERVER_SOFTWARE", "").strip().lower().startswith("gunicorn"):
        return "gunicorn"
    try:
        import uwsgi  # noqa: F401 - a builtin of the uwsgi binary, nowhere else

        return "uwsgi"
    except ImportError:
        pass
    if "CELERY_LOG_LEVEL" in os.environ or "CELERY_LOG_FILE" in os.environ:
        return "celery"
    return None


def _shared_layer_usable(cache: Any) -> bool:
    """Refuse the shared layer, once per process and with a warning, when it
    would be per-process memory in a multi-process deployment.

    An invalidation on `SimpleCache` reaches the worker that wrote and no
    other; with N workers every write leaves N-1 of them serving the old row
    for the full TTL, on every write, and the post-commit replay cannot help.
    That is a wider and more frequent window than the one the layer exists to
    bound, so the layer is turned off (the request-local layer stays) and the
    operator is told to point CACHE_CONFIG at a shared backend.

    The decision FAILS CLOSED: a worker count that is not known is not read
    as one. Under gunicorn or uwsgi with the count unset, or in a Celery
    worker (whose pool the web's count does not describe), the layer is
    refused and the warning names the variable to set. A process nothing
    marks as forked keeps it. A backend that cannot be inspected raises out
    of here (the caller's boundary handles it) and leaves the verdict
    undecided.
    """
    global _shared_layer_verdict
    if _shared_layer_verdict is None:
        per_process = _is_per_process_backend(cache)
        if not per_process:
            _shared_layer_verdict = True
            return True
        backend = type(getattr(cache, "cache", cache)).__name__
        server = _forking_server()
        workers = _worker_count()
        if server == "celery":
            verdict, reason = (
                False,
                "this is a Celery worker process, not the web process",
            )
        elif workers is not None:
            verdict, reason = workers <= 1, f"this is one of {workers} worker processes"
        elif server is not None:
            verdict, reason = (
                False,
                (
                    f"this process is served by {server} and its worker count is unknown "
                    "(neither SERVER_WORKER_AMOUNT nor WEB_CONCURRENCY is set)"
                ),
            )
        else:
            verdict, reason = True, "single process"
        _shared_layer_verdict = verdict
        if not verdict:
            logger.warning(
                "superset_ownership: the shared ownership-row cache is OFF: CACHE_CONFIG "
                "is a per-process backend (%s) and %s, so an invalidation would reach "
                "only the process that wrote and the others would serve the old row "
                "for the full OWNERSHIP_LOOKUP_CACHE_TTL after every write. Use a "
                "shared backend (Redis) to enable it, or export SERVER_WORKER_AMOUNT=1 "
                "when this really is a single web worker; the request-local layer is "
                "unaffected.",
                backend,
                reason,
            )
    return _shared_layer_verdict


def _object_id(value: Any) -> int:
    """Normalise an object id at the cache boundary.

    The database keys rows by integer, the shared key formats `56` and
    `"56"` identically, and the request-local key does not -- so a `str` id
    would miss the row the SELECT returned and publish a negative under the
    key every `int` caller reads. Accept an integer or a string holding one;
    refuse anything else rather than guess.
    """
    if isinstance(value, bool):
        raise TypeError(f"object_id must be an integer, got {value!r}")
    if isinstance(value, Integral):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            raise TypeError(f"object_id must be an integer, got {value!r}") from None
    raise TypeError(f"object_id must be an integer, got {value!r}")


def _gen_key_timeout() -> int:
    return max(_GEN_KEY_MIN_TIMEOUT, 4 * lookup_cache_ttl())


def _read_generation(cache: CacheBackend, gen_key: str = _GEN_KEY) -> int:
    """The generation as the backend holds it, seeded when missing.

    `gen_key` selects which of the two generations (M-2): `_GEN_KEY` for the
    row cache, `_LIST_OBJECTS_GEN_KEY` for the list_objects cache -- the rest
    of this function's reasoning applies identically to either.

    The generation is a TIMESTAMP, not a counter: on a miss it is seeded from
    the clock and stored, and `_bump_generation()` sets it to
    `max(stored + 1, clock)`. A key the backend evicted therefore re-seeds to
    a value at or past every generation ever handed out, so the lost key
    cannot point readers back into a key space that still holds live
    (invalidated) entries. A bump (`_bump_generation`) never depends on the
    local clock being ahead -- it is `max(stored + 1, clock)` read from the
    backend, so it is monotonic across workers however their clocks are
    skewed. Only this re-seed uses the local clock, so the remaining exposure
    is the key being evicted and re-seeded, by a worker whose clock is behind,
    within (clock skew + 1) seconds of a bump: the re-seed lands on the
    bumped-away generation and its entries are reachable again until they
    age out. Bounded by the row TTL.
    """
    raw: Any = None
    try:
        raw = cache.get(gen_key)
    except Exception:  # noqa: BLE001 - a broken backend is a miss, never an error
        raw = None
    try:
        gen = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        gen = 0
    if gen <= 0:
        gen = int(time.time())
        _shared_set(cache, gen_key, gen, _gen_key_timeout())
    return gen


def _generation(
    cache: CacheBackend, rc: Optional[_RequestCache], *, list_objects: bool = False
) -> int:
    """The generation for this request: read once and memoised on `rc`.

    `list_objects=True` reads/memoises the list_objects cache's OWN
    generation (M-2, `rc.list_objects_generation` / `_LIST_OBJECTS_GEN_KEY`)
    instead of the row cache's (`rc.generation` / `_GEN_KEY`).
    """
    if list_objects:
        if rc is not None and rc.list_objects_generation is not None:
            return rc.list_objects_generation
        gen = _read_generation(cache, _LIST_OBJECTS_GEN_KEY)
        if rc is not None:
            rc.list_objects_generation = gen
        return gen
    if rc is not None and rc.generation is not None:
        return rc.generation
    gen = _read_generation(cache, _GEN_KEY)
    if rc is not None:
        rc.generation = gen
    return gen


def _shared_key(gen: int, asset_type: str, object_id: int) -> str:
    return f"superset_ownership:row:{_CACHE_VERSION}:{gen}:{asset_type}:{object_id}"


def _encode(row: Optional[OwnershipRow]) -> Any:
    # Plain dict, not the dataclass instance: a shared backend pickles values,
    # and a pickled class instance would break the moment a deploy changed the
    # class while old entries were still live. A dict of primitives survives
    # that; a dict that no longer fits the class is treated as a miss below.
    return _ABSENT if row is None else asdict(row)


def _decode(value: Any) -> tuple[bool, Optional[OwnershipRow]]:
    """(hit, row). A malformed entry is a miss, never an exception."""
    if value is None:
        return False, None
    if value == _ABSENT:
        return True, None
    try:
        return True, OwnershipRow(**value)
    except Exception:  # noqa: BLE001 - entry from an older/newer shape
        return False, None


def _shared_get(cache: CacheBackend, key: str) -> Any:
    try:
        return cache.get(key)
    except Exception:  # noqa: BLE001
        logger.debug(
            "superset_ownership: shared lookup cache get failed", exc_info=True
        )
        return None


def _shared_set(cache: CacheBackend, key: str, value: Any, ttl: int) -> None:
    try:
        cache.set(key, value, timeout=ttl)
    except Exception:  # noqa: BLE001
        logger.debug(
            "superset_ownership: shared lookup cache set failed", exc_info=True
        )


def _shared_delete(cache: CacheBackend, key: str) -> None:
    try:
        cache.delete(key)
    except Exception:  # noqa: BLE001
        logger.debug(
            "superset_ownership: shared lookup cache delete failed", exc_info=True
        )


def _shared_delete_many(cache: CacheBackend, keys: list[str]) -> None:
    """Delete several keys: one round trip where the backend offers
    `delete_many` (Flask-Caching does), else one delete per key."""
    if not keys:
        return
    if isinstance(cache, BulkCacheBackend):
        try:
            cache.delete_many(*keys)
            return
        except Exception:  # noqa: BLE001
            logger.debug(
                "superset_ownership: shared lookup cache delete_many failed",
                exc_info=True,
            )
            return
    for key in keys:
        _shared_delete(cache, key)


def _select_rows(asset_type: str, object_ids: Iterable[int]) -> dict[int, OwnershipRow]:
    """The ONE database read behind both lookups."""
    ids = list(object_ids)
    if not ids:
        return {}
    rows = (
        _db()
        .session.execute(
            select(ownership_object).where(
                ownership_object.c.asset_type == asset_type,
                ownership_object.c.object_id.in_(ids),
            )
        )
        .mappings()
        .all()
    )
    return {int(r["object_id"]): OwnershipRow(**dict(r)) for r in rows}


def _delete_shared(
    cache: CacheBackend, rc: Optional[_RequestCache], key: tuple[str, int]
) -> None:
    """Delete one object's shared entry under the generation the BACKEND
    holds -- not the one this request memoised, which another worker may
    have bumped since -- and, when the two differ, under the memoised one as
    well for any request still reading it."""
    asset_type, object_id = key
    gen = _read_generation(cache, _GEN_KEY)
    _shared_delete(cache, _shared_key(gen, asset_type, object_id))
    if rc is not None:
        if rc.generation is not None and rc.generation != gen:
            _shared_delete(cache, _shared_key(rc.generation, asset_type, object_id))
        rc.generation = gen


def invalidate(asset_type: str, object_id: int) -> None:
    """Forget one object's row in both layers. Call after EVERY write to it.

    Also marks the key dirty for the rest of the request (a dirty key is
    never published to the shared cache again during this request, so a
    rolled-back write cannot leave a stale entry), records it for the
    post-commit replay (see the module comment and `replay_invalidations`),
    and drops the editors resolver's derived entry for the object, which is
    built from this row and the share rows.
    """
    key = (asset_type, _object_id(object_id))
    rc = _request_cache()
    if rc is not None:
        rc.rows.pop(key, None)
        rc.dirty.add(key)
        rc.pending.add(key)
    cache = _shared_cache()
    if cache is not None:
        _delete_shared(cache, rc, key)
    _drop_editor_cache_entry(asset_type, key[1])


def invalidate_all() -> None:
    """Forget every row: for bulk writes that do not enumerate their rows
    (disable() parking, enable() restoring, teardown) -- and for a test
    fixture that wants a clean slate between tests sharing one long-lived
    application (see the plugin verify / endpoint suites' `harness`
    fixture): unlike the row cache (keyed by object id, so a fresh test's
    fresh object ids never collide with an earlier test's entries), the
    list_objects cache below is keyed by SUBJECT, and a long-lived test
    session's subjects (Ada, Ben, ...) are the same across every test, so
    without a reset here a subject's cached reverse-index answer from one
    test would still be served, inside the TTL, to the next.

    The shared layer cannot enumerate its keys on every backend, so it is
    versioned instead: entries carry a generation number, and bumping it
    makes every existing entry unreachable (they age out on their own). The
    bump is replayed after the commit like a single-key invalidation. Since
    M-2 the list_objects cache (issue #82) has its OWN generation, separate
    from the row cache's -- see that cache's module comment -- so a full
    reset bumps BOTH, one call each; a single ordinary write still turns
    over only the generation the write actually concerns.
    """
    rc = _request_cache()
    if rc is not None:
        rc.rows.clear()
        rc.dirty.clear()
        rc.all_dirty = True
        rc.pending.clear()
        rc.pending_all = True
        rc.list_objects.clear()
        rc.list_objects_dirty.clear()
        rc.list_objects_all_dirty = True
        rc.list_objects_pending.clear()
        rc.list_objects_pending_all = True
    cache = _shared_cache()
    if cache is not None:
        _bump_generation(cache, rc, list_objects=False)
        _bump_generation(cache, rc, list_objects=True)
    _drop_editor_cache_entry(None, None)


def _bump_generation(
    cache: CacheBackend, rc: Optional[_RequestCache], *, list_objects: bool = False
) -> int:
    """Bump the row cache's generation, or (M-2) the list_objects cache's
    own one, making every entry under the old value unreachable."""
    gen_key = _LIST_OBJECTS_GEN_KEY if list_objects else _GEN_KEY
    gen = max(_read_generation(cache, gen_key) + 1, int(time.time()))
    _shared_set(cache, gen_key, gen, _gen_key_timeout())
    if rc is not None:
        if list_objects:
            rc.list_objects_generation = gen
        else:
            rc.generation = gen
    return gen


def replay_invalidations() -> None:
    """Delete the shared entries this request invalidated, again, once its
    transaction has committed. Run by guard._after_commit (outer commits
    only, never a savepoint release) and by the CLI paths after their
    explicit commit; idempotent (the pending set is consumed), and a no-op
    with nothing pending or no shared layer.

    The generation is read at most ONCE PER CACHE per replay (row and
    list_objects, M-2's two independent counters), not once per key: a
    replay of N row keys and M list_objects keys is N+M deletes (more when
    this request memoised an older generation for either), not one read per
    key -- and one `delete_many` round trip where the backend offers it.
    When the request invalidated everything IN ONE of the two caches, that
    cache's generation bump alone makes every one of its entries
    unreachable, so its per-key deletes are skipped; the same bump replaces
    the deletes when more than REPLAY_BUMP_THRESHOLD keys are pending for
    that cache. The two caches are judged, bumped and deleted independently
    -- a row-cache bulk invalidation no longer forces a list_objects bump or
    vice versa.

    Dirty marks are kept: the writing request itself still never
    republishes, whatever it reads after the commit.
    """
    rc = _request_cache()
    lo_any = rc is not None and (rc.list_objects_pending or rc.list_objects_pending_all)
    if rc is None or not (rc.pending or rc.pending_all or lo_any):
        return
    pending, rc.pending = rc.pending, set()
    pending_all, rc.pending_all = rc.pending_all, False
    lo_pending, rc.list_objects_pending = rc.list_objects_pending, set()
    lo_pending_all, rc.list_objects_pending_all = rc.list_objects_pending_all, False
    cache = _shared_cache()
    if cache is None:
        return

    if pending_all or len(pending) > REPLAY_BUMP_THRESHOLD:
        _bump_generation(cache, rc, list_objects=False)
        pending = set()
    if lo_pending_all or len(lo_pending) > REPLAY_BUMP_THRESHOLD:
        _bump_generation(cache, rc, list_objects=True)
        lo_pending = set()
    if not pending and not lo_pending:
        return

    keys: list[str] = []
    if pending:
        gen = _read_generation(cache, _GEN_KEY)
        memoised = rc.generation
        keys += [_shared_key(gen, t, i) for t, i in pending]
        if memoised is not None and memoised != gen:
            keys += [_shared_key(memoised, t, i) for t, i in pending]
        rc.generation = gen
    if lo_pending:
        gen = _read_generation(cache, _LIST_OBJECTS_GEN_KEY)
        memoised = rc.list_objects_generation
        keys += [_list_objects_key(gen, t, s) for t, s in lo_pending]
        if memoised is not None and memoised != gen:
            keys += [_list_objects_key(memoised, t, s) for t, s in lo_pending]
        rc.list_objects_generation = gen
    _shared_delete_many(cache, keys)


def discard_invalidations(*, savepoint: bool = False) -> None:
    """A rollback happened. Forget every request-local row -- the ones read
    after a write hold the rolled-back value -- and the editors resolver's
    derived entries built from them. Dirty marks are kept. Run by
    guard._after_rollback.

    A FULL rollback (`savepoint=False`) also drops the pending replay without
    touching the shared layer: nothing was committed, so the entries a
    concurrent reader published are right.

    A SAVEPOINT rollback (`savepoint=True`) keeps the pending set: the outer
    transaction is still live and the writes made before the savepoint will
    still commit and still need their replay. A key the savepoint's own
    write added costs one redundant delete at the outer commit, which is
    cheaper than a replay that misses a key. Rows are dropped either way,
    because a row read inside the savepoint may hold exactly what it
    discarded; the next read in the request re-selects.

    The `list_objects` cache (issue #82) follows the same rule: its
    memoised values are dropped either way (a value read inside the
    savepoint may hold what it discarded), its pending replay set (and
    `pending_all`) is kept on a savepoint rollback and dropped on a full
    one, and its dirty marks are never cleared here -- same reasoning as
    the row cache's `dirty`.
    """
    rc = _request_cache()
    if rc is None:
        return
    if not savepoint:
        rc.pending.clear()
        rc.pending_all = False
        rc.list_objects_pending.clear()
        rc.list_objects_pending_all = False
    rc.rows.clear()
    rc.list_objects.clear()
    _drop_editor_cache_entry(None, None)


def _drop_editor_cache_entry(
    asset_type: Optional[str], object_id: Optional[int]
) -> None:
    """hooks.extra_editors keeps a per-request cache of the DERIVED editor
    set (row + share rows + Subject rows) under f"{asset_type}:{object_id}".
    It is not a second copy of the row -- it is what the row computes to --
    so it is dropped here, with the row, and on share writes."""
    try:
        from flask import g, has_app_context

        if not has_app_context():
            return
        cache = getattr(g, "_ownership_editor_cache", None)
        if not cache:
            return
        if asset_type is None:
            cache.clear()
        else:
            cache.pop(f"{asset_type}:{object_id}", None)
    except Exception:  # noqa: BLE001
        return


def _seed_request_cache(asset_type: str, rows: Iterable[OwnershipRow]) -> None:
    """Record rows read by a scan of this request's session request-locally,
    so per-object lookups later in the request are free -- for every row the
    scan actually hands it. `owned_or_shared_rows` calls this AFTER
    subtracting any candidate with an undelivered revocation (issue #84), so
    a revoked object's row is never seeded: a per-object `lookup()` on it
    later in the same request (e.g. `raise_for_access_bypass` on the GET
    that then denies it) still reads the DB and publishes to the shared
    layer -- harmless, since the row itself is not the gate, but a real
    query the caller might expect this cache to have already avoided. Never
    touches the shared layer itself: a scan does not know which of its rows
    another request is mid-write on, and only `lookup`/`lookup_many`
    publish."""
    rc = _request_cache()
    if rc is None:
        return
    for row in rows:
        rc.rows[(asset_type, row.object_id)] = row


def lookup(
    asset_type: str, object_id: int, fresh: bool = False
) -> Optional[OwnershipRow]:
    """The ownership row for one object, or None. Cached; see above.

    `fresh=True` skips both caches for the READ (the row is still recorded
    request-locally, never in the shared layer). A writer that decides
    between "govern" and "already governed" reads fresh; one that decides
    between INSERT and UPDATE, or who may write, goes through `lock_object`
    instead, which reads fresh AND holds the row.
    """
    object_id = _object_id(object_id)
    key = (asset_type, object_id)
    rc = _request_cache()
    if not fresh and rc is not None and key in rc.rows:
        return rc.rows[key]

    cache = None if fresh else _shared_cache()
    publishable = cache is not None and (rc is None or not rc.is_dirty(key))
    skey = None
    if publishable:
        skey = _shared_key(_generation(cache, rc), asset_type, object_id)
        hit, row = _decode(_shared_get(cache, skey))
        if hit:
            if rc is not None:
                rc.rows[key] = row
            return row

    row = _select_rows(asset_type, [object_id]).get(object_id)
    if rc is not None:
        rc.rows[key] = row
    if publishable:
        _shared_set(cache, skey, _encode(row), lookup_cache_ttl())
    return row


# --- per-object write lock --------------------------------------------------
#
# Outbox rows are applied in id order per object, and an id is given at
# INSERT time, not at COMMIT time. Two requests mutating the same object at
# once could therefore commit in the opposite order to their ids: the
# mirror's final state would follow commit order, the drain's would follow id
# order, and the two would disagree (see outbox.py, ORDERING). The routes
# close that by taking THIS lock before anything else they do to the object
# and holding it until their commit, so for any one object "lock -> mirror
# write -> enqueue -> commit" runs one request at a time and id order IS
# commit order.
#
# Two locks, one helper. On Postgres a transaction-scoped advisory lock keyed
# on (asset_type, object_id) -- the two-int4 form, no hashing, so distinct
# objects never share a key -- is taken first, so an object with NO row yet --
# one being governed for the first time, or a pre-existing object the
# feature never recorded -- is serialised too (a `FOR UPDATE` that matches
# no row locks nothing). Then `SELECT ... FOR UPDATE` on the row, which is
# the lock every bulk path already shares through its own statements. On
# SQLite there is no advisory lock and `FOR UPDATE` compiles away; writers
# serialise on the database file.
#
# Lock order, so that no two paths can wait on each other:
#
#   * Each REQUEST route locks exactly one row, once, and never a second, so
#     two routes cannot form a cycle whatever objects they are called for.
#   * The bulk paths lock N rows in one transaction -- the hard-delete hook
#     for every object in the flush, `backfill` for every object it sweeps,
#     `purge_tenant` and `teardown` for every row they delete, `disable`
#     and `enable` for every row they park or restore -- and they are the
#     only paths that can wait on each other. All of them take their rows
#     in (asset_type, object_id) order, so two of them over overlapping
#     sets cannot cycle either. That argument needs the advisory key to
#     name one object and one only: two objects sharing a key would let an
#     early object of one transaction wait behind a late object of the
#     other -- an inversion the sort cannot see -- which is why the key is
#     the (asset code, object id) pair and not a hash of it.
#   * Any path that touches an object's SHARE rows takes, or already holds
#     through its own statement, that object's row first: the share routes,
#     `after_asset_delete`, `purge_tenant`, `teardown` and `disable` all
#     lock or write `ownership_object` before `ownership_share`. Object
#     then share, never the reverse -- the reverse is a cycle against a
#     share route in Postgres.
#
# A wait behind another writer is bounded by that writer's request (its
# store calls carry their own timeouts); no separate lock_timeout is set, as
# Superset's session sets none. The lock precedes the authorization decision
# on every route, so a caller the route REFUSES (403) has also held the
# object's lock across their own store calls -- at most two or three strict
# calls before the first refusal (a tenant administrator's group and tenant
# reads; a transfer's tenant read), each bounded by fga.TIMEOUT -- so an
# unauthorised member hammering a write route serialises the owner's writes
# behind their refusals for milliseconds each with the store healthy, a few
# seconds each without. The share-add route keeps its subject validation
# (the one call that can page through a tenant's members) off the lock;
# the row-based decisions cannot move, since they must be made on the
# locked row.


def _lock_statement(asset_type: str, object_id: int):
    """`SELECT ... FOR UPDATE` on one object's row. Postgres takes the row
    lock; SQLAlchemy's SQLite dialect compiles `with_for_update()` to a plain
    SELECT (SQLite has no row locks -- it serialises writers on the whole
    database file), so the same statement is right for both without a
    dialect switch."""
    return (
        select(ownership_object)
        .where(
            ownership_object.c.asset_type == asset_type,
            ownership_object.c.object_id == object_id,
        )
        .with_for_update()
    )


# The first advisory-lock key: a fixed small integer per asset type. With
# the object's id as the second key, the pair names exactly one object, so
# no two objects ever wait on the same advisory lock. Append-only: a code,
# once assigned, is never reused for another type.
ADVISORY_LOCK_CLASS: dict[str, int] = {"chart": 1, "dashboard": 2}


def _advisory_lock_key(asset_type: str, object_id: int) -> tuple[int, int]:
    """The (asset code, object id) pair `pg_advisory_xact_lock(int4, int4)`
    is called with. Distinct objects give distinct pairs -- there is no
    hashing anywhere -- and Superset's ids are int4 primary keys, so the
    pair covers every object there can be. Refuses an asset type it has no
    code for rather than lock nothing for it."""
    try:
        return ADVISORY_LOCK_CLASS[asset_type], object_id
    except KeyError:
        raise ValueError(
            f"no advisory-lock class for asset type {asset_type!r}; "
            f"add it to service.ADVISORY_LOCK_CLASS"
        ) from None


def _take_advisory_lock(asset_type: str, object_id: int) -> bool:
    """Postgres only: `pg_advisory_xact_lock(asset_code, object_id)` --
    the two-int4 form, keyed on the object itself -- released with the
    transaction. Returns whether one was taken (False on any other dialect,
    where the row lock alone -- or SQLite's file lock -- serialises).

    Two int4 keys, not a hash of a string: a hashed key folds every object
    on the instance into 32 bits, and while two unrelated single-object
    writers queueing on a shared key would be harmless, the multi-object
    paths (the hard-delete hook over a bulk delete, the backfill) take one
    advisory lock per object in (asset_type, object_id) order, and two
    objects that hash alike break that order -- one transaction's early
    object waits behind the other's late one, and Postgres reports a
    deadlock. The two-int form has no such pair. The bigint and two-int
    forms are separate key spaces in Postgres, so nothing keyed the other
    way can alias an object's lock."""
    session = _db().session
    try:
        dialect = session.get_bind().dialect.name
    except Exception:  # noqa: BLE001 - no bind yet (unusual); the row lock still applies
        return False
    if dialect != "postgresql":
        return False
    asset_code, key_id = _advisory_lock_key(asset_type, object_id)
    session.execute(
        text("SELECT pg_advisory_xact_lock(:asset_code, :object_id)"),
        {"asset_code": asset_code, "object_id": key_id},
    )
    return True


def lock_object(asset_type: str, object_id: int) -> Optional[OwnershipRow]:
    """Take the per-object write lock; return the row AS STORED, or None.

    Every mutating path calls this first -- before any read that feeds a
    decision -- and holds it for the rest of its transaction (the lock is
    released by the commit or rollback, never here). The row comes back
    fresh: under READ COMMITTED a `FOR UPDATE` that had to wait re-reads the
    row the other writer committed, so a caller that used to make a separate
    `lookup(fresh=True)` does not need it. Recorded request-locally like a
    fresh lookup, never in the shared layer.

    None means there is no row yet. On Postgres the object is serialised all
    the same: the advisory lock is taken before the SELECT, so of two
    writers governing the same row-less object the second waits for the
    first's commit and then sees its row. On other dialects a row-less
    object is not serialised by this call; `upsert_ownership` still settles
    a crossed INSERT through the unique key on (asset_type, object_id) and
    falls back to an UPDATE, so the loser keeps its transaction.
    """
    object_id = _object_id(object_id)
    _take_advisory_lock(asset_type, object_id)
    r = _db().session.execute(_lock_statement(asset_type, object_id)).mappings().first()
    row = OwnershipRow(**dict(r)) if r else None
    rc = _request_cache()
    if rc is not None:
        rc.rows[(asset_type, object_id)] = row
    return row


def disable_in_progress() -> bool:
    """Has disable() parked rows that enable() has not yet restored?

    The durable, worker-visible signal that the feature is mid-rollback. Any
    create path -- the command hook or the flush guard -- must not govern a
    new object while this holds, or the object is armed and then stranded when
    the operator completes the rollback and flips the flag off.
    """
    return (
        _db()
        .session.execute(
            select(ownership_object.c.id)
            .where(ownership_object.c.visibility.like("disabled:%"))
            .limit(1)
        )
        .first()
        is not None
    )


def lookup_many(
    asset_type: str, object_ids: Iterable[int], fresh: bool = False
) -> dict[int, OwnershipRow]:
    """Ownership rows for many objects: at most ONE query, for the ids no
    layer already knows. Objects with no row are absent from the result (and
    remembered as absent request-locally).

    One query however many ids the flush guard or the list filter asks
    about, rather than one per object.

    The shared layer is read with get_many where the backend offers it (one
    round-trip on Redis), else per key; misses are written back with set_many
    the same way.
    """
    ids = list(
        dict.fromkeys(_object_id(i) for i in object_ids)
    )  # de-duplicated, order kept
    if not ids:
        return {}
    rc = _request_cache()
    found: dict[int, OwnershipRow] = {}
    missing: list[int] = []
    for object_id in ids:
        key = (asset_type, object_id)
        if not fresh and rc is not None and key in rc.rows:
            row = rc.rows[key]
            if row is not None:
                found[object_id] = row
        else:
            missing.append(object_id)
    if not missing:
        return found

    cache = None if fresh else _shared_cache()
    ttl = lookup_cache_ttl() if cache is not None else 0
    gen = _generation(cache, rc) if cache is not None else 0
    publishable = [
        i
        for i in missing
        if cache is not None and (rc is None or not rc.is_dirty((asset_type, i)))
    ]
    if publishable:
        keys = {i: _shared_key(gen, asset_type, i) for i in publishable}
        values: dict[str, Any] = {}
        try:
            if isinstance(cache, BulkCacheBackend):
                values = dict(zip(keys.values(), cache.get_many(*keys.values())))
            else:
                values = {k: cache.get(k) for k in keys.values()}
        except Exception:  # noqa: BLE001
            logger.debug(
                "superset_ownership: shared lookup cache get_many failed", exc_info=True
            )
            values = {}
        still_missing = []
        for object_id in missing:
            skey = keys.get(object_id)
            hit, row = _decode(values.get(skey)) if skey else (False, None)
            if hit:
                if rc is not None:
                    rc.rows[(asset_type, object_id)] = row
                if row is not None:
                    found[object_id] = row
            else:
                still_missing.append(object_id)
        missing = still_missing
    if not missing:
        return found

    rows = _select_rows(asset_type, missing)
    to_publish: dict[str, Any] = {}
    publishable_set = set(publishable)
    for object_id in missing:
        row = rows.get(object_id)
        if rc is not None:
            rc.rows[(asset_type, object_id)] = row
        if row is not None:
            found[object_id] = row
        if object_id in publishable_set:
            to_publish[_shared_key(gen, asset_type, object_id)] = _encode(row)
    if to_publish:
        try:
            if isinstance(cache, BulkCacheBackend):
                cache.set_many(to_publish, timeout=ttl)
            else:
                for k, v in to_publish.items():
                    cache.set(k, v, timeout=ttl)
        except Exception:  # noqa: BLE001
            logger.debug(
                "superset_ownership: shared lookup cache set_many failed", exc_info=True
            )
    return found


def list_rows(asset_type: str) -> list[OwnershipRow]:
    db = _db()
    rows = (
        db.session.execute(
            select(ownership_object).where(ownership_object.c.asset_type == asset_type)
        )
        .mappings()
        .all()
    )
    return [OwnershipRow(**dict(r)) for r in rows]


def _update_ownership(
    asset_type: str,
    object_id: int,
    object_uuid: Optional[str],
    owner_user_id: Optional[int],
    visibility: str,
    existing: OwnershipRow,
    tenant_guid: Optional[str] = None,
) -> None:
    values: dict[str, Any] = {
        # Never blank a known uuid. The uuid is how OpenFGA tuples
        # address the object, so overwriting it with None silently
        # detaches every relationship -- and writes "dashboard:None"
        # tuples on the next share.
        "object_uuid": object_uuid or existing.object_uuid,
        "owner_user_id": owner_user_id,
        "visibility": visibility,
    }
    if tenant_guid:
        # Same rule as the uuid: a known tenant is only ever replaced by a
        # tenant, never blanked by a caller that did not resolve one.
        values["tenant_guid"] = normalize_tenant(tenant_guid)
    _db().session.execute(
        update(ownership_object)
        .where(
            ownership_object.c.asset_type == asset_type,
            ownership_object.c.object_id == object_id,
        )
        .values(**values)
    )


def repoint_object_uuid(
    asset_type: str, object_id: int, old_uuid: Optional[str], new_uuid: str
) -> bool:
    """Follow an object whose uuid changed under us, taking its grants along.

    Superset's dashboard import validates collisions by uuid but resolves by
    `slug`, so an import can replace a LIVE dashboard in place and re-point
    `dashboards.uuid` (issue #127). The ownership row, and every tuple in the
    authorization store, then described an object with a uuid nothing has:
    the owner kept the row, the store kept grants under a reference that no
    longer resolves, and `check` reported ok.

    The store is addressed BY uuid, so following the object means moving its
    tuples: purge the old reference and re-queue what the rows say -- the
    owner, the tenant and every share -- under the new one. All of it rides
    the caller's transaction, as every other write in this module does, so a
    rolled-back import takes the re-point with it.

    Returns whether anything was re-pointed.
    """
    if not new_uuid or old_uuid == new_uuid:
        return False

    db = _db()
    row = lock_object(asset_type, object_id)
    if row is None:
        return False
    # Guard against a stale caller: only follow the object when the row is
    # actually pointing at the uuid it just left.
    if old_uuid is not None and row.object_uuid and row.object_uuid != old_uuid:
        return False

    from superset_ownership import outbox
    from superset_ownership.identity import member_ref

    # Only where the store files relationships under the object's uuid.
    # On a backend keyed by object id -- the local one -- the relationships
    # are the share rows themselves: they do not move when the uuid does, so
    # there is nothing to purge, and purging would resolve the STALE uuid to
    # whatever row still holds it and delete that object's shares instead
    # (review round 3).
    by_uuid = getattr(get_authorizer(), "addresses_objects_by_uuid", True)
    stale = row.object_uuid or old_uuid
    if stale and by_uuid:
        outbox.purge_object(asset_type, stale)

    db.session.execute(
        ownership_object.update()
        .where(
            ownership_object.c.asset_type == asset_type,
            ownership_object.c.object_id == object_id,
        )
        .values(object_uuid=new_uuid)
    )
    invalidate(asset_type, object_id)

    obj = f"{asset_type}:{new_uuid}"
    if not by_uuid:
        # Nothing was purged and nothing needs re-writing: the rows this
        # backend answers from are keyed by object id and already correct.
        logger.info(
            "superset_ownership: %s %s changed uuid %s -> %s; the store is keyed by "
            "object id, so its relationships did not move",
            asset_type,
            object_id,
            stale,
            new_uuid,
        )
        return True
    if row.owner_user_id:
        subject = member_ref(row.owner_user_id)
        if subject:
            outbox.write_tuple(subject, "owner", obj)
    if getattr(row, "tenant_guid", None):
        # BY ID. This function exists to repair rows whose uuid disagrees
        # with the world, so it is the last place that may address a row by
        # uuid: `object_uuid` is not unique, and a divergence is precisely
        # the state where two rows can hold the same one (review round 2).
        outbox.set_object_tenant(
            asset_type, new_uuid, row.tenant_guid, object_id=object_id
        )
    for share in (
        db.session.execute(
            select(ownership_share).where(
                ownership_share.c.asset_type == asset_type,
                ownership_share.c.object_id == object_id,
            )
        )
        .mappings()
        .all()
    ):
        outbox.write_tuple(share["subject"], share["role"], obj)

    logger.info(
        "superset_ownership: %s %s changed uuid %s -> %s; ownership followed it",
        asset_type,
        object_id,
        stale,
        new_uuid,
    )
    return True


def upsert_ownership(
    asset_type: str,
    object_id: int,
    object_uuid: Optional[str],
    owner_user_id: Optional[int],
    visibility: str,
    tenant_guid: Optional[str] = None,
) -> None:
    db = _db()
    # A cached row must never decide INSERT vs UPDATE; the lock returns the
    # row as stored, and holds it for the rest of the transaction. A route
    # that already holds it takes it again here at no cost (same
    # transaction). No row -- a new object from govern() or the backfill --
    # means an INSERT; on Postgres the advisory lock inside lock_object has
    # already serialised the object, so a second creator waits here and
    # then finds the row.
    existing = lock_object(asset_type, object_id)
    if existing:
        _update_ownership(
            asset_type,
            object_id,
            object_uuid,
            owner_user_id,
            visibility,
            existing,
            tenant_guid=tenant_guid,
        )
    else:
        # Where nothing serialised the row-less object (another dialect, or
        # a second creator that read "no row" before the first committed),
        # two creators both reach this INSERT and the unique key on
        # (asset_type, object_id) arbitrates. The loser used to raise
        # IntegrityError out of here -- a 500 on a route, an aborted
        # backfill. Like add_share on ownership_share: a savepoint, and on
        # the conflict re-take the lock (which now finds and holds the
        # other creator's committed row) and UPDATE it instead. The outer
        # transaction, and the caller's outbox rows, stay intact.
        from sqlalchemy.exc import IntegrityError

        savepoint = db.session.begin_nested()
        try:
            db.session.execute(
                insert(ownership_object).values(
                    asset_type=asset_type,
                    object_id=object_id,
                    object_uuid=object_uuid,
                    owner_user_id=owner_user_id,
                    visibility=visibility,
                    tenant_guid=normalize_tenant(tenant_guid),
                )
            )
            savepoint.commit()
        except IntegrityError:
            savepoint.rollback()
            existing = lock_object(asset_type, object_id)
            if existing is None:
                # The unique key fired, so a row exists; a lock that cannot
                # see it is a broken invariant, not a race to paper over.
                raise
            _update_ownership(
                asset_type,
                object_id,
                object_uuid,
                owner_user_id,
                visibility,
                existing,
                tenant_guid=tenant_guid,
            )
    invalidate(asset_type, object_id)


def set_visibility(asset_type: str, object_id: int, visibility: str) -> None:
    db = _db()
    db.session.execute(
        update(ownership_object)
        .where(
            ownership_object.c.asset_type == asset_type,
            ownership_object.c.object_id == object_id,
        )
        .values(visibility=visibility)
    )
    invalidate(asset_type, object_id)


# --- list_objects (reverse-index) cache -------------------------------------
#
# Issue #82 / SOW section 6.2. The access DECISION never calls
# `list_objects` -- the owner fast path in `hooks.raise_for_access_bypass`
# returns before any store call, and a non-owner's decision costs exactly one
# `check`. But FAB applies the list base filter (`EXTRA_ACCESS_QUERY_FILTERS`
# -> `hooks._query_filter` -> `owned_or_shared_rows`, below) to a
# single-object GET as well as to the list routes -- the hook has only
# `user_id`, no object context, so it cannot special-case "this is one
# object I already own". That filter calls
# `get_authorizer().list_objects(subject, "viewer", asset_type)` once per
# call, which on the OpenFGA backend is a network round trip: an owner's
# single-object GET, `/data/`, dashboard GET and `/charts` each pay one, and
# a list page pays two (FAB applies the same base filter to both its count
# query and its item query -- review round 2 on PR #94, R2-O3, observed the
# same doubling for `pending_revocations_for`).
#
# This cache is that filter's cost, not the decision's, and it is deliberately
# a SEPARATE cache from the ownership-row one above, reusing the same two
# layers and the same `OWNERSHIP_LOOKUP_CACHE_TTL` (one operator-facing
# knob, not two):
#
#   request-local   `_RequestCache.list_objects`, keyed (asset_type, subject).
#                   Alone this already turns the list page's two calls into
#                   one -- both come from the SAME request.
#
#   shared, short   the same Flask-Caching handle as the row cache, same TTL.
#                    This is what turns the SECOND request's call into zero:
#                    an owner's repeated single-object GET, or a list page
#                    loaded twice, inside one TTL window makes exactly one
#                    `list_objects` call between them, not one each.
#
# ONLY `shared_tuples` -- the raw list `list_objects` returned -- is ever
# cached. `owned_or_shared_rows`'s own `reachable` dict (rows filtered by
# visibility, ownership, and the fresh `pending_revocations_for`
# subtraction) is NEVER cached and never will be: a revocation must take
# effect the moment it is queued, not wait out this TTL, and the
# subtraction below runs on every call whether or not the `shared_tuples`
# behind it came from this cache or a fresh store read (PR #94 review, M-1,
# recorded the constraint before this cache existed; this is it, honoured).
#
# KEYED (asset_type, subject), not (asset_type, user_id): `subject` is
# already what `list_objects` itself was asked about (`member_ref(user_id)`,
# below), two Superset users never resolve to the same subject reference,
# and keying on the exact thing the store call names means an override that
# changes how a user maps to a subject (a custom `Identity`) cannot make two
# different people share a cache entry.
#
# INVALIDATED at TWO points, both calling `invalidate_list_objects`, because
# the store's actual answer changes at TWO different moments depending on
# whether the outbox is enabled:
#
#   ENQUEUE   `outbox.write_tuple` / `delete_tuple` / `revoke_subject` --
#             the three facade functions EVERY share, unshare, transfer,
#             visibility (private re-asserts a revoke) and claim write in
#             this package goes through, enqueued or inline -- invalidate
#             for the subject and asset type the write concerns, BEFORE the
#             write itself. With the outbox DISABLED this is also the
#             moment of delivery, so this alone is enough; with it ENABLED
#             this is what makes "share, then immediately list" show the
#             change within the SAME request, without waiting on the TTL --
#             even though the store itself will not agree until the drain.
#
#   DELIVERY  `outbox._apply`, called by `drain()` for OP_WRITE / OP_DELETE
#             / OP_REVOKE_SUBJECT, invalidates again once the store's
#             answer has ACTUALLY changed. Necessary because a read between
#             the enqueue and the drain is not wrong to publish a fresh
#             cache entry of its own (the store genuinely still has the OLD
#             answer at that moment) -- and that entry would otherwise
#             outlive the delivery and answer stale for up to a full TTL
#             past the point the store's answer changed. This is the
#             authoritative invalidation; the enqueue-time one is the
#             same-request convenience on top of it.
#
# `purge_object` does NOT invalidate this cache at either point: it has no
# single subject to target (it removes every relationship on an object),
# and it does not need one -- the object's own `ownership_object` row is
# gone too by the time it matters, so `owned_or_shared_rows`'s own row scan
# (`list_rows`, above) already drops the object regardless of what a stale
# cached `shared_tuples` entry still names for some other subject.
#
# A GROUP share or revoke is the one write this cache cannot follow
# precisely: the subject named in the write is the GROUP reference
# (`group:eng#member`, say), never the individual members it reaches, and
# resolving membership to invalidate each of them would be its own store
# or directory round trip -- exactly the cost this cache exists to avoid.
# `invalidate_list_objects` handles this the same way `pending_revocations_
# for` handles a non-user subject in its own read (below): fail closed and
# cheap, never precise. A non-`user:` subject turns the WHOLE cache over
# (`invalidate_all()`'s generation bump) instead of deleting one key that
# nothing reads -- so a MEMBER's own cached answer never outlives a group
# write that changes what they can reach, at the cost of the whole cache's
# hit rate for the one write, not just that member's. This is what closes
# the same post-delivery staleness window the module comment above
# describes for an individual write, for the group case too: without it, a
# group revoke's DELIVERY would leave every member's already-warm entry
# answering as if the group grant still held, for up to a full TTL past
# the point the store actually forgot it.
#
# Cross-process staleness is bounded by the TTL, exactly like the row cache,
# and exactly what the SOW criterion accepts: "at most one reverse-index
# read per subject per TTL", not "always fresh". The REVOCATION direction
# never WAITS on it -- `pending_revocations_for` runs fresh on every call
# regardless (above), so a revoke or a demotion still bites the moment it is
# queued -- and, since review round 2 (H-1), it is not merely unaffected by
# a stale entry: `owned_or_shared_rows` actively drops this cache's entry
# for the subject the moment the subtraction finds anything to subtract, so
# the cache cannot go on ANSWERING the pre-revocation list either, on any
# cache backend. Before that fix this paragraph's claim held only on a
# shared backend (Redis), where `outbox._apply`'s delivery-time
# invalidation (above) reaches every process; on a per-process backend
# (SimpleCache -- this stack's flask web + Celery drain shape) delivery
# happens in the DRAIN's process and never reached the web worker's entry,
# so a revoked, group-revoked or transferred-away subject stayed listed,
# and `GET` on the object stayed 200 (FAB applies this filter to a
# single-object GET too), for up to a full TTL after the drain delivered.
# The fix does not depend on which process eventually delivers, or whether
# it does before this cache's TTL would have expired on its own.
#
# DEGRADED STORE (M-1). A `list_objects` answer that could not really be
# read must never be published as "nothing": that would hide every one of a
# subject's real shares for a full TTL after the store came back, which a
# fresh per-request failure does not do (the next request just tries
# again). `cached_list_objects` calls `Authorizer.list_objects(...,
# strict=True)`, which raises `StoreUnavailable` (transport, 5xx) or
# `StoreRejected` (a 4xx / malformed body -- `fga.list_objects` grew the
# same `strict` flag the write methods and `object_tenant` already carry)
# instead of swallowing the failure into `[]`; the exception is caught
# here, nothing is published, and the miss is answered (this request only,
# never written back) so the caller sees no shares rather than a wrong
# store error. This replaces calling `Authorizer.reachable()` on every
# miss: `reachable()` probes a DIFFERENT endpoint (`/healthz`, not
# `/list-objects`), so it could say "up" while the actual read had just
# failed -- and did fail, silently, to `[]` -- costing a second HTTP round
# trip on every miss besides. A non-empty answer needs no probe at all: it
# already proves the store was reachable enough to answer.
#
# GENERATION (M-2). This cache's shared keys carry their OWN generation
# number (`_LIST_OBJECTS_GEN_KEY`, below), separate from the row cache's
# (`_GEN_KEY`) -- `_read_generation` / `_generation` / `_bump_generation`
# all take which key to use. They used to share one counter, on the theory
# that the only case needing a bulk turnover (`invalidate_all()`) already
# had a generation to bump -- true for THAT case, but it also meant a GROUP
# share or revoke, which cannot invalidate this cache precisely (below) and
# so turns over its WHOLE generation, was turning over the ROW cache's
# generation too: every ownership row cached anywhere in the deployment
# went cold on every group write, not just the reverse-index entries the
# group write could actually have changed. `invalidate_all()` still bumps
# BOTH generations (a bulk row write, or a test fixture wanting a clean
# slate, must still turn over everything); the group fallback in
# `invalidate_list_objects` now bumps only its own. An individual
# `invalidate_list_objects` call for a `user:` subject, in between, deletes
# its own key under the CURRENT backend generation for its own cache (read
# fresh, exactly like `_delete_shared` does for a row) -- it does not bump
# anything, so it can never touch another subject's entry either way.

_LIST_OBJECTS_CACHE_VERSION = "v1"


def _list_objects_key(gen: int, asset_type: str, subject: str) -> str:
    v = _LIST_OBJECTS_CACHE_VERSION
    return f"superset_ownership:list_objects:{v}:{gen}:{asset_type}:{subject}"


def _delete_shared_list_objects(
    cache: CacheBackend, rc: Optional[_RequestCache], key: tuple[str, str]
) -> None:
    """Same shape as `_delete_shared` (the row cache): delete under the
    generation the BACKEND holds -- not the one this request memoised,
    which another worker may have bumped since -- and, when the two
    differ, under the memoised one as well for any request still reading
    it. M-2: this cache's OWN generation (`_LIST_OBJECTS_GEN_KEY` /
    `rc.list_objects_generation`), not the row cache's."""
    asset_type, subject = key
    gen = _read_generation(cache, _LIST_OBJECTS_GEN_KEY)
    _shared_delete(cache, _list_objects_key(gen, asset_type, subject))
    if rc is not None:
        if rc.list_objects_generation is not None and rc.list_objects_generation != gen:
            _shared_delete(
                cache,
                _list_objects_key(rc.list_objects_generation, asset_type, subject),
            )
        rc.list_objects_generation = gen


def invalidate_list_objects(asset_type: str, subject: Optional[str]) -> None:
    """Forget one subject's cached `list_objects` answer for one asset type,
    in both layers. Called from TWO points -- see the module comment above
    for why both are needed: `outbox.write_tuple` / `delete_tuple` /
    `revoke_subject` (enqueue or inline write) and `outbox._apply` (actual
    delivery) -- so every share, unshare, transfer, visibility and claim
    write in this package is covered without a separate call site anywhere
    else. `outbox.purge_object` does NOT call this: it removes every
    relationship on an object rather than one subject's, so it has no
    single subject to target -- and does not need one, because the
    object's own `ownership_object` row is gone by the time a purge
    matters, and `owned_or_shared_rows`'s row scan (`list_rows`) already
    drops the object regardless of what a stale cached `shared_tuples`
    entry still names for some other subject. A blank or `None` subject
    (an unresolvable member reference) is a no-op: nothing could have been
    cached under it.

    A GROUP (or other non-`user:`) subject turns the WHOLE list_objects
    cache over (`_invalidate_all_list_objects()`) instead of deleting one
    key. This cache is only ever keyed by the subject a REQUEST actually
    asked about -- nothing calls `cached_list_objects` for a group
    reference -- so a targeted delete under `group:eng#member` would
    invalidate a key nothing reads and leave every MEMBER's own
    already-cached answer untouched, stale, for the rest of its TTL after
    this write changes what they can reach. Resolving membership to
    invalidate precisely would be its own directory or store round trip --
    exactly the cost this cache exists to avoid. Same fail-closed, cheap
    rule `has_pending_revocation` / `pending_revocations_for` already use
    for a non-user subject: treat it as reaching everyone, here by not
    leaving anyone's stale entry behind rather than by denying (that is
    those functions' job, and they do it independently of this cache,
    already fresh on every call). M-2: this bulk turnover bumps only the
    list_objects generation, not the row cache's -- a group write used to
    cold every cached ownership row deployment-wide as a side effect of
    this fallback; it no longer does.
    """
    if not subject:
        return
    if not subject.startswith("user:"):
        _invalidate_all_list_objects()
        return
    key = (asset_type, subject)
    rc = _request_cache()
    if rc is not None:
        rc.list_objects.pop(key, None)
        rc.list_objects_dirty.add(key)
        rc.list_objects_pending.add(key)
    cache = _shared_cache()
    if cache is not None:
        _delete_shared_list_objects(cache, rc, key)


def _invalidate_all_list_objects() -> None:
    """M-2: turn over ONLY the list_objects cache's own generation -- the
    fallback `invalidate_list_objects` takes for a non-`user:` (group)
    subject, which this cache cannot invalidate precisely (see that
    function's own docstring). Bumping just this cache's generation, not
    the row cache's, is what stops a group share or revoke from cooling
    every cached ownership row in the deployment as a side effect: before
    the two caches had separate generations, this fallback had no way to
    turn over only its own.

    `invalidate_all()` (a bulk row write, or a test fixture resetting a
    long-lived session -- see its own docstring) still bumps BOTH
    generations; this is the list_objects-only half of that, used on its
    own here.
    """
    rc = _request_cache()
    if rc is not None:
        rc.list_objects.clear()
        rc.list_objects_dirty.clear()
        rc.list_objects_all_dirty = True
        rc.list_objects_pending.clear()
        rc.list_objects_pending_all = True
    cache = _shared_cache()
    if cache is not None:
        _bump_generation(cache, rc, list_objects=True)


def cached_list_objects(asset_type: str, subject: str) -> list[str]:
    """`get_authorizer().list_objects(subject, "viewer", asset_type)`,
    cached request-locally and for `OWNERSHIP_LOOKUP_CACHE_TTL` seconds. See
    the module comment above for what is and is not cached and why.

    M-1: reads `strict=True`, so a store that could not actually be asked
    (transport failure, timeout, 5xx -- `StoreUnavailable`; a 4xx or a body
    that is not the JSON it promised -- `StoreRejected`) raises instead of
    silently answering `[]`. Caught here: the caller gets an empty answer
    for THIS call only (never written to the request-local or shared
    layer), so the next call -- this request or the next request -- tries
    the store again rather than trusting a published `[]` for the rest of
    the TTL. This replaces the old `Authorizer.reachable()` probe, which
    asked a DIFFERENT endpoint (`/healthz`) than the one that actually
    failed (`/list-objects`) and so could not tell a genuinely empty answer
    from a failed read that happened to land while the health probe still
    said "up" -- and cost a second round trip on every miss regardless.
    """
    key = (asset_type, subject)
    rc = _request_cache()
    if rc is not None and key in rc.list_objects:
        return rc.list_objects[key]

    cache = _shared_cache()
    dirty = rc is not None and rc.is_list_objects_dirty(key)
    publishable = cache is not None and not dirty
    skey = None
    if publishable:
        skey = _list_objects_key(
            _generation(cache, rc, list_objects=True), asset_type, subject
        )
        raw = _shared_get(cache, skey)
        if isinstance(raw, list):
            if rc is not None:
                rc.list_objects[key] = raw
            return raw

    authorizer = get_authorizer()
    try:
        tuples = list(
            authorizer.list_objects(subject, "viewer", asset_type, strict=True)
        )
    except fga.StoreError:
        # M-1: the store could not actually be read. Answer empty for this
        # call, but never publish it request-locally or to the shared
        # layer -- the next read, whenever it comes, must retry rather than
        # trust a failure as a real "nothing shared".
        logger.warning(
            "superset_ownership: list_objects(%s, viewer, %s) failed; "
            "answering empty for this call only, not caching it",
            subject,
            asset_type,
            exc_info=True,
        )
        return []
    if rc is not None:
        rc.list_objects[key] = tuples
    if publishable:
        _shared_set(cache, skey, tuples, lookup_cache_ttl())
    return tuples


def owned_or_shared_object_ids(asset_type: str, user_id: int) -> set[int]:
    """Ids the user owns, or that are shared with them via OpenFGA.

    Excludes public rows -- Superset's own default query filters already
    surface public dashboards/charts; this module has nothing to add there.
    A share with an undelivered revocation is excluded too (issue #84); see
    `owned_or_shared_rows`.
    """
    return set(owned_or_shared_rows(asset_type, user_id))


def owned_or_shared_rows(asset_type: str, user_id: int) -> dict[int, OwnershipRow]:
    """The rows behind `owned_or_shared_object_ids`, keyed by object id.

    One scan of the asset type's rows. The candidates it returns are also
    recorded request-locally, so the list filter (and any per-object lookup
    later in the same request) does not read them a second time -- and
    nothing is published to the shared layer from here.

    REVOCATION WINDOW (issue #84). `list_objects` answers from the store's
    reverse index, which lags the outbox exactly like `check` does: a
    revocation queued but not yet drained still has its tuple in the store,
    so the id it names would otherwise stay in this result -- and therefore
    in the list, and (FAB applies the list base filter to a single-item GET
    too) in a direct GET -- until the drain caught up, even though the same
    request's access DECISION (`hooks.raise_for_access_bypass`, via
    `outbox.has_pending_revocation`) already denies it. Two bounded, indexed
    reads -- `outbox.pending_revocations_for`, the same rule the per-object
    gate applies, but the whole outbox in one pass instead of one query per
    candidate (PR #94 review, H-1: the carve-out resolves `object_id` from
    `rows`, the scan this function already did, rather than re-reading
    `ownership_object`) -- close it here too: any candidate with an
    undelivered revocation for this subject is dropped before this function
    returns, so a revoked share disappears from a list and from
    single-object GET the moment it is queued, not seconds later when the
    drain delivers it. NOT published anywhere: it must be fresh on every
    call, the same reason `has_pending_revocation` is never cached.

    PAID ONLY WHEN IT CAN MATTER (M-1). `pending_revocations_for` can only
    ever remove a SHARED candidate -- the owner branch below never looks at
    `revoked_refs` -- so the call is skipped whenever this request has no
    shared candidate to subtract from: an owner-only list/GET, or any
    subject the store lists nothing for. NOT the same test as "`shared_uuids`
    is empty": the model has `viewer` include `editor` include `owner`
    (`model/ownership.fga`), so `list_objects(subject, "viewer", asset_type)`
    also lists the caller's OWN owned objects once their `owner` tuple has
    reached the store -- `shared_uuids` alone is non-empty for an owner with
    no shares at all. What decides the call is whether any row this scan
    found is BOTH governed and not owned by this caller AND named in
    `shared_uuids` -- an actual shared candidate, the only kind the
    subtraction can ever remove. The cache in front of `list_objects`
    (issue #82, `cached_list_objects` above) wraps `shared_tuples` only,
    never this function's `reachable` result: the subtraction below runs
    fresh on every call regardless of whether the store read behind
    `shared_tuples` was served from that cache.

    The OWNER row is never subtracted: an owner is never a share, so a
    `revoke_subject` / `delete` row can never name them for this object, and
    `has_pending_revocation` does not gate the owner fast path either.
    """
    rows = list_rows(asset_type)
    # Reverse lookup must use Ivanti's member GUID, exactly as the per-object
    # check does. Asking with Superset's integer id silently returns nothing.
    from superset_ownership.identity import member_ref

    subject = member_ref(user_id)
    # Cached: request-locally, and for OWNERSHIP_LOOKUP_CACHE_TTL seconds
    # across requests (issue #82; see `cached_list_objects` above). This is
    # the list base filter's own store call, never the access decision's.
    shared_tuples = cached_list_objects(asset_type, subject) if subject else []
    shared_uuids = {t.split(":", 1)[1] for t in shared_tuples if ":" in t}
    shared_rows = [
        row
        for row in rows
        # H2 (review round 1, issue #93): `private` (but NOT a parked
        # `disabled:private` row -- disable() is deliberately not a denial,
        # see raise_for_access_bypass's own docstring) excluded here too,
        # not only in the `reachable` loop below -- a private candidate can
        # never survive that loop, so it is never worth a
        # `pending_revocations_for` probe (M-1's own "paid only when it can
        # matter" rule, extended to the row this PR's hard deny also
        # refuses regardless of revocation status).
        if row.visibility != "public"
        and row.visibility != "private"
        and row.owner_user_id != user_id
        and row.object_uuid
        and row.object_uuid in shared_uuids
    ]

    revoked_refs: set[str] = set()
    if subject and shared_rows:
        object_id_by_uuid: dict[str, int] = {
            row.object_uuid: row.object_id for row in shared_rows if row.object_uuid
        }
        revoked_refs = outbox.pending_revocations_for(
            subject, asset_type, object_id_by_uuid
        )
        if revoked_refs:
            # H-1 (review round 2, PR #100). `cached_list_objects` above may
            # have just published -- or may already hold -- a shared-layer
            # entry for (asset_type, subject) that answers from BEFORE this
            # revocation reaches the store: correct at the moment it was
            # written, but on a per-process cache backend (SimpleCache --
            # this stack's shape: flask web + Celery drain in a separate
            # process) `outbox._apply`'s post-delivery invalidation runs in
            # the DRAIN's process and never reaches this one, so that entry
            # would otherwise go on answering the pre-revocation list for up
            # to a full TTL PAST the point the drain delivers -- and FAB
            # applies this same list base filter to a single-object GET, so
            # the exposure is not a stale list row, it is `GET
            # /api/v1/chart/<pk>` answering 200 (no `raise_for_access` gate
            # on that route; `ChartFilter` -- this function -- is the gate)
            # for an object this subject no longer has access to.
            #
            # Never leave a list-objects answer published for a subject with
            # an undelivered revocation: drop the entry every time the
            # subtraction actually found one, independent of whether the
            # drain's own invalidation ever reaches this process.
            # `invalidate_list_objects` also marks the key dirty for the
            # rest of THIS request, so a later `cached_list_objects` call
            # this same request re-reads the store rather than the entry
            # just dropped. Idempotent and cheap when there is nothing to
            # drop (the enqueue-time invalidation already ran, or nothing
            # was ever published this TTL window).
            invalidate_list_objects(asset_type, subject)

    reachable: dict[int, OwnershipRow] = {}
    for row in rows:
        if row.visibility == "public":
            continue
        if row.owner_user_id == user_id:
            reachable[row.object_id] = row
        elif row.visibility == "private":
            # H2 (review round 1, issue #93): a `private` row confers
            # nothing on a non-owner -- same rule as the read gate's hard
            # deny -- so it must not be listed even when a stray or
            # undrained mirror row still names one. Without this, the list
            # filter and the read gate disagreed about the same object: for
            # the whole drain window after `-> private` the previously
            # shared user still saw it in their list (and GET on it still
            # answered 200), and a share planted directly on a private
            # object (never through the `shared -> private` transition) was
            # listed forever.
            continue
        elif row.object_uuid and row.object_uuid in shared_uuids:
            if f"{asset_type}:{row.object_uuid}" in revoked_refs:
                continue
            reachable[row.object_id] = row
    _seed_request_cache(asset_type, reachable.values())
    return reachable


def get_user_info(user_id: Optional[int]) -> Optional[dict[str, Any]]:
    if user_id is None:
        return None
    from superset import security_manager

    from superset_ownership.identity import display_name, resolve_member_guid

    db = _db()
    user = db.session.get(security_manager.user_model, user_id)
    if not user:
        return None
    # L-2: both facts go through the Identity seam, not an inline formula
    # and `user.username` -- under an override that relocates the GUID
    # off the username (H-3), `guid` here used to disagree with the value
    # the picker itself uses (`resolve_member_guid`), and the display name
    # bypassed a plugged `Identity.display_name` entirely.
    return {
        "id": user.id,
        "name": display_name(user),
        "email": getattr(user, "email", None),
        "guid": resolve_member_guid(user),
    }


# --- shares (local mirror of the OpenFGA tuples, for GET responses) ---


def list_shares(asset_type: str, object_id: int) -> list[dict[str, Any]]:
    db = _db()
    rows = (
        db.session.execute(
            select(ownership_share).where(
                ownership_share.c.asset_type == asset_type,
                ownership_share.c.object_id == object_id,
            )
        )
        .mappings()
        .all()
    )
    result = []
    seen = set()
    for row in rows:
        seen.add(row["subject"])
        result.append(
            {
                "subject": row["subject"],
                "name": subject_display_name(row["subject"]),
                "role": row["role"],
            }
        )

    # The store is authoritative for shares, so a grant it holds and the
    # mirror does not is still a live grant -- and one the owner could not
    # see, and so could not revoke. Merged in and marked, rather than hidden.
    obj_row = lookup(asset_type, object_id)
    if obj_row is not None and obj_row.object_uuid:
        try:
            for t in get_authorizer().list_grants(
                f"{asset_type}:{obj_row.object_uuid}"
            ):
                if t["user"] in seen:
                    continue
                seen.add(t["user"])
                result.append(
                    {
                        "subject": t["user"],
                        "name": subject_display_name(t["user"]),
                        "role": t["relation"],
                        "unmirrored": True,
                    }
                )
        except Exception:
            logger.exception(
                "superset_ownership: could not read live grants for %s %s",
                asset_type,
                object_id,
            )
    return result


# Relations that satisfy a requested relation. The OpenFGA model expresses
# this as `viewer: [user] or editor`, so an editor can read there. The local
# backend compared roles for exact equality, which meant a share created from
# the UI -- which grants `editor` to a person -- satisfied no read check at
# all: the row existed, the API answered 200, the drawer listed the share,
# and the grantee could not open the object. The two backends have to agree
# on the model, not just on the storage.
ROLE_IMPLIES: dict[str, frozenset[str]] = {
    "viewer": frozenset({"viewer", "editor"}),
    "editor": frozenset({"editor"}),
}


def roles_satisfying(role: str) -> frozenset[str]:
    return ROLE_IMPLIES.get(role, frozenset({role}))


def list_share_rows(asset_type: str, object_id: int) -> list[dict[str, Any]]:
    """The share rows on an object, as stored. No name resolution.

    For the flush guard, which needs subjects and roles and nothing else, on
    every write to a governed object.
    """
    return [
        dict(r)
        for r in _db()
        .session.execute(
            select(ownership_share).where(
                ownership_share.c.asset_type == asset_type,
                ownership_share.c.object_id == object_id,
            )
        )
        .mappings()
        .all()
    ]


def list_share_rows_bulk(
    asset_type: str, object_ids: Iterable[int]
) -> dict[int, list[dict[str, Any]]]:
    """`list_share_rows` for a whole page of objects in ONE query (M-3):
    the list-page's per-row `OWNERSHIP_CAN_MANAGE` object_state used to call
    `list_shares` (a local-mirror query PLUS a store `list_grants` round
    trip for the unmirrored-grant merge) once per row -- N+1 both ways for
    a 100-row page. This reads the local mirror for every id on the page at
    once (an `IN (...)`) and groups by `object_id`; it does NOT merge
    unmirrored store grants (documented in §4.5.2: the list page's
    `object_state["shares"]` is the local mirror only, unlike the detail
    endpoint's `list_shares`, which is exact for one object because it is
    only ever called for one).
    """
    ids = list(object_ids)
    if not ids:
        return {}
    by_id: dict[int, list[dict[str, Any]]] = {oid: [] for oid in ids}
    rows = (
        _db()
        .session.execute(
            select(ownership_share).where(
                ownership_share.c.asset_type == asset_type,
                ownership_share.c.object_id.in_(ids),
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        by_id.setdefault(row["object_id"], []).append(
            {
                "subject": row["subject"],
                "name": subject_display_name(row["subject"]),
                "role": row["role"],
            }
        )
    return by_id


def has_share_row(asset_type: str, object_id: int, subject: str, role: str) -> bool:
    """Does this exact share row exist? One indexed lookup, no name resolution.

    The local authorizer's check() runs on the read path for every private or
    shared object. Answering it through list_shares() would resolve a display
    name for every row on the object -- a user lookup per row, per access
    check -- to compare two strings.
    """
    db = _db()
    return (
        db.session.execute(
            select(ownership_share.c.id).where(
                ownership_share.c.asset_type == asset_type,
                ownership_share.c.object_id == object_id,
                ownership_share.c.subject == subject,
                ownership_share.c.role.in_(roles_satisfying(role)),
            )
        ).first()
        is not None
    )


def subject_display_name(subject: str) -> str:
    """A human name for a subject reference, whichever way it is keyed.

    Subjects arrive as "user:<member guid>" under Ivanti's identity model and
    as "user:<superset id>" on a standalone instance. This used to try
    int() and give up on the ValueError, so every Ivanti-shaped subject
    rendered in the UI as a raw GUID.

    Groups have no Superset record at all -- their names live only in the
    authorization store -- so the tenant is taken out of the group id (from
    whichever end OWNERSHIP_GROUP_ID_FORMAT puts it) and the remainder is
    shown.

    R2-M2: a username-only lookup only finds the account under the plain
    default identity, where a GUID subject and a username are the same
    string. Under a `member_guid` hook that relocates the GUID off the
    username (H-1), the same subject needs the reverse hook -- through the
    same public facades `_validate_user_subject` uses (`api.py`) -- with
    the same forward-mapping check (N-2), so an inconsistent hook cannot
    borrow another account's display name; only when neither the username
    lookup nor the hooked reverse lookup resolves an account does this fall
    back to the raw GUID.
    """
    from superset import security_manager

    from superset_ownership.identity import (
        display_name,
        group_display_name,
        resolve_member_guid,
        subject_ids_match,
        user_for_member_guid,
    )

    kind, _, rest = subject.partition(":")
    ref = rest.split("#", 1)[0]

    if kind == "group":
        # "dashboard_designer_<tenant guid>" -> "dashboard designer"
        return group_display_name(subject).replace("_", " ") or subject

    if kind != "user":
        return subject

    if ref.isdigit():
        info = get_user_info(int(ref))
        return info["name"] if info else subject

    from superset_ownership.identity import member_guid_of_subject_id

    user = security_manager.find_user(username=member_guid_of_subject_id(ref) or ref)
    if user is None:
        # R3-M1: on the class-seam path the reverse lookup is the plug-in's
        # own code and can raise anything (the hook path already fails
        # closed inside the registry). A display name is never worth a 500,
        # and the exception text may carry a credential, so log the class
        # name only and render the raw subject.
        try:
            candidate = user_for_member_guid(ref)
            if candidate is not None:
                if subject_ids_match(resolve_member_guid(candidate), ref):
                    user = candidate
        except Exception as exc:  # noqa: BLE001 - identity seam (I-2)
            logger.warning(
                "superset_ownership: identity plug-in raised %s resolving a "
                "display name for %s; showing the raw subject",
                type(exc).__name__,
                subject,
            )
            return subject
    if user is None:
        return subject
    return display_name(user)


class ShareTargetGoneError(LookupError):
    """The share's object row was deleted between the route's lookup and the
    insert: the share -> object foreign key (revision 0003) refused the row.
    The route answers 404; nothing was written and nothing is queued."""

    def __init__(self, asset_type: str, object_id: int, constraint: Optional[str]):
        super().__init__(
            f"{asset_type} {object_id} has no ownership row (refused by "
            f"{constraint or 'a foreign key'}); the object was deleted"
        )
        self.asset_type = asset_type
        self.object_id = object_id
        self.constraint = constraint


def _integrity_kind(exc: Exception) -> tuple[str, Optional[str]]:
    """("unique" | "foreign_key" | "unknown", constraint name or None) for an
    IntegrityError, from what the driver says: psycopg2's SQLSTATE and
    `diag.constraint_name`; SQLite's message ("UNIQUE constraint failed:
    t.col" / "FOREIGN KEY constraint failed", which names no constraint)."""
    orig = getattr(exc, "orig", None)
    code = getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)
    diag = getattr(orig, "diag", None)
    name = getattr(diag, "constraint_name", None) if diag is not None else None
    if code == "23505":
        return "unique", name
    if code == "23503":
        return "foreign_key", name
    message = str(orig or exc)
    if "UNIQUE constraint failed" in message:
        detail = message.split("UNIQUE constraint failed:", 1)[1].strip()
        return "unique", detail or None
    if "FOREIGN KEY constraint failed" in message:
        return "foreign_key", None
    return "unknown", name


def _insert_share_row(
    db: Any, asset_type: str, object_id: int, subject: str, role: str
) -> None:
    """Insert a share row inside a savepoint, and tell the two ways the
    insert can lose apart.

    Two administrators sharing the same object with the same subject at
    once used to raise UniqueViolation out of here and surface as a 500.
    The unique constraint is the arbiter; losing THAT race means the row is
    already there, which is the state wanted, so its role is updated.

    Since revision 0003 the insert can also be refused by the share ->
    object foreign key: the object row was deleted (a concurrent hard
    delete) between the route's lookup and this insert. That used to be
    swallowed as the unique race -- the fallback UPDATE matched nothing,
    the caller returned True and a store tuple was queued for an object
    with no row. The driver's constraint name is read where it gives one
    (PostgreSQL), the message where it does not (SQLite), and the row is
    re-read as the arbiter for anything else: a row present is the unique
    race whatever the driver said; no row after a unique violation means
    the row was deleted under the request and is ShareTargetGoneError; no
    row after an error the driver could not classify (a NOT NULL or CHECK
    on a dialect that names neither) is neither race and is re-raised as
    the IntegrityError it is, logged, rather than labelled a deleted
    object."""
    from sqlalchemy.exc import IntegrityError

    savepoint = db.session.begin_nested()
    try:
        db.session.execute(
            insert(ownership_share).values(
                asset_type=asset_type,
                object_id=object_id,
                subject=subject,
                role=role,
            )
        )
        savepoint.commit()
        return
    except IntegrityError as exc:
        savepoint.rollback()
        kind, constraint = _integrity_kind(exc)
        if kind == "foreign_key":
            raise ShareTargetGoneError(asset_type, object_id, constraint) from exc
        present = db.session.execute(
            select(ownership_share.c.id).where(
                ownership_share.c.asset_type == asset_type,
                ownership_share.c.object_id == object_id,
                ownership_share.c.subject == subject,
            )
        ).first()
        if present is None and kind == "unique":
            # The row that collided is gone: deleted under the request.
            raise ShareTargetGoneError(asset_type, object_id, constraint) from exc
        if present is None:
            # Neither race: nothing collided and no foreign key refused it.
            # A genuine defect (a column constraint, a schema drift) must
            # not be read as a deleted object.
            logger.error(
                "superset_ownership: inserting the share %s %s -> %s failed with an "
                "IntegrityError that is neither the unique race nor the object "
                "foreign key, and no row is present: %s",
                asset_type,
                object_id,
                subject,
                str(getattr(exc, "orig", exc))[:300],
            )
            raise
    db.session.execute(
        update(ownership_share)
        .where(
            ownership_share.c.asset_type == asset_type,
            ownership_share.c.object_id == object_id,
            ownership_share.c.subject == subject,
        )
        .values(role=role)
    )


def add_share(
    asset_type: str, object_id: int, object_uuid: str, subject: str, role: str
) -> bool:
    """Upsert the share row and queue the store write. Raises
    ShareTargetGoneError when the object row vanished under the request (the
    route answers 404); returns False only when an inline store write is
    refused (the route answers 502)."""
    db = _db()
    existing = (
        db.session.execute(
            select(ownership_share).where(
                ownership_share.c.asset_type == asset_type,
                ownership_share.c.object_id == object_id,
                ownership_share.c.subject == subject,
            )
        )
        .mappings()
        .first()
    )
    if existing:
        prior_role = existing["role"]
        db.session.execute(
            update(ownership_share)
            .where(
                ownership_share.c.asset_type == asset_type,
                ownership_share.c.object_id == object_id,
                ownership_share.c.subject == subject,
            )
            .values(role=role)
        )
        # Relations are separate tuples, not a single mutable field: writing
        # `viewer` does not retract `editor`. Without this delete, demoting an
        # editor to a viewer left the editor tuple in place and the demotion
        # was a no-op everywhere the decision is actually made.
        if prior_role and prior_role != role:
            outbox.delete_tuple(subject, prior_role, f"{asset_type}:{object_uuid}")
    else:
        _insert_share_row(db, asset_type, object_id, subject, role)
    # The row itself is untouched by a share, but the editors resolver's
    # derived entry for the object is not; invalidate drops both.
    invalidate(asset_type, object_id)
    ok = outbox.write_tuple(subject, role, f"{asset_type}:{object_uuid}")
    if not ok:
        # Reachable only with the outbox DISABLED (enabled, the write is
        # queued and always "succeeds" here). The route rolls the mirror
        # change back and answers 502.
        logger.warning(
            "superset_ownership: FGA write failed for share %s %s %s:%s -- rolling back",
            subject,
            role,
            asset_type,
            object_uuid,
        )
    return ok


def remove_share(
    asset_type: str, object_id: int, object_uuid: str, subject: str
) -> bool:
    """Revoke every relation this subject holds on the object.

    Inline mode returns False when there was nothing to revoke in EITHER
    store, so the route can answer 404 rather than reporting a revocation
    that never happened. It used to default the relation to "viewer" when no
    row matched and delete that -- which, combined with a delete-miss
    counting as success, meant DELETE on a misspelled or nonexistent subject
    answered 200 and emitted a share_removed audit event while the real
    grant stayed in force. With the outbox on, "nothing to revoke" cannot be
    known without a store read, so the revocation is always queued.
    """
    db = _db()
    roles = [
        r[0]
        for r in db.session.execute(
            select(ownership_share.c.role).where(
                ownership_share.c.asset_type == asset_type,
                ownership_share.c.object_id == object_id,
                ownership_share.c.subject == subject,
            )
        ).all()
    ]
    if outbox.enabled():
        # Off the request path entirely: delete the mirror rows and enqueue a
        # `revoke_subject`, which at drain time reads EVERY relation the
        # subject holds in the store -- including any the mirror never learned
        # about -- and removes them. No store read here, so revoking works
        # while the store is unreachable, and the read path already denies the
        # subject while the revocation is undelivered (has_pending_revocation).
        # Enqueued even when the mirror holds no row: the mirror is not
        # authoritative for shares, and a grant it has lost is still a grant
        # until the drain reads the store and removes it. (`reconcile` does
        # not delete share tuples without a mirror row, so this is the only
        # path that revokes such a stray.)
        if roles:
            db.session.execute(
                delete(ownership_share).where(
                    ownership_share.c.asset_type == asset_type,
                    ownership_share.c.object_id == object_id,
                    ownership_share.c.subject == subject,
                )
            )
        invalidate(asset_type, object_id)
        return outbox.revoke_subject(asset_type, object_uuid, subject)

    # Inline mode (outbox disabled): every relation the subject holds, from
    # BOTH stores. The mirror can hold `editor` while the store also holds a
    # `viewer` the mirror never learned about; revoking only the mirror's
    # roles left the second grant in force and reported success.
    store_roles = get_authorizer().list_relations(
        subject, f"{asset_type}:{object_uuid}"
    )
    roles = sorted(set(roles) | set(store_roles))
    if not roles:
        return False
    if set(store_roles) - set(
        r[0]
        for r in db.session.execute(
            select(ownership_share.c.role).where(
                ownership_share.c.asset_type == asset_type,
                ownership_share.c.object_id == object_id,
                ownership_share.c.subject == subject,
            )
        ).all()
    ):
        logger.warning(
            "superset_ownership: revoking %s on %s:%s including relations the "
            "local mirror did not have",
            subject,
            asset_type,
            object_uuid,
        )

    db.session.execute(
        delete(ownership_share).where(
            ownership_share.c.asset_type == asset_type,
            ownership_share.c.object_id == object_id,
            ownership_share.c.subject == subject,
        )
    )
    invalidate(asset_type, object_id)
    ok = True
    for role in roles:
        if not outbox.delete_tuple(subject, role, f"{asset_type}:{object_uuid}"):
            ok = False
    return ok


# --- helpers used by the local authorizer backend -------------------------
# The share table is keyed on the integer id; authorizer object references
# arrive as "<asset_type>:<uuid>", so these translate between the two and
# expose the raw row operations without the OpenFGA sync that add_share /
# remove_share perform.


def lookup_by_uuid(asset_type: str, object_uuid: str) -> Optional[OwnershipRow]:
    """The row a store reference names, or None -- INCLUDING when more than
    one row could answer to it.

    `ownership_object.object_uuid` carries no unique constraint, and a
    `uuid_divergence` (a row whose uuid the live object no longer has) is
    exactly the state in which two rows hold one uuid. This is the READ side
    of the bystander class the write sides were fixed for, and on the
    default backend it is the one that decides access: `LocalAuthorizer`
    resolves every object reference through here, so an unordered `.first()`
    handed the reference to whichever row the database felt like returning
    (review round 5). Measured on two rows sharing a uuid: the read gate
    allowed a user on an object never shared with them; the drain wrote a
    share row onto a bystander, purged a bystander's shares, and -- worst --
    delivered a REVOCATION to the bystander, so the subject it was meant to
    cut off kept their grant while the outbox row was marked delivered and
    `has_pending_revocation` stopped denying it.

    Ambiguity is therefore answered as "no row", not as "a row": every
    caller already handles None, and on this path None denies, refuses, or
    skips. `check` reports the divergence that caused it
    (`uuid_divergence`), and `reconcile --write` repairs it.
    """
    rows = (
        _db()
        .session.execute(
            select(ownership_object)
            .where(
                ownership_object.c.asset_type == asset_type,
                ownership_object.c.object_uuid == object_uuid,
            )
            .limit(2)
        )
        .mappings()
        .all()
    )
    if len(rows) > 1:
        logger.error(
            "superset_ownership: %s:%s names %d ownership rows; refusing to guess which "
            "one it is. Run `superset ownership check` (uuid_divergence) and "
            "`reconcile --write`",
            asset_type,
            object_uuid,
            len(rows),
        )
        return None
    return OwnershipRow(**rows[0]) if rows else None


def add_share_row(asset_type: str, object_id: int, subject: str, role: str) -> None:
    """Upsert a share row on the REQUEST's session.

    Deliberately not its own transaction: callers already hold row locks on
    this table within the request, so a second connection would deadlock
    against them.
    """
    session = _db().session
    session.execute(
        delete(ownership_share).where(
            ownership_share.c.asset_type == asset_type,
            ownership_share.c.object_id == object_id,
            ownership_share.c.subject == subject,
        )
    )
    session.execute(
        insert(ownership_share).values(
            asset_type=asset_type, object_id=object_id, subject=subject, role=role
        )
    )
    invalidate(asset_type, object_id)


def remove_share_row(
    asset_type: str, object_id: int, subject: str, role: Optional[str] = None
) -> None:
    """Delete the subject's share row; with `role`, only if it holds that
    role. The local authorizer's delete_tuple passes the relation, so a role
    change (delete editor, write viewer) delivered through the outbox does
    not delete the freshly re-roled row and re-create it a commit later."""
    stmt = delete(ownership_share).where(
        ownership_share.c.asset_type == asset_type,
        ownership_share.c.object_id == object_id,
        ownership_share.c.subject == subject,
    )
    if role is not None:
        stmt = stmt.where(ownership_share.c.role == role)
    _db().session.execute(stmt)
    invalidate(asset_type, object_id)


def uuids_shared_with(asset_type: str, subject: str, role: str) -> list[str]:
    """Object uuids the subject holds `role` on, via the local share table."""
    rows = (
        _db()
        .session.execute(
            select(ownership_object).where(
                ownership_object.c.asset_type == asset_type,
                ownership_object.c.object_id.in_(
                    select(ownership_share.c.object_id).where(
                        ownership_share.c.asset_type == asset_type,
                        ownership_share.c.subject == subject,
                        # Same implication the per-object check applies. Comparing
                        # for equality here meant an editor passed the per-object
                        # check and was still absent from the list query -- the
                        # object was openable by direct URL and invisible in the UI.
                        ownership_share.c.role.in_(roles_satisfying(role)),
                    )
                ),
            )
        )
        .mappings()
        .all()
    )
    return [r["object_uuid"] for r in rows if r["object_uuid"]]


def sync_native_editors(
    asset: Any,
    row: OwnershipRow,
    *,
    previous_owner_id: Optional[int] = None,
    create_missing: bool = True,
    strict: bool = False,
) -> bool:
    """Re-derive `asset.editors` -- the NATIVE editors collection Superset's
    own `is_editor` unions with EXTRA_EDITORS_RESOLVER's dynamic answer
    (hooks.extra_editors) -- from the ownership model.

    ISSUE_80: a stock create (`populate_subjects`) records the creator as a
    native editor, and nothing corrected that collection when ownership
    moved on: `is_editor` consults it directly (`resource.editors`), so a
    transferred-away previous owner stayed a native editor forever, no
    matter what the ownership row or the dynamic resolver said. This is the
    one place that repairs it, called wherever the ownership row's owner
    changes -- transfer, claim, backfill attribution, `_set_asset_visibility`
    adopting an ownerless object -- and from the flush guard, which already
    derives editors for every OTHER write to a governed object.

    REVIEW H-1/H-2 (round 1): the native collection holds the OWNER ONLY.
    An earlier version of this helper also materialised every `editor`
    share holder into `asset.editors`; `is_editor` trusts this collection
    with no further check, so that let a share-holder in without the
    per-user dataset symmetry `hooks.editor_subject_ids` applies for the
    dynamic resolver (H-1: a grant-less share-holder was admitted the next
    time anyone wrote the object), and a later share *revocation* had
    nothing here to undo, so a revoked editor kept native edit rights
    indefinitely (H-2). Editor shares stay dynamic, through
    EXTRA_EDITORS_RESOLVER (`hooks.extra_editors`) -- see guard.py's comment
    on the invariant this enforces for why that is the only place a share
    can be evaluated correctly.

    The invariant: the owner, and only the owner, belongs in the
    collection. What happens to everyone else depends on `strict`:

      - strict=False (the default; every ownership-change call site above):
        only `previous_owner_id` -- named by the caller, who just changed
        the owner -- is removed. Every other pre-existing native editor (an
        admin-added one, say) is left alone: this call is not a general
        audit of the collection, only of the ownership change that
        triggered it. "Do not strip unrelated editors."

      - strict=True (the flush guard, defending every OTHER write to a
        governed object -- see guard.py's `_before_flush`): anything that
        is not the owner is removed. `previous_owner_id` is not needed for
        this and is ignored; the guard does not track "previous" owners,
        only the invariant a governed object's editors must hold at every
        flush -- the owner alone, same as main enforced before this module
        grew a transfer/claim route at all.

    `create_missing` controls whether a Subject that does not exist yet is
    created (`get_or_create_user_subject`) or only looked up
    (`get_user_subject`, never flushing) -- pass False from inside a flush
    event, where creating one raises "Session is already flushing" (see
    guard._owner_subject for the same constraint, before it was replaced by
    this helper). The route handlers that call this run outside any flush
    and may leave it True.

    Returns True if the collection actually changed.
    """
    editors = getattr(asset, "editors", None)
    if editors is None:
        return False

    from superset.subjects.utils import get_or_create_user_subject, get_user_subject

    wanted_user_ids: set[int] = set()
    if row.owner_user_id is not None:
        wanted_user_ids.add(row.owner_user_id)

    existing = list(editors)
    kept = []
    changed = False
    for subject in existing:
        uid = getattr(subject, "user_id", None)
        if uid is not None and uid in wanted_user_ids:
            kept.append(subject)
        elif strict:
            changed = True
        elif (
            uid is not None
            and previous_owner_id is not None
            and uid == previous_owner_id
        ):
            changed = True
        else:
            # Not the owner, and (outside strict mode) not the one editor
            # this call was asked to remove: none of this call's business.
            kept.append(subject)

    kept_user_ids = {getattr(s, "user_id", None) for s in kept}
    missing_user_ids = [uid for uid in wanted_user_ids if uid not in kept_user_ids]

    added = []
    for uid in missing_user_ids:
        subject = (
            get_or_create_user_subject(uid) if create_missing else get_user_subject(uid)
        )
        # A Subject row is looked up, never force-created, in strict/flush
        # mode; a missing one (no Subject yet for the owner) is skipped
        # rather than blocking the rest of the sync.
        if subject is not None and subject not in kept:
            added.append(subject)

    if not changed and not added:
        return False

    for subject in existing:
        if subject not in kept:
            editors.remove(subject)
    for subject in added:
        editors.append(subject)
    return True

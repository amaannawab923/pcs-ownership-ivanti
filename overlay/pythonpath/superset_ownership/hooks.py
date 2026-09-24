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
The three Superset extension hooks this module wires up. See
superset_config_docker_light.py for the wiring, and each function's
docstring for the exact contract Superset calls it under.
"""

from __future__ import annotations

import logging
import re
from superset_ownership.authz import get_authorizer
from typing import Any, Optional

from superset_ownership import outbox, sentinel, service
from superset_ownership.fga import StoreError

logger = logging.getLogger(__name__)


def _asset_uuid(model: Any) -> Optional[str]:
    u = getattr(model, "uuid", None)
    return str(u) if u else None


def govern(
    model: Any, asset_type: str, owner_user_id: Optional[int], emit: bool = True
) -> None:
    """Make a freshly created object governed. ONE implementation.

    Called by the create-command hook and by the flush guard for everything
    the command hook does not see (copies, imports, anything else that
    inserts a Dashboard or Slice). The guard used to do a third of this --
    the row and the sentinel -- so a copied dashboard had no owner tuple, no
    tenant tuple and no object_created event: a tenant administrator could not
    manage it, a purge could not find it, and the audit trail never saw it
    created. Same object, governed two different ways, depending on which
    route made it.

      - the ownership_object row, private, owned by the creator
      - the sentinel in model.viewers (denial)
      - the owner tuple and the tenant tuple in the authorization store
      - the object_created audit event

    `owner_user_id` may be None for an object created with no request user
    (a background job, a CLI command). Such an object is recorded ownerless
    and PUBLIC, not private: born denied to everyone, unshareable, and
    rescuable only by a Superset admin is a worse default than ungoverned,
    and it is what `load-examples` would produce on a fresh install.
    """
    if service.disable_in_progress():
        # The feature is being turned off; do not arm a new object into a
        # rollback in progress. It stays ungoverned (public), which is the
        # correct state for an object created while ownership is disabled.
        logger.info(
            "superset_ownership: disable in progress; %s left ungoverned", asset_type
        )
        return

    object_uuid = _asset_uuid(model)
    visibility = "private" if owner_user_id is not None else "public"

    # Which tenant the object belongs to, recorded at creation from its
    # creator's tenant. A tenant administrator's authority is scoped to
    # their own tenant, so an object with no tenant tuple is one no
    # administrator can act on -- and, under OWNERSHIP_PUBLIC_SCOPE=tenant,
    # one no tenant member can read once public (fail closed). Resolved
    # before the row is written so the row carries it from its first
    # version.
    tenant_guid = None
    if owner_user_id is not None:
        from superset import security_manager
        from superset_ownership.identity import resolve_tenant_guid

        owner = security_manager.get_user_by_id(owner_user_id)
        tenant_guid = resolve_tenant_guid(owner) if owner is not None else None

    # A new object has no ownership row yet. upsert_ownership takes the
    # per-object lock all the same: on Postgres the advisory half of it
    # serialises a second governing of the same object before either
    # INSERTs, and elsewhere the unique key on (asset_type, object_id)
    # settles the crossed INSERT (the loser updates the winner's row).
    service.upsert_ownership(
        asset_type=asset_type,
        object_id=model.id,
        object_uuid=object_uuid,
        owner_user_id=owner_user_id,
        visibility=visibility,
        tenant_guid=tenant_guid,
    )
    if visibility == "private" or service.public_is_tenant_scoped():
        # Public is governed too under tenant scope: the sentinel closes
        # Superset's own "anyone with the dataset grant" rule so the read
        # gate can apply "members of this tenant" instead.
        sentinel.add_sentinel(model)

    if owner_user_id is not None and object_uuid:
        from superset_ownership.identity import member_ref

        subject = member_ref(owner_user_id)
        if subject:
            outbox.write_tuple(subject, "owner", f"{asset_type}:{object_uuid}")
        if tenant_guid:
            outbox.set_object_tenant(
                asset_type, object_uuid, tenant_guid, object_id=model.id
            )

    if emit:
        from superset import security_manager
        from superset_ownership import audit

        audit.emit(
            audit.OBJECT_CREATED,
            actor=security_manager.get_user_by_id(owner_user_id)
            if owner_user_id
            else None,
            asset_type=asset_type,
            object_id=model.id,
            object_uuid=object_uuid,
            after={"visibility": visibility, "owner_user_id": owner_user_id},
        )
    logger.info(
        "superset_ownership: %s %s created %s, owner=%s",
        asset_type,
        model.id,
        visibility,
        owner_user_id,
    )


def after_asset_create(model: Any, asset_type: str) -> None:
    """AFTER_ASSET_CREATE: (model, "chart"|"dashboard"). See govern()."""
    from superset.utils.core import get_user_id

    db = service._db()
    # The create command calls this hook mid-transaction, before its own
    # @transaction() decorator flushes/commits -- model.id and model.uuid
    # (server-side defaults) aren't populated yet without an explicit flush.
    db.session.flush()
    govern(model, asset_type, get_user_id())
    # Flush (not commit) -- this hook runs inside the create command's own
    # @transaction()-managed session; committing here would step on that
    # decorator's commit/rollback handling.
    db.session.flush()
    # govern() already invalidated through upsert_ownership; this is the
    # hook's own guarantee, so it holds even if govern's write path changes
    # (and covers the disable-in-progress early return, which writes nothing
    # but must not leave a pre-flush negative entry behind either).
    service.invalidate(asset_type, model.id)


def _resolve_datasource(obj):
    """Datasource for a Slice, portable across Superset versions.

    ``resolved_datasource`` resolves across datasource types (semantic views
    included) but only exists on newer builds; ``datasource`` is SqlaTable-only
    and is present everywhere. Prefer the former, fall back to the latter.
    """
    resolved = getattr(obj, "resolved_datasource", None)
    if resolved is not None:
        return resolved
    return getattr(obj, "datasource", None)


def base_permission_holds(obj: Any, asset_type: str) -> bool:
    """Re-assert Superset's OWN permission check (permission AND ownership).

    THE MOST IMPORTANT FUNCTION IN THIS MODULE. Without this, a cross-tenant
    share (someone shared a dashboard whose dataset you don't have access
    to) would leak data -- ownership/sharing must never substitute for the
    underlying dataset grant, only add to it.

    Uses Superset's own can_access_datasource, never a reimplementation.
    """
    from superset import security_manager

    if asset_type == "dashboard":
        slices = obj.slices
        if not slices:
            return True
        seen = set()
        for slc in slices:
            key = (slc.datasource_type, slc.datasource_id)
            if key in seen:
                continue
            seen.add(key)
            resolved = _resolve_datasource(slc)
            if resolved is not None and security_manager.can_access_datasource(
                resolved
            ):
                return True
        return False

    # chart
    resolved = _resolve_datasource(obj)
    if resolved is None:
        return False
    return security_manager.can_access_datasource(resolved)


def dataset_resolves(obj: Any, asset_type: str) -> bool:
    """Does the object's dataset still resolve?

    False for a chart whose datasource is gone (deleted, or of a type this
    build cannot resolve), and for a dashboard that has charts and none of
    their datasources resolves. Such an object fails `base_permission_holds`
    for EVERYONE, which is a property of the object, not of any user; the
    transfer check asks this first so its refusal can say so instead of
    blaming the recipient. Mirrors the read gate exactly: an empty dashboard
    passes there, so it answers True here.

    Reads the datasource relationship, which on a chart is a lazy load (a
    query); callers evaluate it under the same guard as the permission check.
    """
    if asset_type == "dashboard":
        slices = obj.slices
        if not slices:
            return True
        return any(_resolve_datasource(slc) is not None for slc in slices)
    return _resolve_datasource(obj) is not None


# This module's own per-request caches. Anything computed while impersonating
# another user must not survive into the caller's request.
_REQUEST_CACHES = ("_ownership_editor_cache",)


def base_permission_holds_for(user: Any, obj: Any, asset_type: str) -> bool:
    """`base_permission_holds`, evaluated for `user` rather than the caller.

    Superset's permission checks read the acting user from `flask.g`, so the
    plain function can only answer for whoever is making the request. A
    transfer needs the answer for the RECIPIENT: the whole point of the check
    is that the caller (who can manage the object) and the recipient (who
    may not be able to open it) are different people.

    Impersonation uses Superset's own `override_user`, and this module's
    per-request caches are set aside for the duration and restored after, so
    nothing evaluated for the recipient is served to the caller later in the
    same request. Superset's request cache for RLS filters is keyed by
    username and needs no such handling.

    `override_user` puts the previous user back only on the normal path (no
    try/finally), so a check that raises would leave the rest of the caller's
    request running AS THE RECIPIENT. The caller is therefore restored here
    explicitly, whatever happens inside.
    """
    from flask import g
    from superset.utils.core import override_user

    had_user = hasattr(g, "user")
    caller = getattr(g, "user", None)
    saved = {}
    for name in _REQUEST_CACHES:
        if hasattr(g, name):
            saved[name] = getattr(g, name)
            delattr(g, name)
    try:
        with override_user(user):
            return base_permission_holds(obj, asset_type)
    finally:
        if had_user:
            g.user = caller
        elif hasattr(g, "user"):
            delattr(g, "user")
        for name in _REQUEST_CACHES:
            if hasattr(g, name):
                delattr(g, name)
        for name, value in saved.items():
            setattr(g, name, value)


def owner_is_active(owner_user_id: Optional[int]) -> bool:
    """Is the recorded owner still an active account?

    Resolved at read time rather than by reacting to a deactivation event.
    Ivanti soft-deletes users and emits no event we could hook, and flipping
    every object belonging to a departing user would be a bulk write with
    real blast radius. Reading the flag when the object is accessed costs one
    indexed lookup and cannot drift.

    An owner_user_id of None means the object never had an owner (the
    backfill state) -- which is NOT the same thing as having lost one.
    """
    if owner_user_id is None:
        return False
    from superset import security_manager

    user = security_manager.get_user_by_id(owner_user_id)
    return bool(user is not None and getattr(user, "is_active", False))


def is_unowned(row: Any) -> bool:
    """Object whose owner has gone away.

    Distinct from never-owned: a backfilled object with no owner stays
    public, while an object whose owner was deactivated falls back to
    administrators until someone reassigns it.
    """
    return row.owner_user_id is not None and not owner_is_active(row.owner_user_id)


def _acting_user(user_id: Optional[int]) -> Any:
    """The user a permission check is being made for: Superset's `g.user`
    when it is that user (the common case, no query), else the user record
    for `user_id`."""
    try:
        from flask import g

        current = getattr(g, "user", None)
    except Exception:  # noqa: BLE001 - no request context
        current = None
    if current is not None and (
        user_id is None or getattr(current, "id", None) == user_id
    ):
        return current
    if user_id is None:
        return None
    from superset import security_manager

    return security_manager.get_user_by_id(user_id)


def raise_for_access_bypass(
    user_id: Optional[int] = None,
    dashboard: Any = None,
    chart: Any = None,
    datasource: Any = None,
    query_context: Any = None,
    **_kwargs: Any,
) -> bool:
    """EXTRA_RAISE_FOR_ACCESS_BYPASS: first statement in raise_for_access.

    Nothing has been checked by Superset yet when this runs. Return True to
    grant, False to fall through to Superset's normal checks (which, for a
    private/shared asset, will hit the sentinel-closed viewers branch and
    raise).
    """
    obj = dashboard or chart
    if obj is None:
        return False

    asset_type = "dashboard" if dashboard is not None else "chart"
    object_id = getattr(obj, "id", None)
    if object_id is None:
        # Not persisted (or not a model): there is no row to govern it by.
        # Fall through to Superset's own checks rather than raise from the
        # access-decision path -- `service.lookup` refuses a None id.
        return False
    # Cached: request-locally and for OWNERSHIP_LOOKUP_CACHE_TTL seconds
    # across requests (service.lookup). For an owner or a public object this
    # is the whole cost of the hook -- one cached lookup, zero store calls.
    row = service.lookup(asset_type, object_id)
    if row is None or not service.is_enforced(row):
        # Ungoverned: public under OWNERSHIP_PUBLIC_SCOPE=instance, or parked
        # by disable() (`disabled:<vis>`): this module does nothing. During a
        # disable window denial is intentionally off.
        return False

    if not base_permission_holds(obj, asset_type):
        return False  # no dataset access -> ownership/sharing can't override

    if row.visibility == "public":
        # Public within the tenant (OWNERSHIP_PUBLIC_SCOPE=tenant). The
        # sentinel closed Superset's own rule; this grants the owner and the
        # members of the object's tenant -- and, for an object outside every
        # tenant, only callers outside every tenant -- from the row alone.
        # Zero store calls: as cheap as public always was, and readable
        # through a store outage. An unowned public
        # object stays readable within its tenant exactly like an owned one:
        # it is the owner's ABSENCE that changes, not the object's audience.
        return service.public_row_visible_to(row, _acting_user(user_id))

    if row.owner_user_id == user_id:
        return True  # owner fast path, zero store calls -- before anything else

    if service.tenant_admin_reads(row, _acting_user(user_id)):
        # A tenant administrator reads every object of their tenant,
        # private, shared or unowned: they can transfer it, take it or claim
        # it, so hiding it only hid the row they need to click on (and left
        # an object whose owner had gone unreachable from the UI, #102).
        # From the row's mirrored tenant, one cached administrator check --
        # AFTER the owner fast path (review round 1 of PR #116): an owner's
        # own read stays zero store calls, and readable through an outage.
        return True

    if is_unowned(row):
        # Owner deactivated. The object falls back to administrators, who
        # reach it through Superset's own is_admin check further down; we
        # grant nobody, and existing shares stop conferring access.
        return False

    # Split out to keep this function's own cyclomatic complexity within the
    # repo's ruff C901 budget (issue #93 added the private-deny branch below,
    # which pushed the inline version over it); a pure extraction, not a
    # behaviour change -- see _non_owner_may_open_governed's docstring for
    # the actual logic.
    return _non_owner_may_open_governed(row, user_id, asset_type)


def _non_owner_may_open_governed(
    row: Any, user_id: Optional[int], asset_type: str
) -> bool:
    """`raise_for_access_bypass`'s decision for a non-owner, once the row is
    known governed (private or shared) and the owner fast path has already
    missed. Every `return` here is a DENY except the final store `check`.
    """
    if row.visibility == "private":
        # Defence in depth for issue #93. `_set_asset_visibility` queues a
        # `revoke_subject` for every existing share the moment an object
        # goes private, through the outbox -- which `has_pending_revocation`
        # (below, on the `shared` path) already denies against while that
        # delivery is in flight. This is the belt to that braces: whatever
        # the mirror or the store still say, a non-owner is refused a
        # PRIVATE object without asking either -- zero store calls, same as
        # the owner fast path above. Public is not reached here (filtered
        # by GOVERNED_VISIBILITIES); shared still falls through to the
        # pending-revocation check and the store below.
        return False

    if row.owner_user_id is None or user_id is None:
        return False

    # Ask OpenFGA about Ivanti's member GUID, not Superset's integer id --
    # their tuples are keyed on GUID v4 and would never match otherwise.
    from superset_ownership.identity import member_ref

    subject = member_ref(user_id)
    if subject is None:
        return False
    obj = f"{asset_type}:{row.object_uuid}"
    # The store lags the mirror until the outbox is drained. For a GRANT that
    # lag fails safe on its own (no tuple yet -> denied). For a REVOCATION it
    # does not: the store still holds the tuple and would keep granting. So
    # before asking the store, deny while a delete/purge for this object and
    # subject is still undelivered (a queued role change, whose subject still
    # has a mirror row, is not a revocation). One indexed query; see
    # outbox.has_pending_revocation for exactly what denies whom.
    #
    # DELIBERATELY NOT CACHED, in either layer. The ownership row above may
    # be up to a TTL old; this gate is what makes a revocation take effect
    # the moment it is queued, and a cached "no pending revocation" would
    # re-open exactly the window the outbox closes. It only runs on the
    # non-owner path, which the one-cached-lookup criterion does not cover.
    if outbox.has_pending_revocation(obj, subject, object_id=row.object_id):
        return False
    return get_authorizer().check(subject, "viewer", obj)


def dashboard_query_filter(user_id: int) -> list[int]:
    """EXTRA_ACCESS_QUERY_FILTERS["dashboards"]: ids to OR into the list query."""
    return _query_filter("dashboard", user_id)


def chart_query_filter(user_id: int) -> list[int]:
    """EXTRA_ACCESS_QUERY_FILTERS["charts"]: ids to OR into the list query."""
    return _query_filter("chart", user_id)


def _query_filter(asset_type: str, user_id: int) -> list[int]:
    # One scan of the asset type's rows answers both questions -- which
    # objects the user can reach, and which of those have a recorded owner --
    # and seeds the request-local cache with the candidates, so the per-object
    # lookups Superset makes while rendering the list are free. Nothing is
    # published to the shared layer from a list page.
    #
    # `owned_or_shared_rows` itself drops a candidate with an undelivered
    # revocation for this subject (issue #84): the store's reverse index
    # lags the outbox the same way `check` does, so without that this filter
    # -- and, through FAB's list base filter, a single-object GET -- would
    # keep admitting a revoked subject after `raise_for_access_bypass`
    # already denies them, until the drain caught up.
    rows = service.owned_or_shared_rows(asset_type, user_id)

    # Drop anything raise_for_access_bypass would refuse anyway. It denies
    # everyone on an object with no recorded owner, so granting here put
    # objects in a user's list that returned 403 the moment they clicked --
    # two hooks in the same module disagreeing about the same object.
    candidate_ids = {
        object_id
        for object_id, row in rows.items()
        if row.owner_user_id is not None and not is_unowned(row)
    }
    ids: list[int] = []
    if candidate_ids:
        from superset import db

        if asset_type == "dashboard":
            from superset.models.dashboard import Dashboard

            objs = db.session.query(Dashboard).filter(Dashboard.id.in_(candidate_ids))
        else:
            from superset.models.slice import Slice

            objs = db.session.query(Slice).filter(Slice.id.in_(candidate_ids))
        ids = [o.id for o in objs.all() if base_permission_holds(o, asset_type)]
    # Public within the tenant: with the sentinel on public objects too,
    # Superset's own list filter no longer admits them, so the ones this
    # caller may see are OR'd back in here -- decided in SQL, the way stock
    # decides public objects, never by loading them (a tenant's whole public
    # set is unbounded; the owned/shared set above is not).
    user = _acting_user(user_id)
    in_sql: list[int] = []
    if service.public_is_tenant_scoped():
        in_sql.extend(_public_ids_in_sql(asset_type, user))
    # A tenant administrator: every governed object of their tenant, the
    # same way -- one statement, drafts included (an administrator re-homes
    # a draft too), the dataset grant applied as for anything else.
    in_sql.extend(_administered_ids_in_sql(asset_type, user))
    if in_sql:
        in_sql = sorted(set(in_sql))
        if len(in_sql) <= _SEED_LIMIT:
            # One more statement, so the per-row lookups the list page makes
            # while rendering (`extra_editors`, the tile marker) are served
            # from the request cache instead of costing one SELECT each.
            # Capped: a page renders at most 100 rows, and a large tenant's
            # whole set is not worth a seed on every request (FAB applies
            # this filter to a single-object GET as well).
            service.lookup_many(asset_type, in_sql)
        ids.extend(in_sql)
    return ids


_SEED_LIMIT = 200


def _public_ids_in_sql(asset_type: str, user: Any) -> list[int]:
    """The public objects of this caller's tenant that Superset's own
    no-viewer rule would list -- `_ids_in_sql` over
    `service.public_object_ids_visible_to`, with stock's `published`
    condition. Kept as the named entry point the tests and the docs use."""
    return _ids_in_sql(
        asset_type,
        service.public_object_ids_visible_to(asset_type, user),
        published_only=True,
    )


def _administered_ids_in_sql(asset_type: str, user: Any) -> list[int]:
    """Every governed object of the tenant this caller administers that the
    dataset grant lets them list (`service.administered_object_ids`);
    empty for a caller who administers no tenant."""
    administered = service.administered_object_ids(asset_type, user)
    if administered is None:
        return []
    return _ids_in_sql(
        asset_type, administered, published_only=False, include_chartless=True
    )


def _ids_in_sql(
    asset_type: str,
    candidate_ids: Any,
    *,
    published_only: bool,
    include_chartless: bool = False,
) -> list[int]:
    """The objects among `candidate_ids` (a selectable of ownership-row
    object ids) that Superset's own no-viewer rule would list: stock's
    `no_viewer_query` restated -- the dataset grant through
    `get_dataset_access_filters`, and with `published_only`, `published`
    for a dashboard -- intersected with the ownership rule as a subquery.
    With `include_chartless`, a dashboard with no chart is listed too
    (stock's inner join drops it; a tenant administrator re-homes an
    empty dashboard as much as a full one).
    One statement whatever the tenant's size; no model is loaded and no
    per-object permission check runs. The dataset grant is evaluated here
    exactly as stock evaluates it for the same objects under instance
    scope, and again on access by `raise_for_access_bypass`
    (`base_permission_holds`).

    The `~has_viewers` clause of stock's query is deliberately absent: the
    sentinel is a viewer, and its whole purpose is to route these objects
    through this hook.
    """
    from superset import db, security_manager
    from superset.connectors.sqla.models import SqlaTable
    from superset.models.core import Database
    from superset.models.slice import Slice
    from superset.utils.filters import get_dataset_access_filters

    grant = get_dataset_access_filters(
        Slice, security_manager.can_access_all_datasources()
    )
    if asset_type == "dashboard":
        from superset.models.dashboard import Dashboard

        conditions = [Dashboard.id.in_(candidate_ids)]
        if include_chartless:
            from sqlalchemy import or_

            conditions.append(or_(Slice.id.is_(None), grant))
        else:
            conditions.append(grant)
        if published_only:
            conditions.append(Dashboard.published.is_(True))
        query = (
            db.session.query(Dashboard.id)
            .join(Dashboard.slices, isouter=True)
            .join(
                SqlaTable,
                Slice.datasource_id == SqlaTable.id,
                isouter=include_chartless,
            )
            .join(
                Database,
                SqlaTable.database_id == Database.id,
                isouter=include_chartless,
            )
            .filter(*conditions)
            .distinct()
        )
    else:
        query = (
            db.session.query(Slice.id)
            .join(SqlaTable, Slice.datasource_id == SqlaTable.id)
            .join(Database, SqlaTable.database_id == Database.id)
            .filter(Slice.id.in_(candidate_ids), grant)
        )
    return [row[0] for row in query.all()]


def extra_editors(resource: Any) -> list[int]:
    """EXTRA_EDITORS_RESOLVER: Subject ids that may EDIT this resource.

    Superset's own `is_editor` consults this, before the viewers branch and
    without requiring `published`. Wiring it is what makes an `editor` share
    confer edit in Superset's own write path -- until it was, the role was a
    viewer share with a different label: the share granted read through the
    bypass hook, and a PUT by the share-holder was 403.

    Only governed objects answer. Only USER subjects can be returned: a group
    that exists solely in the authorization store has no Superset Subject, so
    its editor grant confers read through the ownership hooks and cannot
    confer native edit. Subject rows are looked up, never created -- this
    runs inside permission checks and inside the flush guard.
    """
    asset_type = (
        "dashboard"
        if type(resource).__name__ == "Dashboard"
        else ("chart" if type(resource).__name__ == "Slice" else None)
    )
    object_id = getattr(resource, "id", None)
    if asset_type is None or object_id is None:
        return []

    # Per-request cache of the DERIVED editor set. is_editor() consults this
    # on every permission check, and a single-object check is not batched, so
    # the same object was re-resolved (lookup + share query + subject query)
    # many times per request. Keyed per object; lives only for the request
    # (flask.g).
    #
    # This is not a second copy of the ownership row: the row itself comes
    # from service.lookup's request-local cache, and this entry holds
    # what the row plus the share rows plus the Subject rows compute to. The
    # two are kept coherent by service.invalidate(), which drops this entry
    # whenever the row or a share on the object is written.
    cache_key = f"{asset_type}:{object_id}"
    cache = None
    try:
        from flask import g

        cache = getattr(g, "_ownership_editor_cache", None)
        if cache is None:
            cache = {}
            g._ownership_editor_cache = cache
        if cache_key in cache:
            return list(cache[cache_key])
    except Exception:
        cache = None

    row = service.lookup(asset_type, object_id)
    if row is None or row.visibility not in service.GOVERNED_VISIBILITIES:
        result: list[int] = []
    else:
        result = editor_subject_ids(asset_type, object_id, row, resource)

    if cache is not None:
        cache[cache_key] = list(result)
    return result


def editor_share_user_ids(asset_type: str, object_id: int, row: Any) -> list[int]:
    """Superset user ids explicitly holding an `editor` share on this object.

    Extracted out of `editor_subject_ids` so the identity and tenant-scoping
    rules below live in one place rather than being duplicated by a second
    caller asking the identical question. `service.sync_native_editors` is
    deliberately NOT such a caller (review H-1/H-2, round 1): the dataset
    symmetry `editor_subject_ids` applies below is evaluated for the CURRENT
    user and cannot be derived from the share rows alone, so a native sync
    -- which is not evaluated for any one user -- has no correct way to
    apply it, and materialising a share-holder into `asset.editors` without
    it let `is_editor` (which trusts that collection outright) admit them
    regardless. The native collection holds the owner only; editor shares
    stay dynamic, through this function's caller.

    TENANT -- a resolved editor must belong to the object's tenant. Share
    creation gates this for non-admin callers, but an admin (tenant=None)
    bypasses it and stores can drift, so it is re-asserted here: an editor
    share to someone outside the object's tenant confers nothing.

    M-3 (review round 2, PR #100): `object_tenant` is read LAZILY, the
    first time `in_tenant` actually needs it, not unconditionally before
    the loop. An object with no editor `user:` share at all -- an owner-only
    object, or one shared as `viewer` only -- never reaches `in_tenant`, so
    it costs zero store calls, matching the "at most one reverse-index read
    per subject per TTL" claim the PR's spec amendment makes for the whole
    route: before this, EVERY call here paid one `object_tenant` read
    (cache or no cache -- this is not the list_objects cache's concern),
    on every chart/dashboard GET and list-item serialisation, whether or
    not the object had a single editor share.
    """
    from superset import security_manager

    from superset_ownership.identity import (
        IdentityLookupError,
        normalize_subject,
        resolve_tenant_guid,
    )

    tenant_read = {"done": False, "value": None}

    def _obj_tenant() -> Optional[str]:
        if not tenant_read["done"]:
            tenant_read["value"] = (
                get_authorizer().object_tenant(asset_type, row.object_uuid)
                if row.object_uuid
                else None
            )
            tenant_read["done"] = True
        return tenant_read["value"]

    def in_tenant(user) -> bool:
        # No tenant on the object (standalone / not yet backfilled): do not
        # filter on tenancy. Otherwise the editor must share the tenant.
        obj_tenant = _obj_tenant()
        if not obj_tenant:
            return True
        return service.normalize_tenant(
            resolve_tenant_guid(user)
        ) == service.normalize_tenant(obj_tenant)

    editor_user_ids: list[int] = []
    for share in service.list_share_rows(asset_type, object_id):
        if share["role"] != "editor" or not share["subject"].startswith("user:"):
            continue
        # R-1: an editor share is granted only on a resolvable subject --
        # a raising reverse hook (IdentityLookupError) is treated exactly
        # like "no candidate" here, same as `normalize_subject`'s own
        # None answer, never let past this filter.
        try:
            canonical = normalize_subject(share["subject"])
        except IdentityLookupError:
            canonical = None
        guid = canonical.split(":", 1)[1] if canonical else None
        if not guid:
            continue
        from superset_ownership.identity import member_guid_of_subject_id

        member = member_guid_of_subject_id(guid) or guid
        user = security_manager.find_user(username=member)
        if user is None and member.startswith("local-"):
            user = security_manager.get_user_by_id(int(member[6:]))
        # Deactivated confers nothing (mirrors is_unowned); cross-tenant
        # confers nothing (mirrors the read gate's tenant scoping).
        if user is not None and getattr(user, "is_active", False) and in_tenant(user):
            editor_user_ids.append(user.id)
    return editor_user_ids


def editor_subject_ids(
    asset_type: str, object_id: int, row: Any, resource: Any = None
) -> list[int]:
    """Existing Subject ids for the owner and every user holding an editor share.

    The dataset symmetry below is the one thing `editor_share_user_ids` does
    not do for us, since it is evaluated for the CURRENT user, not derivable
    from the share rows alone:

    DATASET -- the read path only honours a share when the accessor holds the
    underlying dataset grant (base_permission_holds). The write path is
    evaluated for the CURRENT user, so if the current user's edit rights come
    ONLY from an editor share (they are not the owner) and they lack dataset
    access, their own subjects are dropped from the result -- they cannot edit
    an object they could not read. Other subjects are unaffected; each is
    filtered when it is their turn as the current user.
    """
    from superset.subjects.models import Subject
    from superset.utils.core import get_user_id

    db = service._db()

    owner_user_ids: list[int] = []
    if row.owner_user_id is not None:
        owner_user_ids.append(row.owner_user_id)

    # H1 (review round 1, issue #93): a `private` object confers nothing to
    # a non-owner, editor shares included -- the same rule
    # `_non_owner_may_open_governed` applies on the read path. Without this,
    # `is_editor` (which trusts this function outright) admitted an editor
    # share the read gate itself refused: `POST /shares {role: editor}` on a
    # still-private object opened AND edited it.
    #
    # R2-N1 (review round 2, PR #96, `qa/reviews/review-private-revokes-
    # pr96.md`): the short-circuit runs BEFORE `editor_share_user_ids`, not
    # after -- a private object's editor shares confer nothing regardless of
    # what they are, so there is no reason to ask. Skips the
    # `service.list_share_rows` query and every per-share identity
    # resolution `editor_share_user_ids` would otherwise do for a private
    # object's shares, on top of M-3's lazy `object_tenant` read (PR #100),
    # which this also makes moot for a private row specifically: `is_editor`
    # on a private object by a non-owner now makes no store call and no
    # mirror query at all, not merely no store CHECK.
    if row.visibility == "private":
        editor_user_ids = []
    else:
        editor_user_ids = editor_share_user_ids(asset_type, object_id, row)

    # Dataset symmetry: drop the CURRENT user's edit-by-share right if they
    # cannot read the object. The owner is exempt (ownership is the basis).
    current = get_user_id()
    if (
        current is not None
        and current in editor_user_ids
        and current not in owner_user_ids
        and resource is not None
        and not base_permission_holds(resource, asset_type)
    ):
        editor_user_ids = [u for u in editor_user_ids if u != current]

    user_ids = owner_user_ids + editor_user_ids
    if not user_ids:
        return []
    with db.session.no_autoflush:
        return [
            sid
            for (sid,) in db.session.query(Subject.id)
            .filter(Subject.user_id.in_(set(user_ids)))
            .all()
        ]


def after_asset_delete(
    asset_type: str,
    object_id: int,
    object_uuid: Optional[str],
    *,
    lock: bool = True,
) -> None:
    """Collect an object's ownership state when the object itself goes.

    Called from the flush guard for a hard delete (there is no
    AFTER_ASSET_DELETE extension point). Without it every purge -- manual or
    the 30-day retention sweep -- left an ownership row and two tuples
    behind for an object that no longer existed, and `check` went
    permanently red on the first one.
    """
    from superset_ownership.db import ownership_object, ownership_share

    db = service._db()
    # The per-object write lock, as on every mutating route, so the purge
    # row this enqueues cannot take an id out of commit order with a share
    # or transfer racing on the same object. On the ORM path this runs in
    # `before_flush`, ahead of Superset's own statements in the flush, so
    # the ownership row is locked before Superset's row -- the same order
    # the routes use (lock, then touch the asset). The DELETE of the row
    # below takes the same row lock in any case, so the explicit lock adds
    # no wait, and no deadlock exposure, that the hook did not already
    # have; it makes the hook's discipline the routes'. A bulk delete
    # reaches here once per object, in (asset_type, object_id) order
    # (guard._collect_deleted).
    #
    # `lock=False` is the ONE caller that cannot honour that order:
    # `guard._collect_core_deleted`, standing in front of a statement whose
    # caller (`cascade_hard_delete`) already holds the asset row FOR
    # UPDATE. Taking ours there would be the module's only lock-after-asset
    # and the ABBA against the visibility route (review round 1); the row
    # locks the DELETEs below take are enough, because this path's intents
    # are the last the object will ever have.
    if lock:
        service.lock_object(asset_type, object_id)
    # WHICH uuid the store has this object filed under. Normally the two
    # agree, and this is `object_uuid`. They disagree when the same flush
    # both re-points the object's uuid and deletes it: SQLAlchemy drops a
    # deleted object out of `session.dirty`, so `guard._follow_uuid_changes`
    # never sees it, while the caller reads the object's already-reassigned
    # in-memory uuid -- and we would then purge a reference that never held
    # a tuple and leave the real ones filed under the old one forever, with
    # the row gone so nothing could ever find them again (review round 3).
    # The MIRROR is what the store was written from, so purge both.
    #
    # ONLY where the store files relationships under the object's uuid
    # (review round 4). On a backend keyed by object id -- the local one --
    # the relationships ARE the share rows this function deletes a few lines
    # below, so the purge is redundant there; worse, by the time it runs the
    # row is gone, so resolving the reference back to a row lands on
    # whichever OTHER row still holds that uuid (`object_uuid` carries no
    # unique constraint) and deletes THAT object's shares. `reconcile`
    # restores owner tuples but never share tuples, so the victim's share is
    # permanently dead, and its mirror rows survive so `check` stays green.
    # Same declaration `service.repoint_object_uuid` reads, same reason.
    if getattr(get_authorizer(), "addresses_objects_by_uuid", True):
        mirrored = getattr(
            service.lookup(asset_type, object_id, fresh=True), "object_uuid", None
        )
        doomed_refs = [
            u for u in (object_uuid, str(mirrored) if mirrored else None) if u
        ]
        doomed_refs = list(dict.fromkeys(doomed_refs))  # ordered, de-duplicated
    else:
        doomed_refs = []
    # Local rows first: their removal must never depend on the store
    # answering. Then the delete EVENT for the authorization store, in the
    # same transaction, so the object's tuples are removed even if OpenFGA is
    # unreachable at the moment of deletion -- the drain delivers it later.
    # Previously this function only removed the local rows and left every
    # tuple behind, which is the orphan `reconcile` had to sweep up.
    db.session.execute(
        ownership_share.delete().where(
            ownership_share.c.asset_type == asset_type,
            ownership_share.c.object_id == object_id,
        )
    )
    db.session.execute(
        ownership_object.delete().where(
            ownership_object.c.asset_type == asset_type,
            ownership_object.c.object_id == object_id,
        )
    )
    # The row is gone; a cached copy would keep answering for an object that
    # no longer exists (and for its id, should the database ever reuse it).
    service.invalidate(asset_type, object_id)
    for ref in doomed_refs:
        try:
            outbox.purge_object(asset_type, ref)
        except StoreError as exc:
            # Inline mode only (enabled, purge_object is an enqueue and cannot
            # raise): the store could not be purged right now. The local
            # cleanup above stands; the tuples are `reconcile`'s to sweep.
            logger.error(
                "superset_ownership: %s %s hard-deleted but the authorization store could not be "
                "purged inline (%s); run `superset ownership reconcile`",
                asset_type,
                object_id,
                exc,
            )
    logger.info(
        "superset_ownership: %s %s hard-deleted; ownership rows removed",
        asset_type,
        object_id,
    )


# --- chart-level privacy on the DATA path -----------------------------------
#
# Superset gates a dashboard tile's data on the DATASOURCE, not on the chart:
# a tile sends a query context, and `raise_for_access(query_context=...)` never
# looks at the chart's viewers. So a private chart sitting on a dashboard the
# viewer can open rendered its data to anyone holding the dataset grant, even
# though opening that chart directly was a 404.
#
# This closes it at the only place a bolt-on can: a before_request on the
# chart-data endpoints. It does NOT touch the dashboard's position_json --
# removing a tile would corrupt the saved layout -- so the tile stays exactly
# where it is and renders a "private" placeholder instead of the data.


def owners_resolver(resource) -> list:
    """Display names of a governed asset's owner, for Superset's denial paths.

    Wired to `EXTRA_OWNERS_RESOLVER`. Ownership here lives in this module's own
    table, NOT in the model's `owners` relationship (Slice does not even have
    one), so without this a denied viewer is told to "contact your
    administrator" when a specific, reachable owner exists.

    Returns [] rather than raising on any problem: this feeds an error message,
    and failing to name an owner must never turn a clean denial into a 500.
    """
    try:
        asset_type = (
            "dashboard" if resource.__class__.__name__ == "Dashboard" else "chart"
        )
        row = service.lookup(asset_type, resource.id)
        if row is None or not row.owner_user_id:
            return []

        from flask_appbuilder.security.sqla.models import User

        from superset import db

        user = db.session.query(User).get(row.owner_user_id)
        return [str(user)] if user else []
    except Exception:  # noqa: BLE001 - never break a denial over a display name
        logger.debug("superset_ownership: owners_resolver failed", exc_info=True)
        return []


_CHART_DATA_PK = re.compile(r"^/api/v1/chart/(\d+)/data/?$")
_CHART_DATA_BULK = re.compile(r"^/api/v1/chart/data/?$")

# Superset's own canonical error type for "this chart is denied to you". Using
# it -- rather than a bespoke OWNERSHIP_* string -- means the frontend's error
# registry already routes this to the chart-access component, so a denied tile
# and a denied /chart/data call render the SAME message from ONE component.
OWNERSHIP_PRIVATE_ERROR = "CHART_SECURITY_ACCESS_ERROR"


def _slice_id_from_request(request) -> Optional[int]:
    """Which saved chart is this data request for, if any."""
    m = _CHART_DATA_PK.match(request.path or "")
    if m:
        return int(m.group(1))
    if not _CHART_DATA_BULK.match(request.path or ""):
        return None
    payload = request.get_json(silent=True) or {}
    form_data = payload.get("form_data") or {}
    slice_id = form_data.get("slice_id")
    if slice_id is None:
        # Some callers put it on the first query's form_data instead.
        for q in payload.get("queries") or []:
            slice_id = (q.get("form_data") or {}).get("slice_id")
            if slice_id is not None:
                break
    try:
        return int(slice_id) if slice_id is not None else None
    except (TypeError, ValueError):
        return None


def enforce_chart_data_access():
    """Flask before_request: deny a governed chart's data to viewers without access.

    Reuses Superset's own `raise_for_access(chart=...)` -- which runs this
    module's bypass hook and the sentinel -- so the answer is IDENTICAL to
    opening the chart directly. Owners, people it is shared with, tenant
    administrators and admins are unaffected.

    An ad-hoc explore request (no saved chart) is left alone: there is no
    chart to govern, and the datasource check still applies.
    """
    from flask import jsonify, request

    try:
        slice_id = _slice_id_from_request(request)
        if slice_id is None:
            return None

        from superset import db, security_manager
        from superset.models.slice import Slice

        row = service.lookup("chart", slice_id)
        if row is None or not service.is_enforced(row):
            return None  # ungoverned: nothing to enforce

        slc = db.session.query(Slice).get(slice_id)
        if slc is None:
            return None
        if row.visibility == "public" and (
            not base_permission_holds(slc, "chart")
            or service.public_row_visible_to(row, _acting_user(None))
        ):
            # Public: the only thing this module withholds is the other
            # tenant's read. Its own tenant's members are answered by
            # Superset (the dataset check applies as ever), and a caller
            # without the dataset grant gets Superset's own refusal, not
            # the ownership placeholder -- the grant, not sharing, is what
            # they lack.
            return None
        try:
            security_manager.raise_for_access(chart=slc)
            return None  # allowed -- owner, shared, tenant admin or admin
        except Exception:  # noqa: BLE001 - any denial is a denial
            pass

        logger.info(
            "superset_ownership: blocked chart %s data for user %s (private/shared)",
            slice_id,
            getattr(security_manager, "current_user_id", lambda: None)(),
        )
        return (
            jsonify(
                {
                    "errors": [
                        {
                            "message": ("You do not have access to this chart."),
                            "error_type": OWNERSHIP_PRIVATE_ERROR,
                            "level": "warning",
                            "extra": {
                                "ownership": "private",
                                "chart_id": slice_id,
                                "slice_name": slc.slice_name,
                                # Who to ask. The NAME of a chart and of its
                                # owner are not the secret -- the data is --
                                # and without them a viewer cannot say which
                                # tile they need, which is the whole point of
                                # asking.
                                "owners": owners_resolver(slc),
                            },
                        }
                    ]
                }
            ),
            403,
        )
    except Exception:
        # Never let this guard take down a data request on its own fault.
        logger.exception("superset_ownership: chart-data guard failed; allowing")
        return None

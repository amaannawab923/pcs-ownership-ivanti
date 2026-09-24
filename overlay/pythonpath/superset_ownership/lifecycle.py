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
"""Tenant purge and feature disable.

Two operations that exist because of how the module fails, not how it works.

`purge_tenant` is the entry point Ivanti's offboarding calls. Their cleanup
removes users, datasets, charts, the RLS rule and the tenant role, and knows
nothing about ownership rows or authorization tuples. Without this, every
offboarding leaves both stores accreting rows that nothing will collect.

`disable` exists because turning the feature flag off is not, by itself, a
rollback. Denial works by a sentinel row written into the object's viewers;
with the module unloaded that row stays behind and no code remains to
interpret it, so every private and shared object becomes unreachable BY ITS
OWN OWNER. Disabling must strip the sentinels first.

Both are idempotent and safe to call repeatedly.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from superset_ownership import service

logger = logging.getLogger(__name__)


def all_rows(model):
    """Every row of an asset model, INCLUDING soft-deleted ones.

    This build has SoftDelete on, and a soft-deleted object keeps both its
    ownership row and its sentinel while disappearing from an ordinary query.
    So `disable()` skipped it and left a sentinel behind that no code would
    interpret, and `check_consistency` could not see it either and reported
    ok. Soft-delete the object, run the documented rollback, turn the flag
    off, restore the object from the trash, and it was unreachable by its own
    owner, permanently, on a system reporting itself healthy.

    Superset excludes soft-deleted rows through a `do_orm_execute` listener,
    not a where clause, so it is turned off with the mechanism that listener
    reads: `SoftDeleteMixin`'s documented per-query bypass
    (`superset.constants.SKIP_VISIBILITY_FILTER_CLASSES`). Guessing at an
    `include_deleted=True` execution option did nothing at all -- the option
    was accepted, ignored, and the sweep silently kept missing exactly the
    objects it exists to catch.
    """
    from superset import db

    query = db.session.query(model)
    try:
        from superset.constants import SKIP_VISIBILITY_FILTER_CLASSES

        query = query.execution_options(**{SKIP_VISIBILITY_FILTER_CLASSES: {model}})
    except ImportError:
        # A build without the soft-delete mixin: the plain query already
        # returns every row. `sweeps_soft_deleted` is what decides whether
        # that is true or whether this except branch just swallowed the one
        # thing that made the sweep complete -- ask it before TREATING an
        # absence here as "the object is gone".
        pass
    return query.all()


def sweeps_soft_deleted(model) -> bool:
    """Can `all_rows(model)` really see soft-deleted rows of this model?

    `all_rows` applies the documented bypass behind `except ImportError:
    pass`, which is right for a build that has no soft delete and wrong for
    one that has it under a different name: the sweep then silently returns
    the LIVE rows only. Reporting that as "these objects are gone" is
    harmless; DELETING their ownership on it is not, and
    `rows_without_object` does exactly that (review round 1). So the
    destructive caller asks this first and refuses rather than guessing.

    True when the model is not soft-deletable at all (nothing to bypass), or
    when the bypass this build documents is importable.
    """
    try:
        from superset.models.helpers import SoftDeleteMixin
    except ImportError:
        return True  # no soft delete in this build: a plain query is complete
    try:
        if not issubclass(model, SoftDeleteMixin):
            return True
    except TypeError:  # pragma: no cover - a stand-in model in a pure test
        return True
    try:
        from superset.constants import SKIP_VISIBILITY_FILTER_CLASSES  # noqa: F401
    except ImportError:
        return False
    return True


def _member_guids(tenant_guid: str) -> list[str]:
    """Member GUIDs of a tenant, read directly from the store.

    `fga.tenant_members` is moving to `OpenFGADirectory` (spec section 5.4:
    it is one of the three functions the fga.py half of this series
    removes), and purge is store maintenance reading RAW tuples, not a
    `Directory` consumer (D-10) -- so this inlines the same extraction
    `fga.tenant_members` makes, directly against `fga.read_all` rather
    than depending on a function that will not be there.
    """
    from superset_ownership import fga

    return [
        t["user"].split(":", 1)[1]
        for t in fga.read_all(f"tenant:{tenant_guid}", "member")
        if t["user"].startswith("user:")
    ]


def _tenant_object_ids(tenant_guid: str) -> dict[str, list[str]]:
    """Object uuids the authorization store associates with a tenant.

    Walks BOTH relations. Asking only for `viewer` finds objects the tenant's
    members can see and misses every private object a member owns and never
    shared -- the majority of them, and precisely the ones an offboarding
    is supposed to clear.
    """
    from superset_ownership import fga

    out: dict[str, list[str]] = {"dashboard": [], "chart": []}
    members = _member_guids(tenant_guid)
    for asset_type in out:
        seen = set()
        # The tenant's own objects, in one call.
        for uuid in fga.tenant_objects(tenant_guid, asset_type):
            if uuid not in seen:
                seen.add(uuid)
                out[asset_type].append(uuid)
        # Plus anything reachable through its members, which covers objects
        # created before the tenant tuple was written at creation time.
        for guid in members:
            for relation in ("owner", "viewer"):
                for obj in fga.list_objects(f"user:{guid}", relation, asset_type):
                    uuid = obj.split(":", 1)[1]
                    if uuid not in seen:
                        seen.add(uuid)
                        out[asset_type].append(uuid)
    return out


def _describe_plugins() -> dict[str, Any]:
    """`plugins.describe()` (spec section 4: seam -> class, version, source,
    degraded) when the loader module is importable; a narrower hand-built
    version -- just the authorizer and directory class names -- otherwise,
    so `check_consistency` works standalone without it."""
    try:
        from superset_ownership.plugins import describe

        return describe()
    except ImportError:
        from superset_ownership.authz import get_authorizer

        try:  # pragma: no cover - exercised only when plugins.py cannot import
            from superset_ownership.plugins import get_directory
        except ImportError:
            from superset_ownership.directory import get_directory

        authorizer = get_authorizer()
        directory = get_directory()
        return {
            "authorizer": f"{type(authorizer).__module__}:{type(authorizer).__qualname__}",
            "directory": f"{type(directory).__module__}:{type(directory).__qualname__}",
        }


def _requires_openfga(operation: str) -> Optional[dict]:
    """Store-maintenance operations need the store.

    purge and reconcile are OpenFGA housekeeping by nature: they enumerate,
    delete and realign tuples. On the `local` backend there are no tuples, so
    rather than reporting a successful purge of nothing, say what is true.
    """
    from superset_ownership.authz import OpenFGAAuthorizer, get_authorizer

    authorizer = get_authorizer()
    backend = getattr(authorizer, "name", "local")
    # A dotted-path SUBCLASS of OpenFGAAuthorizer (spec section 8, id-shape
    # overrides) still needs the store, so this checks the type rather than
    # the configured name string -- a name like "ivanti_pcs.authz:Prefixed
    # IdAuthorizer" would never equal the literal "openfga" the old
    # config-read compared against.
    if not isinstance(authorizer, OpenFGAAuthorizer):
        return {
            "error": (
                f"{operation} operates on the authorization store and requires "
                f"OWNERSHIP_AUTHORIZER=openfga (current backend: {backend})"
            ),
            "backend": backend,
        }
    return None


def normalize_tenant_guid(tenant_guid: Any) -> str:
    """The tenant as the store spells it: a GUID v4, lower-cased.

    Raises ValueError for any other shape. Every group id parse anchors on
    the GUID v4, so a tenant of any other shape is one the format could
    never find a group for; it must be refused rather than searched for.
    """
    from superset_ownership.identity import GUID_RE

    if not isinstance(tenant_guid, str) or not GUID_RE.fullmatch(tenant_guid):
        raise ValueError(f"tenant must be a GUID v4, got {tenant_guid!r}")
    return tenant_guid.lower()


def _row_scope(
    table: Any, asset_type: Optional[str], object_ids: Optional[list[int]]
) -> list:
    scope = []
    if asset_type is not None:
        scope.append(table.c.asset_type == asset_type)
    if object_ids is not None:
        scope.append(table.c.object_id.in_(object_ids))
    return scope


def ownership_lock_statement(
    asset_type: Optional[str], object_ids: Optional[list[int]]
):
    """`SELECT id ... FOR UPDATE` over the scoped `ownership_object` rows,
    ORDER BY (asset_type, object_id): Postgres locks the rows in the order
    the sort returns them, which is the order every bulk path takes its
    rows in. Compiles to a plain ordered SELECT on SQLite, as
    service._lock_statement does."""
    from sqlalchemy import select

    from superset_ownership.db import ownership_object

    return (
        select(ownership_object.c.id)
        .where(*_row_scope(ownership_object, asset_type, object_ids))
        .order_by(ownership_object.c.asset_type, ownership_object.c.object_id)
        .with_for_update()
    )


def delete_object_rows(
    session: Any,
    asset_type: Optional[str] = None,
    object_ids: Optional[list[int]] = None,
) -> tuple[int, int]:
    """Delete ownership rows -- and their share rows -- in the routes' lock
    order. Returns (share_rows, ownership_rows) deleted.

    A share route takes the object's `ownership_object` row (`FOR UPDATE`,
    `service.lock_object`) and then writes `ownership_share`. A bulk delete
    that went share-first -- DELETE the share rows, then DELETE the object
    rows -- held what the route wanted next while waiting for what the
    route held: a cycle, and Postgres killed one side after
    `deadlock_timeout`. So: lock the object rows first, `SELECT ... FOR
    UPDATE` in (asset_type, object_id) order -- the order the hard-delete
    hook and the backfill take their rows in too -- and only then delete
    the shares and the objects. A route already holding one of the rows
    finishes first; one arriving later waits on the row, never the
    reverse.

    `asset_type` / `object_ids` scope the deletion (purge_tenant); both
    None means every row (teardown). Rows are still deleted by a single
    statement each; the lock just decides the order.
    """
    from superset_ownership.db import ownership_object, ownership_share

    if object_ids is not None and not object_ids:
        return 0, 0
    session.execute(ownership_lock_statement(asset_type, object_ids)).all()
    share_rows = (
        session.execute(
            ownership_share.delete().where(
                *_row_scope(ownership_share, asset_type, object_ids)
            )
        ).rowcount
        or 0
    )
    ownership_rows = (
        session.execute(
            ownership_object.delete().where(
                *_row_scope(ownership_object, asset_type, object_ids)
            )
        ).rowcount
        or 0
    )
    return share_rows, ownership_rows


def purge_tenant(
    tenant_guid: str,
    dry_run: bool = False,
    actor: Any = None,
    force_open: bool = False,
) -> dict[str, Any]:
    """Remove a tenant's ownership rows, sentinels and authorization facts.

    Enumerates the tuples that actually exist rather than assembling a
    cross product of members and objects: OpenFGA's Write is atomic, so a
    batch containing one non-existent combination deletes nothing while
    happily reporting the count it was asked for.

    AN OBJECT THAT STILL EXISTS IN SUPERSET IS LEFT ALONE. Purge has two ways
    to be wrong about a survivor and both were reached:

      strip its sentinel and delete its row  -> the object becomes readable by
          anyone holding the underlying dataset grant. A departing tenant's
          private dashboards open up to the tenants that remain.
      strip its row and keep its sentinel    -> the object is reachable only
          by a Superset admin, with no row left to repair it through the API.

    So a survivor keeps both, is reported under `retained`, and is left
    working exactly as it was. Offboarding is expected to delete the objects
    and then purge; if it purges first, nothing is damaged and the operator is
    told what is still there. `force_open=True` strips the survivors' denial
    deliberately -- for the operator who has decided those objects should
    become ordinary dataset-gated objects.

    Idempotent, and safe for a tenant that was already purged or never
    existed -- both report zeros.

    Raises ValueError for a tenant that is not a GUID v4, before anything is
    read. The check lives here rather than at the callers because there are
    two doors (the CLI and `POST /tenant/<guid>/purge`) and a purge that
    only one of them validates is a purge the other can still get wrong:
    every group id parse anchors on the GUID v4 shape, so any other tenant
    would delete the membership tuples, find none of the tenant's groups and
    report zero for them; and OpenFGA object ids are case-sensitive, so an
    upper-cased GUID would find nothing at all and report a complete-looking
    purge of zeros. The tenant is lower-cased here, as `resolve_tenant_guid`
    reports it and as the seeds write it.
    """
    tenant_guid = normalize_tenant_guid(tenant_guid)
    refusal = _requires_openfga("purge_tenant")
    if refusal:
        return refusal

    from superset import db
    from superset.models.dashboard import Dashboard
    from superset.models.slice import Slice

    from superset_ownership import audit, fga, sentinel
    from superset_ownership.db import ownership_object, ownership_share
    from superset_ownership.identity import group_belongs_to_tenant

    members = _member_guids(tenant_guid)
    objects = _tenant_object_ids(tenant_guid)

    # The tuples that genuinely exist, read back rather than inferred.
    doomed: list[dict] = []
    tenant_groups: set[str] = set()
    for guid in members:
        doomed.append(
            {
                "user": f"user:{guid}",
                "relation": "member",
                "object": f"tenant:{tenant_guid}",
            }
        )
        for grp in fga.list_objects(f"user:{guid}", "member", "group"):
            if group_belongs_to_tenant(grp, tenant_guid):
                tenant_groups.add(grp)
                doomed.append(
                    {"user": f"user:{guid}", "relation": "member", "object": grp}
                )

    # Group-to-group membership. A tenant's own seed makes the tenant
    # administrator group a member of the dashboard designer group (both ids
    # in the configured OWNERSHIP_GROUP_ID_FORMAT), a tuple whose user is a
    # group rather than one of the members walked above, so it survived the
    # purge unreported: reuse the tenant GUID, or add anyone to the surviving
    # group later, and they inherit the old role graph.
    for grp in sorted(tenant_groups):
        for t in fga.read_all(grp):
            if t not in doomed:
                doomed.append(t)
    # Objects that still exist in Superset are retained whole -- including
    # their authorization facts. Deleting the share tuples of an object this
    # same call has decided to keep would revoke live access on a surviving
    # object and leave its mirror claiming the share is still there.
    survivor_uuids: dict[str, set[str]] = {"dashboard": set(), "chart": set()}
    if not force_open:
        model_for_survivors = {"dashboard": Dashboard, "chart": Slice}
        for asset_type, uuids in objects.items():
            wanted = set(uuids)
            for obj in all_rows(model_for_survivors[asset_type]):
                u = str(getattr(obj, "uuid", "") or "")
                if u in wanted:
                    survivor_uuids[asset_type].add(u)

    for asset_type, uuids in objects.items():
        for uuid in uuids:
            if uuid in survivor_uuids[asset_type]:
                continue
            doomed.extend(fga.read_all(f"{asset_type}:{uuid}"))

    counts: dict[str, Any] = {
        "members": len(members),
        "dashboards": len(objects["dashboard"]),
        "charts": len(objects["chart"]),
        "ownership_rows": 0,
        "share_rows": 0,
        "sentinels_removed": 0,
        "tuples_found": len(doomed),
        # Named, not just counted. Reporting objects_retained: 4 beside a
        # three-name list left an operator running a dry run -- whose whole
        # purpose is learning WHICH objects survive -- to guess at the fourth.
        "objects_retained": [
            f"{a}:{u}" for a, uuids in survivor_uuids.items() for u in sorted(uuids)
        ],
    }

    session = db.session
    model_for = {"dashboard": Dashboard, "chart": Slice}
    targets: dict[str, list[int]] = {}
    for asset_type, uuids in objects.items():
        if not uuids:
            continue
        rows = (
            session.execute(
                ownership_object.select().where(
                    ownership_object.c.asset_type == asset_type,
                    ownership_object.c.object_uuid.in_(uuids),
                )
            )
            .mappings()
            .all()
        )
        targets[asset_type] = [r["object_id"] for r in rows]

    if dry_run:
        counts["dry_run"] = True
        # The destructive half has to appear in the dry run. Reporting
        # sentinels_removed: 0 while the real run would strip several hid
        # exactly the consequence an operator checks a dry run for.
        model_for_dry = {"dashboard": Dashboard, "chart": Slice}
        subject = sentinel.get_sentinel_subject()
        counts["retained"] = []
        for asset_type, uuids in survivor_uuids.items():
            for obj in all_rows(model_for_dry[asset_type]):
                if str(getattr(obj, "uuid", "") or "") in uuids:
                    counts["retained"].append(f"{asset_type}:{obj.id}")
        counts["retained"].sort()
        removable: dict[str, list[int]] = {}
        for asset_type, ids in targets.items():
            if not ids:
                continue
            survivors = {
                o.id for o in all_rows(model_for_dry[asset_type]) if o.id in set(ids)
            }
            if survivors and not force_open:
                removable[asset_type] = [i for i in ids if i not in survivors]
                continue
            removable[asset_type] = list(ids)
            for obj in all_rows(model_for_dry[asset_type]):
                if (
                    obj.id in survivors
                    and subject is not None
                    and subject in (obj.viewers or [])
                ):
                    counts["sentinels_removed"] += 1
        counts["ownership_rows"] = sum(len(v) for v in removable.values())
        counts["share_rows"] = sum(
            len(
                session.execute(
                    ownership_share.select().where(
                        ownership_share.c.asset_type == a,
                        ownership_share.c.object_id.in_(ids),
                    )
                )
                .mappings()
                .all()
            )
            for a, ids in targets.items()
            if ids
        )
        counts["tuples_deleted"] = 0
        counts["tuples_failed"] = 0
        counts["would_delete_tuples"] = len(doomed)
        return counts

    # Named from the same set objects_retained counts, so the two agree: a
    # survivor with no ownership row was counted and never named.
    counts["retained"] = []
    for asset_type, uuids in survivor_uuids.items():
        model = model_for[asset_type]
        for obj in all_rows(model):
            if str(getattr(obj, "uuid", "") or "") in uuids:
                counts["retained"].append(f"{asset_type}:{obj.id}")
    counts["retained"].sort()
    # Asset types in order, ids in order: the bulk paths all take their rows
    # in (asset_type, object_id) order, so two of them cannot cycle.
    for asset_type in sorted(targets):
        ids = sorted(targets[asset_type])
        if not ids:
            continue

        # Objects that are still in Superset. Their denial and their ownership
        # row stay together; see the docstring.
        survivors = {
            obj.id: obj for obj in all_rows(model_for[asset_type]) if obj.id in set(ids)
        }
        if survivors and not force_open:
            ids = [i for i in ids if i not in survivors]
            targets[asset_type] = ids
            if not ids:
                continue
        elif survivors and force_open:
            # Deliberate, operator-requested removal of denial: the guard
            # stands down, and the flush happens while it is down. This used
            # to work only because the ownership rows were deleted first, so
            # the guard found nothing to protect -- correctness resting on
            # flush ordering rather than on intent.
            from superset_ownership import guard

            with guard.suppressed(session):
                for obj in survivors.values():
                    before = len(getattr(obj, "viewers", []) or [])
                    sentinel.remove_sentinel(obj)
                    if len(getattr(obj, "viewers", []) or []) < before:
                        counts["sentinels_removed"] += 1
                session.flush()

        # Object rows locked first, then shares, then objects -- the share
        # routes' order; see delete_object_rows.
        share_rows, ownership_rows = delete_object_rows(session, asset_type, ids)
        counts["share_rows"] += share_rows
        counts["ownership_rows"] += ownership_rows
        for object_id in ids:
            service.invalidate(asset_type, object_id)

    result = (
        fga.delete_each(doomed)
        if doomed
        else {"deleted": 0, "already_absent": 0, "failed": 0}
    )
    counts["tuples_deleted"] = result["deleted"]
    counts["tuples_failed"] = result["failed"]

    session.commit()
    # The per-id invalidations above ran before the commit; replay them on
    # the shared layer once the deletes are real (the guard listener does
    # this for a committing session as well; the call is idempotent).
    service.replay_invalidations()
    # The most destructive operation in the module was the one audit record
    # with no actor at all, and a tenant administrator may fire it.
    audit.emit(
        audit.TENANT_PURGED,
        actor=actor,
        after={"tenant": tenant_guid, "counts": counts},
    )
    logger.info("superset_ownership: purged tenant %s -> %s", tenant_guid, counts)
    return counts


def disable(remove_rows: bool = False) -> dict[str, int]:
    """Make the feature safely switchable off.

    Strips every sentinel viewer row, which is what turns flag-off from a
    one-way door into a rollback. Ownership rows are kept by default so the
    state survives a re-enable; pass remove_rows to clear those too.

    Run before setting OWNERSHIP_ENABLED=false:
        python -c "from superset_ownership.lifecycle import disable; disable()"
    """
    from superset import db
    from superset.models.dashboard import Dashboard
    from superset.models.slice import Slice

    from superset_ownership import sentinel
    from superset_ownership.db import ownership_object, ownership_share

    counts = {"sentinels_removed": 0, "ownership_rows": 0, "share_rows": 0}

    # The guard defends the invariant against every other writer, including
    # this one -- so disable() removed each sentinel and had it re-armed inside
    # its own transaction. Standing it down here handles THIS session.
    #
    # It does not handle the web workers. Suppression is per session; the
    # workers have their own, and the ownership rows still said `private`, so
    # the first ordinary dashboard edit after a clean disable re-armed the
    # sentinel -- in exactly the window the runbook tells the operator to
    # stand in (disable, THEN flip the flag). The durable signal has to live
    # where every worker reads it: the rows. Non-public rows are parked as
    # `disabled:<visibility>`; the guard treats anything not private/shared as
    # ungoverned; enable() restores them.
    from superset_ownership import guard
    from sqlalchemy import update

    # Every ownership row, locked in (asset_type, object_id) order before
    # anything else this transaction touches -- the sentinel rows as much
    # as the two UPDATEs below. A write route holds an object's ownership
    # row and then writes its sentinel row; taken the other way round
    # here (strip the sentinels, then lock the ownership rows -- the order
    # this ran in before), a "make public" on one object could hold its
    # ownership row and wait on the sentinel row this sweep had deleted,
    # while this sweep waited on that ownership row: a cycle. Ownership
    # rows first, as the routes and enable() take them, and a route
    # already holding one finishes before this proceeds; one arriving
    # later waits on the row, never the reverse. A bulk UPDATE, too, locks
    # its rows in whatever order the scan returns them; against the
    # hard-delete hook, a backfill or a purge holding one of those rows and
    # wanting another, that is the inversion the other bulk paths avoid.
    # Rows the transaction already holds, the UPDATEs then touch freely.
    # The guard reads the rows with a plain SELECT, so it is unaffected.
    db.session.execute(ownership_lock_statement(None, None)).all()

    with guard.suppressed(db.session):
        for model in (Dashboard, Slice):
            for obj in all_rows(model):
                before = len(getattr(obj, "viewers", []) or [])
                sentinel.remove_sentinel(obj)
                if len(getattr(obj, "viewers", []) or []) < before:
                    counts["sentinels_removed"] += 1
        # Flush inside the suppression, or the next query's autoflush lands
        # outside it and the removals are undone before they reach the table.
        db.session.flush()

    # Public rows are parked too under OWNERSHIP_PUBLIC_SCOPE=tenant: they
    # are governed there (sentinel, tenant-scoped read gate), and a parked
    # row is how the read gate and the guard know the operator stripped the
    # sentinel on purpose. Under instance scope public was never governed
    # and is left alone.
    for vis in service.enforced_visibilities():
        res = db.session.execute(
            update(ownership_object)
            .where(ownership_object.c.visibility == vis)
            .values(visibility=f"disabled:{vis}")
        )
        counts["rows_parked"] = counts.get("rows_parked", 0) + (res.rowcount or 0)
    # Straight after the bulk write: everything read from here to the commit
    # is fresh and stays out of the shared cache (see service.invalidate_all).
    service.invalidate_all()

    if remove_rows:
        counts["share_rows"] = (
            db.session.execute(ownership_share.delete()).rowcount or 0
        )
        counts["ownership_rows"] = (
            db.session.execute(ownership_object.delete()).rowcount or 0
        )

    db.session.commit()
    # And once more after the commit: parking is a bulk UPDATE that does not
    # enumerate its rows, and a cached `private` served after it would have
    # the read path denying (and the guard re-arming) inside the very window
    # the runbook stands in.
    service.invalidate_all()
    logger.info("superset_ownership: disabled -> %s", counts)
    return counts


def group_share_buckets(session: Any = None) -> dict[str, list[str]]:
    """Group subjects in the `ownership_share` mirror that do not parse under
    the configured OWNERSHIP_GROUP_ID_FORMAT, sorted into two buckets, each
    distinct and sorted.

    The mirror is exactly the set of group subjects this instance has ever
    accepted, and it is readable without the authorization store. A group
    share written under one format and read under the other is
    indistinguishable from an empty tenant at every consumer (`groups_for_tenant`
    lists nothing, `_validate_subject` refuses the group as not the caller's,
    the purge finds no groups), so this is the one place a format switch on
    an instance with existing group tuples becomes visible.

    `group_id_mismatch`: the id carries a GUID v4 but does not parse. That
        is what a format switch looks like (`group:dashboard_designer_<guid>`
        read under `{tenant}_{name}`), and the remedy is to restore the
        format the tuples were written in, or to re-seed under the new one.
        Fails `check_consistency`.

    `group_untenanted`: the id carries no GUID at all (`group:eng`). Such a
        share is accepted from a caller without a tenant (a Superset admin),
        was never subject to the tenant checks, does not change when the
        format changes, and the store honours it exactly as before -- so it
        is reported, and does not fail the check. Whether admins should be
        able to share outside the tenant convention at all is a policy
        question for `_validate_subject`, not for the format detector;
        counting these as mismatches turned the deployment gate red for
        good, with a remedy that did not apply.
    """
    from sqlalchemy import select

    from superset_ownership.db import ownership_share
    from superset_ownership.identity import GUID_RE, split_group_id

    if session is None:
        from superset import db

        session = db.session
    subjects = (
        session.execute(
            select(ownership_share.c.subject)
            .where(ownership_share.c.subject.like("group:%"))
            .distinct()
        )
        .scalars()
        .all()
    )
    mismatch: set[str] = set()
    untenanted: set[str] = set()
    for subject in subjects:
        if split_group_id(subject) is not None:
            continue
        # `group:` and `#member` cannot hold a GUID, so searching the whole
        # reference asks the same question as searching the bare id.
        if GUID_RE.search(subject):
            mismatch.add(subject)
        else:
            untenanted.add(subject)
    return {
        "group_id_mismatch": sorted(mismatch),
        "group_untenanted": sorted(untenanted),
    }


def user_share_buckets(session: Any = None) -> dict[str, list[str]]:
    """User subjects in the `ownership_share` mirror spelt without the
    tenant while the account they name sits in one: `user:<member>` rows
    written before the store id carried the tenant (`user:<tenant>.<member>`).

    `user_id_mismatch`: the check asks the store under the account's
        current spelling, so such a share grants nothing while the drawer
        still lists it. The analogue of `group_id_mismatch`, and it fails
        `check_consistency` the same way. The remedy is to unshare the row
        (the delete revokes under the row's own spelling) and share again.
        A `user:<member>` row whose account is in no tenant, or names no
        account at all, is the store's spelling for that person and is not
        listed.
    """
    from sqlalchemy import select

    from superset_ownership.db import ownership_share
    from superset_ownership.identity import (
        resolve_member_guid,
        split_subject_id,
        user_for_member_guid,
    )

    if session is None:
        from superset import db

        session = db.session
    subjects = (
        session.execute(
            select(ownership_share.c.subject)
            .where(ownership_share.c.subject.like("user:%"))
            .distinct()
        )
        .scalars()
        .all()
    )
    mismatch: set[str] = set()
    for subject in subjects:
        tenant, member = split_subject_id(subject)
        if tenant or not member:
            continue
        try:
            user = user_for_member_guid(member)
            current = resolve_member_guid(user) if user is not None else None
        except Exception:  # noqa: BLE001 - a hook outage is not a mismatch
            logger.debug(
                "lifecycle: could not resolve share subject %r", subject, exc_info=True
            )
            continue
        if current and split_subject_id(current)[0]:
            mismatch.add(subject)
    return {"user_id_mismatch": sorted(mismatch)}


def group_id_mismatches(session: Any = None) -> list[str]:
    """The `group_id_mismatch` bucket of `group_share_buckets`: tenanted
    group subjects the configured format cannot parse."""
    return group_share_buckets(session)["group_id_mismatch"]


def check_consistency() -> dict[str, Any]:
    """Report objects whose stored visibility and actual denial disagree.

    Two failure modes, and they are not symmetric.

    SILENTLY OPEN: an ownership row says private or shared, but the object
    carries no sentinel -- so Superset's permissive `else` branch runs and
    anyone holding the dataset grant can open it. This is a data leak, and
    it is exactly what a plain OWNERSHIP_ENABLED=false ... =true cycle
    leaves behind if `disable()` stripped the sentinels: the rows survive
    and look authoritative while nothing enforces them.

    ORPHANED SENTINEL: the object carries a sentinel with no ownership row
    behind it -- unreachable by everyone except a Superset admin, with no
    row for the API to repair. The inverse of a leak, and just as wrong.

    Read-only. `enable()` is what fixes the first kind; the second is
    fixed by `disable()` or by re-creating the row.

    MISSING OBJECT: an ownership row for a dashboard or chart that no longer
    exists. Still reported here, and still fails the check, because the
    object side deliberately has no foreign key (revision 0003 constrains
    only the owner and the share rows; qa/reviews/decision-owner-fk.md): a
    hard delete that bypasses the flush guard leaves the row, and the row
    is what carries the uuid the store must be purged of.

    MISSING CONSTRAINT: `owner_fk_present` / `share_fk_present` say whether
    revision 0003's two foreign keys are in the database. On a database at
    or beyond 0003 a missing one is listed in `constraints_missing` and
    fails the check: 0003 can legitimately be recorded with the owner
    constraint skipped (ab_user absent when it ran, which is reachable
    only outside the CLI -- a direct `migrate.upgrade()` from an embedding
    or a custom entrypoint that creates no application first; see
    constraints.py), and the re-check that adds it runs only when
    `migrate.upgrade` runs again. Until it does, this is the signal;
    `superset ownership db upgrade` closes it. `owner_fk_validated` /
    `share_fk_validated` say whether PostgreSQL has checked the existing
    rows: a constraint added `NOT VALID` by hand is present but enforced
    for new writes only, is listed in `constraints_unvalidated` and fails
    the check until the next upgrade repairs the rows and validates it.
    Below 0003 all four are reported and nothing is required.

    Also reports `user_id_mismatch` (`user_share_buckets`): user share rows
    spelt without the tenant while the account sits in one, which grant
    nothing under the store's current spelling; fails the check, remedy is
    unshare and share again.

    Also reports the two buckets of `group_share_buckets`. `group_id_mismatch`
    (tenanted group subjects the configured group id format cannot parse)
    fails the check, because every one of them is a share the tenant checks
    silently no longer honour; the fix is to restore the format the tuples
    were written in, or to re-seed under the new one. `group_untenanted`
    (group subjects carrying no tenant at all) is reported like
    `ungoverned` and does not fail the check: the store honours those
    shares regardless of the format.

    Also reports the feature's switches, `enabled_backend`
    (OWNERSHIP_ENABLED), `enabled_ui` (FEATURE_FLAGS["OBJECT_OWNERSHIP"], the
    static dict) and `enabled_ui_runtime` (the flag through the feature-flag
    manager, hooks applied, for an anonymous caller -- what the UI reads),
    and fails the check when they disagree (`flags_agree` false): the config
    derives the flag from the setting, so a disagreement is a later config
    layer setting the flag by hand or a GET_FEATURE_FLAGS_FUNC /
    IS_FEATURE_ENABLED_FUNC hook moving it -- see flags.py for what each
    state breaks. `ui_flag_hooked` says a hook is configured at all.
    """
    from superset import db
    from superset.models.dashboard import Dashboard
    from superset.models.slice import Slice

    from superset_ownership import flags, sentinel
    from superset_ownership.db import ownership_object

    subject = sentinel.get_sentinel_subject()
    report: dict[str, Any] = {
        "silently_open": [],
        "orphaned_sentinel": [],
        "missing_object": [],
        # Issue #127: the row and the live object disagree about the uuid the
        # authorization store addresses this object by, so every tuple on it
        # describes something that no longer exists under that reference.
        "uuid_divergence": [],
    }

    # Read the columns the database actually has: a deliberate downgrade
    # below 0004 (or code deployed ahead of `superset ownership db upgrade`)
    # has no tenant column, and `check` must still answer -- reporting the
    # gap -- rather than fail on its first SELECT.
    from sqlalchemy import inspect as sa_inspect, select

    have_tenant_column = "tenant_guid" in {
        c["name"]
        for c in sa_inspect(db.session.get_bind()).get_columns("ownership_object")
    }
    columns = [
        ownership_object.c.asset_type,
        ownership_object.c.object_id,
        ownership_object.c.visibility,
        ownership_object.c.object_uuid,
    ]
    if have_tenant_column:
        columns.append(ownership_object.c.tenant_guid)
    columns.append(ownership_object.c.owner_user_id)
    rows = db.session.execute(select(*columns)).mappings().all()
    by_type: dict[str, dict[int, str]] = {"dashboard": {}, "chart": {}}
    uuid_by_type: dict[str, dict[int, Optional[str]]] = {"dashboard": {}, "chart": {}}
    tenant_by_type: dict[str, dict[int, Optional[str]]] = {"dashboard": {}, "chart": {}}
    owner_by_type: dict[str, dict[int, Optional[int]]] = {"dashboard": {}, "chart": {}}
    for r in rows:
        by_type.setdefault(r["asset_type"], {})[r["object_id"]] = r["visibility"]
        uuid_by_type.setdefault(r["asset_type"], {})[r["object_id"]] = r["object_uuid"]
        tenant_by_type.setdefault(r["asset_type"], {})[r["object_id"]] = (
            r["tenant_guid"] if have_tenant_column else None
        )
        owner_by_type.setdefault(r["asset_type"], {})[r["object_id"]] = r[
            "owner_user_id"
        ]
    owner_tenant = _owner_tenant_resolver()
    enforced = service.enforced_visibilities()
    tenant_scoped = service.public_is_tenant_scoped()
    report["public_scope"] = service.public_scope()
    if tenant_scoped and not have_tenant_column:
        # Public within the tenant cannot be decided without the column:
        # the module cannot even read its rows (`service` selects the full
        # table) until `superset ownership db upgrade` reaches 0004.
        # Reported, and fails the check under tenant scope.
        report["schema_behind"] = "0004_ownership_object_tenant"

    for asset_type, model in (("dashboard", Dashboard), ("chart", Slice)):
        wanted = by_type.get(asset_type, {})
        tenants = tenant_by_type.get(asset_type, {})
        uuids = uuid_by_type.get(asset_type, {})
        present = {o.id: o for o in all_rows(model)}
        for object_id, visibility in wanted.items():
            obj = present.get(object_id)
            if obj is None:
                report["missing_object"].append(f"{asset_type}:{object_id}")
                continue
            # The store is addressed by uuid. A row pointing at a uuid the
            # object no longer carries means every tuple on it -- owner,
            # tenant and every share -- hangs off a reference that resolves
            # to nothing (issue #127: a dashboard import can rewrite the
            # uuid of a live object in place).
            mirrored_uuid = uuids.get(object_id)
            live_uuid = str(getattr(obj, "uuid", "") or "") or None
            if mirrored_uuid and live_uuid and mirrored_uuid != live_uuid:
                report["uuid_divergence"].append(
                    {
                        "object": f"{asset_type}:{object_id}",
                        "mirrored": mirrored_uuid,
                        "live": live_uuid,
                    }
                )
            armed = subject is not None and subject in (obj.viewers or [])
            if visibility in enforced and not armed:
                report["silently_open"].append(f"{asset_type}:{object_id}")
            if visibility.startswith("disabled:"):
                report.setdefault("parked", []).append(f"{asset_type}:{object_id}")
            if visibility == "public" and tenant_scoped and not tenants.get(object_id):
                # No tenant on the row: under the fail-closed rule nobody in
                # any tenant can read it (only its owner and callers outside
                # every tenant). Surfaced so an operator can see it. When the
                # owner resolves to a tenant the row can be stamped -- the
                # object is invisible to its own tenant for no reason but a
                # missing mirror -- and that FAILS the check until
                # `backfill-tenants` (or a start with OWNERSHIP_REPAIR_ON_START)
                # fills it in. An owner outside every tenant, or no owner, is
                # informational: nothing can stamp it, and it is readable by
                # exactly the callers it should be.
                ref = f"{asset_type}:{object_id}"
                report.setdefault("untenanted_public", []).append(ref)
                if owner_tenant(owner_by_type.get(asset_type, {}).get(object_id)):
                    report.setdefault("untenanted_public_repairable", []).append(ref)
        for object_id, obj in present.items():
            armed = subject is not None and subject in (obj.viewers or [])
            if armed and object_id not in wanted:
                report["orphaned_sentinel"].append(f"{asset_type}:{object_id}")
            elif object_id not in wanted:
                # No ownership row at all. This is the expunge-after-flush gap
                # and the pre-backfill state: the object is treated as public,
                # which is the safe default, so it does NOT fail the check --
                # but it is surfaced so an operator can see a backfill is due
                # rather than it being invisible. `backfill.run()` gives every
                # such object a public row.
                report.setdefault("ungoverned", []).append(f"{asset_type}:{object_id}")

    # The outbox is part of consistency: a dead row is an authorization
    # write the store never received (a revocation the read path is denying
    # by hand, or a grant that never landed), and pending work older than
    # outbox.STALL_AFTER means nothing is draining it. Both fail the check,
    # so the scheduled run of this report is the control for the outbox too,
    # not only the sentinels. A failure to READ the outbox (table not yet
    # migrated: code deployed ahead of `superset ownership db upgrade`) is
    # reported, and fails the check, but never takes the sentinel scan above
    # down with it.
    from superset_ownership import outbox

    outbox_failed = False
    if outbox.enabled():
        try:
            ob = outbox.status()
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            report["outbox"] = {"error": f"{exc.__class__.__name__}: {str(exc)[:200]}"}
            outbox_failed = True
        else:
            report["outbox"] = {
                k: ob[k]
                for k in (
                    "pending",
                    "claimed",
                    "dead",
                    "blocked",
                    "stalled",
                    "oldest_pending",
                    "oldest_dead",
                    "last_delivered",
                )
            }
            outbox_failed = bool(ob["dead"] or ob["stalled"])

    report.update(group_share_buckets(db.session))
    # One identity lookup per DISTINCT bare-GUID user subject in the mirror
    # (none on a store written under the current shapes): bounded by the
    # number of people ever shared with under the old spelling, not by rows.
    report.update(user_share_buckets(db.session))
    report.update(flags.report())
    report.update(constraint_status(db.session))
    report["plugins"] = _describe_plugins()

    # The dashboard-tile access marker is installed at runtime by wrapping
    # one stock method (dashboard_patch.install, from the ON mutator). When
    # the stock method no longer matches the package's pins, this command is
    # allowed to boot without the wrap precisely so it can say so here.
    from superset_ownership import dashboard_patch

    report["dashboard_chart_patch_installed"] = dashboard_patch.installed()

    # missing_object counts against ok. It did not, so the scheduled check
    # nominated as the control for the guard's blind spots reported healthy
    # while accumulating rows for objects that do not exist.
    report["ok"] = not (
        report["silently_open"]
        or report["orphaned_sentinel"]
        or report["missing_object"]
        or report["uuid_divergence"]
        or report["group_id_mismatch"]
        or report["user_id_mismatch"]
        or report["constraints_missing"]
        or report["constraints_unvalidated"]
        or report.get("constraints_error")
        or outbox_failed
        or not report["flags_agree"]
        or not report["dashboard_chart_patch_installed"]
        or bool(report.get("schema_behind"))
        or bool(report.get("untenanted_public_repairable"))
    )
    return report


def _owner_tenant_resolver():
    """`owner_user_id -> tenant GUID or None`, memoised per owner for one
    `check_consistency` run: resolving a tenant reads roles (or calls the
    OWNERSHIP_TENANT_GUID hook), and one owner can hold many objects."""
    from superset import security_manager

    from superset_ownership.identity import resolve_tenant_guid

    memo: dict[int, Optional[str]] = {}

    def resolve(owner_user_id: Optional[int]) -> Optional[str]:
        if owner_user_id is None:
            return None
        if owner_user_id not in memo:
            owner = security_manager.get_user_by_id(owner_user_id)
            try:
                memo[owner_user_id] = (
                    service.normalize_tenant(resolve_tenant_guid(owner))
                    if owner is not None
                    else None
                )
            except Exception:  # noqa: BLE001 - a resolver failure is "unknown"
                memo[owner_user_id] = None
        return memo[owner_user_id]

    return resolve


def constraint_status(session: Any) -> dict[str, Any]:
    """The MISSING CONSTRAINT part of `check_consistency`: inspect the
    database for revision 0003's two foreign keys and, when the chain is at
    or beyond 0003, list the absent ones (`constraints_missing`) and the
    ones present but not yet validated (`constraints_unvalidated`: a
    PostgreSQL constraint added ``NOT VALID`` by hand, enforced for new
    writes only until the next upgrade validates it). A failure to inspect
    is reported (`constraints_error`, which fails the check like an
    unreadable outbox does), never raised: the rest of the report must
    still come out."""
    from superset_ownership import constraints

    try:
        fk = constraints.status(session.get_bind())
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        return {
            "owner_fk_present": None,
            "share_fk_present": None,
            "owner_fk_validated": None,
            "share_fk_validated": None,
            "constraints_missing": [],
            "constraints_unvalidated": [],
            "constraints_error": f"{exc.__class__.__name__}: {str(exc)[:200]}",
        }
    missing = []
    unvalidated = []
    if fk["required"]:
        for name, present, validated in (
            (constraints.OWNER_FK, fk["owner_fk_present"], fk["owner_fk_validated"]),
            (constraints.SHARE_FK, fk["share_fk_present"], fk["share_fk_validated"]),
        ):
            if not present:
                missing.append(name)
            elif not validated:
                unvalidated.append(name)
    return {
        "owner_fk_present": fk["owner_fk_present"],
        "share_fk_present": fk["share_fk_present"],
        "owner_fk_validated": fk["owner_fk_validated"],
        "share_fk_validated": fk["share_fk_validated"],
        "constraints_missing": missing,
        "constraints_unvalidated": unvalidated,
    }


def enable(dry_run: bool = False) -> dict[str, Any]:
    """Re-arm denial for every non-public ownership row.

    The counterpart to `disable()`, and the reason turning the flag back on
    is not just an env change. `disable()` strips sentinels to make
    flag-off a real rollback; without this, flipping the flag back on
    leaves every private and shared object standing wide open while the API
    and the UI both report it as private. The rows are not the enforcement
    -- the sentinel is -- so the rows have to be replayed onto the objects.

    Run after setting OWNERSHIP_ENABLED=true:
        python -c "from superset_ownership.lifecycle import enable; print(enable())"

    Idempotent: an object that already carries its sentinel is counted as
    already_armed and left alone.
    """
    from superset import db
    from superset.models.dashboard import Dashboard
    from superset.models.slice import Slice

    from superset_ownership import sentinel
    from superset_ownership.db import ownership_object

    from sqlalchemy import update

    subject = sentinel.get_sentinel_subject()
    counts: dict[str, Any] = {
        "armed": 0,
        "already_armed": 0,
        "public_skipped": 0,
        "missing_object": 0,
    }

    # Rows parked by disable() come back first, so the flush below finds them
    # governed and the guard agrees with what this function is about to do.
    # Locked in (asset_type, object_id) order first, as disable() does,
    # so the two UPDATEs touch rows this transaction already holds rather
    # than locking them in scan order against another bulk path.
    db.session.execute(ownership_lock_statement(None, None)).all()
    # `disabled:public` rows exist only from a disable() run under tenant
    # scope; restoring them is right under either scope (a public row is
    # just ungoverned again under instance scope), so all three are tried.
    for vis in ("private", "shared", "public"):
        res = db.session.execute(
            update(ownership_object)
            .where(ownership_object.c.visibility == f"disabled:{vis}")
            .values(visibility=vis)
        )
        counts["rows_restored"] = counts.get("rows_restored", 0) + (res.rowcount or 0)
    # Straight after the bulk write, for the same reason as in disable().
    service.invalidate_all()

    enforced = service.enforced_visibilities()
    rows = (
        db.session.execute(
            ownership_object.select().where(ownership_object.c.visibility.in_(enforced))
        )
        .mappings()
        .all()
    )

    for asset_type, model in (("dashboard", Dashboard), ("chart", Slice)):
        ids = [r["object_id"] for r in rows if r["asset_type"] == asset_type]
        if not ids:
            continue
        # all_rows, not a plain query: disable() and check_consistency() both
        # sweep soft-deleted objects, so enable() must too, or a private object
        # sitting in the trash comes back with its row restored but no sentinel
        # replayed -- silently open the moment it is restored.
        found = {o.id: o for o in all_rows(model) if o.id in set(ids)}
        counts["missing_object"] += len(ids) - len(found)
        for obj in found.values():
            if subject is not None and subject in (obj.viewers or []):
                counts["already_armed"] += 1
                continue
            if not dry_run:
                sentinel.add_sentinel(obj)
            counts["armed"] += 1

    counts["public_skipped"] = (
        0
        if "public" in enforced
        else db.session.execute(
            ownership_object.select().where(ownership_object.c.visibility == "public")
        ).rowcount
        or 0
    )

    if dry_run:
        counts["dry_run"] = True
        db.session.rollback()
        # The restore above was read back (the sentinel replay resolves rows)
        # before being rolled back; drop whatever that populated.
        service.invalidate_all()
        return counts

    db.session.commit()
    service.invalidate_all()
    logger.info("superset_ownership: enabled -> %s", counts)
    return counts


def startup_check(repair: bool = False) -> dict[str, Any]:
    """Called at module init. Refuses to stay quiet about a silent leak.

    Logs at ERROR when private or shared objects are reachable by anyone
    with the dataset grant, because that state is indistinguishable from
    working: the API reports "private", the UI draws a lock, and the object
    opens for everybody. Set OWNERSHIP_REPAIR_ON_START to re-arm instead of
    only reporting.
    """
    from superset_ownership.identity import group_id_format

    try:
        report = check_consistency()
    except Exception:
        logger.exception("superset_ownership: startup consistency check failed")
        return {"ok": None}

    if report.get("group_untenanted"):
        # Informational: these shares are honoured as written and do not
        # fail the check, but an operator reading the boot log should be
        # able to see that shares exist outside the tenant convention.
        logger.info(
            "superset_ownership: %d group subject(s) in the share mirror are "
            "outside the tenant convention (no tenant in the id; accepted from a "
            "caller without a tenant): %s",
            len(report["group_untenanted"]),
            report["group_untenanted"][:20],
        )

    if report["ok"]:
        logger.info("superset_ownership: startup check ok")
        return report

    if report["silently_open"]:
        logger.error(
            "superset_ownership: %d object(s) are recorded non-public but carry no "
            "sentinel and are therefore readable by anyone with the dataset grant: %s. "
            "Run superset_ownership.lifecycle.enable() (or set "
            "OWNERSHIP_REPAIR_ON_START=true).",
            len(report["silently_open"]),
            report["silently_open"][:20],
        )
    if report["orphaned_sentinel"]:
        logger.warning(
            "superset_ownership: %d object(s) carry a sentinel with no ownership row -- "
            "reachable only by an administrator: %s",
            len(report["orphaned_sentinel"]),
            report["orphaned_sentinel"][:20],
        )
    if report.get("group_id_mismatch"):
        logger.error(
            "superset_ownership: %d tenanted group subject(s) in the share mirror do "
            "not parse under OWNERSHIP_GROUP_ID_FORMAT=%r; those shares are no longer "
            "honoured by the tenant checks. Restore the format they were written in, "
            "or re-seed the groups under the configured one: %s",
            len(report["group_id_mismatch"]),
            group_id_format(),
            report["group_id_mismatch"][:20],
        )
    if report.get("uuid_divergence"):
        logger.error(
            "superset_ownership: %d ownership row(s) name a uuid the live object no "
            "longer carries, so every tuple on them -- owner, tenant and every share "
            "-- hangs off a reference that resolves to nothing. Run `superset "
            "ownership reconcile --write` to follow them: %s",
            len(report["uuid_divergence"]),
            report["uuid_divergence"][:20],
        )
    if report.get("user_id_mismatch"):
        logger.error(
            "superset_ownership: %d user subject(s) in the share mirror are spelt "
            "without the tenant while the account is in one; those shares grant "
            "nothing under the store's current spelling. Unshare each and share "
            "again: %s",
            len(report["user_id_mismatch"]),
            report["user_id_mismatch"][:20],
        )
    if not report.get("flags_agree", True):
        # The full diagnosis (which state, what it breaks, what to remove)
        # is the WARNING flags.warn_if_flags_disagree wrote from the app
        # mutator a moment before this ran; this line says why the check
        # is not ok without repeating it.
        logger.warning(
            "superset_ownership: startup check not ok: OWNERSHIP_ENABLED=%s but "
            "FEATURE_FLAGS[%r]=%s, at runtime %s (see the superset_ownership.flags "
            "warning above)",
            report["enabled_backend"],
            "OBJECT_OWNERSHIP",
            report["enabled_ui"],
            report.get("enabled_ui_runtime"),
        )
    _log_missing_constraints(report)
    _log_untenanted_public(report)
    return _repair(report) if repair else report


def _repair(report: dict[str, Any]) -> dict[str, Any]:
    """`startup_check(repair=True)`: the mirror pass for untenanted public
    rows, then `enable()` for silently open ones. Each re-reads, so the
    returned report describes the state the repair LEAVES."""
    if report.get("untenanted_public_repairable"):
        report = _repair_tenants()

    if report["silently_open"]:
        # This runs once per web worker, so two of them can repair the same
        # objects at the same moment. enable() converges either way, but the
        # loser of the race hits the viewers unique constraint; that is the
        # race being lost, not a failure to repair.
        from superset import db

        try:
            logger.warning("superset_ownership: repairing -> %s", enable())
        except Exception:
            db.session.rollback()
            logger.warning(
                "superset_ownership: another worker is repairing concurrently; "
                "re-reading instead",
                exc_info=True,
            )
        # Re-read, so the returned report describes the state the check LEAVES
        # rather than the one it found. A monitor consuming the old return
        # value alarmed on a system that had just been fixed.
        report = check_consistency()
        report["repaired"] = True
    return report


def _log_missing_constraints(report: dict[str, Any]) -> None:
    """The boot-log line for `constraints_missing`, logged at every boot
    until the next `superset ownership db upgrade` (or a boot with
    auto-migrate on) re-checks and adds the constraint; the deferral's own
    WARNING was logged once, at the time."""
    missing = report.get("constraints_missing") or []
    if missing:
        logger.warning(
            "superset_ownership: the ownership chain is at revision 0003 or later "
            "but %s %s missing from the database (ab_user did not exist when the "
            "chain ran, or the chain was stamped); a deleted user's objects are "
            "not un-owned by the database until it is added. Run `superset "
            "ownership db upgrade`.",
            " and ".join(missing),
            "is" if len(missing) == 1 else "are",
        )
    unvalidated = report.get("constraints_unvalidated") or []
    if unvalidated:
        logger.warning(
            "superset_ownership: %s %s present but NOT VALID (added by hand and "
            "not yet validated; enforced for new writes only). Run `superset "
            "ownership db upgrade`, which repairs the existing rows and runs "
            "VALIDATE CONSTRAINT.",
            " and ".join(unvalidated),
            "is" if len(unvalidated) == 1 else "are",
        )
    if report.get("constraints_error"):
        logger.warning(
            "superset_ownership: could not inspect the database for revision 0003's "
            "constraints: %s",
            report["constraints_error"],
        )


def _log_untenanted_public(report: dict[str, Any]) -> None:
    if not report.get("untenanted_public_repairable"):
        return
    logger.error(
        "superset_ownership: %d public object(s) carry no tenant on their row "
        "although their owner belongs to one; until the mirror is filled in "
        "nobody in that tenant can read them: %s. Run `superset ownership "
        "backfill-tenants` (or set OWNERSHIP_REPAIR_ON_START=true).",
        len(report["untenanted_public_repairable"]),
        report["untenanted_public_repairable"][:20],
    )


def _repair_tenants() -> dict[str, Any]:
    """`startup_check(repair=True)`'s mirror pass: stamps the store where it
    has no tenant and fills the row from the store -- idempotent, so the
    web workers that run this concurrently converge. Returns a fresh
    report of the state the repair leaves."""
    from superset import db

    try:
        logger.warning(
            "superset_ownership: repairing tenants -> %s", backfill_object_tenants()
        )
    except Exception:
        db.session.rollback()
        logger.warning(
            "superset_ownership: tenant repair failed; re-reading instead",
            exc_info=True,
        )
    report = check_consistency()
    report["repaired"] = True
    return report


def backfill_object_tenants() -> dict[str, Any]:
    """Give pre-existing objects the tenant tuple new objects now get.

    Objects created before the tenant tuple was written at creation time
    have no tenant, and an object with no tenant is one no tenant
    administrator can act on -- the scoping check has nothing to compare
    against and correctly refuses. The tenant is taken from the recorded
    owner, which is the same source creation uses.

    Skips rows with no owner. Objects that already carry a tenant in the
    store are not rewritten there, but the row's mirror of it is filled in
    when missing. Safe to re-run.
    """
    from superset import db, security_manager

    from superset_ownership.authz import get_authorizer
    from superset_ownership.db import ownership_object
    from superset_ownership.identity import resolve_tenant_guid

    counts: dict[str, Any] = {
        "written": 0,
        "already_set": 0,
        "no_owner": 0,
        "no_tenant": 0,
        "failed": 0,
    }
    # Every row, public included: under OWNERSHIP_PUBLIC_SCOPE=tenant the
    # tenant is what "public" is scoped by, and the row's mirror of it
    # (revision 0004) is filled here for objects stamped in the store
    # before the column existed.
    rows = db.session.execute(ownership_object.select()).mappings().all()

    for r in rows:
        # The store addresses the object by uuid. A prototype-era row that
        # never recorded it is stamped from the object's own uuid, so it is
        # not left untenanted (and `check` pointing here is not a dead end).
        uuid = r["object_uuid"] or _object_uuid(r["asset_type"], r["object_id"])
        if not uuid:
            counts["no_uuid"] = counts.get("no_uuid", 0) + 1
            continue
        stored = service.normalize_tenant(
            get_authorizer().object_tenant(r["asset_type"], uuid)
        )
        if stored:
            counts["already_set"] += 1
            if r["tenant_guid"] != stored:
                service.set_row_tenant(
                    r["asset_type"], uuid, stored, object_id=r["object_id"]
                )
                counts["mirrored"] = counts.get("mirrored", 0) + 1
            continue
        if r["owner_user_id"] is None:
            counts["no_owner"] += 1
            continue
        owner = security_manager.get_user_by_id(r["owner_user_id"])
        tenant_guid = service.normalize_tenant(
            resolve_tenant_guid(owner) if owner is not None else None
        )
        if not tenant_guid:
            counts["no_tenant"] += 1
            continue
        if get_authorizer().set_object_tenant(r["asset_type"], uuid, tenant_guid):
            service.set_row_tenant(
                r["asset_type"], uuid, tenant_guid, object_id=r["object_id"]
            )
            counts["written"] += 1
        else:
            counts["failed"] += 1

    db.session.commit()
    service.invalidate_all()
    logger.info("superset_ownership: backfilled object tenants -> %s", counts)
    return counts


def _object_uuid(asset_type: str, object_id: int) -> Optional[str]:
    from superset import db
    from superset.models.dashboard import Dashboard
    from superset.models.slice import Slice

    model = Dashboard if asset_type == "dashboard" else Slice
    obj = db.session.get(model, int(object_id))
    uuid = getattr(obj, "uuid", None) if obj is not None else None
    return str(uuid) if uuid else None


def _repoint_diverged_rows(dry_run: bool = True) -> dict[str, Any]:
    """Follow objects whose uuid moved while nothing was watching.

    `guard._follow_uuid_changes` catches this as it happens (issue #127), so
    this is for the rows an instance carries from before that existed -- the
    ones `check` reports as `uuid_divergence`. Same hook as the live path
    (`service.repoint_object_uuid`): purge the dead reference, then re-queue
    the owner, the tenant and every share under the live uuid.
    """
    from superset.models.dashboard import Dashboard
    from superset.models.slice import Slice

    from superset_ownership.db import ownership_object

    from superset import db

    rows = db.session.execute(ownership_object.select()).mappings().all()
    live_uuid: dict[tuple[str, int], Optional[str]] = {}
    for asset_type, model in (("dashboard", Dashboard), ("chart", Slice)):
        ids = {r["object_id"] for r in rows if r["asset_type"] == asset_type}
        if not ids:
            continue
        for obj in all_rows(model):
            if obj.id in ids:
                live_uuid[(asset_type, obj.id)] = (
                    str(getattr(obj, "uuid", "") or "") or None
                )

    diverged = [
        r
        for r in rows
        if r["object_uuid"]
        and live_uuid.get((r["asset_type"], r["object_id"])) is not None
        and str(r["object_uuid"]) != live_uuid[(r["asset_type"], r["object_id"])]
    ]
    report: dict[str, Any] = {
        "rows_with_a_diverged_uuid": [
            f"{r['asset_type']}:{r['object_id']}" for r in diverged
        ]
    }
    if not diverged or dry_run:
        return report

    for r in sorted(diverged, key=lambda r: (r["asset_type"], r["object_id"])):
        service.repoint_object_uuid(
            r["asset_type"],
            r["object_id"],
            str(r["object_uuid"]),
            live_uuid[(r["asset_type"], r["object_id"])],
        )
    db.session.commit()
    report["rows_repointed"] = len(diverged)
    return report


def rows_without_object(dry_run: bool = True) -> dict[str, Any]:
    """Remove ownership rows whose OBJECT no longer exists.

    Until the Core-delete listener (`guard._before_core_delete`, issue #120)
    every hard delete -- the purge route and the retention sweep both -- left
    the ownership row, its shares and every one of the object's tuples behind,
    including live `viewer` grants, and `check` went red with nothing able to
    clear it: the API has no route for it, `prune` only touches delivered
    outbox rows, and this command's store-to-row direction could not see it.
    New orphans should no longer appear; this repairs the ones an instance
    already has. Called from `reconcile`, and importable on its own for an
    install whose store is not OpenFGA (`reconcile` requires it; this does
    not).

    `all_rows`, not a plain query: a SOFT-deleted object still exists and
    keeps its ownership, because restoring it from the trash has to restore
    what it was. Only an object missing from that sweep is really gone.
    """
    from superset import db
    from superset.models.dashboard import Dashboard
    from superset.models.slice import Slice

    from superset_ownership.db import ownership_object
    from superset_ownership.hooks import after_asset_delete

    rows = db.session.execute(ownership_object.select()).mappings().all()
    alive: dict[str, set] = {}
    blind: list[str] = []
    for asset_type, model in (("dashboard", Dashboard), ("chart", Slice)):
        ids = {r["object_id"] for r in rows if r["asset_type"] == asset_type}
        if not ids:
            continue
        # "Absent from all_rows" only means "gone" when all_rows can see
        # everything. If it cannot, a soft-deleted object -- which still
        # exists and must keep its ownership -- would read as an orphan and
        # be destroyed along with its live grants (review round 1). Skip the
        # asset type entirely and say why.
        if not sweeps_soft_deleted(model):
            blind.append(asset_type)
            continue
        alive[asset_type] = {o.id for o in all_rows(model) if o.id in ids}

    orphans = [
        r
        for r in rows
        if r["asset_type"] not in blind
        and r["object_id"] not in alive.get(r["asset_type"], set())
    ]
    report: dict[str, Any] = {
        "rows_without_object": [f"{r['asset_type']}:{r['object_id']}" for r in orphans]
    }
    if blind:
        report["rows_without_object_skipped"] = sorted(blind)
        logger.error(
            "superset_ownership: cannot tell a deleted %s from a soft-deleted one on "
            "this build (the documented soft-delete bypass is not importable); their "
            "ownership rows were NOT swept",
            " or ".join(sorted(blind)),
        )
    if not orphans or dry_run:
        return report

    # The same hook the delete path uses, so the repair and the live path
    # cannot drift: local rows first, then ONE purge_object intent per object,
    # which is what takes the tuples with it. Ordered by (asset_type,
    # object_id) like every other bulk path here.
    for r in sorted(orphans, key=lambda r: (r["asset_type"], r["object_id"])):
        after_asset_delete(r["asset_type"], r["object_id"], r["object_uuid"])
    db.session.commit()
    report["rows_removed"] = len(orphans)
    return report


def _row_still_there(snapshot_row) -> bool:
    """Is the ownership row this snapshot came from still there, unchanged?

    `reconcile` reads every row once and then spends a network round trip
    per row talking to the store. A hard delete landing in that window
    removes the row and purges the object's tuples -- and a repair written
    afterwards from the stale snapshot puts a live `owner` grant back under
    a reference nothing names any more (review round 3). Compared on the
    uuid as well as existence, so a row re-pointed under us is skipped too
    rather than repaired against the reference it has just left.

    And on the OWNER, because that is what the repair writes (review round
    4). A transfer landing in the same window leaves the row in place with
    a new owner, while `expected` still names the old one: the repair then
    wrote the previous owner's `owner` tuple back and deleted the new
    owner's, handing access back to the person just transferred away from
    -- with `reconcile` reporting `remaining: {}` and `check` reporting ok.
    The rule is the general one: re-read everything the write is derived
    from, not merely enough to prove the row exists.
    """
    from superset import db

    from superset_ownership.db import ownership_object

    current = (
        db.session.execute(
            ownership_object.select().where(
                ownership_object.c.asset_type == snapshot_row["asset_type"],
                ownership_object.c.object_id == snapshot_row["object_id"],
            )
        )
        .mappings()
        .first()
    )
    if current is None:
        return False
    if str(current["object_uuid"] or "") != str(snapshot_row["object_uuid"] or ""):
        return False
    return current["owner_user_id"] == snapshot_row["owner_user_id"]


def reconcile(dry_run: bool = True) -> dict[str, Any]:
    """Bring the authorization store's OWNER tuples back in line with the rows.

    Ownership and visibility are decided from `ownership_object` in Superset's
    metadata database. The store holds the relations that share decisions are
    made from, plus an `owner` tuple and a `tenant` tuple that exist so a
    tenant purge and a tenant administrator's scoping can enumerate objects
    without inferring anything.

    That owner tuple is written and, outside purge, never read -- which means
    nothing noticed when it disagreed with the row. On this stack it disagreed
    on three of seven objects, including two with no owner tuple at all and
    two carrying tuples for users and groups that do not exist anywhere.

    Rewrites the owner tuple from the row, removes owner tuples that name
    anyone else, and reports share tuples with no mirror row behind them
    (those are NOT deleted: the store is authoritative for shares, so a tuple
    the mirror has lost is the mirror's problem to be looked at, not the
    store's to be silently discarded).

    Defaults to a dry run: pass dry_run=False to write.
    """
    refusal = _requires_openfga("reconcile")
    if refusal:
        return refusal

    from superset import db

    from superset_ownership import fga, outbox
    from superset_ownership.db import ownership_object, ownership_share
    from superset_ownership.identity import member_ref

    report: dict[str, Any] = {
        "checked": 0,
        "owner_tuple_missing": [],
        "owner_tuple_wrong": [],
        "owner_tuple_written": 0,
        "owner_tuple_removed": 0,
        "share_tuples_without_row": [],
        # Rows that changed while this command was talking to the store: a
        # transfer or a delete landing in the window between the snapshot and
        # the write. Nothing is wrong and nothing is needed -- a later run
        # sees the settled state -- but an operator reading the JSON should
        # be able to tell "I declined to repair this" from "I repaired it".
        "skipped_changed_under_us": [],
        "dry_run": dry_run,
    }

    # A row whose uuid no longer matches the live object addresses a store
    # reference that resolves to nothing, so every tuple this function would
    # write from it lands on the dead reference and repairs nothing -- while
    # reporting `owner_tuple_written` as though it had (review round 1).
    # Follow the object FIRST, with the same hook the live path uses, so the
    # loop below works from rows that name something real.
    report.update(_repoint_diverged_rows(dry_run=dry_run))

    rows = db.session.execute(ownership_object.select()).mappings().all()
    for r in rows:
        if not r["object_uuid"]:
            continue
        report["checked"] += 1
        obj = f"{r['asset_type']}:{r['object_uuid']}"
        expected = member_ref(r["owner_user_id"]) if r["owner_user_id"] else None
        actual = [t["user"] for t in fga.read_all(obj, "owner")]

        if expected and expected not in actual:
            report["owner_tuple_missing" if not actual else "owner_tuple_wrong"].append(
                {"object": obj, "expected": expected, "actual": actual}
            )
            # Nit (review round 2, PR #100): through the outbox FACADE, not
            # `fga.write_tuple` directly -- the same CLI tail the #75
            # comment documents for the row cache applied here too: a
            # direct `fga` call invalidates nothing, so a stale
            # "owner_tuple_wrong" object's list_objects entry (issue #82)
            # kept naming the OLD owner as a viewer for up to a TTL after
            # the repair. `outbox.write_tuple` invalidates the subject's
            # list_objects entry before writing, and enqueues (or writes
            # inline with the outbox disabled) exactly like every other
            # write in this package.
            # The snapshot `rows` was read once, up front, and every
            # iteration since has made a network call to the store. An object
            # deleted in that window has had its row and its tuples removed
            # already -- writing the owner tuple now files a live `owner`
            # grant under a reference nothing names any more, and neither
            # `check` nor `reconcile` can ever see it again, because both
            # enumerate FROM `ownership_object` (review round 3). Re-read
            # before writing; the row is the authority, not the snapshot.
            if not dry_run and _row_still_there(r):
                if outbox.write_tuple(expected, "owner", obj):
                    report["owner_tuple_written"] += 1
            elif not dry_run:
                report["skipped_changed_under_us"].append(obj)
        for user in actual:
            if user != expected:
                if dry_run:
                    report.setdefault("owner_tuple_to_remove", []).append(
                        {"object": obj, "user": user}
                    )
                elif not _row_still_there(r):
                    report["skipped_changed_under_us"].append(obj)
                elif outbox.delete_tuple(user, "owner", obj):
                    report["owner_tuple_removed"] += 1

        mirrored = {
            sh["subject"]
            for sh in db.session.execute(
                ownership_share.select().where(
                    ownership_share.c.asset_type == r["asset_type"],
                    ownership_share.c.object_id == r["object_id"],
                )
            )
            .mappings()
            .all()
        }
        for t in fga.read_all(obj):
            if t["relation"] in ("owner", "tenant"):
                continue
            if t["user"] not in mirrored:
                report["share_tuples_without_row"].append(
                    {"object": obj, "user": t["user"], "relation": t["relation"]}
                )

    if not dry_run:
        # The outbox facade (above) does not commit its own enqueued rows
        # (`outbox.enqueue`'s docstring: it must ride the caller's
        # transaction). Nothing else in this loop writes a row to piggyback
        # the commit on, so this command commits explicitly -- otherwise an
        # enqueued repair would sit uncommitted, invisible to any drain,
        # for as long as this CLI process's session stays open.
        db.session.commit()

    # Mirror rows whose object has no ownership row at all. purge builds its
    # targets from ownership_object, so these survive a tenant purge; and the
    # store->row direction above cannot see them either. Since revision 0003
    # the share -> object foreign key cascades these away on a database that
    # enforces it (PostgreSQL always; SQLite only with PRAGMA foreign_keys),
    # so on a migrated install this sweep finds nothing -- kept for installs
    # not yet at 0003 and for SQLite.
    governed_keys = {(r["asset_type"], r["object_id"]) for r in rows}
    orphan_shares = [
        r
        for r in db.session.execute(ownership_share.select()).mappings().all()
        if (r["asset_type"], r["object_id"]) not in governed_keys
    ]
    report["share_rows_without_object"] = [
        f"{r['asset_type']}:{r['object_id']}:{r['subject']}" for r in orphan_shares
    ]
    if orphan_shares and not dry_run:
        for r in orphan_shares:
            db.session.execute(
                ownership_share.delete().where(ownership_share.c.id == r["id"])
            )
        db.session.commit()
        for r in orphan_shares:
            service.invalidate(r["asset_type"], r["object_id"])
        report["share_rows_removed"] = len(orphan_shares)

    report.update(rows_without_object(dry_run=dry_run))

    logger.info(
        "superset_ownership: reconcile -> %s",
        {k: (len(v) if isinstance(v, list) else v) for k, v in report.items()},
    )
    if not dry_run:
        # Report the state this leaves, not the one it found -- the same
        # correction already made to startup_check. A caller that saw
        # owner_tuple_wrong: 1 after a successful --write had no way to tell
        # repaired from still-broken. With the outbox ENABLED, the owner
        # tuple write/delete above is enqueued, not immediate -- like every
        # other write in this package -- so this recheck's own `fga.
        # read_all` may still see the pre-repair tuple until the next
        # drain, exactly as `remaining: owner_tuple_wrong` would after any
        # other queued write; it is not this command failing to fix it.
        after = reconcile(dry_run=True)
        report["remaining"] = {
            k: v
            for k, v in after.items()
            if k not in ("dry_run", "checked") and (v if isinstance(v, int) else len(v))
        }
    return report


def _objects_still_denied() -> list[str]:
    """Objects that still carry the sentinel, ownership rows or not."""
    from superset.models.dashboard import Dashboard
    from superset.models.slice import Slice

    from superset_ownership import sentinel

    subject = sentinel.get_sentinel_subject()
    if subject is None:
        return []
    out = []
    for asset_type, model in (("dashboard", Dashboard), ("chart", Slice)):
        for obj in all_rows(model):
            if subject in (getattr(obj, "viewers", []) or []):
                out.append(f"{asset_type}:{obj.id}")
    return out


def teardown(confirm: bool = False) -> dict[str, Any]:
    """Remove every trace of the feature: sentinels, rows, tables, the
    chain's version table, role.

    The tables are the module's own Alembic chain's (`superset ownership db
    upgrade`), so the uninstall drops its version table too: with the
    tables gone and `alembic_version_ownership` still at head, the next
    `db upgrade` was a no-op and the feature could not be installed again
    (found on the PCS-10243 manual round after PR #113).

    Order matters. `disable()` first, so no object is left carrying a denial
    nothing can interpret; then the rows; then the tables; then the role.
    The result reports each step (`disable`, `share_rows`, `ownership_rows`,
    `tables_dropped`, `chain_forgotten`, `role_removed`). "Every trace"
    holds once the module is out of the configuration: a next boot with
    it still configured and OWNERSHIP_AUTO_MIGRATE on recreates the empty
    tables, which is the reinstall this now makes possible.

    Requires confirm=True: it is irreversible, and every ownership and share
    record goes with it.
    """
    if not confirm:
        return {"error": "teardown is irreversible; call with confirm=True"}

    from superset import db, security_manager

    from superset_ownership import sentinel
    from superset_ownership.db import metadata

    # Strip denial, keep the rows, verify -- and only THEN delete the rows.
    # The previous order (disable with remove_rows, then verify) meant the
    # abort path left objects still carrying a sentinel with no row left to
    # explain or replay them: enable() could not repair it.
    result: dict[str, Any] = {"disable": disable(remove_rows=False)}

    # Verify before the point of no return. teardown dropped both tables and
    # the sentinel role while seven objects still carried a sentinel -- leaving
    # them reachable only by a Superset admin with nothing left in the instance
    # able to diagnose or repair it, not even `check`.
    leftover = _objects_still_denied()
    if leftover:
        result["error"] = (
            "aborted: denial is still in place on "
            f"{len(leftover)} object(s) and dropping the tables now would "
            "strand them permanently"
        )
        result["still_denied"] = leftover
        logger.error("superset_ownership: teardown aborted -> %s", result)
        return result

    # Every row, in the share routes' lock order (object rows first, then
    # shares, then objects); see delete_object_rows.
    result["share_rows"], result["ownership_rows"] = delete_object_rows(db.session)
    db.session.commit()
    service.invalidate_all()

    metadata.drop_all(bind=db.session.get_bind(), checkfirst=True)
    result["tables_dropped"] = sorted(metadata.tables)
    from superset_ownership import migrate

    result["chain_forgotten"] = migrate.forget(bind=db.session.get_bind())

    # The guard refuses to let anything delete the sentinel role -- see
    # guard._protect_the_sentinel. teardown is the sanctioned exception, and
    # it has already verified above that no object still depends on it.
    from superset_ownership import guard

    role = security_manager.find_role(sentinel.SENTINEL_ROLE_NAME)
    if role is not None:
        with guard.suppressed(db.session):
            db.session.delete(role)
            db.session.flush()
        result["role_removed"] = sentinel.SENTINEL_ROLE_NAME
    db.session.commit()

    logger.warning("superset_ownership: torn down -> %s", result)
    return result

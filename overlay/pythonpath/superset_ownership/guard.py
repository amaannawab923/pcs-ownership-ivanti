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
"""Making the sentinel non-forgeable.

Denial is a row in the object's `viewers` collection. `viewers` is an
ordinary editable field on the object: a plain

    PUT /api/v1/dashboard/13   {"viewers": []}

through stock Superset removes it. The ownership API kept reporting the
object as private, the UI kept drawing the lock, and anyone holding the
underlying dataset grant could open it -- with no audit record and nothing
detecting the state until the next restart. Detection at boot is not a
control for a service that stays up for months.

So the sentinel is re-asserted at the layer everything shares: a
`before_flush` listener on Superset's own session. Whatever route, command,
import or script tries to write a non-public object without its sentinel,
the sentinel goes back on in the same transaction, and the attempt is
audited. There is no path around it that does not bypass SQLAlchemy
entirely.

This is one hook on one event. It is deliberately not a permission check --
it does not care WHO is writing or WHY, only that the object's stored
visibility and its actual enforcement cannot diverge.

One more listener rides along: `before_delete` on FAB's User. Revision
0003's owner foreign key is ON DELETE SET NULL, so a hard-deleted user's
objects become unowned in the database with no trace of who owned them;
the listener reads the rows about to be un-owned and queues one audit event
per object, emitted after the delete commits. ORM deletes only -- a Core
`DELETE FROM ab_user` fires no ORM event and leaves the constraint's effect
as the only record, which `check`/`reconcile` then report.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager

logger = logging.getLogger(__name__)

_INSTALLED = False

# Session key marking a block where the guard must stand down. Lifecycle
# operations -- disable, teardown, a deliberate --force-open purge -- exist to
# REMOVE denial, and the guard re-armed it inside their own transaction: they
# reported success, changed nothing, and fired an alert per object. A rollback
# path that silently no-ops is worse than no rollback path, because the
# operator turns the flag off believing the objects are clear.
_SUPPRESSED = "_superset_ownership_guard_suppressed"

# Events raised during a flush, emitted only if that transaction commits.
_PENDING = "_superset_ownership_guard_pending_events"

# Objects seen as new during a flush, governed at before_commit -- see
# _govern_new_objects for why it cannot happen any earlier.
_NEW_ASSETS = "_superset_ownership_guard_new_assets"
_SENTINEL_CACHE = "_superset_ownership_guard_sentinel_subject"

# Re-entrancy flag for the Core-delete listener (see _before_core_delete): the
# statements it issues itself go through the same Session, and so through the
# same event.
_IN_CORE_DELETE = "_superset_ownership_guard_in_core_delete"

# The tables a governed object actually lives in. A DELETE aimed at one of
# these is a hard delete of a chart or a dashboard however it was written.
_GOVERNED_TABLES = {"slices": "chart", "dashboards": "dashboard"}


@contextmanager
def suppressed(session=None):
    """Stand the guard down for a block that is legitimately removing denial.

    Use ONLY for the lifecycle operations. Everything else -- routes,
    commands, imports, scripts -- must go through the invariant.
    """
    if session is None:
        from superset import db

        session = db.session
    prior = session.info.get(_SUPPRESSED, False)
    session.info[_SUPPRESSED] = True
    try:
        yield
    finally:
        session.info[_SUPPRESSED] = prior


def _request_actor():
    """Who is making this request, under either authentication scheme.

    `flask_login.current_user` is populated for a cookie session and NOT for
    a bearer token, which is the primary API path -- so the one audit event
    whose entire purpose is attribution recorded a null actor for exactly the
    caller it exists to name. The routes resolve the identity from the
    verified token; this does the same, and falls back to the session.
    """
    try:
        from flask import has_request_context

        if not has_request_context():
            return None
    except Exception:
        return None
    try:
        from flask_jwt_extended import get_jwt_identity, verify_jwt_in_request

        verify_jwt_in_request(optional=True)
        identity = get_jwt_identity()
        if identity is not None:
            from superset import security_manager

            return security_manager.get_user_by_id(int(identity))
    except Exception:
        pass
    try:
        from flask_login import current_user

        if getattr(current_user, "is_authenticated", False):
            return current_user
    except Exception:
        pass
    # flask.g.user is what Superset's own code and Celery's override_user set,
    # and it is the fallback the routes' own identity resolution uses.
    try:
        from flask import g

        user = getattr(g, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            return user
    except Exception:
        pass
    return None


def _asset_type_of(obj) -> str | None:
    from superset.models.dashboard import Dashboard
    from superset.models.slice import Slice

    if isinstance(obj, Dashboard):
        return "dashboard"
    if isinstance(obj, Slice):
        return "chart"
    return None


def _note_new_objects(session) -> None:
    """Remember objects being inserted, to govern them at commit time.

    Nothing can be decided during the flush that inserts them: `id` and `uuid`
    are server-side defaults, so at `before_flush` they are both None and there
    is no row to attach ownership to. The first attempt at this deferred to
    "the next flush", which never came for an operation that flushes once and
    commits -- so a copied dashboard stayed ungoverned.
    """
    tracked = session.info.setdefault(_NEW_ASSETS, [])
    for obj in session.new:
        if obj in session.deleted:
            continue  # created and deleted in one transaction -> nothing to govern
        if _asset_type_of(obj) and obj not in tracked:
            tracked.append(obj)


def _govern_new_objects(session) -> None:
    """Give every newly created object an ownership row, whatever created it.

    `AFTER_ASSET_CREATE` fires for the create commands and nothing else. It
    does not fire for `POST /api/v1/dashboard/<id>/copy/`, so a copy of a
    private dashboard landed with no ownership row, no denial and no tenant.
    The v1 importer has the same gap.

    Runs at `before_commit`. That event fires BEFORE the commit's own flush,
    so a plain `add()` + `commit()` with no flush in between had inserted
    nothing yet when this ran, and the object was governed one transaction
    late or never. So: flush first. That runs the insert, populates ids, and
    lets `_note_new_objects` see the object -- then govern, then flush again
    so the ownership row and the sentinel go out in the same commit.
    """
    from superset_ownership import service
    from superset_ownership.hooks import govern

    session.flush()
    tracked = session.info.pop(_NEW_ASSETS, None)
    if not tracked:
        return

    # Mid-rollback: govern() itself also checks this, but short-circuit here
    # too so the tracked list is cleared and no per-object work happens.

    if service.disable_in_progress():
        logger.info(
            "superset_ownership: disable in progress; new objects left ungoverned"
        )
        return

    from superset.utils.core import get_user_id

    try:
        owner_user_id = get_user_id()
    except Exception:
        owner_user_id = None

    from sqlalchemy import inspect as sa_inspect

    governed = 0
    with session.no_autoflush:
        for obj in tracked:
            asset_type = _asset_type_of(obj)
            object_id = getattr(obj, "id", None)
            if asset_type is None or object_id is None:
                continue
            # Survived to the commit? An object whose INSERT a savepoint rolled
            # back is detached/transient and must not be governed -- that was
            # dashboard 45, a row for an object that never existed.
            try:
                state = sa_inspect(obj)
                if state.deleted or not state.persistent or obj not in session:
                    continue
            except Exception:
                continue
            try:
                # Fresh: deciding whether to govern a new object must never
                # rest on a cached negative for its id.
                if service.lookup(asset_type, object_id, fresh=True) is not None:
                    continue
                # No request user means ownerless-and-public (see govern()).
                # Do NOT fall back to created_by_fk: an object created by a job
                # on someone's behalf is not owned by them for access purposes,
                # and inferring an owner here contradicts that decision.
                govern(obj, asset_type, owner_user_id, emit=False)
                # The event is queued like every other guard event: emitted
                # after commit, discarded on rollback.
                from superset import security_manager
                from superset_ownership import audit

                session.info.setdefault(_PENDING, []).append(
                    audit.build(
                        audit.OBJECT_CREATED,
                        actor=security_manager.get_user_by_id(owner_user_id)
                        if owner_user_id
                        else None,
                        asset_type=asset_type,
                        object_id=object_id,
                        object_uuid=str(getattr(obj, "uuid", "") or "") or None,
                        after={
                            "visibility": "private"
                            if owner_user_id is not None
                            else "public",
                            "owner_user_id": owner_user_id,
                            "via": "flush guard",
                        },
                    )
                )
                governed += 1
            except Exception:
                logger.exception(
                    "superset_ownership: could not govern new %s %s",
                    asset_type,
                    object_id,
                )
    if governed:
        session.flush()


def _protect_the_sentinel(session) -> None:
    """Refuse to delete the row the whole mechanism hangs on.

    `dashboard_viewers.subject_id` and `chart_viewers.subject_id` are ON DELETE
    CASCADE, so deleting the sentinel's Subject row -- or the
    `__ownership_sentinel__` role behind it, which looks like an empty junk
    role in stock Security -> Roles and which any admin can delete in two
    clicks -- strips denial from every governed object in the instance at once.
    No object is dirty, so nothing else here notices.

    Raising is the right response: this is not a state to repair afterwards,
    it is a deletion that must not happen. `lifecycle.teardown()` is the
    sanctioned way to remove it, and it suppresses this check.
    """
    from superset_ownership import sentinel

    role_name = sentinel.SENTINEL_ROLE_NAME
    for obj in session.deleted:
        cls = type(obj).__name__
        if cls == "Role" and getattr(obj, "name", None) == role_name:
            raise ValueError(
                f"refusing to delete the {role_name} role: object ownership "
                "enforcement depends on it, and removing it would open every "
                "private and shared object in this instance. Use "
                "`superset ownership teardown` to uninstall the feature."
            )
        if cls == "Subject":
            try:
                subject = sentinel.get_sentinel_subject()
            except Exception:
                continue
            if subject is not None and getattr(obj, "id", None) == getattr(
                subject, "id", None
            ):
                raise ValueError(
                    "refusing to delete the ownership sentinel subject: it "
                    "carries denial for every private and shared object in "
                    "this instance. Use `superset ownership teardown`."
                )


def _before_flush(session, flush_context, instances) -> None:
    """Hold the viewers invariant on every non-public object being written.

    A governed object's `viewers` must contain exactly the sentinel and
    nothing else. Two directions, and only one of them was defended:

    REMOVAL   drops the denial -- the object reports private and opens to
              anyone with the dataset grant.
    ADDITION  IS A GRANT. `raise_for_access` returns early for a published
              object whose viewers include you, so `PUT /api/v1/dashboard/13
              {"viewers": [24]}` hands a user in ANOTHER TENANT full access to
              a private dashboard, with no ownership row, no tuple, no audit
              event, and this module still reporting `shares: []`. Any owner
              of an object could do it; no tenant check was involved.

    So additions are stripped as well. Sharing on a governed object goes
    through the ownership API, which is where the tenant check, the
    authorization-store write and the audit record live.

    COST. This runs on every flush in the application, so it is ordered to
    pay as little as possible on the overwhelmingly common case of a flush
    that touches nothing governed: one batched ownership lookup, and the
    sentinel subject is not resolved at all unless a governed object is
    actually dirty.
    """
    if session.info.get(_SUPPRESSED):
        return

    from superset_ownership import audit, service

    _note_new_objects(session)
    _protect_the_sentinel(session)
    _collect_deleted(session)
    _follow_uuid_changes(session)

    candidates = [
        o for o in session.dirty if _asset_type_of(o) and getattr(o, "id", None)
    ]
    if not candidates:
        return

    with session.no_autoflush:
        # One query per asset type for the whole flush, not one per object.
        by_type: dict[str, list] = {}
        for obj in candidates:
            by_type.setdefault(_asset_type_of(obj), []).append(obj)

        governed: list[tuple] = []
        for asset_type, objs in by_type.items():
            try:
                # Fresh: the guard defends the sentinel invariant on the WRITE
                # path, and a row up to a TTL old from another worker must not
                # decide whether an object is governed. Still one query per
                # asset type per flush.
                rows = service.lookup_many(asset_type, [o.id for o in objs], fresh=True)
            except Exception:
                logger.exception(
                    "superset_ownership: guard could not read ownership for %s",
                    asset_type,
                )
                continue
            for obj in objs:
                row = rows.get(obj.id)
                # Governed means private or shared -- and public under
                # OWNERSHIP_PUBLIC_SCOPE=tenant. A row parked by disable()
                # (`disabled:<visibility>`) is deliberately not, so a web
                # worker does not re-arm what the operator just stripped.
                if service.is_enforced(row):
                    governed.append((asset_type, obj, row))

        # Nothing governed is being written: cost stops here, before the two
        # queries that resolving the sentinel subject would cost.
        if not governed:
            return

        try:
            subject = _sentinel_subject_cached(session)
        except Exception:
            logger.exception(
                "superset_ownership: could not resolve the sentinel subject"
            )
            return
        if subject is None:
            return

        for asset_type, obj, row in governed:
            viewers = obj.viewers
            removed = [v for v in list(viewers) if v is not subject and v != subject]
            rearmed = subject not in viewers

            # `editors` is the other grant channel, and the stronger one:
            # raise_for_access returns for an editor BEFORE it looks at
            # viewers, and does not require the object to be published. With
            # only `viewers` held, `PUT {"editors": [<other tenant's role>]}`
            # handed a whole tenant read AND write on a private object, with
            # this module still reporting `shares: []`.
            #
            # Native `editors` is stripped to the owner alone. Co-editors are
            # supplied dynamically by EXTRA_EDITORS_RESOLVER (hooks.extra_editors)
            # from the `editor` shares, so they do not need a row in the
            # object's own collection -- and a group editor, which has no
            # Superset Subject, is honoured that way and could never be honoured
            # here. REVIEW H-1/H-2 (round 1): an earlier version of this guard
            # (and of `service.sync_native_editors`, below) also materialised
            # share-holders into this collection, which let `is_editor` --
            # which trusts `editors` with no further check -- admit a
            # share-holder without the per-user dataset symmetry the dynamic
            # resolver applies, and left nothing here to undo when a share was
            # later revoked. The owner alone is what finally makes the
            # `editor` share role mean something in Superset's own write path
            # rather than only in ours, without reopening either regression.
            #
            # `service.sync_native_editors` is the ONE place that derives
            # this (ISSUE_80: it used to be duplicated here and in the
            # ownership-change routes, which is how a transfer could rewrite
            # the ownership row and the store but leave the previous owner a
            # native editor until this guard's next unrelated flush). Called
            # here with strict=True: anything that is not the owner is
            # removed -- no "previous owner" exception, since the guard does
            # not track one, only the invariant every governed object's
            # editors must hold. And with create_missing=False: a Subject
            # that does not exist yet is looked up, never created --
            # get_or_create_user_subject -> flush -> "Session is already
            # flushing" inside a flush event.
            editors = getattr(obj, "editors", None)
            before_editor_ids = (
                {s.id for s in editors} if editors is not None else set()
            )
            editors_changed = False
            if editors is not None:
                try:
                    editors_changed = service.sync_native_editors(
                        obj, row, strict=True, create_missing=False
                    )
                except Exception:
                    # A flush hook must not itself turn an unrelated write
                    # into a 500. Same posture as the sentinel-subject
                    # lookup above: log it, leave `editors` as found, and
                    # let the write proceed -- worst case an already-stale
                    # native editors collection stays stale one more flush.
                    logger.exception(
                        "superset_ownership: could not sync native editors for %s %s",
                        asset_type,
                        obj.id,
                    )
            after_editor_ids = {s.id for s in editors} if editors is not None else set()
            stripped = len(before_editor_ids - after_editor_ids)
            added = len(after_editor_ids - before_editor_ids)

            if not removed and not rearmed and not editors_changed:
                continue

            for extra in removed:
                viewers.remove(extra)
            if rearmed:
                viewers.append(subject)

            logger.warning(
                "superset_ownership: access fields corrected on %s %s (%s) -- "
                "denial restored=%s, direct viewers stripped=%d, direct editors "
                "stripped=%d, direct editors added=%d",
                asset_type,
                obj.id,
                row.visibility,
                rearmed,
                len(removed),
                stripped,
                added,
            )
            # `enforced` in before reflects whether denial had actually been
            # removed; an editors-only correction leaves it True, which is how
            # a consumer tells a re-arm from a direct-grant strip.
            # Queued, not emitted. Emitting here recorded a change for a
            # transaction that could still roll back -- which it did, in the
            # review, producing an event for a change that never happened.
            session.info.setdefault(_PENDING, []).append(
                audit.build(
                    audit.ACCESS_CORRECTED,
                    actor=_request_actor(),
                    asset_type=asset_type,
                    object_id=obj.id,
                    object_uuid=row.object_uuid,
                    before={
                        "enforced": not rearmed,
                        "direct_viewers": len(removed),
                        "direct_editors": stripped,
                    },
                    after={
                        "enforced": True,
                        "visibility": row.visibility,
                        "direct_viewers": 0,
                        "direct_editors": 0,
                    },
                )
            )


def _same_table(a, b) -> bool:
    """Are these two the same table, annotations aside?

    `delete(Slice)` -- the ORM entity form, which is also what
    `Query.delete()` compiles to -- carries its target as an
    `AnnotatedTable`: `==` the plain `Table`, but not `is` it. An identity
    check therefore declined EVERY entity-form delete as "spans 1 table",
    which is how `superset/commands/security/reset.py` came to delete every
    chart and dashboard while leaving every ownership row and every live
    `viewer` tuple behind (review round 2). Compared deannotated, which is
    what SQLAlchemy itself does to answer this question.
    """
    for side in (a, b):
        if side is None:
            return False
    left = a._deannotate() if hasattr(a, "_deannotate") else a
    right = b._deannotate() if hasattr(b, "_deannotate") else b
    return left is right


def _before_core_delete(state) -> None:
    """Collect ownership for a chart or dashboard deleted by a Core statement.

    `_collect_deleted` reads `session.deleted`, which only the ORM fills, so it
    never saw the hard-delete path: `cascade_hard_delete` (the purge route and
    the retention sweep both) removes the entity with `session.execute(
    sa.delete(table)...)`, and a bulk `session.execute(sa.delete(Slice)...)`
    does the same. The object went, its ownership row, its shares and all of
    its tuples stayed -- two of them live `viewer` grants -- and `check` went
    permanently red with nothing able to clear it (issue #120).

    This runs BEFORE the delete statement executes, which is what makes it
    correct rather than merely early: `cascade_hard_delete` does its work
    inside a savepoint and rolls it back when it loses the race for the row,
    and the ownership rows removed here are inside that same savepoint, so
    they come back with it. The ids are read under the FOR UPDATE claim that
    path already holds.

    COST. This fires on every `Session.execute` in the application, so the
    first thing it does is the cheapest question available: is this a DELETE
    at all. Everything else is behind that. Measured at roughly 60ns on a
    statement that is not a governed delete (review round 1).

    IT MUST NOT RAISE. It sits between a caller and their own statement, so
    anything it raises turns a legal DELETE into an error -- which review
    round 1 demonstrated two ways: a whereclause carrying an unbound
    `bindparam` (the parameters live on the execution, not the statement),
    and any failure inside the collection itself. Both are now contained
    here, on the same policy as the ORM path (`_collect_deleted`): a
    `DBAPIError` has already aborted the transaction on PostgreSQL and is
    re-raised where it happened; everything else is logged and the delete
    goes ahead, leaving at worst the orphan that `check` reports as
    `missing_object` and `reconcile --write` clears.
    """
    if not state.is_delete:
        return
    table = getattr(state.statement, "table", None)
    asset_type = _GOVERNED_TABLES.get(getattr(table, "name", None))
    if asset_type is None:
        return

    session = state.session
    if session.info.get(_SUPPRESSED) or session.info.get(_IN_CORE_DELETE):
        return

    from sqlalchemy.exc import DBAPIError

    session.info[_IN_CORE_DELETE] = True
    try:
        # In a SAVEPOINT of its own, so that containing a failure contains
        # ALL of it. The collection removes the local rows first and only
        # then invalidates the cache and enqueues the store purge; without
        # this, a failure in one of those later steps left the earlier ones
        # committed -- the object gone, the row gone, and the store still
        # holding `owner` and `viewer`, which nothing can then detect
        # because `check` and `reconcile` both enumerate from
        # `ownership_object` (review round 2).
        #
        # On the CONNECTION, and with autoflush off, not
        # `session.begin_nested()` (review round 3).
        # `SessionTransaction._take_snapshot` runs a full `session.flush()`
        # before the SAVEPOINT is emitted, so the session form made this
        # listener flush the caller's entire pending unit of work in front
        # of every governed Core DELETE -- a statement that autoflushes
        # nothing on its own. When that flush failed, the `IntegrityError`
        # is a `DBAPIError`, so the re-raise below carried it out of the
        # caller's own `session.execute(delete(...))` and left the Session
        # deactivated; `cascade_hard_delete` then reported the entity as
        # blocked by a cascade integrity constraint, which it was not. The
        # same idiom, and the same reason, as
        # `superset/versioning/baseline/insertion.py`.
        with session.no_autoflush, session.connection().begin_nested():
            _collect_core_deleted(state, session, table, asset_type)
    except DBAPIError:
        raise
    except Exception:  # noqa: BLE001 - never turn someone else's DELETE into an error
        logger.exception(
            "superset_ownership: could not collect ownership for a %s delete; the "
            "delete proceeds and the row is `reconcile`'s to sweep",
            asset_type,
        )
    finally:
        session.info.pop(_IN_CORE_DELETE, None)


def _collect_core_deleted(state, session, table, asset_type: str) -> None:
    """`_before_core_delete`'s body, wrapped by its caller's guard."""
    from sqlalchemy import select

    from superset_ownership.hooks import after_asset_delete

    id_column = table.c.get("id")
    if id_column is None:  # pragma: no cover - neither table is without one
        return
    uuid_column = table.c.get("uuid")
    columns = [id_column] + ([uuid_column] if uuid_column is not None else [])

    statement = state.statement
    lookup = select(*columns)
    if statement.whereclause is not None:
        lookup = lookup.where(statement.whereclause)

    # A whereclause that drags in another table would make this SELECT a
    # cartesian product, and its ids would name objects the DELETE does not
    # touch. PostgreSQL compiles such a DELETE as `DELETE ... USING`, which
    # is legal, so this is a real statement shape and not a theoretical one
    # (review round 1). Refuse to guess: say so and collect nothing, which
    # leaves exactly the orphan `check` already reports.
    froms = lookup.get_final_froms()
    if len(froms) != 1 or not _same_table(froms[0], table):
        logger.warning(
            "superset_ownership: a %s DELETE whose criteria spans %d table(s) was not "
            "collected; run `ownership reconcile --write` afterwards",
            asset_type,
            len(froms),
        )
        return

    # The parameters live on the EXECUTION, not on the statement: re-running
    # the whereclause without them raises `A value is required for bind
    # parameter` and, from inside the event, kills the caller's DELETE
    # (review round 1). `bind_arguments` and `execution_options` carry the
    # bind and the schema translation for the same reason.
    parameters = state.parameters
    execution_options = dict(state.execution_options or {})
    bind_arguments = dict(state.bind_arguments or {})

    def _ids(params):
        return session.execute(
            lookup,
            params,
            execution_options=execution_options,
            bind_arguments=bind_arguments,
        ).all()

    if isinstance(parameters, (list, tuple)):
        # executemany: one DELETE per parameter set, so one id set per set.
        doomed = [row for params in parameters for row in _ids(params)]
    else:
        doomed = _ids(parameters)

    # Same (asset_type, object_id) order as the ORM path, for the same
    # reason: two bulk deletes over overlapping sets must not each hold a
    # row the other waits for. De-duplicated because an executemany can name
    # the same row twice.
    seen: set = set()
    for row in sorted(doomed, key=lambda r: r[0]):
        if row[0] in seen:
            continue
        seen.add(row[0])
        uuid = str(row[1]) if len(row) > 1 and row[1] else None
        # NOT under this module's per-object advisory lock, unlike every
        # other write: `cascade_hard_delete` already holds the asset row
        # `FOR UPDATE` before it issues the statement we are standing in
        # front of, so taking our lock here would be the one place in the
        # module that takes it AFTER the asset row -- the ABBA against the
        # visibility route, which takes ours first and touches the asset
        # row second (review round 1). The DELETEs below take their own row
        # locks, and this path's intents are the last ones the object will
        # ever have.
        after_asset_delete(asset_type, row[0], uuid, lock=False)


def _follow_uuid_changes(session) -> None:
    """Re-point ownership at a governed object whose uuid changed in place.

    Superset's dashboard import validates collisions by uuid but resolves by
    `slug`, so an import can replace a LIVE dashboard and give it the
    imported file's uuid (issue #127). Nothing else in Superset rewrites a
    uuid, so this is cheap: the attribute history of the objects already
    known to be dirty, and a re-point only when one actually moved.

    The store is addressed by uuid, so the tuples move with the row -- see
    `service.repoint_object_uuid`, which does the work in this same
    transaction, so an import that rolls back takes the re-point with it.
    """
    from sqlalchemy import inspect as sa_inspect

    from superset_ownership import service

    for obj in session.dirty:
        asset_type = _asset_type_of(obj)
        if asset_type is None or getattr(obj, "id", None) is None:
            continue
        try:
            history = sa_inspect(obj).attrs.uuid.history
        except Exception:  # noqa: BLE001 - a model without the attribute
            continue
        if not history.has_changes() or not history.deleted:
            continue
        old_uuid = str(history.deleted[0]) if history.deleted[0] else None
        new_uuid = str(getattr(obj, "uuid", "") or "")
        if not new_uuid or old_uuid == new_uuid:
            continue
        try:
            service.repoint_object_uuid(asset_type, obj.id, old_uuid, new_uuid)
        except Exception:
            # Never break the write that carried the change: the divergence
            # is reported by `check` (uuid_divergence) and repairable by
            # `reconcile --write`.
            logger.exception(
                "superset_ownership: could not follow %s %s from uuid %s to %s",
                asset_type,
                obj.id,
                old_uuid,
                new_uuid,
            )


def _collect_deleted(session) -> None:
    """Remove ownership rows for objects being hard-deleted in this flush.

    There is no AFTER_ASSET_DELETE extension point, so a purge (manual, or the
    retention sweep) left the row and the tuples behind and turned `check`
    permanently red. A SOFT delete only sets deleted_at and is an ordinary
    dirty update, so it is NOT collected here -- the object still exists.
    """
    from sqlalchemy.exc import DBAPIError

    from superset_ownership.hooks import after_asset_delete

    # A bulk delete collects N objects in ONE transaction, and the hook
    # locks each object's row (service.lock_object). Taken in
    # (asset_type, object_id) order -- the order every bulk path uses --
    # rather than in identity-map order, so two bulk deletes, or a bulk
    # delete and a backfill or a purge, over overlapping sets cannot each
    # hold a row the other is waiting for.
    doomed = []
    for obj in session.deleted:
        asset_type = _asset_type_of(obj)
        if asset_type is None or getattr(obj, "id", None) is None:
            continue
        doomed.append((asset_type, obj.id, obj))
    for asset_type, object_id, obj in sorted(doomed, key=lambda d: (d[0], d[1])):
        uuid = str(getattr(obj, "uuid", "") or "") or None
        try:
            after_asset_delete(asset_type, object_id, uuid)
        except DBAPIError:
            # A database error -- a deadlock or lock wait from the row lock,
            # a lost connection -- has already aborted this transaction on
            # Postgres. Swallowing it let the flush carry on and fail two
            # statements later with InFailedSqlTransaction, and logged the
            # cause as "could not collect deleted". Let it fail where it
            # failed.
            raise
        except Exception:
            # An unexpected error in the hook itself must not stop Superset
            # from deleting the object; the row, if any, is `reconcile`'s
            # to sweep. (The ownership tables missing after a teardown is
            # not this case: Postgres reports it as UndefinedTable, a
            # database error, and it surfaces through the branch above.)
            logger.exception(
                "superset_ownership: could not collect deleted %s %s",
                asset_type,
                object_id,
            )


def _sentinel_subject_cached(session):
    """The sentinel subject, resolved once per session rather than per flush.

    `get_sentinel_subject()` is two queries. Caching on `flask.g` helped web
    requests and nothing else -- a CLI command or a Celery task paid both
    queries on every flush. `session.info` is cleared when the scoped session
    is removed, which is per request on the web and per task elsewhere.
    """
    from superset_ownership import sentinel

    cached = session.info.get(_SENTINEL_CACHE)
    if cached is not None:
        return cached
    subject = sentinel.get_sentinel_subject()
    if subject is not None:
        session.info[_SENTINEL_CACHE] = subject
    return subject


def _before_commit(session) -> None:
    """Govern anything created in this transaction, before it becomes real."""
    if session.info.get(_SUPPRESSED):
        session.info.pop(_NEW_ASSETS, None)
        return
    from sqlalchemy.exc import SQLAlchemyError

    try:
        _govern_new_objects(session)
    except SQLAlchemyError:
        # A database error here is the caller's own transaction failing (the
        # first thing _govern_new_objects does is flush the caller's pending
        # INSERTs -- a duplicate slug, a constraint violation). Swallowing it
        # turned the caller's IntegrityError into an opaque PendingRollbackError
        # and hid the real cause. Let it propagate unchanged.
        raise
    except Exception:
        # A fault in OUR governance logic must not take down the user's commit.
        logger.exception("superset_ownership: governing new objects failed")


def _after_commit(session) -> None:
    """The transaction is real: replay the ownership cache invalidations the
    request made before the commit, then emit what the flush queued.

    The replay comes first and is the security-relevant half. Every writer
    invalidates the shared row cache at the time of its write, and a reader
    in another request can republish the still-committed old row in the
    window before this commit; deleting the same keys again here is what
    removes that entry. See the cache section of service.py.

    SQLAlchemy dispatches `after_commit` for a SAVEPOINT release as well as
    for the outer commit (`SessionTransaction.commit`: `if self._parent is
    None or self.nested`). A savepoint release makes nothing visible to
    other connections, so it is not the moment to replay: a replay there
    consumes the pending set while the transaction is still open, leaves
    the outer commit nothing to replay, and lets a reader that republishes
    the old row between the release and the commit keep it for a full TTL.
    Inside the listener `session.in_nested_transaction()` is True for a
    release and False for the outer commit; both the replay and the audit
    emission wait for the latter, so events queued inside a savepoint are
    emitted only once the transaction that contains them is committed.
    """
    if session.in_nested_transaction():
        return  # a SAVEPOINT release; the outer transaction is not real yet
    from superset_ownership import service

    try:
        service.replay_invalidations()
    except Exception:  # noqa: BLE001 - a cache fault must not fail the commit path
        logger.exception("superset_ownership: replaying cache invalidations failed")
    events = session.info.pop(_PENDING, None)
    if not events:
        return
    from superset_ownership import audit

    for record in events:
        audit.emit_record(record)


def _after_rollback(session, previous_transaction=None) -> None:
    """Discard queued events, new-object tracking and the row cache's
    pending state -- but ONLY for a full rollback, never a savepoint. A
    savepoint rollback drops the request-local rows and nothing else.

    `after_soft_rollback` fires for nested (savepoint) rollbacks too. Clearing
    unconditionally meant a `begin_nested()` rollback anywhere in a transaction
    threw away the OUTER transaction's tracking: an object already flushed
    before the savepoint committed with no ownership row, and a strip already
    queued was never audited. The v1 importer, the group-subject IntegrityError
    race and purge_cascade all roll back savepoints mid-transaction.

    `previous_transaction.nested` is True for a savepoint. When it is, the
    outer transaction is still live and its tracking must survive.
    """
    from superset_ownership import service

    # A savepoint rollback fires this too. Do NOT clear tracking then: the
    # outer transaction is still live, and objects it inserted before the
    # savepoint must still be governed. Objects the savepoint itself rolled
    # back are dropped at governance time by the persistence check in
    # _govern_new_objects, not here -- clearing everything orphaned the outer
    # object, keeping everything governed a nonexistent one. The row cache's
    # pending replay stays for the same reason; only the request-local rows
    # go, since one read inside the savepoint may hold what it discarded.
    if previous_transaction is not None and getattr(
        previous_transaction, "nested", False
    ):
        try:
            service.discard_invalidations(savepoint=True)
        except Exception:  # noqa: BLE001
            logger.exception(
                "superset_ownership: discarding cache invalidations failed"
            )
        return
    session.info.pop(_PENDING, None)
    session.info.pop(_NEW_ASSETS, None)
    # The row cache: drop the pending shared-layer replay (nothing was
    # committed, so the entries other requests hold are right) and the
    # request-local rows, which may hold the rolled-back value.
    try:
        service.discard_invalidations()
    except Exception:  # noqa: BLE001
        logger.exception("superset_ownership: discarding cache invalidations failed")


def _before_user_delete(mapper, connection, target) -> None:
    """A FAB User is being hard-deleted through the ORM. Record every object
    it owns before the owner foreign key (ON DELETE SET NULL, revision 0003)
    makes the rows read as never-owned.

    Runs inside the flush, on the flush's own connection, so it sees the
    rows exactly as the DELETE is about to find them. The events go onto the
    session's pending list and are emitted by `_after_commit`, like every
    other event this module raises during a flush: nothing is reported for
    a delete that rolls back. Without a session (a detached instance) they
    are emitted at once. Never raises: a fault here must not stop the
    delete.

    Inert when `ownership_object` is absent: the listener is installed by
    `install()` whenever the module is wired, which is before the tables
    exist under OWNERSHIP_AUTO_MIGRATE=false (until the operator's first
    `superset ownership db upgrade`) and after `superset ownership teardown`
    in every process still running. The table's presence is checked with
    the inspector BEFORE the SELECT, because a statement that fails on
    PostgreSQL aborts the flush's transaction -- catching the exception is
    not enough there, the DELETE that follows would fail with
    InFailedSqlTransaction and the user would not be deleted. The SELECT
    itself runs under a SAVEPOINT (`begin_nested`) for every other way it
    can fail -- a role without SELECT on the table, the table dropped by a
    concurrent teardown between the inspector's read and the SELECT -- so
    that failure is rolled back to the savepoint, the flush's transaction
    stays usable, and the DELETE goes through."""
    from sqlalchemy import inspect, select
    from sqlalchemy.orm import object_session

    from superset_ownership import audit
    from superset_ownership.db import ownership_object

    user_id = getattr(target, "id", None)
    if user_id is None:
        return
    try:
        if not inspect(connection).has_table(ownership_object.name):
            logger.info(
                "superset_ownership: user %s is being deleted while %s does not "
                "exist (the ownership chain has not run, or was torn down); "
                "nothing to record",
                user_id,
                ownership_object.name,
            )
            return
        with connection.begin_nested():
            rows = connection.execute(
                select(
                    ownership_object.c.asset_type,
                    ownership_object.c.object_id,
                    ownership_object.c.object_uuid,
                )
                .where(ownership_object.c.owner_user_id == user_id)
                .order_by(ownership_object.c.asset_type, ownership_object.c.object_id)
            ).all()
    except Exception:  # noqa: BLE001 - a fault here must not stop the delete
        logger.warning(
            "superset_ownership: could not read owned objects of user %s",
            user_id,
            exc_info=True,
        )
        return
    if not rows:
        return
    logger.warning(
        "superset_ownership: hard delete of user %s un-owns %d object(s) (the owner "
        "foreign key sets them NULL); one %s event per object: %s",
        user_id,
        len(rows),
        audit.OWNER_REMOVED_BY_USER_DELETE,
        [f"{r[0]}:{r[1]}" for r in rows[:20]],
    )
    actor = _request_actor()
    session = object_session(target)
    for asset_type, object_id, object_uuid in rows:
        record = audit.build(
            audit.OWNER_REMOVED_BY_USER_DELETE,
            actor=actor,
            asset_type=asset_type,
            object_id=object_id,
            object_uuid=object_uuid,
            before={"owner_user_id": user_id},
            after={"owner_user_id": None},
        )
        if session is not None:
            session.info.setdefault(_PENDING, []).append(record)
        else:
            audit.emit_record(record)


def install_user_delete_listener(user_model) -> bool:
    """`before_delete` on the mapped user class. Separate from `install()` so
    it can be attached to a stand-in model in a test; returns whether it
    was attached (False when already there)."""
    from sqlalchemy import event

    if event.contains(user_model, "before_delete", _before_user_delete):
        return False
    event.listen(user_model, "before_delete", _before_user_delete)
    return True


def _user_model():
    """FAB's User as Superset configures it, or None where there is no FAB
    (a stub `superset` in the pure tests)."""
    try:
        from superset import security_manager

        model = getattr(security_manager, "user_model", None)
        if model is not None:
            return model
    except Exception:  # noqa: BLE001 - no real Superset here; try FAB directly
        logger.debug(
            "superset_ownership: no security_manager.user_model", exc_info=True
        )
    try:
        from flask_appbuilder.security.sqla.models import User

        return User
    except Exception:  # noqa: BLE001
        logger.debug(
            "superset_ownership: flask_appbuilder not importable", exc_info=True
        )
        return None


def install() -> None:
    """Attach the listeners to Superset's session (and the user-delete
    listener to FAB's User). Idempotent."""
    global _INSTALLED
    if _INSTALLED:
        return
    from sqlalchemy import event
    from superset import db

    event.listen(db.session, "before_flush", _before_flush)
    event.listen(db.session, "do_orm_execute", _before_core_delete)
    event.listen(db.session, "before_commit", _before_commit)
    event.listen(db.session, "after_commit", _after_commit)
    event.listen(db.session, "after_soft_rollback", _after_rollback)
    user_model = _user_model()
    if user_model is not None:
        install_user_delete_listener(user_model)
    else:
        logger.info(
            "superset_ownership: no user model; user-delete audit not installed"
        )
    _INSTALLED = True
    logger.info("superset_ownership: viewers guard installed")

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
"""Transactional outbox for authorization-store writes (SOW §3, spec §8.1).

THE PROBLEM. Every share, transfer, visibility change and delete has to be
recorded in two places: Superset's own database, and OpenFGA, the external
service that is actually asked "may this person open this?". If the two
disagree, a person is either wrongly denied or -- worse -- wrongly granted.

BEFORE. The request wrote Superset's row, then called OpenFGA inline, while
the user waited. If OpenFGA was slow, sharing was slow. If OpenFGA was down,
NOBODY could share, even though Superset itself was fine.

NOW. The request writes Superset's row AND one row in `ownership_outbox` in
the SAME transaction, and returns. A background drain (Celery beat, or the
`superset ownership outbox drain` command) delivers each row to OpenFGA with
retries. Because the outbox row is committed atomically with the change it
describes, there is no window in which the change exists but the instruction
to sync it does not: the write cannot be lost.

THE WINDOW, BOTH DIRECTIONS. Between commit and delivery the store lags the
mirror. The two directions are not symmetric and both are closed:

  grant   (write pending)  the recipient is DENIED until delivered -- the
                           store does not have the tuple yet. Fails safe on
                           its own.
  revoke  (delete/purge    the store STILL HAS the tuple until delivered, so
           pending)        the store alone would keep GRANTING. This does not
                           fail safe on its own. The read path therefore
                           consults the outbox first -- see
                           :func:`has_pending_revocation`, called from
                           `hooks.raise_for_access_bypass` -- and denies while
                           a revocation for that object/subject is undelivered.

ORDERING AND CONCURRENCY. A transfer is delete(old) then write(new); the write
must never overtake the delete. Rows are applied in id order per object, and
a row that is in flight (claimed), failed, or dead blocks every later row for
the same object. Each row is taken with ONE atomic claim statement that
carries both guarantees:

    UPDATE ... SET status='claimed'
     WHERE id=:id AND status='pending'
       AND NOT EXISTS (an earlier undelivered row for the same object)

so two drains running at once -- overlapping beat ticks, several workers, an
operator's CLI -- can never both deliver the same row, and can never apply a
later row for an object while an earlier one is pending, claimed or dead in
another drain. There is no window between "check the object is free" and
"take the row": they are the same statement. Claims left by a crashed drain
are reaped after a lease, and a drain that outlives its own lease has its
outcome discarded rather than overwriting the re-claimer's.

FAILURE CLASSIFICATION. "The store could not give an answer" and "the store
said no" are different. The first (connection refused, timeout, a 5xx from a
balancer in front of a restarting store) is retried and trips the circuit
breaker, so a pass stops early rather than burning a timeout per row. The
second (a 4xx: model validation, a stale model pin) will not succeed on retry
and is parked as `dead` immediately, where an operator can see it, fix the
cause, and replay it. The split is made by the client from the store's own
response -- :class:`fga.StoreUnavailable` versus :class:`fga.StoreRejected`
-- never by probing whether the store happens to answer a moment later. Dead
rows are never deleted.

ID ORDER IS COMMIT ORDER. Ids are given at INSERT time, not at COMMIT time,
so on their own they would not order two requests that mutate the same
object at the same moment: the second to enqueue could be the first to
commit, the mirror would end in commit order and the drain would apply id
order. What makes the two the same is the per-object write lock,
`service.lock_object` -- `SELECT ... FOR UPDATE` on the object's
`ownership_object` row -- which every mutating path (share, revoke,
visibility, owner, claim, the hard-delete hook, and `upsert_ownership` when
it updates a row) takes before anything else it does to the object and holds
until its commit. For any one object, "lock, mirror write, enqueue, commit"
runs one request at a time; the next writer's id is allocated only after the
previous writer's commit, so the ids of an object's rows increase in the
order their transactions committed, and the drain's order is the mirror's.
An object with no row yet -- brand-new, or pre-existing and never recorded
-- is serialised on Postgres by the transaction-scoped advisory lock the
same helper takes before its SELECT, so two writers governing it cannot
enqueue out of commit order either; on other dialects the unique key on the
INSERT settles a crossed first governing (the loser updates instead of
failing), and such an object's only enqueued rows are its owner write and
tenant stamp, which do not order against a revocation on another subject.
Bulk paths (backfill, lifecycle's purge / disable / enable / teardown) do
not enqueue per object -- they write the store inline -- and they take the
same row locks, object row before share rows and rows in (asset_type,
object_id) order, until their own commit, so they cannot interleave with a
request on the same object and cannot cycle with a share route or with one
another.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from sqlalchemy import delete, exists, func, insert, select, update

from superset_ownership.db import ownership_object, ownership_outbox, ownership_share
from superset_ownership.fga import StoreRejected, StoreUnavailable

logger = logging.getLogger(__name__)

OP_WRITE = "write"
OP_DELETE = "delete"
OP_SET_TENANT = "set_tenant"
OP_PURGE_OBJECT = "purge_object"
OP_REVOKE_SUBJECT = "revoke_subject"  # every relation one subject holds on an object
REVOKING_OPS = (OP_DELETE, OP_PURGE_OBJECT, OP_REVOKE_SUBJECT)

STATUS_PENDING = "pending"
STATUS_CLAIMED = "claimed"  # taken by a drain that has not finished with it
STATUS_DONE = "done"
STATUS_DEAD = "dead"
UNDELIVERED = (STATUS_PENDING, STATUS_CLAIMED, STATUS_DEAD)

DEFAULT_MAX_ATTEMPTS = 10
# Stop a pass after this many consecutive TRANSPORT failures.
CONSECUTIVE_FAILURE_LIMIT = 3
# A claim older than this belongs to a drain that died mid-row; reclaim it.
# Must exceed the Celery task's hard time_limit (below) by a clear margin, so
# a task that is merely slow is killed before its claims are handed to
# another drain. The CLI drain has no time limit; its outcome UPDATEs are
# guarded by the claim timestamp, so if it does outlive the lease it loses
# rather than overwrites.
CLAIM_LEASE = timedelta(minutes=10)
# Pending work older than this means nothing is draining; status() reports
# it and the consistency check fails on it.
STALL_AFTER = timedelta(minutes=15)
TASK_SOFT_TIME_LIMIT = 270
TASK_TIME_LIMIT = 300
TASK_NAME = "superset_ownership.outbox.drain"

# Signals that a drain must stop NOW and hand its current row back untouched
# (attempts unchanged, status pending): the worker's soft time limit, and an
# operator's Ctrl-C on the CLI. They are re-raised, never recorded as the
# store's verdict.
try:
    from billiard.exceptions import SoftTimeLimitExceeded
except ImportError:  # no Celery in this process

    class SoftTimeLimitExceeded(Exception):  # type: ignore[no-redef]
        """Stand-in so the drain's handler has one name to catch."""


_INTERRUPTS: tuple[type[BaseException], ...] = (
    SoftTimeLimitExceeded,
    KeyboardInterrupt,
    SystemExit,
)


def _utcnow() -> datetime:
    # Naive UTC, to match the naive DateTime columns. Never the DB's local
    # now(), so ages computed by an operator are not off by the DB timezone.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def enabled() -> bool:
    """Route authorization writes through the outbox?

    Config layer first (like OWNERSHIP_AUTHORIZER), then the environment,
    default on. OWNERSHIP_OUTBOX_ENABLED=false restores inline writes --
    sensible for the local (non-OpenFGA) authorizer, where there is no
    remote store to decouple from. Legacy grammar (settings.as_legacy_bool):
    0/1/false/true/no/yes/off/on/"" are all recognised; an unrecognised
    spelling is read as off (one WARNING), never silently re-enabling a
    deployment that opted out with a working spelling settings.as_bool
    would otherwise reject (section 3.1).
    """
    from superset_ownership import settings

    return settings.get("OWNERSHIP_OUTBOX_ENABLED", True, settings.as_legacy_bool)


def _session():
    from superset_ownership import service

    return service._db().session


def get_authorizer():
    # Lazy: authz imports service, service imports this module.
    from superset_ownership.plugins import get_authorizer as _get

    return _get()


# --------------------------------------------------------------------------- enqueue


def enqueue(
    op: str,
    obj: str,
    subject: Optional[str] = None,
    relation: Optional[str] = None,
    session: Any = None,
) -> int:
    """Add one instruction to the outbox ON THE CALLER'S SESSION.

    Deliberately does not commit: the row must ride the same transaction as
    the local change it describes, so both land or neither does.
    """
    session = session or _session()
    result = session.execute(
        insert(ownership_outbox).values(
            op=op, subject=subject, relation=relation, object=obj, created_on=_utcnow()
        )
    )
    row_id = result.inserted_primary_key[0]
    logger.debug("outbox enqueue #%s %s %s %s %s", row_id, op, subject, relation, obj)
    return row_id


# --------------------------------------------------------------------------- facade
#
# Call sites use these instead of the authorizer directly. Enabled, they
# enqueue and return True; delivery is the drain's job. Disabled, they go
# straight to the authorizer, exactly as the inline calls they replaced.
#
# Every one of them that names a SUBJECT -- write_tuple, delete_tuple,
# revoke_subject, below -- also invalidates that subject's list_objects
# cache entry for this object's asset type (issue #82,
# `service.invalidate_list_objects`), for whichever relation: the OpenFGA
# model has `viewer` include `editor` include `owner`, so a write or delete
# of ANY of the three can change what `list_objects(subject, "viewer",
# asset_type)` would answer for them next -- an owner tuple (govern,
# transfer, claim, backfill all call `write_tuple` with relation="owner")
# as much as a share. This is the ONE call site every share, unshare,
# transfer, visibility and claim write in the package goes through
# (`service.py`, `hooks.py`, `api.py`), so hooking invalidation in here
# covers all of them without a second call site anywhere else. Invalidating
# BEFORE the write (enqueue or inline) rather than after costs nothing
# extra and means a request that shares-then-lists in the same request
# never reads its own stale cache entry back.
#
# `purge_object` does not invalidate: see `invalidate_list_objects`'s own
# docstring for why it does not need to.


def write_tuple(subject: str, relation: str, obj: str) -> bool:
    _invalidate_list_objects(obj, subject)
    if enabled():
        enqueue(OP_WRITE, obj, subject, relation)
        return True
    return get_authorizer().write_tuple(subject, relation, obj)


def delete_tuple(subject: str, relation: str, obj: str) -> bool:
    _invalidate_list_objects(obj, subject)
    if enabled():
        enqueue(OP_DELETE, obj, subject, relation)
        return True
    return get_authorizer().delete_tuple(subject, relation, obj)


def _invalidate_list_objects(obj: str, subject: Optional[str]) -> None:
    """`obj` is "<asset_type>:<uuid>"; the asset type is all
    `service.invalidate_list_objects` needs. A blank/None subject (a group
    reference is still a subject string, never blank; only an unresolved
    member lookup upstream would produce one) is a no-op there."""
    asset_type, _, _ = obj.partition(":")
    if not asset_type:
        return
    from superset_ownership import service

    service.invalidate_list_objects(asset_type, subject)


def set_object_tenant(
    asset_type: str,
    object_uuid: str,
    tenant_guid: str,
    *,
    object_id: Optional[int] = None,
) -> bool:
    """`object_id`, when the caller knows it, is what the ROW is matched on.

    `ownership_object.object_uuid` carries no unique constraint, and on the
    instances that most need repairing it is demonstrably not unique -- a
    `uuid_divergence` is exactly a row holding a uuid some other object may
    now carry. Matching the mirror write by uuid there stamps a BYSTANDER
    row's tenant (review round 2). The store half is addressed by uuid
    because that is how the store addresses objects; the row half does not
    have to be.
    """
    if not (object_uuid and tenant_guid):
        return False
    # The row's mirror of the tenant (revision 0004) is written here, in the
    # caller's transaction, whether the store write is queued or inline: the
    # "public within the tenant" read gate decides from the row, and must
    # not wait for a drain to know which tenant an object is in.
    from superset_ownership import service

    service.set_row_tenant(asset_type, object_uuid, tenant_guid, object_id=object_id)
    if enabled():
        enqueue(OP_SET_TENANT, f"{asset_type}:{object_uuid}", tenant_guid, "tenant")
        return True
    return get_authorizer().set_object_tenant(asset_type, object_uuid, tenant_guid)


def revoke_subject(asset_type: str, object_uuid: str, subject: str) -> bool:
    """Remove EVERY relation `subject` holds on the object -- a share revoked.

    Resolved at drain time by reading the store, so a relation the mirror
    never learned about is revoked too, and the request path does no store
    read at all.
    """
    if not (object_uuid and subject):
        return False
    obj = f"{asset_type}:{object_uuid}"
    from superset_ownership import service

    service.invalidate_list_objects(asset_type, subject)
    if enabled():
        enqueue(OP_REVOKE_SUBJECT, obj, subject)
        return True
    return get_authorizer().revoke_subject(subject, obj)


def purge_object(asset_type: str, object_uuid: str) -> bool:
    """Remove EVERY relationship on an object -- the delete event. The drain
    (or, disabled, the authorizer itself) reads what exists and removes it."""
    if not object_uuid:
        return False
    obj = f"{asset_type}:{object_uuid}"
    if enabled():
        enqueue(OP_PURGE_OBJECT, obj)
        return True
    return get_authorizer().purge_object(obj)


# --------------------------------------------------------------------------- read path


def _role_change_subjects_indexed(
    session: Any, asset_type: str, object_id: int, subjects: set[str]
) -> set[str]:
    """Which of `subjects` still hold a mirror share row on this object --
    the role-change carve-out, resolved with a point lookup on
    `uq_ownership_share_subject (asset_type, object_id, subject)` instead of
    a join back into `ownership_object`. Callers already know `object_id`
    (the ownership row that got them here); this is what lets the carve-out
    stay indexed without a migration."""
    if not subjects:
        return set()
    return {
        r[0]
        for r in session.execute(
            select(ownership_share.c.subject).where(
                ownership_share.c.asset_type == asset_type,
                ownership_share.c.object_id == object_id,
                ownership_share.c.subject.in_(subjects),
            )
        )
    }


def _role_change_subjects_by_uuid(
    session: Any, asset_type: str, object_uuid: str, subjects: set[str]
) -> set[str]:
    """Same question, without a known `object_id`: correlate through
    `ownership_object` by `object_uuid`. There is no index on that column
    (see Observations, PR #94 review), so this is a full scan of the mirror
    -- kept only as a fallback for a caller that does not have the row
    already (a bare `session=` test, or a future call site that has not been
    taught to pass `object_id`); every call this module makes itself passes
    `object_id`."""
    if not subjects:
        return set()
    sh = ownership_share.alias("sh")
    oo = ownership_object.alias("oo")
    join_on = (sh.c.asset_type == oo.c.asset_type) & (sh.c.object_id == oo.c.object_id)
    q = (
        select(sh.c.subject)
        .select_from(sh.join(oo, join_on))
        .where(
            oo.c.asset_type == asset_type,
            oo.c.object_uuid == object_uuid,
            sh.c.subject.in_(subjects),
        )
    )
    return {r[0] for r in session.execute(q)}


def has_pending_revocation(
    obj: str,
    subject: Optional[str],
    object_id: Optional[int] = None,
    session: Any = None,
) -> bool:
    """Is a revocation for this object still undelivered, as far as `subject`
    (a ``user:`` reference) is concerned?

    Consulted by the access decision BEFORE the store, because until the
    drain delivers a revocation the store still grants. Dead rows count: a
    revocation an operator has yet to resolve is still a revocation. This is
    the query `ix_ownership_outbox_object` exists for.

    What denies whom:
      purge_object            everyone on the object
      delete / revoke_subject the row's subject, when it is the user asked
                              about ...
                              ... or ANY member, when the row's subject is
                              not a user -- a group. The read path holds only
                              the user's own reference, not the groups the
                              store knows them by, so a revoked group share
                              is treated as revoking every member until
                              delivered (seconds). Fail-closed and cheap;
                              resolving group membership here would put a
                              store read back on every access decision.

    What is NOT a revocation: a `delete` row whose subject still holds a
    share row in the mirror. That is a role CHANGE (delete editor, write
    viewer), and the store's not-yet-retracted tuple grants at least what
    the mirror now says. Judged on the ROW's subject, so it holds for a
    group's role change as much as a user's. A revoked subject has no mirror
    row; a transferred-away owner has none; only the re-roled keep one.

    `object_id`: pass the caller's own ownership row id when it has one
    (both call sites -- `hooks.raise_for_access_bypass`,
    `api._already_opens` -- already hold the row) so the carve-out above is
    one indexed point lookup on `ownership_share` instead of a join that
    seq-scans `ownership_object` in full (PR #94 review, Observations: no
    index on `object_uuid`). Omitted, the carve-out falls back to that scan
    -- correct, just the pre-existing cost, and never hit by either
    production call site once both pass their row's id.
    """
    session = session or _session()
    asset_type, _, object_uuid = obj.partition(":")
    q = select(ownership_outbox.c.op, ownership_outbox.c.subject).where(
        ownership_outbox.c.object == obj,
        ownership_outbox.c.status.in_(UNDELIVERED),
        ownership_outbox.c.op.in_(REVOKING_OPS),
    )
    if subject is not None:
        q = q.where(
            (ownership_outbox.c.op == OP_PURGE_OBJECT)
            | (ownership_outbox.c.subject == subject)
            | ~ownership_outbox.c.subject.like("user:%")
        )
    rows = session.execute(q).all()
    if not rows:
        return False

    delete_subjects = {
        r.subject for r in rows if r.op == OP_DELETE and r.subject is not None
    }
    if object_id is not None:
        role_change_subjects = _role_change_subjects_indexed(
            session, asset_type, object_id, delete_subjects
        )
    else:
        role_change_subjects = _role_change_subjects_by_uuid(
            session, asset_type, object_uuid, delete_subjects
        )
    return any(
        not (r.op == OP_DELETE and r.subject in role_change_subjects) for r in rows
    )


def pending_revocations_for(
    subject: str,
    asset_type: str,
    object_id_by_uuid: Mapping[str, int],
    session: Any = None,
) -> set[str]:
    """Object refs (`type:uuid`) `has_pending_revocation` would deny `subject`
    on, RIGHT NOW, for objects of `asset_type`, answered for every object in
    ONE bounded, indexed read instead of one query per object (issue #84).

    The access decision already closes the revocation window per object
    (`has_pending_revocation`, above); the list filter and, through FAB's
    list base filter, single-object GET did not, because they build their
    candidate set from the store's reverse index (`list_objects`), which
    lags the outbox exactly like `check` does. This is that same rule --
    same UNDELIVERED statuses, same REVOKING_OPS, same role-change carve-out,
    same fail-closed group rule -- run once across the whole outbox instead
    of once per candidate, so `owned_or_shared_rows` can subtract the result
    from what the store just said is reachable before a single row reaches
    the caller.

    SHAPE (PR #94 review, H-1). The first cut answered this with a single
    query whose role-change carve-out joined `ownership_object` to
    `ownership_share` inside a correlated `NOT EXISTS`, keyed on a string
    concatenation (`asset_type || ':' || object_uuid`). There is no index on
    `object_uuid`, and the concatenation is not sargable either way, so
    Postgres's only plan was a hash of BOTH mirror tables in full -- cost
    proportional to the size of the mirror, not to the (usually tiny)
    undelivered backlog, paid on every list request and every single-object
    GET. This shape is two bounded reads instead:

      1. the undelivered backlog for `subject` (indexed on
         `ix_ownership_outbox_status_id`; `object LIKE '<asset_type>:%'`
         narrows it to this call's asset type -- the outbox has no
         per-type index, but this filters rows already pulled off the
         status index, not a second scan);
      2. for the `delete` rows in that backlog ONLY (nothing else can be a
         role change; see `has_pending_revocation`), whether the row's
         subject still holds a mirror share row on that object -- resolved
         by looking up `object_id` in `object_id_by_uuid` (the caller's own
         `ownership_object` scan; nothing here reads that table again) and
         probing `uq_ownership_share_subject (asset_type, object_id,
         subject)`.

    An object whose uuid is not in `object_id_by_uuid` cannot be one of the
    caller's candidates anyway (`owned_or_shared_rows` only subtracts
    against its own scan), so it is left out of the carve-out lookup -- it
    still counts as a plain revocation from query 1, which is the safe
    (fail-closed) default.

    GROUPS: a revoke of `group:<id>#member` is included here whenever ANY
    undelivered row names a non-`user:` subject, regardless of whether this
    particular subject is actually a member of that group -- the same
    fail-closed, cheap rule `has_pending_revocation` uses, for the same
    reason (resolving membership would need a store read, and the whole
    point of this path is that it does not read the store). The cost: a
    group revocation hides the object from every subject asking, member or
    not, for the seconds until the drain delivers it -- never fewer objects
    than a fresh `check` would grant, sometimes (briefly) more.
    """
    session = session or _session()
    q = select(
        ownership_outbox.c.op, ownership_outbox.c.subject, ownership_outbox.c.object
    ).where(
        ownership_outbox.c.status.in_(UNDELIVERED),
        ownership_outbox.c.op.in_(REVOKING_OPS),
        ownership_outbox.c.object.like(f"{asset_type}:%"),
        (ownership_outbox.c.op == OP_PURGE_OBJECT)
        | (ownership_outbox.c.subject == subject)
        | ~ownership_outbox.c.subject.like("user:%"),
    )
    rows = session.execute(q).all()
    if not rows:
        return set()

    # (object_id, subject) -> object ref, for the delete rows whose object we
    # can resolve from what the caller already scanned. Only these can be a
    # role change, so this is the whole set query 2 needs to check.
    candidates: dict[tuple[int, str], str] = {}
    for r in rows:
        if r.op != OP_DELETE:
            continue
        _, _, uuid = r.object.partition(":")
        object_id = object_id_by_uuid.get(uuid)
        if object_id is not None:
            candidates[(object_id, r.subject)] = r.object

    role_change_refs: set[str] = set()
    if candidates:
        ids = {object_id for object_id, _ in candidates}
        subjects = {row_subject for _, row_subject in candidates}
        found = session.execute(
            select(ownership_share.c.object_id, ownership_share.c.subject).where(
                ownership_share.c.asset_type == asset_type,
                ownership_share.c.object_id.in_(ids),
                ownership_share.c.subject.in_(subjects),
            )
        ).all()
        for object_id, row_subject in found:
            if object_id is None or row_subject is None:
                continue
            ref = candidates.get((object_id, row_subject))
            if ref is not None:
                role_change_refs.add(ref)

    return {
        r.object
        for r in rows
        if not (r.op == OP_DELETE and r.object in role_change_refs)
    }


# --------------------------------------------------------------------------- drain


def _apply(row: dict[str, Any], authorizer: Any) -> None:
    """Deliver one row, strictly: a failure surfaces as StoreUnavailable
    (retry) or StoreRejected (park), classified by the client from the
    store's own response. A backend that answers False instead of raising
    is treated as a rejection -- it answered, and said no.

    Issue #82: `write_tuple` / `delete_tuple` / `revoke_subject` (the
    facade, above) invalidate the subject's `list_objects` cache entry at
    ENQUEUE time, which is what makes "share, then immediately list" show
    the share within the SAME request without waiting on the TTL. But a
    read between the enqueue and this delivery is not wrong to publish a
    fresh cache entry of its own -- the store genuinely still has the OLD
    answer until this call lands -- and that entry would otherwise outlive
    the delivery and answer stale, past the point the store's answer
    actually changed, for up to a full TTL. So THIS is the second, and
    authoritative, invalidation: the moment the store's answer for
    (asset_type, subject) actually changes, seconds after the enqueue, the
    common case. With the outbox DISABLED this function is never called at
    all -- the facade's pre-write invalidation (`write_tuple` / `delete_
    tuple` / `revoke_subject`, above) is the only one there is, and it runs
    BEFORE the inline store write, so the straddle it leaves behind is the
    same shape as the enqueued case's, not a second call site collapsing
    into the first.
    """
    op, subject, relation, obj = (
        row["op"],
        row["subject"],
        row["relation"],
        row["object"],
    )
    if op == OP_WRITE:
        ok = authorizer.write_tuple(subject, relation, obj, strict=True)
    elif op == OP_DELETE:
        ok = authorizer.delete_tuple(subject, relation, obj, strict=True)
    elif op == OP_SET_TENANT:
        asset_type, _, object_uuid = obj.partition(":")
        ok = authorizer.set_object_tenant(asset_type, object_uuid, subject, strict=True)
    elif op == OP_PURGE_OBJECT:
        ok = authorizer.purge_object(obj)
    elif op == OP_REVOKE_SUBJECT:
        ok = authorizer.revoke_subject(subject, obj)
    else:
        raise StoreRejected(f"unknown outbox op {op!r}")
    if not ok:
        raise StoreRejected(
            f"authorization store did not accept {op} {subject} {relation} {obj}"
        )
    if op in (OP_WRITE, OP_DELETE, OP_REVOKE_SUBJECT):
        # Nit (review round 2, PR #100): the store write above already
        # succeeded -- `drain()` is about to record this row DELIVERED.
        # This call runs inside `drain()`'s own try/except, so letting an
        # exception out of it -- from a cache backend behaving in some way
        # none of its own internal handlers anticipated -- would be caught
        # there, classified as the STORE's own rejection, and park an
        # already-delivered row DEAD. A cache failure is never that: log it
        # and move on, never let it retroactively un-deliver a write that
        # landed.
        try:
            _invalidate_list_objects(obj, subject)
        except Exception:  # noqa: BLE001 - never turn a delivered write into a parked row
            logger.warning(
                "outbox: list_objects cache invalidation failed for %s %s after a "
                "successful %s delivery; the row is still DELIVERED",
                subject,
                obj,
                op,
                exc_info=True,
            )


def _reap_stale_claims(session: Any) -> int:
    """Claims older than the lease belong to a drain that died. Free them."""
    cutoff = _utcnow() - CLAIM_LEASE
    n = session.execute(
        update(ownership_outbox)
        .where(
            ownership_outbox.c.status == STATUS_CLAIMED,
            ownership_outbox.c.claimed_at < cutoff,
        )
        .values(status=STATUS_PENDING, claimed_at=None, updated_on=_utcnow())
    ).rowcount
    if n:
        session.commit()
        logger.warning(
            "outbox: reclaimed %s stale claim(s) older than %s", n, CLAIM_LEASE
        )
    return n


def _earlier_undelivered_exists(row_id: Any, obj: Any):
    """Correlated predicate: some row with a smaller id on the same object is
    still pending, claimed, or dead."""
    o2 = ownership_outbox.alias("o2")
    return exists().where(
        o2.c.object == obj,
        o2.c.id < row_id,
        o2.c.status.in_(UNDELIVERED),
    )


def _claim(session: Any, row_id: int, claim_ts: datetime) -> int:
    """Take one row, atomically, and only if nothing earlier for its object is
    undelivered anywhere. One statement, so there is no gap between the two
    conditions. Returns the rowcount: 1 = ours, 0 = someone else has the row
    or the object is not free. Commits."""
    n = session.execute(
        update(ownership_outbox)
        .where(
            ownership_outbox.c.id == row_id,
            ownership_outbox.c.status == STATUS_PENDING,
            ~_earlier_undelivered_exists(row_id, ownership_outbox.c.object),
        )
        .values(status=STATUS_CLAIMED, claimed_at=claim_ts)
    ).rowcount
    session.commit()
    return n


def _release(session: Any, row_id: int, claim_ts: datetime) -> None:
    """Hand a claimed row back untouched (interrupted mid-delivery)."""
    session.execute(
        update(ownership_outbox)
        .where(
            ownership_outbox.c.id == row_id,
            ownership_outbox.c.status == STATUS_CLAIMED,
            ownership_outbox.c.claimed_at == claim_ts,
        )
        .values(status=STATUS_PENDING, claimed_at=None, updated_on=_utcnow())
    )
    session.commit()


def _settle(session: Any, row_id: int, claim_ts: datetime, **values: Any) -> bool:
    """Record a row's outcome -- only if the claim is still ours. A drain
    that outlived its lease finds the row re-claimed (claimed_at differs) or
    already settled by the re-claimer; its verdict is then discarded, never
    written over the other's. Returns whether the write landed."""
    n = session.execute(
        update(ownership_outbox)
        .where(
            ownership_outbox.c.id == row_id,
            ownership_outbox.c.status == STATUS_CLAIMED,
            ownership_outbox.c.claimed_at == claim_ts,
        )
        .values(**values)
    ).rowcount
    session.commit()
    if n != 1:
        logger.warning(
            "outbox #%s: outcome discarded, the claim was reaped and re-taken while this drain "
            "was delivering (lease %s)",
            row_id,
            CLAIM_LEASE,
        )
    return n == 1


def drain(
    limit: int = 100,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    session: Any = None,
    authorizer: Any = None,
) -> dict[str, Any]:
    """Deliver pending rows to the authorization store. Safe to run anytime,
    from any number of processes at once.

    Each row's outcome is committed on its own, so a crash loses at most one
    row's claim -- which the lease reaper frees -- and re-delivery is
    idempotent (the store treats "already exists"/"does not exist" as success).
    """
    session = session or _session()
    authorizer = authorizer or get_authorizer()
    stats = {
        "delivered": 0,
        "failed": 0,
        "dead": 0,
        "skipped": 0,
        "circuit_open": False,
        "reclaimed": 0,
    }
    stats["reclaimed"] = _reap_stale_claims(session)

    # Deliverable rows, in id order. A row behind a dead or in-flight row for
    # the same object is excluded IN SQL so `limit` counts deliverable rows:
    # rows parked behind a dead object can never starve everything queued
    # after them. (Earlier *pending* rows are not excluded: they are in this
    # same list, ahead, and are applied first.) The claim below re-checks all
    # of this atomically; this query only decides what is worth trying.
    o2 = ownership_outbox.alias("o2")
    q = (
        select(ownership_outbox)
        .where(
            ownership_outbox.c.status == STATUS_PENDING,
            ~exists().where(
                o2.c.object == ownership_outbox.c.object,
                o2.c.id < ownership_outbox.c.id,
                o2.c.status.in_((STATUS_DEAD, STATUS_CLAIMED)),
            ),
        )
        .order_by(ownership_outbox.c.id)
        .limit(limit)
    )
    pending = [dict(r) for r in session.execute(q).mappings().all()]

    blocked: set[str] = set()  # objects that must not advance in THIS pass
    consecutive_transport_failures = 0
    for row in pending:
        if row["object"] in blocked:
            stats["skipped"] += 1
            continue

        # Atomic claim: only one drain wins this row, and only while nothing
        # earlier for the object is undelivered anywhere. Losing means another
        # drain has it, or is ahead of us on this object -- either way the
        # object is closed to us for the rest of the pass.
        claim_ts = _utcnow()
        if _claim(session, row["id"], claim_ts) != 1:
            blocked.add(row["object"])
            stats["skipped"] += 1
            continue

        try:
            _apply(row, authorizer)
        except BaseException as exc:  # noqa: BLE001 - every outcome is recorded below
            if isinstance(exc, _INTERRUPTS) or not isinstance(exc, Exception):
                # Not the store's verdict: we were told to stop. Hand the row
                # back untouched and let the signal through -- the signal,
                # not a database error met while releasing (the lease reaper
                # frees the row in that case).
                try:
                    _release(session, row["id"], claim_ts)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "outbox #%s: could not release the claim on interrupt",
                        row["id"],
                    )
                raise
            now = _utcnow()
            attempts = row["attempts"] + 1
            transport = isinstance(exc, StoreUnavailable)
            if transport and attempts < max_attempts:
                status = STATUS_PENDING
            else:
                status = STATUS_DEAD  # rejected, or out of retries
            settled = _settle(
                session,
                row["id"],
                claim_ts,
                attempts=attempts,
                status=status,
                claimed_at=None,
                last_error=str(exc)[:2000],
                updated_on=now,
            )
            blocked.add(row["object"])
            if not settled:
                continue  # not our row any more; nothing to count or announce
            if status == STATUS_DEAD:
                stats["dead"] += 1
                logger.error(
                    "outbox #%s DEAD (%s, attempt %s): %s",
                    row["id"],
                    "unreachable, retries exhausted"
                    if transport
                    else "rejected by store",
                    attempts,
                    exc,
                )
            else:
                stats["failed"] += 1
                logger.warning(
                    "outbox #%s attempt %s, store unavailable: %s",
                    row["id"],
                    attempts,
                    exc,
                )
            if transport:
                consecutive_transport_failures += 1
                if consecutive_transport_failures >= CONSECUTIVE_FAILURE_LIMIT:
                    stats["circuit_open"] = True
                    logger.warning(
                        "outbox: %s consecutive transport failures; ending pass",
                        consecutive_transport_failures,
                    )
                    break
            continue

        now = _utcnow()
        if _settle(
            session,
            row["id"],
            claim_ts,
            status=STATUS_DONE,
            attempts=row["attempts"] + 1,
            claimed_at=None,
            done_on=now,
            updated_on=now,
            last_error=None,
        ):
            stats["delivered"] += 1
        consecutive_transport_failures = 0

    if stats["delivered"] or stats["failed"] or stats["dead"] or stats["reclaimed"]:
        logger.info("outbox drain: %s", stats)
    elif stats["skipped"]:
        # Nothing moved and something is stuck -- say so, or a dead object
        # silently freezes its queue forever.
        logger.warning(
            "outbox drain: %s pending row(s) skipped behind blocked objects",
            stats["skipped"],
        )
    return stats


def status(session: Any = None) -> dict[str, Any]:
    """Counts by status, what is stuck, and how old the oldest problem is."""
    session = session or _session()
    counts = {
        r[0]: r[1]
        for r in session.execute(
            select(ownership_outbox.c.status, func.count()).group_by(
                ownership_outbox.c.status
            )
        )
    }
    dead_objects = {
        r[0]
        for r in session.execute(
            select(ownership_outbox.c.object).where(
                ownership_outbox.c.status == STATUS_DEAD
            )
        )
    }
    blocked = 0
    if dead_objects:
        blocked = session.execute(
            select(func.count())
            .select_from(ownership_outbox)
            .where(
                ownership_outbox.c.status == STATUS_PENDING,
                ownership_outbox.c.object.in_(dead_objects),
            )
        ).scalar_one()

    def _iso(v):
        return (v.isoformat() + "Z") if v else None

    def oldest(st: str):
        return _iso(
            session.execute(
                select(func.min(ownership_outbox.c.created_on)).where(
                    ownership_outbox.c.status == st
                )
            ).scalar()
        )

    # When something last reached the store: the operator's "is the drain
    # running at all?" signal.
    last_delivered = _iso(
        session.execute(select(func.max(ownership_outbox.c.done_on))).scalar()
    )
    # Stalled: work is waiting and has been for longer than any healthy drain
    # cadence explains -- no worker, no beat, or a breaker tripping on every
    # tick. `pending` alone is normal for a few seconds; this is not.
    oldest_pending_at = session.execute(
        select(func.min(ownership_outbox.c.created_on)).where(
            ownership_outbox.c.status == STATUS_PENDING
        )
    ).scalar()
    stalled = bool(oldest_pending_at) and oldest_pending_at < _utcnow() - STALL_AFTER

    return {
        "pending": counts.get(STATUS_PENDING, 0),
        "claimed": counts.get(STATUS_CLAIMED, 0),
        "done": counts.get(STATUS_DONE, 0),
        "dead": counts.get(STATUS_DEAD, 0),
        "blocked": blocked,  # pending rows frozen behind a dead object
        "oldest_pending": oldest(STATUS_PENDING),
        "oldest_dead": oldest(STATUS_DEAD),
        "last_delivered": last_delivered,
        "stalled": stalled,
        "enabled": enabled(),
    }


def dead_rows(session: Any = None) -> list[dict[str, Any]]:
    """What an operator needs to decide what to do: id, op, object, error."""
    session = session or _session()
    return [
        {
            "id": r["id"],
            "op": r["op"],
            "subject": r["subject"],
            "object": r["object"],
            "attempts": r["attempts"],
            "last_error": r["last_error"],
        }
        for r in session.execute(
            select(ownership_outbox)
            .where(ownership_outbox.c.status == STATUS_DEAD)
            .order_by(ownership_outbox.c.id)
        ).mappings()
    ]


def replay_dead(row_ids: Optional[list[int]] = None, session: Any = None) -> int:
    """Return dead rows to pending so the next drain retries them."""
    session = session or _session()
    stmt = (
        update(ownership_outbox)
        .where(ownership_outbox.c.status == STATUS_DEAD)
        .values(
            status=STATUS_PENDING, attempts=0, last_error=None, updated_on=_utcnow()
        )
    )
    if row_ids:
        stmt = stmt.where(ownership_outbox.c.id.in_(row_ids))
    n = session.execute(stmt).rowcount
    session.commit()
    return n


def discard_dead(
    row_ids: Optional[list[int]] = None,
    session: Any = None,
    *,
    including_revocations: bool = False,
) -> int:
    """Delete dead rows so the object's queue can move again.

    The escape hatch issue #118 showed was missing. A row the store will
    never accept (a subject it refuses outright) stays `dead` forever, and
    because delivery is ordered per object, every later intent for that
    object waits behind it: the API keeps answering 200 while nothing
    reaches the store, and `check` stays red with no supported way back.
    `replay` cannot help -- the next drain kills the row again -- and
    `prune` only deletes delivered rows.

    Deliberately destructive and deliberately narrow: it discards the
    INTENT, not the mirror row. What the store then holds for that object
    is whatever it held before, so an operator's next step is
    `reconcile --write` (owner tuples) or re-issuing the share; both are
    visible in `check`. Takes explicit row ids, or every dead row.

    A REVOKING row is not discarded by this (review round 1). A dead
    `revoke_subject`, `delete` or `purge_object` row is not merely an
    undelivered intent: `has_pending_revocation` counts dead rows ON
    PURPOSE, so while it sits there the read gate denies the subject
    locally even though the store's tuple was never removed. Discarding it
    therefore does not leave access as it was -- it GRANTS, silently, and
    `reconcile` will not take the tuple away again (it never deletes a
    share tuple the mirror has lost). Those rows need
    `discard_dead(..., including_revocations=True)`, which the CLI puts
    behind its own flag and its own warning.
    """
    session = session or _session()
    revoking = (OP_REVOKE_SUBJECT, OP_DELETE, OP_PURGE_OBJECT)
    base = [ownership_outbox.c.status == STATUS_DEAD]
    if row_ids:
        base.append(ownership_outbox.c.id.in_(row_ids))

    held_back = 0
    if not including_revocations:
        held_back = len(
            session.execute(
                select(ownership_outbox.c.id).where(
                    *base, ownership_outbox.c.op.in_(revoking)
                )
            ).all()
        )

    stmt = delete(ownership_outbox).where(*base)
    if not including_revocations:
        stmt = stmt.where(ownership_outbox.c.op.notin_(revoking))
    n = session.execute(stmt).rowcount
    session.commit()
    if n:
        logger.warning(
            "superset_ownership: discarded %d dead outbox row(s) %s; the store was "
            "NOT changed -- run `ownership check` and re-issue anything still missing",
            n,
            row_ids or "(all)",
        )
    if held_back:
        logger.warning(
            "superset_ownership: kept %d dead REVOKING row(s): discarding one restores "
            "access the store never took away. Resolve the store, then `outbox replay` "
            "-- or discard them knowingly with --including-revocations and remove the "
            "tuples yourself",
            held_back,
        )
    if including_revocations and n:
        logger.warning(
            "superset_ownership: revoking rows were among them; subjects denied only by "
            "those rows can read again NOW. Remove their tuples in the store, then "
            "`ownership check`",
        )
    return n


def prune_done(older_than: timedelta, session: Any = None) -> int:
    """Delete `done` rows settled before the cutoff. Storage only: every
    hot query is indexed away from done rows, so this is housekeeping, and
    it never touches pending, claimed or dead rows."""
    session = session or _session()
    n = session.execute(
        delete(ownership_outbox).where(
            ownership_outbox.c.status == STATUS_DONE,
            ownership_outbox.c.done_on < _utcnow() - older_than,
        )
    ).rowcount
    session.commit()
    return n


def warn_if_no_drainer(app: Any) -> None:
    """The foot-gun: outbox on, nothing draining it, every share denied forever.
    Called at startup; a WARNING is the least it deserves."""
    if not enabled():
        return
    cfg = app.config.get("CELERY_CONFIG")
    if cfg is None:
        schedule: dict = {}
    elif isinstance(cfg, dict):
        schedule = cfg.get("beat_schedule") or {}
    else:  # a config class, as Superset conventionally uses
        schedule = getattr(cfg, "beat_schedule", None) or {}
    if not any(e.get("task") == TASK_NAME for e in schedule.values()):
        logger.warning(
            "superset_ownership: the outbox is ENABLED but no Celery beat entry runs %s. "
            "Sharing changes will queue and never reach the authorization store unless "
            "`superset ownership outbox drain` is run by something.",
            TASK_NAME,
        )


# --------------------------------------------------------------------------- celery
#
# The production drain, on Superset's own Celery app so it rides the worker and
# beat the deployment already runs. Superset's task base provides the Flask app
# context. Only an ImportError (no Superset in this process: bare CLI, unit
# tests) is tolerated, and it is logged; anything else is a real problem.

try:
    from superset.extensions import celery_app
except ImportError as _exc:
    logger.debug("superset_ownership.outbox: Celery task not registered (%s)", _exc)
    drain_task = None  # type: ignore[assignment]
else:

    @celery_app.task(
        name=TASK_NAME,
        ignore_result=True,
        soft_time_limit=TASK_SOFT_TIME_LIMIT,
        time_limit=TASK_TIME_LIMIT,
    )
    def drain_task() -> dict[str, Any]:
        # A pass is at most `limit` rows x the client's timeout per failure,
        # plus one delete per tuple for a purge; kept well inside the soft
        # limit, and the soft limit itself hands the current row back
        # (see _INTERRUPTS) rather than recording it as the store's verdict.
        return drain(limit=50)

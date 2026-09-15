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
"""Audit events for ownership, sharing and visibility changes.

Ivanti emits audit records onto an internal Service Bus and asked Preset to
define the events and the interface, with Ivanti implementing the bridge.
This module is that contract.

Every mutating operation emits exactly one event, AFTER the transaction it
describes has committed. (One event, `owner_refused`, describes a write
that was REFUSED and so has no transaction; it is the exception and says so
in the catalogue below.) That ordering is deliberate and it is a trade: an
event is never emitted for a change that was rolled back, but a process that
dies between the commit and the emit loses the record of a change that did
happen. Emitting inside the transaction would swap those two failures for
each other. Events are therefore a record of committed changes, not a
write-ahead log, and a deployment that needs the stronger guarantee should
have the sink write to the same database in the same transaction -- which is
why the sink is a config hook rather than something this module owns.

Nothing here talks to a message bus: a deployment supplies a sink, and the
default sink writes to the application log so an instance with nothing
configured still records what happened.

To bridge, set one config value:

    def to_service_bus(event: dict) -> None:
        ...                     # your code

    OWNERSHIP_AUDIT_SINK = to_service_bus

The sink is called with a plain dict, is never passed anything it cannot
JSON-encode, and its exceptions are swallowed and logged -- an audit
transport failing must not fail the user's request.

EVENT CATALOGUE

    ownership.visibility_changed   before/after: {"visibility": ...}
    ownership.share_added          after:  {"subject", "role"}
    ownership.share_removed        before: {"subject"}
    ownership.owner_assigned       before/after: {"owner_user_id": ...}
    ownership.owner_refused        before/after: {"owner_user_id": ...}
                                   (IDENTICAL: the owner that stays in place)
                                   attempted: {"owner_user_id": <recipient>,
                                               "reason": "missing_grant" |
                                                         "unverifiable" |
                                                         "no_dataset"}
                                   NOT a change: nothing was written, and
                                   `before` == `after` says so in the shape,
                                   not only in this text -- a consumer that
                                   folds events into current state needs no
                                   special case for it. Emitted when a
                                   transfer or claim was refused because the
                                   recipient does not hold the baseline
                                   permission (the underlying dataset grant)
                                   for the object, because that permission
                                   could not be evaluated, or because the
                                   object's dataset does not resolve (a chart
                                   whose dataset is gone; a dashboard none of
                                   whose charts' datasets resolve). Recorded
                                   because an attempt to hand an object to
                                   someone outside its dataset grant is the
                                   signal worth keeping, not the refusal
                                   itself.
                                   The only event carrying `attempted`.
    ownership.object_created       after:  {"visibility", "owner_user_id"}
    ownership.owner_removed_by_user_delete
                                   before: {"owner_user_id": <the user>}
                                   after:  {"owner_user_id": null}
                                   One per object the user owned, emitted
                                   when a FAB User is hard-deleted through
                                   the ORM (`session.delete(user)`; FAB's
                                   user admin, a script). The un-owning
                                   itself is the database's: revision 0003's
                                   owner foreign key is ON DELETE SET NULL,
                                   so the row reads NULL afterwards, which is
                                   indistinguishable from never-owned. This
                                   event is the record of who owned it. The
                                   actor is whoever is making the request,
                                   None from a shell. NOT emitted for a Core
                                   `DELETE FROM ab_user` (no ORM event fires;
                                   the constraint still nulls the owner, and
                                   `check`/`reconcile` are the record then),
                                   nor for a deactivation (`active=false`),
                                   which changes no row of ours.
    ownership.tenant_purged        after:  {"tenant", "counts": {...}}
    ownership.access_corrected     before: {"enforced", "direct_viewers",
                                            "direct_editors"}
                                   after:  {"enforced": true, "visibility",
                                            "direct_viewers": 0, "direct_editors": 0}
                                   Emitted when a write would have broken the
                                   viewers/editors invariant on a governed
                                   object and the guard corrected it.
                                   `before.enforced` false means denial had
                                   been removed (a re-arm); True with
                                   direct_viewers/direct_editors > 0 means a
                                   native grant was stripped while denial was
                                   already intact.

                                   INVESTIGATE, do not alarm. An ordinary PUT
                                   that does not echo `viewers`/`editors` back
                                   produces it, so firing is not evidence of an
                                   attack, and it is NOT the detection story
                                   for what the guard cannot see (raw DML, or
                                   deleting the sentinel subject) -- neither
                                   produces an event. `superset ownership
                                   check` on a schedule is that control.

EVENT SHAPE

    {
      "event":       "ownership.share_added",
      "occurred_at": "2026-09-10T18:22:31.441Z",   # UTC, ISO 8601
      "actor": {
        "superset_id": 11,
        "member_guid": "3f0a91c7-...",             # None outside Ivanti's model
        "tenant_guid": "a1e4c2d0-...",             # None if the actor has no tenant
        "username":    "3f0a91c7-..."
      },
      "object": {"type": "dashboard", "id": 5, "uuid": "c7bc10f4-..."},
      "before": {...} | None,
      "after":  {...} | None,
      "attempted": {...},                          # owner_refused only; absent otherwise
      "admitted_by": "owner",                      # the API's writes; see below
      "request_id": "..."                          # correlation, when available
    }

ADMITTED_BY

    The events the REST API's four writes emit -- visibility_changed,
    share_added, share_removed, owner_assigned -- and the owner_refused that
    a transfer or claim can produce instead, carry `admitted_by`: the rule
    under which the caller was allowed to make the change.

        "owner"              the object's recorded owner
        "tenant_admin"       a tenant administrator, on their own tenant's object
                             (ownership writes only: transfer, claim, release;
                             a sharing write is refused on this ground, or
                             admitted as "manage_permission" when the
                             administrator also holds that role)
        "admin"              a Superset admin
        "manage_permission"  a holder of the optional manage-sharing role
                             (OWNERSHIP_MANAGE_PERMISSION; spec section 14.5),
                             on an owned object of their own tenant they
                             could already see. Recorded only on the sharing
                             actions that ground admits: a share, unshare or
                             visibility change (never to public) and a
                             transfer to a third party, or the owner_refused
                             such a transfer produces. A claim, a release, a
                             self-share or a self-transfer under it is a 403
                             and emits nothing.
        "visibility_change"  share_removed only (issue #93): `private`
                             revokes every existing share of the object as
                             a CONSEQUENCE of the visibility write, not a
                             separate share-management decision, so the
                             ground recorded on each of those events is the
                             change itself, not whichever of the four
                             grounds above admitted the caller to make it
                             (that ground is on the visibility_changed event
                             the same request also emits).

    Precedence when several hold: admin > owner > tenant_admin >
    manage_permission -- the broadest ground is the one recorded. The key is
    present only on those events; object_created, tenant_purged and
    access_corrected are not admitted by a manage rule and keep their shape.

    WHAT TO WATCH FOR under "manage_permission". The permission's matrix
    closes every SINGLE write that would benefit the holder (no self-share,
    self-transfer, claim, release or public). It does not close two people
    each holding a ground on the object acting in turn, and is not meant
    to: refusing that would need the seam to track who shared whom under
    which ground, which no ground does. The paths, each fully attributable
    from this trail, are:

      * a round trip: `owner_assigned` with admitted_by "manage_permission"
        (holder H hands V's object to C), then `owner_assigned` with
        admitted_by "owner" whose actor is C and whose `after` is H. Two
        events, and V never appears as an actor;
      * mutual promotion: two `share_added` events with admitted_by
        "manage_permission" whose actor and `after.subject` are swapped
        (H1 shares H2 as editor, H2 shares H1), each subject holding the
        configured role;
      * a holder narrowing a public object to private, which locks out
        every reader who had no share (`visibility_changed`, public ->
        private, admitted_by "manage_permission"); the owner can undo it,
        the holder cannot.

    A consumer that alerts on `admitted_by == "manage_permission"` where
    the event's subject (the new owner, the shared subject) also holds the
    configured role has the signal for the first two.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Optional

logger = logging.getLogger(__name__)

VISIBILITY_CHANGED = "ownership.visibility_changed"
SHARE_ADDED = "ownership.share_added"
SHARE_REMOVED = "ownership.share_removed"
OWNER_ASSIGNED = "ownership.owner_assigned"
OWNER_REFUSED = "ownership.owner_refused"
OBJECT_CREATED = "ownership.object_created"
OWNER_REMOVED_BY_USER_DELETE = "ownership.owner_removed_by_user_delete"
TENANT_PURGED = "ownership.tenant_purged"
ACCESS_CORRECTED = "ownership.access_corrected"


def _actor(user: Any) -> dict[str, Any]:
    if user is None:
        return {
            "superset_id": None,
            "member_guid": None,
            "tenant_guid": None,
            "username": None,
        }
    from superset_ownership.identity import resolve_member_guid, resolve_tenant_guid

    return {
        "superset_id": getattr(user, "id", None),
        "member_guid": resolve_member_guid(user),
        "tenant_guid": resolve_tenant_guid(user),
        "username": getattr(user, "username", None),
    }


def _request_id() -> Optional[str]:
    try:
        from flask import request

        return request.headers.get("X-Request-Id") or request.headers.get(
            "X-Correlation-Id"
        )
    except Exception:  # outside a request
        return None


def build(
    event: str,
    actor: Any = None,
    asset_type: Optional[str] = None,
    object_id: Optional[int] = None,
    object_uuid: Optional[str] = None,
    before: Optional[dict] = None,
    after: Optional[dict] = None,
    attempted: Optional[dict] = None,
    admitted_by: Optional[str] = None,
) -> dict[str, Any]:
    """The event record. Separated from emission so it can be asserted in tests.

    `attempted` describes a write that was REFUSED (`owner_refused`);
    `admitted_by` names the rule that let the caller make (or attempt) the
    change. Each key is present only when given, so every other event keeps
    its exact shape.
    """
    record: dict[str, Any] = {
        "event": event,
        "occurred_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "actor": _actor(actor),
        "object": {"type": asset_type, "id": object_id, "uuid": object_uuid},
        "before": before,
        "after": after,
        "request_id": _request_id(),
    }
    if attempted is not None:
        record["attempted"] = attempted
    if admitted_by is not None:
        record["admitted_by"] = admitted_by
    return record


@lru_cache(maxsize=8)
def _resolve_sink_ref(ref: str) -> Any:
    """A dotted `OWNERSHIP_AUDIT_SINK` string, resolved once per distinct
    value (the import is not free, and this is read on every emit)."""
    from superset_ownership.plugins import resolve_class_ref

    return resolve_class_ref(ref, aliases={}, protocol=None, key="OWNERSHIP_AUDIT_SINK")


def _resolved_sink() -> Any:
    """The configured sink, read fresh on every call (a per-emit read, not
    cached at import or at app-creation time -- the harness sets an
    instance after the app exists, and a config layer or the environment
    may change it on a running process for demonstration purposes) via one
    precedence (settings.get). A str value is resolved into a callable
    (cached by the string itself, see _resolve_sink_ref); an object already
    built (an instance, or a callable the harness set directly) is used
    as-is.
    """
    from superset_ownership import settings

    raw = settings.get("OWNERSHIP_AUDIT_SINK", None, settings.as_class_ref)
    if raw is None:
        return None
    if isinstance(raw, str):
        return _resolve_sink_ref(raw)
    return raw


def emit_record(record: dict[str, Any]) -> None:
    """Send an already-built record to the sink.

    Separate from emit() so a listener can BUILD an event during a flush and
    emit it only once that transaction commits -- the event's own timestamp and
    actor are captured at the moment of the change, but nothing is reported
    until the change is real.
    """
    sink = _resolved_sink()

    if sink is None:
        logger.info("AUDIT %s", record)
        return
    try:
        sink(record)
    except Exception:
        logger.exception(
            "superset_ownership: audit sink failed for %s", record.get("event")
        )


def emit(event: str, **kwargs: Any) -> None:
    """Emit one audit event through the configured sink.

    Never raises. An audit transport that is down must not take the user's
    request down with it -- the failure is logged and the request proceeds.
    Building the record is under the same guard: resolving the actor reads
    the user's roles, and if the request is being refused BECAUSE the
    database is unwell, that read can fail too; the refusal must still be
    answered as documented rather than as a 500 from the audit line.
    """
    try:
        record = build(event, **kwargs)
    except Exception:
        logger.exception("superset_ownership: could not build audit event %s", event)
        return
    emit_record(record)

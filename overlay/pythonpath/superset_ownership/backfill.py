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
One-shot backfill for every pre-existing dashboard/chart: visibility=public,
owner resolved by a fallback chain. Public means no sentinel is written, so
behaviour is unchanged for everything that already exists.

OWNER FALLBACK: created_by_fk -> changed_by_fk -> OWNERSHIP_DEFAULT_OWNER.
Example/imported objects usually have no recorded creator (created_by_fk is
NULL), and an object backfilled to public with a NULL owner is "owner
unknown": its own creator cannot take it private or share it, because every
management path needs an owner. Setting a default owner closes that. When no
default is configured the owner may still be NULL -- the previous behaviour,
kept for backward compatibility.

Run inside the container:
    python -c "from superset_ownership.backfill import run; run()"
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, Callable, Optional

# The permission predicate `holds(user_id, obj)`: one alias shared with the
# transfer decision and `first_holder`, so the backfill's fallback and the
# route's check are provably the same shape.
from superset_ownership.transfer import HoldsFn

try:  # pragma: no cover - exercised once superset_ownership.plugins lands
    from superset_ownership.plugins import get_directory
except ImportError:  # this branch predates the plugins.py loader (spec section 4)
    from superset_ownership.directory import get_directory

logger = logging.getLogger(__name__)


def _default_owner() -> int | None:
    """The configured last-resort owner, or None."""
    from superset_ownership import settings

    return settings.get("OWNERSHIP_DEFAULT_OWNER", None, settings.as_int)


ExistsFn = Callable[[int], bool]


def _user_exists_checker() -> ExistsFn:
    """`exists(user_id)`: is there a row in ab_user with this id? One query
    per distinct id per run.

    Since revision 0003 `owner_user_id` is a foreign key into ab_user, so an
    owner that does not exist is refused by the database -- as a raw
    IntegrityError from `upsert_ownership` that would abort the whole sweep
    and roll every object back. The two attribution sources nothing else
    validates are OWNERSHIP_DEFAULT_OWNER (an integer from the environment)
    and the creator history records (a user id that may since have been
    hard-deleted); `created_by_fk`/`changed_by_fk` are themselves foreign
    keys and always resolve, but are checked through the same cache for
    uniformity."""
    from sqlalchemy import text
    from superset import db

    known: dict[int, bool] = {}

    def exists(user_id: int) -> bool:
        if user_id not in known:
            known[user_id] = (
                db.session.execute(
                    text("SELECT 1 FROM ab_user WHERE id = :uid"), {"uid": int(user_id)}
                ).first()
                is not None
            )
        return known[user_id]

    return exists


# Continuum's history tables record WHICH USER ran each transaction, so the
# INSERT that first created a row names its creator even when the row's own
# `created_by_fk` is NULL (rows written before AuditMixin stamped it, rows
# created by an import, rows whose audit columns were dropped in a migration).
# operation_type 0 is INSERT.
_HISTORY_TABLES = {"dashboard": "dashboards_version", "chart": "slices_version"}


def _creator_from_history(asset_type: str, object_id: int) -> int | None:
    """The user whose transaction first inserted this row, if history knows.

    Best-effort: any problem (no versioning configured, table absent, schema
    differences) returns None so the caller falls through to the next source.
    """
    table = _HISTORY_TABLES.get(asset_type)
    if not table:
        return None
    try:
        from sqlalchemy import text

        from superset import db

        row = db.session.execute(
            text(
                f"""
                SELECT vt.user_id
                FROM {table} v
                JOIN version_transaction vt ON vt.id = v.transaction_id
                WHERE v.id = :oid
                  AND v.operation_type = 0
                  AND vt.user_id IS NOT NULL
                ORDER BY vt.id ASC
                LIMIT 1
                """
            ),
            {"oid": object_id},
        ).first()
        return int(row[0]) if row and row[0] is not None else None
    except Exception:  # noqa: BLE001 - history is a bonus source, never a blocker
        logger.debug(
            "no usable history for %s %s", asset_type, object_id, exc_info=True
        )
        return None


def _tenant_datasources() -> dict[int, set[str]]:
    """table_id -> the tenant guids entitled to it, built ONCE per run.

    Ivanti runs one cross-tenant instance and keeps tenants apart at the DATA
    layer: an RLS rule bound to the tenant role, on top of `datasource_access`
    granted to that same role. Either way the binding between a dataset and a
    tenant already exists -- which means an object's tenant is DERIVABLE from
    the data it reads, and an object need not be tenantless just because
    nobody recorded a tenant on it.

    Both bindings are read, RLS first: a deployment may grant broad
    datasource_access and separate tenants purely by row filter.
    """
    from superset import db
    from superset_ownership.identity import tenant_of_role_name

    def tenant_of(role_name: str) -> str | None:
        # Exactly the membership role -- never `tenant_administrator_<guid>`.
        return tenant_of_role_name(role_name)

    out: dict[int, set[str]] = {}

    def add(table_id, role_name):
        guid = tenant_of(role_name)
        if guid is not None and table_id is not None:
            out.setdefault(int(table_id), set()).add(guid)

    from sqlalchemy import text

    # 1. RLS filters bound to a tenant role -- Ivanti's stated mechanism.
    try:
        for table_id, role_name in db.session.execute(
            text(
                """
            SELECT ft.table_id, r.name
            FROM rls_filter_tables ft
            JOIN rls_filter_subjects fs ON fs.rls_filter_id = ft.rls_filter_id
            JOIN subjects sub ON sub.id = fs.subject_id
            JOIN ab_role r ON r.id = sub.role_id
            """
            )
        ):
            add(table_id, role_name)
    except Exception:  # noqa: BLE001 - schema varies; the grant scan still runs
        logger.debug("RLS tenant binding unreadable", exc_info=True)

    # 2. `datasource_access` granted to a tenant role. The view-menu name
    #    carries the table id, e.g. "[examples].[video_game_sales](id:20)".
    try:
        for table_id, role_name in db.session.execute(
            text(
                """
            SELECT substring(vm.name from 'id:([0-9]+)'), r.name
            FROM ab_role r
            JOIN ab_permission_view_role pvr ON pvr.role_id = r.id
            JOIN ab_permission_view pv ON pv.id = pvr.permission_view_id
            JOIN ab_permission p ON p.id = pv.permission_id
            JOIN ab_view_menu vm ON vm.id = pv.view_menu_id
            WHERE p.name = 'datasource_access'
              AND vm.name LIKE '%%id:%%'
            """
            )
        ):
            add(table_id, role_name)
    except Exception:  # noqa: BLE001
        logger.debug("datasource_access tenant binding unreadable", exc_info=True)

    return out


def _tenant_of_user(user_id: int | None) -> str | None:
    """The tenant of a Superset account, from its `tenant_<guid>` role."""
    if not user_id:
        return None
    try:
        from superset import security_manager as sm

        from superset_ownership.identity import resolve_tenant_guid

        return resolve_tenant_guid(sm.get_user_by_id(int(user_id)))
    except Exception:  # noqa: BLE001
        return None


def _tenant_from_creator(asset_type: str, obj) -> str | None:
    """The tenant of whoever created this object.

    THE primary signal. Ivanti's tenant identity reaches Superset as a
    tenant-specific FAB role (`tenant_<guid>`) created at SSO/JIT provisioning
    -- that is what their per-tenant RLS rule binds to, and what
    `resolve_tenant_guid` reads. A PERSON therefore belongs to exactly one
    tenant even where the DATA does not: their RLS rule is attached only to
    the device-derived datasets, so a dataset may be shared by several tenants
    or bound to none, while the creator's tenant is unambiguous either way.

    Deriving from the datasource alone was backwards for that reason: it works
    only where datasets happen to be partitioned one-per-tenant, which is a
    property of our local fixture, not of a real deployment.
    """
    for attr in ("created_by_fk", "changed_by_fk"):
        tenant = _tenant_of_user(getattr(obj, attr, None))
        if tenant:
            return tenant
    return _tenant_of_user(_creator_from_history(asset_type, obj.id))


def _derive_tenant(asset_type: str, obj, tenant_ds: dict[int, set[str]]) -> str | None:
    """The tenant an object belongs to.

    Creator first (see `_tenant_from_creator`), then the data it reads. A
    chart is its datasource's tenant; a dashboard is the tenant of the
    datasources its charts read. The datasource step returns None unless
    exactly one tenant matches -- a dataset shared by two tenants and
    separated only by row filters genuinely does not say which tenant a chart
    on it belongs to, and a wrong tenant is worse than none: it hands the
    object to one tenant's administrator and locks out the other's.
    """
    try:
        by_creator = _tenant_from_creator(asset_type, obj)
        if by_creator:
            return by_creator

        if asset_type == "chart":
            guids = tenant_ds.get(getattr(obj, "datasource_id", None) or -1, set())
        else:
            guids = set()
            for slc in getattr(obj, "slices", None) or []:
                guids |= tenant_ds.get(getattr(slc, "datasource_id", None) or -1, set())
        return next(iter(guids)) if len(guids) == 1 else None
    except Exception:  # noqa: BLE001
        logger.debug(
            "could not derive tenant for %s %s",
            asset_type,
            getattr(obj, "id", "?"),
            exc_info=True,
        )
        return None


def _admins_of_tenant(tenant_guid: str) -> list[int]:
    """Active Superset ids of a tenant's administrators, from the directory.

    The active-account filter stays here (`Directory.tenant_administrators`
    already answers "who administers this tenant"; whether that account is
    still active on THIS Superset instance is not the directory's question
    to answer twice).
    """
    ids: list[int] = []
    try:
        for u in get_directory().tenant_administrators(tenant_guid):
            if u["superset_id"] is not None:
                ids.append(u["superset_id"])
    except Exception:  # noqa: BLE001
        logger.warning(
            "could not read administrators of tenant %s", tenant_guid, exc_info=True
        )
    return sorted(ids)


def _tenant_admins() -> list[int]:
    """Superset ids of the tenant's administrators -- ONLY when unambiguous.

    Returns [] unless the instance has exactly ONE tenant. This guard is the
    whole point: an ungoverned legacy object carries no tenant (there is no
    tenant column; tenancy lives in the authorization store and is stamped
    when an owner is assigned). With two tenants there is no signal saying
    which one a pre-existing chart belongs to, and guessing is not neutral --
    assigning it stamps the object into that tenant, and
    `_manage_reason` then refuses the OTHER tenant's administrator,
    who could manage it a moment earlier while it was untenanted. A wrong
    guess is strictly worse than leaving it claimable.

    Sorted by Superset id so the choice is deterministic across re-runs.
    """
    from superset import security_manager as sm

    from superset_ownership.identity import tenant_of_role_name

    # OpenFGA has no "list every tenant" call -- /read needs a concrete
    # `tenant:<guid>`. So the FAB roles supply CANDIDATE guids (the only
    # enumerable source, and the same source `resolve_tenant_guid` reads), and
    # the directory is then asked about each one. A candidate the store does
    # not know -- a stale or hand-made `tenant_<guid>` role with no members --
    # is not a tenant, so a leftover role cannot make the instance look
    # multi-tenant and suppress the assignment below.
    candidates = set()
    for role in sm.get_all_roles():
        # Exactly the membership role -- never `tenant_administrator_<guid>`.
        guid = tenant_of_role_name(getattr(role, "name", "") or "")
        if guid:
            candidates.add(guid)

    directory = get_directory()
    tenants = set()
    for guid in sorted(candidates):
        try:
            # One member is enough to confirm the tenant is real; the
            # backfill only needs to know how MANY tenants exist, not who
            # is in them -- `tenant_administrators` below reads that once,
            # for the single confirmed tenant.
            confirmed = bool(directory.search_users(guid, "", limit=1)["items"])
        except Exception:  # noqa: BLE001 - an unreadable tenant is not a tenant
            logger.warning(
                "superset_ownership backfill: could not read tenant %s from the "
                "authorization store; skipping it",
                guid,
                exc_info=True,
            )
            continue
        if confirmed:
            tenants.add(guid)

    logger.info(
        "superset_ownership backfill: %d candidate tenant role(s), %d confirmed "
        "in the authorization store: %s",
        len(candidates),
        len(tenants),
        sorted(tenants),
    )

    if len(tenants) != 1:
        logger.info(
            "superset_ownership backfill: %d tenants found; not assigning "
            "unowned objects to a tenant administrator (cannot tell which "
            "tenant a legacy object belongs to)",
            len(tenants),
        )
        return []

    tenant = next(iter(tenants))
    # Administrator status is read from the directory, not from a FAB role --
    # it is the source of truth for it everywhere else in this module, and
    # the backfill must not disagree with the check that
    # `is_tenant_administrator` will make at request time. The active-account
    # filter is the directory's (spec section 7.2): this is the same call
    # `_admins_of_tenant` makes.
    ids = sorted(
        u["superset_id"]
        for u in directory.tenant_administrators(tenant)
        if u["superset_id"] is not None
    )

    logger.info(
        "superset_ownership backfill: tenant %s has %d confirmed administrator(s): %s",
        tenant,
        len(ids),
        ids,
    )
    return ids


def _holds_baseline(asset_type: str) -> HoldsFn:
    """`holds(user_id, obj)` for the tenant-administrator fallback: does this
    administrator carry the baseline permission (dataset grant) for the object?

    The same check the transfer route makes (spec section 8), evaluated for
    the candidate rather than for whoever is running the backfill. The
    backfill runs under an app context, which is all `override_user` needs.
    """
    from superset import security_manager as sm

    from superset_ownership.hooks import base_permission_holds_for

    def holds(user_id: int, obj: Any) -> bool:
        user = sm.get_user_by_id(user_id)
        return user is not None and base_permission_holds_for(user, obj, asset_type)

    return holds


# How `_resolve_owner` arrived at its answer. The summary reports unowned
# objects by cause: "no administrator holds the grant" and "could not tell"
# are different facts from "no recoverable creator", and an operator reads
# them differently.
ATTRIBUTED = "attributed"  # creator, last editor, history, or the configured default
ADMINISTRATOR = "administrator"  # the tenant-administrator fallback chose one
NO_CANDIDATES = (
    "no_candidates"  # nothing to attribute to and no administrator to fall back to
)
NO_HOLDER = "no_holder"  # administrators exist, none holds the object's dataset grant
UNVERIFIABLE = (
    "unverifiable"  # nobody chosen, and at least one administrator's check raised
)


@dataclass(frozen=True)
class Resolution:
    """`_resolve_owner_detailed`'s answer: the owner (None when unowned), why,
    and the administrators whose permission check raised."""

    owner: Optional[int]
    reason: str
    errored: tuple[int, ...] = ()


def _resolve_owner(
    obj: Any,
    default: Optional[int],
    asset_type: str,
    tenant_admins: list[int],
    derived_admins: list[int],
    holds: Optional[HoldsFn] = None,
    exists: Optional[ExistsFn] = None,
) -> Optional[int]:
    """Who should own this object, best available attribution first. See
    `_resolve_owner_detailed` for the reasoning; this returns only the owner."""
    return _resolve_owner_detailed(
        obj,
        default,
        asset_type,
        tenant_admins,
        derived_admins,
        holds=holds,
        exists=exists,
    ).owner


def _resolve_owner_detailed(
    obj: Any,
    default: Optional[int],
    asset_type: str,
    tenant_admins: list[int],
    derived_admins: list[int],
    holds: Optional[HoldsFn] = None,
    exists: Optional[ExistsFn] = None,
) -> Resolution:
    """Who should own this object, best available attribution first.

    created_by_fk -> changed_by_fk -> the creating transaction in history ->
    the configured default -> the tenant administrator. Leaving an object
    unowned is the last resort, not a shrug: an unowned object cannot be
    shared by anybody, so every owner this recovers is one object that does
    not need manual reassignment later.

    OWNERSHIP_DEFAULT_OWNER comes BEFORE the tenant administrator because it is
    an explicit operator decision and should beat an inferred one.

    Only ONE administrator is recorded -- `owner_user_id` is a single column.
    That is not a limitation in practice: once the object is owned and stamped
    into a tenant, `_manage_reason` grants EVERY administrator of that
    tenant the same manage rights as the owner, so co-administrators keep full
    control without being listed as owners.

    The administrator fallback is a TRANSFER to someone who is not the
    object's creator, so it is subject to the recipient baseline check
    (spec section 8): among the candidate administrators, the first (by
    Superset id, so the choice stays deterministic) who holds the object's
    dataset grant is chosen; one who could not open the object is skipped.
    When no administrator holds it the object stays unowned and claimable
    rather than being handed to an owner it would be denied to. The check is
    NOT applied to the attribution steps before it (creator, last editor,
    history, configured default): those record who the object already
    belonged to, not a change of hands, and the operator's explicit default
    is theirs to choose. `holds` is injectable for the tests; None means
    "no check", which keeps the earlier behaviour for callers that pass no
    predicate.

    `exists` validates each attribution candidate against ab_user before it
    is chosen: a candidate that names no user (a hard-deleted creator in the
    history tables, a mistyped OWNERSHIP_DEFAULT_OWNER) is logged and the
    next source tried, instead of the owner foreign key refusing the write
    later with a raw IntegrityError that aborts the sweep. None means "no
    check" (the pure tests; callers that trust their sources).
    """
    from superset_ownership.transfer import first_holder

    sources = (
        ("created_by_fk", getattr(obj, "created_by_fk", None)),
        ("changed_by_fk", getattr(obj, "changed_by_fk", None)),
        ("history", None),
        ("OWNERSHIP_DEFAULT_OWNER", default),
    )
    for source, candidate in sources:
        if source == "history":
            candidate = _creator_from_history(asset_type, obj.id)
        if not candidate:
            continue
        if exists is not None and not exists(candidate):
            logger.warning(
                "superset_ownership backfill: %s names user %s for %s %s, but no such "
                "user exists in ab_user; skipping that source",
                source,
                candidate,
                asset_type,
                getattr(obj, "id", None),
            )
            continue
        return Resolution(candidate, ATTRIBUTED)

    # The candidates in order of preference -- the object's own tenant's
    # administrators first, the single-tenant fallback after -- as an ordered
    # set. On a single-tenant instance the two lists are the same group read
    # from the same store, so without the deduplication every administrator
    # would be evaluated twice per unowned object, and a raising check would
    # be logged twice and counted twice in `errored`.
    candidates = list(dict.fromkeys([*derived_admins, *tenant_admins]))
    if not candidates:
        return Resolution(None, NO_CANDIDATES)
    if holds is None:
        return Resolution(candidates[0], ADMINISTRATOR)

    picked = first_holder(candidates, obj, holds)
    if picked.chosen is not None:
        return Resolution(picked.chosen, ADMINISTRATOR, picked.errored)
    if picked.unverifiable:
        # Each raising candidate is already logged with its traceback by
        # first_holder; this line is the per-object verdict. Not "none holds
        # it": that was never established.
        logger.warning(
            "superset_ownership backfill: could not evaluate the dataset grant "
            "for %d of %d administrator(s) (%s) for %s %s, and none of the rest "
            "holds it; not assigning it to one",
            len(picked.errored),
            len(candidates),
            list(picked.errored),
            asset_type,
            getattr(obj, "id", None),
        )
        return Resolution(None, UNVERIFIABLE, picked.errored)
    logger.info(
        "superset_ownership backfill: none of %d administrator(s) %s holds "
        "the dataset grant for %s %s; not assigning it to one",
        len(candidates),
        candidates,
        asset_type,
        getattr(obj, "id", None),
    )
    return Resolution(None, NO_HOLDER)


def _sync_backfilled_editors(
    asset_type: str, obj: Any, owner: int, row: Any = None
) -> None:
    """`service.sync_native_editors`, keyed off backfill's own resolved owner.

    F-3: a Public/Draft object is only visible to its native editors under
    Superset's own rules; backfill attributes an owner in THIS module's
    table, and until now never touched `obj.editors` to match, so an owner
    it had just attributed could still 404 on their own object.

    `row` is the ownerless row being re-attributed; omitted when this is a
    brand new row (nothing to replace -- built fresh instead). No previous
    owner to name either way (the object was, by construction, ownerless),
    so nothing is removed; this only ensures `owner` is present. Best-effort
    and idempotent: a re-run with the same attribution finds the owner
    already a native editor and no-ops; a failure here is logged, not
    raised -- a sweep across every object in the instance must not abort
    over one object's editors.
    """
    from superset_ownership import service

    try:
        uuid = str(obj.uuid) if getattr(obj, "uuid", None) else None
        new_row = (
            replace(row, owner_user_id=owner)
            if row is not None
            else service.OwnershipRow(
                id=0,
                asset_type=asset_type,
                object_id=obj.id,
                object_uuid=uuid,
                owner_user_id=owner,
                visibility="public",
            )
        )
        service.sync_native_editors(obj, new_row, previous_owner_id=None)
    except Exception:
        logger.exception(
            "superset_ownership backfill: could not sync native editors for %s %s",
            asset_type,
            getattr(obj, "id", None),
        )


def _stamp_tenant(asset_type: str, obj, uuid, tenant, row, st: dict) -> str | None:
    """Stamp the object's tenant on the store (once) and mirror it onto the
    row (revision 0004, what "public within the tenant" is decided from).
    Returns the tenant the object is now recorded under in the store --
    what the upserts that follow may write to the row -- or None when the
    store has none and could not be given one.

    The STORE is the source of the mirror. `tenant` is only a derivation
    (creator's current role, last editor, history, dataset binding) and it
    moves whenever any of those move; the store's tuple is what every
    manage and transfer decision reads. So: a tenant the store already
    holds is mirrored as it is and never replaced by the derivation; only
    an object the store has no tenant for is stamped, in the store first
    and on the row only once that write succeeded. Re-running is then safe
    -- a re-provisioned creator cannot flip which tenant reads a public
    object -- and the row is never ahead of the store. Runs on every sweep,
    so a database migrated from before 0004 fills its mirror in on the next
    backfill.

    The store read is strict: this read decides whether to STAMP, and a
    store that cannot be read must answer "could not read", never "no
    tenant" -- otherwise an outage would select the stamp branch and write
    the derivation over a mirror the store disagrees with. On a failed read
    nothing is written anywhere (`tenant_unreadable` in the summary); the
    next run, with the store back, fills it.
    """
    from superset_ownership import service
    from superset_ownership.authz import get_authorizer

    if not uuid:
        return None
    tenant = service.normalize_tenant(tenant)
    try:
        stored = service.normalize_tenant(
            get_authorizer().object_tenant(asset_type, uuid, strict=True)
        )
    except Exception:  # noqa: BLE001 - reported; this object is left alone
        st["tenant_unreadable"] += 1
        logger.debug(
            "could not read the tenant of %s %s", asset_type, obj.id, exc_info=True
        )
        return None
    if stored is None and tenant:
        try:
            if get_authorizer().set_object_tenant(asset_type, uuid, tenant):
                st["tenant_stamped"] += 1
                stored = tenant
        except Exception:  # noqa: BLE001 - stamping is best-effort
            logger.debug(
                "could not stamp tenant on %s %s", asset_type, obj.id, exc_info=True
            )
    if stored and row is not None and row.tenant_guid != stored:
        service.set_row_tenant(asset_type, uuid, stored, object_id=obj.id)
    return stored


def _arm_public(obj, st: dict) -> None:
    """Under OWNERSHIP_PUBLIC_SCOPE=tenant a public object carries the
    sentinel so Superset's own rule does not reopen it to every tenant that
    holds the dataset grant; the read gate grants the object's tenant
    instead. Idempotent (add_sentinel checks first); nothing under instance
    scope, where public stays ungoverned."""
    from superset_ownership import sentinel, service

    if not service.public_is_tenant_scoped():
        return
    subject = sentinel.get_sentinel_subject()
    if subject is None or subject in (obj.viewers or []):
        return
    sentinel.add_sentinel(obj)
    st["public_armed"] += 1


def run() -> None:
    from superset import db
    from superset.models.dashboard import Dashboard
    from superset.models.slice import Slice

    from superset_ownership.db import create_tables
    from superset_ownership import service
    from superset_ownership.lifecycle import all_rows

    create_tables(db.engine)
    exists = _user_exists_checker()
    default_owner = _default_owner()
    if default_owner is not None and not exists(default_owner):
        # Validated once, up front, so the sweep never asks the database to
        # write an owner it would refuse (owner_user_id -> ab_user.id).
        logger.warning(
            "superset_ownership backfill: OWNERSHIP_DEFAULT_OWNER=%r names no user in "
            "ab_user; ignoring it for this run (objects with no other attribution "
            "fall through to the tenant administrator, or stay unowned)",
            default_owner,
        )
        default_owner = None
    tenant_admins = _tenant_admins()
    tenant_ds = _tenant_datasources()
    admins_cache: dict[str, list[int]] = {}
    logger.info(
        "superset_ownership backfill: %d datasource(s) bound to a tenant",
        len(tenant_ds),
    )

    # all_rows, not a plain query: a soft-deleted object still exists and can
    # be restored, and disable/enable/check all sweep it -- backfill must too,
    # or a restored object comes back ungoverned.
    # `unowned` is the total; `no_holder` and `unverifiable` split out the two
    # causes that are NOT "no recoverable creator", so the summary does not
    # attribute them to it.
    def _counters() -> dict[str, int]:
        return {
            "created": 0,
            "reattributed": 0,
            "unowned": 0,
            "tenant_stamped": 0,
            "tenant_unreadable": 0,
            "public_armed": 0,
            "no_holder": 0,
            "unverifiable": 0,
        }

    stats = {"dashboard": _counters(), "chart": _counters()}

    def sweep(asset_type, model_cls):
        st = stats[asset_type]
        holds = _holds_baseline(asset_type)
        # In id order: each upsert locks the object's row for the rest of
        # this one transaction, so the sweep takes N rows, and it takes them
        # in (asset_type, object_id) order -- the order the hard-delete hook
        # and purge_tenant use -- so two bulk paths cannot cycle.
        for obj in sorted(all_rows(model_cls), key=lambda o: o.id):
            # Fresh: a repair decision must be made on the row as stored.
            row = service.lookup(asset_type, obj.id, fresh=True)
            uuid = str(obj.uuid) if getattr(obj, "uuid", None) else None

            # The tenant the object is stamped with, where the store has none
            # yet, is ITS OWNER's tenant -- the invariant every transfer,
            # claim and adoption already enforces (an owner is always in the
            # object's tenant). The creator / last editor / history /
            # dataset derivation (`_derive_tenant`) is the fallback for an
            # owner outside every tenant, and what picks the administrators
            # the attribution below may fall back to. Deriving from the
            # creator FIRST stamped the wrong tenant whenever the creator
            # was not the owner the row already named (attribution that
            # moved before anything was stamped -- a reassigned dashboard,
            # an object handed over by a service account): the object
            # landed in the creator's tenant with an owner from another,
            # invisible to the owner's own tenant. Stamped even on
            # an object that is ALREADY owned: Ivanti runs one cross-tenant
            # instance, so "no tenant" is not a real state for an object --
            # only a gap in what we recorded, and every gap costs a tenant
            # administrator the right to manage their own tenant's object.
            derived = _derive_tenant(asset_type, obj, tenant_ds)
            if row is not None and row.visibility == "public":
                # Under tenant scope a public object is governed and must
                # carry the sentinel; a row from before that (or a database
                # switched from instance scope) has none. Idempotent.
                _arm_public(obj, st)

            if row is not None and row.owner_user_id is not None:
                # Already attributed -- never overwrite a real owner. A
                # DEACTIVATED owner is still a real owner (the row names an
                # existing user; `is_unowned` handles it at read time).
                tenant = _tenant_of_user(row.owner_user_id) or derived
                _stamp_tenant(asset_type, obj, uuid, tenant, row, st)
                continue
            tenant = derived

            # Administrators of THIS object's tenant. Because the tenant is
            # derived per object, this works on a multi-tenant instance --
            # unlike `tenant_admins`, which can only be used when the whole
            # instance has a single tenant.
            derived_admins: list[int] = []
            if tenant:
                if tenant not in admins_cache:
                    admins_cache[tenant] = _admins_of_tenant(tenant)
                derived_admins = admins_cache[tenant]

            resolution = _resolve_owner_detailed(
                obj,
                default_owner,
                asset_type,
                tenant_admins,
                derived_admins,
                holds=holds,
                exists=exists,
            )
            owner = resolution.owner
            if owner is None:
                if resolution.reason == NO_HOLDER:
                    st["no_holder"] += 1
                elif resolution.reason == UNVERIFIABLE:
                    st["unverifiable"] += 1
            # The owner just resolved names the tenant; the derivation only
            # where that owner (or no owner at all) is outside every tenant.
            # What the store now holds after the stamp (None: nothing, or
            # unreadable) is what the rows written below carry -- never the
            # bare derivation -- so the mirror is never ahead of the store.
            if owner is not None:
                tenant = _tenant_of_user(owner) or derived
            stored_tenant = _stamp_tenant(asset_type, obj, uuid, tenant, row, st)

            if row is None:
                service.upsert_ownership(
                    asset_type=asset_type,
                    object_id=obj.id,
                    object_uuid=uuid,
                    owner_user_id=owner,
                    visibility="public",
                    tenant_guid=stored_tenant,
                )
                _arm_public(obj, st)
                st["created"] += 1
                if owner is None:
                    st["unowned"] += 1
                else:
                    # F-3: a Public/Draft object is only visible to its
                    # native editors under Superset's own rules -- backfill
                    # attributes an owner in THIS table, but never touched
                    # `obj.editors`, so the owner it just attributed could
                    # still get a 404 on their own object. Idempotent: a
                    # re-run with the same attribution finds them already
                    # there and no-ops.
                    _sync_backfilled_editors(asset_type, obj, owner)
                continue

            # The row exists but has no owner. Re-running the backfill used to
            # SKIP it outright, so an object that landed unowned on the first
            # pass stayed unowned for good -- re-running reported "0 objects"
            # and changed nothing, even once attribution became available or a
            # default owner was configured. Repair it in place instead,
            # KEEPING its current visibility: someone may have already made it
            # private or shared, and resetting that to public would silently
            # widen access during what is meant to be a repair.
            #
            # Since revision 0003 this is also how a HARD-DELETED owner's
            # objects are re-homed: the owner foreign key (ON DELETE SET NULL)
            # nulls the row, which reads here exactly like never-owned, so the
            # next sweep attributes it -- creator, last editor, history, the
            # configured default, a tenant administrator -- and the object
            # stops being orphaned. (Before 0003 the dangling id counted as a
            # real owner and the row was skipped.) The re-homing is
            # `reattributed` in the summary and, like every upsert, is not
            # itself audited; the delete's `owner_removed_by_user_delete`
            # events (guard.py) are the record of who owned it.
            if owner is None:
                st["unowned"] += 1
                continue
            service.upsert_ownership(
                asset_type=asset_type,
                object_id=obj.id,
                object_uuid=row.object_uuid or uuid,
                owner_user_id=owner,
                visibility=row.visibility,
                tenant_guid=stored_tenant,
            )
            st["reattributed"] += 1
            # F-3, same as the newly-created branch above.
            _sync_backfilled_editors(asset_type, obj, owner, row)

    # Asset types in order too ("chart" before "dashboard"); see sweep.
    sweep("chart", Slice)
    sweep("dashboard", Dashboard)
    n_dash = stats["dashboard"]["created"]
    n_chart = stats["chart"]["created"]

    db.session.commit()
    # Each upsert already invalidated its own row; the sweep is a bulk write
    # across every object, so the whole cache is turned over once it is
    # committed rather than trusting the per-row bookkeeping alone.
    service.invalidate_all()

    # Reconcile owner tuples for everything now owned (backfill wrote rows but
    # not tuples). Cheap, idempotent, and keeps the store consistent so a
    # tenant administrator / purge can see these objects.
    from superset_ownership.lifecycle import reconcile

    try:
        reconcile(dry_run=False)
    except Exception:
        pass

    for asset_type in ("dashboard", "chart"):
        st = stats[asset_type]
        print(
            f"superset_ownership backfill [{asset_type}]: "
            f"{st['created']} created, {st['reattributed']} re-attributed, "
            f"{st['tenant_stamped']} tenant stamped, {st['public_armed']} public armed, "
            f"{st['unowned']} still UNOWNED"
        )
    total_unowned = sum(stats[a]["unowned"] for a in stats)
    no_holder = sum(stats[a]["no_holder"] for a in stats)
    unverifiable = sum(stats[a]["unverifiable"] for a in stats)
    no_creator = total_unowned - no_holder - unverifiable
    if no_creator:
        print(
            f"superset_ownership backfill: {no_creator} object(s) have no "
            "recoverable creator in this database (no created_by_fk, no "
            "changed_by_fk, no creating transaction in history) and no tenant "
            "administrator to fall back to. They stay public and readable, but "
            "cannot be SHARED until someone owns them. Set "
            "OWNERSHIP_DEFAULT_OWNER and re-run to assign a fallback owner, or "
            "let a tenant administrator claim them."
        )
    if no_holder:
        print(
            f"superset_ownership backfill: {no_holder} object(s) have no "
            "recoverable creator, and none of their tenant's administrators "
            "holds the object's dataset grant, so none was made the owner "
            "(an owner without the grant could not open the object). They stay "
            "unowned and claimable by an administrator who is granted access."
        )
    if unverifiable:
        print(
            f"superset_ownership backfill: {unverifiable} object(s) were left "
            "unowned because an administrator's dataset grant could NOT be "
            "evaluated (see the WARNING lines above for the cause); whether "
            "one holds it is unknown. Fix the cause and re-run: an unowned "
            "row is repaired in place."
        )
    print(f"superset_ownership backfill: default_owner={default_owner}")

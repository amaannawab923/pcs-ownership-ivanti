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
"""Pluggable authorization backend.

The point of this module is the boundary it draws. Object ownership and
sharing is a generic capability; OpenFGA is one way to answer the four
questions it asks. Keeping those four questions behind an interface means
the ownership feature can ship without OpenFGA going anywhere near it, and
a deployment that wants OpenFGA selects it with one config value.

    OWNERSHIP_AUTHORIZER = "local"    # relationships in Superset's own DB
    OWNERSHIP_AUTHORIZER = "openfga"  # relationships in an OpenFGA store

Both backends answer the same QUESTIONS, so behaviour is a property of the
feature, not of the backend.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class Authorizer(Protocol):
    """The relations half of the contract.

    What an access decision is made from: `check`, the write/read primitives
    behind a share, and the object-tenant bookkeeping that is OUR write (the
    outbox stamps it, `hooks.py:503` reads it), not a fact a `Directory`
    could answer instead -- putting it on a plug-in would put a plug-in on
    the access path (spec section 7.5, D-9). Everything about PEOPLE --
    tenant membership, group listing, group existence, tenant administrators
    -- moved to `superset_ownership.directory.Directory`
    (`qa/design/directory-hook/02-technical-spec.md` section 7); this
    Protocol used to answer both halves, which is why a third-party backend
    written to the old docstring's "four questions" would have failed at
    runtime against the twelve methods both implementations actually
    answered.

    `user_in_group` is the one exception: it stays here as the ONE live
    membership check `is_tenant_administrator` makes (`api.py:397`,
    unchanged), per D-1. `Directory.user_in_group` also exists (the
    picker/verify agreement check); `LocalAuthorizer.user_in_group` is the
    single implementation both call.
    """

    name: str

    # --- relations -----------------------------------------------------------

    def check(self, user: str, relation: str, obj: str) -> bool:
        """Does `user` hold `relation` on `obj`?"""

    def list_objects(
        self, user: str, relation: str, object_type: str, *, strict: bool = False
    ) -> list[str]:
        """Which objects of `object_type` does `user` hold `relation` on?

        Non-strict by default, like every access read: a store that cannot
        be read answers "nothing", which the list filter's own fresh
        revocation subtraction treats no differently from a real empty
        answer. `service.cached_list_objects` (M-1, issue #82's cache)
        passes ``strict=True`` so it can tell "the store said nothing" from
        "the store could not be asked" -- ``strict`` raises
        `superset_ownership.fga.StoreUnavailable` / `StoreRejected` on
        failure instead of answering `[]`, the same additive contract the
        write methods already carry. A backend that never fails a read
        (`LocalAuthorizer`) may ignore the flag.
        """

    def write_tuple(
        self, user: str, relation: str, obj: str, *, strict: bool = False
    ) -> bool:
        """Grant `relation`. Idempotent."""

    def delete_tuple(
        self, user: str, relation: str, obj: str, *, strict: bool = False
    ) -> bool:
        """Revoke `relation`. Idempotent."""

    def list_relations(self, user: str, obj: str) -> list[str]:
        """Every share relation `user` holds on `obj` (owner/tenant excluded)."""

    def list_grants(self, obj: str) -> list[dict]:
        """Every share grant on `obj`, as {"user", "relation"}."""

    def user_in_group(self, user_guid: str, group: str) -> bool:
        """Is this member in this group reference? The one directory-shaped
        question that stays here (D-1): `is_tenant_administrator`'s live
        check must not depend on which `Directory` is configured."""

    def object_tenant(
        self, asset_type: str, object_uuid: str, *, strict: bool = False
    ) -> str | None:
        """Which tenant an object belongs to. With ``strict`` an unreadable
        store raises (StoreUnavailable / StoreRejected) instead of answering
        "no tenant": a write that treats "untenanted" and "could not read"
        as different answers must not be handed the second as the first."""

    def tenant_objects(self, tenant_guid: str, asset_type: str) -> list[str]:
        """Every object uuid of `asset_type` in a tenant."""

    def purge_object(self, obj: str) -> bool:
        """Remove EVERY relationship on an object (its delete event).

        Must raise -- StoreUnavailable or StoreRejected -- when the store
        could not be consulted or refused; "I could not read it" is never
        "there was nothing to remove".
        """

    def revoke_subject(self, user: str, obj: str) -> bool:
        """Remove every SHARE relation `user` holds on `obj` -- never `owner`
        or `tenant`, which are not shares and are managed by transfer and
        creation. Same fail rule as purge_object."""

    def reachable(self) -> bool:
        """Is the store serving right now? For operator-facing messages
        ("cannot verify" versus "no"). Not a classifier for a failed write:
        the write's own outcome carries that (see ``strict``)."""

    def set_object_tenant(
        self,
        asset_type: str,
        object_uuid: str,
        tenant_guid: str,
        *,
        strict: bool = False,
    ) -> bool:
        """Record an object's tenant. Idempotent."""

    # ``strict`` on the write methods: the default keeps the inline callers'
    # contract (False on failure, they answer 502). The outbox drain passes
    # strict=True and gets the failure as a typed exception -- StoreUnavailable
    # (retry) or StoreRejected (park) -- decided from the store's own response
    # rather than guessed afterwards. A backend that never fails a write may
    # ignore it.


class LocalAuthorizer:
    """Relationships stored in Superset's own metadata database.

    The default. Requires no external service, which is what lets the
    ownership feature ship to a deployment that has never heard of OpenFGA.
    Object references arrive as "<asset_type>:<uuid>"; the share table is
    keyed on the integer id, so uuids are resolved back through it.
    """

    name = "local"

    def _resolve(self, obj: str) -> tuple[str | None, int | None]:
        asset_type, _, ident = obj.partition(":")
        if not ident:
            return None, None
        from superset_ownership import service

        row = service.lookup_by_uuid(asset_type, ident)
        return (asset_type, row.object_id) if row else (asset_type, None)

    def check(self, user: str, relation: str, obj: str) -> bool:
        if obj.startswith("tenant:"):
            # The tenant object's relations, answered from FAB roles the way
            # `user_in_group` answers a group's: `admin` is membership of
            # the `tenant_administrator_<guid>` role, `member` of the
            # tenant's own role.
            return self._tenant_relation(user, relation, obj[len("tenant:") :])
        asset_type, object_id = self._resolve(obj)
        if object_id is None:
            return False
        from superset_ownership import service

        return service.has_share_row(asset_type, object_id, user, relation)

    def _tenant_relation(self, user: str, relation: str, tenant: str) -> bool:
        from superset_ownership.identity import (
            member_guid_of_subject_id,
            tenant_of_role_name,
            TENANT_ADMINISTRATOR_GROUP,
            user_for_member_guid,
        )

        account = user_for_member_guid(member_guid_of_subject_id(user) or user)
        if account is None:
            return False
        names = [getattr(r, "name", "") or "" for r in (account.roles or [])]
        if relation == "member":
            return any(tenant_of_role_name(n) == tenant for n in names)
        if relation == "admin":
            return any(
                n.lower() == f"{TENANT_ADMINISTRATOR_GROUP}_{tenant}".lower()
                for n in names
            )
        return False

    def list_objects(
        self, user: str, relation: str, object_type: str, *, strict: bool = False
    ) -> list[str]:
        # This backend's "store" is Superset's own database: a read from it
        # either succeeds or raises a real exception (never a silent "down"
        # the way an HTTP call can be), so there is nothing for `strict` to
        # change here -- accepted for Protocol conformance only.
        from superset_ownership import service

        return [
            f"{object_type}:{uuid}"
            for uuid in service.uuids_shared_with(object_type, user, relation)
        ]

    def list_grants(self, obj: str) -> list[dict]:
        """Every share relation on an object. On this backend the mirror IS
        the store, so this is the share table."""
        asset_type, object_id = self._resolve(obj)
        if object_id is None:
            return []
        from superset_ownership import service

        return [
            {"user": r["subject"], "relation": r["role"]}
            for r in service.list_share_rows(asset_type, object_id)
        ]

    # --- directory ---------------------------------------------------------
    # `user_in_group` is the one directory-shaped question kept here (D-1):
    # `superset_ownership.directory.LocalDirectory.user_in_group` calls this
    # same method rather than duplicating it, so the two backends -- and the
    # picker's agreement check -- can never disagree about it. Everything
    # else people-shaped (tenant membership, group listing, group existence)
    # moved to `LocalDirectory` (`directory.py`); without it the request path
    # called OpenFGA directly, so on the documented standalone setting
    # (`OWNERSHIP_AUTHORIZER=local`) every tenant-administrator right
    # silently evaluated to False and the tenant branch of the read scope
    # was a no-op. A backend that is offered has to work -- `LocalDirectory`
    # closes that.

    def user_in_group(self, user_guid: str, group: str) -> bool:
        """Groups are FAB roles on this backend; membership is role
        membership. A tenant userset (`tenant:<t>#admin`, the default
        administrator reference) is answered from the tenant's roles."""
        from superset import security_manager as sm

        if group.startswith("tenant:"):
            obj, _, relation = group.partition("#")
            return self._tenant_relation(
                f"user:{user_guid}", relation or "member", obj[len("tenant:") :]
            )
        name = group.split(":", 1)[-1].split("#", 1)[0]
        from superset_ownership.identity import member_guid_of_subject_id

        user = sm.find_user(username=member_guid_of_subject_id(user_guid) or user_guid)
        if user is None:
            return False
        return any(getattr(r, "name", None) == name for r in (user.roles or []))

    def object_tenant(
        self, asset_type: str, object_uuid: str, *, strict: bool = False
    ) -> str | None:
        """The owner's tenant. There is no separate store to record one in,
        so there is nothing for ``strict`` to fail on; accepted for the
        contract."""
        from superset_ownership import service
        from superset_ownership.identity import resolve_tenant_guid

        row = service.lookup_by_uuid(asset_type, object_uuid)
        if row is None or row.owner_user_id is None:
            return None
        from superset import security_manager as sm

        return resolve_tenant_guid(sm.get_user_by_id(row.owner_user_id))

    def tenant_objects(self, tenant_guid: str, asset_type: str) -> list[str]:
        """Every object whose owner is in the tenant, in one owners query.

        The same answer as `object_tenant` per row, resolved for the page:
        the distinct owner ids on the asset type's rows are loaded with their
        roles in one SELECT, and each row's tenant is read from its owner in
        memory. Per row this was a row lookup plus a user lookup plus the
        lazy roles load -- O(rows) queries on every list page of a tenant
        administrator or a manage-sharing holder.
        """
        from sqlalchemy.orm import joinedload

        from superset import db
        from superset import security_manager as sm

        from superset_ownership import service
        from superset_ownership.identity import resolve_tenant_guid

        rows = [
            row
            for row in service.list_rows(asset_type)
            if row.object_uuid and row.owner_user_id is not None
        ]
        owner_ids = {row.owner_user_id for row in rows}
        if not owner_ids:
            return []
        model = sm.user_model
        owners = (
            db.session.query(model)
            .options(joinedload(model.roles))
            .filter(model.id.in_(owner_ids))
            .all()
        )
        in_tenant = {u.id for u in owners if resolve_tenant_guid(u) == tenant_guid}
        return [row.object_uuid for row in rows if row.owner_user_id in in_tenant]

    def set_object_tenant(
        self,
        asset_type: str,
        object_uuid: str,
        tenant_guid: str,
        *,
        strict: bool = False,
    ) -> bool:
        """Nothing to write: the owner's tenant IS the object's tenant here."""
        return True

    def list_relations(self, user: str, obj: str) -> list[str]:
        asset_type, object_id = self._resolve(obj)
        if object_id is None:
            return []
        from superset_ownership import service

        return sorted(
            {
                sh["role"]
                for sh in service.list_shares(asset_type, object_id)
                if sh["subject"] == user
            }
        )

    # The writes below run on the request session (`service._db().session`)
    # rather than one handed in, because on this backend the "store" IS the
    # mirror and the caller already holds row locks on it; a second
    # connection would deadlock. The Protocol has no session parameter for
    # the same reason.

    def write_tuple(
        self, user: str, relation: str, obj: str, *, strict: bool = False
    ) -> bool:
        asset_type, object_id = self._resolve(obj)
        if object_id is None:
            # The object resolved when this was asked for (the routes resolve
            # it) and does not now: it was deleted in between. There is
            # nothing to grant on, and nothing a retry could change; a queued
            # write for a deleted object must not park dead and block that
            # object's purge behind it.
            logger.info(
                "local authorizer: %s no longer resolves; nothing to write", obj
            )
            return True
        from superset_ownership import service

        service.add_share_row(asset_type, object_id, user, relation)
        return True

    def delete_tuple(
        self, user: str, relation: str, obj: str, *, strict: bool = False
    ) -> bool:
        asset_type, object_id = self._resolve(obj)
        if object_id is None:
            return True  # nothing to revoke on an object that is gone
        from superset_ownership import service

        service.remove_share_row(asset_type, object_id, user, role=relation)
        return True

    # Relationships here are the share ROWS, keyed by (asset_type,
    # object_id). They do not move when an object's uuid changes, so a
    # re-point has nothing to purge or re-write on this backend -- and must
    # not try, because the stale uuid it would purge resolves to whatever
    # row still holds it, which is somebody else's object (review round 3).
    # OpenFGA files tuples under the object REFERENCE, so the same re-point
    # genuinely has to move them there; `service.repoint_object_uuid` reads
    # this to tell the two apart.
    addresses_objects_by_uuid = False

    def purge_object(self, obj: str) -> bool:
        """Local relationships ARE the share rows, which after_asset_delete has
        already removed in the same transaction; anything left is swept here.
        An object that no longer resolves has nothing to purge."""
        asset_type, object_id = self._resolve(obj)
        if object_id is None:
            return True
        from sqlalchemy import delete

        from superset_ownership import service
        from superset_ownership.db import ownership_share

        service._db().session.execute(
            delete(ownership_share).where(
                ownership_share.c.asset_type == asset_type,
                ownership_share.c.object_id == object_id,
            )
        )
        # A share write, like every other: the editors resolver's derived
        # entry for the object is built from the share rows.
        service.invalidate(asset_type, object_id)
        return True

    def revoke_subject(self, user: str, obj: str) -> bool:
        """Share rows only; `ownership_object` (the owner) is never touched."""
        asset_type, object_id = self._resolve(obj)
        if object_id is None:
            return True
        from superset_ownership import service

        service.remove_share_row(asset_type, object_id, user)
        return True

    def reachable(self) -> bool:
        return True  # the store is this database


class OpenFGAAuthorizer:
    """Relationships stored in an OpenFGA store.

    A thin adapter over the HTTP client. Nothing above this class knows
    OpenFGA exists.
    """

    name = "openfga"
    protocol_version = 1

    # --- reference formatting (spec section 8) --------------------------
    #
    # Every method below builds a subject or object string through these
    # four hooks and nothing else formats one. The expected override is a
    # SUBCLASS that changes only these -- their own authorization model
    # differs from ours in id shapes (a `parent_tenant`-style prefix,
    # object-id prefixes; issue #73) -- and nothing else in this class.
    # `fga.py` keeps taking fully formed references; it has no idea a
    # subclass exists.
    #
    # Example (shipped in `qa/ivanti_pcs_example/authz.py`):
    #
    #     class PrefixedIdAuthorizer(OpenFGAAuthorizer):
    #         '''Their model keys objects as dashboard:pcs-<uuid> and users
    #         as user:neurons|<guid>.'''
    #         def _object_ref(self, obj):
    #             t, _, i = obj.partition(":")
    #             return f"{t}:pcs-{i}"
    #         def _from_object_ref(self, ref):
    #             t, _, i = ref.partition(":")
    #             return f"{t}:{i.removeprefix('pcs-')}"
    #         def _subject_ref(self, s):
    #             return s.replace("user:", "user:neurons|", 1) if s.startswith("user:") else s
    #         def _from_subject_ref(self, s):
    #             return s.replace("user:neurons|", "user:", 1)

    def _object_ref(self, obj: str) -> str:
        """`"<asset_type>:<uuid>"` as this module speaks it. Identity by
        default; a subclass overrides this (and `_from_object_ref`) to
        change how an object is spelled in the subclass's store."""
        return obj

    def _from_object_ref(self, ref: str) -> str:
        """Inverse of `_object_ref`: a reference read back from the store,
        in OUR spelling. Identity by default."""
        return ref

    def _subject_ref(self, subject: str) -> str:
        """`"user:<guid>"` | `"group:<id>#member"` | `"tenant:<guid>#member"`
        as this module speaks it. Identity by default; a subclass overrides
        this (and `_from_subject_ref`) to change how a subject is spelled."""
        return subject

    def _from_subject_ref(self, ref: str) -> str:
        """Inverse of `_subject_ref`. Identity by default."""
        return ref

    def check(self, user: str, relation: str, obj: str) -> bool:
        from superset_ownership import fga

        return fga.check(self._subject_ref(user), relation, self._object_ref(obj))

    def list_objects(
        self, user: str, relation: str, object_type: str, *, strict: bool = False
    ) -> list[str]:
        from superset_ownership import fga

        return [
            self._from_object_ref(o)
            for o in fga.list_objects(
                self._subject_ref(user), relation, object_type, strict=strict
            )
        ]

    def write_tuple(
        self, user: str, relation: str, obj: str, *, strict: bool = False
    ) -> bool:
        from superset_ownership import fga

        return fga.write_tuple(
            self._subject_ref(user), relation, self._object_ref(obj), strict=strict
        )

    def delete_tuple(
        self, user: str, relation: str, obj: str, *, strict: bool = False
    ) -> bool:
        from superset_ownership import fga

        return fga.delete_tuple(
            self._subject_ref(user), relation, self._object_ref(obj), strict=strict
        )

    def purge_object(self, obj: str) -> bool:
        """Strict end to end: a store that cannot be read, or refuses a delete,
        raises rather than reading as empty or counting as done -- so a purge
        is never recorded delivered with tuples still there. An HTTP 503 from
        a balancer, or a 400 from a stale model pin, is a failure here, not
        an empty object."""
        from superset_ownership import fga

        tuples = fga.read_all(self._object_ref(obj), strict=True)
        if not tuples:
            return True
        fga.delete_each(tuples, strict=True)
        return True

    def revoke_subject(self, user: str, obj: str) -> bool:
        """Every share relation the subject holds, resolved from the store so a
        relation the mirror never learned about goes too. `owner` and `tenant`
        are excluded: a stale share row on the current owner (left by a
        transfer) must revoke the share, not the ownership."""
        from superset_ownership import fga

        ref, subject = self._object_ref(obj), self._subject_ref(user)
        mine = [
            t
            for t in fga.read_all(ref, strict=True)
            if t.get("user") == subject and t.get("relation") not in ("owner", "tenant")
        ]
        if not mine:
            return True
        fga.delete_each(mine, strict=True)
        return True

    def reachable(self) -> bool:
        from superset_ownership import fga

        return fga.reachable()

    def list_relations(self, user: str, obj: str) -> list[str]:
        from superset_ownership import fga

        return fga.relations_of(self._subject_ref(user), self._object_ref(obj))

    def list_grants(self, obj: str) -> list[dict]:
        from superset_ownership import fga

        return [
            {"user": self._from_subject_ref(t["user"]), "relation": t["relation"]}
            for t in fga.read_all(self._object_ref(obj))
            if t.get("relation") not in ("owner", "tenant")
        ]

    # --- directory ---------------------------------------------------------
    # `user_in_group` is the one directory-shaped question kept here (D-1);
    # `superset_ownership.directory.OpenFGADirectory.user_in_group` makes the
    # identical `check` so the two can never disagree. Tenant membership,
    # group listing and group existence moved to `OpenFGADirectory`.

    def user_in_group(self, user_guid: str, group: str) -> bool:
        """Membership of a userset: `group:<id>` (its members), or any
        `<object>#<relation>` -- `tenant:<t>#admin` is how a tenant's
        administrators are asked about (Neurons' shape)."""
        from superset_ownership import fga
        from superset_ownership.identity import split_userset

        obj, relation = split_userset(group)
        return fga.check(self._subject_ref(f"user:{user_guid}"), relation, obj)

    def object_tenant(
        self, asset_type: str, object_uuid: str, *, strict: bool = False
    ) -> str | None:
        from superset_ownership import fga

        mapped_type, _, mapped_uuid = self._object_ref(
            f"{asset_type}:{object_uuid}"
        ).partition(":")
        return fga.object_tenant(mapped_type, mapped_uuid, strict=strict)

    def tenant_objects(self, tenant_guid: str, asset_type: str) -> list[str]:
        """Every object uuid of `asset_type` in a tenant, in OUR spelling.

        Unlike the other methods here this has no single object string to
        run through `_object_ref` -- it asks the store for every object of a
        TYPE, not one object -- so a subclass whose `_object_ref` changes
        the uuid (not just the type prefix) is not represented by this
        method alone. Documented, not silently wrong: the shipped example
        override only reshapes ids losslessly (`pcs-<uuid>`), for which the
        raw uuid this returns is already correct without translation.
        """
        from superset_ownership import fga

        return fga.tenant_objects(tenant_guid, asset_type)

    def set_object_tenant(
        self,
        asset_type: str,
        object_uuid: str,
        tenant_guid: str,
        *,
        strict: bool = False,
    ) -> bool:
        from superset_ownership import fga

        mapped_type, _, mapped_uuid = self._object_ref(
            f"{asset_type}:{object_uuid}"
        ).partition(":")
        return fga.set_object_tenant(
            mapped_type, mapped_uuid, tenant_guid, strict=strict
        )


# The one alias table for OWNERSHIP_AUTHORIZER, resolved by
# superset_ownership.plugins.load()/get_authorizer() (plugins.py owns
# selection, construction and the "switch on a running instance" behaviour
# this module used to implement itself). tests/conftest.py registers a
# third alias, "counting", here.
_BACKENDS = {"local": LocalAuthorizer, "openfga": OpenFGAAuthorizer}


def get_authorizer() -> Authorizer:
    """Backward-compatible re-export. Every existing call site
    (``from superset_ownership.authz import get_authorizer``, or
    ``monkeypatch.setattr(api, "get_authorizer", ...)`` against api.py's own
    import of this name) keeps working unchanged; the implementation now
    lives in ``plugins.get_authorizer``. Imported lazily to avoid a module
    import cycle (plugins.py reads ``_BACKENDS`` off this module).
    """
    from superset_ownership.plugins import get_authorizer as _get_authorizer

    return _get_authorizer()

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
"""The reference implementation for the sixteen function hooks (contract
`qa/design/directory-hook/01-plugin-contract.md` §4.5.2): every hook this
package's contract defines, each one answering the SAME question the
built-in default answers, but reading it from a DIFFERENT source -- so a run
with all sixteen configured (`config_hooks_example.py`) proves the hooks are
a real seam, not a documentation exercise that happens to reproduce the
default's own logic under a new name.

Configure with the dotted paths in `config_hooks_example.py` (copy-pasteable
into `superset_config_docker.py`), or a subset -- every hook not named keeps
the built-in default (§4.5.1 item 3).

One-time setup, from a CLI context (`flask shell`, never a web request --
the same I-6 rule `ivanti_pcs_example.identity.install()` follows, and for
the same reason: DDL and a bulk role scan have no business running inside
the first request or `plugin verify` invocation that happens to reach this
module)::

    from ivanti_pcs_example.hooks import install, sync_tenants_from_roles
    install()                    # creates this package's two attribute tables
    sync_tenants_from_roles()    # populates the tenant table from tenant_<guid> roles

Two small attribute tables back the caller-side hooks, both created by
`install()`:

  * `ivanti_pcs_example_member_guid` -- NOT owned by this module; it is
    `ivanti_pcs_example.identity`'s table, reused here (imported, never
    redefined) so a deployment that already populated it for
    `AttributeIdentity` (§3 of the README) does not have to populate a
    second, parallel one for these hooks to read the same fact from.
  * `ivanti_pcs_example_tenant` (new for this file): `user_id` (FK
    `ab_user.id`) -> `tenant_guid`, filled once by `sync_tenants_from_roles()`
    for every user holding a `tenant_<guid>` role, and from then on by
    whatever provisions a tenant membership (a real deployment's JIT login
    would call `set_tenant` at the point it already knows the tenant, the
    same moment `ivanti_pcs_example.identity.set_member_guid` would be
    called for the member GUID).

A recurring rule worth stating once rather than in every function below:
hooks in this module call their SIBLING functions directly (a plain Python
call to `member_guid`/`tenant_guid`/`tenant_administrator_group` etc.,
defined further down in this same file), never the
`superset_ownership.identity` facades (`resolve_member_guid`,
`resolve_tenant_guid`, `identity.group_id`, ...). Those facades resolve
through the ACTIVE `Identity`/hook registry (`plugins.get_identity()`'s
`HookedIdentity` wrapper) -- and when `config_hooks_example.py` is in force,
the active hook for, say, `OWNERSHIP_TENANT_GUID` IS this module's own
`tenant_guid` function. Calling `identity.resolve_tenant_guid(user)` from
inside `tenant_guid`'s own fallback branch would therefore call back into
`tenant_guid` itself -- unbounded recursion the first time the table has no
row for a user. Every fallback below goes to
`superset_ownership.identity.DefaultIdentity()` directly instead: the raw
class, never wrapped by a hook, which is what "the default" is supposed to
mean for a hook that itself failed to find something newer.

Every hook here is read-only, takes its arguments explicitly, and never
imports `flask.g` or `current_user` (§4.5.1 items 2 and 6) -- and none of
them catch their own exceptions: a real DB error or a real OpenFGA failure
propagates out of these functions exactly as raised, because
`plugin_hooks.call`/`call_or_default` (the machinery that invokes every hook
in this registry) is what classifies and fail-closes it (§4.5.1 item 4);
catching here too would just hide the raise from `plugin verify`'s
`hooks/*` section, which calls each configured hook and reports exactly
that.
"""

from __future__ import annotations

import re
from typing import Any, Optional

import sqlalchemy as sa

# Reused, not redefined: this package's existing member-GUID attribute
# table (`ivanti_pcs_example_member_guid`), so a deployment that already
# populated it for `AttributeIdentity` (README §3) is answering the SAME
# fact for these hooks, not a second copy of it.
from ivanti_pcs_example.identity import (
    install as _install_member_guid_table,
    member_guid_table,
)

_METADATA = sa.MetaData()

# One tenant GUID per Superset account -- new for this file. A real
# deployment's own attribute store (or JIT claim) replaces this table; see
# the module docstring for why it is a *table*, not
# `user.extra_attributes` (identity.py's module docstring has the full
# story: that FAB relationship is a fixed-column row with no generic slot).
tenant_table = sa.Table(
    "ivanti_pcs_example_tenant",
    _METADATA,
    sa.Column("user_id", sa.Integer, primary_key=True),
    sa.Column("tenant_guid", sa.String(64), nullable=False, index=True),
)


def install() -> None:
    """One-time setup for BOTH of this file's attribute tables -- run once,
    from a CLI context, before first use (I-6; see the module docstring).
    Creates `ivanti_pcs_example_member_guid` (via
    `ivanti_pcs_example.identity.install`, so calling this alone is enough
    even if that module's own `install()` was never called separately) and
    this file's own `ivanti_pcs_example_tenant`.
    """
    from superset import db

    _install_member_guid_table()
    tenant_table.create(bind=db.engine, checkfirst=True)


def set_tenant(user: Any, guid: str) -> None:
    """Record `user`'s tenant GUID, replacing any row already on file --
    the manual equivalent of what a real JIT login would call at
    provisioning time, and what `sync_tenants_from_roles` calls in bulk."""
    from superset import db

    db.session.execute(sa.delete(tenant_table).where(tenant_table.c.user_id == user.id))
    db.session.execute(tenant_table.insert().values(user_id=user.id, tenant_guid=guid))
    db.session.commit()


def sync_tenants_from_roles() -> int:
    """Populate `ivanti_pcs_example_tenant` for every user holding a
    `tenant_<guid>` FAB role -- the one-time backfill an operator runs
    before switching a running instance to `config_hooks_example.py`
    (`tenant_guid`'s hook below only reads this table; it stops seeing a
    user's tenant if the table has never been told about them at all,
    unlike `DefaultIdentity`, which always has the role to fall back on).

    Reads the role the same way `DefaultIdentity.tenant_guid` does --
    calling that class directly, never through `identity.resolve_tenant_guid`
    (the module docstring's recursion note) -- so this helper and this
    file's own `tenant_guid` hook agree on what "the role says" means.
    Returns the number of accounts written.
    """
    from superset_ownership.identity import DefaultIdentity

    from superset import security_manager

    default_identity = DefaultIdentity()
    count = 0
    for user in security_manager.get_all_users():
        guid = default_identity.tenant_guid(user)
        if guid:
            set_tenant(user, guid)
            count += 1
    return count


# -------------------------------------------------------------------------- caller-side


def member_guid(user: Any) -> Optional[str]:
    """OWNERSHIP_MEMBER_GUID: the default reads a GUID out of the username
    or email; this reads `ivanti_pcs_example_member_guid` (this package's
    existing attribute table, shared with `AttributeIdentity`) instead, and
    falls back to `DefaultIdentity`'s own computation -- NOT the bare
    username (H-2c: the documented shape for this hook, §4.5.2 row 1, is a
    GUID v4 or the `local-<id>` placeholder; a bare username is neither) --
    when the account has no row, so an account created outside the JIT flow
    still resolves to something in the documented shape instead of a raw
    username or `None`.
    """
    if user is None:
        return None
    from superset import db

    row = (
        db.session.execute(
            member_guid_table.select().where(member_guid_table.c.user_id == user.id)
        )
        .mappings()
        .first()
    )
    if row is not None:
        return row["member_guid"]
    from superset_ownership.identity import DefaultIdentity

    return DefaultIdentity().member_guid(user)


def tenant_guid(user: Any) -> Optional[str]:
    """OWNERSHIP_TENANT_GUID: the default scans the user's roles for
    `tenant_<guid>`; this reads the new `ivanti_pcs_example_tenant` table
    instead, falling back to `DefaultIdentity`'s role scan (called
    directly, per the module docstring's recursion note) when the table has
    no row for this account yet -- e.g. before `sync_tenants_from_roles`
    has run, or for an account provisioned outside it.
    """
    if user is None:
        return None
    from superset import db

    row = (
        db.session.execute(
            tenant_table.select().where(tenant_table.c.user_id == user.id)
        )
        .mappings()
        .first()
    )
    if row is not None:
        return row["tenant_guid"]
    from superset_ownership.identity import DefaultIdentity

    return DefaultIdentity().tenant_guid(user)


def display_name(user: Any) -> str:
    """OWNERSHIP_DISPLAY_NAME: "Last, First" order -- deliberately the
    OPPOSITE of the default's "First Last", so a screenshot of the UI is
    enough to prove this hook is live rather than the default silently
    still answering. Falls back to the username when neither name is set,
    same as the default.
    """
    if user is None:
        return ""
    last = (getattr(user, "last_name", "") or "").strip()
    first = (getattr(user, "first_name", "") or "").strip()
    if last and first:
        return f"{last}, {first}"
    return last or first or user.username


def is_tenant_administrator(user: Any) -> bool:
    """OWNERSHIP_IS_TENANT_ADMINISTRATOR: "our source first, store second".
    The default asks the Directory seam whether the user is a member of
    `tenant_administrator_<tenant>`. This hook first checks a FAB role of
    our own naming, `neurons_tenant_admin_<tenant>` -- modelling a
    deployment whose administrator flag is a role their JIT already grants,
    cheaper than a store round trip -- and only when the user holds no such
    role does it fall through to the store, through the Directory seam
    exactly as the default does (`get_directory().user_in_group`, which
    itself consults `OWNERSHIP_USER_IN_GROUP` first when that hook is also
    configured -- see `user_in_group` below).
    """
    if user is None:
        return False
    tenant = tenant_guid(user)
    if not tenant:
        return False
    role_name = f"neurons_tenant_admin_{tenant}"
    if any(
        (getattr(role, "name", "") or "") == role_name
        for role in (getattr(user, "roles", None) or [])
    ):
        return True
    guid = member_guid(user)
    if not guid:
        return False
    from superset_ownership.plugins import get_directory

    return get_directory().user_in_group(guid, tenant_administrator_group(tenant))


def user_for_member_guid(guid: str) -> Any:
    """OWNERSHIP_USER_FOR_MEMBER_GUID: the reverse of `member_guid` above,
    with the forward-mapping check the contract requires (§4.2) -- a match
    is accepted only when `member_guid(that account) == guid` for the SAME
    account, so a stale or duplicated row can never disagree with the
    forward direction. Tries the attribute table first (the reverse of
    `member_guid`'s primary source), then `member_guid`'s own fallback
    (username, then email) so a guid that only ever matched through that
    fallback still resolves back.
    """
    if not guid:
        return None
    from superset import db, security_manager

    row = (
        db.session.execute(
            member_guid_table.select().where(member_guid_table.c.member_guid == guid)
        )
        .mappings()
        .first()
    )
    if row is not None:
        user = security_manager.get_user_by_id(row["user_id"])
        if user is not None and member_guid(user) == guid:
            return user
    user = security_manager.find_user(username=guid)
    if user is None:
        user = security_manager.find_user(email=guid)
    if user is not None and member_guid(user) == guid:
        return user
    return None


# ----------------------------------------------------------------------- directory-side
#
# Every function below reads the OpenFGA store through
# `superset_ownership.fga` DIRECTLY -- the module's own client, never
# `OpenFGADirectory` -- the "Neurons queries its own store" shape the
# contract asks this file to demonstrate. Every store read passes
# `strict=True` and does not catch what it raises: a real store failure
# must reach `plugin_hooks.call`, which is what fail-closes it and is what
# `plugin verify`'s `hooks/*` section inspects (module docstring). Every
# page request is capped at the store's own maximum page size (100); this
# reference implementation reads one such page per call rather than
# looping to fill the caller's `limit` the way `OpenFGADirectory` does --
# enough to prove the direct-query shape and to round-trip a `next_cursor`
# a caller resumes from, without reproducing that class's own pagination
# loop here too.

_STORE_MAX_PAGE_SIZE = 100


def _page_size(limit: Any) -> int:
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = _STORE_MAX_PAGE_SIZE
    return max(1, min(n, _STORE_MAX_PAGE_SIZE))


def users_of_tenant(
    tenant: str, query: str, limit: int, cursor: Optional[str]
) -> dict[str, Any]:
    """OWNERSHIP_USERS_OF_TENANT: the default reads `tenant:<t>#member`
    tuples through `OpenFGADirectory`; this reads the identical tuple
    shape, but through `fga.read_page` directly."""
    from superset_ownership import fga

    tuple_key = {"object": f"tenant:{tenant}", "relation": "member"}
    tuples, next_token = fga.read_page(
        tuple_key, page_size=_page_size(limit), token=cursor or "", strict=True
    )
    q = (query or "").strip().lower()
    items = []
    for t in tuples:
        raw_user = t.get("user", "")
        if not raw_user.startswith("user:"):
            continue
        guid = raw_user.split(":", 1)[1]
        account = user_for_member_guid(guid)
        label = display_name(account) if account is not None else guid
        if q and q not in label.lower() and q not in guid.lower():
            continue
        items.append(
            {
                "guid": guid,
                "display_name": label,
                "email": getattr(account, "email", None),
                "superset_id": getattr(account, "id", None),
            }
        )
    return {"items": items, "next_cursor": next_token or None}


def groups_of_tenant(
    tenant: str, query: str, limit: int, cursor: Optional[str]
) -> dict[str, Any]:
    """OWNERSHIP_GROUPS_OF_TENANT: the default's fast path reads the
    `group.tenant` relation through `OpenFGADirectory._fast_groups`; this
    reads the identical tuple shape directly (a `user`-filtered,
    bare-type-object Read: `tenant:<t>#member tenant group:`)."""
    from superset_ownership import fga

    tuple_key = {
        "user": f"tenant:{tenant}#member",
        "relation": "tenant",
        "object": "group:",
    }
    tuples, next_token = fga.read_page(
        tuple_key, page_size=_page_size(limit), token=cursor or "", strict=True
    )
    q = (query or "").strip().lower()
    items = []
    for t in tuples:
        obj = t.get("object", "")
        if not obj.startswith("group:"):
            continue
        parsed = split_group_id(obj)
        if parsed is None or parsed[1] != tenant.lower():
            continue
        name, group_tenant = parsed
        label = group_display_name(obj)
        if q and q not in label.lower() and q not in name.lower():
            continue
        items.append(
            {"id": obj, "display_name": label, "tenant": group_tenant, "members": None}
        )
    return {"items": items, "next_cursor": next_token or None}


def members_of_group(
    group_id: str, limit: int, cursor: Optional[str]
) -> dict[str, Any]:
    """OWNERSHIP_MEMBERS_OF_GROUP: the default reads `group:<id>#member`
    tuples (nested groups expanded); this reads the same relation directly
    on the ONE object, one level, no nested-group expansion -- enough to
    demonstrate the direct-query shape without reproducing
    `OpenFGADirectory.group_members`'s two-level walk here too.
    """
    from superset_ownership import fga

    obj = group_id if group_id.startswith("group:") else f"group:{group_id}"
    tuples, next_token = fga.read_page(
        {"object": obj, "relation": "member"},
        page_size=_page_size(limit),
        token=cursor or "",
        strict=True,
    )
    items = []
    for t in tuples:
        raw_user = t.get("user", "")
        if not raw_user.startswith("user:"):
            continue
        guid = raw_user.split(":", 1)[1]
        account = user_for_member_guid(guid)
        items.append(
            {
                "guid": guid,
                "display_name": display_name(account) if account is not None else guid,
                "email": getattr(account, "email", None),
                "superset_id": getattr(account, "id", None),
            }
        )
    return {"items": items, "next_cursor": next_token or None}


def user_in_group(  # noqa: A001 - `member_guid` is the contract's own arg name
    member_guid: str, group_id: str
) -> bool:
    """OWNERSHIP_USER_IN_GROUP: the default makes one `check`; this makes
    the identical `check`, but through `fga.check` directly rather than
    through `OpenFGADirectory.user_in_group`. `fga.check` has no `strict`
    variant of its own -- a transport failure inside it is already logged
    there and answered `False`, the same fail-closed answer this hook would
    give had it raised instead, so nothing further is needed here.
    """
    from superset_ownership import fga

    obj = group_id if group_id.startswith("group:") else f"group:{group_id}"
    return fga.check(f"user:{member_guid}", "member", obj)


def group_exists(group_id: str) -> bool:
    """OWNERSHIP_GROUP_EXISTS: the default is "a tenant tuple, or at least
    one member"; this reads both relations directly, capped at one row
    each (a single-page existence probe needs nothing more)."""
    from superset_ownership import fga

    obj = group_id if group_id.startswith("group:") else f"group:{group_id}"
    tuples, _ = fga.read_page(
        {"object": obj, "relation": "tenant"}, page_size=1, strict=True
    )
    if tuples:
        return True
    tuples, _ = fga.read_page(
        {"object": obj, "relation": "member"}, page_size=1, strict=True
    )
    return bool(tuples)


def administrators_of_tenant(tenant: str) -> list[dict[str, Any]]:
    """OWNERSHIP_ADMINISTRATORS_OF_TENANT: the default is "members of
    `group:tenant_administrator_<tenant>`"; this UNIONS that store group
    (read directly, one capped page) with the holders of the
    `neurons_tenant_admin_<tenant>` FAB role that `is_tenant_administrator`
    above also checks first -- so the two hooks agree about who
    administers a tenant regardless of which of the two sources granted it.
    Active accounts only, same as the default.
    """
    from superset_ownership import fga

    from superset import security_manager

    role_name = f"neurons_tenant_admin_{tenant}"
    by_guid: dict[str, dict[str, Any]] = {}
    for user in security_manager.get_all_users():
        if not getattr(user, "is_active", False):
            continue
        if any(
            (getattr(role, "name", "") or "") == role_name
            for role in (getattr(user, "roles", None) or [])
        ):
            guid = member_guid(user)
            if guid:
                by_guid[guid] = {
                    "guid": guid,
                    "display_name": display_name(user),
                    "email": user.email,
                    "superset_id": user.id,
                }

    group_obj = tenant_administrator_group(tenant)
    tuples, _ = fga.read_page(
        {"object": group_obj, "relation": "member"},
        page_size=_STORE_MAX_PAGE_SIZE,
        strict=True,
    )
    for t in tuples:
        raw_user = t.get("user", "")
        if not raw_user.startswith("user:"):
            continue
        guid = raw_user.split(":", 1)[1]
        if guid in by_guid:
            continue
        account = user_for_member_guid(guid)
        if account is None or not getattr(account, "is_active", False):
            continue
        by_guid[guid] = {
            "guid": guid,
            "display_name": display_name(account),
            "email": account.email,
            "superset_id": account.id,
        }
    return list(by_guid.values())


# --------------------------------------------------------------------------- shape-side
#
# Pure, no I/O. Implements the contract's default `{name}_{tenant}` layout
# EXPLICITLY -- not by delegating to `superset_ownership.identity`'s own
# `_default_group_id`/`_default_split_group_id` -- so this pair proves the
# shape hooks are the seam, not a relabelled call into the code they stand
# in for. `group_id`/`split_group_id` are exact inverses of each other (the
# contract's own requirement, §4.5.2) and consistent with the tuples the
# acceptance store already holds: `OWNERSHIP_GROUP_ID_FORMAT`'s default is
# this same `{name}_{tenant}` layout, so a store seeded under the default
# format still parses under these hooks.

_GUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_GROUP_ID_RE = re.compile(
    rf"^(?P<name>.+)_(?P<tenant>{_GUID_RE.pattern})$", re.IGNORECASE
)

TENANT_ADMINISTRATOR_GROUP_NAME = "tenant_administrator"


def group_id(name: str, tenant: str) -> str:
    """OWNERSHIP_GROUP_ID: `{name}_{tenant}`, explicitly -- the tenant is
    lower-cased, matching how `split_group_id` and `resolve_tenant_guid`
    report a tenant elsewhere in this contract."""
    if not name or not tenant:
        raise ValueError("a group id needs both a name and a tenant")
    return f"{name}_{tenant.lower()}"


def split_group_id(group_id_or_ref: Optional[str]) -> Optional[tuple[str, str]]:
    """OWNERSHIP_SPLIT_GROUP_ID: the inverse of `group_id` above. Accepts
    `<id>`, `group:<id>` and `group:<id>#member`, exactly like the default;
    returns `None` for anything that does not parse as `{name}_{tenant}`
    with a GUID v4 tenant."""
    if not group_id_or_ref:
        return None
    text = group_id_or_ref
    if text.startswith("group:"):
        text = text[len("group:") :]
    text = text.split("#", 1)[0]
    match = _GROUP_ID_RE.match(text)
    if not match:
        return None
    return match.group("name"), match.group("tenant").lower()


def group_display_name(group_id_or_ref: str) -> str:
    """OWNERSHIP_GROUP_DISPLAY_NAME: title-case with spaces
    (`dashboard_designer` -> `Dashboard Designer`) -- visibly different
    from the default, which reports the bare name (`dashboard_designer`,
    underscores kept)."""
    parsed = split_group_id(group_id_or_ref)
    name = parsed[0] if parsed is not None else (group_id_or_ref or "")
    if name.startswith("group:"):
        name = name[len("group:") :]
    return name.replace("_", " ").title()


def tenant_administrator_group(tenant: str) -> str:
    """OWNERSHIP_TENANT_ADMINISTRATOR_GROUP: `group:tenant_administrator_<tenant>`,
    built through THIS file's own `group_id` (above), the same shape the
    default produces (`identity.TENANT_ADMINISTRATOR_GROUP`), so a store
    seeded under either format names the same object."""
    return f"group:{group_id(TENANT_ADMINISTRATOR_GROUP_NAME, tenant)}"


# ------------------------------------------------------------------------ decision-side


def can_manage(user: Any, object_state: dict[str, Any]) -> Optional[str]:
    """OWNERSHIP_CAN_MANAGE: passes the default reason through unchanged,
    except one deliberate NARROWING (the only kind a hook may make, §4.5.2):
    a caller admitted only under `manage_permission` (the optional
    sharing-manager role, never admin/owner/tenant_admin) may not manage an
    object whose visibility is `"private"`. `user` is accepted, unused, to
    match the contract's `(user, object_state) -> reason` signature -- the
    example needs no fact about the caller beyond what
    `object_state["default_reason"]` already encodes.
    """
    default_reason = object_state.get("default_reason")
    if (
        default_reason == "manage_permission"
        and object_state.get("visibility") == "private"
    ):
        return None
    return default_reason

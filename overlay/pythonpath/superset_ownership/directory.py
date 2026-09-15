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
"""The `Directory` protocol: who can be shared with.

Split out of `Authorizer` (spec `qa/design/directory-hook/02-technical-spec.md`
section 7). A `Directory` answers the picker, subject validation, tenant
administrator resolution and backfill attribution -- questions about PEOPLE.
It never sits on the access path: an access decision is a `check` against
the `Authorizer`, made once per request, and a plug-in that also had to be
consulted there would put a network call on every dashboard open. That
invariant is enforced structurally (`test_directory.py` and the harness's
counting proxies), not just documented.

The protocol is read-only by construction -- it has no write method -- so an
override cannot become a second writer to the authorization store.

    OWNERSHIP_DIRECTORY = "openfga"  # reads the store (default with openfga)
    OWNERSHIP_DIRECTORY = "local"    # FAB roles and users (default with local)

This module also carries a lazy fallback accessor (`get_directory`) for use
without `superset_ownership.plugins`' loader/registry -- a script or test
that imports this module directly, with no app registry loaded: call sites
import `get_directory` from `plugins` first and fall back to this module's
copy, so the registry accessor takes over without either seam having to
change shape whenever a registry IS loaded.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import (
    Any,
    Generic,
    Optional,
    Protocol,
    runtime_checkable,
    TypedDict,
    TypeVar,
)

from superset_ownership.fga import (
    StoreRejected as DirectoryRejected,
    StoreUnavailable as DirectoryUnavailable,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Re-exported so an override need not import `superset_ownership.fga` to
# raise the classified failures the call sites already know how to handle
# (`DirectoryUnavailable` -> "cannot verify", `DirectoryRejected` -> "no").
__all__ = [
    "UserRef",
    "GroupRef",
    "Page",
    "DirectoryHealth",
    "Directory",
    "DirectoryUnavailable",
    "DirectoryRejected",
    "OpenFGADirectory",
    "LocalDirectory",
    "get_directory",
    "validate_page",
    "USER_REF_KEYS",
    "GROUP_REF_KEYS",
    "_DIRECTORIES",
    "tenant_members",
    "group_exists",
]


class UserRef(TypedDict):
    guid: str
    display_name: str
    email: Optional[str]
    superset_id: Optional[int]


class GroupRef(TypedDict):
    id: str
    display_name: str
    tenant: str
    members: Optional[int]  # None = not counted (the fast path)


class Page(TypedDict, Generic[T]):
    items: list[T]
    next_cursor: Optional[str]


@dataclass(frozen=True)
class DirectoryHealth:
    ok: bool
    detail: str = ""
    last_sync_at: Optional[str] = None


USER_REF_KEYS = frozenset({"guid", "display_name", "email", "superset_id"})
GROUP_REF_KEYS = frozenset({"id", "display_name", "tenant", "members"})


def validate_page(page: Any, item_keys: frozenset[str] = USER_REF_KEYS) -> "Page[Any]":
    """Guard a plugged `Directory`'s return value at the boundary (M-1).

    A misbehaving override might return `None`, a bare list, a dict missing
    `items`/`next_cursor`, or item dicts missing required keys; left
    unchecked, that reaches the caller as a raw `AttributeError` /
    `TypeError` / `KeyError` -- an uncaught 500 carrying driver text, not
    the fixed "cannot verify"/`degraded` answer the contract promises.
    Raises `DirectoryRejected`, classified by callers exactly like any
    other store rejection (fail-closed, no grant, no plug-in text echoed).
    """
    if not isinstance(page, dict) or "items" not in page or "next_cursor" not in page:
        raise DirectoryRejected(
            f"directory returned a malformed Page: {type(page).__name__}"
        )
    items = page["items"]
    if not isinstance(items, list):
        raise DirectoryRejected(
            f"directory Page.items is not a list: {type(items).__name__}"
        )
    for item in items:
        if not isinstance(item, dict) or not item_keys.issubset(item):
            raise DirectoryRejected(
                f"directory Page item missing required keys "
                f"{sorted(item_keys)}: {item!r}"
            )
    return page  # type: ignore[return-value]


@runtime_checkable
class Directory(Protocol):
    """Answers only the picker, subject validation, tenant-administrator
    resolution and backfill attribution -- never the access path. No method
    takes `strict`: every method RAISES `DirectoryUnavailable` /
    `DirectoryRejected` on a store failure rather than answering an
    ambiguous default, so a caller that must fail closed (`_validate_subject`)
    and a caller that may fail open can both be written against one
    contract, exactly as `_validate_subject` classifies `StoreError` (§7.7 invariant).
    """

    protocol_version: int

    def search_users(
        self, tenant: str, query: str, *, limit: int = 100, cursor: Optional[str] = None
    ) -> "Page[UserRef]": ...

    def user_in_tenant(self, member_guid: str, tenant: str) -> bool: ...

    def list_groups(
        self, tenant: str, *, cursor: Optional[str] = None
    ) -> "Page[GroupRef]": ...

    def group_members(
        self, group_id: str, *, cursor: Optional[str] = None
    ) -> "Page[UserRef]": ...

    def user_in_group(self, member_guid: str, group_id: str) -> bool: ...

    def group_exists(self, group_id: str) -> bool: ...

    def tenant_administrators(self, tenant: str) -> list[UserRef]: ...

    def health(self) -> DirectoryHealth: ...


# ---------------------------------------------------------------------------
# settings.get() fallback: the settings module (spec section 3) is another
# seam in this series and is not necessarily on this branch yet. Read
# straight from the environment when it is absent, which is exactly the
# no-app-context branch `settings.get` documents for itself -- so once
# `settings.py` lands this local copy can be deleted with no behaviour
# change for a deployment that only ever used the environment.
# ---------------------------------------------------------------------------


def _setting(name: str, default: T, parser=None) -> T:
    try:
        from superset_ownership import settings as _settings
    except ImportError:
        pass
    else:
        return (
            _settings.get(name, default, parser)
            if parser
            else _settings.get(name, default)
        )
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    if parser is None:
        return raw  # type: ignore[return-value]
    try:
        return parser(raw)
    except Exception:  # noqa: BLE001 - an unreadable value falls back, like settings.get
        logger.warning(
            "superset_ownership: %s=%r is not valid; using %r", name, raw, default
        )
        return default


_GROUP_WALK_MODES = frozenset({"auto", "always", "never"})

# N-4: a sane cap on the ONE cursor loop in this class that is entirely
# ours (no plug-in involved) -- `OpenFGADirectory.search_users` follows a
# token `fga.read_page` hands back directly from OpenFGA. `/subjects`
# (api.py) caps its own `list_groups` loop at 20 pages for the same reason;
# this is generous because a real tenant can legitimately have many pages
# of members when `limit` is large.
_MAX_SEARCH_USERS_PAGES = 1000

# I-1: OpenFGA rejects any `page_size` outside `[1, 100]` on a Read
# (`400 page_size_invalid`). `search_users` used to request the caller's
# entire remaining budget in one page (`page_size=limit - len(items)`),
# which is correct for N-3's "never lose a truncated remainder" contract
# but not for a store that rejects anything over 100 -- the default picker
# call (`api.py`'s `limit=200`) turned into a single oversized Read that
# OpenFGA refuses outright, so the picker's whole user list came back
# empty (`degraded: true`) for every tenant. Every page requested here is
# now capped at the store's own limit and the loop keeps going until
# `limit` is reached or the store runs out of pages, so N-3's guarantee
# (no page ever contributes more than there is room for, nothing lost
# past a cut) still holds -- it is now enforced per store page instead of
# in one page.
_STORE_MAX_PAGE_SIZE = 100


def _valid_group_walk_mode(raw: T) -> str:
    """`OWNERSHIP_DIRECTORY_GROUP_WALK` (L-4): only the exact spellings
    `auto`/`always`/`never` select a mode. Anything else -- a typo
    (`Always`), a legacy spelling (`off`) -- used to be silently `auto`
    (the dispatcher's `if self._mode == "always":` simply never matched),
    against the settings module's own warn-on-unrecognised rule; this warns
    once, naming the bad value, and still falls back to `auto`. Exact, not
    case-folded: unlike `OWNERSHIP_AUTHORIZER`/`OWNERSHIP_DIRECTORY`, a
    differently-cased spelling here is exactly the silent-typo case the
    fix closes, so it does not get normalised into passing.
    """
    value = raw.strip() if isinstance(raw, str) else raw
    if value in _GROUP_WALK_MODES:
        return value  # type: ignore[return-value]
    logger.warning(
        "superset_ownership: OWNERSHIP_DIRECTORY_GROUP_WALK=%r is not one of "
        "%s; using 'auto'",
        raw,
        sorted(_GROUP_WALK_MODES),
    )
    return "auto"


# ---------------------------------------------------------------------------
# Group/tenant-membership reads (PCS-10243 #91, residue 1). Moved here from
# `fga.py` -- spec §2 always meant this logic to live beside `Directory`
# rather than in the bare HTTP client, and every production caller already
# reached it that way (`OpenFGADirectory` and `lifecycle._member_guids` call
# through rather than duplicating it); only the bodies themselves stayed
# behind. `fga.tenant_members`/`fga.group_exists` are now thin, deprecated
# re-exports of the functions below, kept for one release for any caller
# still importing them from there. `fga.groups_for_tenant` had no such
# re-export made of it (M3, review round 1, PR #101): the non-strict walk it
# wrapped had no caller left once `OpenFGADirectory._walk_groups` became its
# own strict version rather than delegating to it, so it was deleted
# outright rather than carried forward as a fourth shim nothing calls.
# ---------------------------------------------------------------------------


def tenant_members(tenant_guid: str, *, strict: bool = False) -> list[str]:
    """Member GUIDs belonging to a tenant, from OpenFGA."""
    from superset_ownership import fga

    return [
        t["user"].split(":", 1)[1]
        for t in fga.read_all(f"tenant:{tenant_guid}", "member", strict=strict)
        if t["user"].startswith("user:")
    ]


def group_exists(group_name: str) -> bool:
    """Does this group exist in the authorization store?

    A group exists if anything is a member of it. There is no "list groups"
    call, and a group with no members grants nothing anyway, so membership is
    both the available test and the meaningful one.

    Fails CLOSED on a store error, like every other read here: an
    unverifiable group is not granted.

    N1 (review round 1, PR #101): before the move, this built the request
    body by hand but already sent it through `fga._request` -- the
    connection's headers and 401-refresh-and-retry applied then too, so
    going through `fga.read_page` now changes neither. What actually
    changed on the wire: the body now carries `page_size: 100` (`read_page`
    always sets it; the hand-built body did not), and the failure log line
    changed from `openfga group_exists failed for <name>` to `openfga
    read_page failed tuple_key={...}` -- `read_page`'s own `what` label,
    not one naming this function.
    """
    from superset_ownership import fga

    tuples, _ = fga.read_page({"relation": "member", "object": f"group:{group_name}"})
    return bool(tuples)


class OpenFGADirectory:
    """Reads the authorization store. The default `Directory` when
    `OWNERSHIP_AUTHORIZER=openfga`.

    Group listing has two paths (spec section 7.2): the FAST path reads the
    additive `group.tenant` relation directly, one paginated Read per
    tenant; the FALLBACK walks out from tenant members the way the
    pre-split `fga.groups_for_tenant` does, one call per member. Which path a tenant
    uses is decided once per tenant and cached with a TTL
    (`OWNERSHIP_DIRECTORY_GROUP_WALK_TTL`), because a store mid-rollout has
    some tenants backfilled with the new tuple and some not -- a single
    process-wide flag would give one tenant's answer to another's tenant
    (the correctness bug closed in review round 1, finding B3).
    """

    name = "openfga"
    protocol_version = 1

    def __init__(self) -> None:
        # Instance state, initialised HERE and never at class level: a
        # class-level dict/set would be shared by every OpenFGADirectory
        # instance in the process (and across test cases).
        self._mode = _valid_group_walk_mode(
            _setting("OWNERSHIP_DIRECTORY_GROUP_WALK", "auto")
        )
        self._ttl = _setting("OWNERSHIP_DIRECTORY_GROUP_WALK_TTL", 300, _as_int)
        self._verdict: dict[
            str, tuple[bool, float]
        ] = {}  # tenant -> (fast_ok, expires_at)
        self._model_lacks_relation: Optional[bool] = (
            None  # store-wide: a model-version fact
        )
        self._warned: set[str] = set()  # tenants that already got their one WARNING

    # -- picker ---------------------------------------------------------

    def search_users(
        self, tenant: str, query: str, *, limit: int = 100, cursor: Optional[str] = None
    ) -> "Page[UserRef]":
        """Tenant members from the store, labelled from Superset's `ab_user`.

        Paginated on the store's OWN continuation token (M-3): each store
        page costs one `ab_user` query to label it (D-11: a member Superset
        has never seen still appears, keyed by GUID), and the walk stops
        once `limit` matching rows are collected or the store runs out of
        pages -- `next_cursor` is that token, so a caller can resume
        exactly where this page left off. Raises on a store failure (the
        protocol's contract) rather than answering an empty page silently;
        the picker route (`api.py:search_subjects`) is the caller that
        decides what a raise means (`degraded: true`).

        N-3: each store read is sized to the REMAINING budget, capped at
        the store's own page-size limit (I-1:
        `page_size=min(limit - len(items), _STORE_MAX_PAGE_SIZE)`), never
        the store's own default and never more than OpenFGA accepts, so a
        single page can never contribute more matches than there is room
        for -- `len(items)` after processing a page is therefore always
        `<= limit`, and there is never a truncated remainder to lose. The
        earlier version always requested a full default-size page and then
        sliced `items[:limit]` afterwards, silently discarding whatever
        matched past the cut -- that row's page was never revisited, so it
        was unreachable by any cursor the caller could resume from. A
        still-earlier version (I-1) requested the entire remaining budget
        in one page, which OpenFGA rejects outright once it exceeds 100
        (`400 page_size_invalid`) -- capping the request and looping keeps
        both guarantees at once.
        """
        from superset_ownership import fga

        q = (query or "").strip().lower()
        items: list[UserRef] = []
        token = cursor or ""
        pages = 0
        while True:
            remaining = limit - len(items)
            if remaining <= 0:
                return {"items": items, "next_cursor": token or None}
            pages += 1
            if pages > _MAX_SEARCH_USERS_PAGES:
                # N-4: a sane cap on a loop this class owns outright (no
                # plug-in involved -- the store token comes straight from
                # `fga.read_page`), so a misbehaving or malicious store
                # cannot hang a request by never advancing the token.
                logger.warning(
                    "ownership: search_users(tenant=%s): stopped after %d "
                    "store pages without reaching limit=%d; the store's "
                    "continuation token may not be advancing",
                    tenant,
                    _MAX_SEARCH_USERS_PAGES,
                    limit,
                )
                return {"items": items, "next_cursor": token or None}
            tuple_key = {"object": f"tenant:{tenant}", "relation": "member"}
            tuples, token = fga.read_page(
                tuple_key,
                page_size=min(remaining, _STORE_MAX_PAGE_SIZE),
                token=token,
                strict=True,
            )
            guids = [
                _unmap_member_guid(t["user"].split(":", 1)[1])
                for t in tuples
                if t.get("user", "").startswith("user:")
            ]
            labels = _user_labels(guids)
            for member_guid in guids:
                display_name, email, superset_id = labels.get(
                    member_guid, (member_guid, None, None)
                )
                matches = (
                    not q
                    or q in display_name.lower()
                    or q in member_guid.lower()
                    or q in (email or "").lower()
                )
                if not matches:
                    continue
                items.append(
                    {
                        "guid": member_guid,
                        "display_name": display_name,
                        "email": email,
                        "superset_id": superset_id,
                    }
                )
            if not token:
                return {"items": items, "next_cursor": None}
            if len(items) >= limit:
                return {"items": items, "next_cursor": token}

    def user_in_tenant(self, member_guid: str, tenant: str) -> bool:
        """One strict `check`, cheaper than the paginated walk it replaces
        (D-3). `fga.check` itself is fail-closed and never raises (the
        contract, used everywhere access decisions read it); a caller that
        must tell "not a member" from "could not ask" needs the store's own
        failure, so this makes the same request directly rather than
        through that fail-closed wrapper. References are mapped through the
        loaded `Authorizer`'s `_subject_ref` (M-4), so an authorizer
        subclass that reshapes how a subject is spelled (a prefixed-id
        override) and this Directory agree on which tuple they mean."""
        return _strict_check(
            _map_ref(f"user:{member_guid}"), "member", _map_ref(f"tenant:{tenant}")
        )

    # -- groups -----------------------------------------------------------

    def list_groups(
        self, tenant: str, *, cursor: Optional[str] = None
    ) -> "Page[GroupRef]":
        """Spec section 7.2's two-path listing: the fast `group.tenant`
        Read when it is known (or assumed) to work for this tenant, the
        member walk otherwise, with the empty-first-page case walking once
        to tell "no groups yet" apart from "tuples not backfilled".

        N5 (review round 1, PR #101, pre-existing observation, not this
        PR's to fix): on this deployment's OpenFGA build, a Read naming a
        relation the model does not define answers `200`/empty rather than
        `400` -- so the `"group#tenant" in str(exc)` lazy discovery below
        (and in `group_exists`/`group_tenant`) never actually fires here;
        the fast path just reads back nothing forever, and the per-tenant
        fallback still works, through the empty-first-page walk above. In
        practice this makes `OpenFGADirectory.health()` the ONLY thing that
        ever sets `_model_lacks_relation = True` on this build -- and
        `health()` runs in the `plugin verify` CLI process, never the one
        serving requests, so this instance's own flag stays `None`
        (equivalent to "present" here) for as long as the process runs.
        """
        if self._mode == "always":
            return self._walk_groups(tenant)
        if self._mode == "never":
            return self._fast_groups(tenant, cursor)  # empty means empty; a 400 raises
        if self._model_lacks_relation:
            return self._walk_groups(tenant)

        fast_ok, expires_at = self._verdict.get(tenant, (None, 0.0))  # type: ignore[assignment]
        if fast_ok is not None and expires_at > time.monotonic():
            return (
                self._fast_groups(tenant, cursor)
                if fast_ok
                else self._walk_groups(tenant)
            )

        try:
            page = self._fast_groups(tenant, cursor)
        except DirectoryRejected as exc:
            if "group#tenant" in str(exc):
                self._model_lacks_relation = True
                self._warn(tenant, "model lacks group.tenant")
                return self._walk_groups(tenant)
            raise

        if page["items"] or cursor:
            # A non-empty first page, or any later page: the fast path is
            # proven for this tenant.
            self._remember(tenant, True)
            return page

        # An empty first page is ambiguous: no groups yet, or tuples not
        # backfilled. Walk ONCE to tell them apart.
        walked = self._walk_groups(tenant)
        self._remember(tenant, not walked["items"])
        if walked["items"]:
            self._warn(tenant, "no group→tenant tuples yet")
        return walked

    def _remember(self, tenant: str, fast_ok: bool) -> None:
        # Prune expired entries and cap the table at 1,000 tenants (oldest
        # expiry evicted first) before inserting, so a very large or very
        # long-lived deployment cannot grow this dict without bound.
        now = time.monotonic()
        self._verdict = {t: v for t, v in self._verdict.items() if v[1] > now}
        if len(self._verdict) >= 1000:
            oldest = min(self._verdict, key=lambda t: self._verdict[t][1])
            del self._verdict[oldest]
        self._verdict[tenant] = (fast_ok, now + self._ttl)

    def _warn(self, tenant: str, why: str) -> None:
        key = "*" if why.startswith("model lacks") else tenant
        if key in self._warned:
            return
        self._warned.add(key)
        logger.warning(
            "ownership: tenant %s: falling back to the member walk for %ds (%s); "
            "install the shipped model and write group→tenant tuples "
            "(fga install-model)",
            tenant,
            self._ttl,
            why,
        )

    def _fast_groups(self, tenant: str, cursor: Optional[str]) -> "Page[GroupRef]":
        """The additive path: one paginated Read of `group.tenant`, filtered
        by `group_belongs_to_tenant`. `members` is left `None` -- counting
        would cost a second Read per group, which is exactly the O(members)
        cost this path exists to avoid."""
        from superset_ownership.identity import (
            group_belongs_to_tenant,
            group_display_name,
        )

        ids, next_cursor = _fast_group_read(tenant, token=cursor or "")
        items: list[GroupRef] = []
        for name in ids:
            gid = f"group:{name}"
            if not group_belongs_to_tenant(gid, tenant):
                continue
            items.append(
                {
                    "id": gid,
                    "display_name": group_display_name(gid).replace("_", " "),
                    "tenant": tenant,
                    "members": None,
                }
            )
        return {"items": items, "next_cursor": next_cursor or None}

    def _walk_groups(self, tenant: str) -> "Page[GroupRef]":
        """The fallback: one member-walk call per tenant member, with
        `members` reported as a count. Owned here rather than delegating to
        `groups_for_tenant` (M-3): that helper folds in a non-strict
        `tenant_members` read, which the protocol's "every method raises"
        contract forbids, so the membership half is made here with
        `strict=True` directly. The per-member `list_objects` call is also
        `strict=True` (PCS-10243 #91, residue 2) -- `fga.list_objects` grew
        a strict variant in #100, so a store failure mid-walk now raises
        `DirectoryUnavailable`/`DirectoryRejected` like every other read in
        this class, instead of silently under-reporting the walk. References
        are mapped through the loaded `Authorizer`'s `_subject_ref` (M-4).

        M3 (review round 1, PR #101): calls the module-level `tenant_members`
        above directly, not `fga.tenant_members` -- that name is now a
        deprecated re-export OF this module's own function (PCS-10243 #91,
        residue 1), and bouncing through it here would have this module
        reach itself through the very shim it deprecates."""
        from superset_ownership import fga
        from superset_ownership.identity import (
            group_belongs_to_tenant,
            group_display_name,
        )

        found: dict[str, set[str]] = {}
        for raw_guid in tenant_members(tenant, strict=True):
            guid = _unmap_member_guid(raw_guid)
            for obj in fga.list_objects(
                _map_ref(f"user:{guid}"), "member", "group", strict=True
            ):
                if not group_belongs_to_tenant(obj, tenant):
                    continue
                found.setdefault(obj, set()).add(guid)
        items: list[GroupRef] = [
            {
                "id": g,
                "display_name": group_display_name(g).replace("_", " "),
                "tenant": tenant,
                "members": len(m),
            }
            for g, m in sorted(found.items())
        ]
        return {"items": items, "next_cursor": None}

    def group_members(
        self, group_id: str, *, cursor: Optional[str] = None
    ) -> "Page[UserRef]":
        from superset_ownership import fga

        obj = group_id if group_id.startswith("group:") else f"group:{group_id}"
        guids: list[str] = []
        nested: list[str] = []
        for t in fga.read_all(obj, "member", strict=True):
            user = t.get("user", "")
            if user.startswith("user:"):
                guids.append(_unmap_member_guid(user.split(":", 1)[1]))
            elif user.startswith("group:"):
                nested.append(user.split("#", 1)[0])
        # `group:X#member` rows are expanded one level, per the protocol
        # note -- a group nested more than one level deep is a modelling
        # choice this method does not need to resolve recursively.
        for nested_obj in nested:
            for t in fga.read_all(nested_obj, "member", strict=True):
                user = t.get("user", "")
                if user.startswith("user:"):
                    guids.append(_unmap_member_guid(user.split(":", 1)[1]))
        seen: dict[str, None] = {}
        for g in guids:
            seen.setdefault(g, None)
        labels = _user_labels(list(seen))
        items = [
            {
                "guid": g,
                "display_name": labels.get(g, (g, None, None))[0],
                "email": labels.get(g, (g, None, None))[1],
                "superset_id": labels.get(g, (g, None, None))[2],
            }
            for g in seen
        ]
        return {"items": items, "next_cursor": None}

    def user_in_group(self, member_guid: str, group_id: str) -> bool:
        obj = group_id if group_id.startswith("group:") else f"group:{group_id}"
        return _strict_check(_map_ref(f"user:{member_guid}"), "member", obj)

    def group_exists(self, group_id: str) -> bool:
        from superset_ownership import fga

        obj = group_id if group_id.startswith("group:") else f"group:{group_id}"
        name = obj.split(":", 1)[-1].split("#", 1)[0]
        # Fast path: a group with a tenant tuple exists (the contract
        # commitment: an empty group with a tenant tuple still exists). Falls
        # back to the member-based test when the model has no
        # `group.tenant` relation, or when the fast test found nothing (an
        # empty-but-real group is indistinguishable from "no such group"
        # without the member fallback).
        if not self._model_lacks_relation:
            try:
                if fga.read_all(obj, "tenant", strict=True):
                    return True
            except fga.StoreRejected as exc:
                if "group#tenant" in str(exc):
                    self._model_lacks_relation = True
                else:
                    raise
            except fga.StoreUnavailable:
                raise
        # `fga.group_exists` is non-strict and swallows a store failure to
        # False (M-3 forbids that here): read the same member tuples
        # directly, strict.
        member_key = {"relation": "member", "object": f"group:{name}"}
        tuples, _ = fga.read_page(member_key, strict=True)
        return bool(tuples)

    def group_tenant(self, group_id: str) -> Optional[str]:
        """The tenant this group's own STORE `tenant` tuple names, or
        `None` when this model has no `group.tenant` relation at all (the
        same fast-path detection `group_exists` uses) or the group simply
        carries no such tuple.

        `api.py`'s M-6 cross-tenant guard uses this to confirm a configured
        `OWNERSHIP_SPLIT_GROUP_ID` hook's tenant claim against an
        independent store fact instead of trusting the hook's own parse
        alone -- a hook that lies (answers the caller's tenant for a
        foreign group) cannot make the STORE agree. Not part of the
        `Directory` protocol (accessed with `getattr(..., None)`,
        best-effort): the `local` backend has no independent group-tenancy
        fact to check against (tenancy there IS the group id format), so it
        does not define this method, and its absence is the caller's
        signal to fall back to the id-format round-trip invariant instead.
        """
        if self._model_lacks_relation:
            return None
        from superset_ownership import fga

        obj = group_id if group_id.startswith("group:") else f"group:{group_id}"
        try:
            tuples = fga.read_all(obj, "tenant", strict=True)
        except fga.StoreRejected as exc:
            if "group#tenant" in str(exc):
                self._model_lacks_relation = True
                return None
            raise
        for t in tuples:
            user = t.get("user", "")
            if user.startswith("tenant:"):
                return user.split(":", 1)[1].split("#", 1)[0]
        return None

    # -- tenant administrators -------------------------------------------

    def tenant_administrators(self, tenant: str) -> list[UserRef]:
        """Parity with the pre-split `backfill._admins_of_tenant`: tenant members
        crossed with `check(member, member, admin_group)`, active accounts
        only. A single-Read version is a v2 optimisation (D-6, settled).
        Strict throughout (M-3: raises rather than answering `[]` on a store
        failure); the reverse account lookup goes through
        `identity.user_for_member_guid` rather than assuming the username
        IS the GUID (H-3), and references are mapped through the loaded
        `Authorizer`'s `_subject_ref` (M-4).

        M3 (review round 1, PR #101): calls the module-level `tenant_members`
        directly, not the deprecated `fga.tenant_members` re-export -- see
        `_walk_groups`'s docstring above.
        """
        from superset_ownership.identity import (
            tenant_administrator_group,
            user_for_member_guid,
        )

        group = tenant_administrator_group(tenant)
        guids: list[str] = []
        for raw_guid in tenant_members(tenant, strict=True):
            guid = _unmap_member_guid(raw_guid)
            if _strict_check(_map_ref(f"user:{guid}"), "member", group):
                guids.append(guid)
        labels = _user_labels(guids)
        out: list[UserRef] = []
        for g in guids:
            user = user_for_member_guid(g)
            if user is None or not getattr(user, "is_active", False):
                continue
            display_name, email, superset_id = labels.get(
                g, (g, None, user.id if user else None)
            )
            out.append(
                {
                    "guid": g,
                    "display_name": display_name,
                    "email": email,
                    "superset_id": superset_id,
                }
            )
        return out

    # -- health -------------------------------------------------------------

    def health(self) -> DirectoryHealth:
        from superset_ownership import fga

        ok = fga.reachable()
        if not ok:
            return DirectoryHealth(ok=False, detail="store unreachable")
        # `get_model`/`list_models` are new in the fga.py half of this series
        # (spec section 5.4) and may not be on this branch yet; report what
        # can be told without them rather than raising.
        get_model = getattr(fga, "get_model", None)
        if get_model is None:
            return DirectoryHealth(
                ok=True, detail="reachable; model relations not checked"
            )
        # M2 (review round 1, PR #101, `review-sweep-pr101.md`): pass the
        # connection's PINNED model id through, exactly as
        # `plugin_verify.RequiredRelations.fetch_model_relations` does
        # (`fga.get_model(connection.model_id if connection else None)`) --
        # an unpinned call always reads the store's latest model, which
        # disagrees with `plugin verify`'s pinned read the moment
        # `OWNERSHIP_FGA_MODEL` names anything but the latest version (a
        # freshly-written model with an old id still deployed, e.g.). Both
        # checks now read the same model the same way, so they cannot
        # disagree about it.
        try:
            from superset_ownership import plugins

            connection = plugins.get_fga_connection()
        except Exception:  # noqa: BLE001 - health never raises
            connection = None
        try:
            model = get_model(connection.model_id if connection else None)
        except Exception as exc:  # noqa: BLE001 - health never raises
            return DirectoryHealth(
                ok=True, detail=f"reachable; model check failed: {exc}"
            )
        model_id = model.get("authorization_model_id") or model.get("id") or "?"
        # PCS-10243 #91, residue 3: probe the MODEL directly (one cheap read,
        # already fetched above) rather than trusting `self._model_lacks_relation`
        # -- that flag is only set the first time the fast group-read path
        # actually hits the 400 that reveals the relation is missing, so a
        # process that has not yet called `list_groups`/`group_exists` for
        # ANY tenant used to report "present" by default here regardless of
        # the model. `_type_relation_in_model` parses the model the same way
        # `plugin_verify._relations_in_model` does, so this can never
        # disagree with `plugin verify`'s `required_relations` check about
        # the same model. Also updates the flag itself, so a caller that
        # asks `health()` first gets the fast path's own verdict for free.
        has_relation = _type_relation_in_model(model, "group", "tenant")
        self._model_lacks_relation = not has_relation
        detail = (
            f"model {model_id}: group.tenant {'present' if has_relation else 'absent'}"
        )
        return DirectoryHealth(ok=True, detail=detail)


def _type_relation_in_model(model_json: dict, type_: str, relation: str) -> bool:
    """Does `model_json` (as returned by `fga.get_model`) define
    `type_.relation`? Deliberately a standalone copy of the same parse
    `plugin_verify._relations_in_model` does, rather than an import of it --
    `plugin_verify` imports this module, and a `directory.py -> plugin_verify`
    import would cycle back. Kept in lockstep so `OpenFGADirectory.health()`
    and `plugin verify`'s `required_relations` check read the same model the
    same way and can never disagree about it (PCS-10243 #91, residue 3)."""
    for t in model_json.get("type_definitions", []) or []:
        if t.get("type") == type_:
            return relation in (t.get("relations") or {})
    return False


def _fast_group_read(tenant: str, *, token: str = "") -> tuple[list[str], str]:
    """The one Read shape `fga.read_all`/`fga.list_objects` cannot make: a
    `user`-filtered, bare-type-object Read (`fga.tenant_objects` uses the
    identical shape, but is not strict and cannot tell a 400 -- the model
    lacking `group.tenant` -- apart from a store that is merely unreachable).

    Goes through `fga.read_page` (H-1): that function already accepts an
    arbitrary `tuple_key` and threads the connection through `fga._request`
    (headers, the 401 refresh), so there is no need to build this request by
    hand -- the earlier version's own raw `requests.post` bypassed both and
    answered 401 on every call against a credentialed store.
    """
    from superset_ownership import fga

    tuple_key = {
        "user": f"tenant:{tenant}#member",
        "relation": "tenant",
        "object": "group:",
    }
    tuples, next_token = fga.read_page(tuple_key, token=token, strict=True)
    ids = [
        t["object"].split(":", 1)[1]
        for t in tuples
        if t.get("object", "").startswith("group:")
    ]
    return ids, next_token


def _strict_check(user: str, relation: str, obj: str) -> bool:
    """The same request `fga.check` makes, but raising `DirectoryUnavailable`
    / `DirectoryRejected` on a store failure instead of answering `False`.

    `fga.py` has no strict `check` yet, so this builds the request body
    itself -- but goes through `fga._request` (H-1) for the actual HTTP
    call, exactly as every strict call in `fga.py` does: the connection's
    headers and the 401-refresh-and-retry are applied here too, which the
    earlier version's raw `requests.post` skipped entirely (headers=None on
    a credentialed store, so every check answered 401). `_request` itself
    no longer takes ``strict`` (part 1 of the round-2 review moved
    classification to the caller for every function in `fga.py`); pass
    ``model=True`` so the pinned model id is merged fresh on the 401 retry
    too, rather than baked in once from the pre-refresh connection."""
    from superset_ownership import fga

    what = f"check {user} {relation} {obj}"
    body = {"tuple_key": {"user": user, "relation": relation, "object": obj}}
    try:
        resp = fga._request(  # noqa: SLF001 - the ONE place that talks HTTP (fga.py)
            "POST", "/check", json=body, what=what, model=True
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - classified below
        raise fga._classify(what, exc) from exc  # noqa: SLF001
    return bool(resp.json().get("allowed", False))


# ---------------------------------------------------------------------------
# Reference mapping (M-4): OpenFGADirectory only ever names user/group/tenant
# identities -- never an owned asset -- so the loaded Authorizer's single
# `_subject_ref` hook (spec section 8) covers every reference this class
# builds or reads back, on either side of a tuple_key. Consulted here rather
# than assumed identity, so a prefixed-id Authorizer subclass (issue #73)
# and the default OpenFGADirectory agree on which store tuple they mean.
# ---------------------------------------------------------------------------


def _loaded_authorizer() -> Any:
    try:
        from superset_ownership.plugins import get_authorizer
    except ImportError:
        try:
            from superset_ownership.authz import get_authorizer
        except ImportError:
            return None
    try:
        return get_authorizer()
    except Exception:  # noqa: BLE001 - no authorizer resolvable; map as identity
        return None


def _map_ref(ref: str) -> str:
    """`ref`, translated into the loaded `Authorizer`'s store vocabulary
    through its `_subject_ref` hook, or unchanged when it has none (the
    default `OpenFGAAuthorizer`, a non-OpenFGA authorizer, or no authorizer
    at all -- this module used standalone)."""
    authorizer = _loaded_authorizer()
    hook = getattr(authorizer, "_subject_ref", None) if authorizer else None
    return hook(ref) if hook is not None else ref


def _unmap_ref(ref: str) -> str:
    """Inverse of `_map_ref`: a reference read back FROM the store, in OUR
    vocabulary, through `_from_subject_ref`."""
    authorizer = _loaded_authorizer()
    hook = getattr(authorizer, "_from_subject_ref", None) if authorizer else None
    return hook(ref) if hook is not None else ref


def _unmap_member_guid(raw_guid: str) -> str:
    """A member GUID read back from a `user:<...>` tuple's `user` field,
    unmapped from the authorizer's store vocabulary into ours."""
    mapped = _unmap_ref(f"user:{raw_guid}")
    return mapped.split(":", 1)[1] if mapped.startswith("user:") else mapped


def _display_name(user) -> str:
    """`identity.display_name(user)` when the Identity facade (spec section
    6, a sibling seam in this series) is on the branch; the same formula
    inline otherwise, so this module works standalone."""
    try:
        from superset_ownership import identity
    except ImportError:
        pass
    else:
        fn = getattr(identity, "display_name", None)
        if fn is not None:
            return fn(user)
    first = getattr(user, "first_name", "") or ""
    last = getattr(user, "last_name", "") or ""
    return f"{first} {last}".strip() or user.username


def _user_labels(
    guids: list[str],
) -> dict[str, tuple[str, Optional[str], Optional[int]]]:
    """GUID -> (display_name, email, superset_id), one `ab_user` query for
    the whole page. Today's `api.py` label map (moved here whole): keyed by
    username AND by the member GUID the username resolves to, so an
    upper-cased or `local-<id>` spelling still finds its row."""
    from superset import db, security_manager

    from superset_ownership import identity

    labels: dict[str, tuple[str, Optional[str], Optional[int]]] = {}
    if not guids:
        return labels
    user_model = security_manager.user_model
    for u in db.session.query(user_model).all():
        label = (_display_name(u), u.email, u.id)
        labels[u.username] = label
        member_guid = identity.resolve_member_guid(u)
        if member_guid:
            labels.setdefault(member_guid, label)
    return labels


class LocalDirectory:
    """FAB roles and users. The default `Directory` when
    `OWNERSHIP_AUTHORIZER=local`, and usable standalone (no OpenFGA)
    regardless of which authorizer is configured.

    Moved from `LocalAuthorizer.tenant_members` / `.group_exists` /
    `.groups_for_tenant`, reshaped to the protocol. `user_in_group` is
    duplicated rather than moved: it stays on `LocalAuthorizer` (D-1 --
    `is_tenant_administrator`'s live check needs it on `Authorizer`
    regardless of which `Directory` is configured), and
    `LocalDirectory.user_in_group` calls the same method so the two never
    disagree. Never raises: the store is the database Superset already has
    an open session to.
    """

    name = "local"
    protocol_version = 1

    def search_users(
        self, tenant: str, query: str, *, limit: int = 100, cursor: Optional[str] = None
    ) -> "Page[UserRef]":
        from superset import security_manager as sm

        from superset_ownership import identity

        q = (query or "").strip().lower()
        rows: list[UserRef] = []
        for user in sm.get_all_users():
            if identity.resolve_tenant_guid(user) != tenant:
                continue
            guid = identity.resolve_member_guid(user)
            if not guid:
                continue
            display_name = _display_name(user)
            if (
                q
                and q not in display_name.lower()
                and q not in guid.lower()
                and q not in (user.email or "").lower()
            ):
                continue
            rows.append(
                {
                    "guid": guid,
                    "display_name": display_name,
                    "email": user.email,
                    "superset_id": user.id,
                }
            )
        rows.sort(key=lambda r: r["guid"])
        offset = int(cursor) if cursor else 0
        page = rows[offset : offset + limit]
        next_cursor = str(offset + limit) if offset + limit < len(rows) else None
        return {"items": page, "next_cursor": next_cursor}

    def user_in_tenant(self, member_guid: str, tenant: str) -> bool:
        """H-2: the pre-split `LocalAuthorizer.tenant_members` answered
        `guid in tenant_members(t)`, and `tenant_members` was a scan --
        `resolve_member_guid(user)` for every tenant-scoped account, never a
        username lookup. `member_guid` here is `resolve_member_guid`'s own
        output (`local-<id>` for a plain username, the lower-cased GUID for
        a mixed-case one), neither of which is a username `find_user` could
        match; answering over the identity, the same scan `search_users`
        already makes, is parity with the method this replaced for every
        spelling, not only an account whose username already IS a GUID."""
        from superset import security_manager as sm

        from superset_ownership.identity import (
            resolve_member_guid,
            resolve_tenant_guid,
        )

        return any(
            resolve_tenant_guid(user) == tenant
            and resolve_member_guid(user) == member_guid
            for user in sm.get_all_users()
        )

    def list_groups(
        self, tenant: str, *, cursor: Optional[str] = None
    ) -> "Page[GroupRef]":
        from superset import security_manager as sm

        from superset_ownership.identity import (
            group_belongs_to_tenant,
            group_display_name,
            resolve_member_guid,
            resolve_tenant_guid,
            TENANT_ROLE_PREFIX,
        )

        found: dict[str, set[str]] = {}
        for user in sm.get_all_users():
            if resolve_tenant_guid(user) != tenant:
                continue
            guid = resolve_member_guid(user)
            for role in user.roles or []:
                name = getattr(role, "name", "") or ""
                if name == f"{TENANT_ROLE_PREFIX}{tenant}":
                    continue
                if guid and group_belongs_to_tenant(name, tenant):
                    found.setdefault(f"group:{name}", set()).add(guid)
        items: list[GroupRef] = [
            {
                "id": g,
                "display_name": group_display_name(g).replace("_", " "),
                "tenant": tenant,
                "members": len(m),
            }
            for g, m in sorted(found.items())
        ]
        return {"items": items, "next_cursor": None}

    def group_members(
        self, group_id: str, *, cursor: Optional[str] = None
    ) -> "Page[UserRef]":
        from superset import security_manager as sm

        from superset_ownership import identity

        name = group_id.split(":", 1)[-1].split("#", 1)[0]
        items: list[UserRef] = []
        for user in sm.get_all_users():
            if not any(getattr(r, "name", None) == name for r in (user.roles or [])):
                continue
            guid = identity.resolve_member_guid(user)
            if not guid:
                continue
            items.append(
                {
                    "guid": guid,
                    "display_name": _display_name(user),
                    "email": user.email,
                    "superset_id": user.id,
                }
            )
        return {"items": items, "next_cursor": None}

    def user_in_group(self, member_guid: str, group_id: str) -> bool:
        from superset_ownership.authz import LocalAuthorizer

        return LocalAuthorizer().user_in_group(member_guid, group_id)

    def group_exists(self, group_id: str) -> bool:
        from superset import security_manager as sm

        name = group_id.split(":", 1)[-1].split("#", 1)[0]
        return sm.find_role(name) is not None

    def tenant_administrators(self, tenant: str) -> list[UserRef]:
        from superset_ownership.identity import tenant_administrator_group

        group = tenant_administrator_group(tenant)
        page = self.group_members(group)
        return page["items"]

    def health(self) -> DirectoryHealth:
        return DirectoryHealth(ok=True, detail="FAB roles")


_DIRECTORIES: dict[str, "type | object"] = {
    "openfga": OpenFGADirectory,
    "local": LocalDirectory,
}
_instance: Optional[Directory] = None


def _as_int(v) -> int:
    return int(v)


def _configured_directory_name() -> str:
    """`OWNERSHIP_DIRECTORY` when set, else the authorizer's own alias --
    the default-directory rule (D-4): any authorizer spelling other than
    `local`/`openfga` requires `OWNERSHIP_DIRECTORY` to be set explicitly,
    which `plugins.load`'s registry enforces. This fallback, used only when
    no registry is loaded (a script or test importing this module
    directly), degrades to `local` for an unrecognised authorizer rather
    than raising, which is a narrower contract than the registry's --
    acceptable because it exists solely to keep this module importable on
    its own.

    I-8: this used to also fall back to `os.environ.get("OWNERSHIP_
    DIRECTORY")` between the `current_app.config` read above and the
    `authz._configured_name()` call below -- dead once `plugins.py`'s
    registry (which resolves `OWNERSHIP_DIRECTORY` through `settings.get`,
    config-then-environment-then-default, in ONE place) is the path every
    production call site actually takes; nothing calls this fallback
    accessor with an app registry loaded and no config value set but an
    environment variable present.
    """
    try:
        from flask import current_app

        name = current_app.config.get("OWNERSHIP_DIRECTORY")
        if name:
            return str(name).lower()
    except Exception as exc:  # noqa: BLE001 - outside an app context
        # Debug, not a warning: no app context (a script, an early import)
        # is the routine, expected reason this raises, not a fault.
        logger.debug(
            "superset_ownership: no app context for OWNERSHIP_DIRECTORY (%s)", exc
        )
    try:
        from superset_ownership.authz import _configured_name

        return _configured_name()
    except Exception:  # noqa: BLE001
        return "local"


def get_directory() -> Directory:
    """Lazy fallback accessor, mirroring `authz.get_authorizer`'s shape
    (module-level singleton, re-resolved on a name change) so a running
    process can switch backends the same way `authz.get_authorizer` does.
    Superseded by `superset_ownership.plugins.get_directory` whenever a
    registry is loaded; call sites try that import first and fall back to
    this one only when it is not.
    """
    global _instance
    name = _configured_directory_name()
    if _instance is None or getattr(_instance, "name", None) != name:
        cls = _DIRECTORIES.get(name)
        if cls is None:
            logger.warning(
                "superset_ownership: unknown directory %r, falling back to local", name
            )
            cls = LocalDirectory
        _instance = cls()  # type: ignore[operator]
        logger.info("superset_ownership: directory backend = %s", _instance.name)
    return _instance

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
REST API for the ownership prototype: /api/v1/ownership/*

Plain Flask Blueprint (registered via the BLUEPRINTS config hook) rather
than a flask_appbuilder ModelRestApi -- a FAB API needs its permission/view
rows synced to roles before anyone (even Admin) can call it, which only
happens on `superset init`. Auth instead mirrors flask_appbuilder's own
protect() fallback verbatim (session cookie first, then JWT bearer token
via flask_jwt_extended) -- see flask_appbuilder/security/decorators.py.
That's enough to populate flask_login's `current_user` / Superset's `g.user`
correctly for the rest of the security_manager machinery to work normally.

WHO MAY DO WHAT

Every write and the detail's `can_manage` go through one seam,
`_manage_reason`, which names the ground admitting the caller: `admin`,
`owner`, `tenant_admin`, or -- only when `OWNERSHIP_MANAGE_PERMISSION` names
a role -- `manage_permission`. `admin` and `owner` admit the caller to
every action on the object. `tenant_admin` is an OWNERSHIP ground: who owns
the object is the administrator's to decide, who sees it is the owner's
(`_sharing_ground`). `manage_permission` is a SHARING permission, not a
take-ownership one. The routes narrow both by reading the reason back:

    action                                  tenant_admin    manage_permission holder
    --------------------------------------  --------------  ------------------------------
    add / change / remove a share           no (403; take   yes, for a THIRD PARTY only
                                              ownership
                                              first)
    share to self, change own role,         no              no  (403)
      remove own share
    share to / unshare the tenant's own     no              no  (403; `tenant_<guid>` is the
      membership or administrator group                         whole tenant, not a third
                                                                party)
    visibility private <-> shared           no              yes
    visibility -> public                    no              no  (403; widens read for all)
    transfer to a validated third party     yes             yes (recipient baseline check
                                                                 as for any transfer)
    transfer to self ("Take ownership")     yes             no  (403)
    claim  (POST /claim)                    yes             no  (403)
    release (PUT /owner {"subject": null})  yes             no  (403)
    unowned / ownerless object              claim only      not admitted at all
    parked object                           not admitted    not admitted

A tenant administrator who ALSO holds the manage-sharing permission is
admitted to the sharing rows on the permission's ground (the opt-in wins;
see `_sharing_ground`).

and the seam itself admits the holder only to an object that is in their own
tenant, has a live owner, and that they can already see AND open: public or
listed for them by the store, with no revocation of theirs still queued, and
holding the object's dataset grant. `test_api.py` has one test per cell.

An object never moves between tenants, under ANY ground and on every path
that assigns an owner -- a transfer, a claim, and the visibility route's
adoption of an ownerless object: each refuses (409) a new owner whose own
tenant role is not the object's tenant, or who has no tenant role at all
(on the local backend an object's tenant IS its owner's, so a tenantless
owner would untenant it), and none re-stamps the object's tenant. Only
assigning an owner to an UNTENANTED object gives it one: the tenant of the
owner it is given. The tenant is read STRICTLY on those paths (503 when the
store cannot answer), because "no tenant" selects the stamp branch and
"could not read" must not.

What the matrix does NOT close is collusion between two people who each
hold a ground on the object: a holder can transfer to an accomplice who,
as owner, transfers back; two holders can share each other as editors.
Both are two attributable writes, each recorded with `admitted_by` and the
subject (`audit.py`, ADMITTED_BY), and are accepted as the cost of the
third-party transfer the permission exists to allow.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any, Optional

from superset_ownership.identity import GUID_RE

try:  # pragma: no cover - exercised once superset_ownership.plugins lands
    from superset_ownership.plugins import get_authorizer, get_directory, get_hook
except ImportError:  # this branch predates the plugins.py loader (spec section 4)
    from superset_ownership.authz import get_authorizer
    from superset_ownership.directory import get_directory

    def get_hook(name: str) -> None:  # noqa: ARG001 - predates plugin_hooks.py too
        return None


from flask import Blueprint, Response, jsonify, request
from flask_jwt_extended import verify_jwt_in_request
from flask_login import current_user
from sqlalchemy.exc import IntegrityError

from superset_ownership import audit, outbox, plugin_hooks, sentinel, service

logger = logging.getLogger(__name__)

ownership_bp = Blueprint("superset_ownership", __name__, url_prefix="/api/v1/ownership")

# Relations a share may grant. Anything else went into the authorization
# store verbatim -- a typo ("veiwer") wrote a tuple that granted nothing, and
# the request still answered 200, so the share looked applied and was not.
# A relation the model does not define is a 400, not a silent no-op.
VALID_SHARE_ROLES = frozenset({"viewer", "editor"})

# List pagination. A caller may ask for more, up to the cap.
DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 1000

VALID_VISIBILITIES = {"private", "shared", "public"}


def _display_visibility(row) -> str:
    """The visibility to SHOW. A parked row (`disabled:private`) shows as its
    underlying value so the UI does not render a raw state string as
    "Unknown", but it is reported alongside `disabled: true` and with
    can_manage/can_share false so nothing offers a control that the API
    would 409 anyway."""
    if row is None:
        return "public"
    v = str(row.visibility)
    return v[len("disabled:") :] if v.startswith("disabled:") else v


def _is_parked(row) -> bool:
    """A row disable() has parked. The feature is mid-rollback for this object;
    mutating it would fight enable() and re-arm what the operator stripped."""
    return row is not None and str(row.visibility).startswith("disabled:")


def _parked_error():
    return _err(
        409,
        "object ownership is disabled on this instance; run `superset ownership "
        "enable` before changing ownership or sharing",
    )


def _jwt_user():
    """The user a bearer token identifies, resolved from the token itself.

    Deliberately does NOT read flask_login's `current_user`. That proxy is
    populated for a bearer request only as a side effect of Flask-AppBuilder's
    own request loader, and it was observed returning an anonymous user for a
    token that verified cleanly -- for one account and not another of the same
    shape. An authorization module cannot be built on a signal that sometimes
    is not there: the identity claim is read straight from the verified token
    and the user looked up from it.

    Returns None when there is no valid token, or when it names an account
    that no longer exists or has been deactivated -- Ivanti soft-deletes
    users, so a token can outlive the account it was issued to.
    """
    from flask_jwt_extended import get_jwt_identity

    try:
        verify_jwt_in_request()
    except Exception:  # noqa: BLE001 - no/!valid token is simply "not authenticated"
        return None

    identity = get_jwt_identity()
    if identity is None:
        return None
    from superset import security_manager

    try:
        user = security_manager.get_user_by_id(int(identity))
    except (TypeError, ValueError):
        return None
    if user is None or not getattr(user, "is_active", False):
        return None
    return user


def _csrf_ok() -> bool:
    """Did this request carry a valid CSRF token?

    The blueprint is registered CSRF-exempt (it is not a flask_appbuilder API,
    so Flask-WTF's dotted-path exempt list cannot cover it), which means
    nothing validates the token for us. This does it explicitly, so a session
    cookie can be honoured on a state-changing route without that route being
    forgeable: a cross-site request carries the victim's cookie but cannot
    read the token.
    """
    from flask_wtf.csrf import validate_csrf

    token = (
        request.headers.get("X-CSRFToken")
        or request.headers.get("X-CSRF-Token")
        or request.form.get("csrf_token")
        or (request.get_json(silent=True) or {}).get("csrf_token")
    )
    if not token:
        return False
    try:
        validate_csrf(token)
        return True
    except Exception:  # noqa: BLE001 - any validation failure is a failure
        return False


def _authn(mutating: bool = False):
    """Authenticate the caller.

    A bearer token is always accepted. A mutating route ALSO accepts the
    cookie session, but only together with a valid CSRF token -- the pairing
    is what makes it unforgeable. Refusing the session outright was the safe
    half of that rule without the workable half: the sharing UI signs its
    requests with cookies and `X-CSRFToken`, never a JWT, so every write it
    made -- visibility, share, unshare, owner, claim -- answered 401 and the
    whole feature was read-only in a browser.
    """
    user = _jwt_user()
    if user is not None:
        return user
    if not current_user.is_authenticated:
        return None
    if mutating and not _csrf_ok():
        return None
    return current_user


# One definition of "a GUID v4", shared with the group id helpers so the
# member check and the tenant parse can never drift apart.
_GUID_RE = GUID_RE


def _validate_subject(
    subject: str, caller, store: bool = True
) -> tuple[Optional[bool], str]:
    """Is this a subject the caller may grant to?

    Two separate checks. The reference must be well formed -- an unvalidated
    string went straight into the authorization store before this existed.
    And it must belong to the caller's own tenant, so one tenant cannot grant
    access to another's members or groups.

    `store=False` answers from Superset's own records only: a user subject
    whose tenant only the authorization store can settle (an account whose
    role names another tenant, or none) is returned as (None, "") --
    undecided -- for the caller to ask again with the store once the
    answer may be given. The share route asks that way before its lock,
    and again after the caller is admitted: the member paging is one
    store round trip per page of the tenant, made in exactly one of the
    three cases, and its duration would tell a refused caller which case
    the subject fell in -- the bit the held verdict keeps from them.
    """
    if not subject or ":" not in subject:
        return False, "subject must be 'user:<guid>' or 'group:<name>#member'"

    kind, _, rest = subject.partition(":")
    from superset_ownership.identity import resolve_tenant_guid

    tenant = resolve_tenant_guid(caller)

    if kind == "user":
        return _validate_user_subject(subject, rest, tenant, store)

    if kind == "group":
        return _validate_group_subject(rest, tenant, store)

    return False, "subject must be a user or a group"


def _cannot_verify_tenant_membership(
    exc: BaseException, suffix: str = ""
) -> tuple[bool, str]:
    """M-1: a plugged Directory can raise anything, not only `StoreError`
    (its own HTTP/LDAP client's exception, a bug), and the client must
    never see that text verbatim -- it can carry a connection string or a
    bind password. Caught broadly by the callers below, logged here with a
    traceback, and reported as this fixed "cannot verify" answer either
    way."""
    from superset_ownership.fga import StoreUnavailable

    logger.exception(
        "_validate_subject: directory lookup failed (%s)", exc.__class__.__name__
    )
    why = (
        "is unavailable"
        if isinstance(exc, StoreUnavailable)
        else f"refused the lookup ({exc.__class__.__name__})"
    )
    return (
        False,
        f"cannot verify tenant membership: the authorization store {why}{suffix}",
    )


def _identity_lookup_failed(exc: BaseException) -> str:
    """I-2: M-1 applied to the Identity seam. A plugged `Identity`'s
    `member_guid`/`tenant_guid`/`user_for_member_guid`/`display_name`/
    `normalize_subject` can raise anything -- its own HTTP/LDAP client's
    exception, a permissions error from a lazily created table's DDL (the
    shipped example's reverse hook) -- and that text must never reach a
    client: it can carry a connection string or a bind password (the
    review's own probe used `RuntimeError("... password=hunter2")`).
    Logged here with a traceback, exactly as `_cannot_verify_tenant_
    membership` does for the Directory seam; every caller below builds its
    own fixed sentence from the classification this returns rather than
    the exception's own `str()`.

    R-1: `identity.py`'s `_default_normalize_subject` wraps a raising
    reverse hook in its own marker, `IdentityLookupError`, so the class
    name reaching a caller here is not always `exc`'s own -- unwrap it
    first so the sentence still names the ORIGINAL failure (e.g.
    `RuntimeError`), never the wrapper's name."""
    from superset_ownership.fga import StoreUnavailable
    from superset_ownership.identity import IdentityLookupError

    logger.exception("identity plug-in lookup failed (%s)", exc.__class__.__name__)
    if isinstance(exc, IdentityLookupError):
        return f"refused the lookup ({exc.cls_name})"
    return (
        "is unavailable"
        if isinstance(exc, StoreUnavailable)
        else f"refused the lookup ({exc.__class__.__name__})"
    )


def _confirm_tenant_membership(
    guid: str, tenant: str, store: bool, *, suffix: str = ""
) -> tuple[Optional[bool], str]:
    """The one strict `Directory.user_in_tenant` read `_validate_user_subject`
    needs, in either direction: `store=False` answers undecided (`None, ""`)
    without touching the directory; otherwise the store's own verdict, or
    M-1's fixed "cannot verify" answer on any exception."""
    if not store:
        return None, ""
    try:
        if get_directory().user_in_tenant(guid, tenant):
            return True, ""
    except Exception as exc:  # noqa: BLE001 - classified above (M-1)
        return _cannot_verify_tenant_membership(exc, suffix)
    return False, "subject is not a member of your tenant"


def _validate_user_subject(
    subject: str, rest: str, tenant: Optional[str], store: bool
) -> tuple[Optional[bool], str]:
    """The `user:` half of `_validate_subject`.

    I-2: every call into the plugged Identity seam this makes
    (`normalize_subject`, `user_for_member_guid`, the `member_guid`/
    `tenant_guid` reads below) is covered by ONE guard around the whole
    body -- the Directory seam's calls this function reaches
    (`_confirm_tenant_membership`) already classify their own exceptions
    and never raise outward, so nothing here needs a second, narrower
    guard around each identity call individually. `store=False` (the share
    route's pre-403 probe) answers undecided on a raise, exactly as it
    already does for a store-settled tenant question -- a refused caller
    must not learn anything from an identity plug-in's failure that the
    held verdict would otherwise keep from them; `store=True` answers the
    fixed "cannot verify" sentence, never the plug-in's own text.
    """
    try:
        return _validate_user_subject_unguarded(subject, rest, tenant, store)
    except Exception as exc:  # noqa: BLE001 - I-2: identity seam (M-1)
        why = _identity_lookup_failed(exc)
        if not store:
            return None, ""
        return False, f"cannot verify the subject: the identity plug-in {why}"


def _validate_user_subject_unguarded(
    subject: str, rest: str, tenant: Optional[str], store: bool
) -> tuple[Optional[bool], str]:
    """`_validate_user_subject`'s actual logic, wrapped by its caller's
    single M-1/I-2 guard -- see that function's docstring."""
    ref = rest.split("#", 1)[0]
    # A reference has to name a real account, whichever identifier it uses.
    # Requiring a GUID here made the feature unusable on any instance whose
    # usernames are not GUIDs -- including this repo's own demo users --
    # while a GUID that names nobody still passed.
    from superset_ownership.identity import (
        normalize_subject,
        resolve_member_guid,
        resolve_tenant_guid,
        user_for_member_guid,
    )

    canonical = normalize_subject(subject)
    if canonical is None:
        return False, "subject does not name a known account"
    if not tenant:
        if not _GUID_RE.fullmatch(ref) and not ref.isdigit() and "@" not in ref:
            return (
                False,
                "user subject must be a GUID v4, a Superset user id or an email",
            )
        return True, ""

    guid = canonical.split(":", 1)[1]
    # Membership is checked against Superset's OWN record first: a member's
    # tenant is their `tenant_<guid>` role (the same signal resolve_tenant_
    # guid reads for the caller). That keeps sharing to a known account
    # working when the authorization store is unreachable -- the write
    # itself is deferred via the outbox, so this read was the last thing
    # tying the request to the store. Only a subject with no Superset
    # account (a directory-only member) still needs the store to answer.
    #
    # H-3: the reverse lookup goes through the identity seam's
    # `user_for_member_guid`, not `find_user(username=guid)` -- `guid` is
    # `resolve_member_guid`'s output, which is the username ONLY for the
    # default identity's happy path (H-2's exact bug, for the same reason,
    # on the Local backend).
    target = user_for_member_guid(guid)
    if target is not None:
        # N-2: trust the hook's answer only when the forward mapping
        # agrees -- an inconsistent hook (a `.first()` over a stale or
        # duplicated attribute query is the obvious real-world bug) must
        # not admit whatever GUID was submitted just because it returned
        # *a* user. Fall through to the store's verdict exactly as when
        # the hook found nobody; contract section 5 puts the tenant
        # boundary in OUR code "regardless of which class answered".
        found_guid = resolve_member_guid(target)
        if found_guid is None or found_guid.lower() != guid.lower():
            target = None
    if target is None:
        return _confirm_tenant_membership(guid, tenant, store)

    if not getattr(target, "is_active", False):
        return False, "subject's account is deactivated"
    if resolve_tenant_guid(target) == tenant:
        return True, ""
    # The role names one tenant; a member of several may carry a different
    # one first. Let the store settle it when it can. A strict read so that
    # a store that cannot answer (down, or a 5xx from its balancer) is
    # reported as exactly that, not as "not a member".
    return _confirm_tenant_membership(
        guid, tenant, store, suffix=" and the account's role names another tenant"
    )


def _group_tenant_from_store(name: str) -> Optional[str]:
    """`OpenFGADirectory.group_tenant(name)` when the active Directory
    resolved to that class, else `None` -- M-6's call site treats `None` as
    "no independent store fact to check the hook's parse against" and falls
    back to the id-format round-trip invariant instead. A store failure is
    treated the same way (never a 500 from this confirmation step;
    `group_exists`, called right after this returns, is the check that
    still fails closed on a genuine store outage).

    Checked by `isinstance`, deliberately NOT `getattr(..., "group_tenant",
    None)`: a plain `unittest.mock.Mock()` (what most of this module's own
    tests hand `get_directory()`) answers ANY attribute access via its own
    `__getattr__`, so a duck-typed probe would "find" `group_tenant` on
    every mocked directory and call it, breaking every existing group-share
    test that never configured one.
    """
    from superset_ownership.directory import OpenFGADirectory
    from superset_ownership.plugins import HookedDirectory

    directory = get_directory()
    raw = directory._unwrap() if isinstance(directory, HookedDirectory) else directory  # noqa: SLF001
    if not isinstance(raw, OpenFGADirectory):
        return None
    try:
        return raw.group_tenant(name)
    except Exception:  # noqa: BLE001 - "cannot confirm" here, not a 500
        logger.debug(
            "_validate_subject: could not confirm group tenancy via the store",
            exc_info=True,
        )
        return None


def _validate_group_subject(
    rest: str, tenant: Optional[str], store: bool
) -> tuple[Optional[bool], str]:
    """The `group:` half of `_validate_subject`."""
    name = rest.split("#", 1)[0]
    # The tenant is part of the group id, in whichever position
    # OWNERSHIP_GROUP_ID_FORMAT puts it; the helper is the only thing that
    # knows which.
    from superset_ownership.identity import group_belongs_to_tenant

    if tenant and not group_belongs_to_tenant(name, tenant):
        return False, "group does not belong to your tenant"

    # L-5: the user branch above honours `store=False` (returns the
    # undecided `(None, "")` before touching the directory); the group
    # branch used to read the directory unconditionally, so a refused
    # caller's pre-403 probe (`api.py`'s share route, `store=False`) still
    # triggered a directory round trip -- and, before M-1, could turn into
    # a 500 -- for a group of their OWN tenant. The M-6 store confirmation
    # below is itself a store read, so it stays behind this same gate.
    if not store:
        return None, ""

    # M-6: the check above trusts a configured `OWNERSHIP_SPLIT_GROUP_ID`
    # hook's own parse of `name` -- a hook that lies (always answers the
    # caller's own tenant, regardless of which group was actually asked
    # about) would otherwise admit a share to a foreign tenant's group.
    # Re-confirmed against an INDEPENDENT store fact when the backend has
    # one (`Directory.group_tenant`, best-effort/not part of the protocol
    # -- only `OpenFGADirectory` defines it, when the model records
    # `group.tenant` as a tuple); when no backend fact is available (the
    # `local` backend, where a group's tenant IS its id format and there is
    # nothing else to check it against, or an OpenFGA model that does not
    # record the relation at all), fall back to asserting the hook's OWN
    # parse is at least self-consistent -- `group_id(*split_group_id(id))
    # == id` -- which still catches a broken (not merely lying) hook.
    if tenant:
        store_tenant = _group_tenant_from_store(name)
        if store_tenant is not None:
            if store_tenant.lower() != tenant.lower():
                return False, "group does not belong to your tenant"
        else:
            from superset_ownership.identity import group_id, split_group_id

            parsed = split_group_id(name)
            if parsed is None or group_id(*parsed) != name:
                return False, "group does not belong to your tenant"

    # The group has to exist. Without a caller tenant -- which is every
    # Superset admin -- this branch returned True for anything, so
    # "group:totally-fake#member" was accepted, mirrored, listed in the
    # drawer and written to the store, granting nobody anything. That is
    # the unvalidated string this function exists to stop.
    try:
        exists = get_directory().group_exists(name)
    except Exception as exc:  # noqa: BLE001 - M-1: any plug-in failure, not just StoreError
        from superset_ownership.fga import StoreUnavailable

        logger.exception(
            "_validate_subject: directory group lookup failed (%s)",
            exc.__class__.__name__,
        )
        why = (
            "is unavailable"
            if isinstance(exc, StoreUnavailable)
            else f"refused the lookup ({exc.__class__.__name__})"
        )
        return False, f"cannot verify the group: the authorization store {why}"
    if not exists:
        return False, "no such group in the authorization store"
    return True, ""


def _err(status: int, msg: str):
    return jsonify({"message": msg}), status


def _get_dashboard(pk: int):
    from superset import db
    from superset.models.dashboard import Dashboard

    return db.session.get(Dashboard, pk)


def _get_chart(pk: int):
    from superset import db
    from superset.models.slice import Slice

    return db.session.get(Slice, pk)


# asset_type -> (model loader, not-found message, ORM model class for list queries)
_ASSET_LOADERS = {
    "dashboard": (_get_dashboard, "dashboard not found"),
    "chart": (_get_chart, "chart not found"),
}


def _get_asset(asset_type: str, pk: int):
    loader, _ = _ASSET_LOADERS[asset_type]
    return loader(pk)


def _not_found_msg(asset_type: str) -> str:
    return _ASSET_LOADERS[asset_type][1]


def is_tenant_administrator(user) -> bool:
    """Does this user administer their own tenant?

    Ivanti's tenant administrators are a role in their directory, not FAB
    Admin. Checking `is_admin()` alone would mean a tenant administrator has
    no elevated rights anywhere in this module, which is what the reviewer
    found: nobody could reassign an object whose owner had left.

    `OWNERSHIP_IS_TENANT_ADMINISTRATOR` (spec §4.5.2), when configured,
    answers this question directly and wins outright (§4.5.1 item 3's
    precedence -- a hook over the class-seam method). Otherwise membership
    is read through the `Directory` seam (`get_directory().user_in_group`,
    which itself consults `OWNERSHIP_USER_IN_GROUP` first) rather than the
    `Authorizer` directly -- the directory stays the source of truth for
    "who is in which group" everywhere else in this module.
    """
    if user is None:
        return False
    from superset_ownership.identity import (
        resolve_member_guid,
        resolve_tenant_guid,
        tenant_administrator_group,
    )

    # I-2: an elevated-permission check fails CLOSED on an identity seam
    # failure (not "cannot verify" -- there is no caller-facing subject to
    # answer that way here, only a yes/no on the CALLER's own standing),
    # consistent with the rest of this module's fail-closed rule; logged
    # with a traceback so an operator sees the plug-in error rather than a
    # silently-denied elevation.
    try:
        tenant = resolve_tenant_guid(user)
        guid = resolve_member_guid(user)
    except Exception as exc:  # noqa: BLE001 - I-2: identity seam (M-1)
        _identity_lookup_failed(exc)
        return False

    hook = get_hook("OWNERSHIP_IS_TENANT_ADMINISTRATOR")
    if hook is not None:
        return plugin_hooks.call(
            hook,
            "OWNERSHIP_IS_TENANT_ADMINISTRATOR",
            user,
            cache_key=getattr(user, "id", None),
        )
    if not tenant or not guid:
        return False
    return get_directory().user_in_group(guid, tenant_administrator_group(tenant))


# Why a caller may manage an object, as the detail reports it. The client
# shows different copy after a transfer depending on which of these still
# holds: an owner who handed the object over no longer manages it, but an
# administrator does, and the drawer says so rather than leaving live
# controls unexplained.
MANAGE_REASON_ADMIN = "admin"
MANAGE_REASON_OWNER = "owner"
MANAGE_REASON_TENANT_ADMIN = "tenant_admin"
MANAGE_REASON_MANAGE_PERMISSION = "manage_permission"

# The config key naming the optional manage-sharing role (spec section 14.5).
MANAGE_PERMISSION_CONFIG_KEY = "OWNERSHIP_MANAGE_PERMISSION"

# The one placeholder a manage-sharing role name may carry (issue #83):
# substituted with the CALLER's own tenant GUID by `holds_manage_permission`,
# never with anyone else's, so a single setting can name a per-tenant role
# ("sharing_manager_{tenant}") that admits exactly that tenant's holders --
# Ivanti provisions "sharing manager" as one directory group per tenant, the
# same way every other JIT role/group is tenant-suffixed, and one literal
# value cannot serve more than one tenant.
MANAGE_PERMISSION_TENANT_PLACEHOLDER = "{tenant}"

# A GUID v4 substituted for `{tenant}` ONLY to probe, at parse time, whether
# a template would render to a structural role name for some tenant -- the
# shape check (`identity.is_structural_tenant_role`) does not depend on
# which GUID is used, so any well-formed one will do. Never compared against
# a real account; `holds_manage_permission` substitutes the caller's own
# tenant at request time instead.
_MANAGE_PERMISSION_PROBE_TENANT = "00000000-0000-4000-8000-000000000000"

# Role names the setting refuses. Flask-AppBuilder's built-ins are held by
# whole populations (`Gamma` is every ordinary user; `Public` is everyone),
# and the two structural tenant roles are populations too: `tenant_<guid>`
# is membership of a tenant, `tenant_administrator_<guid>` its
# administrators (`identity.is_structural_tenant_role`, anchored). Naming
# one of these would hand the permission to a population, which is the one
# operator mistake the seam makes catastrophic: every member could unshare
# everyone else and re-share the tenant's objects. A templated name
# (`tenant_{tenant}`, `tenant_administrator_{tenant}`) is refused the same
# way, checked against the PROBE tenant above -- the shape is structural or
# it is not, for every tenant alike.
#
# Any OTHER name carrying a tenant GUID -- `sharing_manager_<guid>`, how a
# directory group is spelled as a role by Ivanti's just-in-time path -- is
# an ordinary role and is accepted: the seam scopes every holder to their
# own tenant regardless, so a per-tenant role admits exactly what a global
# one would, to that tenant's members. (One setting names one role, so a
# per-tenant name serves one tenant unless it carries the `{tenant}`
# placeholder above, issue #83.)
STRUCTURAL_ROLE_NAMES = frozenset({"Admin", "Alpha", "Gamma", "Public", "sql_lab"})

# Config values already warned about, so a bad setting is logged once per
# process and not on every request that reads it.
_WARNED_CONFIG_VALUES: set[str] = set()


def _warn_config_once(raw: Any, why: str) -> None:
    key = repr(raw)
    if key in _WARNED_CONFIG_VALUES:
        return
    _WARNED_CONFIG_VALUES.add(key)
    logger.warning(
        "superset_ownership: %s %s, got %r; the manage-sharing permission is off",
        MANAGE_PERMISSION_CONFIG_KEY,
        why,
        raw,
    )


def parse_manage_permission(raw: Any) -> str | None:
    """The role name `OWNERSHIP_MANAGE_PERMISSION` configures, or None for off.

    None, an empty string and whitespace all mean "no such permission"; a
    name is returned stripped. The value is a Flask-AppBuilder ROLE name, so
    anything that is not a string -- a `(permission, view)` tuple set by
    someone expecting a FAB permission -- is refused with a warning rather
    than stringified into a name no role will ever match, which would look
    like a permission that is on and admits nobody.

    A built-in or structural role name (`STRUCTURAL_ROLE_NAMES`, or exactly
    `tenant_<guid>` / `tenant_administrator_<guid>`) is refused the same
    way: the permission must be a dedicated role, never a population. A
    dedicated per-tenant role (`sharing_manager_<guid>`) is accepted.

    The name may instead carry the `{tenant}` placeholder exactly once
    (issue #83), e.g. `sharing_manager_{tenant}` -- `holds_manage_permission`
    substitutes the CALLER's own tenant GUID into it before matching. Any
    brace at all routes the value through this template check rather than
    the literal path below: a value with `{tenant}` appearing more than
    once, any other `{...}` placeholder alongside it, or a brace with no
    well-formed `{tenant}` in it at all (a typo -- `{Tenant}`, `{TENANT}`,
    `{ tenant }`, `{tenant_guid}` -- or a stray `{}`) is refused rather than
    silently accepted as a literal role name that happens to contain braces
    and so matches nobody. The bare placeholder alone (`{tenant}`, no
    surrounding text) is refused too: it names a tenant, not a role, and
    would render to nothing but a GUID. A template that would render to a
    structural role name for ANY tenant (`tenant_{tenant}`,
    `tenant_administrator_{tenant}`) is refused the same way a literal
    structural name is, checked once against a probe GUID -- the shape does
    not depend on which tenant fills it in.

    Each refusal is logged once.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        _warn_config_once(raw, "must be a role name (a string)")
        return None
    name = raw.strip()
    if not name:
        return None
    from superset_ownership.identity import is_structural_tenant_role

    if "{" in name or "}" in name:
        if name.count(MANAGE_PERMISSION_TENANT_PLACEHOLDER) != 1:
            _warn_config_once(raw, "must contain the {tenant} placeholder exactly once")
            return None
        remainder = name.replace(MANAGE_PERMISSION_TENANT_PLACEHOLDER, "", 1)
        if "{" in remainder or "}" in remainder:
            _warn_config_once(
                raw, "may not contain any placeholder other than {tenant}"
            )
            return None
        if name == MANAGE_PERMISSION_TENANT_PLACEHOLDER:
            _warn_config_once(raw, "must name a role, not just a tenant")
            return None
        rendered = name.replace(
            MANAGE_PERMISSION_TENANT_PLACEHOLDER, _MANAGE_PERMISSION_PROBE_TENANT
        )
        if is_structural_tenant_role(rendered):
            _warn_config_once(
                raw,
                "must name a dedicated role, not a built-in or a tenant's "
                "membership / administrator role",
            )
            return None
        return name

    if name in STRUCTURAL_ROLE_NAMES or is_structural_tenant_role(name):
        _warn_config_once(
            raw,
            "must name a dedicated role, not a built-in or a tenant's "
            "membership / administrator role",
        )
        return None
    return name


def manage_permission_role() -> str | None:
    """The configured manage-sharing role name, or None when the seam is off.

    Flask config first (like OWNERSHIP_AUTHORIZER and the outbox switch),
    then the environment, default off -- so an instance that has not decided
    the question behaves exactly as before: the owner, a tenant administrator
    and a Superset admin manage an object and nobody else.

    A ROLE name rather than a `(permission, view)` pair on purpose. Ivanti's
    tenant roles arrive in Superset as FAB roles provisioned just in time
    from their directory, so "who holds this" is a question their tooling
    already answers by putting a member in a role; a FAB permission would
    need a view-menu row synced on `superset init` and a role to carry it
    anyway. The role need not exist for the setting to be valid: a name no
    account holds simply admits nobody.
    """
    from superset_ownership import settings

    raw = settings.get(MANAGE_PERMISSION_CONFIG_KEY, None, settings.as_str)
    return parse_manage_permission(raw)


def holds_manage_permission(user) -> bool:
    """Does this user hold the configured manage-sharing role?

    A literal role name is matched exactly against the user's role names --
    the same place `resolve_tenant_guid` reads tenancy from -- so holding it
    costs no store call. False whenever the seam is off, whatever roles the
    user has.

    A templated name (`{tenant}` in the configured role, issue #83) is
    matched instead against the template rendered with the CALLER's OWN
    tenant GUID -- never anyone else's -- so a member of tenant A holds it
    only through that tenant's rendering of the template. A caller with no
    tenant (`resolve_tenant_guid` returns None) never holds a templated
    permission. Only the GUID segment the placeholder renders to is folded
    case-insensitively, the same way the tenant role match
    `resolve_tenant_guid` itself relies on does -- the GUID a directory
    writes into a role name may not be cased the way `resolve_tenant_guid`
    reports it. The literal text around the placeholder is matched exactly,
    like the literal (non-templated) form above: `SHARING_MANAGER_{tenant}`
    does not match a role spelled `sharing_manager_<guid>`.
    """
    role = manage_permission_role()
    if not role or user is None:
        return False
    roles = getattr(user, "roles", None) or []
    if MANAGE_PERMISSION_TENANT_PLACEHOLDER in role:
        from superset_ownership.identity import resolve_tenant_guid

        caller_tenant = resolve_tenant_guid(user)
        if not caller_tenant:
            return False
        prefix, suffix = role.split(MANAGE_PERMISSION_TENANT_PLACEHOLDER, 1)
        want = caller_tenant.lower()

        def _renders_for_caller(name: str) -> bool:
            if len(name) < len(prefix) + len(suffix):
                return False
            if not (name.startswith(prefix) and name.endswith(suffix)):
                return False
            return name[len(prefix) : len(name) - len(suffix)].lower() == want

        return any(_renders_for_caller(getattr(r, "name", None) or "") for r in roles)
    return any((getattr(r, "name", None) or "") == role for r in roles)


def _manage_reason(row, user) -> str | None:
    """The first ground on which this caller may manage the object, or None
    -- see `_default_manage_reason` for the rule itself.

    Wraps the default with `OWNERSHIP_CAN_MANAGE` (spec §4.5.2), when
    configured: the hook receives `user` and an `object_state` dict (the
    shape the detail endpoint reports, plus `default_reason`) and may only
    NARROW the default -- `None`, or the same reason back -- never grant a
    reason the default did not (`plugin_hooks.narrow_manage_reason`
    logs and ignores a widening attempt, per §5's non-overridable
    invariant). This is the single authorization seam for the four writes
    (share, unshare, visibility, owner/claim) and for the `can_manage` the
    detail reports; the list-page's own per-row summary (`_list_assets`)
    consults the same hook separately, for its own bulk-computed default.
    """
    default_reason = _default_manage_reason(row, user)
    hook = get_hook("OWNERSHIP_CAN_MANAGE")
    if hook is None:
        return default_reason
    object_state = _can_manage_object_state(row, user, default_reason)
    hook_reason = plugin_hooks.call(hook, "OWNERSHIP_CAN_MANAGE", user, object_state)
    return plugin_hooks.narrow_manage_reason(hook_reason, default_reason)


_UNSET: Any = object()


def _can_manage_object_state(
    row,
    user,
    default_reason: str | None,
    *,
    tenant: Any = _UNSET,
    shares: Any = _UNSET,
) -> dict[str, Any]:
    """The dict `OWNERSHIP_CAN_MANAGE` receives: `asset_type`, `object_id`,
    `object_uuid`, `owner` (the Superset user id), `tenant`, `visibility`,
    `shares` and `default_reason` (§4.5.2 -- amended to list these exact
    keys, M-3: not literally the detail endpoint's own response dict, which
    also carries `can_manage`/`can_share`/`unowned`/`manage_reason` that are
    this function's OUTPUTS, never its input).

    `tenant`/`shares` are looked up here (one store call, one query) ONLY
    when the caller (`_manage_reason`, the single-object write/detail path)
    does not already have them; the list page (`_list_row_manage_reason`)
    passes both in, computed ONCE for the whole page, to avoid an N+1 (M-3).
    """
    if tenant is _UNSET:
        tenant = None
        if row and row.object_uuid:
            try:
                tenant = get_authorizer().object_tenant(row.asset_type, row.object_uuid)
            except Exception:  # noqa: BLE001 - a tenant lookup failure here must not crash the manage decision
                tenant = None
    if shares is _UNSET:
        shares = service.list_shares(row.asset_type, row.object_id) if row else []
    return {
        "asset_type": row.asset_type if row else None,
        "object_id": row.object_id if row else None,
        "object_uuid": row.object_uuid if row else None,
        "owner": row.owner_user_id if row else None,
        "tenant": tenant,
        "visibility": row.visibility if row else None,
        "shares": shares,
        "default_reason": default_reason,
    }


def _default_manage_reason(row, user) -> str | None:
    """The first ground on which this caller may manage the object, or None.

    Checked in order of breadth: a Superset admin manages everything, an
    owner manages their own object, a tenant administrator manages their own
    tenant's objects (transfer and claim only -- a sharing write under that
    ground is refused by `_sharing_ground`; the administrator takes
    ownership first, unless they also hold the manage-sharing permission), and -- only when `OWNERSHIP_MANAGE_PERMISSION`
    names a role -- a holder of that role manages the sharing of the owned
    objects of their own tenant that they can already see. The order matters only
    to the reason reported -- an admin who also owns the object is reported
    as an admin, a tenant administrator who also holds the manage role is
    reported as a tenant administrator -- never to whether access is
    granted.

    This is the single authorization seam for the four writes (share,
    unshare, visibility, owner/claim) and for the `can_manage` the detail
    reports. The routes read the REASON, not a boolean: the first three
    grounds admit every action, while `manage_permission` admits only the
    sharing actions (the matrix in the module docstring), and each route
    refuses the rest under that ground. It is NOT consulted by anything that
    decides whether an object can be OPENED or LISTED
    (`hooks.raise_for_access_bypass`, the list filters): managing an
    object's sharing and reading the object are different questions, and a
    rule added here widens only the first.
    """
    from superset import security_manager

    if security_manager.is_admin():
        return MANAGE_REASON_ADMIN
    if row and user is not None and row.owner_user_id == user.id:
        return MANAGE_REASON_OWNER

    # A tenant administrator may manage objects in their OWN tenant only.
    # Checking the administrator role without checking the object's tenant
    # would let one tenant's administrator seize and re-share another
    # tenant's objects -- which, where tenants share datasets and are
    # separated by row-level security, is a data-exposure path rather than
    # a control-plane annoyance.
    if is_tenant_administrator(user):
        reason = _tenant_admin_reason(row, user)
        if reason is not None:
            return reason

    return _manage_permission_reason(row, user)


def _tenant_admin_reason(row, user) -> str | None:
    """`MANAGE_REASON_TENANT_ADMIN` if a tenant administrator may manage the
    object, else None. The caller has already established that `user`
    administers a tenant."""
    from superset_ownership.identity import resolve_tenant_guid

    caller_tenant = resolve_tenant_guid(user)
    if not caller_tenant:
        return None
    obj_tenant = (
        get_authorizer().object_tenant(row.asset_type, row.object_uuid)
        if row and row.object_uuid
        else None
    )
    if obj_tenant:
        # The object belongs to a tenant: only that tenant's administrator.
        return MANAGE_REASON_TENANT_ADMIN if obj_tenant == caller_tenant else None

    # No tenant on the object. A tenant administrator may take it IF it is
    # unowned -- no owner, or the owner's account is deactivated -- so that an
    # object nobody owns can be rescued: the administrator sees it, assigns an
    # owner, and it becomes their tenant's. An untenanted object that still has
    # a live owner is that owner's (or a Superset admin's), not a tenant
    # administrator's to seize.
    return MANAGE_REASON_TENANT_ADMIN if _is_claimable(row) else None


def _manage_permission_reason(row, user) -> str | None:
    """`MANAGE_REASON_MANAGE_PERMISSION` if the optional manage-sharing role
    admits this caller to this object, else None.

    Four conditions, all required, cheapest first:

      1. The seam is on and the caller holds the configured role. Off is the
         default and costs nothing beyond a config read: no role lookup, no
         tenant resolution, no store call. A TEMPLATED role is not free the
         same way: `holds_manage_permission` resolves the caller's tenant to
         render it, so this ground alone costs one `resolve_tenant_guid`
         call, and ground 4 below resolves it again -- `resolve_tenant_guid`
         is cheap on the default identity (a role scan) but a custom
         `Identity` that does real work for it pays that cost twice.
      2. The object has an owner recorded. An object with no owner has
         nobody whose sharing there is to manage: a role that manages
         sharing is not a role that adopts strays, and the visibility
         route's ownerless branch (which hands the object to whoever makes
         it non-public) is a tenant administrator's rescue path, not a
         holder's. Parked rows are refused by the routes before the seam.
      3. The caller can ALREADY see the object (`_listed_for`): it is
         public, or the store lists them as its owner or a share recipient.
         Asked BEFORE the tenant read because it is the cheaper refusal: the
         detail route has already made this read for its own 404 decision
         (`_reachable_ids` is memoised for the request), so a private object
         never shared with the holder -- the commonest refusal -- costs no
         `object_tenant` round-trip, and a public object costs nothing here
         at all.
      4. The object is in the caller's OWN tenant, by the same test the
         tenant-administrator branch applies: the caller's tenant from their
         `tenant_<guid>` role, the object's from the authorizer (which, on
         the local backend, is the owner's tenant). An object with no
         tenant, or a caller with none, is refused -- there is no rescue
         path here as there is for a tenant administrator. A member of more
         than one tenant is scoped to the first `tenant_<guid>` role on
         their account, as everywhere else in this module.
      5. The caller can already OPEN it (`_already_opens`): no revocation of
         theirs still queued, a live owner, and the object's dataset grant,
         the grant asked last. Together with 3 this is what keeps the
         permission from widening read access. Without it the detail route
         would disclose a private object's owner and share list to anyone
         holding the role, and a holder could share an object they had
         never been given. Sharing to THEMSELVES is refused by the share and
         owner routes whatever they already hold.
    """
    if not holds_manage_permission(user):
        return None
    if row is None or not row.object_uuid or not _is_ownable(row):
        return None

    from superset_ownership.identity import resolve_tenant_guid

    caller_tenant = resolve_tenant_guid(user)
    if not caller_tenant:
        return None
    if not _listed_for(row, user):
        return None
    obj_tenant = get_authorizer().object_tenant(row.asset_type, row.object_uuid)
    if not obj_tenant or obj_tenant != caller_tenant:
        return None
    if not _already_opens(row, user):
        return None
    return MANAGE_REASON_MANAGE_PERMISSION


def _reachable_ids(asset_type: str, user_id: int) -> set[int]:
    """`service.owned_or_shared_object_ids`, read at most once per request.

    On the OpenFGA backend that read is a `list_objects` round-trip. The
    detail route needs the answer for its own 404 decision and, with the
    seam on, the seam needs it too for the same caller and asset type; one
    request must not pay for it twice. Memoised on `flask.g`, so it lives
    exactly as long as the request; with no app context (CLI, tests) it is
    simply read.
    """
    memo: dict | None = None
    try:
        from flask import g, has_app_context

        if has_app_context():
            memo = getattr(g, "_ownership_reach", None)
            if memo is None:
                memo = {}
                g._ownership_reach = memo
    except Exception:  # noqa: BLE001 - no Flask, or a torn-down context
        memo = None
    key = (asset_type, user_id)
    if memo is None or key not in memo:
        ids = set(service.owned_or_shared_object_ids(asset_type, user_id))
        if memo is None:
            return ids
        memo[key] = ids
    # A copy: callers build on the answer in place.
    return set(memo[key])


def _listed_for(row, user) -> bool:
    """Could this caller SEE the object without the manage-sharing role?

    Public, or listed for them by the store as owner or share recipient --
    the test `_get_asset_detail` applies before disclosing a non-public
    object's metadata, read through `_reachable_ids` so that the detail
    route and the seam share one read per request.
    """
    if _display_visibility(row) == "public":
        # Public within the tenant: the other tenant's members are not
        # "listed for" a public object, and get the same 404 as for a
        # private one -- the metadata is not theirs to read either.
        if service.public_is_tenant_scoped() and not service.public_row_visible_to(
            row, user
        ):
            return row.object_id in _reachable_ids(row.asset_type, user.id)
        return True
    return row.object_id in _reachable_ids(row.asset_type, user.id)


def _already_opens(row, user) -> bool:
    """Could this caller OPEN the object without the manage-sharing role?
    Asked only once `_listed_for` has passed.

    Each test is one the read path (`hooks.raise_for_access_bypass`) also
    applies, so a reason granted here never lets a holder manage an object
    they could not have reached; the ORDER is this seam's own, cheapest
    refusal first, and one rule is stricter than the read gate:

      * no revocation of theirs still queued in the outbox
        (`outbox.has_pending_revocation`, the deny the read gate applies
        BEFORE asking the store, because the store keeps granting until the
        drain delivers the delete). Without it a holder whose share was
        just revoked could re-grant it inside the drain window. Asked for a
        shared object only: public is not reached through a share;
      * the owner is a live account (`is_unowned`). For a shared object the
        read gate requires this before any share confers access. For a
        PUBLIC object the read gate opens it at the visibility test and
        never asks -- so refusing it here is this module's rule, not the
        read gate's: an object whose owner is gone falls to administrators
        (as `can_share` reports it), and is not a holder's to re-share;
      * the dataset grant (`base_permission_holds_for`, evaluated for the
        caller as the transfer check evaluates it for a recipient), the
        first thing the read gate asserts on a governed object and the
        dearest check here, so asked last.
    """
    if _display_visibility(row) != "public":
        from superset_ownership.identity import member_ref

        subject = member_ref(user.id)
        if subject is None:
            return False
        if outbox.has_pending_revocation(
            f"{row.asset_type}:{row.object_uuid}", subject, object_id=row.object_id
        ):
            return False
    if _is_unowned(row):
        return False
    from superset_ownership.hooks import base_permission_holds_for

    asset = _get_asset(row.asset_type, row.object_id)
    return asset is not None and base_permission_holds_for(user, asset, row.asset_type)


def _is_claimable(row) -> bool:
    """Unowned enough that a tenant administrator may take it: no ownership
    row at all, no owner recorded, or a recorded owner whose account is gone."""
    if row is None:
        return True
    if row.owner_user_id is None:
        return True
    return _is_unowned(row)


def _owner_view(owner_user_id, privileged: bool):
    """The owner block, at the detail the caller is entitled to.

    A caller who cannot manage the object gets a display name and nothing
    else. The full block carries the owner's email, member GUID and Superset
    id -- directory data about another tenant's staff, which was being handed
    to every authenticated user on every list request.
    """
    info = service.get_user_info(owner_user_id)
    if info is None:
        return None
    if privileged:
        return info
    return {"id": info["id"], "name": info["name"]}


def _visibility_scope(asset_type: str, user, rows: dict):
    """Who may see ownership metadata about which objects.

    Ownership metadata is not public information: an object's visibility, its
    owner and its share list together describe the access-control graph of the
    deployment. These routes used to iterate every object in the instance for
    any authenticated caller, so a Gamma user in one tenant could read the
    owners and full share lists of every other tenant's objects.

    Returns (scope_ids, privileged, tenant_uuids):
      scope_ids   object ids whose metadata this caller may read; None means
                  "no restriction" (a Superset admin).
      privileged  may see the full owner block and the share list.
    """
    from superset import security_manager

    if security_manager.is_admin():
        return None, True, set()

    tenant_uuids: set[str] = set()
    if is_tenant_administrator(user):
        from superset_ownership.identity import resolve_tenant_guid

        caller_tenant = resolve_tenant_guid(user)
        if caller_tenant:
            # One request for the whole tenant, not one per object.
            tenant_uuids = set(
                get_authorizer().tenant_objects(caller_tenant, asset_type)
            )
        scope = {r.object_id for r in rows.values() if r.object_uuid in tenant_uuids}
        # Their own tenant's objects, plus the public objects they may see
        # and anything reaching them.
        scope |= _public_scope_ids(rows, user)
        scope |= _reachable_ids(asset_type, user.id)
        scope |= {r.object_id for r in rows.values() if r.owner_user_id == user.id}
        return scope, True, tenant_uuids

    scope = _public_scope_ids(rows, user)
    scope |= {r.object_id for r in rows.values() if r.owner_user_id == user.id}
    scope |= _reachable_ids(asset_type, user.id)
    return scope, False, tenant_uuids


def _public_scope_ids(rows: dict, user) -> set[int]:
    """The public rows whose metadata this caller may read: under tenant
    scope their own tenant's and the untenanted ones (the read gate's rule,
    `service.public_row_visible_to`); under instance scope every public row.
    The ownership record of another tenant's public object -- its owner's
    name, its visibility -- is not this caller's to read."""
    public = [r for r in rows.values() if r.visibility == "public"]
    if not service.public_is_tenant_scoped():
        return {r.object_id for r in public}
    # The caller's tenant once, not per row (the rule is otherwise the read
    # gate's, `service.public_row_visible_to`).
    from superset_ownership.identity import resolve_tenant_guid

    user_id = getattr(user, "id", None)
    tenant = service.normalize_tenant(resolve_tenant_guid(user)) if user else None
    return {
        r.object_id
        for r in public
        if (r.owner_user_id is not None and r.owner_user_id == user_id)
        or service.normalize_tenant(r.tenant_guid) == tenant
    }


def _list_row_default_reason(
    *,
    row,
    user,
    unowned: bool,
    superset_admin: bool,
    tenant_admin: bool,
    tenant_uuids: set,
    manage_holder_uuids: set,
) -> str | None:
    """`_list_assets`'s per-row default -- the bulk-computed equivalent of
    `_default_manage_reason`, split out so the loop that calls it (and
    `OWNERSHIP_CAN_MANAGE`, via `_list_row_manage_reason`) stays readable."""
    if superset_admin:
        return MANAGE_REASON_ADMIN
    if row is not None and row.owner_user_id == user.id:
        return MANAGE_REASON_OWNER
    if (
        tenant_uuids
        and row is not None
        and row.object_uuid
        and row.object_uuid in tenant_uuids
    ):
        return MANAGE_REASON_TENANT_ADMIN
    if tenant_admin and _is_claimable(row):
        # Unowned and untenanted: a tenant administrator may claim it.
        return MANAGE_REASON_TENANT_ADMIN
    if (
        row is not None
        and row.object_uuid
        and row.object_uuid in manage_holder_uuids
        and _is_ownable(row)
        and not unowned
    ):
        # Holder of the manage-sharing role, object in their tenant with a
        # live owner, and (by construction of the scope) already visible to
        # them. The flag is the page's summary of the seam; the per-row
        # reads the seam also makes (a queued revocation, the dataset
        # grant) are decided by the detail and the writes, not repeated
        # here for every row.
        return MANAGE_REASON_MANAGE_PERMISSION
    return None


def _list_row_manage_reason(
    manage_hook,
    row,
    user,
    default_reason: str | None,
    *,
    tenant: Any = _UNSET,
    shares: Any = _UNSET,
):
    """`default_reason`, narrowed by `OWNERSHIP_CAN_MANAGE` when configured
    (§4.5.2) -- the list-page's own call site for the same hook
    `_manage_reason` consults for the detail and the writes.

    `tenant`/`shares` are passed in by `_list_assets`, computed ONCE for the
    whole page (M-3): `tenant` from data the page already has (zero extra
    store calls) and `shares` from one batched query, instead of a per-row
    store call and query pair.
    """
    if manage_hook is None:
        return default_reason
    object_state = _can_manage_object_state(
        row, user, default_reason, tenant=tenant, shares=shares
    )
    hook_reason = plugin_hooks.call(
        manage_hook, "OWNERSHIP_CAN_MANAGE", user, object_state
    )
    return plugin_hooks.narrow_manage_reason(hook_reason, default_reason)


def _manage_hook_page_context(
    manage_hook, user, asset_type: str, assets: list
) -> tuple[Optional[str], dict[int, list[dict[str, Any]]]]:
    """`_list_assets`'s ONE-per-page precompute for a configured
    `OWNERSHIP_CAN_MANAGE` hook (M-3): the caller's own tenant guid
    (`resolve_tenant_guid` reads roles, no store call) and every share row
    on the page in ONE batched query -- never a per-row store call and
    query pair. A no-op, zero-cost, when no hook is configured."""
    if manage_hook is None:
        return None, {}
    from superset_ownership.identity import resolve_tenant_guid

    caller_tenant_guid = resolve_tenant_guid(user)
    # §4.5.2: documented as the local mirror only, unlike `list_shares`,
    # which also merges unmirrored store grants for the single-object
    # detail/write path.
    shares_by_object = service.list_share_rows_bulk(asset_type, [a.id for a in assets])
    return caller_tenant_guid, shares_by_object


def _manage_hook_row_tenant(
    manage_hook,
    row,
    caller_tenant_guid: Optional[str],
    tenant_uuids,
    manage_holder_uuids,
) -> Optional[str]:
    """`object_state["tenant"]` for one row, from data `_list_assets`
    already has -- `tenant_uuids`/`manage_holder_uuids`, both built from
    ONE `tenant_objects` call for the caller's own tenant -- never a
    per-row `object_tenant` store lookup (M-3)."""
    if manage_hook is None or row is None or not row.object_uuid:
        return None
    if row.object_uuid in tenant_uuids or row.object_uuid in manage_holder_uuids:
        return caller_tenant_guid
    return None


def _list_assets(asset_type: str):
    """Shared GET /<asset_type>s handler.

    Paginated. `limit` and `offset` query parameters, `limit` capped at
    MAX_LIST_LIMIT -- the handler used to select every dashboard in the
    instance on every render, which is the first thing to fall over on a
    deployment with tens of thousands of objects.

    `can_manage` / `can_share` on a list row are the PAGE'S SUMMARY of the
    manage rule, resolved in bulk; the detail's are the decision. For a
    holder of the manage-sharing permission the two can disagree: the list
    answers from tenant, ownable and live owner, and skips the per-row reads
    the seam also makes (a queued revocation of theirs, the dataset grant),
    so a holder without the grant sees `can_manage: true` here and
    `can_manage: false` on the detail of the same object. The list flag
    gates the ENTRY control (the row's Sharing action); the drawer
    re-decides every control from the detail and fails closed.
    """
    user = _authn()
    if not user:
        return _err(401, "authentication required")

    from superset import db, security_manager

    if asset_type == "dashboard":
        from superset.models.dashboard import Dashboard as Model
    else:
        from superset.models.slice import Slice as Model

    try:
        # Clamped at BOTH ends. `offset` was clamped and `limit` only capped,
        # so ?limit=-5 reached the database and came back as a 500 with the
        # SELECT echoed to the caller.
        limit = max(
            1, min(int(request.args.get("limit", DEFAULT_LIST_LIMIT)), MAX_LIST_LIMIT)
        )
        offset = max(int(request.args.get("offset", 0)), 0)
    except (TypeError, ValueError):
        return _err(400, "limit and offset must be integers")

    rows = {r.object_id: r for r in service.list_rows(asset_type)}
    scope_ids, privileged, tenant_uuids = _visibility_scope(asset_type, user, rows)

    # The list carries the same can_share / unowned / can_manage a single
    # object reports, so a client needs one request for a page rather than one
    # detail request per row. Resolved in bulk: owner liveness is ONE query
    # for every owner id on the page, and the object-to-tenant map is one
    # request for the whole tenant. A caller who is neither a Superset admin
    # nor a tenant administrator makes zero calls to the authorization store
    # beyond the reverse share lookup the scope already needs.
    superset_admin = security_manager.is_admin()
    # privileged is True for both a Superset admin and a tenant administrator;
    # this isolates the tenant-administrator case for the claim rule below.
    tenant_admin = privileged and not superset_admin

    # The optional manage-sharing role (`_manage_permission_reason`). Its
    # holder's scope is NOT widened -- `_visibility_scope` gave them the
    # public / owned / shared rows any member gets, which is exactly the
    # "can already see" condition the seam requires -- so the only question
    # left per row is the tenant one, answered with the same single
    # tenant-objects request a tenant administrator's page makes. A tenant
    # administrator who also holds the role is covered by `tenant_uuids`
    # already and reported under their broader ground.
    manage_holder_uuids: set[str] = set()
    if not privileged and holds_manage_permission(user):
        from superset_ownership.identity import resolve_tenant_guid

        holder_tenant = resolve_tenant_guid(user)
        if holder_tenant:
            manage_holder_uuids = set(
                get_authorizer().tenant_objects(holder_tenant, asset_type)
            )

    query = db.session.query(Model).order_by(Model.id)
    if scope_ids is not None:
        if not scope_ids:
            return jsonify({"result": [], "count": 0, "limit": limit, "offset": offset})
        query = query.filter(Model.id.in_(scope_ids))
    count = query.count()
    assets = query.limit(limit).offset(offset).all()

    page_rows = [rows.get(a.id) for a in assets]
    owner_ids = {
        r.owner_user_id
        for r in page_rows
        if r is not None and r.owner_user_id is not None
    }
    active_owners = _active_owner_ids(owner_ids)

    # Read once for the whole page, never per row: the common case (no
    # OWNERSHIP_CAN_MANAGE configured) must keep the zero-extra-calls
    # property this loop was written for; a deployment that configures the
    # hook has opted into paying its cost once for the page instead of once
    # per row (M-3 -- this used to be N+1: a store `object_tenant` call plus
    # a `list_shares` query per row).
    manage_hook = get_hook("OWNERSHIP_CAN_MANAGE")

    # M-3: precomputed ONCE for the whole page -- never per row -- from data
    # this handler already has, not a store call. See
    # `_manage_hook_page_context`'s docstring.
    caller_tenant_guid, shares_by_object = _manage_hook_page_context(
        manage_hook, user, asset_type, assets
    )

    result = []
    for asset in assets:
        row = rows.get(asset.id)
        unowned = bool(
            row is not None
            and row.owner_user_id is not None
            and row.owner_user_id not in active_owners
        )

        default_reason = _list_row_default_reason(
            row=row,
            user=user,
            unowned=unowned,
            superset_admin=superset_admin,
            tenant_admin=tenant_admin,
            tenant_uuids=tenant_uuids,
            manage_holder_uuids=manage_holder_uuids,
        )
        row_tenant = _manage_hook_row_tenant(
            manage_hook, row, caller_tenant_guid, tenant_uuids, manage_holder_uuids
        )
        reason = _list_row_manage_reason(
            manage_hook,
            row,
            user,
            default_reason,
            tenant=row_tenant,
            shares=shares_by_object.get(asset.id, []),
        )
        can_manage = reason is not None

        parked = _is_parked(row)
        result.append(
            {
                "object_id": asset.id,
                # Per row, never per caller. `privileged or can_manage` meant a
                # tenant administrator got the full owner block -- email and
                # member GUID -- for every row on the page including objects in
                # other tenants that the DETAIL route correctly redacted.
                "owner": _owner_view(row.owner_user_id, can_manage) if row else None,
                "visibility": _display_visibility(row),
                "disabled": parked,
                "unowned": unowned,
                "can_manage": can_manage and not parked,
                "can_share": can_manage
                and not parked
                and not unowned
                and _is_ownable(row)
                and _may_share(row, user, reason),
            }
        )
    return jsonify({"result": result, "count": count, "limit": limit, "offset": offset})


def _is_ownable(row) -> bool:
    """Can this object be shared at all?

    An object with no owner recorded cannot: `raise_for_access_bypass` treats
    owner=None as "nobody can ever be granted", so a share on it writes a
    tuple, answers 200, appears in the grantee's list, and 403s when they
    click it. Two hooks in the same module disagreeing is worse than a
    refusal.
    """
    return row is not None and row.owner_user_id is not None


def _active_owner_ids(owner_ids: set[int]) -> set[int]:
    """Which of these user ids are still active, in one query.

    The per-object path can afford a lookup per object; a list page cannot.
    Ivanti soft-deletes users, so "active" is a column read, not an absence.
    """
    if not owner_ids:
        return set()
    from superset import security_manager

    model = security_manager.user_model
    from superset import db

    return {
        uid
        for (uid,) in db.session.query(model.id)
        .filter(model.id.in_(owner_ids), model.active.is_(True))
        .all()
    }


def _get_asset_detail(asset_type: str, pk: int):
    """Shared GET /<asset_type>/<pk> handler."""
    user = _authn()
    if not user:
        return _err(401, "authentication required")

    asset = _get_asset(asset_type, pk)
    if not asset:
        return _err(404, _not_found_msg(asset_type))

    row = service.lookup(asset_type, pk)

    # Evaluated ONCE. This used to call _require_owner_or_admin twice, each
    # doing an authorization-store check plus a read, and _is_unowned three
    # times -- four round-trips and three user lookups to answer one GET.
    parked = _is_parked(row)
    # Two distinct questions. `may_manage` decides who can SEE the ownership
    # metadata (owner/admin/tenant-admin). `can_manage` is the control flag
    # returned to the client, and a parked object offers no controls -- but it
    # is still visible to those who manage it, so parking must not hide it.
    manage_reason = _manage_reason(row, user)
    may_manage = manage_reason is not None
    can_manage = may_manage and not parked
    unowned = _is_unowned(row)
    visibility = _display_visibility(row)

    # Ownership metadata is not public information; see _visibility_scope.
    # A caller with no relationship to a non-public object is told it is not
    # there rather than being shown its owner and who it is shared with.
    # `_reachable_ids` is memoised for the request: with the seam on, the
    # seam has already made this read for a holder, and it is not repeated.
    if not may_manage and (
        visibility != "public"
        or (
            service.public_is_tenant_scoped()
            and not service.public_row_visible_to(row, user)
        )
    ):
        # ... nor a public object of another tenant (public within the
        # tenant): its owner's name is that tenant's information.
        reachable = pk in _reachable_ids(asset_type, user.id)
        if not reachable:
            return _err(404, _not_found_msg(asset_type))

    return jsonify(
        {
            "object_id": asset.id,
            "object_uuid": row.object_uuid if row else None,
            "owner": _owner_view(row.owner_user_id, may_manage) if row else None,
            "visibility": visibility,
            "disabled": parked,
            # Who else an object is shared with is management information: it
            # names other people's accounts. Only a caller who could change
            # the shares gets to enumerate them.
            "shares": service.list_shares(asset_type, pk)
            if (row and may_manage)
            else [],
            # An object whose owner has been deactivated is unowned: it keeps
            # its visibility (a public dashboard stays public) but nobody can
            # change its sharing until an administrator reassigns it.
            "unowned": unowned,
            # A tenant administrator manages (transfer, claim) but does not
            # share, unless they also hold the manage-sharing permission:
            # see _sharing_ground.
            "can_share": can_manage
            and not unowned
            and _is_ownable(row)
            and _may_share(row, user, manage_reason),
            "can_manage": can_manage,
            # WHY the caller manages it ('owner', 'tenant_admin', 'admin' or
            # 'manage_permission'), null when they do not. After transferring
            # an object away the client tells the caller on which ground its
            # controls stay live.
            "manage_reason": manage_reason if can_manage else None,
            # The caller's OWN store reference, as a share to them would be
            # spelled. A holder of the manage-sharing permission may not
            # change their own share, and the client cannot otherwise tell
            # which share row is theirs (the drawer knows subjects, not
            # who is looking). Resolved from the account in hand: no query.
            "caller_subject": _caller_subject(user),
        }
    )


def _caller_subject(user) -> str | None:
    from superset_ownership.identity import member_ref

    return member_ref(user)


def _is_unowned(row) -> bool:
    """Owner recorded but no longer an active account.

    Distinct from an object that never had an owner, which is the backfill
    state and stays public.
    """
    if row is None:
        return False
    from superset_ownership.hooks import is_unowned

    return is_unowned(row)


def _native_editor_user_ids(asset: Any) -> list[int]:
    """The Superset user ids currently in `asset.editors` (a plain read, no
    query), sorted for a stable audit record. Used to put a before/after
    `editor_user_ids` pair on the transfer and claim OWNER_ASSIGNED events
    (review M-1): `ownership check` and the audit trail did not otherwise
    have anything to say about the native collection at all."""
    editors = getattr(asset, "editors", None)
    if not editors:
        return []
    return sorted(
        {
            uid
            for uid in (getattr(s, "user_id", None) for s in editors)
            if uid is not None
        }
    )


def _sync_native_editors_or_error(
    asset: Any, row: "service.OwnershipRow", *, previous_owner_id: Optional[int] = None
) -> Optional[Any]:
    """`service.sync_native_editors`, called where an ownership write's
    owner just changed (transfer, claim, adopting an ownerless object).

    REVIEW M-1 (round 1): transactional, not best-effort. An earlier
    version of this helper swallowed the exception on the premise that "by
    the time this runs the ownership row and the store tuple are already
    written" -- that premise does not hold on any of these three routes:
    `service.upsert_ownership` writes through the session and
    `outbox.enqueue` deliberately does not commit either, and the caller's
    `db.session.commit()` is always AFTER this call. A rollback is
    available here and costs nothing, so a sync failure must not leave a
    committed ownership change next to a native `editors` collection that
    still names the previous owner -- the exact half-applied state issue
    #80 was about, reopened through a new seam.

    Returns None on success. On failure, rolls the whole write back (the
    ownership row, the queued outbox writes, everything this request did)
    and returns the 503 response the caller should return as-is: a fixed
    sentence, never the exception text, matching the posture the outbox
    write failure above each call site already uses (api.py, the 502
    "the authorization store rejected the new owner" branch).
    """
    try:
        service.sync_native_editors(asset, row, previous_owner_id=previous_owner_id)
    except Exception:
        logger.exception(
            "superset_ownership: could not sync native editors for %s %s",
            row.asset_type,
            row.object_id,
        )
        from superset import db as _db_rollback

        _db_rollback.session.rollback()
        return _err(
            503,
            "could not synchronise this object's native editors; nothing was changed",
        )
    return None


def _object_tenant_for_write(asset_type: str, row):
    """The object's tenant as a write that may STAMP one must read it:
    strictly. Returns `(tenant, None)`, or `(None, response)` -- a 503 --
    when the store could not answer.

    The owner, claim and visibility-adoption paths branch on "the object
    has no tenant" to decide whether to stamp the new owner's. A non-strict
    read answers None for an outage too, which would turn a store failure
    into a stamp (and skip the recipient-tenant refusal) for exactly the
    callers whose recipient validation makes no strict store read of its
    own -- a Superset admin, or an owner naming a same-role recipient. The
    read gate may treat a failed read as "no tenant"; a write may not. The
    local backend has no store and never raises.

    A store that answers "no tenant" is then read against the row's mirror
    (revision 0004): the mirror is written in the same transaction as the
    `set_tenant` outbox row, before it, so while the outbox is still
    delivering a fresh object's stamp the store is behind and the row is
    not. Without this a transfer or claim in that window saw no tenant,
    skipped the recipient-tenant refusal and stamped the recipient's tenant
    over the pending one. The mirror is never AHEAD of a tenant the store
    holds (it is filled from the store), so a store answer always wins.
    """
    if row is None or not row.object_uuid:
        return None, None
    from superset_ownership.fga import StoreError, StoreUnavailable

    try:
        stored = get_authorizer().object_tenant(
            asset_type, row.object_uuid, strict=True
        )
        return (stored or getattr(row, "tenant_guid", None) or None), None
    except StoreError as exc:
        why = (
            "is unavailable"
            if isinstance(exc, StoreUnavailable)
            else f"refused the lookup ({exc})"
        )
        return None, _err(
            503,
            f"cannot verify the object's tenant: the authorization store {why}; "
            "nothing was changed",
        )


def _tenant_move_refusal(new_owner_tenant: str | None, *, who: str, what: str):
    """The 409 for a new owner who would move a tenanted object: their tenant
    role names another tenant (`new_owner_tenant`), or they have none. `who`
    is "the recipient" or "you"; `what` is the action ("a transfer", "a
    claim", ...)."""
    if new_owner_tenant is None:
        head = (
            "you have no tenant role" if who == "you" else f"{who} has no tenant role"
        )
    else:
        head = (
            "your tenant role names another tenant"
            if who == "you"
            else f"{who}'s tenant role names another tenant"
        )
    return _err(409, f"{head}; {what} never moves an object out of its tenant")


# The `admitted_by` recorded on a share_removed event that _set_asset_
# visibility emits itself, rather than the ground the CALLER used to change
# visibility (already recorded on the visibility_changed event alongside
# it) -- issue #93: private revokes every existing share as a consequence
# of the visibility change, not a separate share-management action, so the
# ground is the change itself, not owner/tenant_admin/admin/manage_permission.
ADMITTED_BY_VISIBILITY_CHANGE = "visibility_change"


def _revoke_shares_for_private(
    asset_type: str, pk: int, object_uuid: Optional[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Every existing share on the object, revoked through the SAME path the
    unshare route uses (issue #93): the mirror row deleted and a
    `revoke_subject` queued through the outbox, so the read gate's
    `has_pending_revocation` covers the delivery window exactly as it does
    for an explicit unshare -- "Visible to only you" is no longer true of a
    still-shared object for the seconds until the drain runs.

    Runs on the caller's session, inside the same transaction as the
    visibility write; nothing here commits. Returns `(revoked, failed)`:
    `revoked` the shares whose removal succeeded, captured BEFORE the
    revoke so the route can audit one event per share once the transaction
    that removed them has committed. `failed` the shares `service.
    remove_share` refused -- reachable only in inline mode
    (`OWNERSHIP_OUTBOX_ENABLED=false`), where a store refusal is answered
    inline rather than queued (M2, review round 1): with the outbox on,
    `remove_share`'s enqueue cannot itself fail this way, and a queue
    failure raises and unwinds the whole transaction -- visibility write
    included -- before this function would ever report one. The caller
    rolls back and answers 502 for a non-empty `failed`, the same shape
    `_remove_asset_share` already uses for the identical refusal.

    A no-op, cheaply, when the object has no shares (the common case) or has
    no uuid yet (a row this same request just created can hold no share).
    """
    if not object_uuid:
        return [], []
    shares = service.list_share_rows(asset_type, pk)
    revoked: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for share in shares:
        if service.remove_share(asset_type, pk, object_uuid, share["subject"]):
            revoked.append(share)
        else:
            failed.append(share)
    return revoked, failed


def _revoke_shares_for_private_or_502(
    asset_type: str, pk: int, row: Any, asset: Any
) -> tuple[list[dict[str, Any]], Any]:
    """`_set_asset_visibility`'s own `-> private` step: run
    `_revoke_shares_for_private` and, on a partial refusal, roll back and
    build its 502 -- extracted (R2-N5, review round 2, PR #96,
    `review-private-revokes-pr96.md`) purely to keep that orchestration out
    of the caller's own branch count, tests unchanged.

    Returns `(revoked_shares, error_response)`: `error_response` is `None`
    on success (`revoked_shares` is what the caller's audit loop reports);
    when it is not `None` the transaction has ALREADY been rolled back here
    and the caller must return it immediately, without any further write.
    """
    from superset import db

    object_uuid_for_revoke = row.object_uuid if row else str(asset.uuid)
    revoked_shares, failed_shares = _revoke_shares_for_private(
        asset_type, pk, object_uuid_for_revoke
    )
    if not failed_shares:
        return revoked_shares, None
    # M2 (review round 1): inline mode only -- the outbox path cannot reach
    # here (an enqueue failure raises and unwinds the whole transaction,
    # visibility write included, before this point). A refusal here means a
    # share is still live in the authorization store while its mirror row
    # is already gone and `private` is about to commit; roll back and
    # answer 502 BEFORE the sentinel write.
    #
    # R2-N2: with two or more shares, `_revoke_shares_for_private` does NOT
    # stop at the first refusal -- it keeps trying the rest (fail-safe
    # direction: the store ends up narrower than the mirror, never wider,
    # and a retry heals it, since `remove_share` on an already-gone tuple is
    # satisfied, not a second failure). So an EARLIER share in the same
    # request can already be gone from the store by the time a LATER one is
    # refused and this 502 fires -- the mirror row for that earlier share is
    # restored by the rollback below same as every other row, but the store
    # already does not have it, so "nothing was changed" is not true for
    # it. Unlike the unshare route's identical 502 (exactly one share per
    # request, so nothing PARTIALLY succeeds there), this one says so.
    db.session.rollback()
    return revoked_shares, _err(
        502,
        "the authorization store rejected this change; some grants may "
        "already be revoked -- retry to finish revoking the rest",
    )


def _set_asset_visibility(asset_type: str, pk: int):
    """Shared PUT /<asset_type>/<pk>/visibility handler.

    A tenant administrator is refused here (403, `_sharing_ground`): the
    object's sharing is its owner's, and an administrator who needs to
    change it takes ownership first (transfer to self, or a claim on an
    ownerless object) -- unless they also hold the manage-sharing
    permission, on whose ground they then proceed.

    The ownerless -> non-public branch ADOPTS the object for the caller (a
    Superset admin; a tenant administrator is refused above and claims
    instead; the seam never admits a manage-sharing holder to an ownerless
    object). Adoption assigns an owner, so it keeps the same invariant as a
    transfer or a claim: the object's tenant is read strictly (503 when the
    store cannot answer); a caller whose tenant role names another tenant,
    or who has no tenant role, is refused (409) before any write; and the
    caller's tenant is stamped only on an object that has none.
    """
    user = _authn(mutating=True)
    if not user:
        return _err(401, "authentication required")

    asset = _get_asset(asset_type, pk)
    if not asset:
        return _err(404, _not_found_msg(asset_type))

    # The per-object write lock, first: it returns the row as stored (a
    # write route decides who may write from that, not from a copy up to a
    # cache TTL old) and is held to the commit, so this request's mirror
    # write, outbox row and commit cannot interleave with another writer's on
    # the same object. See service.lock_object.
    row = service.lock_object(asset_type, pk)

    prior_visibility = row.visibility if row else None
    if _is_parked(row):
        return _parked_error()
    admitted_by = _manage_reason(row, user)
    if admitted_by is None:
        return _err(
            403,
            "only the owner, an admin or a holder of the manage-sharing "
            "permission can change visibility; a tenant administrator takes "
            "ownership first",
        )
    admitted_by, refused = _sharing_ground(row, user, admitted_by)
    if refused is not None:
        return refused

    payload = request.get_json(silent=True) or {}
    visibility = payload.get("visibility")
    if visibility not in VALID_VISIBILITIES:
        return _err(400, f"visibility must be one of {sorted(VALID_VISIBILITIES)}")
    if visibility == "public" and admitted_by == MANAGE_REASON_MANAGE_PERMISSION:
        # The one action in the set that widens READ access, for everybody
        # rather than for a named subject. The permission manages who an
        # object is shared with; opening it to the whole organisation is the
        # owner's (or an administrator's) call. private <-> shared, and
        # public -> private / shared, stay with the holder.
        return _err(
            403,
            "the manage-sharing permission does not include making an object public",
        )
    if (
        visibility == "private"
        and admitted_by == MANAGE_REASON_MANAGE_PERMISSION
        and row is not None
        and _private_would_revoke_holder_rows(asset_type, pk, user, admitted_by)
    ):
        # M1 (review round 1): `-> private` revokes every mirror row
        # (below), which for a manage-sharing holder can include their own
        # share and the tenant's structural group -- exactly the two writes
        # this same ground refuses individually on POST/DELETE /shares
        # (`_self_grant_under_manage_permission`,
        # `_structural_group_under_manage_permission`). Reaching them
        # through `-> private` instead let a holder revoke their own access
        # (and the tenant's whole-membership grant) with no way back:
        # `-> shared` afterwards starts empty, and the seam that admitted
        # them here no longer finds a share of theirs to admit them by.
        # Refused before any write, same shape as the `-> public` refusal
        # above.
        return _err(
            403,
            "the manage-sharing permission does not include making an "
            "object private when a mirror row is your own share or the "
            "tenant's structural group",
        )

    from superset import db

    if row is None:
        from superset_ownership.identity import member_ref, resolve_tenant_guid

        new_row = service.OwnershipRow(
            id=0,
            asset_type=asset_type,
            object_id=pk,
            object_uuid=str(asset.uuid),
            owner_user_id=user.id,
            visibility=visibility,
        )
        # Adopting assigns an owner: the same tenant invariant as the
        # `elif` branch below, a transfer or a claim, decided before the
        # first write. The store may hold a tenant for an object whose row
        # is gone; the adopter must be of it.
        obj_tenant, unreadable = _object_tenant_for_write(asset_type, new_row)
        if unreadable is not None:
            return unreadable
        tenant_guid = resolve_tenant_guid(user)
        if obj_tenant and service.normalize_tenant(
            tenant_guid
        ) != service.normalize_tenant(obj_tenant):
            return _tenant_move_refusal(
                tenant_guid, who="you", what="adopting an object with no record"
            )
        service.upsert_ownership(
            asset_type,
            pk,
            str(asset.uuid),
            user.id,
            visibility,
            tenant_guid=obj_tenant or tenant_guid,
        )
        # M-3 (review round 1): same invariant as the `elif` branch just
        # below -- adopting assigns an owner, so the object's native
        # editors must gain that owner too (see service.sync_native_editors)
        # -- this branch (no ownership row existed at all yet) was the one
        # F-3 call site that got missed the first time: a tenant
        # administrator adopting a pre-module Draft dashboard through here
        # could set it Public/private and still 404 themselves.
        err = _sync_native_editors_or_error(asset, new_row)
        if err is not None:
            return err
        # And the store learns the owner and -- when it had none -- the
        # tenant, exactly as the `elif` branch writes them. Without the
        # tenant stamp an adopted object set Public here stayed untenanted:
        # under "public within the tenant" that is an object its own tenant
        # cannot read, which `check` then reports as repairable.
        subject = member_ref(user.id)
        if subject:
            outbox.write_tuple(subject, "owner", f"{asset_type}:{asset.uuid}")
        if tenant_guid and not obj_tenant:
            outbox.set_object_tenant(asset_type, str(asset.uuid), tenant_guid)
    elif row.owner_user_id is None and visibility != "public":
        # Backfilled / pre-existing rows can have no owner (e.g. example
        # data created with no created_by_fk). An ownerless object can
        # never be shared (raise_for_access_bypass treats owner=None as
        # "nobody can ever be granted"), so claim ownership for whoever is
        # making it private/shared. That is a Superset admin or a tenant
        # administrator: the seam never admits a manage-sharing holder to
        # an ownerless object (`_is_ownable`), so this branch cannot hand
        # one to them.
        from superset_ownership.identity import member_ref, resolve_tenant_guid

        # Adopting assigns an owner: same invariant as a transfer or a
        # claim, decided before the first write.
        obj_tenant, unreadable = _object_tenant_for_write(asset_type, row)
        if unreadable is not None:
            return unreadable
        tenant_guid = resolve_tenant_guid(user)
        if obj_tenant and service.normalize_tenant(
            tenant_guid
        ) != service.normalize_tenant(obj_tenant):
            return _tenant_move_refusal(
                tenant_guid, who="you", what="adopting an ownerless object"
            )
        service.upsert_ownership(asset_type, pk, row.object_uuid, user.id, visibility)
        # F-3: adopting assigns an owner same as a transfer or a claim, so
        # the object's native editors must gain that owner the same way too
        # -- otherwise a Draft object set Public/private here can still
        # vanish from its own new owner (see service.sync_native_editors).
        # M-1 (review round 1): transactional, not best-effort -- see
        # _sync_native_editors_or_error's docstring.
        err = _sync_native_editors_or_error(
            asset,
            dataclasses.replace(row, owner_user_id=user.id, visibility=visibility),
        )
        if err is not None:
            return err
        if row.object_uuid:
            # Ivanti's tuples are keyed on the member GUID. Writing
            # "user:<superset integer id>" here produced a tuple that no
            # subsequent check could ever match, so the claim recorded an
            # owner the authorization store did not agree existed.
            subject = member_ref(user.id)
            if subject:
                outbox.write_tuple(subject, "owner", f"{asset_type}:{row.object_uuid}")
            # Stamped only when the object had no tenant; one that has a
            # tenant keeps it (the caller was refused above unless theirs
            # is the same).
            if tenant_guid and not obj_tenant:
                outbox.set_object_tenant(asset_type, row.object_uuid, tenant_guid)
    else:
        service.set_visibility(asset_type, pk, visibility)

    # Private revokes (issue #93): every share on the object is queued for
    # revocation in the SAME transaction as the visibility write, before the
    # commit below -- so a shared user is never left readable of an object
    # the drawer now tells them is "Visible to only you". `private ->
    # shared` afterwards starts with zero shares, exactly as a brand-new
    # object does. Captured before commit so the audit loop below has
    # something to report even though the rows are gone by then.
    revoked_shares: list[dict[str, Any]] = []
    if visibility == "private":
        revoked_shares, error = _revoke_shares_for_private_or_502(
            asset_type, pk, row, asset
        )
        if error is not None:
            return error

    if visibility == "public" and not service.public_is_tenant_scoped():
        # Instance scope: public hands the object back to Superset's rules.
        sentinel.remove_sentinel(asset)
    else:
        # Private, shared -- and public under tenant scope, where the read
        # gate grants the object's tenant instead of Superset's own
        # "anyone with the dataset grant".
        sentinel.add_sentinel(asset)

    db.session.commit()
    audit.emit(
        audit.VISIBILITY_CHANGED,
        actor=user,
        asset_type=asset_type,
        object_id=pk,
        object_uuid=row.object_uuid if row else None,
        before={"visibility": prior_visibility},
        after={"visibility": visibility},
        admitted_by=admitted_by,
    )
    for share in revoked_shares:
        audit.emit(
            audit.SHARE_REMOVED,
            actor=user,
            asset_type=asset_type,
            object_id=pk,
            object_uuid=row.object_uuid if row else None,
            before={"subject": share["subject"], "mirror_row": True},
            admitted_by=ADMITTED_BY_VISIBILITY_CHANGE,
        )
    logger.info(
        "superset_ownership: %s %s visibility -> %s (by user %s)%s",
        asset_type,
        pk,
        visibility,
        user.id,
        f"; revoked {len(revoked_shares)} share(s)" if revoked_shares else "",
    )
    return jsonify({"object_id": pk, "visibility": visibility}), 200


_SHARES_403 = (
    "only the owner, an admin or a holder of the manage-sharing permission "
    "can manage shares; a tenant administrator takes ownership first"
)
_TENANT_ADMIN_SHARING_403 = (
    "a tenant administrator may transfer ownership but not change how an "
    "object is shared; take ownership of it first"
)


def _sharing_ground(row, user, admitted_by: str | None):
    """The ground a SHARING write (visibility, share, unshare) proceeds on,
    or the 403 it stops with: `(admitted_by, None)` or `(None, response)`.

    A tenant administrator's authority is over who OWNS an object, not over
    who sees it: they may transfer it -- to a member, or to themselves --
    but a change to its sharing is the owner's alone. So an administrator
    who needs to change it takes ownership first, and the owner line then
    says so: a change of sharing can only ever have been made by the name
    on it, and an intervention leaves the administrator's name there.
    Superset administrators (break-glass) are not narrowed, and neither is
    the manage-sharing permission, an explicit opt-in whose purpose is
    exactly sharing without owning: a tenant administrator who ALSO holds
    it -- `_manage_reason` reports the administrator ground first -- is
    admitted on the permission's ground instead, with that ground's own
    narrowing (no public, no self-share, ...) and audit label, rather than
    losing a permission the deployment granted them. The grounds are
    decided in `_manage_reason`; this re-decides one of them on these three
    routes only.
    """
    if admitted_by != MANAGE_REASON_TENANT_ADMIN:
        return admitted_by, None
    holder = _manage_permission_reason(row, user)
    if holder is not None:
        return holder, None
    return None, _err(403, _TENANT_ADMIN_SHARING_403)


def _may_share(row, user, reason: str | None) -> bool:
    """`can_share` for the detail and list rows: the caller's manage ground
    admits a sharing write -- every ground but a tenant administrator's,
    unless that administrator also holds the manage-sharing permission."""
    if reason is None:
        return False
    if reason != MANAGE_REASON_TENANT_ADMIN:
        return True
    return _manage_permission_reason(row, user) is not None


_SELF_SHARE_403 = (
    "the manage-sharing permission does not include sharing to yourself, "
    "changing your own role or removing your own share"
)
_STRUCTURAL_GROUP_SHARE_403 = (
    "the manage-sharing permission does not include sharing to, or unsharing, "
    "the tenant's own membership or administrator group; that is the whole "
    "tenant, not a third party"
)


def _self_grant_under_manage_permission(
    subject: str, user, admitted_by: str | None
) -> bool:
    """Is this a share or owner write naming the CALLER, admitted only by the
    manage-sharing permission?

    Compared on the canonical store reference, which is what `subject` has
    been normalised to by the time this is asked; a caller with no member
    reference at all cannot be named by any subject.
    """
    if admitted_by != MANAGE_REASON_MANAGE_PERMISSION:
        return False
    from superset_ownership.identity import member_ref

    own = member_ref(user.id)
    return own is not None and subject == own


def _structural_group_under_manage_permission(
    subject: str, admitted_by: str | None
) -> bool:
    """Is this a share write naming a tenant's STRUCTURAL group, admitted only
    by the manage-sharing permission?

    `group:tenant_<guid>#member` is every member of the tenant and
    `group:tenant_administrator_<guid>#member` its administrators. A share
    to the first is the whole-tenant read grant that `visibility -> public`
    reserves for the owner, minus the label; and both include the holder,
    so a change to either is in part the holder's own share, which this
    ground leaves to the owner in either direction (as the self-check
    does). Any other group is a third party even when the holder is a
    member: a group share never grants more than viewer, which the holder
    already holds on an object the seam admitted them to. `_validate_subject`
    has already required the group to be the caller's own tenant's.
    """
    if admitted_by != MANAGE_REASON_MANAGE_PERMISSION:
        return False
    kind, _, rest = (subject or "").partition(":")
    if kind != "group":
        return False
    from superset_ownership.identity import is_structural_tenant_role

    return is_structural_tenant_role(rest.split("#", 1)[0])


def _private_would_revoke_holder_rows(
    asset_type: str, pk: int, user, admitted_by: str | None
) -> bool:
    """M1's own question, extracted out of `_set_asset_visibility`'s branch
    count (R2-N5, review round 2, PR #96, `review-private-revokes-
    pr96.md`): would `-> private`'s revoke-every-share step (below) revoke a
    mirror row that is the manage-sharing holder's OWN share, or the
    tenant's structural group? Both are refused individually on `POST`/
    `DELETE /shares` already (`_self_grant_under_manage_permission`,
    `_structural_group_under_manage_permission`); this is the same refusal,
    asked before `-> private` reaches them a different way.
    """
    return any(
        _self_grant_under_manage_permission(share["subject"], user, admitted_by)
        or _structural_group_under_manage_permission(share["subject"], admitted_by)
        for share in service.list_share_rows(asset_type, pk)
    )


def _write_share(asset_type: str, pk: int, object_uuid: str, subject: str, role: str):
    """`service.add_share` for the POST route: (written, None) on a write
    the service accepted, (False, response) when the insert was refused.

    404 when the object row was deleted between the route's lookup and the
    insert (a concurrent hard delete): the share -> object foreign key
    refused the row. 500 with a fixed sentence for an IntegrityError the
    service could not classify (a column constraint, a schema drift) with
    no row present -- a defect, not a deleted object; the service has
    logged the driver text at ERROR. Left to Superset's generic handler
    that 500's body for a non-guest caller would carry the driver message,
    the INSERT and its parameters -- the same rule as _refuse_transfer:
    the exception goes to the log, never to the client. In both cases
    nothing was written and nothing is queued."""
    from superset import db

    try:
        return service.add_share(asset_type, pk, object_uuid, subject, role), None
    except service.ShareTargetGoneError as exc:
        db.session.rollback()
        logger.info("superset_ownership: share refused, %s", exc)
        return False, _err(
            404, f"{asset_type} {pk} no longer exists; nothing was changed"
        )
    except IntegrityError:
        db.session.rollback()
        logger.exception(
            "superset_ownership: recording the share %s %s -> %s failed with an "
            "unclassified IntegrityError; answered 500 with a fixed body",
            asset_type,
            pk,
            subject,
        )
        return False, _err(500, "the share could not be recorded; nothing was changed")


def _add_asset_share(asset_type: str, pk: int):
    """Shared POST /<asset_type>/<pk>/shares handler."""
    user = _authn(mutating=True)
    if not user:
        return _err(401, "authentication required")

    asset = _get_asset(asset_type, pk)
    if not asset:
        return _err(404, _not_found_msg(asset_type))

    # The body's shape, BEFORE the lock: these read nothing.
    payload = request.get_json(silent=True) or {}
    subject = payload.get("subject")
    role = payload.get("role", "viewer")
    if role not in VALID_SHARE_ROLES:
        return _err(400, f"role must be one of {sorted(VALID_SHARE_ROLES)}")
    if not subject:
        return _err(400, "subject is required, e.g. 'user:<guid>'")

    # The subject, also BEFORE the lock -- but its verdict is HELD, not
    # answered, and its member paging is NOT made. `_validate_subject`
    # reads the caller's tenant role, the subject's Superset account and
    # -- for a group -- the store's group check; it never reads the
    # ownership row, so nothing it decides is decided from a copy the lock
    # would have refreshed, and running it here keeps those reads off the
    # object's write lock. There is no row to re-compare after locking: no
    # copy of it has been read yet. Its answer waits until the caller has
    # been admitted below: the lookup resolves a username, an email or an
    # integer id against every account on the instance, so "does not name
    # a known account" / "is deactivated" / "is not a member of your
    # tenant" told a caller with no right to share anything whether an
    # account exists, and in which tenant -- the enumeration the subject
    # search refuses. A refused caller is answered the one 403 whatever
    # the subject; an admitted caller is told what is wrong with it.
    #
    # The one read it does not make yet is the store's member paging, one
    # HTTP round trip per page of the caller's tenant, which the subject
    # needs in exactly one case: an account whose role names another
    # tenant (or none). Made here, its duration would have answered the
    # refused caller what the body does not -- that the account exists,
    # outside their tenant. So `store=False`: that case comes back
    # undecided (None) and is settled after admission, under the lock, by
    # a caller who may share anyway. What a refused caller can still time
    # is Superset's own account lookups, which every case makes.
    subject_ok, subject_why = _validate_subject(subject, user, store=False)

    # The per-object write lock, first of anything that touches the object:
    # it returns the row as stored (a write route decides who may write from
    # that, not from a copy up to a cache TTL old) and is held to the
    # commit, so this request's mirror write, outbox row and commit cannot
    # interleave with another writer's on the same object. Every decision
    # read of the row -- parked, unowned, the admitting rule, ownable --
    # follows it. See service.lock_object.
    row = service.lock_object(asset_type, pk)
    if _is_parked(row):
        return _parked_error()
    if _is_unowned(row):
        # No owner to authorise the grant. The object keeps its visibility --
        # a public dashboard stays public -- but sharing is closed until an
        # administrator reassigns ownership.
        return _err(409, "object is unowned; assign an owner before sharing")

    admitted_by = _manage_reason(row, user)
    if admitted_by is None:
        return _err(403, _SHARES_403)
    admitted_by, refused = _sharing_ground(row, user, admitted_by)
    if refused is not None:
        return refused

    # Admitted: the held verdict on the subject can be given -- and the
    # one the store has to settle, asked for now.
    if subject_ok is None:
        subject_ok, subject_why = _validate_subject(subject, user)
    if not subject_ok:
        return _err(400, subject_why)

    if not _is_ownable(row):
        # No owner has ever been recorded. raise_for_access_bypass refuses
        # everyone on such an object, so accepting the share would write a
        # tuple, answer 200, put the object in the grantee's list and 403 when
        # they clicked it.
        return _err(409, "object has no owner; assign one before sharing")

    if row is not None and row.visibility == "private":
        # H1 (review round 1, issue #93): the read gate refuses a private
        # object to every non-owner regardless of role, editor included
        # (`hooks.editor_subject_ids`). Writing a grant here would still
        # answer 200 and put a row in the mirror the read path ignores for
        # a viewer and honours for an editor -- accepted, but inert for
        # neither role. The drawer never sends a share write while an
        # object is private (visibility and shares are separate applies),
        # so nothing user-facing changes; a script gets a truthful answer
        # instead of a share the object silently withholds.
        return _err(409, "object is private; make it shared before sharing")

    from superset import db

    object_uuid = row.object_uuid if row else str(asset.uuid)
    # Store the canonical reference so the grant and the later access check
    # are asking about the same string.
    from superset_ownership.identity import IdentityLookupError, normalize_subject

    # R-1: `_validate_subject` above already ran this same lookup
    # successfully (a raise there would have answered not-ok and returned
    # before this point), but a plugged Identity backed by a live
    # connection can fail on a second call moments later. Fail closed
    # exactly as a None answer already does here (keep the raw subject)
    # rather than let the exception escape -- this call site has no
    # caller-facing "cannot verify" sentence to give, unlike the guard in
    # `_validate_user_subject`.
    try:
        subject = normalize_subject(subject) or subject
    except IdentityLookupError:
        pass
    if _self_grant_under_manage_permission(subject, user, admitted_by):
        # A holder shared with as viewer would otherwise write themselves an
        # editor share: granting oneself what the owner withheld is not
        # managing sharing. Third parties only under this ground.
        return _err(403, _SELF_SHARE_403)
    if _structural_group_under_manage_permission(subject, admitted_by):
        # The whole tenant is not a third party.
        return _err(403, _STRUCTURAL_GROUP_SHARE_403)
    written, refused = _write_share(asset_type, pk, object_uuid, subject, role)
    if refused is not None:
        return refused
    if not written:
        # Reachable only with the outbox DISABLED (inline writes): the store
        # answered and refused, so the share does not exist whatever the
        # mirror says -- roll the mirror back and say so. With the outbox on,
        # add_share enqueues and returns True; a later rejection is parked as
        # a dead outbox row for an operator, not surfaced here.
        db.session.rollback()
        return _err(
            502,
            "the authorization store rejected this share; nothing was changed",
        )
    db.session.commit()
    audit.emit(
        audit.SHARE_ADDED,
        actor=user,
        asset_type=asset_type,
        object_id=pk,
        object_uuid=row.object_uuid if row else None,
        after={"subject": subject, "role": role},
        admitted_by=admitted_by,
    )
    logger.info(
        "superset_ownership: %s %s shared with %s as %s", asset_type, pk, subject, role
    )
    return jsonify({"object_id": pk, "subject": subject, "role": role}), 200


def _remove_asset_share(asset_type: str, pk: int, subject: str):
    """Shared DELETE /<asset_type>/<pk>/shares/<subject> handler."""
    user = _authn(mutating=True)
    if not user:
        return _err(401, "authentication required")

    asset = _get_asset(asset_type, pk)
    if not asset:
        return _err(404, _not_found_msg(asset_type))

    # The per-object write lock, first: it returns the row as stored (a
    # write route decides who may write from that, not from a copy up to a
    # cache TTL old) and is held to the commit, so this request's mirror
    # write, outbox row and commit cannot interleave with another writer's on
    # the same object. See service.lock_object.
    row = service.lock_object(asset_type, pk)
    if _is_parked(row):
        return _parked_error()
    if _is_unowned(row):
        # No owner to authorise the grant. The object keeps its visibility --
        # a public dashboard stays public -- but sharing is closed until an
        # administrator reassigns ownership.
        return _err(409, "object is unowned; assign an owner before sharing")

    admitted_by = _manage_reason(row, user)
    if admitted_by is None:
        return _err(403, _SHARES_403)
    admitted_by, refused = _sharing_ground(row, user, admitted_by)
    if refused is not None:
        return refused

    from superset import db

    object_uuid = row.object_uuid if row else str(asset.uuid)

    # DELETE used to perform no validation at all: a misspelled subject, a
    # GUID naming nobody, a group reference with its `#member` suffix dropped,
    # even the literal string "bananas" all answered 200 and emitted a
    # share_removed audit event while the real grant stayed in force. A
    # revocation is the one operation where a false success is worst.
    #
    # Issue #93: validated against the MIRROR ROW first. A subject this
    # object was actually shared with is accepted for unsharing even when
    # it no longer resolves through the identity seam -- an account
    # renamed, a GUID relocated, a group removed from the authorization
    # store since the share was made. `_validate_subject` exists to keep an
    # unvalidated string from ever reaching a grant/revoke call; a subject
    # already sitting in `ownership_share` for THIS object got there
    # through that same validation when it was granted, so re-running it
    # here only makes a share that no longer resolves permanently stuck.
    # Only a subject with NEITHER a mirror row NOR a valid identity/group
    # resolution is still a 400, exactly as before.
    #
    # One read of the share table serves both this check and `existing`
    # below (nit, review round 1): `list_shares` already selects every row
    # `list_share_rows` would, plus the store's unmirrored grants, so a
    # second, identical `ownership_share` query gained nothing.
    share_rows = service.list_shares(asset_type, pk)
    mirror_subjects = {sh["subject"] for sh in share_rows if not sh.get("unmirrored")}
    if subject in mirror_subjects:
        ok, why = True, ""
    else:
        ok, why = _validate_subject(subject, user)
    if not ok:
        return _err(400, why)
    from superset_ownership.identity import IdentityLookupError, normalize_subject

    # R-1: same second-call guard as the share route above -- fail closed
    # on the raw subject rather than let a raise escape.
    try:
        subject = normalize_subject(subject) or subject
    except IdentityLookupError:
        pass
    if _self_grant_under_manage_permission(subject, user, admitted_by):
        # The holder's own share is the owner's to change, in either
        # direction: this ground manages third parties' shares.
        return _err(403, _SELF_SHARE_403)
    if _structural_group_under_manage_permission(subject, admitted_by):
        # And so is the tenant's own group, which the holder is part of.
        return _err(403, _STRUCTURAL_GROUP_SHARE_403)
    existing = {sh["subject"] for sh in share_rows}
    if subject not in existing and object_uuid:
        if not outbox.enabled():
            # Inline mode: ask the store as well before declaring there is
            # nothing here. A grant the mirror has lost is still a grant.
            if not get_authorizer().list_relations(
                subject, f"{asset_type}:{object_uuid}"
            ):
                return _err(404, "no such share on this object")
        elif not subject.startswith("user:"):
            # Outbox on, GROUP subject, no mirror row. A queued group
            # revocation denies every non-owner on the object until it is
            # delivered (the read path cannot tell who is in the group), so a
            # revocation that is almost certainly a no-op is not worth that
            # window. A stray group tuple the mirror never knew about is an
            # administrator's `reconcile`/`purge` to clear.
            return _err(404, "no such share on this object")
        # Outbox on, USER subject, no mirror row: there is no store read on
        # the request path; the `revoke_subject` row is resolved against the
        # store at drain time, so a grant the mirror never knew about is
        # revoked all the same, and one that was never there costs a no-op
        # row that denies only that user until delivered. The subject was
        # validated above, so this cannot be a typo answered with 202.

    if not service.remove_share(asset_type, pk, object_uuid, subject):
        # Reachable only with the outbox DISABLED, where the store refused the
        # delete: reporting a revocation that did not happen tells someone
        # access was taken away while it is still in force. With the outbox
        # on, the revocation is queued and the read path denies the subject
        # until it is delivered.
        db.session.rollback()
        return _err(
            502,
            "the authorization store rejected this change; nothing was changed",
        )
    db.session.commit()
    had_row = subject in existing
    audit.emit(
        audit.SHARE_REMOVED,
        actor=user,
        asset_type=asset_type,
        object_id=pk,
        object_uuid=row.object_uuid if row else None,
        # The audit record must not say a share was removed when the mirror
        # held none: with the outbox on, that case is a revocation QUEUED
        # against whatever the store turns out to hold (usually nothing).
        before={"subject": subject, "mirror_row": had_row},
        admitted_by=admitted_by,
    )
    logger.info(
        "superset_ownership: %s %s unshared from %s%s",
        asset_type,
        pk,
        subject,
        "" if had_row else " (no mirror row; revocation queued)",
    )
    body = {"object_id": pk, "subject": subject}
    if not had_row:
        # 202: accepted, resolved at drain time. A caller that expected 404
        # for "nothing to revoke" can tell this case apart.
        body["queued"] = True
        body["mirror_row"] = False
        return jsonify(body), 202
    return jsonify(body), 200


@ownership_bp.route("/dashboards", methods=["GET"])
def list_dashboards():
    return _list_assets("dashboard")


@ownership_bp.route("/dashboard/<int:pk>", methods=["GET"])
def get_dashboard(pk: int):
    return _get_asset_detail("dashboard", pk)


@ownership_bp.route("/dashboard/<int:pk>/visibility", methods=["PUT"])
def set_visibility(pk: int):
    return _set_asset_visibility("dashboard", pk)


@ownership_bp.route("/dashboard/<int:pk>/shares", methods=["POST"])
def add_share(pk: int):
    return _add_asset_share("dashboard", pk)


@ownership_bp.route("/dashboard/<int:pk>/shares/<path:subject>", methods=["DELETE"])
def remove_share(pk: int, subject: str):
    return _remove_asset_share("dashboard", pk, subject)


@ownership_bp.route("/charts", methods=["GET"])
def list_charts():
    return _list_assets("chart")


@ownership_bp.route("/chart/<int:pk>", methods=["GET"])
def get_chart(pk: int):
    return _get_asset_detail("chart", pk)


@ownership_bp.route("/chart/<int:pk>/visibility", methods=["PUT"])
def set_chart_visibility(pk: int):
    return _set_asset_visibility("chart", pk)


@ownership_bp.route("/chart/<int:pk>/shares", methods=["POST"])
def add_chart_share(pk: int):
    return _add_asset_share("chart", pk)


@ownership_bp.route("/chart/<int:pk>/shares/<path:subject>", methods=["DELETE"])
def remove_chart_share(pk: int, subject: str):
    return _remove_asset_share("chart", pk, subject)


def _refuse_transfer(
    asset_type: str,
    pk: int,
    asset: Any,
    row: Any,
    recipient: Any,
    actor: Any,
    *,
    admitted_by: str,
) -> Optional[tuple[Response, int]]:
    """The recipient baseline check (spec section 8), shared by transfer and claim.

    Returns None when ownership may pass to `recipient`, or the error
    response to send instead. Evaluated BEFORE anything is written: a refusal
    leaves the local row, the outbox and the store exactly as they were.

    `admitted_by` is required, keyword-only: `audit.py` documents the key as
    present on every `owner_refused`, and a caller that forgot it would emit
    the event without the key and nothing would notice (`audit.build` omits
    None).

    Ownership adds to the underlying dataset grant, never replaces it:
    `raise_for_access_bypass` asserts `base_permission_holds` before the owner
    fast path, so a recipient without the grant would become the owner of an
    object they cannot open while the previous owner lost it. The check is
    evaluated for the RECIPIENT (`base_permission_holds_for`), not the caller.

    Statuses: 409 when the recipient lacks the grant or the object's dataset
    does not resolve (a conflict with the object's state); 500 when the
    check itself could not be evaluated. The latter is this instance failing
    to decide -- the check runs against Superset's own metadata database, no
    gateway is involved -- so it is reported as a server error, not as the
    502 this route uses for the authorization store refusing an inline
    write. The body is a fixed sentence either way; the exception behind an
    `unverifiable` outcome goes to the log with its traceback, never to the
    client.

    Every read the check makes -- the datasource load, the permission check,
    the recipient's display name -- is handed to `transfer.decide` to run
    under its guard. When the metadata database is what failed, each of
    them can raise; none of them may escape to Superset's generic error
    handler, whose body for a non-guest caller carries the driver message,
    the statement and its parameters.

    The refusal branch after the check touches no mapped attribute: the
    object's uuid, the recipient's id and username and the caller's id are
    read BEFORE the check, while the instances are known to be loaded. A
    statement that fails can leave the session rolled back, and a mapped
    attribute on an expired instance is then a query on a dead session --
    which would turn the documented 500 back into the generic handler's.
    Nothing on the route's path expires them today; this keeps it so
    whatever is added around the check later.
    """
    from superset_ownership import transfer
    from superset_ownership.hooks import base_permission_holds_for, dataset_resolves

    object_uuid = row.object_uuid if row else str(asset.uuid)
    prior_owner_id = row.owner_user_id if row else None
    recipient_id = recipient.id
    recipient_username = recipient.username
    actor_id = getattr(actor, "id", None)

    def display_name() -> str:
        # Only on the refusal path: the allowed path never shows the name,
        # and looking it up is a query.
        info = service.get_user_info(recipient_id)
        return info["name"] if info else recipient_username

    decision = transfer.decide(
        recipient,
        asset,
        asset_type,
        lambda user, obj: base_permission_holds_for(user, obj, asset_type),
        recipient_name=display_name,
        has_dataset=lambda: dataset_resolves(asset, asset_type),
    )
    if decision.allowed:
        return None

    # Nothing changed, and the record's shape says so: `before` and `after`
    # are the same owner; the refused write is in `attempted`.
    unchanged = {"owner_user_id": prior_owner_id}
    audit.emit(
        audit.OWNER_REFUSED,
        actor=actor,
        asset_type=asset_type,
        object_id=pk,
        object_uuid=object_uuid,
        before=unchanged,
        after=dict(unchanged),
        attempted={"owner_user_id": recipient_id, "reason": decision.outcome},
        # The rule that admitted the caller to attempt it, as on the write
        # that would have followed.
        admitted_by=admitted_by,
    )
    if decision.outcome == transfer.UNVERIFIABLE:
        # The broad catch in `transfer.decide` is the decision rule; this is
        # where the exception is seen. A programming error inside the check
        # would otherwise refuse every transfer with no trace of why. The
        # sentence names the read that raised (the datasource load or the
        # permission check itself) and whether the display-name lookup went
        # down with it; the traceback is the original exception's.
        logger.warning(
            "superset_ownership: %s %s owner -> %s refused (unverifiable; by user %s): "
            "the %s raised%s",
            asset_type,
            pk,
            recipient_id,
            actor_id,
            decision.raised,
            "; the display-name lookup raised too"
            if decision.name_lookup_raised
            else "",
            exc_info=decision.error,
        )
        return _err(500, decision.message)
    logger.info(
        "superset_ownership: %s %s owner -> %s refused (%s; by user %s): %s",
        asset_type,
        pk,
        recipient_id,
        decision.outcome,
        actor_id,
        decision.message,
    )
    return _err(409, decision.message)


def _set_asset_owner(asset_type: str, pk: int):
    """Assign or clear an object's owner.

    PUT /api/v1/ownership/{chart|dashboard}/<pk>/owner
    body: {"subject": "user:<guid>"} to assign, {"subject": null} to release

    The counterpart to the unowned guard: when an owner is deactivated the
    object becomes unshareable, and without this route there was no way back
    -- a routine event (someone leaves) bricked the object permanently.

    Owner, tenant administrator or Superset admin may call it, and a holder
    of the manage-sharing permission may TRANSFER to a third party only.
    Passing a null subject clears the owner, which is how an object is
    deliberately released; a holder may not release, and may not name
    themselves as the recipient (neither is managing sharing).

    Responses:
      200  {"object_id", "owner_user_id"}   owner changed (or released)
      400  malformed body / subject; a group as owner
      401  not authenticated
      403  caller may not manage this object; or is admitted only by the
           manage-sharing permission and asked to release, or to transfer
           to themselves
      404  object or recipient's Superset account not found
      409  ownership parked (feature disabled); recipient deactivated;
           recipient's own tenant is not the object's, or the recipient
           has no tenant role at all while the object has a tenant (a
           transfer never moves an object between tenants, and on the
           local backend a tenantless owner would untenant it); recipient
           does not hold the baseline permission (dataset grant) for this
           object; or the object's dataset does not resolve (a chart whose
           dataset is gone; a dashboard none of whose charts' datasets
           resolve) -- the transfer is refused and nothing is written
      500  the recipient's baseline permission could not be evaluated;
           nothing was changed
      502  the authorization store refused an inline write
      503  the object's tenant could not be read from the authorization
           store (the read is strict on this route); nothing was changed
    """
    user = _authn(mutating=True)
    if not user:
        return _err(401, "authentication required")

    asset = _get_asset(asset_type, pk)
    if not asset:
        return _err(404, _not_found_msg(asset_type))

    # The per-object write lock, first: it returns the row as stored (a
    # write route decides who may write from that, not from a copy up to a
    # cache TTL old) and is held to the commit, so this request's mirror
    # write, outbox row and commit cannot interleave with another writer's on
    # the same object. See service.lock_object.
    row = service.lock_object(asset_type, pk)
    if _is_parked(row):
        return _parked_error()
    admitted_by = _manage_reason(row, user)
    if admitted_by is None:
        return _err(
            403,
            "only the owner, a tenant administrator, an admin or a holder of the "
            "manage-sharing permission may assign ownership",
        )

    payload = request.get_json(silent=True) or {}
    # Releasing ownership must be stated, not implied. `subject` absent used to
    # fall through to "clear the owner" and answer 200, so any client that sent
    # a slightly wrong body -- a different key, an empty payload -- silently
    # un-owned the object, and an unowned object can no longer be shared.
    # An explicit `{"subject": null}` still releases it.
    if "subject" not in payload:
        return _err(
            400,
            "subject is required: 'user:<guid>' to assign an owner, or an "
            "explicit null to release ownership",
        )
    subject = payload.get("subject")
    if subject is None and admitted_by == MANAGE_REASON_MANAGE_PERMISSION:
        # Released, the object is ownerless: nobody can share it, and on the
        # local backend it loses its tenant with its owner, so only a
        # tenant administrator or an admin could repair it. Not a sharing
        # action.
        return _err(
            403, "the manage-sharing permission does not include releasing ownership"
        )

    new_owner_id = None
    if subject is not None:
        ok, why = _validate_subject(subject, user)
        if not ok:
            return _err(400, why)
        if not subject.startswith("user:"):
            return _err(400, "an owner must be a user, not a group")
        # Resolve exactly the way _validate_subject just did. This used to
        # re-resolve with find_user(username=...) only, so a Superset id or an
        # email that the validator had accepted came back as "no Superset
        # account for that member" for an account that plainly exists.
        from superset_ownership.identity import normalize_subject

        # I-2: `_validate_subject` just above already ran this same
        # identity lookup successfully (a raise there would have answered
        # `ok=False` and returned before this point) -- guarded again here
        # anyway rather than trusted to keep succeeding, since a plugged
        # Identity backed by a live connection (the shipped example's
        # reverse hook, a DB query) can fail on a second call the same
        # request makes moments later.
        try:
            canonical = normalize_subject(subject)
        except Exception as exc:  # noqa: BLE001 - I-2: identity seam (M-1)
            why = _identity_lookup_failed(exc)
            return _err(400, f"cannot verify the subject: the identity plug-in {why}")
        guid = canonical.split(":", 1)[1] if canonical else None
        from superset import security_manager

        candidate = security_manager.find_user(username=guid) if guid else None
        if candidate is None:
            return _err(404, "no Superset account for that member")
        if not getattr(candidate, "is_active", False):
            return _err(409, "cannot assign ownership to a deactivated account")
        if candidate.id == user.id and admitted_by == MANAGE_REASON_MANAGE_PERMISSION:
            # A claim by another name. Compared on the resolved account, not
            # the subject string, so no spelling of the caller's own
            # reference gets past it.
            return _err(
                403,
                "the manage-sharing permission does not include transferring ownership to yourself",
            )
        # A transfer never moves an object between tenants, under ANY
        # ground. The object's tenant is stamped from its owner's first
        # `tenant_<guid>` role, so a recipient whose first tenant role names
        # another tenant -- a member of several, whom `_validate_subject`
        # accepted because the store lists them in the caller's tenant --
        # would carry the object with them: the previous owner loses it,
        # this tenant's administrators can no longer manage it, the other
        # tenant's can. Not a data exposure (shares and RLS are unchanged),
        # but a control-plane move across the boundary this module keeps,
        # and one the caller cannot undo. A recipient with NO tenant role
        # is refused on the same rule: on the local backend the object's
        # tenant is its owner's, so handing it to a tenantless account
        # would untenant it (on OpenFGA the tuple would stay, but the two
        # backends must answer alike). Refused before the recipient
        # baseline check and before any write; the tenant is read strictly
        # so that an outage is a 503 here and never a stamp below. Only
        # assigning an owner to an UNTENANTED object gives it a tenant.
        obj_tenant, unreadable = _object_tenant_for_write(asset_type, row)
        if unreadable is not None:
            return unreadable
        if obj_tenant:
            from superset_ownership.identity import resolve_tenant_guid

            try:
                recipient_tenant = resolve_tenant_guid(candidate)
            except Exception as exc:  # noqa: BLE001 - I-2: identity seam (M-1)
                why = _identity_lookup_failed(exc)
                msg = (
                    f"cannot verify the recipient's tenant: the identity plug-in {why}"
                )
                return _err(400, msg)
            if service.normalize_tenant(recipient_tenant) != service.normalize_tenant(
                obj_tenant
            ):
                return _tenant_move_refusal(
                    recipient_tenant, who="the recipient", what="a transfer"
                )
        # The recipient must independently hold the baseline permission for
        # the object. Checked last, after the cheaper refusals, and before
        # ANY write.
        refused = _refuse_transfer(
            asset_type, pk, asset, row, candidate, user, admitted_by=admitted_by
        )
        if refused is not None:
            return refused
        new_owner_id = candidate.id
    else:
        obj_tenant = None

    object_uuid = row.object_uuid if row else str(asset.uuid)
    prior_owner_id = row.owner_user_id if row else None

    service.upsert_ownership(
        asset_type=asset_type,
        object_id=pk,
        object_uuid=object_uuid,
        owner_user_id=new_owner_id,
        visibility=row.visibility if row else "private",
    )

    # Ownership is a relation in the authorization store, and that is where
    # every access decision is made. Updating only the local row moved the
    # name shown in the UI while the store still named the previous owner --
    # so the new owner could not open their own object and the old one still
    # could.
    from superset_ownership.identity import member_ref

    if object_uuid:
        prior_subject = member_ref(prior_owner_id) if prior_owner_id else None
        if prior_subject:
            outbox.delete_tuple(prior_subject, "owner", f"{asset_type}:{object_uuid}")
        new_subject = member_ref(new_owner_id) if new_owner_id else None
        if new_subject and not outbox.write_tuple(
            new_subject, "owner", f"{asset_type}:{object_uuid}"
        ):
            # Reachable only with the outbox DISABLED (inline write refused).
            from superset import db as _db_rollback

            _db_rollback.session.rollback()
            return _err(
                502,
                "the authorization store rejected the new owner; nothing was changed",
            )

        # Stamp the object's tenant from the new owner ONLY when it has none:
        # when a tenant administrator assigns an owner to a previously
        # untenanted, unowned object, this is what moves it INTO their
        # tenant, so subsequent management follows normal tenant scoping. An
        # object that already has a tenant keeps it -- the recipient was
        # refused above unless theirs is the same -- and is not re-stamped.
        if new_owner_id and not obj_tenant:
            from superset import security_manager
            from superset_ownership.identity import resolve_tenant_guid

            new_owner = security_manager.get_user_by_id(new_owner_id)
            # I-2: by this point the ownership row and the `owner` tuple are
            # already written (best-effort past the point of no return for
            # this request) -- an identity failure here must not turn a
            # transfer that already happened into a 500 the caller reads as
            # "nothing changed". Logged and skipped: the object is left
            # untenanted rather than mis-stamped, exactly the state it was
            # in before this optional stamp, and a retry (or the next
            # backfill) can still set it once the plug-in recovers.
            try:
                new_tenant = resolve_tenant_guid(new_owner) if new_owner else None
            except Exception as exc:  # noqa: BLE001 - I-2: identity seam (M-1)
                _identity_lookup_failed(exc)
                new_tenant = None
            if new_tenant:
                outbox.set_object_tenant(asset_type, object_uuid, new_tenant)

    # ISSUE_80: a stock create records the creator in the object's own
    # `editors`, and Superset's own `is_editor` consults that collection
    # directly (in addition to EXTRA_EDITORS_RESOLVER's dynamic answer, which
    # already reflects the ownership row) -- so the previous owner stayed a
    # native editor, and therefore an editor, until the flush guard next
    # happened to touch this object for an unrelated reason. Same
    # transaction as the ownership row change: remove the previous owner and
    # add the new one, via the same helper `populate_subjects` uses to
    # create a Subject. See service.sync_native_editors (H-1/H-2, review
    # round 1: the native collection is the owner ONLY -- an editor share
    # stays dynamic, through EXTRA_EDITORS_RESOLVER, whether it belonged to
    # the previous owner or to anyone else).
    new_row = (
        dataclasses.replace(row, owner_user_id=new_owner_id)
        if row is not None
        else service.OwnershipRow(
            id=0,
            asset_type=asset_type,
            object_id=pk,
            object_uuid=object_uuid,
            owner_user_id=new_owner_id,
            visibility="private",
        )
    )
    before_editor_ids = _native_editor_user_ids(asset)
    # M-1 (review round 1): transactional, not best-effort -- see
    # _sync_native_editors_or_error's docstring.
    err = _sync_native_editors_or_error(
        asset, new_row, previous_owner_id=prior_owner_id
    )
    if err is not None:
        return err
    after_editor_ids = _native_editor_user_ids(asset)

    from superset import db

    db.session.commit()

    audit.emit(
        audit.OWNER_ASSIGNED,
        actor=user,
        asset_type=asset_type,
        object_id=pk,
        object_uuid=object_uuid,
        before={"owner_user_id": prior_owner_id, "editor_user_ids": before_editor_ids},
        after={"owner_user_id": new_owner_id, "editor_user_ids": after_editor_ids},
        admitted_by=admitted_by,
    )
    logger.info(
        "superset_ownership: %s %s owner -> %s (by user %s)",
        asset_type,
        pk,
        new_owner_id,
        getattr(user, "id", None),
    )
    return jsonify({"object_id": pk, "owner_user_id": new_owner_id})


@ownership_bp.route("/dashboard/<int:pk>/owner", methods=["PUT"])
def set_dashboard_owner(pk: int):
    return _set_asset_owner("dashboard", pk)


@ownership_bp.route("/chart/<int:pk>/owner", methods=["PUT"])
def set_chart_owner(pk: int):
    return _set_asset_owner("chart", pk)


def _claim_asset(asset_type: str, pk: int):
    """Assign the CALLER as owner. The self-service path behind "Assign to me".

    POST /api/v1/ownership/{chart|dashboard}/<pk>/claim   (no body)

    Owner, tenant administrator or Superset admin -- the grounds that admit
    the ownership actions -- so a tenant administrator can claim an unowned
    object without the client needing to know the caller's member GUID
    (and, since a tenant administrator does not change the sharing of an
    object they do not own, this is their way in). A holder
    of the manage-sharing permission is refused (403): taking an owned
    object from its live owner, who is neither told nor able to take it
    back, is not managing its sharing.

    The caller is the recipient here, and the same baseline check applies:
    a tenant administrator who does not hold the object's dataset grant may
    not claim it (409, nothing written). Being able to MANAGE an object is
    not the same as being able to OPEN it.

    A claim never moves an object between tenants: on a tenanted object a
    caller whose tenant role names another tenant, or who has no tenant
    role, is refused (409, nothing written); an untenanted object is stamped
    with the claimant's tenant, if they have one. The tenant is read
    strictly (503 when the store cannot answer).

    Responses: as _set_asset_owner, with the caller as the recipient.
    """
    user = _authn(mutating=True)
    if not user:
        return _err(401, "authentication required")
    asset = _get_asset(asset_type, pk)
    if not asset:
        return _err(404, _not_found_msg(asset_type))
    # The per-object write lock, first: it returns the row as stored (a
    # write route decides who may write from that, not from a copy up to a
    # cache TTL old) and is held to the commit, so this request's mirror
    # write, outbox row and commit cannot interleave with another writer's on
    # the same object. See service.lock_object.
    row = service.lock_object(asset_type, pk)
    if _is_parked(row):
        return _parked_error()
    admitted_by = _manage_reason(row, user)
    if admitted_by is None:
        return _err(
            403,
            "only the owner, a tenant administrator or an admin may claim this object",
        )
    if admitted_by == MANAGE_REASON_MANAGE_PERMISSION:
        return _err(
            403, "the manage-sharing permission does not include claiming ownership"
        )

    from superset_ownership.identity import member_ref, resolve_tenant_guid

    # As on the owner route: a claim never moves an object between tenants,
    # and the tenant is read strictly (503 when the store cannot answer).
    # Reachable only by a Superset admin: one who also carries another
    # tenant's role, or one with NO tenant role -- the ordinary local admin,
    # for whom "take it over and fix it" on a tenanted object is a transfer
    # to a tenanted member instead (PUT /owner), since on the local backend
    # a tenantless owner would untenant the object. An owner's or a tenant
    # administrator's claim is always within the object's tenant, or of an
    # object that has none.
    obj_tenant, unreadable = _object_tenant_for_write(asset_type, row)
    if unreadable is not None:
        return unreadable
    tenant = resolve_tenant_guid(user)
    if obj_tenant and service.normalize_tenant(tenant) != service.normalize_tenant(
        obj_tenant
    ):
        return _tenant_move_refusal(tenant, who="you", what="a claim")
    refused = _refuse_transfer(
        asset_type, pk, asset, row, user, user, admitted_by=admitted_by
    )
    if refused is not None:
        return refused

    object_uuid = row.object_uuid if row else str(asset.uuid)
    prior_owner_id = row.owner_user_id if row else None

    service.upsert_ownership(
        asset_type=asset_type,
        object_id=pk,
        object_uuid=object_uuid,
        owner_user_id=user.id,
        visibility=row.visibility if row else "public",
    )
    if object_uuid:
        prior_subject = member_ref(prior_owner_id) if prior_owner_id else None
        if prior_subject:
            outbox.delete_tuple(prior_subject, "owner", f"{asset_type}:{object_uuid}")
        subject = member_ref(user.id)
        if subject:
            outbox.write_tuple(subject, "owner", f"{asset_type}:{object_uuid}")
        # Stamped only when the object had no tenant: this is the rescue
        # path that moves an untenanted stray INTO the claimant's tenant.
        if tenant and not obj_tenant:
            outbox.set_object_tenant(asset_type, object_uuid, tenant)

    # ISSUE_80: same invariant as a transfer -- the previous owner, if any,
    # must not remain a native editor once someone else claims the object.
    # See the matching comment (and service.sync_native_editors) in
    # _set_asset_owner.
    new_row = (
        dataclasses.replace(row, owner_user_id=user.id)
        if row is not None
        else service.OwnershipRow(
            id=0,
            asset_type=asset_type,
            object_id=pk,
            object_uuid=object_uuid,
            owner_user_id=user.id,
            visibility="public",
        )
    )
    before_editor_ids = _native_editor_user_ids(asset)
    # M-1 (review round 1): transactional, not best-effort -- see
    # _sync_native_editors_or_error's docstring.
    err = _sync_native_editors_or_error(
        asset, new_row, previous_owner_id=prior_owner_id
    )
    if err is not None:
        return err
    after_editor_ids = _native_editor_user_ids(asset)

    from superset import db

    db.session.commit()
    audit.emit(
        audit.OWNER_ASSIGNED,
        actor=user,
        asset_type=asset_type,
        object_id=pk,
        object_uuid=object_uuid,
        before={"owner_user_id": prior_owner_id, "editor_user_ids": before_editor_ids},
        after={"owner_user_id": user.id, "editor_user_ids": after_editor_ids},
        admitted_by=admitted_by,
    )
    logger.info("superset_ownership: %s %s claimed by user %s", asset_type, pk, user.id)
    return jsonify({"object_id": pk, "owner_user_id": user.id})


@ownership_bp.route("/dashboard/<int:pk>/claim", methods=["POST"])
def claim_dashboard(pk: int):
    return _claim_asset("dashboard", pk)


@ownership_bp.route("/chart/<int:pk>/claim", methods=["POST"])
def claim_chart(pk: int):
    return _claim_asset("chart", pk)


@ownership_bp.route("/tenant/<tenant_guid>/purge", methods=["POST"])
def purge_tenant_route(tenant_guid: str):
    """Remove a tenant's ownership rows and authorization facts.

    Called from Ivanti's offboarding job. Idempotent: calling it twice, or
    calling it for a tenant that never existed, reports zero rather than
    failing. Pass ?dry_run=1 to see the counts without deleting.

    The tenant must be a GUID v4 (400 otherwise) and is lower-cased before
    anything else looks at it: OpenFGA object ids are case-sensitive, so
    the same GUID posted in upper case used to purge nothing and answer 200
    with zeros, and a tenant administrator posting it that way was told it
    was not their tenant.
    """
    user = _authn(mutating=True)
    if not user:
        return _err(401, "authentication required")

    from superset import security_manager

    if not (security_manager.is_admin() or is_tenant_administrator(user)):
        return _err(403, "only an admin or a tenant administrator may purge a tenant")

    from superset_ownership import lifecycle
    from superset_ownership.identity import resolve_tenant_guid

    try:
        tenant_guid = lifecycle.normalize_tenant_guid(tenant_guid)
    except ValueError as exc:
        return _err(400, str(exc))

    caller_tenant = resolve_tenant_guid(user)
    if not security_manager.is_admin() and caller_tenant != tenant_guid:
        return _err(403, "you may only purge your own tenant")

    dry = request.args.get("dry_run") in ("1", "true", "yes")
    counts = lifecycle.purge_tenant(tenant_guid, dry_run=dry, actor=user)
    return jsonify({"tenant": tenant_guid, "dry_run": dry, **counts})


@ownership_bp.route("/subjects", methods=["GET"])
def search_subjects():
    """Users and groups you can share with.

    Sourced from OpenFGA, not Superset. Group membership lives entirely in the
    authorization store -- Superset has no record of a "dashboard_designer"
    group and never learns of one. Superset is consulted only to put a human
    name against a member GUID, and a member with no Superset account still
    appears, labelled by GUID.

    Results are scoped to the caller's own tenant, so one tenant can never
    enumerate another's members.
    """
    user = _authn()
    if not user:
        return _err(401, "authentication required")

    q = (request.args.get("q") or "").strip().lower()

    from superset_ownership.identity import resolve_tenant_guid

    try:
        tenant = resolve_tenant_guid(user)
    except Exception as exc:  # noqa: BLE001 - I-2: identity seam (M-1)
        why = _identity_lookup_failed(exc)
        return _err(400, f"cannot verify your tenant: the identity plug-in {why}")
    if not tenant:
        # No tenant on the caller (a local admin, say). Nothing to enumerate
        # from the authorization store; return empty rather than falling back
        # to every Superset account, which would leak across tenants.
        return jsonify({"result": [], "tenant": None, "source": "openfga"})

    source = _directory_source()
    user_rows, user_degraded = _subjects_user_rows(tenant, q)
    group_rows, group_degraded = _subjects_group_rows(tenant, q, source)

    payload = {
        "result": [*user_rows, *group_rows],
        "tenant": tenant,
        "source": source,
    }
    if user_degraded or group_degraded:
        payload["degraded"] = True
    return jsonify(payload)


def _subjects_user_rows(tenant: str, q: str) -> tuple[list[dict], bool]:
    """The `/subjects` user half: `Directory.search_users`, guarded exactly
    as M-1 requires -- ANY exception a plugged Directory raises (not only
    `StoreError`: its own HTTP/LDAP client's exception, a `None`/malformed
    return the shape guard below turns into one) is caught here, logged
    with a traceback, and answered as `degraded: true`, never as an
    uncaught 500 carrying the plug-in's own text."""
    from superset_ownership.directory import USER_REF_KEYS, validate_page

    try:
        page = validate_page(
            get_directory().search_users(tenant, q, limit=200), USER_REF_KEYS
        )
    except Exception:  # noqa: BLE001 - classified as degraded either way (M-1)
        logger.exception("subjects: could not search users for tenant %s", tenant)
        return [], True
    rows = [
        {
            "value": f"user:{u['guid']}",
            "text": u["display_name"],
            # `id` is the Superset account id when the GUID is known to
            # Superset, so a client can match a row against the owner
            # block of a detail (which carries the same id) without the
            # two having to agree on the GUID's spelling.
            "extra": {
                "email": u["email"],
                "type": "user",
                "guid": u["guid"],
                "id": u["superset_id"],
            },
        }
        for u in page["items"]
    ]
    return rows, False


def _subjects_group_rows(tenant: str, q: str, source: str) -> tuple[list[dict], bool]:
    """The `/subjects` group half: `Directory.list_groups`, paged (a
    runaway store must not hang the request), M-1-guarded like the user
    half above, and re-scoped to the caller's tenant in OUR code (M-2:
    contract §5 says another tenant's rows are "discarded, never
    surfaced" -- the plug-in is not trusted to have scoped its own answer;
    `_fast_groups` applies this same filter internally, this applies it
    uniformly for every configured Directory)."""
    from superset_ownership.directory import GROUP_REF_KEYS, validate_page
    from superset_ownership.identity import (
        group_belongs_to_tenant,
        is_structural_tenant_role,
    )

    groups: list[dict] = []
    cursor = None
    degraded = False
    for _ in range(20):  # cap: a runaway store must not hang the request
        try:
            grp_page = validate_page(
                get_directory().list_groups(tenant, cursor=cursor), GROUP_REF_KEYS
            )
        except Exception:  # noqa: BLE001 - classified as degraded either way (M-1)
            logger.exception("subjects: could not list groups for tenant %s", tenant)
            degraded = True
            break
        groups.extend(grp_page["items"])
        cursor = grp_page["next_cursor"]
        if not cursor:
            break

    rows = []
    foreign_dropped = 0
    for grp in groups:
        gid = grp.get("id")
        if not group_belongs_to_tenant(gid, tenant) or grp.get("tenant") != tenant:
            foreign_dropped += 1
            continue
        if q and q not in grp["display_name"].lower():
            continue
        rows.append(
            {
                "value": f"{grp['id']}#member",
                "text": grp["display_name"],
                "extra": {
                    "type": "group",
                    "members": grp["members"],
                    "in_superset": False,
                    # The tenant's own membership or administrator group
                    # -- a population, not a third party. A holder of the
                    # manage-sharing permission may not share to or
                    # unshare either (`_structural_group_under_manage_
                    # permission`), and the server says which rows those
                    # are so the client does not re-derive the group-id
                    # format (OWNERSHIP_GROUP_ID_FORMAT) to lock them.
                    "structural": is_structural_tenant_role(
                        grp["id"].split(":", 1)[-1]
                    ),
                },
            }
        )
    if foreign_dropped:
        logger.warning(
            "subjects: directory %r answered %d group(s) outside tenant %s; dropped",
            source,
            foreign_dropped,
            tenant,
        )
    return rows, degraded


def _directory_source() -> str:
    """The configured directory's identity, for the `/subjects` payload's
    `source` field -- `local`/`openfga` for the two shipped classes, or the
    plugged class's dotted path (`describe()["directory"]["alias"]`),
    truthfully naming whatever is actually answering the request rather
    than the built-in `openfga` label regardless of what is plugged (I-4:
    this used to read a `describe()` key -- `alias` -- that did not exist,
    so the `KeyError` was always swallowed and this always fell through to
    the same-shaped fallback below, which only coincidentally reads
    `openfga` when the plugged class defines no `name` of its own).
    `describe()`'s `source` key is a different thing (N1: which config
    layer resolved `OWNERSHIP_DIRECTORY` -- config/environment/default) and
    must not be read here. Falls back to the directory instance's own
    `name` if `plugins.describe()` itself raises (a bare-`Authorizer` seam
    with no registry, or before it is fully wired).

    N-3: when `OWNERSHIP_USERS_OF_TENANT`/`OWNERSHIP_GROUPS_OF_TENANT` are
    configured, the ROWS this payload lists were answered by the hook, not
    by the directory class the alias above names -- reporting the class
    would misname what actually served the request. Reported as
    `hook:<setting path>` instead (both, joined, when both are configured
    and differ); a hook set as a bare callable (no dotted path to show)
    reports `hook:<setting name>`.
    """
    try:
        from superset_ownership.plugins import describe

        info = describe()
        hooks = info.get("hooks", {})
        hook_paths: list[str] = []
        for setting in ("OWNERSHIP_USERS_OF_TENANT", "OWNERSHIP_GROUPS_OF_TENANT"):
            entry = hooks.get(setting, {})
            if entry.get("source") == "config":
                hook_paths.append(entry.get("path") or setting)
        if hook_paths:
            return "hook:" + ",".join(dict.fromkeys(hook_paths))
        return info["directory"]["alias"]
    except Exception:  # noqa: BLE001 - plugins.py may not exist on this branch yet
        return getattr(get_directory(), "name", "openfga")

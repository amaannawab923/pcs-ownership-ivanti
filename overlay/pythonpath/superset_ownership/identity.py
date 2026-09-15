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
"""Resolving a Superset session to the identity OpenFGA is keyed on.

Ivanti's authorization facts are keyed on GUID v4 for both tenant and member
("Tenant ID format is GUID (v4)... Owner ID/Member ID is also GUID (v4)"),
while Superset knows users by an integer primary key. Every tuple we read or
write has to be expressed in their identifiers, so this module is the single
place that translates.

INFERRED, NOT CONFIRMED -- see `_default_member_guid` and
`_default_tenant_guid` for the reasoning and what to ask Ivanti. Both are
isolated here so a wrong guess costs one function rather than a rewrite.

Overriding
----------
The translation above is one deployment's guess, not a fact every deployment
shares, so it is expressed as an `Identity` plug-in rather than baked into
the callers. `OWNERSHIP_IDENTITY = "pkg.mod:Class"` names a class satisfying
the `Identity` protocol below; the loader (`superset_ownership.plugins`)
resolves it once at startup and every caller in this package reaches it
through `get_identity()` -- callers never import a concrete class.

The straightforward override is a subclass of `DefaultIdentity` that
replaces only the method that differs -- most often `member_guid`, when the
member GUID lives on a user attribute or a JWT claim rather than the
username -- and inherits the rest unchanged:

    from superset_ownership.identity import DefaultIdentity

    class AttributeIdentity(DefaultIdentity):
        protocol_version = 1

        def member_guid(self, user):
            guid = (getattr(user, "extra_attributes", None) or {}).get(
                "member_guid"
            )
            return guid or super().member_guid(user)

An override that RELOCATES the GUID this way must also override
`user_for_member_guid` -- the reverse of `member_guid` -- or a subject the
picker itself emits (`user:<the relocated GUID>`) cannot be validated back
to an account (H-3, `qa/reviews/review-plugin-pr90-part2.md`). Both
directions are one fact about one deployment; an override that answers only
`member_guid` has told this module how to go account -> GUID but not GUID ->
account, and `normalize_subject` needs both because a share is written
under one and read back under the other:

    class AttributeIdentity(DefaultIdentity):
        protocol_version = 1

        def member_guid(self, user):
            guid = (getattr(user, "extra_attributes", None) or {}).get(
                "member_guid"
            )
            return guid or super().member_guid(user)

        def user_for_member_guid(self, guid):
            from superset import db, security_manager

            user_model = security_manager.user_model
            user = (
                db.session.query(user_model)
                .filter(user_model.extra_attributes.op("->>")("member_guid") == guid)
                .first()
            )
            return user or super().user_for_member_guid(guid)

`normalize_subject` is deliberately NOT something an override is expected to
reimplement: its body is the only place in this module that queries
Superset (`security_manager`), and it resolves a subject by calling back
through `member_ref` -> `resolve_member_guid` -> `get_identity().member_guid`
for the account -> GUID direction, and through `self.user_for_member_guid`
for the GUID -> account direction, so an override that only replaces
`member_guid` (and, when it relocates the GUID, `user_for_member_guid`) gets
a working `normalize_subject` for free by inheriting `DefaultIdentity`
rather than implementing `Identity` from nothing.
"""

from __future__ import annotations

import functools
import logging
import re
import string
from typing import Any, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

GUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
    re.IGNORECASE,
)

# Role naming convention carrying the tenant. Ivanti's management service
# "upserts a per-tenant RLS rule bound to the tenant role", so the tenant role
# is where tenancy already lives; we read the GUID back out of its name.
TENANT_ROLE_PREFIX = "tenant_"


def tenant_role(tenant_guid: str) -> str:
    """The FAB role name that makes an account a member of the tenant."""
    return f"{TENANT_ROLE_PREFIX}{tenant_guid}"


# --- group ids ---------------------------------------------------------------
#
# A group lives only in the authorization store, keyed `group:<id>`, and its
# id carries the tenant so two tenants' "dashboard_designer" groups can never
# collide. WHERE the tenant goes is not settled: the SOW says group ids are
# "tenant-prefixed", the pre-configuration shape puts the tenant LAST (every
# existing tuple is in it), and the directory-hook specification that would
# decide it is outstanding. So it is one setting, OWNERSHIP_GROUP_ID_FORMAT,
# and every place that builds or takes apart a group id goes through the
# helpers below; nothing else may concatenate a name and a tenant.
#
#   "{name}_{tenant}"   the pre-configuration shape (the default):
#                        dashboard_designer_<guid>
#   "{tenant}_{name}"   the SOW's shape:
#                        <guid>_dashboard_designer
#
# Both halves are constrained so that `group_id` and `split_group_id` are a
# round trip: the tenant must be a GUID v4 (the parse anchors on that shape;
# any other tenant would build an id nothing can read back), and the name may
# not contain whitespace, `:` or `#` (OpenFGA refuses such object ids, and
# `#` and `:` are the reference delimiters the parser strips).
#
# TO CONFIRM WITH IVANTI: which one does the directory hook write?
GROUP_ID_FORMAT_SETTING = "OWNERSHIP_GROUP_ID_FORMAT"
GROUP_ID_FORMAT_SUFFIX = "{name}_{tenant}"
GROUP_ID_FORMAT_PREFIX = "{tenant}_{name}"
GROUP_ID_FORMAT_DEFAULT = GROUP_ID_FORMAT_SUFFIX

# The group whose members administer a tenant, before the tenant is applied.
TENANT_ADMINISTRATOR_GROUP = "tenant_administrator"


class IdentityLookupError(Exception):
    """Marker raised by `_default_normalize_subject`'s reverse-hook branch
    when an override's `user_for_member_guid`/`member_guid` raises (R-1).

    Carries ONLY the failing exception's class name (`cls_name`) -- never
    its message, which can carry a connection string or a bind password,
    the same rule `api.py`'s `_identity_lookup_failed`/`_cannot_verify_
    tenant_membership` already apply at the Directory seam (M-1). `api.py`'s
    route-level guard around `_validate_user_subject_unguarded` (the ONE
    caller that needs to tell an identity-plug-in outage from a subject
    that simply names nobody) catches this and classifies it into the fixed
    "cannot verify the subject" sentence, using `cls_name` in place of the
    raw exception's own class so the sentence still names the ORIGINAL
    failure (e.g. `RuntimeError`), not this wrapper. Every other caller of
    `normalize_subject` -- `hooks.py`'s editor-share resolution, the two
    `subject = normalize_subject(subject) or subject` call sites in
    `api.py` -- must keep treating a raise here exactly like the "no
    candidate" answer `_default_normalize_subject` used to give directly:
    fail closed, never let this propagate past those sites.
    """

    def __init__(self, cls_name: str) -> None:
        super().__init__(cls_name)
        self.cls_name = cls_name


class GroupIdFormatError(ValueError):
    """OWNERSHIP_GROUP_ID_FORMAT is not a usable template.

    A ValueError, and deliberately NOT a warn-and-use-the-default: a cache TTL
    typo costs performance, a group-format typo writes every group tuple in
    a shape nothing else reads, so the store and Superset silently disagree
    about who is in which group. That is exactly what the setting exists to
    prevent, so a bad value stops the process instead.
    """


def parse_group_id_format(raw: Any) -> tuple[str, str, str, str]:
    """Validate a group-id template; return (leading, first, separator, trailing).

    `first` is "name" or "tenant" -- whichever placeholder comes first; the
    other follows the separator. The template must name `{name}` and
    `{tenant}` exactly once each, have a non-empty literal between them (a
    tenant GUID glued straight onto a name could not be taken apart again),
    and contain no other placeholder. Literal text before or after the pair
    is allowed. None or blank means the default.

    Raises GroupIdFormatError, with the offending value in the message, so a
    startup check can fail loudly rather than the first share request.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raw = GROUP_ID_FORMAT_DEFAULT
    if not isinstance(raw, str):
        raise GroupIdFormatError(
            f"{GROUP_ID_FORMAT_SETTING} must be a string template such as "
            f"{GROUP_ID_FORMAT_SUFFIX!r}, got {raw!r}"
        )
    return _parse_template(raw.strip())


@functools.lru_cache(maxsize=8)
def _parse_template(template: str) -> tuple[str, str, str, str]:
    try:
        pieces = list(string.Formatter().parse(template))
    except ValueError as exc:
        raise GroupIdFormatError(
            f"{GROUP_ID_FORMAT_SETTING}={template!r} is not a valid template: {exc}"
        ) from exc

    # Formatter.parse yields (literal, field, spec, conversion); the literal
    # PRECEDES its field, and a trailing literal comes with field None.
    literals: list[str] = []
    fields: list[str] = []
    for literal, field, spec, conversion in pieces:
        literals.append(literal)
        if field is None:
            continue
        if spec or conversion:
            raise GroupIdFormatError(
                f"{GROUP_ID_FORMAT_SETTING}={template!r}: placeholders take no "
                f"format spec or conversion"
            )
        fields.append(field)

    if sorted(fields) != ["name", "tenant"]:
        raise GroupIdFormatError(
            f"{GROUP_ID_FORMAT_SETTING}={template!r} must contain {{name}} and "
            f"{{tenant}} exactly once each and nothing else in braces "
            f"(found: {fields or 'no placeholders'})"
        )
    leading, separator = literals[0], literals[1]
    trailing = literals[2] if len(literals) > 2 else ""
    if not separator:
        raise GroupIdFormatError(
            f"{GROUP_ID_FORMAT_SETTING}={template!r} needs a separator between "
            f"{{{fields[0]}}} and {{{fields[1]}}}; a name and a tenant glued "
            f"together cannot be split apart again"
        )
    return leading, fields[0], separator, trailing


def group_id_format() -> str:
    """The configured template: one precedence (settings.get), like every
    other OWNERSHIP_* key. Read per call, like OWNERSHIP_AUTHORIZER, so the
    seed scripts and a running instance answer from the same source;
    validated on every read (the parse is cached per value).

    settings.get() only catches SettingError; GroupIdFormatError is a
    different exception and is NOT caught here or by settings.get -- a bad
    template is policy, not a warn-and-default typo (see the POLICY,
    DELIBERATE comment in superset_config_docker_light.py), so it raises
    and stops the process exactly as it always has.
    """
    from superset_ownership import settings

    raw = settings.get(GROUP_ID_FORMAT_SETTING, None, settings.as_str)
    parse_group_id_format(raw)  # raises on a bad value
    # Report the template the parse resolved: blank or whitespace-only means
    # the default, so that is what is in force, not the empty string.
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return GROUP_ID_FORMAT_DEFAULT


# Characters a group NAME may not contain. OpenFGA rejects object ids with
# whitespace, `:` or `#`, and `split_group_id` strips `group:` and `#member`
# from a reference, so a name carrying either delimiter could not be told
# apart from the reference syntax around it.
_GROUP_NAME_FORBIDDEN_RE = re.compile(r"[\s:#]")


def _get_hook(name: str) -> Optional[Any]:
    """The resolved `OWNERSHIP_*` hook callable, or None -- mirrors
    `_get_identity`'s lazy-import fallback so this module keeps working
    standalone (spec §4.5, plugin_hooks.py/plugins.py may not be on this
    branch yet, or a registry may not have loaded)."""
    try:
        from superset_ownership.plugins import get_hook as _plugins_get_hook
    except ImportError:
        return None
    return _plugins_get_hook(name)


def group_id(name: str, tenant: str) -> str:
    """The store's id for group `name` in `tenant`, per the configured format
    or, when `OWNERSHIP_GROUP_ID` is set, the configured shape hook (§4.5.2 --
    pure, no caching needed; a raise falls back to the format-string default
    rather than a fixed "unknown", since callers need SOME id back)."""
    hook = _get_hook("OWNERSHIP_GROUP_ID")
    if hook is not None:
        from superset_ownership import plugin_hooks

        return plugin_hooks.call_or_default(
            hook, "OWNERSHIP_GROUP_ID", (name, tenant), _default_group_id
        )
    return _default_group_id(name, tenant)


def _default_group_id(name: str, tenant: str) -> str:
    """Raises ValueError for anything `split_group_id` could not read back: a
    missing half, a tenant that is not a GUID v4, or a name containing
    whitespace, `:` or `#`. The helper is the only place that may combine a
    name and a tenant, so it must never build an id the module cannot parse.
    """
    if not name or not tenant:
        raise ValueError("a group id needs both a name and a tenant")
    if not GUID_RE.fullmatch(tenant):
        raise ValueError(f"tenant must be a GUID v4, got {tenant!r}")
    if _GROUP_NAME_FORBIDDEN_RE.search(name):
        raise ValueError(
            f"group name may not contain whitespace, ':' or '#', got {name!r}"
        )
    leading, first, separator, trailing = parse_group_id_format(group_id_format())
    parts = {"name": name, "tenant": tenant.lower()}
    second = "tenant" if first == "name" else "name"
    return f"{leading}{parts[first]}{separator}{parts[second]}{trailing}"


@functools.lru_cache(maxsize=8)
def _group_id_re(template: str) -> re.Pattern[str]:
    leading, first, separator, trailing = parse_group_id_format(template)
    guid = GUID_RE.pattern
    # The GUID is a fixed shape, so it anchors the split: the name is
    # whatever is on the other side of the separator from it. Splitting on
    # the separator itself would break on any name containing one
    # ("dashboard_designer").
    if first == "tenant":
        body = f"(?P<tenant>{guid}){re.escape(separator)}(?P<name>.+?)"
    else:
        body = f"(?P<name>.+?){re.escape(separator)}(?P<tenant>{guid})"
    return re.compile(
        f"^{re.escape(leading)}{body}{re.escape(trailing)}$", re.IGNORECASE | re.DOTALL
    )


def _bare_group_id(group_id_or_ref: Optional[str]) -> Optional[str]:
    """`<id>` from any of `<id>`, `group:<id>`, `group:<id>#member`."""
    if not group_id_or_ref:
        return None
    text = group_id_or_ref
    if text.startswith("group:"):
        text = text[len("group:") :]
        text = text.split("#", 1)[0]
    return text or None


def split_group_id(group_id_or_ref: Optional[str]) -> Optional[tuple[str, str]]:
    """(name, tenant) for a group id or reference, or None if it is not one
    in the configured format (or, when `OWNERSHIP_SPLIT_GROUP_ID` is set,
    what the configured shape hook answers -- the inverse of `group_id`,
    §4.5.2). Accepts `<id>`, `group:<id>` and `group:<id>#member`."""
    hook = _get_hook("OWNERSHIP_SPLIT_GROUP_ID")
    if hook is not None:
        from superset_ownership import plugin_hooks

        return plugin_hooks.call_or_default(
            hook,
            "OWNERSHIP_SPLIT_GROUP_ID",
            (group_id_or_ref,),
            _default_split_group_id,
        )
    return _default_split_group_id(group_id_or_ref)


def _default_split_group_id(
    group_id_or_ref: Optional[str],
) -> Optional[tuple[str, str]]:
    """The tenant comes back lower-cased, as `resolve_tenant_guid` reports
    it."""
    bare = _bare_group_id(group_id_or_ref)
    if bare is None:
        return None
    m = _group_id_re(group_id_format()).match(bare)
    if not m:
        return None
    return m.group("name"), m.group("tenant").lower()


def group_object(name: str, tenant: str) -> str:
    """`group:<id>` -- the OBJECT form, what a membership check or a
    directory write names."""
    return f"group:{group_id(name, tenant)}"


def group_ref(name: str, tenant: str) -> str:
    """`group:<id>#member` -- the SUBJECT form a share is granted to."""
    return f"{group_object(name, tenant)}#member"


def group_belongs_to_tenant(
    group_id_or_ref: Optional[str], tenant: Optional[str]
) -> bool:
    """Is this group (id, object or subject reference) one of `tenant`'s?

    False for anything that does not parse in the configured format, and
    for a missing tenant: a group of unknown tenancy is nobody's.
    """
    if not tenant:
        return False
    parsed = split_group_id(group_id_or_ref)
    return parsed is not None and parsed[1] == tenant.lower()


def group_display_name(group_id_or_ref: str) -> str:
    """The name without its tenant, for the UI (or, when
    `OWNERSHIP_GROUP_DISPLAY_NAME` is set, the configured shape hook's
    answer, §4.5.2); the input as given when it does not parse (a group
    seeded outside the convention still has to render as something)."""
    hook = _get_hook("OWNERSHIP_GROUP_DISPLAY_NAME")
    if hook is not None:
        from superset_ownership import plugin_hooks

        return plugin_hooks.call_or_default(
            hook,
            "OWNERSHIP_GROUP_DISPLAY_NAME",
            (group_id_or_ref,),
            _default_group_display_name,
        )
    return _default_group_display_name(group_id_or_ref)


def _default_group_display_name(group_id_or_ref: str) -> str:
    parsed = split_group_id(group_id_or_ref)
    if parsed:
        return parsed[0]
    return _bare_group_id(group_id_or_ref) or group_id_or_ref


def tenant_administrator_group(tenant: str) -> str:
    """`group:<id>` of the group whose members administer `tenant` (or, when
    `OWNERSHIP_TENANT_ADMINISTRATOR_GROUP` is set, the configured shape
    hook's answer, §4.5.2)."""
    hook = _get_hook("OWNERSHIP_TENANT_ADMINISTRATOR_GROUP")
    if hook is not None:
        from superset_ownership import plugin_hooks

        return plugin_hooks.call_or_default(
            hook,
            "OWNERSHIP_TENANT_ADMINISTRATOR_GROUP",
            (tenant,),
            _default_tenant_administrator_group,
        )
    return _default_tenant_administrator_group(tenant)


def _default_tenant_administrator_group(tenant: str) -> str:
    return group_object(TENANT_ADMINISTRATOR_GROUP, tenant)


def is_structural_tenant_role(name: str) -> bool:
    """Is this name exactly a tenant's membership or administrator role?

    Exactly: the membership role `tenant_<guid>`, or the administrator
    group's id in the configured OWNERSHIP_GROUP_ID_FORMAT
    (`tenant_administrator_<guid>` by default), and nothing around either.
    Either names a POPULATION rather than an entitlement, which is why
    neither may be the manage-sharing role and neither may be shared to
    under that permission. Any other name carrying a GUID -- how a
    directory group is spelled as a role -- is an ordinary role. Case-
    insensitive, as `resolve_tenant_guid` and `split_group_id` are.
    """
    if not name:
        return False
    bare = name.strip()
    guid = _first_guid(bare)
    if not guid:
        return False
    if bare.lower() == tenant_role(guid):
        return True
    parsed = split_group_id(bare)
    return parsed is not None and parsed[0].lower() == TENANT_ADMINISTRATOR_GROUP


def _first_guid(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    m = GUID_RE.search(text)
    return m.group(0).lower() if m else None


# --- the Identity protocol ----------------------------------------------------


@runtime_checkable
class Identity(Protocol):
    """What OpenFGA needs to know about a Superset session: who they are
    (`member_guid`), what tenant they act in (`tenant_guid`), what to call
    them (`display_name`), how to read a subject reference someone typed,
    pasted or sent through the API (`normalize_subject`), and the reverse
    of `member_guid` -- the Superset account a member GUID names
    (`user_for_member_guid`, H-3).

    A Protocol, not a base class: an override satisfies it by having these
    five methods and a `protocol_version`, however it is built. See the
    module docstring for the expected override shape (subclass
    `DefaultIdentity`, replace what differs).
    """

    protocol_version: int

    def member_guid(self, user: Any) -> Optional[str]:
        """The id OpenFGA knows this Superset user by, or None for no user."""
        ...

    def tenant_guid(self, user: Any) -> Optional[str]:
        """The tenant this Superset user acts in, or None if it has none."""
        ...

    def display_name(self, user: Any) -> str:
        """A human name for this Superset user, for the UI."""
        ...

    def user_for_member_guid(self, guid: str) -> Any:
        """The Superset account this member GUID names, or None.

        The reverse of `member_guid` (H-3): an override that relocates the
        GUID off username/email -- onto an attribute, a JWT claim, anything
        -- must implement this too, or a subject the picker itself emits
        (keyed by that GUID, `directory.py`'s `_user_labels` /
        `search_users`) can never be validated back to an account by
        `normalize_subject`. The default queries `security_manager` by
        username, then email -- the same two fields `_default_member_guid`
        reads from -- so `DefaultIdentity` round-trips without either
        method needing to be overridden.
        """
        ...

    def normalize_subject(self, raw: str) -> Optional[str]:
        """The canonical `user:<guid>` / `group:<id>[#member]` reference for
        whatever identifier `raw` carries, or None if it names nobody."""
        ...


def _default_member_guid(user: Any) -> Optional[str]:
    """The member GUID for a Superset user.

    ASSUMPTION: the GUID is carried on the username, set when Neurons
    just-in-time provisions the account. Nothing in the SOW states this; it is
    inferred from users being JIT-provisioned by a system that already knows
    the member GUID, and username/email being the fields it must populate.

    Falls back to email, then to the integer id prefixed so it is obviously
    not a GUID -- a local instance with ordinary usernames still works, it
    just is not speaking Ivanti's identifiers.

    TO CONFIRM WITH IVANTI: is the member GUID the Superset username?
    """
    if user is None:
        return None
    guid = _first_guid(getattr(user, "username", None)) or _first_guid(
        getattr(user, "email", None)
    )
    if guid:
        return guid
    return f"local-{user.id}"


def _default_tenant_guid(user: Any) -> Optional[str]:
    """The tenant GUID for a Superset user.

    ASSUMPTION: read from the user's tenant-specific role. This one is well
    supported -- asked directly how Superset absorbs tenant information, the
    answer was "It is through RLS, and we have tenant-specific roles", and
    Ivanti's own RLS rule is described as "bound to the tenant role".

    Their `{{ device_scope_filter() }}` macro must already do exactly this
    resolution at query-build time. If they share it, replace the body of this
    function with whatever it does and the guess disappears.

    TO CONFIRM WITH IVANTI: can we see device_scope_filter()?
    """
    if user is None:
        return None
    for role in getattr(user, "roles", []) or []:
        name = getattr(role, "name", "") or ""
        # Exactly `tenant_<guid>`. Matching any role with "tenant" in its name
        # also matched `tenant_administrator_<guid>`, so granting someone the
        # administrator role on the local backend silently made them a member
        # of that tenant -- two orthogonal facts riding on one string.
        if name.lower().startswith(TENANT_ROLE_PREFIX):
            guid = _first_guid(name)
            if guid and name.lower() == f"{TENANT_ROLE_PREFIX}{guid}":
                return guid
    return None


def _default_display_name(user: Any) -> str:
    """First and last name, joined and trimmed; the username when neither is
    set. `service.py`'s and `api.py`'s former inline copies of this line
    both fall back to whatever string looked the user up in the first place,
    which is the username on every path that reaches them -- see
    `test_identity.py` for the pinned equivalence."""
    return f"{user.first_name or ''} {user.last_name or ''}".strip() or user.username


def _guid_matches(computed: Optional[str], expected: str) -> bool:
    """Case-normalised equality for a GUID pair (N-2). `resolve_member_guid`
    lower-cases a GUID-shaped value (`_first_guid`); an override need not,
    so this compares case-insensitively rather than assuming it does."""
    return computed is not None and computed.lower() == expected.lower()


def _default_user_for_member_guid(guid: str) -> Any:
    """`DefaultIdentity`'s reverse lookup (H-3): the account whose username
    or email carries this GUID -- the same two fields `_default_member_guid`
    reads from, so the pair round-trips without either needing an override.
    An override that relocates the GUID (an attribute, a JWT claim) has to
    replace this too; see the module docstring."""
    if not guid:
        return None
    from superset import security_manager

    user = security_manager.find_user(username=guid)
    if user is None:
        user = security_manager.find_user(email=guid)
    return user


def _trust_if_forward_matches(self: "DefaultIdentity", candidate: Any, ref: str) -> Any:
    """`candidate` if `self.member_guid(candidate)` agrees with `ref`
    (`_guid_matches`), else `None`. This is the forward-mapping check
    (N-2) `_default_normalize_subject` applies to every account candidate
    it is asked to trust rather than one it looked up directly by id --
    the reverse hook's own candidate (H-3), and, under a configured
    identity, the username/email candidate too (R2-M1) -- so an
    inconsistent hook cannot resolve whatever was asked for to an
    unrelated account. A raising `member_guid` is reclassified into
    `IdentityLookupError`, carrying only the exception's class name, for
    `_validate_user_subject` (via `_identity_lookup_failed`) to turn into
    the fixed "cannot verify the subject" sentence rather than crash or
    leak the hook's own text (a connection string, a bind password)."""
    if candidate is None:
        return None
    try:
        matches = _guid_matches(self.member_guid(candidate), ref)
    except Exception as exc:
        logger.exception(
            "identity: forward hook (member_guid) failed while validating %r",
            ref,
        )
        raise IdentityLookupError(exc.__class__.__name__) from None
    return candidate if matches else None


def _default_normalize_subject(self: "DefaultIdentity", subject: str) -> Optional[str]:
    """The canonical reference for a subject, or None if it names nobody.

    Callers name users in whichever identifier they hold: Ivanti's UI passes
    the member GUID, a script or a standalone instance passes the Superset
    integer id. Both must end up as the SAME string, because a grant is
    written under one reference and the access check asks under another --
    and a share written as "user:2" while the check asks for "user:local-2"
    grants nothing while reporting success.

    Groups pass through: they exist only in the authorization store, so there
    is nothing to resolve them against.

    The only place in this module that queries Superset (`security_manager`)
    for the account -> GUID direction: an override need not reimplement this
    method to change `member_guid`, because it resolves a subject by calling
    back through `member_ref` -> `resolve_member_guid` ->
    `get_identity().member_guid`, which reaches whichever `Identity` is
    currently loaded. The GUID -> account direction goes through
    `self.user_for_member_guid`, consulted only after the username/email
    lookups miss (H-3): an override that relocates the GUID off those two
    fields is offered the chance to answer before this method gives up.
    """
    from superset import security_manager

    if not subject or ":" not in subject:
        return None
    kind, _, rest = subject.partition(":")
    ref = rest.split("#", 1)[0]

    if kind == "group":
        return subject
    if kind != "user":
        return None

    if ref.isdigit():
        user = security_manager.get_user_by_id(int(ref))
    else:
        user = security_manager.find_user(username=ref)
        if user is None:
            user = security_manager.find_user(email=ref)
        if user is not None and type(self) is not DefaultIdentity:
            # R2-M1: under a configured identity -- a `member_guid` hook
            # (this method is only ever invoked as `self` = the
            # `HookedIdentity` wrapper when one of the pair is set) or a
            # non-default `Identity` class -- the username/email lookup is
            # no longer guaranteed to BE the member GUID, so its candidate
            # must clear the same forward-mapping check already applied to
            # the reverse hook's candidate below (rule 3, negative
            # direction): an account only resolves here when its own
            # `member_guid` agrees with what was asked for. Under the plain
            # `DefaultIdentity` this is a no-op -- `_default_member_guid`
            # reads the same username/email fields this lookup just used,
            # so the two always agree -- which is why this branch is
            # skipped there rather than merely redundant.
            user = _trust_if_forward_matches(self, user, ref)
        if user is None:
            # The picker's own value under an override that relocated the
            # GUID: neither username nor email carries it (or the
            # forward-mapping check just above rejected it), so ask the
            # reverse hook before giving up (H-3); `_trust_if_forward_
            # matches` applies the same N-2 check to its answer.
            #
            # I-2/R-1: an override's `user_for_member_guid`/`member_guid`
            # can raise -- the shipped example's reverse hook is a DB query
            # behind a lazily created table, so a first-boot permissions
            # error is a realistic trigger. This used to be swallowed here
            # exactly like "no candidate" -- `user` stayed None and this
            # function returned None -- which kept the plug-in's own text
            # (a connection string, a bind password) from ever escaping,
            # but also meant the class name of the ORIGINAL failure was
            # lost before it reached `api.py`'s route-level guard: an
            # admitted owner read the outage as "subject does not name a
            # known account" rather than "cannot verify the subject" (R-1).
            # So this branch now re-raises `IdentityLookupError`, carrying
            # ONLY `exc`'s class name -- never its message -- for that
            # guard (`_validate_user_subject`, via `_identity_lookup_
            # failed`) to classify into the fixed "cannot verify" sentence.
            # Every OTHER caller of `normalize_subject` (`hooks.py`, the two
            # `... or subject` sites in `api.py`) still has to fail closed
            # on this, same as before -- see `IdentityLookupError`'s
            # docstring.
            try:
                candidate = self.user_for_member_guid(ref)
            except Exception as exc:
                logger.exception(
                    "identity: reverse hook (user_for_member_guid) failed "
                    "while resolving %r",
                    ref,
                )
                raise IdentityLookupError(exc.__class__.__name__) from None
            user = _trust_if_forward_matches(self, candidate, ref)
    if user is None:
        return None
    return member_ref(user)


class DefaultIdentity:
    """Today's identity resolution, unchanged: GUID-bearing username or
    email for `member_guid`, the `tenant_<guid>` role for `tenant_guid`,
    first/last name for `display_name`, and a Superset lookup by id,
    username or email for `normalize_subject`. The default `OWNERSHIP_IDENTITY`
    plug-in, and the base class an override is expected to subclass."""

    protocol_version = 1

    member_guid = staticmethod(_default_member_guid)
    tenant_guid = staticmethod(_default_tenant_guid)

    def display_name(self, user: Any) -> str:
        return _default_display_name(user)

    def user_for_member_guid(self, guid: str) -> Any:
        return _default_user_for_member_guid(guid)

    def normalize_subject(self, raw: str) -> Optional[str]:
        # Not a staticmethod (unlike member_guid/tenant_guid above): H-3's
        # reverse hook needs `self` so an override's `user_for_member_guid`
        # is the one consulted, not always DefaultIdentity's.
        return _default_normalize_subject(self, raw)


# Alias table for OWNERSHIP_IDENTITY (superset_ownership.plugins.resolve_class_ref).
_IDENTITIES: dict[str, Any] = {"default": DefaultIdentity}

# Fallback used only when superset_ownership.plugins (spec §3/§4) is not on
# this branch, or has not loaded a registry yet: a bare, unconfigurable
# DefaultIdentity so this module works standalone and every facade below has
# something to call.
_default_identity = DefaultIdentity()


def _get_identity() -> Identity:
    """The active `Identity` plug-in.

    Lazily imports `superset_ownership.plugins` (owned by spec §3/§4's
    loader/registry) and defers to its `get_identity()` accessor, which
    reads the loaded registry (or a no-app-context fallback built from
    settings). Falls back to a module-level `DefaultIdentity()` singleton
    when `plugins` cannot be imported, so this module and every facade
    below work standalone before that module lands.
    """
    try:
        from superset_ownership.plugins import get_identity as _plugins_get_identity
    except ImportError:
        return _default_identity
    return _plugins_get_identity()


# --- facades -------------------------------------------------------------
#
# Public names and signatures unchanged from before the Identity protocol
# existed, so every one of the ~45 existing call sites needs no edit: each
# keeps calling the same module function it always has, and that function
# delegates to whichever Identity is loaded.


def resolve_member_guid(user: Any) -> Optional[str]:
    return _get_identity().member_guid(user)


def resolve_tenant_guid(user: Any) -> Optional[str]:
    return _get_identity().tenant_guid(user)


def display_name(user: Any) -> str:
    return _get_identity().display_name(user)


def normalize_subject(subject: str) -> Optional[str]:
    return _get_identity().normalize_subject(subject)


def user_for_member_guid(guid: str) -> Any:
    """The reverse of `resolve_member_guid` (H-3): the Superset account this
    member GUID names, through whichever `Identity` is loaded. Callers that
    used to assume `guid == username` (`_validate_subject`'s role-first
    branch, `LocalDirectory.user_in_tenant`, `OpenFGADirectory.
    tenant_administrators`) go through this instead, so an override that
    relocates the GUID is honoured on the reverse path too."""
    return _get_identity().user_for_member_guid(guid)


def current_member_guid() -> Optional[str]:
    from flask import g

    return resolve_member_guid(getattr(g, "user", None))


def current_tenant_guid() -> Optional[str]:
    from flask import g

    return resolve_tenant_guid(getattr(g, "user", None))


def member_ref(user_or_id: Any) -> Optional[str]:
    """OpenFGA user reference for a Superset user or user id.

    The int -> user lookup is format logic (what a reference looks like),
    not identity resolution, so it stays here rather than moving onto the
    Identity protocol; only the GUID half is delegated.
    """
    from superset import security_manager

    user = user_or_id
    if isinstance(user_or_id, int):
        user = security_manager.get_user_by_id(user_or_id)
    guid = resolve_member_guid(user)
    return f"user:{guid}" if guid else None

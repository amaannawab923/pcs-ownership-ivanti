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
"""The API's manage-reason rule, the optional manage-sharing role layered on
it (`OWNERSHIP_MANAGE_PERMISSION`, spec section 14.5), the detail payload that
carries the reason, the `admitted_by` the writes record, and the labelling
and search of `/subjects`.

Pure: no Superset app and no database. `superset` is stubbed in `sys.modules`
for the imports `api.py` makes inside its functions (`security_manager`,
`db`); the authorizer, the service layer and the identity resolvers are
replaced at the api module's own seams. What is exercised is the module's
logic -- which ground wins, what the payload says, how a member is labelled
and matched -- not Flask-AppBuilder.

`api.py` imports Flask, flask_jwt_extended and flask_login at module level,
so this file is skipped wherever those are not installed (anywhere but a
Superset environment); the rest of the package's tests do not need them.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field, replace
from unittest import mock

import pytest

flask = pytest.importorskip("flask")
pytest.importorskip("flask_jwt_extended")
pytest.importorskip("flask_login")

from superset_ownership import (  # noqa: E402
    api,
    audit,
    hooks,
    identity,
    outbox,
    sentinel,
    service,
)

CALLER_ID = 7
OTHER_ID = 3
TENANT_A = "0a1b2c3d-1111-4222-8333-444455556666"
TENANT_B = "9f8e7d6c-1111-4222-8333-444455556666"
# Upper-cased on the username, as a directory may spell it; the store and
# `resolve_member_guid` lower-case it.
BOB_GUID_UPPER = "2F1E7C4A-1B2C-4D3E-8F9A-0B1C2D3E4F5A"
BOB_GUID = BOB_GUID_UPPER.lower()


@dataclass
class Role:
    name: str


@dataclass
class User:
    id: int
    username: str = "someone"
    first_name: str = ""
    last_name: str = ""
    email: str | None = None
    roles: list = field(default_factory=list)


def row(owner_user_id, object_uuid="obj-uuid", visibility="private", tenant_guid=None):
    return service.OwnershipRow(
        id=1,
        asset_type="chart",
        object_id=7,
        object_uuid=object_uuid,
        owner_user_id=owner_user_id,
        visibility=visibility,
        tenant_guid=tenant_guid,
    )


def mirrored(the_row, store_tenant):
    """The row as the mirror keeps it: carrying the tenant the store holds
    (revision 0004). The cases below name the store's answer once; the
    row's copy of it is derived here rather than repeated in every tuple."""
    if the_row is None or store_tenant is None or the_row.tenant_guid:
        return the_row
    return replace(the_row, tenant_guid=store_tenant)


@pytest.fixture(autouse=True)
def _pre_configuration_group_ids(monkeypatch):
    """This file's fixtures spell group ids in the pre-configuration shape
    (`<name>_<tenant>`); the format-independent logic under test is the
    same under Neurons' default (`<tenant>.<name>`), which
    `test_group_id.py` pins on its own. Tests about the format itself set
    it explicitly."""
    from superset_ownership.identity import (
        GROUP_ID_FORMAT_SETTING,
        GROUP_ID_FORMAT_SUFFIX,
    )

    monkeypatch.setenv(GROUP_ID_FORMAT_SETTING, GROUP_ID_FORMAT_SUFFIX)


@pytest.fixture
def superset_stub(monkeypatch):
    """A `superset` module with the two names api.py imports from it."""
    stub = types.ModuleType("superset")
    stub.security_manager = mock.Mock()
    stub.security_manager.is_admin.return_value = False
    stub.db = mock.Mock()
    monkeypatch.setitem(sys.modules, "superset", stub)
    return stub


@pytest.fixture
def authz(monkeypatch):
    store = mock.Mock()
    store.object_tenant.return_value = None
    store.user_in_group.return_value = False
    monkeypatch.setattr(api, "get_authorizer", lambda: store)

    # The `Directory` seam. Every test that only asks for `authz` still gets
    # a directory that answers "nobody"/"nothing" -- the same empty-tenant
    # shape the old `tenant_members`/`groups_for_tenant` defaults gave --
    # rather than a bare `Mock()` whose truthy default answers would let a
    # rewritten route silently pass an assertion it should fail. A test that
    # needs to configure or assert on the directory itself asks for
    # `directory_mock` (an alias for this same object), never a second patch.
    directory = mock.Mock()
    directory.name = "openfga"
    directory.user_in_tenant.return_value = False
    # PCS-10243 §4.5.2: is_tenant_administrator's default path (no
    # OWNERSHIP_IS_TENANT_ADMINISTRATOR hook configured) reads through
    # get_directory().user_in_group -- an unconfigured Mock() answers truthy
    # by default, which would silently make every caller a tenant
    # administrator under this fixture.
    directory.user_in_group.return_value = False
    directory.group_exists.return_value = False
    directory.search_users.return_value = {"items": [], "next_cursor": None}
    directory.list_groups.return_value = {"items": [], "next_cursor": None}
    monkeypatch.setattr(api, "get_directory", lambda: directory)
    store.directory = directory
    return store


@pytest.fixture
def directory_mock(authz):
    return authz.directory


# --- _manage_reason ---------------------------------------------------------

ADMIN, OWNER, TENANT_ADMIN, MANAGE_PERMISSION = (
    api.MANAGE_REASON_ADMIN,
    api.MANAGE_REASON_OWNER,
    api.MANAGE_REASON_TENANT_ADMIN,
    api.MANAGE_REASON_MANAGE_PERMISSION,
)
MANAGE_ROLE = "sharing_manager"
# The seam as written, for tests that install a stand-in and then need the
# real rule back behind a route. Likewise the subject validation, which the
# write fixtures stub to "fine".
_real_manage_reason = api._manage_reason
_real_validate_subject = api._validate_subject


# (case, is_admin, row, is_tenant_admin, caller_tenant, object_tenant, unowned)
# -> the reason reported.
PRECEDENCE = [
    # Breadth first: a Superset admin is reported as such whatever else is
    # true of them, and the owner over a tenant administrator.
    ("admin who owns it", True, row(CALLER_ID), True, TENANT_A, TENANT_A, False, ADMIN),
    ("admin, no row", True, None, False, None, None, False, ADMIN),
    (
        "owner who administers the tenant",
        False,
        row(CALLER_ID),
        True,
        TENANT_A,
        TENANT_A,
        False,
        OWNER,
    ),
    ("owner, nothing else", False, row(CALLER_ID), False, None, None, False, OWNER),
    # A tenant administrator manages their own tenant's objects only.
    (
        "tenant admin, same tenant",
        False,
        row(OTHER_ID),
        True,
        TENANT_A,
        TENANT_A,
        False,
        TENANT_ADMIN,
    ),
    (
        "tenant admin, other tenant",
        False,
        row(OTHER_ID),
        True,
        TENANT_A,
        TENANT_B,
        False,
        None,
    ),
    # An untenanted object is a tenant administrator's only when nobody owns
    # it: no owner, a deactivated owner, or no row at all.
    (
        "tenant admin, untenanted, live owner",
        False,
        row(OTHER_ID),
        True,
        TENANT_A,
        None,
        False,
        None,
    ),
    (
        "tenant admin, untenanted, no owner",
        False,
        row(None),
        True,
        TENANT_A,
        None,
        False,
        TENANT_ADMIN,
    ),
    (
        "tenant admin, untenanted, owner gone",
        False,
        row(OTHER_ID),
        True,
        TENANT_A,
        None,
        True,
        TENANT_ADMIN,
    ),
    ("tenant admin, no row", False, None, True, TENANT_A, None, False, TENANT_ADMIN),
    # The administrator group without a tenant of one's own is nothing.
    ("tenant admin group, no tenant", False, row(None), True, None, None, False, None),
    ("nobody", False, row(OTHER_ID), False, TENANT_A, TENANT_A, False, None),
]


@pytest.mark.parametrize(
    "case, is_admin, the_row, is_ta, caller_tenant, obj_tenant, unowned, expected",
    PRECEDENCE,
    ids=[c[0] for c in PRECEDENCE],
)
def test_manage_reason_precedence(
    superset_stub,
    authz,
    monkeypatch,
    case,
    is_admin,
    the_row,
    is_ta,
    caller_tenant,
    obj_tenant,
    unowned,
    expected,
):
    superset_stub.security_manager.is_admin.return_value = is_admin
    authz.user_in_group.return_value = is_ta
    # PCS-10243 §4.5.2: is_tenant_administrator's default path reads
    # through the Directory seam, not the Authorizer directly.
    authz.directory.user_in_group.return_value = is_ta
    authz.object_tenant.return_value = obj_tenant
    monkeypatch.setattr(identity, "resolve_tenant_guid", lambda user: caller_tenant)
    monkeypatch.setattr(identity, "resolve_member_guid", lambda user: "caller-guid")
    monkeypatch.setattr(api, "_is_unowned", lambda r: unowned)
    user = User(CALLER_ID)

    assert api._manage_reason(the_row, user) == expected, case


def test_manage_reason_for_no_caller(superset_stub, authz):
    assert api._manage_reason(row(CALLER_ID), None) is None


def test_precedence_table_makes_no_reach_lookup_with_the_seam_off(
    superset_stub, authz, monkeypatch
):
    # Off is the default and must cost nothing: the "can already see" test
    # scans the asset type's rows, and a caller who is none of admin, owner
    # or tenant administrator used to make no such read.
    monkeypatch.delenv(api.MANAGE_PERMISSION_CONFIG_KEY, raising=False)
    reach = mock.Mock(return_value=set())
    monkeypatch.setattr(service, "owned_or_shared_object_ids", reach)
    monkeypatch.setattr(identity, "resolve_tenant_guid", lambda user: TENANT_A)
    monkeypatch.setattr(identity, "resolve_member_guid", lambda user: "caller-guid")
    user = User(CALLER_ID, roles=[Role(MANAGE_ROLE)])

    assert api._manage_reason(row(OTHER_ID), user) is None
    reach.assert_not_called()
    authz.object_tenant.assert_not_called()


# --- OWNERSHIP_MANAGE_PERMISSION: parsing and the holder check --------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("\t\n", None),
        (MANAGE_ROLE, MANAGE_ROLE),
        (f"  {MANAGE_ROLE}  ", MANAGE_ROLE),
    ],
)
def test_parse_manage_permission(raw, expected):
    assert api.parse_manage_permission(raw) == expected


def test_parse_manage_permission_refuses_a_non_string(monkeypatch, caplog):
    # A `(permission, view)` pair is what someone expecting a FAB permission
    # would write. Stringified it would match no role and look like a
    # permission that is on; it is off, and the log says why.
    monkeypatch.setattr(api, "_WARNED_CONFIG_VALUES", set())
    with caplog.at_level("WARNING", logger="superset_ownership.api"):
        assert api.parse_manage_permission(("can_manage_sharing", "Ownership")) is None
    assert "must be a role name" in caplog.text


@pytest.mark.parametrize(
    "name",
    [
        "Admin",
        "Alpha",
        "Gamma",
        "Public",
        "sql_lab",
        f"tenant_{TENANT_A}",
        f"TENANT_{TENANT_A.upper()}",  # the GUID and the prefix, any case
        f"tenant_administrator_{TENANT_A}",
        "  Gamma  ",
        f"  tenant_{TENANT_A}  ",
    ],
)
def test_parse_manage_permission_refuses_a_structural_role_name(
    monkeypatch, caplog, name
):
    # `Gamma` is every ordinary user and `tenant_<guid>` is every member of
    # the tenant: naming either would hand the permission to a population.
    # Refused, off, and the log says so.
    monkeypatch.setattr(api, "_WARNED_CONFIG_VALUES", set())
    with caplog.at_level("WARNING", logger="superset_ownership.api"):
        assert api.parse_manage_permission(name) is None
    assert "must name a dedicated role" in caplog.text
    # And through the holder check: a member of the named population is
    # not a holder.
    monkeypatch.setenv(api.MANAGE_PERMISSION_CONFIG_KEY, name)
    assert (
        api.holds_manage_permission(User(CALLER_ID, roles=[Role(name.strip())]))
        is False
    )


@pytest.mark.parametrize(
    "name",
    [
        f"sharing_manager_{TENANT_A}",  # a per-tenant role, as the JIT path spells one
        f"SHARING_MANAGER_{TENANT_A.upper()}",
        f"dashboard_designers_{TENANT_A}",  # a directory group, as a role
        f"tenant_{TENANT_A}_managers",  # carries the structural name; is not it
        f"xtenant_{TENANT_A}",
        "tenant_",
        "tenant_00000000-0000-0000-0000-000000000000",  # not a v4 GUID: not a tenant role
        "gamma",
        "Gamma2",
    ],
)
def test_parse_manage_permission_accepts_a_dedicated_role_carrying_a_guid(
    monkeypatch, caplog, name
):
    # The guard is anchored: exactly `tenant_<guid>` / `tenant_administrator_
    # <guid>` and the built-ins are populations; any other name -- including
    # one spelled `<group>_<guid>` the way a directory group arrives as a
    # role -- is a role of its own. The seam scopes a holder to their own
    # tenant regardless, so a per-tenant role admits no more than a global
    # one.
    monkeypatch.setattr(api, "_WARNED_CONFIG_VALUES", set())
    with caplog.at_level("WARNING", logger="superset_ownership.api"):
        assert api.parse_manage_permission(name) == name
    assert caplog.text == ""
    monkeypatch.setenv(api.MANAGE_PERMISSION_CONFIG_KEY, name)
    assert api.holds_manage_permission(User(CALLER_ID, roles=[Role(name)])) is True
    assert (
        api.holds_manage_permission(User(CALLER_ID, roles=[Role(f"tenant_{TENANT_A}")]))
        is False
    )


@pytest.mark.parametrize(
    "name, structural",
    [
        (f"tenant_{TENANT_A}", True),
        (f"tenant_administrator_{TENANT_A}", True),
        (f"Tenant_Administrator_{TENANT_A.upper()}", True),
        (f"sharing_manager_{TENANT_A}", False),
        (f"tenant_{TENANT_A}#member", False),  # the bare name, not a reference
        ("tenant_administrator_", False),
        ("", False),
    ],
)
def test_is_structural_tenant_role(name, structural):
    assert identity.is_structural_tenant_role(name) is structural


def test_the_structural_administrator_group_follows_the_group_id_format(monkeypatch):
    # The administrators are the `admin` relation on the tenant object in
    # every format (Neurons' shape), so `tenant_administrator_group` is
    # format-independent. The STRUCTURAL role names are: the membership
    # role in either shape, an administrator group id in the configured
    # format, and the local backend's fixed `tenant_administrator_<guid>`.
    monkeypatch.setenv(
        identity.GROUP_ID_FORMAT_SETTING, identity.GROUP_ID_FORMAT_PREFIX
    )
    assert identity.tenant_administrator_group(TENANT_A) == f"tenant:{TENANT_A}#admin"
    assert (
        identity.is_structural_tenant_role(f"{TENANT_A}_tenant_administrator") is True
    )
    assert identity.is_structural_tenant_role(f"tenant_{TENANT_A}") is True
    assert identity.is_structural_tenant_role(f"Tenant_{TENANT_A}_Role") is True
    assert (
        identity.is_structural_tenant_role(f"tenant_administrator_{TENANT_A}") is True
    )
    assert identity.is_structural_tenant_role("tenant_administrator") is False
    # And the config guard follows: the prefix-format administrator group
    # cannot be the manage-sharing role.
    monkeypatch.setattr(api, "_WARNED_CONFIG_VALUES", set())
    assert api.parse_manage_permission(f"{TENANT_A}_tenant_administrator") is None


def test_parse_manage_permission_warns_once_per_value(monkeypatch, caplog):
    # Read on every request; a bad setting must not log on every request.
    monkeypatch.setattr(api, "_WARNED_CONFIG_VALUES", set())
    with caplog.at_level("WARNING", logger="superset_ownership.api"):
        for _ in range(3):
            assert api.parse_manage_permission("Gamma") is None
            assert api.parse_manage_permission(("x", "y")) is None
    assert caplog.text.count("Gamma") == 1
    assert caplog.text.count("must be a role name") == 1


def test_manage_permission_role_reads_flask_config_before_the_environment(monkeypatch):
    monkeypatch.setenv(api.MANAGE_PERMISSION_CONFIG_KEY, "from_env")
    app = flask.Flask(__name__)

    # No app context: the environment.
    assert api.manage_permission_role() == "from_env"
    # An app that says nothing: still the environment.
    with app.app_context():
        assert api.manage_permission_role() == "from_env"
    # An app that names a role: the app wins.
    app.config[api.MANAGE_PERMISSION_CONFIG_KEY] = " from_config "
    with app.app_context():
        assert api.manage_permission_role() == "from_config"
    # Blank in the app falls through to the environment, exactly like every
    # other OWNERSHIP_* key read through settings.get: "not set here" at one
    # level means the next level answers, with no per-key exception. (Before
    # the settings.py migration this key alone treated a blank config value
    # as a terminal "off"; unified onto the one precedence rule in section
    # 3 of the plug-in architecture spec.)
    app.config[api.MANAGE_PERMISSION_CONFIG_KEY] = ""
    with app.app_context():
        assert api.manage_permission_role() == "from_env"
    # Neither layer set: off.
    app.config.pop(api.MANAGE_PERMISSION_CONFIG_KEY, None)
    monkeypatch.delenv(api.MANAGE_PERMISSION_CONFIG_KEY)
    assert api.manage_permission_role() is None


def test_holds_manage_permission(monkeypatch):
    holder = User(CALLER_ID, roles=[Role(f"tenant_{TENANT_A}"), Role(MANAGE_ROLE)])
    member = User(OTHER_ID, roles=[Role(f"tenant_{TENANT_A}")])

    monkeypatch.delenv(api.MANAGE_PERMISSION_CONFIG_KEY, raising=False)
    assert api.holds_manage_permission(holder) is False  # off: the role means nothing

    monkeypatch.setenv(api.MANAGE_PERMISSION_CONFIG_KEY, MANAGE_ROLE)
    assert api.holds_manage_permission(holder) is True
    assert api.holds_manage_permission(member) is False
    assert api.holds_manage_permission(None) is False
    assert api.holds_manage_permission(User(9, roles=[])) is False
    # Exact: FAB role names are case-sensitive, and a near-miss must not
    # quietly widen who manages sharing.
    monkeypatch.setenv(api.MANAGE_PERMISSION_CONFIG_KEY, MANAGE_ROLE.upper())
    assert api.holds_manage_permission(holder) is False


# --- OWNERSHIP_MANAGE_PERMISSION: the {tenant} placeholder (issue #83) ------


@pytest.mark.parametrize(
    "name",
    [
        "sharing_manager_{tenant}",  # the JIT per-tenant spelling, templated
        "{tenant}_sharing_manager",
        "SHARING_MANAGER_{tenant}",
    ],
)
def test_parse_manage_permission_accepts_a_tenant_placeholder(
    monkeypatch, caplog, name
):
    # Exactly one {tenant} placeholder and nothing else in braces is kept AS
    # A TEMPLATE, unrendered: `holds_manage_permission` substitutes the
    # CALLER's own tenant at request time, never a fixed one here.
    monkeypatch.setattr(api, "_WARNED_CONFIG_VALUES", set())
    with caplog.at_level("WARNING", logger="superset_ownership.api"):
        assert api.parse_manage_permission(name) == name
    assert caplog.text == ""


@pytest.mark.parametrize(
    "name, expected_message",
    [
        # {tenant} missing, or appearing more than once: the "exactly once"
        # guard fires -- this is the one that also catches a mistyped
        # placeholder (review M1), since a typo never produces the exact
        # substring "{tenant}".
        (
            "sharing_manager_{tenant}_{tenant}",  # {tenant} appearing twice
            "must contain the {tenant} placeholder exactly once",
        ),
        ("{tenant}{tenant}", "must contain the {tenant} placeholder exactly once"),
        (
            "sharing_manager_{Tenant}",  # wrong case on the placeholder itself
            "must contain the {tenant} placeholder exactly once",
        ),
        (
            "sharing_manager_{TENANT}",
            "must contain the {tenant} placeholder exactly once",
        ),
        (
            "sharing_manager_{ tenant }",  # stray whitespace inside the braces
            "must contain the {tenant} placeholder exactly once",
        ),
        (
            "sharing_manager_{tenant_guid}",  # a different name in braces
            "must contain the {tenant} placeholder exactly once",
        ),
        ("sharing_manager_{}", "must contain the {tenant} placeholder exactly once"),
        (
            "sharing_manager_tenant}",  # a stray `}` with no `{` at all
            "must contain the {tenant} placeholder exactly once",
        ),
        # {tenant} once, but a second, different placeholder alongside it:
        # the "no other placeholder" guard fires instead.
        (
            "sharing_manager_{tenant}_{other}",  # a second, different placeholder
            "may not contain any placeholder other than {tenant}",
        ),
        (
            "{name}_{tenant}",
            "may not contain any placeholder other than {tenant}",
        ),
    ],
)
def test_parse_manage_permission_refuses_a_malformed_template(
    monkeypatch, caplog, name, expected_message
):
    # {tenant} is the only placeholder this setting understands: missing,
    # twice, or alongside any other `{...}` (including a stray brace that is
    # not a well-formed {tenant} at all -- a typo like {Tenant}/{TENANT}/
    # { tenant}/{tenant_guid}/{} -- review M1) is a parse refusal, never a
    # value accepted as a silent literal that happens to contain braces and
    # so matches nobody. The two refusal messages are distinguishable
    # (review N4) so each guard can be pinned to the input that trips it.
    monkeypatch.setattr(api, "_WARNED_CONFIG_VALUES", set())
    with caplog.at_level("WARNING", logger="superset_ownership.api"):
        assert api.parse_manage_permission(name) is None
    assert expected_message in caplog.text
    # And through the holder check: nothing holds a refused value, silently.
    monkeypatch.setenv(api.MANAGE_PERMISSION_CONFIG_KEY, name)
    assert (
        api.holds_manage_permission(
            User(
                CALLER_ID,
                roles=[Role(f"tenant_{TENANT_A}"), Role(f"sharing_manager_{TENANT_A}")],
            )
        )
        is False
    )


def test_parse_manage_permission_refuses_a_bare_template(monkeypatch, caplog):
    # A bare `{tenant}` (review N5) names a tenant, not a role: rendered it
    # is nothing but a GUID, which is not a role naming convention any JIT
    # path produces. Refused like any other malformed template, not
    # accepted as a role named by a bare GUID.
    monkeypatch.setattr(api, "_WARNED_CONFIG_VALUES", set())
    with caplog.at_level("WARNING", logger="superset_ownership.api"):
        assert api.parse_manage_permission("{tenant}") is None
        assert api.parse_manage_permission("  {tenant}  ") is None
    assert "must name a role, not just a tenant" in caplog.text
    monkeypatch.setenv(api.MANAGE_PERMISSION_CONFIG_KEY, "{tenant}")
    holder_named_by_the_bare_guid = User(
        CALLER_ID, roles=[Role(f"tenant_{TENANT_A}"), Role(TENANT_A)]
    )
    assert api.holds_manage_permission(holder_named_by_the_bare_guid) is False


@pytest.mark.parametrize(
    "name",
    [
        "tenant_{tenant}",
        "tenant_administrator_{tenant}",
        "Tenant_Administrator_{tenant}",
    ],
)
def test_parse_manage_permission_refuses_a_templated_structural_role_name(
    monkeypatch, caplog, name
):
    # The placeholder does not exempt a structural name: substituted with
    # ANY tenant it is still every member's (or every administrator's) own
    # role, so it is refused exactly like the literal form, with the same
    # one-per-value warning -- checked once, against a probe GUID, since the
    # shape does not depend on which tenant fills it in.
    monkeypatch.setattr(api, "_WARNED_CONFIG_VALUES", set())
    with caplog.at_level("WARNING", logger="superset_ownership.api"):
        assert api.parse_manage_permission(name) is None
    assert "must name a dedicated role" in caplog.text


def test_holds_manage_permission_with_a_tenant_placeholder(monkeypatch):
    monkeypatch.setattr(api, "_WARNED_CONFIG_VALUES", set())
    monkeypatch.setenv(api.MANAGE_PERMISSION_CONFIG_KEY, "sharing_manager_{tenant}")

    holder_a = User(
        CALLER_ID,
        roles=[Role(f"tenant_{TENANT_A}"), Role(f"sharing_manager_{TENANT_A}")],
    )
    # Holds the OTHER tenant's rendering of the role, not their own -- must
    # not match.
    wrong_tenant = User(
        OTHER_ID,
        roles=[Role(f"tenant_{TENANT_A}"), Role(f"sharing_manager_{TENANT_B}")],
    )
    # No tenant role at all: a templated permission can never match --
    # there is nothing to substitute for {tenant}. Also carries the role
    # the template would render to if a caller with no tenant were, by
    # mistake, matched against an EMPTY substitution ("sharing_manager_")
    # instead of being refused outright -- so a mutant that drops the
    # early "no caller tenant" refusal cannot pass by accident.
    no_tenant = User(
        4, roles=[Role(f"sharing_manager_{TENANT_A}"), Role("sharing_manager_")]
    )

    assert api.holds_manage_permission(holder_a) is True
    assert api.holds_manage_permission(wrong_tenant) is False
    assert api.holds_manage_permission(no_tenant) is False
    assert api.holds_manage_permission(None) is False


def test_holds_manage_permission_template_matches_the_guid_case_insensitively(
    monkeypatch,
):
    # A directory may spell the GUID in a role name differently-cased than
    # `resolve_tenant_guid` reports it (identity.py lower-cases); the
    # templated match follows the same case-insensitive rule the tenant
    # role match itself already applies (identity.py's `_guid_matches`).
    monkeypatch.setattr(api, "_WARNED_CONFIG_VALUES", set())
    monkeypatch.setenv(api.MANAGE_PERMISSION_CONFIG_KEY, "sharing_manager_{tenant}")
    holder = User(
        CALLER_ID,
        roles=[
            Role(f"tenant_{TENANT_A}"),
            Role(f"sharing_manager_{TENANT_A.upper()}"),
        ],
    )
    assert api.holds_manage_permission(holder) is True


def test_holds_manage_permission_template_folds_only_the_guid(monkeypatch):
    # Review M2: the literal text around {tenant} is matched EXACTLY, like
    # the literal (non-templated) form -- `test_holds_manage_permission`
    # above pins "FAB role names are case-sensitive, and a near-miss must
    # not quietly widen who manages sharing" for that form, and the
    # templated form must not contradict it by folding the whole name.
    # Only the substituted GUID segment is case-insensitive.
    monkeypatch.setattr(api, "_WARNED_CONFIG_VALUES", set())

    # The config spells the role upper-cased; a role that spells the
    # PREFIX lower-cased does not match, even though the GUID would.
    monkeypatch.setenv(api.MANAGE_PERMISSION_CONFIG_KEY, "SHARING_MANAGER_{tenant}")
    lower_prefix = User(
        CALLER_ID,
        roles=[Role(f"tenant_{TENANT_A}"), Role(f"sharing_manager_{TENANT_A}")],
    )
    assert api.holds_manage_permission(lower_prefix) is False

    # And the reverse: the config spells the role lower-cased; a role that
    # spells the prefix upper-cased does not match either.
    monkeypatch.setenv(api.MANAGE_PERMISSION_CONFIG_KEY, "sharing_manager_{tenant}")
    upper_prefix = User(
        CALLER_ID,
        roles=[Role(f"tenant_{TENANT_A}"), Role(f"SHARING_MANAGER_{TENANT_A}")],
    )
    assert api.holds_manage_permission(upper_prefix) is False

    # The GUID segment alone still folds, prefix and all else exact.
    exact_prefix_folded_guid = User(
        CALLER_ID,
        roles=[
            Role(f"tenant_{TENANT_A}"),
            Role(f"sharing_manager_{TENANT_A.upper()}"),
        ],
    )
    assert api.holds_manage_permission(exact_prefix_folded_guid) is True


def test_holds_manage_permission_template_with_a_suffix(monkeypatch):
    # R2-N2(b) (review round 2, PR #98, `review-manage-template-pr98.md`):
    # every other templated `holds_manage_permission` test in this file
    # uses a PREFIX template (`sharing_manager_{tenant}`), so
    # `_renders_for_caller`'s `name.endswith(suffix)` half is never
    # exercised. `{tenant}_sharing_manager` is a SUFFIX template instead.
    monkeypatch.setattr(api, "_WARNED_CONFIG_VALUES", set())
    monkeypatch.setenv(api.MANAGE_PERMISSION_CONFIG_KEY, "{tenant}_sharing_manager")

    exact = User(
        CALLER_ID,
        roles=[Role(f"tenant_{TENANT_A}"), Role(f"{TENANT_A}_sharing_manager")],
    )
    # The suffix near-misses by one character -- must not match: proves
    # `endswith(suffix)` is actually checked, not merely "contains the
    # GUID somewhere".
    near_miss_suffix = User(
        OTHER_ID,
        roles=[Role(f"tenant_{TENANT_A}"), Role(f"{TENANT_A}_sharing_manageX")],
    )
    assert api.holds_manage_permission(exact) is True
    assert api.holds_manage_permission(near_miss_suffix) is False


def test_holds_manage_permission_template_refuses_an_extension_past_the_guid(
    monkeypatch,
):
    # R2-N2(c): the matcher must pin "renders to EXACTLY prefix + GUID +
    # suffix", not "the GUID appears somewhere in the name" -- a role
    # naming the caller's own tenant GUID but with trailing (or leading,
    # via another tenant's GUID) text past what the template renders to
    # must not match.
    monkeypatch.setattr(api, "_WARNED_CONFIG_VALUES", set())
    monkeypatch.setenv(api.MANAGE_PERMISSION_CONFIG_KEY, "sharing_manager_{tenant}")

    extra_suffix = User(
        CALLER_ID,
        roles=[Role(f"tenant_{TENANT_A}"), Role(f"sharing_manager_{TENANT_A}_extra")],
    )
    # The caller's own GUID (A) appears in the role name, but only as a
    # trailing segment after a DIFFERENT tenant's GUID (B) fills the
    # rendered position -- "contains A" must not be enough.
    another_tenants_guid_then_mine = User(
        OTHER_ID,
        roles=[
            Role(f"tenant_{TENANT_A}"),
            Role(f"sharing_manager_{TENANT_B}_{TENANT_A}"),
        ],
    )
    assert api.holds_manage_permission(extra_suffix) is False
    assert api.holds_manage_permission(another_tenants_guid_then_mine) is False


# --- _manage_reason with the seam on -----------------------------------------


def holder(tenant=TENANT_A, with_role=True):
    roles = [Role(f"tenant_{tenant}")] if tenant else []
    if with_role:
        roles.append(Role(MANAGE_ROLE))
    return User(CALLER_ID, roles=roles)


@pytest.fixture
def reach_seams(monkeypatch):
    """What `_already_opens` consults beyond the store listing, pinned to
    the answers of a holder in good standing: a member reference, no queued
    revocation, a live owner, the dataset grant. Each test that is about one
    of them overrides that one."""
    monkeypatch.setattr(identity, "member_ref", lambda user_or_id: "user:caller-guid")
    revocation = mock.Mock(return_value=False)
    monkeypatch.setattr(outbox, "has_pending_revocation", revocation)
    monkeypatch.setattr(api, "_is_unowned", lambda r: False)
    asset = mock.Mock()
    asset.id = 7
    monkeypatch.setattr(api, "_get_asset", lambda asset_type, pk: asset)
    grant = mock.Mock(return_value=True)
    monkeypatch.setattr(hooks, "base_permission_holds_for", grant)
    return types.SimpleNamespace(revocation=revocation, grant=grant, asset=asset)


# (case, is_admin, row, is_tenant_admin, object_tenant, caller, reaches)
# -> the reason reported. The caller's tenant comes from their real
# `tenant_<guid>` role; `reaches` is whether the store lists the object as
# owned by or shared with the caller.
MANAGE_PERMISSION_CASES = [
    # The permission admits: own tenant, and the object already reaches them.
    (
        "holder, own tenant, shared with them",
        False,
        row(OTHER_ID),
        False,
        TENANT_A,
        holder(),
        True,
        MANAGE_PERMISSION,
    ),
    (
        "holder, own tenant, public",
        False,
        row(OTHER_ID, visibility="public"),
        False,
        TENANT_A,
        holder(),
        False,
        MANAGE_PERMISSION,
    ),
    # NOT A ROLE THAT ADOPTS STRAYS: an object with no owner has nobody
    # whose sharing there is to manage, tenanted (the OpenFGA backfill
    # shape) or not. Admitting it would let the visibility route hand the
    # object to the holder.
    (
        "holder, own tenant, ownerless object",
        False,
        row(None, visibility="public"),
        False,
        TENANT_A,
        holder(),
        True,
        None,
    ),
    (
        "holder, own tenant, ownerless, shared",
        False,
        row(None),
        False,
        TENANT_A,
        holder(),
        True,
        None,
    ),
    # READ IS NOT WIDENED: a private object of their own tenant that was
    # never shared with them is not theirs to manage either.
    (
        "holder, own tenant, private, not shared",
        False,
        row(OTHER_ID),
        False,
        TENANT_A,
        holder(),
        False,
        None,
    ),
    # THE TENANT BOUNDARY: a holder in tenant B and a tenant-A object, even
    # one shared with them or public.
    (
        "holder in B, object in A, shared",
        False,
        row(OTHER_ID),
        False,
        TENANT_A,
        holder(TENANT_B),
        True,
        None,
    ),
    (
        "holder in B, object in A, public",
        False,
        row(OTHER_ID, visibility="public"),
        False,
        TENANT_A,
        holder(TENANT_B),
        False,
        None,
    ),
    # Neither side has a tenant to compare: refused. No rescue path here,
    # and no `None == None` pass when both are missing.
    (
        "holder, untenanted object",
        False,
        row(OTHER_ID, visibility="public"),
        False,
        None,
        holder(),
        True,
        None,
    ),
    (
        "holder with no tenant of their own",
        False,
        row(OTHER_ID, visibility="public"),
        False,
        TENANT_A,
        holder(tenant=None),
        True,
        None,
    ),
    (
        "holder with no tenant, untenanted object",
        False,
        row(OTHER_ID, visibility="public"),
        False,
        None,
        holder(tenant=None),
        True,
        None,
    ),
    ("holder, no row", False, None, False, None, holder(), True, None),
    (
        "holder, row without a uuid",
        False,
        row(OTHER_ID, object_uuid=None, visibility="public"),
        False,
        TENANT_A,
        holder(),
        True,
        None,
    ),
    # The role alone is not the permission; the config must name it.
    (
        "member without the role",
        False,
        row(OTHER_ID, visibility="public"),
        False,
        TENANT_A,
        holder(with_role=False),
        True,
        None,
    ),
    # PRECEDENCE: admin > owner > tenant_admin > manage_permission.
    (
        "admin who holds the role",
        True,
        row(OTHER_ID),
        False,
        TENANT_A,
        holder(),
        True,
        ADMIN,
    ),
    (
        "owner who holds the role",
        False,
        row(CALLER_ID),
        False,
        TENANT_A,
        holder(),
        True,
        OWNER,
    ),
    (
        "tenant admin who holds the role, own tenant",
        False,
        row(OTHER_ID),
        True,
        TENANT_A,
        holder(),
        True,
        TENANT_ADMIN,
    ),
    # A tenant administrator's refusal falls through to the role, which
    # refuses on the same boundary.
    (
        "tenant admin of B holding the role, object in A",
        False,
        row(OTHER_ID),
        True,
        TENANT_A,
        holder(TENANT_B),
        True,
        None,
    ),
    # A tenant administrator refused for an untenanted, owned object is not
    # admitted through the role either (no tenant to match).
    (
        "tenant admin holding the role, untenanted, live owner",
        False,
        row(OTHER_ID, visibility="public"),
        True,
        None,
        holder(),
        True,
        None,
    ),
]


@pytest.mark.parametrize(
    "case, is_admin, the_row, is_ta, obj_tenant, caller, reaches, expected",
    MANAGE_PERMISSION_CASES,
    ids=[c[0] for c in MANAGE_PERMISSION_CASES],
)
def test_manage_permission_reason(
    superset_stub,
    authz,
    reach_seams,
    monkeypatch,
    case,
    is_admin,
    the_row,
    is_ta,
    obj_tenant,
    caller,
    reaches,
    expected,
):
    monkeypatch.setenv(api.MANAGE_PERMISSION_CONFIG_KEY, MANAGE_ROLE)
    superset_stub.security_manager.is_admin.return_value = is_admin
    authz.user_in_group.return_value = is_ta
    # PCS-10243 §4.5.2: is_tenant_administrator's default path reads
    # through the Directory seam, not the Authorizer directly.
    authz.directory.user_in_group.return_value = is_ta
    authz.object_tenant.return_value = obj_tenant
    monkeypatch.setattr(identity, "resolve_member_guid", lambda user: "caller-guid")
    monkeypatch.setattr(
        service,
        "owned_or_shared_object_ids",
        lambda asset_type, user_id: (
            {the_row.object_id} if (reaches and the_row) else set()
        ),
    )
    the_row = mirrored(the_row, obj_tenant)

    assert api._manage_reason(the_row, caller) == expected, case


@pytest.mark.parametrize(
    "case, is_admin, the_row, is_ta, obj_tenant, caller, reaches, expected",
    [c for c in MANAGE_PERMISSION_CASES if c[-1] == MANAGE_PERMISSION],
    ids=[c[0] for c in MANAGE_PERMISSION_CASES if c[-1] == MANAGE_PERMISSION],
)
def test_manage_permission_admits_nobody_when_off(
    superset_stub,
    authz,
    reach_seams,
    monkeypatch,
    case,
    is_admin,
    the_row,
    is_ta,
    obj_tenant,
    caller,
    reaches,
    expected,
):
    # The same cases the seam admits, with the setting unset: nothing.
    monkeypatch.delenv(api.MANAGE_PERMISSION_CONFIG_KEY, raising=False)
    authz.object_tenant.return_value = obj_tenant
    monkeypatch.setattr(identity, "resolve_member_guid", lambda user: "caller-guid")
    monkeypatch.setattr(
        service,
        "owned_or_shared_object_ids",
        lambda asset_type, user_id: {the_row.object_id},
    )

    assert api._manage_reason(the_row, caller) is None, case


def test_manage_reason_with_a_templated_manage_permission(
    superset_stub, authz, reach_seams, monkeypatch
):
    # Review N6: an end-to-end row through `_manage_reason` with a
    # TEMPLATED `OWNERSHIP_MANAGE_PERMISSION`, not just the unit-level
    # `parse_manage_permission` / `holds_manage_permission` tests above --
    # pinning the seam-plus-template composition the PR body claims: a
    # holder of the tenant's OWN rendering of the role manages an object IN
    # that tenant, and the SAME holder does not manage an object in another
    # tenant, even though the object reaches them by the store's own
    # listing (the "reaches" ground alone is not enough; ground 4, the
    # tenant match, still applies to a templated holder exactly as it does
    # to a literal one).
    monkeypatch.setattr(api, "_WARNED_CONFIG_VALUES", set())
    monkeypatch.setenv(api.MANAGE_PERMISSION_CONFIG_KEY, "sharing_manager_{tenant}")
    superset_stub.security_manager.is_admin.return_value = False
    authz.user_in_group.return_value = False
    authz.directory.user_in_group.return_value = False
    monkeypatch.setattr(identity, "resolve_member_guid", lambda user: "caller-guid")
    the_row = row(OTHER_ID)
    monkeypatch.setattr(
        service,
        "owned_or_shared_object_ids",
        lambda asset_type, user_id: {the_row.object_id},
    )
    caller = User(
        CALLER_ID,
        roles=[Role(f"tenant_{TENANT_A}"), Role(f"sharing_manager_{TENANT_A}")],
    )

    authz.object_tenant.return_value = TENANT_A
    assert api._manage_reason(the_row, caller) == MANAGE_PERMISSION

    authz.object_tenant.return_value = TENANT_B
    assert api._manage_reason(the_row, caller) is None


# --- "can already see": the read path's tests, not a looser one -----------


@pytest.fixture
def admitted_holder(superset_stub, authz, reach_seams, monkeypatch):
    """A holder the seam admits: config on, own tenant, object shared with
    them. Tests flip one read-path condition and expect a refusal."""
    monkeypatch.setenv(api.MANAGE_PERMISSION_CONFIG_KEY, MANAGE_ROLE)
    authz.object_tenant.return_value = TENANT_A
    monkeypatch.setattr(identity, "resolve_member_guid", lambda user: "caller-guid")
    monkeypatch.setattr(
        service, "owned_or_shared_object_ids", lambda asset_type, user_id: {7}
    )
    assert api._manage_reason(row(OTHER_ID), holder()) == MANAGE_PERMISSION
    return reach_seams


def test_a_holder_whose_revocation_is_still_queued_is_not_admitted(admitted_holder):
    # The owner unshared the holder; the mirror row is gone and the delete
    # is queued, but until the drain runs the STORE still lists the object
    # for them. The read gate denies on the pending revocation before it
    # asks the store; so does the seam. Otherwise a holder scripting
    # POST /shares in a loop re-grants themselves inside the drain window,
    # and the queued delete is then classified as a role change.
    admitted_holder.revocation.return_value = True

    assert api._manage_reason(row(OTHER_ID), holder()) is None
    admitted_holder.revocation.assert_called_with(
        "chart:obj-uuid", "user:caller-guid", object_id=7
    )


def test_the_revocation_guard_is_asked_for_a_shared_object_not_a_public_one(
    admitted_holder,
):
    # Public is not reached through a share, so there is no share to have
    # revoked; the read gate never asks either. One indexed query saved per
    # public object, and the guard is exactly the read gate's.
    admitted_holder.revocation.reset_mock()
    assert (
        api._manage_reason(row(OTHER_ID, visibility="public"), holder())
        == MANAGE_PERMISSION
    )
    admitted_holder.revocation.assert_not_called()


def test_a_holder_without_a_member_reference_is_not_admitted_to_a_shared_object(
    admitted_holder, monkeypatch
):
    monkeypatch.setattr(identity, "member_ref", lambda user_or_id: None)
    assert api._manage_reason(row(OTHER_ID), holder()) is None


def test_a_holder_is_not_admitted_to_an_object_whose_owner_is_deactivated(
    admitted_holder, monkeypatch
):
    # The read gate grants nobody on an object whose owner has gone; a share
    # confers nothing there, so the holder cannot see it either.
    monkeypatch.setattr(api, "_is_unowned", lambda r: True)
    assert api._manage_reason(row(OTHER_ID), holder()) is None
    assert api._manage_reason(row(OTHER_ID, visibility="public"), holder()) is None


def test_a_holder_without_the_dataset_grant_is_not_admitted(admitted_holder):
    # "Can already see" is the read gate's definition: a share, or public,
    # never substitutes for the dataset grant. Evaluated for the caller
    # through the same impersonating check a transfer applies to a recipient.
    admitted_holder.grant.return_value = False

    assert api._manage_reason(row(OTHER_ID), holder()) is None
    assert api._manage_reason(row(OTHER_ID, visibility="public"), holder()) is None
    user, asset, asset_type = admitted_holder.grant.call_args.args
    assert (user.id, asset, asset_type) == (CALLER_ID, admitted_holder.asset, "chart")


def test_the_dataset_grant_is_asked_last(admitted_holder, monkeypatch):
    # The cheapest refusals first: an object that does not reach the holder
    # never costs the permission check.
    admitted_holder.grant.reset_mock()
    monkeypatch.setattr(
        service, "owned_or_shared_object_ids", lambda asset_type, user_id: set()
    )
    assert api._manage_reason(row(OTHER_ID), holder()) is None
    admitted_holder.grant.assert_not_called()


def test_an_object_not_listed_for_the_holder_costs_no_tenant_read(
    admitted_holder, authz, monkeypatch
):
    # The store listing is the read the detail route has already made for
    # its own 404 decision, so it is asked BEFORE the object's tenant: a
    # private object never shared with the holder -- the commonest refusal
    # -- costs neither the `object_tenant` round-trip nor the revocation
    # query nor the grant check.
    authz.object_tenant.reset_mock()
    admitted_holder.revocation.reset_mock()
    admitted_holder.grant.reset_mock()
    monkeypatch.setattr(
        service, "owned_or_shared_object_ids", lambda asset_type, user_id: set()
    )

    assert api._manage_reason(row(OTHER_ID), holder()) is None
    authz.object_tenant.assert_not_called()
    admitted_holder.revocation.assert_not_called()
    admitted_holder.grant.assert_not_called()


def test_the_tenant_boundary_is_asked_before_the_object_is_opened(
    admitted_holder, authz, monkeypatch
):
    # And for an object that IS listed (or public), the tenant read comes
    # before the revocation query and the impersonated grant check: another
    # tenant's object costs neither.
    admitted_holder.revocation.reset_mock()
    admitted_holder.grant.reset_mock()
    authz.object_tenant.return_value = TENANT_B

    assert api._manage_reason(row(OTHER_ID), holder()) is None
    assert api._manage_reason(row(OTHER_ID, visibility="public"), holder()) is None
    admitted_holder.revocation.assert_not_called()
    admitted_holder.grant.assert_not_called()


# --- The role never widens read access ---------------------------------------


def test_read_paths_do_not_consult_the_manage_seam():
    # Structural: the hooks that decide whether an object can be OPENED
    # (`raise_for_access_bypass`) or LISTED (the query filters) never import
    # the API's manage rule, so nothing added to the seam can reach them.
    import inspect

    source = inspect.getsource(hooks)
    for name in (
        "_manage_reason",
        "holds_manage_permission",
        "manage_permission_role",
        "MANAGE_PERMISSION",
        "superset_ownership.api",
        "is_tenant_administrator",
    ):
        assert name not in source, name


def test_holder_still_cannot_open_a_shared_object(superset_stub, authz, monkeypatch):
    # The behavioural half: with the seam ON and a holder whose tenant owns
    # the object, `raise_for_access_bypass` answers exactly as it would for
    # any other member -- the store's answer, and the store says no.
    #
    # Issue #93: on a `private` object this hook now denies a non-owner
    # (manage-permission holder or not) BEFORE it ever asks the store --
    # zero calls, covered separately by the private hard-deny's own tests
    # (superset_harness-driven, test_endpoints.py). This test's actual
    # property -- the manage-sharing seam is never consulted on the READ
    # path, only the store's own answer decides -- needs a `shared` object
    # to reach the store-asking branch at all.
    monkeypatch.setenv(api.MANAGE_PERMISSION_CONFIG_KEY, MANAGE_ROLE)
    shared = row(OTHER_ID, visibility="shared")
    chart = mock.Mock()
    chart.id = shared.object_id
    monkeypatch.setattr(service, "lookup", lambda asset_type, object_id: shared)
    monkeypatch.setattr(hooks, "base_permission_holds", lambda obj, asset_type: True)
    monkeypatch.setattr(hooks, "is_unowned", lambda r: False)
    monkeypatch.setattr(identity, "member_ref", lambda user_id: "user:caller-guid")
    monkeypatch.setattr(
        outbox, "has_pending_revocation", lambda obj, subject, object_id=None: False
    )
    store = mock.Mock()
    store.check.return_value = False
    monkeypatch.setattr(hooks, "get_authorizer", lambda: store)
    seam = mock.Mock(
        side_effect=AssertionError("the read path consulted the manage seam")
    )
    monkeypatch.setattr(api, "holds_manage_permission", seam)
    monkeypatch.setattr(api, "_manage_reason", seam)

    assert hooks.raise_for_access_bypass(user_id=CALLER_ID, chart=chart) is False
    store.check.assert_called_once_with("user:caller-guid", "viewer", "chart:obj-uuid")
    seam.assert_not_called()


def test_holder_gets_nothing_extra_in_the_list_filter(superset_stub, monkeypatch):
    monkeypatch.setenv(api.MANAGE_PERMISSION_CONFIG_KEY, MANAGE_ROLE)
    monkeypatch.setattr(service, "owned_or_shared_rows", lambda asset_type, user_id: {})
    monkeypatch.setattr(hooks, "_public_ids_in_sql", lambda asset_type, user: [])
    seam = mock.Mock(
        side_effect=AssertionError("the list filter consulted the manage seam")
    )
    monkeypatch.setattr(api, "holds_manage_permission", seam)

    assert hooks.chart_query_filter(CALLER_ID) == []
    assert hooks.dashboard_query_filter(CALLER_ID) == []
    seam.assert_not_called()


def test_detail_stays_404_for_a_holder_on_a_private_object_not_shared(
    detail_seams, reach_seams, superset_stub, authz, monkeypatch
):
    # Through the REAL seam, not the mocked one the fixture installs: a
    # private object of the holder's own tenant that was never shared with
    # them is reported as absent, exactly as to any other member. Holding the
    # role does not disclose its owner or share list.
    monkeypatch.setattr(api, "_manage_reason", _real_manage_reason)
    monkeypatch.setenv(api.MANAGE_PERMISSION_CONFIG_KEY, MANAGE_ROLE)
    monkeypatch.setattr(api, "_authn", lambda mutating=False: holder())
    authz.object_tenant.return_value = TENANT_A

    status, body = get_detail(monkeypatch, row(OTHER_ID))

    assert status == 404
    assert "owner" not in body and "shares" not in body


def test_detail_makes_one_reach_read_for_a_holder(
    detail_seams, reach_seams, superset_stub, authz, monkeypatch
):
    # On OpenFGA the reach read is a `list_objects` round-trip. The seam
    # needs it and the route's own 404 decision needs it, for the same
    # caller and asset type: one request, one read, seam on or off.
    monkeypatch.setattr(api, "_manage_reason", _real_manage_reason)
    monkeypatch.setattr(api, "_authn", lambda mutating=False: holder())
    authz.object_tenant.return_value = TENANT_A
    reach = mock.Mock(return_value=set())
    monkeypatch.setattr(service, "owned_or_shared_object_ids", reach)

    monkeypatch.setenv(api.MANAGE_PERMISSION_CONFIG_KEY, MANAGE_ROLE)
    assert get_detail(monkeypatch, row(OTHER_ID))[0] == 404
    assert reach.call_count == 1

    reach.reset_mock()
    monkeypatch.delenv(api.MANAGE_PERMISSION_CONFIG_KEY)
    assert get_detail(monkeypatch, row(OTHER_ID))[0] == 404
    assert reach.call_count == 1


def test_the_reach_memo_is_per_request(superset_stub, monkeypatch):
    calls = mock.Mock(side_effect=[{1}, {2}])
    monkeypatch.setattr(service, "owned_or_shared_object_ids", calls)
    app = flask.Flask(__name__)
    with app.test_request_context("/"):
        assert api._reachable_ids("chart", CALLER_ID) == {1}
        assert api._reachable_ids("chart", CALLER_ID) == {1}
        # A copy: building on the answer does not change the memo.
        api._reachable_ids("chart", CALLER_ID).add(99)
        assert api._reachable_ids("chart", CALLER_ID) == {1}
    with app.test_request_context("/"):
        assert api._reachable_ids("chart", CALLER_ID) == {2}
    assert calls.call_count == 2


def test_detail_reports_the_permission_once_the_object_reaches_the_holder(
    detail_seams, reach_seams, superset_stub, authz, monkeypatch
):
    monkeypatch.setattr(api, "_manage_reason", _real_manage_reason)
    monkeypatch.setenv(api.MANAGE_PERMISSION_CONFIG_KEY, MANAGE_ROLE)
    monkeypatch.setattr(api, "_authn", lambda mutating=False: holder())
    authz.object_tenant.return_value = TENANT_A
    monkeypatch.setattr(
        service, "owned_or_shared_object_ids", lambda asset_type, user_id: {7}
    )

    status, body = get_detail(monkeypatch, row(OTHER_ID))

    assert status == 200
    assert body["can_manage"] is True
    assert body["can_share"] is True
    assert body["manage_reason"] == MANAGE_PERMISSION
    # Manages it, so the full owner block -- the same as any other manager.
    assert body["owner"]["email"] == "jane@example.com"


# --- GET /<asset>s: can_manage for a holder ---------------------------------


class _Column:
    def in_(self, ids):
        return set(ids)


class _Query:
    """Enough of a SQLAlchemy query for `_list_assets`: the scope filter is
    honoured, so what the route hands back is what it would select."""

    def __init__(self, assets):
        self._assets = list(assets)

    def order_by(self, *_):
        return self

    def filter(self, ids):
        self._assets = [a for a in self._assets if a.id in ids]
        return self

    def count(self):
        return len(self._assets)

    def limit(self, *_):
        return self

    def offset(self, *_):
        return self

    def all(self):
        return self._assets


def list_charts(monkeypatch, superset_stub, caller, rows, assets, active_owners=None):
    model = types.SimpleNamespace(id=_Column())
    slice_module = types.ModuleType("superset.models.slice")
    slice_module.Slice = model
    models_pkg = types.ModuleType("superset.models")
    monkeypatch.setitem(sys.modules, "superset.models", models_pkg)
    monkeypatch.setitem(sys.modules, "superset.models.slice", slice_module)
    superset_stub.db.session.query = lambda m: _Query(assets)
    monkeypatch.setattr(api, "_authn", lambda mutating=False: caller)
    monkeypatch.setattr(service, "list_rows", lambda asset_type: rows)
    monkeypatch.setattr(
        api,
        "_active_owner_ids",
        lambda ids: set(ids) if active_owners is None else set(active_owners),
    )
    monkeypatch.setattr(
        service,
        "get_user_info",
        lambda user_id: {"id": user_id, "name": "X", "email": "x@e", "guid": "g"},
    )
    with flask.Flask(__name__).test_request_context("/api/v1/ownership/charts"):
        response = api._list_assets("chart")
    return {item["object_id"]: item for item in response.get_json()["result"]}


def chart_row(object_id, owner, uuid, visibility):
    """A row whose uuid names its tenant: `a-*` is tenant A's, `b-*` tenant
    B's (the mirror, revision 0004), anything else is untenanted."""
    tenant = {"a": TENANT_A, "b": TENANT_B}.get((uuid or "")[:1])
    return service.OwnershipRow(
        id=object_id,
        asset_type="chart",
        object_id=object_id,
        object_uuid=uuid,
        owner_user_id=owner,
        visibility=visibility,
        tenant_guid=tenant,
    )


@pytest.fixture
def four_charts():
    """Public in A, private-shared in A, private-not-shared in A, public in B."""
    rows = [
        chart_row(1, OTHER_ID, "a-public", "public"),
        chart_row(2, OTHER_ID, "a-shared", "shared"),
        chart_row(3, OTHER_ID, "a-private", "private"),
        chart_row(4, OTHER_ID, "b-public", "public"),
    ]
    assets = [types.SimpleNamespace(id=r.object_id) for r in rows]
    return rows, assets


def test_list_flags_for_a_holder(superset_stub, authz, monkeypatch, four_charts):
    rows, assets = four_charts
    monkeypatch.setenv(api.MANAGE_PERMISSION_CONFIG_KEY, MANAGE_ROLE)
    authz.tenant_objects.return_value = ["a-public", "a-shared", "a-private"]
    monkeypatch.setattr(
        service, "owned_or_shared_object_ids", lambda asset_type, user_id: {2}
    )

    listed = list_charts(monkeypatch, superset_stub, holder(), rows, assets)

    # The private, unshared object is not on the page at all: the scope is
    # a member's scope, unchanged by the role. Nor is the other tenant's
    # public object: public is public within its tenant.
    assert set(listed) == {1, 2}
    assert listed[1]["can_manage"] is True and listed[1]["can_share"] is True
    assert listed[2]["can_manage"] is True and listed[2]["can_share"] is True
    # The owner block follows can_manage per row, as for a tenant administrator.
    assert "email" in listed[1]["owner"]
    authz.tenant_objects.assert_called_once_with(TENANT_A, "chart")


def test_list_flags_agree_with_the_seam_on_ownerless_and_unowned_rows(
    superset_stub, authz, monkeypatch
):
    # The page's can_manage for a holder is the seam's answer in summary:
    # an object with no owner, or a deactivated one, is not theirs to
    # manage, in the list as at the detail and the writes.
    rows = [
        chart_row(1, OTHER_ID, "a-owned", "public"),
        chart_row(2, None, "a-ownerless", "public"),
        chart_row(3, 99, "a-unowned", "public"),
    ]
    assets = [types.SimpleNamespace(id=r.object_id) for r in rows]
    monkeypatch.setenv(api.MANAGE_PERMISSION_CONFIG_KEY, MANAGE_ROLE)
    authz.tenant_objects.return_value = ["a-owned", "a-ownerless", "a-unowned"]
    monkeypatch.setattr(
        service, "owned_or_shared_object_ids", lambda asset_type, user_id: set()
    )

    # Owner 99 is the deactivated one.
    listed = list_charts(
        monkeypatch, superset_stub, holder(), rows, assets, active_owners={OTHER_ID}
    )

    assert listed[1]["can_manage"] is True
    assert listed[2]["can_manage"] is False and listed[2]["can_share"] is False
    assert listed[3]["unowned"] is True
    assert listed[3]["can_manage"] is False and listed[3]["can_share"] is False


def test_list_makes_one_tenant_request_for_a_tenant_admin_holding_the_role(
    superset_stub, authz, monkeypatch, four_charts
):
    # The holder branch is for callers the broader grounds do not cover: a
    # tenant administrator's page already made the tenant-objects request,
    # and must not make it again for the role they also hold.
    rows, assets = four_charts
    monkeypatch.setenv(api.MANAGE_PERMISSION_CONFIG_KEY, MANAGE_ROLE)
    authz.user_in_group.return_value = True
    authz.directory.user_in_group.return_value = True
    authz.tenant_objects.return_value = ["a-public", "a-shared", "a-private"]
    monkeypatch.setattr(identity, "resolve_member_guid", lambda user: "caller-guid")
    monkeypatch.setattr(
        service, "owned_or_shared_object_ids", lambda asset_type, user_id: set()
    )

    listed = list_charts(monkeypatch, superset_stub, holder(), rows, assets)

    assert set(listed) == {1, 2, 3}
    assert all(listed[i]["can_manage"] for i in (1, 2, 3))
    authz.tenant_objects.assert_called_once_with(TENANT_A, "chart")


def test_list_with_can_manage_hook_makes_no_per_row_store_calls(
    superset_stub, authz, monkeypatch, four_charts
):
    """M-3: a configured OWNERSHIP_CAN_MANAGE hook used to cost one
    `object_tenant` store round trip AND one `list_shares` query PER ROW
    (an N+1 for a page). `object_state["tenant"]` is now built from data the
    list handler already computed (`tenant_uuids`, from the ONE
    `tenant_objects` call this tenant-administrator page already makes),
    and `object_state["shares"]` from ONE batched query for the whole page
    -- not four `object_tenant` calls and four share queries for these four
    rows."""
    rows, assets = four_charts
    authz.user_in_group.return_value = True
    authz.directory.user_in_group.return_value = True
    authz.tenant_objects.return_value = [r.object_uuid for r in rows[:3]]  # a-*
    monkeypatch.setattr(identity, "resolve_tenant_guid", lambda user: TENANT_A)

    hook_calls: list[dict] = []

    def can_manage_hook(user, object_state):
        hook_calls.append(object_state)
        return None  # never widen; this test is about call counts, not the decision

    # Only OWNERSHIP_CAN_MANAGE is configured -- a blanket `lambda name:
    # can_manage_hook` also answers `get_hook("OWNERSHIP_IS_TENANT_
    # ADMINISTRATOR")`, and that hook's 1-arg signature does not match
    # this one's 2-arg `(user, object_state)`, raising a TypeError that
    # silently fails `is_tenant_administrator` closed instead of using the
    # mocked directory this test actually relies on for privilege.
    monkeypatch.setattr(
        api,
        "get_hook",
        lambda name: can_manage_hook if name == "OWNERSHIP_CAN_MANAGE" else None,
    )
    bulk_calls: list[tuple] = []

    def counting_bulk(asset_type, object_ids):
        # A fake, not the real DB-backed function: this "pure" test file's
        # `superset_stub` mocks `superset.db` entirely, so the real query
        # would hit a Mock, not a database. The call COUNT/ARGS are what
        # this test is about.
        ids = tuple(object_ids)
        bulk_calls.append((asset_type, ids))
        return {i: [] for i in ids}

    monkeypatch.setattr(service, "list_share_rows_bulk", counting_bulk)
    monkeypatch.setattr(
        service, "owned_or_shared_object_ids", lambda asset_type, user_id: set()
    )

    listed = list_charts(monkeypatch, superset_stub, holder(), rows, assets)

    assert set(listed) == {1, 2, 3}
    assert len(hook_calls) == 3, "the hook is still asked once per visible row"
    authz.object_tenant.assert_not_called()
    assert len(bulk_calls) == 1, "shares must be fetched once for the whole page"
    assert bulk_calls[0][0] == "chart"
    assert set(bulk_calls[0][1]) == {1, 2, 3}
    # Rows in the caller's own tenant (a-*) report it, from data already in
    # hand -- no per-row store lookup was needed.
    assert all(call["tenant"] == TENANT_A for call in hook_calls)


def test_list_flags_unchanged_for_a_member_without_the_role(
    superset_stub, authz, monkeypatch, four_charts
):
    rows, assets = four_charts
    monkeypatch.setenv(api.MANAGE_PERMISSION_CONFIG_KEY, MANAGE_ROLE)
    monkeypatch.setattr(
        service, "owned_or_shared_object_ids", lambda asset_type, user_id: {2}
    )

    listed = list_charts(
        monkeypatch, superset_stub, holder(with_role=False), rows, assets
    )

    assert set(listed) == {1, 2}
    assert all(item["can_manage"] is False for item in listed.values())
    authz.tenant_objects.assert_not_called()


def test_list_flags_unchanged_for_a_holder_when_off(
    superset_stub, authz, monkeypatch, four_charts
):
    rows, assets = four_charts
    monkeypatch.delenv(api.MANAGE_PERMISSION_CONFIG_KEY, raising=False)
    monkeypatch.setattr(
        service, "owned_or_shared_object_ids", lambda asset_type, user_id: {2}
    )

    listed = list_charts(monkeypatch, superset_stub, holder(), rows, assets)

    assert all(item["can_manage"] is False for item in listed.values())
    authz.tenant_objects.assert_not_called()


def test_list_can_manage_is_the_pages_summary_and_the_detail_decides(
    superset_stub, authz, reach_seams, monkeypatch
):
    # The contract, stated: a holder WITHOUT the object's dataset grant gets
    # `can_manage: true` on the list row (tenant, ownable, live owner --
    # resolved in bulk, no per-row reads) and `can_manage: false` on the
    # detail of the same object (the seam asks the grant). A client renders
    # controls from the detail; the list flag is advisory.
    monkeypatch.setenv(api.MANAGE_PERMISSION_CONFIG_KEY, MANAGE_ROLE)
    reach_seams.grant.return_value = False
    shared = chart_row(7, OTHER_ID, "obj-uuid", "shared")
    authz.tenant_objects.return_value = ["obj-uuid"]
    authz.object_tenant.return_value = TENANT_A
    monkeypatch.setattr(identity, "resolve_member_guid", lambda user: "caller-guid")
    monkeypatch.setattr(
        service, "owned_or_shared_object_ids", lambda asset_type, user_id: {7}
    )

    listed = list_charts(
        monkeypatch, superset_stub, holder(), [shared], [types.SimpleNamespace(id=7)]
    )
    assert listed[7]["can_manage"] is True and listed[7]["can_share"] is True
    reach_seams.grant.assert_not_called()

    monkeypatch.setattr(api, "_authn", lambda mutating=False: holder())
    monkeypatch.setattr(service, "list_shares", lambda asset_type, pk: [])
    status, body = get_detail(monkeypatch, row(OTHER_ID, visibility="shared"))
    assert status == 200
    assert body["can_manage"] is False and body["manage_reason"] is None
    reach_seams.grant.assert_called_once()


def test_local_tenant_objects_loads_the_owners_in_one_query(superset_stub, monkeypatch):
    # `LocalAuthorizer.tenant_objects` answered per row: a row lookup, a user
    # lookup and the lazy roles load for every object of the asset type, on
    # every list page of a tenant administrator or a manage-sharing holder.
    # One SELECT of the distinct owners with their roles instead.
    from superset_ownership.authz import LocalAuthorizer

    rows = [
        chart_row(1, 1, "a-one", "public"),
        chart_row(2, 2, "b-one", "public"),
        chart_row(3, 1, "a-two", "private"),
        chart_row(4, None, "ownerless", "public"),
        chart_row(5, 3, "no-tenant", "public"),
        chart_row(6, 1, None, "public"),
    ]
    monkeypatch.setattr(service, "list_rows", lambda asset_type: rows)
    owners = [
        User(1, roles=[Role(f"tenant_{TENANT_A}")]),
        User(2, roles=[Role(f"tenant_{TENANT_B}")]),
        User(3, roles=[]),
    ]
    monkeypatch.setattr("sqlalchemy.orm.joinedload", lambda attr: attr)
    superset_stub.security_manager.user_model = types.SimpleNamespace(
        id=_Column(), roles=object()
    )
    query = superset_stub.db.session.query
    query.return_value.options.return_value.filter.return_value.all.return_value = (
        owners
    )

    assert LocalAuthorizer().tenant_objects(TENANT_A, "chart") == ["a-one", "a-two"]
    assert LocalAuthorizer().tenant_objects(TENANT_B, "chart") == ["b-one"]
    assert query.call_count == 2  # one per call, not one per row
    query.return_value.options.return_value.filter.assert_called_with({1, 2, 3})
    superset_stub.security_manager.get_user_by_id.assert_not_called()

    # No owned rows at all: no query.
    monkeypatch.setattr(
        service, "list_rows", lambda asset_type: [chart_row(4, None, "x", "public")]
    )
    assert LocalAuthorizer().tenant_objects(TENANT_A, "chart") == []
    assert query.call_count == 2


# --- Audit: which rule admitted the caller ----------------------------------


def test_audit_record_carries_admitted_by_only_when_given():
    plain = audit.build(audit.SHARE_ADDED, asset_type="chart", object_id=7)
    assert "admitted_by" not in plain
    assert set(plain) == {
        "event",
        "occurred_at",
        "actor",
        "object",
        "before",
        "after",
        "request_id",
    }

    admitted = audit.build(
        audit.SHARE_ADDED,
        asset_type="chart",
        object_id=7,
        admitted_by=MANAGE_PERMISSION,
    )
    assert admitted["admitted_by"] == MANAGE_PERMISSION
    assert set(admitted) == set(plain) | {"admitted_by"}


@pytest.fixture
def write_seams(superset_stub, authz, monkeypatch):
    """Every write the four mutating routes make, stubbed to succeed, and
    `audit.emit` captured. Returns the captured emitter."""
    asset = mock.Mock()
    asset.id = 7
    asset.uuid = "obj-uuid"
    # A real (empty) list, not a bare Mock attribute: `_set_asset_owner` and
    # `_claim_asset` now read `asset.editors` directly, before and after
    # `service.sync_native_editors` (review M-1's before/after audit pair,
    # `_native_editor_user_ids`), and iterate it -- a Mock() attribute is
    # not iterable and would fail every write this fixture stubs, not only
    # the owner/claim ones the real sync used to touch.
    asset.editors = []
    monkeypatch.setattr(api, "_get_asset", lambda asset_type, pk: asset)
    monkeypatch.setattr(api, "_is_unowned", lambda r: False)
    monkeypatch.setattr(
        api, "_validate_subject", lambda subject, caller, **_: (True, "")
    )
    monkeypatch.setattr(identity, "normalize_subject", lambda subject: subject)
    monkeypatch.setattr(identity, "member_ref", lambda user_or_id: f"user:{user_or_id}")
    for name in ("upsert_ownership", "set_visibility"):
        monkeypatch.setattr(service, name, mock.Mock())
    # The owner/claim/adopt routes call this transactionally now (review
    # M-1): stubbed here the same as every other service seam this fixture
    # replaces, so these unit-level tests stay about the routes' own logic
    # (which ground admitted the caller, what the audit record says, the
    # lock ordering) rather than exercising the real native-editors sync
    # against a database that does not exist in this fixture at all.
    monkeypatch.setattr(service, "sync_native_editors", mock.Mock(return_value=False))
    monkeypatch.setattr(service, "add_share", mock.Mock(return_value=True))
    monkeypatch.setattr(service, "remove_share", mock.Mock(return_value=True))
    monkeypatch.setattr(
        service,
        "list_shares",
        lambda asset_type, pk: [
            {"subject": "user:bob", "role": "viewer"},
            {"subject": TENANT_GROUP, "role": "viewer"},
            {"subject": TENANT_ADMIN_GROUP, "role": "viewer"},
            {"subject": DESIGNERS_GROUP, "role": "viewer"},
        ],
    )
    # Issue #93: the raw mirror rows, as `_remove_asset_share` (the mirror-
    # row-first subject check) and `_revoke_shares_for_private` (the
    # visibility -> private revoke) both now read directly, unlike
    # `list_shares` above (which resolves display names and merges live
    # store grants -- neither of which either new code path needs or
    # wants). Same subjects, so a REMOVE(...) row in MANAGE_PERMISSION_
    # MATRIX naming any of them finds a mirror row and skips
    # `_validate_subject`, which `write_seams` already stubs open anyway.
    monkeypatch.setattr(
        service,
        "list_share_rows",
        lambda asset_type, pk: [
            {"subject": "user:bob", "role": "viewer"},
            {"subject": TENANT_GROUP, "role": "viewer"},
            {"subject": TENANT_ADMIN_GROUP, "role": "viewer"},
            {"subject": DESIGNERS_GROUP, "role": "viewer"},
        ],
    )
    monkeypatch.setattr(
        service, "get_user_info", lambda user_id: {"id": user_id, "name": "X"}
    )
    for name in ("write_tuple", "delete_tuple", "set_object_tenant"):
        monkeypatch.setattr(outbox, name, mock.Mock(return_value=True))
    monkeypatch.setattr(outbox, "enabled", lambda: True)
    monkeypatch.setattr(sentinel, "add_sentinel", mock.Mock())
    monkeypatch.setattr(sentinel, "remove_sentinel", mock.Mock())
    emit = mock.Mock()
    monkeypatch.setattr(audit, "emit", emit)
    return emit


# The tenant's two structural groups, and an ordinary directory group.
TENANT_GROUP = f"group:tenant_{TENANT_A}#member"
TENANT_ADMIN_GROUP = f"group:tenant_administrator_{TENANT_A}#member"
DESIGNERS_GROUP = f"group:dashboard_designers_{TENANT_A}#member"


def call(route, method, path, json=None):
    return call_full(route, method, path, json)[0]


def call_full(route, method, path, json=None):
    """(status, decoded JSON body) of a route called in a request context."""
    with flask.Flask(__name__).test_request_context(path, method=method, json=json):
        response = route()
    if isinstance(response, tuple):
        return response[1], response[0].get_json()
    return response.status_code, response.get_json()


WRITES = [
    (
        "visibility",
        lambda: api._set_asset_visibility("chart", 7),
        "PUT",
        "/api/v1/ownership/chart/7/visibility",
        {"visibility": "shared"},
        audit.VISIBILITY_CHANGED,
    ),
    (
        "share add",
        lambda: api._add_asset_share("chart", 7),
        "POST",
        "/api/v1/ownership/chart/7/shares",
        {"subject": "user:bob", "role": "viewer"},
        audit.SHARE_ADDED,
    ),
    (
        "share remove",
        lambda: api._remove_asset_share("chart", 7, "user:bob"),
        "DELETE",
        "/api/v1/ownership/chart/7/shares/user:bob",
        None,
        audit.SHARE_REMOVED,
    ),
    (
        "owner",
        lambda: api._set_asset_owner("chart", 7),
        "PUT",
        "/api/v1/ownership/chart/7/owner",
        {"subject": "user:bob"},
        audit.OWNER_ASSIGNED,
    ),
    (
        "claim",
        lambda: api._claim_asset("chart", 7),
        "POST",
        "/api/v1/ownership/chart/7/claim",
        None,
        audit.OWNER_ASSIGNED,
    ),
]


@pytest.fixture
def admitted_writer(write_seams, superset_stub, monkeypatch):
    """The four routes with the seam stubbed to a chosen reason, the
    recipient lookup answering for `user:bob` (a third party) and `user:7`
    (the caller: `member_ref` is stubbed to `user:<id>`), and the transfer
    baseline check passing. Returns a function `(reason) -> None`."""
    caller = holder()
    monkeypatch.setattr(api, "_authn", lambda mutating=False: caller)
    # H1 (review round 1, issue #93): `_add_asset_share` now refuses a share
    # write on a `private` object outright, before any admission-ground
    # check -- unrelated to what this fixture's tests exercise (which rule
    # admitted the caller, the audit record, lock ordering across every
    # WRITES route). `shared`, not the `row()` default, so "share add"/
    # "share self"/"change own role" reach the ground check this fixture is
    # actually about.
    monkeypatch.setattr(
        service,
        "lock_object",
        lambda asset_type, pk: row(OTHER_ID, visibility="shared"),
    )
    monkeypatch.setattr(api, "_refuse_transfer", lambda *a, **k: None)
    recipient = User(OTHER_ID, username="bob")
    recipient.is_active = True
    caller.is_active = True
    superset_stub.security_manager.find_user.side_effect = lambda username: (
        caller if username == str(CALLER_ID) else recipient
    )
    superset_stub.security_manager.get_user_by_id.return_value = recipient

    def admit(reason):
        monkeypatch.setattr(api, "_manage_reason", lambda r, u: reason)

    admit.recipient = recipient
    return admit


# The writes a tenant administrator's ground does NOT admit: the object's
# sharing is its owner's; the administrator transfers (to a member, or to
# themselves) and claims. See api._tenant_admin_sharing_refusal.
SHARING_WRITES = ("visibility", "share add", "share remove")


@pytest.mark.parametrize(
    "name, route, method, path, body, event", WRITES, ids=[w[0] for w in WRITES]
)
@pytest.mark.parametrize("reason", [OWNER, TENANT_ADMIN, ADMIN])
def test_each_write_records_which_rule_admitted_the_caller(
    admitted_writer, write_seams, name, route, method, path, body, event, reason
):
    admitted_writer(reason)

    status = call(route, method, path, body)

    if reason == TENANT_ADMIN and name in SHARING_WRITES:
        assert status == 403, (name, status)
        write_seams.assert_not_called()
        return
    assert status == 200, (name, status)
    assert write_seams.call_count == 1, name
    args, kwargs = write_seams.call_args
    assert args[0] == event
    assert kwargs["admitted_by"] == reason
    # The existing keys are as they were.
    assert {"actor", "asset_type", "object_id", "object_uuid"} <= set(kwargs)


@pytest.mark.parametrize(
    "name, route, method, path, body, event",
    [w for w in WRITES if w[0] in SHARING_WRITES],
    ids=SHARING_WRITES,
)
def test_a_tenant_administrator_is_told_to_take_ownership_first(
    admitted_writer, write_seams, name, route, method, path, body, event
):
    admitted_writer(TENANT_ADMIN)
    status, payload = call_full(route, method, path, body)
    assert status == 403, name
    assert "take ownership" in payload["message"]
    write_seams.assert_not_called()


@pytest.mark.parametrize(
    "name, route, method, path, body, event",
    [w for w in WRITES if w[0] in SHARING_WRITES],
    ids=SHARING_WRITES,
)
def test_a_tenant_administrator_holding_the_manage_permission_shares_on_that_ground(
    admitted_writer, write_seams, monkeypatch, name, route, method, path, body, event
):
    # The seam reports the administrator ground first; the explicit opt-in
    # wins on a sharing route: admitted, and audited, as the permission.
    admitted_writer(TENANT_ADMIN)
    monkeypatch.setattr(
        api, "_manage_permission_reason", lambda r, u: MANAGE_PERMISSION
    )
    status = call(route, method, path, body)
    assert status == 200, (name, status)
    assert write_seams.call_args.kwargs["admitted_by"] == MANAGE_PERMISSION


def test_a_dual_holder_gets_the_permissions_narrowing_not_the_administrators(
    admitted_writer, write_seams, monkeypatch
):
    # ... and with it the permission's own refusals: making the object
    # public is not a sharing action under that ground either.
    admitted_writer(TENANT_ADMIN)
    monkeypatch.setattr(
        api, "_manage_permission_reason", lambda r, u: MANAGE_PERMISSION
    )
    status, body = call_full(VIS[0], VIS[1], VIS[2], {"visibility": "public"})
    assert status == 403
    assert "does not include making an object public" in body["message"]
    write_seams.assert_not_called()


# --- The manage-sharing permission's matrix, one cell per row ---------------

SELF = f"user:{CALLER_ID}"
VIS = (
    lambda: api._set_asset_visibility("chart", 7),
    "PUT",
    "/api/v1/ownership/chart/7/visibility",
)
ADD = (
    lambda: api._add_asset_share("chart", 7),
    "POST",
    "/api/v1/ownership/chart/7/shares",
)
OWN = (
    lambda: api._set_asset_owner("chart", 7),
    "PUT",
    "/api/v1/ownership/chart/7/owner",
)
CLAIM = (
    lambda: api._claim_asset("chart", 7),
    "POST",
    "/api/v1/ownership/chart/7/claim",
)


def REMOVE(subject):
    return (
        lambda: api._remove_asset_share("chart", 7, subject),
        "DELETE",
        f"/api/v1/ownership/chart/7/shares/{subject}",
    )


# (cell, (route, method, path), body, status under manage_permission, event
# on success). The other three grounds answer 200 to every row: the
# narrowing is the fourth ground's alone.
MANAGE_PERMISSION_MATRIX = [
    (
        # M1 (review round 1): `write_seams`'s stubbed mirror rows include
        # the tenant's structural group (TENANT_GROUP), so `-> private`
        # would revoke it along with everything else -- one of the two
        # writes this ground refuses individually on /shares. Refused here
        # too, before any write. See the dedicated cases below for the
        # third-party-only (200) and own-share (403) shapes this one cell
        # cannot tell apart on its own.
        "visibility -> private (mirror includes the tenant's structural group)",
        VIS,
        {"visibility": "private"},
        403,
        None,
    ),
    (
        "visibility -> shared",
        VIS,
        {"visibility": "shared"},
        200,
        audit.VISIBILITY_CHANGED,
    ),
    ("visibility -> public", VIS, {"visibility": "public"}, 403, None),
    (
        "share a third party",
        ADD,
        {"subject": "user:bob", "role": "viewer"},
        200,
        audit.SHARE_ADDED,
    ),
    (
        "change a third party's role",
        ADD,
        {"subject": "user:bob", "role": "editor"},
        200,
        audit.SHARE_ADDED,
    ),
    ("share self", ADD, {"subject": SELF, "role": "viewer"}, 403, None),
    ("change own role", ADD, {"subject": SELF, "role": "editor"}, 403, None),
    ("unshare a third party", REMOVE("user:bob"), None, 200, audit.SHARE_REMOVED),
    ("unshare self", REMOVE(SELF), None, 403, None),
    # A group the holder belongs to is still a third party: a group share
    # never grants more than viewer, which the holder already has. The
    # tenant's OWN membership group is the whole tenant -- the read grant
    # `-> public` reserves for the owner, minus the label -- and its
    # administrator group is structural too; both include the holder.
    (
        "share a group",
        ADD,
        {"subject": DESIGNERS_GROUP, "role": "editor"},
        200,
        audit.SHARE_ADDED,
    ),
    ("unshare a group", REMOVE(DESIGNERS_GROUP), None, 200, audit.SHARE_REMOVED),
    (
        "share the tenant's membership group",
        ADD,
        {"subject": TENANT_GROUP, "role": "viewer"},
        403,
        None,
    ),
    (
        "share the tenant's administrator group",
        ADD,
        {"subject": TENANT_ADMIN_GROUP, "role": "editor"},
        403,
        None,
    ),
    ("unshare the tenant's membership group", REMOVE(TENANT_GROUP), None, 403, None),
    (
        "unshare the tenant's administrator group",
        REMOVE(TENANT_ADMIN_GROUP),
        None,
        403,
        None,
    ),
    (
        "transfer to a third party",
        OWN,
        {"subject": "user:bob"},
        200,
        audit.OWNER_ASSIGNED,
    ),
    ("transfer to self", OWN, {"subject": SELF}, 403, None),
    ("release", OWN, {"subject": None}, 403, None),
    ("claim", CLAIM, None, 403, None),
]


@pytest.mark.parametrize(
    "cell, route, body, expected, event",
    MANAGE_PERMISSION_MATRIX,
    ids=[m[0] for m in MANAGE_PERMISSION_MATRIX],
)
def test_manage_permission_matrix(
    admitted_writer, write_seams, cell, route, body, expected, event
):
    admitted_writer(MANAGE_PERMISSION)
    fn, method, path = route

    with flask.Flask(__name__).test_request_context(path, method=method, json=body):
        response = fn()
    status = response[1] if isinstance(response, tuple) else response.status_code

    assert status == expected, (cell, status)
    if expected == 200:
        # Issue #93: "visibility -> private" emits one VISIBILITY_CHANGED
        # event followed by one SHARE_REMOVED per mocked mirror row
        # (`write_seams`'s `list_share_rows` stub) -- the FIRST call is the
        # route's own event, which is what `event` names for every other
        # row too (each of which writes exactly once).
        args, kwargs = write_seams.call_args_list[0]
        assert (args[0], kwargs["admitted_by"]) == (event, MANAGE_PERMISSION)
    else:
        # Refused BEFORE anything is written or recorded, and the body says
        # it is this permission that stops short, not a missing grant.
        write_seams.assert_not_called()
        assert (
            "manage-sharing permission does not include"
            in response[0].get_json()["message"]
        )
        for writer in (
            service.upsert_ownership,
            service.set_visibility,
            service.add_share,
            service.remove_share,
            outbox.write_tuple,
            outbox.delete_tuple,
        ):
            writer.assert_not_called()


def test_manage_permission_may_go_private_with_third_party_shares_only(
    admitted_writer, write_seams, monkeypatch
):
    """M1 (review round 1): the refusal is about the ROWS being revoked, not
    about `-> private` itself -- a holder admitted only by the manage-
    sharing permission may still take an object private when every mirror
    row is a third party's share (no self-grant, no structural group)."""
    admitted_writer(MANAGE_PERMISSION)
    monkeypatch.setattr(
        service,
        "list_share_rows",
        lambda asset_type, pk: [
            {"subject": "user:bob", "role": "viewer"},
            {"subject": DESIGNERS_GROUP, "role": "editor"},
        ],
    )

    status = call(
        lambda: api._set_asset_visibility("chart", 7),
        "PUT",
        "/api/v1/ownership/chart/7/visibility",
        {"visibility": "private"},
    )

    assert status == 200
    args, kwargs = write_seams.call_args_list[0]
    assert (args[0], kwargs["admitted_by"]) == (
        audit.VISIBILITY_CHANGED,
        MANAGE_PERMISSION,
    )


def test_manage_permission_may_not_go_private_with_own_share(
    admitted_writer, write_seams, monkeypatch
):
    """M1: refused the same way even when the ONLY mirror row is the
    holder's own share (no structural group involved) -- `-> private`
    would revoke it, which is the self-unshare this ground refuses on
    DELETE /shares."""
    admitted_writer(MANAGE_PERMISSION)
    monkeypatch.setattr(
        service,
        "list_share_rows",
        lambda asset_type, pk: [{"subject": SELF, "role": "viewer"}],
    )

    status = call(
        lambda: api._set_asset_visibility("chart", 7),
        "PUT",
        "/api/v1/ownership/chart/7/visibility",
        {"visibility": "private"},
    )

    assert status == 403
    write_seams.assert_not_called()


@pytest.mark.parametrize(
    "cell, route, body, expected, event",
    [m for m in MANAGE_PERMISSION_MATRIX if m[3] == 403],
    ids=[m[0] for m in MANAGE_PERMISSION_MATRIX if m[3] == 403],
)
@pytest.mark.parametrize("reason", [OWNER, TENANT_ADMIN, ADMIN])
def test_the_refused_cells_stay_open_to_the_other_grounds(
    admitted_writer, write_seams, cell, route, body, expected, event, reason
):
    admitted_writer(reason)
    fn, method, path = route

    if reason == TENANT_ADMIN and fn is not OWN[0] and fn is not CLAIM[0]:
        # A tenant administrator's ground admits the owner/claim cells only;
        # the sharing cells are refused on that ground for its own reason
        # (`test_a_tenant_administrator_is_told_to_take_ownership_first`).
        assert call(fn, method, path, body) == 403, (cell, reason)
        write_seams.assert_not_called()
        return
    # 202 is the unshare route's "no mirror row; revocation queued" success.
    assert call(fn, method, path, body) in (200, 202), (cell, reason)
    # `call_args_list[0]`, not `call_args` (issue #93): `visibility ->
    # private` against a mirror that actually has rows (the structural-group
    # cell) emits `visibility_changed` (admitted by `reason`, checked here)
    # FIRST, then one `share_removed` per revoked row (admitted by
    # `visibility_change`, not `reason`) -- `call_args` alone would catch
    # the LAST of those instead of the write this test is about.
    assert write_seams.call_args_list[0].kwargs["admitted_by"] == reason


def test_the_self_check_compares_canonical_references(
    admitted_writer, write_seams, monkeypatch
):
    # The subject is normalised before it is compared, so an alternative
    # spelling of the caller's own reference (their Superset id, their email)
    # does not get past the check.
    admitted_writer(MANAGE_PERMISSION)
    monkeypatch.setattr(
        identity,
        "normalize_subject",
        lambda subject: SELF if subject == "user:me@example.com" else subject,
    )

    status = call(
        ADD[0], ADD[1], ADD[2], {"subject": "user:me@example.com", "role": "editor"}
    )

    assert status == 403
    write_seams.assert_not_called()


def test_the_owner_route_self_check_compares_resolved_accounts(
    admitted_writer, write_seams, superset_stub, monkeypatch
):
    # The owner route compares the RESOLVED account, not the subject string:
    # a spelling of the caller's own reference that is not their canonical
    # one (their email, say) resolves to their account and is refused the
    # same. A string compare against `member_ref(user.id)` would let it
    # through.
    admitted_writer(MANAGE_PERMISSION)
    caller = superset_stub.security_manager.find_user(username=str(CALLER_ID))
    monkeypatch.setattr(
        identity,
        "normalize_subject",
        lambda subject: SELF if subject == "user:me@example.com" else subject,
    )

    status = call(OWN[0], OWN[1], OWN[2], {"subject": "user:me@example.com"})

    assert status == 403
    assert caller.id == CALLER_ID  # resolved to the caller's own account
    write_seams.assert_not_called()
    service.upsert_ownership.assert_not_called()
    outbox.write_tuple.assert_not_called()


# --- A transfer never moves an object between tenants ------------------------


@pytest.mark.parametrize("reason", [MANAGE_PERMISSION, OWNER, TENANT_ADMIN, ADMIN])
def test_a_transfer_never_moves_the_object_out_of_its_tenant(
    admitted_writer, write_seams, authz, reason
):
    # The recipient is a member of two tenants whose FIRST tenant role names
    # the other one; `_validate_subject` accepts them because the store lists
    # them in the caller's tenant, and the object's tenant is stamped from
    # the owner's first role -- so the object would follow the recipient
    # into tenant B: the previous owner loses it, A's administrators can no
    # longer manage it, B's can. Refused under EVERY ground, before the
    # recipient baseline check and before any write.
    admitted_writer(reason)
    authz.object_tenant.return_value = TENANT_A
    admitted_writer.recipient.roles = [
        Role(f"tenant_{TENANT_B}"),
        Role(f"tenant_{TENANT_A}"),
    ]
    baseline = mock.Mock(return_value=None)
    monkeypatch_refuse = mock.patch.object(api, "_refuse_transfer", baseline)

    with (
        monkeypatch_refuse,
        flask.Flask(__name__).test_request_context(
            OWN[2], method=OWN[1], json={"subject": "user:bob"}
        ),
    ):
        response = OWN[0]()

    assert response[1] == 409
    assert (
        "never moves an object out of its tenant" in response[0].get_json()["message"]
    )
    baseline.assert_not_called()
    write_seams.assert_not_called()
    service.upsert_ownership.assert_not_called()
    outbox.write_tuple.assert_not_called()
    outbox.delete_tuple.assert_not_called()
    outbox.set_object_tenant.assert_not_called()


@pytest.mark.parametrize("reason", [MANAGE_PERMISSION, OWNER, TENANT_ADMIN, ADMIN])
def test_a_transfer_within_the_tenant_keeps_the_objects_tenant_as_it_is(
    admitted_writer, write_seams, authz, reason
):
    # A recipient of the object's own tenant: the transfer goes through and
    # the object's tenant is not re-stamped -- it already has the right one.
    admitted_writer(reason)
    authz.object_tenant.return_value = TENANT_A
    admitted_writer.recipient.roles = [Role(f"tenant_{TENANT_A}")]

    assert call(OWN[0], OWN[1], OWN[2], {"subject": "user:bob"}) == 200
    assert write_seams.call_args.args[0] == audit.OWNER_ASSIGNED
    assert write_seams.call_args.kwargs["admitted_by"] == reason
    outbox.write_tuple.assert_called_once_with("user:3", "owner", "chart:obj-uuid")
    outbox.set_object_tenant.assert_not_called()


def test_assigning_an_owner_to_an_untenanted_object_gives_it_the_recipients_tenant(
    admitted_writer, write_seams, authz
):
    # The rescue path, unchanged: an object with no tenant moves INTO the
    # tenant of the owner it is given.
    admitted_writer(TENANT_ADMIN)
    authz.object_tenant.return_value = None
    admitted_writer.recipient.roles = [Role(f"tenant_{TENANT_A}")]

    assert call(OWN[0], OWN[1], OWN[2], {"subject": "user:bob"}) == 200
    outbox.set_object_tenant.assert_called_once_with("chart", "obj-uuid", TENANT_A)


def test_a_claim_never_moves_the_object_out_of_its_tenant(
    admitted_writer, write_seams, authz
):
    # The same invariant on the claim route. Only a Superset admin who also
    # carries another tenant's role can reach it: an owner's or a tenant
    # administrator's claim is always within the object's tenant, or of an
    # object that has none.
    admitted_writer(ADMIN)
    authz.object_tenant.return_value = TENANT_B  # the caller, `holder()`, is in A

    with flask.Flask(__name__).test_request_context(CLAIM[2], method=CLAIM[1]):
        response = CLAIM[0]()
    assert response[1] == 409
    assert (
        "never moves an object out of its tenant" in response[0].get_json()["message"]
    )
    write_seams.assert_not_called()
    service.upsert_ownership.assert_not_called()

    # In the caller's own tenant: claimed, not re-stamped. Untenanted: stamped.
    authz.object_tenant.return_value = TENANT_A
    assert call(CLAIM[0], CLAIM[1], CLAIM[2]) == 200
    outbox.set_object_tenant.assert_not_called()
    authz.object_tenant.return_value = None
    assert call(CLAIM[0], CLAIM[1], CLAIM[2]) == 200
    outbox.set_object_tenant.assert_called_once_with("chart", "obj-uuid", TENANT_A)


@pytest.mark.parametrize("reason", [MANAGE_PERMISSION, OWNER, TENANT_ADMIN, ADMIN])
def test_a_tenanted_object_is_not_handed_to_a_recipient_with_no_tenant_role(
    admitted_writer, write_seams, authz, reason
):
    # No tenant role names the object's tenant here: the recipient has
    # none. On the local backend the object's tenant IS its owner's, so the
    # transfer would untenant it; refused under every ground with a message
    # that says what is missing, not one that names a tenant that does not
    # exist.
    admitted_writer(reason)
    authz.object_tenant.return_value = TENANT_A
    admitted_writer.recipient.roles = []

    with flask.Flask(__name__).test_request_context(
        OWN[2], method=OWN[1], json={"subject": "user:bob"}
    ):
        response = OWN[0]()

    assert response[1] == 409
    message = response[0].get_json()["message"]
    assert message.startswith("the recipient has no tenant role"), message
    assert "a transfer never moves an object out of its tenant" in message
    assert "another tenant" not in message
    write_seams.assert_not_called()
    service.upsert_ownership.assert_not_called()
    outbox.write_tuple.assert_not_called()
    outbox.set_object_tenant.assert_not_called()


def test_a_tenantless_admin_may_not_claim_a_tenanted_object(
    admitted_writer, write_seams, authz, monkeypatch
):
    # The ordinary local admin -- no `tenant_` role at all -- claiming an
    # object that has a tenant. Refused (409, nothing written) with the
    # sentence for that case; the same admin claims an UNTENANTED object
    # freely, and stamps nothing since there is no tenant to stamp.
    admitted_writer(ADMIN)
    monkeypatch.setattr(api, "_authn", lambda mutating=False: User(CALLER_ID))
    authz.object_tenant.return_value = TENANT_A

    with flask.Flask(__name__).test_request_context(CLAIM[2], method=CLAIM[1]):
        response = CLAIM[0]()
    assert response[1] == 409
    message = response[0].get_json()["message"]
    assert message.startswith("you have no tenant role"), message
    assert "a claim never moves an object out of its tenant" in message
    write_seams.assert_not_called()
    service.upsert_ownership.assert_not_called()

    authz.object_tenant.return_value = None
    assert call(CLAIM[0], CLAIM[1], CLAIM[2]) == 200
    service.upsert_ownership.assert_called_once()
    outbox.set_object_tenant.assert_not_called()


# --- Adopting an ownerless object is the third path that assigns an owner ---


def adopt(caller, monkeypatch, visibility="private"):
    """PUT /visibility as `caller` on an OWNERLESS row; returns (status, body)."""
    monkeypatch.setattr(api, "_authn", lambda mutating=False: caller)
    monkeypatch.setattr(service, "lock_object", lambda asset_type, pk: row(None))
    with flask.Flask(__name__).test_request_context(
        VIS[2], method=VIS[1], json={"visibility": visibility}
    ):
        response = VIS[0]()
    if isinstance(response, tuple):
        return response[1], response[0].get_json()
    return response.status_code, response.get_json()


def test_adopting_an_ownerless_object_never_moves_it_out_of_its_tenant(
    admitted_writer, write_seams, authz, monkeypatch
):
    # A Superset admin carrying tenant B's role makes an ownerless object
    # whose tenant tuple says A non-public. Adoption assigns an owner, so
    # the transfer and claim invariant applies: refused before any write,
    # and the object is not stamped B (on OpenFGA that would leave it with
    # two tenant tuples and `object_tenant` answering whichever is read
    # first).
    admitted_writer(ADMIN)
    authz.object_tenant.return_value = TENANT_A
    admin_in_b = User(CALLER_ID, roles=[Role(f"tenant_{TENANT_B}")])

    status, body = adopt(admin_in_b, monkeypatch)

    assert status == 409
    assert body["message"].startswith("your tenant role names another tenant")
    assert (
        "adopting an ownerless object never moves an object out of its tenant"
        in body["message"]
    )
    authz.object_tenant.assert_called_once_with("chart", "obj-uuid", strict=True)
    service.upsert_ownership.assert_not_called()
    service.set_visibility.assert_not_called()
    outbox.write_tuple.assert_not_called()
    outbox.set_object_tenant.assert_not_called()
    sentinel.add_sentinel.assert_not_called()
    write_seams.assert_not_called()


def test_a_tenantless_admin_may_not_adopt_a_tenanted_ownerless_object(
    admitted_writer, write_seams, authz, monkeypatch
):
    admitted_writer(ADMIN)
    authz.object_tenant.return_value = TENANT_A

    status, body = adopt(User(CALLER_ID), monkeypatch)

    assert status == 409
    assert body["message"].startswith("you have no tenant role")
    service.upsert_ownership.assert_not_called()
    outbox.set_object_tenant.assert_not_called()
    write_seams.assert_not_called()


def test_adopting_within_the_tenant_keeps_the_objects_tenant_as_it_is(
    admitted_writer, write_seams, authz, monkeypatch
):
    # A Superset admin carrying tenant A's role (the rescue path) on an
    # ownerless object already stamped A: adopted, owner tuple written, NOT
    # re-stamped.
    admitted_writer(ADMIN)
    authz.object_tenant.return_value = TENANT_A

    status, _ = adopt(holder(TENANT_A, with_role=False), monkeypatch)

    assert status == 200
    service.upsert_ownership.assert_called_once_with(
        "chart", 7, "obj-uuid", CALLER_ID, "private"
    )
    outbox.write_tuple.assert_called_once_with(
        f"user:{CALLER_ID}", "owner", "chart:obj-uuid"
    )
    outbox.set_object_tenant.assert_not_called()
    # Issue #93: adopting assigns `private` visibility, which now also
    # revokes every mocked mirror row (`write_seams`'s `list_share_rows`
    # stub) -- one SHARE_REMOVED per row, after the route's own
    # VISIBILITY_CHANGED event, which is therefore the FIRST call rather
    # than the last.
    assert write_seams.call_args_list[0].args[0] == audit.VISIBILITY_CHANGED
    assert write_seams.call_args_list[0].kwargs["admitted_by"] == ADMIN


def test_a_tenant_administrator_does_not_adopt(
    admitted_writer, write_seams, authz, monkeypatch
):
    # The ownerless-object rescue by a tenant administrator is a CLAIM, not
    # a visibility change: the visibility write is refused on that ground
    # before the adoption branch, nothing is written or stamped.
    admitted_writer(TENANT_ADMIN)
    authz.object_tenant.return_value = TENANT_A

    status, body = adopt(holder(TENANT_A, with_role=False), monkeypatch)

    assert status == 403
    assert "take ownership" in body["message"]
    service.upsert_ownership.assert_not_called()
    outbox.write_tuple.assert_not_called()
    outbox.set_object_tenant.assert_not_called()
    write_seams.assert_not_called()


def test_adopting_an_untenanted_object_gives_it_the_adopters_tenant(
    admitted_writer, write_seams, authz, monkeypatch
):
    # The path as it was: an ownerless, untenanted stray moves INTO the
    # adopter's tenant (a Superset admin carrying a tenant role). A
    # tenantless adopter stamps nothing.
    admitted_writer(ADMIN)
    authz.object_tenant.return_value = None

    assert adopt(holder(TENANT_A, with_role=False), monkeypatch)[0] == 200
    outbox.set_object_tenant.assert_called_once_with("chart", "obj-uuid", TENANT_A)

    outbox.set_object_tenant.reset_mock()
    assert adopt(User(CALLER_ID), monkeypatch)[0] == 200
    outbox.set_object_tenant.assert_not_called()


def test_only_the_adoption_branch_of_the_visibility_route_reads_the_tenant(
    admitted_writer, write_seams, authz
):
    # An OWNED object's visibility change assigns no owner, so it neither
    # reads nor stamps a tenant: the strict read is the adoption branch's
    # cost alone.
    admitted_writer(ADMIN)
    assert call(VIS[0], VIS[1], VIS[2], {"visibility": "private"}) == 200
    authz.object_tenant.assert_not_called()
    outbox.set_object_tenant.assert_not_called()


# --- The tenant is read STRICTLY where "no tenant" would mean "stamp" -------

from superset_ownership.fga import StoreRejected, StoreUnavailable  # noqa: E402

STAMPING_WRITES = [
    ("owner", OWN, {"subject": "user:bob"}, row(OTHER_ID)),
    ("claim", CLAIM, None, row(OTHER_ID)),
    ("adopt", VIS, {"visibility": "private"}, row(None)),
]


@pytest.mark.parametrize(
    "failure, phrase",
    [
        (
            StoreUnavailable("read_all chart:obj-uuid: ConnectionError"),
            "is unavailable",
        ),
        (StoreRejected("read_all chart:obj-uuid: HTTP 400"), "refused the lookup"),
    ],
    ids=["unavailable", "rejected"],
)
@pytest.mark.parametrize(
    "name, route, body, the_row", STAMPING_WRITES, ids=[w[0] for w in STAMPING_WRITES]
)
def test_a_write_that_may_stamp_a_tenant_answers_503_when_the_store_cannot_be_read(
    admitted_writer,
    write_seams,
    authz,
    monkeypatch,
    name,
    route,
    body,
    the_row,
    failure,
    phrase,
):
    # A non-strict read answers None for an outage, and None is exactly the
    # answer that skips the recipient-tenant refusal and selects the stamp
    # branch. The caller here is the one whose recipient validation makes
    # no strict store read of its own -- a Superset admin -- naming a
    # recipient in another tenant; with the store down that used to be a
    # 200 and a `set_object_tenant(B)`.
    admitted_writer(ADMIN)
    monkeypatch.setattr(service, "lock_object", lambda asset_type, pk: the_row)
    admitted_writer.recipient.roles = [Role(f"tenant_{TENANT_B}")]
    authz.object_tenant.side_effect = failure

    with flask.Flask(__name__).test_request_context(
        route[2], method=route[1], json=body
    ):
        response = route[0]()

    assert response[1] == 503, name
    message = response[0].get_json()["message"]
    assert message.startswith("cannot verify the object's tenant"), message
    assert phrase in message
    assert "nothing was changed" in message
    authz.object_tenant.assert_called_once_with("chart", "obj-uuid", strict=True)
    write_seams.assert_not_called()
    service.upsert_ownership.assert_not_called()
    service.set_visibility.assert_not_called()
    outbox.write_tuple.assert_not_called()
    outbox.delete_tuple.assert_not_called()
    outbox.set_object_tenant.assert_not_called()


def test_the_strict_tenant_read_is_made_before_the_recipient_baseline_check(
    admitted_writer, write_seams, authz, monkeypatch
):
    admitted_writer(OWNER)
    baseline = mock.Mock(return_value=None)
    monkeypatch.setattr(api, "_refuse_transfer", baseline)
    authz.object_tenant.side_effect = StoreUnavailable("down")

    assert call(OWN[0], OWN[1], OWN[2], {"subject": "user:bob"}) == 503
    baseline.assert_not_called()


def test_the_local_backend_accepts_a_strict_tenant_read(superset_stub, monkeypatch):
    # No store, nothing to fail: the owner's tenant, as before.
    from superset_ownership.authz import LocalAuthorizer

    monkeypatch.setattr(
        service, "lookup_by_uuid", lambda asset_type, object_uuid: row(OTHER_ID)
    )
    superset_stub.security_manager.get_user_by_id.return_value = holder(
        TENANT_A, with_role=False
    )
    assert LocalAuthorizer().object_tenant("chart", "obj-uuid", strict=True) == TENANT_A
    monkeypatch.setattr(
        service, "lookup_by_uuid", lambda asset_type, object_uuid: row(None)
    )
    assert LocalAuthorizer().object_tenant("chart", "obj-uuid", strict=True) is None


# --- The collusion paths are attributable ------------------------------------


def test_every_write_under_the_permission_names_it_and_its_subject(
    admitted_writer, write_seams
):
    # The matrix does not close two people acting in turn (a holder hands
    # the object to an accomplice who hands it back; two holders share each
    # other as editors). What it guarantees is that each leg is one event
    # carrying `admitted_by` and the subject, so the trail shows both legs
    # and who took each. Here: the first leg of a round trip, and one
    # editor share, both under the permission; the return leg, as owner.
    admitted_writer(MANAGE_PERMISSION)
    assert call(OWN[0], OWN[1], OWN[2], {"subject": "user:bob"}) == 200
    leg_one = write_seams.call_args
    assert leg_one.args[0] == audit.OWNER_ASSIGNED
    assert leg_one.kwargs["admitted_by"] == MANAGE_PERMISSION
    # `editor_user_ids` (review M-1): the before/after native-editors pair
    # `_set_asset_owner` now puts on this event; `write_seams` stubs
    # `asset.editors` as a real (empty) list, so both are empty here.
    assert leg_one.kwargs["after"] == {"owner_user_id": OTHER_ID, "editor_user_ids": []}

    assert (
        call(ADD[0], ADD[1], ADD[2], {"subject": "user:bob", "role": "editor"}) == 200
    )
    share = write_seams.call_args
    assert share.args[0] == audit.SHARE_ADDED
    assert share.kwargs["admitted_by"] == MANAGE_PERMISSION
    assert share.kwargs["after"] == {"subject": "user:bob", "role": "editor"}

    admitted_writer(OWNER)
    assert call(OWN[0], OWN[1], OWN[2], {"subject": SELF}) == 200
    leg_two = write_seams.call_args
    assert leg_two.kwargs["admitted_by"] == OWNER
    assert leg_two.kwargs["after"] == {
        "owner_user_id": CALLER_ID,
        "editor_user_ids": [],
    }


def test_refuse_transfer_requires_the_admitting_rule():
    # Keyword-only and required: a caller that forgot it would emit
    # owner_refused without the key `audit.py` documents as present.
    with pytest.raises(TypeError):
        api._refuse_transfer(
            "chart", 7, mock.Mock(), row(OTHER_ID), User(OTHER_ID), User(CALLER_ID)
        )


@pytest.mark.parametrize(
    "name, route, method, path, body, event", WRITES, ids=[w[0] for w in WRITES]
)
def test_each_write_is_refused_and_emits_nothing_when_no_rule_admits(
    write_seams, monkeypatch, name, route, method, path, body, event
):
    monkeypatch.setattr(api, "_authn", lambda mutating=False: holder())
    monkeypatch.setattr(service, "lock_object", lambda asset_type, pk: row(OTHER_ID))
    monkeypatch.setattr(api, "_manage_reason", lambda r, u: None)

    assert call(route, method, path, body) == 403, name
    write_seams.assert_not_called()


# --- The per-object write lock comes first on every write ------------------
#
# Outbox ids are given at INSERT; the lock (service.lock_object, held to the
# commit) is what makes their order the commit order per object. So on every
# mutating route it has to be taken BEFORE the first write -- mirror or outbox
# -- and it must be the only lock the route takes.

MIRROR_WRITES = ("upsert_ownership", "set_visibility", "add_share", "remove_share")
OUTBOX_WRITES = ("write_tuple", "delete_tuple", "set_object_tenant", "revoke_subject")


@pytest.fixture
def write_order(write_seams, admitted_writer, monkeypatch):
    """Records, in order, the lock and every mirror / outbox write a route
    makes; returns the list."""
    events: list[str] = []

    def lock(asset_type, pk):
        events.append("lock")
        # H1 (review round 1, issue #93): `shared`, not the `row()` default
        # (`private`) -- `_add_asset_share` now refuses a share write on a
        # `private` object outright, which is not what the lock/decision-
        # ordering tests built on this fixture are about.
        return row(OTHER_ID, visibility="shared")

    def recorder(name):
        return mock.Mock(side_effect=lambda *a, **k: events.append(name) or True)

    monkeypatch.setattr(service, "lock_object", lock)
    for name in MIRROR_WRITES:
        monkeypatch.setattr(service, name, recorder(name))
    for name in OUTBOX_WRITES:
        monkeypatch.setattr(outbox, name, recorder(name))
    admitted_writer(OWNER)
    return events


WRITE_PARAMS = "name, route, method, path, body, event"
WRITE_IDS = [w[0] for w in WRITES]


@pytest.mark.parametrize(WRITE_PARAMS, WRITES, ids=WRITE_IDS)
def test_every_mutating_route_locks_the_object_before_its_first_write(
    write_order, name, route, method, path, body, event
):
    assert call(route, method, path, body) == 200, name
    writes = [e for e in write_order if e != "lock"]
    assert writes, (name, "the route wrote nothing")
    assert write_order[0] == "lock", (name, write_order)
    assert write_order.count("lock") == 1, (name, write_order)


@pytest.mark.parametrize(WRITE_PARAMS, WRITES, ids=WRITE_IDS)
def test_a_refused_write_still_took_the_lock_first_and_wrote_nothing(
    write_order, monkeypatch, name, route, method, path, body, event
):
    # The lock precedes the decision, so a route refused by the seam has
    # locked and released (on the request's rollback), and written nothing.
    monkeypatch.setattr(api, "_manage_reason", lambda r, u: None)
    assert call(route, method, path, body) == 403, name
    assert write_order == ["lock"], (name, write_order)


# The reads of the ownership row that feed a route's decisions. The lock has
# to precede the FIRST of them -- not merely the first write -- or the
# decision is made on a copy another writer may have changed by the time
# the row is held.
DECISION_READS = ("_is_parked", "_is_unowned", "_manage_reason", "_is_ownable")


@pytest.fixture
def decision_order(write_order, monkeypatch):
    """`write_order` plus every decision read of the row, and the subject
    validation, in the order the route makes them."""

    def recorder(name, result):
        def record(*args, **kwargs):
            write_order.append(name)
            return result

        return record

    monkeypatch.setattr(api, "_is_parked", recorder("_is_parked", False))
    monkeypatch.setattr(api, "_is_unowned", recorder("_is_unowned", False))
    monkeypatch.setattr(api, "_manage_reason", recorder("_manage_reason", OWNER))
    monkeypatch.setattr(api, "_is_ownable", recorder("_is_ownable", True))
    monkeypatch.setattr(
        api, "_validate_subject", recorder("_validate_subject", (True, ""))
    )
    return write_order


@pytest.mark.parametrize(WRITE_PARAMS, WRITES, ids=WRITE_IDS)
def test_every_mutating_route_locks_before_its_first_decision_read(
    decision_order, name, route, method, path, body, event
):
    # Moving a route's lock to just before its first write -- after the
    # parked / unowned / admitting-rule reads -- would still pass the
    # write-order tests above. This pins the lock ahead of the reads.
    assert call(route, method, path, body) == 200, name
    reads = [e for e in decision_order if e in DECISION_READS]
    assert reads, (name, decision_order)
    assert reads[0] == "_is_parked", (name, decision_order)
    assert "_manage_reason" in reads, (name, decision_order)
    lock_at = decision_order.index("lock")
    before_lock = set(decision_order[:lock_at])
    assert not before_lock & set(DECISION_READS), (name, decision_order)
    assert decision_order[lock_at + 1] == "_is_parked", (name, decision_order)


def test_the_share_route_validates_the_subject_before_it_takes_the_lock(decision_order):
    # The subject check reads the caller's tenant, the subject's account and
    # (for a group) the store -- never the ownership row -- so it runs
    # before the lock and those reads are not made while the object's
    # writers queue behind this request. The row's decision reads all
    # still follow the lock.
    body = {"subject": "user:bob", "role": "viewer"}
    assert call(ADD[0], ADD[1], ADD[2], body) == 200
    assert decision_order.index("_validate_subject") < decision_order.index("lock")
    assert decision_order.index("lock") < decision_order.index("_is_parked")


def test_a_rejected_subject_is_reported_only_to_an_admitted_caller(
    decision_order, monkeypatch, write_seams
):
    """The subject is validated before the lock (its store calls stay off
    it) but its verdict is held: a caller the object refuses is answered
    the uniform 403 whatever the subject, and only an admitted caller is
    told what is wrong with it. Nothing is written in either case."""
    monkeypatch.setattr(
        api,
        "_validate_subject",
        lambda subject, caller, **_: (
            decision_order.append("_validate_subject")
            or (False, "subject does not name a known account")
        ),
    )
    # Refused caller: 403, after the lock and the row's decision reads.
    monkeypatch.setattr(
        api,
        "_manage_reason",
        lambda r, u: decision_order.append("_manage_reason") or None,
    )
    assert call(ADD[0], ADD[1], ADD[2], {"subject": "user:nobody"}) == 403
    assert decision_order == [
        "_validate_subject",
        "lock",
        "_is_parked",
        "_is_unowned",
        "_manage_reason",
    ], decision_order
    write_seams.assert_not_called()
    # Admitted caller: the held 400, still before any write.
    decision_order.clear()
    monkeypatch.setattr(
        api,
        "_manage_reason",
        lambda r, u: decision_order.append("_manage_reason") or OWNER,
    )
    assert call(ADD[0], ADD[1], ADD[2], {"subject": "user:nobody"}) == 400
    assert decision_order == [
        "_validate_subject",
        "lock",
        "_is_parked",
        "_is_unowned",
        "_manage_reason",
    ], decision_order
    write_seams.assert_not_called()
    # A body with no subject, or a bad role, reads nothing and is refused
    # before the lock: the shape of the request is not a fact about anyone.
    decision_order.clear()
    monkeypatch.setattr(
        api, "_validate_subject", lambda subject, caller, **_: (True, "")
    )
    assert call(ADD[0], ADD[1], ADD[2], {"subject": "user:bob", "role": "god"}) == 400
    assert call(ADD[0], ADD[1], ADD[2], {"role": "viewer"}) == 400
    assert decision_order == [], decision_order


def test_a_subject_only_the_store_can_settle_is_settled_after_admission(
    decision_order, monkeypatch, write_seams
):
    """The pre-lock check is asked without the store (`store=False`) and
    may come back undecided (None). That case is asked again, with the
    store, only once `_manage_reason` has admitted the caller: a refused
    caller never causes the member paging, so its duration cannot tell
    them the subject's account exists in another tenant."""

    def validate(subject, caller, store=True):
        decision_order.append(
            "_validate_subject+store" if store else "_validate_subject"
        )
        return (True, "") if store else (None, "")

    monkeypatch.setattr(api, "_validate_subject", validate)
    # Refused caller: one store-less check, the lock, the reads, the 403.
    monkeypatch.setattr(
        api,
        "_manage_reason",
        lambda r, u: decision_order.append("_manage_reason") or None,
    )
    assert call(ADD[0], ADD[1], ADD[2], {"subject": f"user:{OUTSIDER_GUID}"}) == 403
    assert decision_order == [
        "_validate_subject",
        "lock",
        "_is_parked",
        "_is_unowned",
        "_manage_reason",
    ], decision_order
    write_seams.assert_not_called()
    # Admitted caller: the same, then the check with the store, then the
    # share goes through.
    decision_order.clear()
    monkeypatch.setattr(
        api,
        "_manage_reason",
        lambda r, u: decision_order.append("_manage_reason") or OWNER,
    )
    assert call(ADD[0], ADD[1], ADD[2], {"subject": f"user:{OUTSIDER_GUID}"}) == 200
    assert decision_order[:6] == [
        "_validate_subject",
        "lock",
        "_is_parked",
        "_is_unowned",
        "_manage_reason",
        "_validate_subject+store",
    ], decision_order
    write_seams.assert_called_once()


OUTSIDER_GUID = "7c6b5a49-1b2c-4d3e-8f9a-0b1c2d3e4f5a"
NOBODY_GUID = "00000000-0000-4000-8000-000000000000"


@pytest.fixture
def real_subject_validation(admitted_writer, superset_stub, authz, monkeypatch):
    """The share route with the REAL `_validate_subject` behind it and three
    subjects to name: Bob, a member of the caller's tenant; an account in
    another tenant; and a GUID that names nobody. The store lists no
    members for the caller's tenant, so the outsider is refused on their
    role."""
    monkeypatch.setattr(api, "_validate_subject", _real_validate_subject)
    bob = User(OTHER_ID, username=BOB_GUID, roles=[Role(f"tenant_{TENANT_A}")])
    outsider = User(11, username=OUTSIDER_GUID, roles=[Role(f"tenant_{TENANT_B}")])
    bob.is_active = outsider.is_active = True
    accounts = {BOB_GUID: bob, OUTSIDER_GUID: outsider}
    monkeypatch.setattr(
        identity,
        "normalize_subject",
        lambda subject: subject if subject.split(":", 1)[1] in accounts else None,
    )
    superset_stub.security_manager.find_user.side_effect = (
        lambda username=None, email=None: accounts.get(username)
    )
    authz.directory.user_in_tenant.return_value = False
    return admitted_writer


SUBJECT_CASES = [
    ("unknown account", f"user:{NOBODY_GUID}", "subject does not name a known account"),
    (
        "account in another tenant",
        f"user:{OUTSIDER_GUID}",
        "subject is not a member of your tenant",
    ),
    ("member of the caller's tenant", f"user:{BOB_GUID}", None),
]


@pytest.mark.parametrize(
    "case, subject, why", SUBJECT_CASES, ids=[c[0] for c in SUBJECT_CASES]
)
def test_a_refused_caller_is_answered_the_same_403_whatever_the_subject(
    real_subject_validation, write_seams, case, subject, why
):
    """A caller with no right to manage the object learns nothing about
    the subject: not whether the account exists, not which tenant it is
    in. One status, one body, for all three."""
    real_subject_validation(None)
    status, body = call_full(
        ADD[0], ADD[1], ADD[2], {"subject": subject, "role": "viewer"}
    )
    assert (status, body) == (403, {"message": api._SHARES_403}), case
    write_seams.assert_not_called()


@pytest.mark.parametrize(
    "case, subject, why", SUBJECT_CASES, ids=[c[0] for c in SUBJECT_CASES]
)
def test_a_refused_caller_never_causes_the_store_to_be_paged(
    real_subject_validation, authz, case, subject, why
):
    """The timing half of the same guarantee, with the real check: the
    directory read (`user_in_tenant`, one round trip for the caller's
    tenant) is made for exactly one of the three subjects -- the account in
    another tenant -- and only after admission. Refused, all three cost the
    same Superset lookups and no store call."""
    real_subject_validation(None)
    assert call(ADD[0], ADD[1], ADD[2], {"subject": subject, "role": "viewer"}) == 403
    authz.directory.user_in_tenant.assert_not_called()


@pytest.mark.parametrize(
    "case, subject, why", SUBJECT_CASES, ids=[c[0] for c in SUBJECT_CASES]
)
def test_an_admitted_caller_is_told_what_is_wrong_with_the_subject(
    real_subject_validation, write_seams, authz, case, subject, why
):
    real_subject_validation(OWNER)
    status, body = call_full(
        ADD[0], ADD[1], ADD[2], {"subject": subject, "role": "viewer"}
    )
    if why is None:
        assert status == 200, (case, body)
        assert body["subject"] == subject
        write_seams.assert_called_once()
    else:
        assert (status, body) == (400, {"message": why}), case
        write_seams.assert_not_called()
    # The store is read for the admitted caller, and for the one subject
    # that needs it -- under the lock, after admission (the ordering test
    # above), not before.
    if case == "account in another tenant":
        authz.directory.user_in_tenant.assert_called_once_with(OUTSIDER_GUID, TENANT_A)
    else:
        authz.directory.user_in_tenant.assert_not_called()


def test_every_asset_type_a_route_serves_has_an_advisory_lock_class():
    """`service._advisory_lock_key` refuses an asset type it has no code
    for, and `lock_object` would surface that as a 500. The routes
    dispatch on `_ASSET_LOADERS`; a third asset type added there and not
    to `ADVISORY_LOCK_CLASS` (or the reverse) fails here, not in
    production."""
    assert set(service.ADVISORY_LOCK_CLASS) == set(api._ASSET_LOADERS)


def test_a_holder_admitted_through_the_real_seam_is_recorded_as_such(
    write_seams, reach_seams, superset_stub, authz, monkeypatch
):
    # End to end through `_manage_reason`: config on, role held, own tenant,
    # object shared with them -> the visibility write goes through and the
    # event says the permission admitted it, not ownership.
    monkeypatch.setenv(api.MANAGE_PERMISSION_CONFIG_KEY, MANAGE_ROLE)
    monkeypatch.setattr(api, "_authn", lambda mutating=False: holder())
    monkeypatch.setattr(service, "lock_object", lambda asset_type, pk: row(OTHER_ID))
    monkeypatch.setattr(
        service, "owned_or_shared_object_ids", lambda asset_type, user_id: {7}
    )
    monkeypatch.setattr(identity, "resolve_member_guid", lambda user: "caller-guid")
    authz.object_tenant.return_value = TENANT_A

    status = call(
        lambda: api._set_asset_visibility("chart", 7),
        "PUT",
        "/api/v1/ownership/chart/7/visibility",
        {"visibility": "shared"},
    )

    assert status == 200
    assert write_seams.call_args.kwargs["admitted_by"] == MANAGE_PERMISSION

    # The same holder, from tenant B: refused, and nothing is recorded.
    write_seams.reset_mock()
    monkeypatch.setattr(api, "_authn", lambda mutating=False: holder(TENANT_B))
    status = call(
        lambda: api._set_asset_visibility("chart", 7),
        "PUT",
        "/api/v1/ownership/chart/7/visibility",
        {"visibility": "shared"},
    )
    assert status == 403
    write_seams.assert_not_called()


def test_a_refused_transfer_records_the_admitting_rule_too(
    write_seams, superset_stub, monkeypatch
):
    # A holder transferring to a third party who lacks the dataset grant:
    # the transfer the permission allows, refused by the recipient baseline
    # check, and the event says under which ground it was attempted.
    from superset_ownership import transfer

    monkeypatch.setattr(api, "_authn", lambda mutating=False: holder())
    monkeypatch.setattr(service, "lock_object", lambda asset_type, pk: row(OTHER_ID))
    monkeypatch.setattr(api, "_manage_reason", lambda r, u: MANAGE_PERMISSION)
    monkeypatch.setattr(
        transfer,
        "decide",
        lambda *a, **k: transfer.Decision(
            outcome=transfer.MISSING_GRANT, message="no grant"
        ),
    )
    monkeypatch.setattr(hooks, "base_permission_holds_for", lambda *a: False)
    monkeypatch.setattr(hooks, "dataset_resolves", lambda *a: True)
    recipient = User(OTHER_ID, username="bob")
    recipient.is_active = True
    superset_stub.security_manager.find_user.return_value = recipient

    status = call(
        lambda: api._set_asset_owner("chart", 7),
        "PUT",
        "/api/v1/ownership/chart/7/owner",
        {"subject": "user:bob"},
    )

    assert status == 409
    args, kwargs = write_seams.call_args
    assert args[0] == audit.OWNER_REFUSED
    assert kwargs["admitted_by"] == MANAGE_PERMISSION
    assert kwargs["attempted"]["reason"] == transfer.MISSING_GRANT


# --- GET /<asset>/<pk> ------------------------------------------------------


@pytest.fixture
def detail_seams(superset_stub, authz, monkeypatch):
    """Everything `_get_asset_detail` reaches for, pinned to a known shape.

    Returns the mock standing in for `_manage_reason`, so a test can set the
    reason and check it was consulted exactly once.
    """
    asset = mock.Mock()
    asset.id = 7
    monkeypatch.setattr(api, "_authn", lambda mutating=False: User(CALLER_ID))
    monkeypatch.setattr(api, "_get_asset", lambda asset_type, pk: asset)
    monkeypatch.setattr(api, "_is_unowned", lambda r: False)
    reason = mock.Mock(return_value=None)
    monkeypatch.setattr(api, "_manage_reason", reason)
    monkeypatch.setattr(
        service,
        "get_user_info",
        lambda user_id: {
            "id": user_id,
            "name": "Jane Doe",
            "guid": "jane-guid",
            "email": "jane@example.com",
        },
    )
    monkeypatch.setattr(service, "list_shares", lambda asset_type, pk: [])
    monkeypatch.setattr(
        service, "owned_or_shared_object_ids", lambda asset_type, user_id: set()
    )
    return reason


def get_detail(monkeypatch, the_row):
    monkeypatch.setattr(service, "lookup", lambda asset_type, pk: the_row)
    with flask.Flask(__name__).test_request_context("/api/v1/ownership/chart/7"):
        response = api._get_asset_detail("chart", 7)
    if isinstance(response, tuple):
        body, status = response
        return status, body.get_json()
    return response.status_code, response.get_json()


@pytest.mark.parametrize("reason", [OWNER, TENANT_ADMIN, ADMIN])
def test_detail_reports_the_manage_reason(detail_seams, monkeypatch, reason):
    detail_seams.return_value = reason
    status, body = get_detail(monkeypatch, row(OTHER_ID))

    assert status == 200
    assert body["can_manage"] is True
    # A tenant administrator manages (transfer, claim) but does not share.
    assert body["can_share"] is (reason != TENANT_ADMIN)
    assert body["manage_reason"] == reason
    # The full owner block, since the caller manages the object.
    assert body["owner"] == {
        "id": OTHER_ID,
        "name": "Jane Doe",
        "guid": "jane-guid",
        "email": "jane@example.com",
    }
    # The CALLER's own store reference -- `User(CALLER_ID)` has no GUID on
    # its username or email, so it is the `local-<id>` spelling -- never
    # the owner's ("jane-guid").
    assert body["caller_subject"] == f"user:local-{CALLER_ID}"
    # Evaluated once: it is an authorization-store round-trip.
    assert detail_seams.call_count == 1
    if reason == TENANT_ADMIN:
        # ... unless they also hold the manage-sharing permission.
        monkeypatch.setattr(
            api, "_manage_permission_reason", lambda r, u: MANAGE_PERMISSION
        )
        status, body = get_detail(monkeypatch, row(OTHER_ID))
        assert (status, body["can_share"], body["manage_reason"]) == (
            200,
            True,
            TENANT_ADMIN,
        )


def test_detail_nulls_the_reason_for_a_parked_object(detail_seams, monkeypatch):
    # The caller would manage it, but the object is parked: no controls, so
    # no reason for controls either, while the object stays visible.
    detail_seams.return_value = OWNER
    status, body = get_detail(monkeypatch, row(CALLER_ID, visibility="disabled:shared"))

    assert status == 200
    assert body["disabled"] is True
    assert body["visibility"] == "shared"
    assert body["can_manage"] is False
    assert body["can_share"] is False
    assert body["manage_reason"] is None
    assert body["owner"]["email"] == "jane@example.com"


def test_detail_nulls_the_reason_for_a_reader(detail_seams, monkeypatch):
    # A public object read by someone with no standing on it: the reduced
    # owner block, no shares, and no reason.
    status, body = get_detail(monkeypatch, row(OTHER_ID, visibility="public"))

    assert status == 200
    assert body["can_manage"] is False
    assert body["manage_reason"] is None
    assert body["owner"] == {"id": OTHER_ID, "name": "Jane Doe"}
    assert body["shares"] == []
    # A reader is told their OWN reference too: it names nobody else.
    assert body["caller_subject"] == f"user:local-{CALLER_ID}"


def test_detail_spells_the_caller_subject_as_the_store_does(detail_seams, monkeypatch):
    # A GUID username is lower-cased, as `resolve_member_guid` reports it
    # and as /subjects spells a share to the same account.
    detail_seams.return_value = OWNER
    monkeypatch.setattr(
        api, "_authn", lambda mutating=False: User(CALLER_ID, username=BOB_GUID_UPPER)
    )
    status, body = get_detail(monkeypatch, row(CALLER_ID))

    assert status == 200
    assert body["caller_subject"] == f"user:{BOB_GUID}"


def test_detail_hides_a_private_object_from_a_stranger(detail_seams, monkeypatch):
    status, body = get_detail(monkeypatch, row(OTHER_ID))

    assert status == 404
    assert "manage_reason" not in body
    assert "caller_subject" not in body


# --- GET /subjects ----------------------------------------------------------


def search(monkeypatch, caller, query=""):
    monkeypatch.setattr(api, "_authn", lambda mutating=False: caller)
    path = "/api/v1/ownership/subjects"
    with flask.Flask(__name__).test_request_context(path, query_string={"q": query}):
        response = api.search_subjects()
    return response.get_json()


def in_tenant(user_id, tenant=TENANT_A, **fields):
    return User(user_id, roles=[Role(f"tenant_{tenant}")], **fields)


@pytest.fixture
def directory(directory_mock):
    """Four tenant members the directory answers with: one known by its
    `local-<id>` spelling, one by an upper-cased GUID username (lower-cased,
    as the store spells it), one with no display name, and one Superset has
    never heard of.

    Labelling and query filtering moved to the `Directory`
    (`OpenFGADirectory.search_users`, whose own behaviour is covered in
    test_directory.py); this fake reproduces just enough of it -- a query
    matching name, guid or email -- to exercise the route's plumbing and
    mapping, not re-test the directory's own filtering.
    """
    users = {
        "local-1": {
            "guid": "local-1",
            "display_name": "Jane Doe",
            "email": "jane@example.com",
            "superset_id": 1,
        },
        BOB_GUID: {
            "guid": BOB_GUID,
            "display_name": "Bob Smith",
            "email": "bob@example.com",
            "superset_id": 2,
        },
        "local-3": {
            "guid": "local-3",
            "display_name": "carol",
            "email": None,
            "superset_id": 3,
        },
        "ghost-guid": {
            "guid": "ghost-guid",
            "display_name": "ghost-guid",
            "email": None,
            "superset_id": None,
        },
    }

    def fake_search_users(tenant, query, *, limit=100, cursor=None):
        q = (query or "").lower()
        items = [
            u
            for u in users.values()
            if not q
            or q in u["display_name"].lower()
            or q in u["guid"].lower()
            or q in (u["email"] or "").lower()
        ]
        return {"items": items, "next_cursor": None}

    directory_mock.search_users.side_effect = fake_search_users
    return directory_mock


def test_subjects_are_labelled_for_the_store_spelling(directory, monkeypatch):
    body = search(monkeypatch, in_tenant(1))

    assert body["tenant"] == TENANT_A
    by_guid = {r["extra"]["guid"]: r for r in body["result"]}
    assert set(by_guid) == {"local-1", BOB_GUID, "local-3", "ghost-guid"}

    jane = by_guid["local-1"]
    assert (jane["value"], jane["text"]) == ("user:local-1", "Jane Doe")
    assert jane["extra"] == {
        "type": "user",
        "guid": "local-1",
        "email": "jane@example.com",
        "id": 1,
    }
    bob = by_guid[BOB_GUID]
    assert (bob["text"], bob["extra"]["id"], bob["extra"]["email"]) == (
        "Bob Smith",
        2,
        "bob@example.com",
    )
    # No display name: labelled by username, still carrying the id.
    assert (by_guid["local-3"]["text"], by_guid["local-3"]["extra"]["id"]) == (
        "carol",
        3,
    )
    # Unknown to Superset: labelled by the GUID, no email, no id.
    ghost = by_guid["ghost-guid"]
    assert (ghost["text"], ghost["extra"]["email"], ghost["extra"]["id"]) == (
        "ghost-guid",
        None,
        None,
    )


@pytest.mark.parametrize(
    "query, expected",
    [
        ("smith", ["Bob Smith"]),  # name
        ("local-1", ["Jane Doe"]),  # store GUID
        ("bob@", ["Bob Smith"]),  # email, which the picker shows
        ("EXAMPLE.COM", ["Bob Smith", "Jane Doe"]),  # case-insensitive
        ("zzz", []),
    ],
)
def test_subjects_search_matches_name_guid_and_email(
    directory, monkeypatch, query, expected
):
    body = search(monkeypatch, in_tenant(1), query)
    assert sorted(r["text"] for r in body["result"]) == sorted(expected)


def test_subjects_flag_the_tenants_structural_groups(directory, monkeypatch):
    # The administrator group (and, were a backend to list it, the
    # membership group) is a population, not a third party: a holder of the
    # manage-sharing permission may not share to or unshare it. The server
    # says which rows those are, in whichever group-id format is configured,
    # so the client locks them without re-deriving the rule.
    directory.list_groups.return_value = {
        "items": [
            {
                "id": f"group:tenant_administrator_{TENANT_A}",
                "display_name": "tenant administrator",
                "tenant": TENANT_A,
                "members": 1,
            },
            {
                "id": f"group:tenant_{TENANT_A}",
                "display_name": "tenant",
                "tenant": TENANT_A,
                "members": 2,
            },
            {
                "id": f"group:dashboard_designers_{TENANT_A}",
                "display_name": "dashboard designers",
                "tenant": TENANT_A,
                "members": 1,
            },
        ],
        "next_cursor": None,
    }
    body = search(monkeypatch, in_tenant(1))

    groups = {
        r["value"]: r["extra"] for r in body["result"] if r["extra"]["type"] == "group"
    }
    assert groups == {
        f"group:tenant_administrator_{TENANT_A}#member": {
            "type": "group",
            "members": 1,
            "in_superset": False,
            "structural": True,
        },
        f"group:tenant_{TENANT_A}#member": {
            "type": "group",
            "members": 2,
            "in_superset": False,
            "structural": True,
        },
        f"group:dashboard_designers_{TENANT_A}#member": {
            "type": "group",
            "members": 1,
            "in_superset": False,
            "structural": False,
        },
    }
    # Users carry no such key.
    assert all(
        "structural" not in r["extra"]
        for r in body["result"]
        if r["extra"]["type"] == "user"
    )


def test_subjects_flag_the_structural_group_in_the_prefix_format(
    directory, monkeypatch
):
    monkeypatch.setenv(
        identity.GROUP_ID_FORMAT_SETTING, identity.GROUP_ID_FORMAT_PREFIX
    )
    directory.list_groups.return_value = {
        "items": [
            {
                "id": f"group:{TENANT_A}_tenant_administrator",
                "display_name": "tenant administrator",
                "tenant": TENANT_A,
                "members": 0,
            },
            {
                "id": f"group:{TENANT_A}_dashboard_designers",
                "display_name": "dashboard designers",
                "tenant": TENANT_A,
                "members": 0,
            },
        ],
        "next_cursor": None,
    }
    body = search(monkeypatch, in_tenant(1))

    flags = {
        r["text"]: r["extra"]["structural"]
        for r in body["result"]
        if r["extra"]["type"] == "group"
    }
    assert flags == {"tenant administrator": True, "dashboard designers": False}


def test_subjects_are_empty_for_a_caller_with_no_tenant(directory, monkeypatch):
    body = search(monkeypatch, User(1, username="admin"))

    assert body == {"result": [], "tenant": None, "source": "openfga"}
    directory.search_users.assert_not_called()
    directory.list_groups.assert_not_called()


def test_subjects_default_limit_returns_users_past_a_hundred_members(
    superset_stub, monkeypatch
):
    """I-1, run through the real route and a REAL `OpenFGADirectory` rather
    than the mocked `directory` fixture every other test in this section
    uses: `search_subjects` asks for `limit=200` (api.py's own default),
    and a tenant with more than 100 members used to come back with NO
    users at all, `degraded: true` -- OpenFGA rejects a `page_size` over
    100 outright, and `search_users` used to request the caller's entire
    remaining budget (200) in one page. 140 members here, comfortably past
    the 100-per-page cap; all of them must come back, and `degraded` must
    be absent (`FakeStore` -- `test_directory.py`'s fake, imported rather
    than re-implemented here -- enforces the same `[1, 100]` range the
    reviewer's own reproduction hit against the real store)."""
    from superset_ownership import fga
    from superset_ownership.directory import OpenFGADirectory
    from test_directory import FakeStore

    store = FakeStore()
    guids = [f"{i:08x}-0000-4000-8000-{i:012x}" for i in range(140)]
    store.tenant_members[TENANT_A] = guids
    monkeypatch.setattr(fga.requests, "post", store.post)
    monkeypatch.setattr(api, "get_directory", lambda: OpenFGADirectory())
    superset_stub.security_manager.user_model = object()
    superset_stub.db.session.query.return_value.all.return_value = []

    body = search(monkeypatch, in_tenant(1))

    assert "degraded" not in body
    users = [r for r in body["result"] if r["extra"]["type"] == "user"]
    assert len(users) == len(guids)
    assert {u["extra"]["guid"] for u in users} == set(guids)


# --- M-2: /subjects re-checks tenant on every plug-in group row -------------


def test_subjects_drops_groups_the_plugin_answers_from_another_or_no_tenant(
    directory, monkeypatch
):
    """M-2: contract section 5 says another tenant's rows are "discarded,
    never surfaced" in OUR code -- a plugged Directory is not trusted to
    have scoped its own answer. Probe shaped like the review's own: a
    foreign-tenant group, an untenanted group, and a same-tenant group in
    one page. This test FAILS if the `group_belongs_to_tenant`/`tenant`
    re-check in `search_subjects` is removed -- the foreign/untenanted rows
    would show up in `body["result"]` alongside the legitimate one."""
    from superset_ownership.identity import group_object

    ok_id = group_object("eng", TENANT_A)
    foreign_id = group_object("eng", TENANT_B)
    untenanted_id = "group:eng"
    directory.list_groups.return_value = {
        "items": [
            {"id": ok_id, "display_name": "eng", "tenant": TENANT_A, "members": 1},
            # The plug-in's own `tenant` field says A, but the id itself
            # parses as B's -- a directory that mislabels its own tenant
            # field must not be trusted over the id it names.
            {
                "id": foreign_id,
                "display_name": "eng (foreign)",
                "tenant": TENANT_A,
                "members": 1,
            },
            {
                "id": untenanted_id,
                "display_name": "eng (bare)",
                "tenant": TENANT_A,
                "members": 1,
            },
        ],
        "next_cursor": None,
    }

    body = search(monkeypatch, in_tenant(1))

    values = {r["value"] for r in body["result"] if r["extra"]["type"] == "group"}
    assert values == {f"{ok_id}#member"}


def test_subjects_drops_a_group_whose_own_tenant_field_disagrees_with_the_caller(
    directory, monkeypatch
):
    """The other half of M-2: a group id that DOES parse as the caller's
    tenant but whose `tenant` field the plug-in filled in wrong is dropped
    too -- the route trusts neither signal alone."""
    from superset_ownership.identity import group_object

    mislabelled_id = group_object("eng", TENANT_A)
    directory.list_groups.return_value = {
        "items": [
            {
                "id": mislabelled_id,
                "display_name": "eng",
                "tenant": TENANT_B,
                "members": 1,
            },
        ],
        "next_cursor": None,
    }

    body = search(monkeypatch, in_tenant(1))

    assert [r for r in body["result"] if r["extra"]["type"] == "group"] == []


# --- M-1: a plug-in exception or malformed Page never reaches the client ----


def test_subjects_search_users_exception_degrades_rather_than_500s(
    directory, monkeypatch
):
    directory.search_users.side_effect = RuntimeError(
        "ldap bind failed: cn=svc,dc=x password=hunter2"
    )
    body = search(monkeypatch, in_tenant(1))

    assert body["degraded"] is True
    assert [r for r in body["result"] if r["extra"]["type"] == "user"] == []
    assert "hunter2" not in str(body)


def test_subjects_list_groups_exception_degrades_rather_than_500s(
    directory, monkeypatch
):
    directory.list_groups.side_effect = RuntimeError("driver exploded")
    body = search(monkeypatch, in_tenant(1))

    assert body["degraded"] is True
    assert "driver exploded" not in str(body)


@pytest.mark.parametrize(
    "bad_page",
    [
        None,
        [],
        {"items": []},  # missing next_cursor
        {"items": None, "next_cursor": None},
        {"items": [{"guid": "g"}], "next_cursor": None},  # item missing keys
    ],
)
def test_subjects_a_malformed_user_page_degrades_rather_than_crashes(
    directory_mock, monkeypatch, bad_page
):
    directory_mock.search_users.return_value = bad_page
    body = search(monkeypatch, in_tenant(1))

    assert body["degraded"] is True
    assert [r for r in body["result"] if r["extra"]["type"] == "user"] == []


def test_subjects_a_malformed_group_page_degrades_rather_than_crashes(
    directory_mock, monkeypatch
):
    directory_mock.list_groups.return_value = {
        "items": [{"id": "group:x"}],
        "next_cursor": None,
    }
    body = search(monkeypatch, in_tenant(1))

    assert body["degraded"] is True
    assert [r for r in body["result"] if r["extra"]["type"] == "group"] == []


def test_validate_subject_a_non_store_error_from_user_in_tenant_never_echoes_its_text(
    directory_mock, superset_stub, monkeypatch
):
    """M-1: a plugged Directory's own exception class (not `StoreError`)
    is caught, logged with a traceback, and classified exactly like any
    other store rejection -- the response never carries the exception's
    own text (which could be a driver's connection string or password).

    The target's own username IS the GUID being validated (N-2: the
    forward mapping has to agree for the role-first lookup to be trusted
    at all), so this pins M-1's behaviour on the CONSISTENT case -- the
    inconsistent one is `test_validate_subject_role_first_branch_
    rejects_an_inconsistent_reverse_hook` below.
    """
    caller = in_tenant(CALLER_ID)
    # The role names a DIFFERENT tenant than the caller's, so the store is
    # consulted -- and raises something that is not a StoreError.
    target = User(OTHER_ID, username=BOB_GUID, roles=[Role(f"tenant_{TENANT_B}")])
    target.is_active = True
    superset_stub.security_manager.find_user.side_effect = (
        lambda username=None, email=None: target if username == BOB_GUID else None
    )
    monkeypatch.setattr(
        identity, "normalize_subject", lambda subject: f"user:{BOB_GUID}"
    )
    directory_mock.user_in_tenant.side_effect = RuntimeError(
        "ldap bind failed password=hunter2"
    )

    ok, why = api._validate_subject(f"user:{BOB_GUID}", caller)

    assert ok is False
    assert "hunter2" not in why
    assert why == (
        "cannot verify tenant membership: the authorization store refused "
        "the lookup (RuntimeError) and the account's role names another tenant"
    )


def test_validate_subject_group_branch_honours_store_false(directory_mock, monkeypatch):
    """L-5: the user branch already answers `(None, "")` -- undecided,
    no store call -- when `store=False`; the group branch used to read the
    directory unconditionally, so a refused caller's pre-403 probe still
    triggered (and, before M-1, could crash on) a directory round trip for
    a group of their own tenant."""
    from superset_ownership.identity import group_object

    caller = in_tenant(CALLER_ID)
    name = group_object("eng", TENANT_A).split(":", 1)[1]

    ok, why = api._validate_subject(f"group:{name}#member", caller, store=False)

    assert (ok, why) == (None, "")
    directory_mock.group_exists.assert_not_called()


def test_validate_subject_role_first_branch_uses_the_identity_reverse_hook(
    superset_stub, directory_mock, monkeypatch
):
    """H-3: the role-first lookup goes through `identity.user_for_member_guid`
    (which an override may relocate), not a raw
    `security_manager.find_user(username=guid)` -- the same assumption H-2
    closes on the Local backend's `user_in_tenant`. The forward mapping
    (`resolve_member_guid`) is mocked to agree with the hook here -- this
    is the CONSISTENT case (N-2 requires it, and the guard would otherwise
    fall this through to the store); the inconsistent case is
    `test_validate_subject_role_first_branch_rejects_an_inconsistent_
    reverse_hook` below."""
    caller = in_tenant(CALLER_ID)
    monkeypatch.setattr(
        identity, "normalize_subject", lambda subject: "user:relocated-guid"
    )
    target = User(
        OTHER_ID,
        username="whatever-the-username-is",
        roles=[Role(f"tenant_{TENANT_A}")],
    )
    target.is_active = True
    calls = []

    def fake_user_for_member_guid(guid):
        calls.append(guid)
        return target if guid == "relocated-guid" else None

    monkeypatch.setattr(identity, "user_for_member_guid", fake_user_for_member_guid)
    # The override's forward direction agrees with its reverse hook --
    # the same fact, both ways -- exactly what N-2's guard checks for.
    monkeypatch.setattr(
        identity,
        "resolve_member_guid",
        lambda user: "relocated-guid" if user is target else None,
    )
    # If the old `find_user(username=guid)` path were still live, this would
    # answer None (no user has that literal username) and the subject would
    # be refused for the wrong reason.
    superset_stub.security_manager.find_user.return_value = None

    ok, why = api._validate_subject("user:relocated-guid", caller)

    assert (ok, why) == (True, "")
    assert calls == ["relocated-guid"]


def test_validate_subject_role_first_branch_rejects_an_inconsistent_reverse_hook(
    real_subject_validation, write_seams, authz, monkeypatch
):
    """N-2 (High): the round-2 regression, reproduced exactly as the
    reviewer verified it on the route -- an admitted owner in tenant A,
    subject naming an outsider's GUID, and a reverse hook that (wrongly)
    answers a real, SAME-tenant account for it, the way a `.first()` over
    a stale or duplicated attribute query would. Before the guard this
    wrote the share (200) even though the store said the outsider was not
    a member; the fix falls through to the store's own verdict whenever
    the hook's answer does not carry the GUID being validated."""
    real_subject_validation(OWNER)
    bob = User(OTHER_ID, username=BOB_GUID, roles=[Role(f"tenant_{TENANT_A}")])
    bob.is_active = True
    # The lying hook: answers Bob (real, tenant A) for ANY guid, including
    # the outsider's -- never checking it is the account the guid names.
    monkeypatch.setattr(identity, "user_for_member_guid", lambda guid: bob)

    status, body = call_full(
        ADD[0], ADD[1], ADD[2], {"subject": f"user:{OUTSIDER_GUID}", "role": "viewer"}
    )

    assert (status, body) == (
        400,
        {"message": "subject is not a member of your tenant"},
    )
    write_seams.assert_not_called()
    authz.directory.user_in_tenant.assert_called_once_with(OUTSIDER_GUID, TENANT_A)


# --- L-2: service.get_user_info routes through the identity seam -----------


def test_get_user_info_routes_name_and_guid_through_the_identity_seam(
    superset_stub, monkeypatch
):
    """L-2: `service.get_user_info` used to keep an inline display-name
    formula and report `guid: user.username` -- both bypassing the
    `Identity` seam every other name/GUID fact in this package goes
    through. Under a plugged override the detail's owner block must show
    the SAME name and GUID the picker (`directory.py`) and the rest of the
    module (`identity.display_name`, `resolve_member_guid`) would -- not
    diverge because this one call site reads the user object directly."""
    target = User(OTHER_ID, username="ben", email="ben@example.com")
    superset_stub.security_manager.user_model = object()
    superset_stub.db.session.get.return_value = target
    monkeypatch.setattr(identity, "display_name", lambda u: "OVERRIDDEN NAME")
    monkeypatch.setattr(identity, "resolve_member_guid", lambda u: "overridden-guid")

    info = service.get_user_info(OTHER_ID)

    assert info == {
        "id": OTHER_ID,
        "name": "OVERRIDDEN NAME",
        "email": "ben@example.com",
        "guid": "overridden-guid",
    }


def test_get_user_info_none_for_a_missing_or_null_user_id(superset_stub):
    assert service.get_user_info(None) is None
    superset_stub.security_manager.user_model = object()
    superset_stub.db.session.get.return_value = None
    assert service.get_user_info(999) is None

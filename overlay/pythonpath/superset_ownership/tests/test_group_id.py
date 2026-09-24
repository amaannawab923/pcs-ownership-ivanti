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
"""The group id format: OWNERSHIP_GROUP_ID_FORMAT and the helpers in
`identity` that every site building or reading a group id goes through.

A group is `group:<id>` and the id carries the tenant GUID. Whether the
tenant goes first ("{tenant}_{name}", the SOW's "tenant-prefixed") or last
("{name}_{tenant}", the pre-configuration shape) is one setting; these
tests run every helper and every consumer under BOTH, so the answer, when
the client gives it, is a config change and nothing else.

Pure: no Superset. The two backends' `groups_for_tenant` are driven through
a fake security manager (local) and patched client functions (OpenFGA), as
the existing tests do; the tenant purge and the consistency check run with
`superset` stubbed in `sys.modules` (the check against a real SQLite mirror).
The `_validate_subject` and purge-route cases need `api.py`, which imports
Flask at module level, and the config-precedence case needs a bare Flask
app; those skip where Flask is not installed.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from unittest import mock

import pytest
from superset_ownership import identity
from superset_ownership.identity import (
    group_belongs_to_tenant,
    group_display_name,
    group_id,
    group_id_format,
    GROUP_ID_FORMAT_NEURONS,
    GROUP_ID_FORMAT_PREFIX,
    GROUP_ID_FORMAT_SETTING,
    GROUP_ID_FORMAT_SUFFIX,
    group_object,
    group_ref,
    GroupIdFormatError,
    parse_group_id_format,
    split_group_id,
    tenant_administrator_group,
)

TENANT_A = "a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40"
TENANT_B = "b7f28e5a-9c14-4d6b-a2f0-5e3d8c1a6b92"
ADA = "3f0a91c7-2d84-4e63-9b15-7c4e8a2f6d31"
BEN = "6c2b48e9-5a71-4f92-8d03-2e9b7c1a4d53"
CLEO = "9d7e35a1-8c62-4b04-a7f1-3d5e9b2c8a76"

FORMATS = [
    pytest.param(GROUP_ID_FORMAT_NEURONS, id="neurons"),
    pytest.param(GROUP_ID_FORMAT_SUFFIX, id="suffix"),
    pytest.param(GROUP_ID_FORMAT_PREFIX, id="prefix"),
]


@pytest.fixture(params=FORMATS)
def fmt(request, monkeypatch):
    """Run the test under one format, set the way a deployment sets it."""
    monkeypatch.setenv(GROUP_ID_FORMAT_SETTING, request.param)
    return request.param


# --- the template -----------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, ("", "tenant", ".", "")),
        ("", ("", "tenant", ".", "")),
        ("   ", ("", "tenant", ".", "")),
        (GROUP_ID_FORMAT_NEURONS, ("", "tenant", ".", "")),
        (GROUP_ID_FORMAT_SUFFIX, ("", "name", "_", "")),
        (GROUP_ID_FORMAT_PREFIX, ("", "tenant", "_", "")),
        ("{tenant}-{name}", ("", "tenant", "-", "")),
        ("{tenant}/{name}", ("", "tenant", "/", "")),
        # Literal text around the pair is allowed; it just has to be there
        # on the way back out too.
        ("grp-{tenant}:{name}", ("grp-", "tenant", ":", "")),
        ("{name}_{tenant}-role", ("", "name", "_", "-role")),
        (" {name}_{tenant} ", ("", "name", "_", "")),
    ],
)
def test_parse_accepts_a_usable_template(raw, expected):
    assert parse_group_id_format(raw) == expected


@pytest.mark.parametrize(
    "raw, why",
    [
        ("{name}{tenant}", "separator"),
        ("{tenant}{name}", "separator"),
        ("{name}", "exactly once"),
        ("{tenant}", "exactly once"),
        ("{name}_{name}", "exactly once"),
        ("{name}_{tenant}_{tenant}", "exactly once"),
        ("{name}_{tenant}_{extra}", "exactly once"),
        ("{name}_{guid}", "exactly once"),
        ("name_tenant", "exactly once"),
        ("{name}_{tenant", "not a valid template"),
        ("{name:>8}_{tenant}", "format spec"),
        ("{name!r}_{tenant}", "format spec"),
        (42, "must be a string"),
        (["{name}_{tenant}"], "must be a string"),
    ],
)
def test_parse_rejects_a_bad_template_by_name(raw, why):
    with pytest.raises(GroupIdFormatError) as exc:
        parse_group_id_format(raw)
    msg = str(exc.value)
    assert GROUP_ID_FORMAT_SETTING in msg
    assert why in msg


def test_a_bad_value_is_a_value_error_for_a_startup_check():
    assert issubclass(GroupIdFormatError, ValueError)


def test_format_read_from_env_then_default(monkeypatch):
    # The default is Neurons' shape: `<tenant-guid>.<group-local-id>`.
    monkeypatch.delenv(GROUP_ID_FORMAT_SETTING, raising=False)
    assert group_id_format() == GROUP_ID_FORMAT_NEURONS
    monkeypatch.setenv(GROUP_ID_FORMAT_SETTING, "")
    assert group_id_format() == GROUP_ID_FORMAT_NEURONS
    # Whitespace-only is blank too, and what is reported is the template in
    # force -- the default -- not the blank that resolved to it.
    monkeypatch.setenv(GROUP_ID_FORMAT_SETTING, "   ")
    assert group_id_format() == GROUP_ID_FORMAT_NEURONS
    assert group_id("eng", TENANT_A) == f"{TENANT_A}.eng"
    assert split_group_id(f"group:{TENANT_A}.eng#member") == ("eng", TENANT_A)
    monkeypatch.setenv(GROUP_ID_FORMAT_SETTING, GROUP_ID_FORMAT_PREFIX)
    assert group_id_format() == GROUP_ID_FORMAT_PREFIX


def test_a_bad_env_value_fails_at_first_use_not_silently(monkeypatch):
    monkeypatch.setenv(GROUP_ID_FORMAT_SETTING, "{name}{tenant}")
    with pytest.raises(GroupIdFormatError):
        group_id("dashboard_designer", TENANT_A)
    with pytest.raises(GroupIdFormatError):
        split_group_id(f"dashboard_designer_{TENANT_A}")


def test_flask_config_wins_over_env(monkeypatch):
    flask = pytest.importorskip("flask")
    monkeypatch.setenv(GROUP_ID_FORMAT_SETTING, GROUP_ID_FORMAT_SUFFIX)
    app = flask.Flask("t")
    app.config[GROUP_ID_FORMAT_SETTING] = GROUP_ID_FORMAT_PREFIX
    with app.app_context():
        assert group_id_format() == GROUP_ID_FORMAT_PREFIX
        assert group_id("x", TENANT_A) == f"{TENANT_A}_x"
    assert group_id("x", TENANT_A) == f"x_{TENANT_A}"


# --- build and split --------------------------------------------------------


def test_neurons_format_is_the_default(monkeypatch):
    """The default is what Neurons writes: `<tenant-guid>.<group-local-id>`,
    nested as-is, no separate group-to-tenant tuple."""
    monkeypatch.delenv(GROUP_ID_FORMAT_SETTING, raising=False)
    assert group_id("dashboard_designer", TENANT_A) == f"{TENANT_A}.dashboard_designer"
    assert (
        group_ref("chart_designer", TENANT_A)
        == f"group:{TENANT_A}.chart_designer#member"
    )
    assert split_group_id(f"group:{TENANT_A}.chart_designer") == (
        "chart_designer",
        TENANT_A,
    )
    assert group_belongs_to_tenant(f"{TENANT_A}.chart_designer", TENANT_A)
    assert not group_belongs_to_tenant(f"{TENANT_A}.chart_designer", TENANT_B)


def test_suffix_format_is_the_pre_configuration_shape(monkeypatch):
    """The shape the module generated before Ivanti confirmed theirs."""
    monkeypatch.setenv(GROUP_ID_FORMAT_SETTING, GROUP_ID_FORMAT_SUFFIX)
    assert group_id("dashboard_designer", TENANT_A) == f"dashboard_designer_{TENANT_A}"
    assert (
        group_object("chart_designer", TENANT_A) == f"group:chart_designer_{TENANT_A}"
    )
    assert (
        group_ref("chart_designer", TENANT_A)
        == f"group:chart_designer_{TENANT_A}#member"
    )
    # The administrators are not a group in any format: the `admin`
    # relation on the tenant object (Neurons' shape).
    assert tenant_administrator_group(TENANT_A) == f"tenant:{TENANT_A}#admin"


def test_prefix_format_is_the_sow_shape(monkeypatch):
    monkeypatch.setenv(GROUP_ID_FORMAT_SETTING, GROUP_ID_FORMAT_PREFIX)
    assert group_id("dashboard_designer", TENANT_A) == f"{TENANT_A}_dashboard_designer"
    assert (
        group_ref("chart_designer", TENANT_A)
        == f"group:{TENANT_A}_chart_designer#member"
    )
    assert tenant_administrator_group(TENANT_A) == f"tenant:{TENANT_A}#admin"


@pytest.mark.parametrize(
    "name",
    [
        "eng",
        "dashboard_designer",
        "finance_reporting_emea",
        "a_b_c_d",
        "_leading",
        "trailing_",
        "tenant_administrator",
        # A name that itself looks like a tenant id in the OTHER format must
        # still come back whole: the split anchors on the GUID at the
        # configured end, never on the first or last separator it sees.
        f"legacy_{TENANT_B}",
        f"{TENANT_B}_legacy",
        "with-dash.and+other~chars",
    ],
    ids=lambda n: n[:24],
)
def test_round_trip_in_both_formats(fmt, name):
    gid = group_id(name, TENANT_A)
    assert split_group_id(gid) == (name, TENANT_A)
    assert split_group_id(f"group:{gid}") == (name, TENANT_A)
    assert split_group_id(f"group:{gid}#member") == (name, TENANT_A)
    assert split_group_id(group_ref(name, TENANT_A)) == (name, TENANT_A)
    assert group_display_name(group_ref(name, TENANT_A)) == name


def test_round_trip_with_literal_text_around_the_pair(monkeypatch):
    monkeypatch.setenv(GROUP_ID_FORMAT_SETTING, "grp-{tenant}:{name}")
    gid = group_id("dashboard_designer", TENANT_A)
    assert gid == f"grp-{TENANT_A}:dashboard_designer"
    assert split_group_id(gid) == ("dashboard_designer", TENANT_A)
    # Without the literal it is not an id in this format.
    assert split_group_id(f"{TENANT_A}:dashboard_designer") is None


def test_tenant_case_is_normalised(fmt):
    upper = TENANT_A.upper()
    assert group_id("eng", upper) == group_id("eng", TENANT_A)
    gid = group_id("eng", TENANT_A)
    assert split_group_id(gid.replace(TENANT_A, upper)) == ("eng", TENANT_A)
    assert group_belongs_to_tenant(gid, upper)


def test_the_other_format_does_not_parse(monkeypatch):
    """An id whose only GUID sits at the un-configured end does not parse:
    that is how a mis-set instance shows up as 'group does not belong to
    your tenant' instead of quietly matching the wrong tenant.

    This is not a general guarantee. The parse anchors on a GUID at the
    configured end and takes everything else as the name, so an id with a
    GUID at BOTH ends (`<A>_dashboard_designer_<B>`) parses under both
    formats -- as tenant A in prefix and tenant B in suffix. A name may
    therefore legitimately start or end with a GUID only on the side away
    from the tenant; `_validate_subject` still requires the exact id to
    exist in the store, so such an id is never accepted on parse alone."""
    monkeypatch.setenv(GROUP_ID_FORMAT_SETTING, GROUP_ID_FORMAT_PREFIX)
    assert split_group_id(f"dashboard_designer_{TENANT_A}") is None
    assert split_group_id(f"{TENANT_A}_dashboard_designer_{TENANT_B}") == (
        f"dashboard_designer_{TENANT_B}",
        TENANT_A,
    )
    monkeypatch.setenv(GROUP_ID_FORMAT_SETTING, GROUP_ID_FORMAT_SUFFIX)
    assert split_group_id(f"{TENANT_A}_dashboard_designer") is None
    assert split_group_id(f"{TENANT_A}_dashboard_designer_{TENANT_B}") == (
        f"{TENANT_A}_dashboard_designer",
        TENANT_B,
    )


@pytest.mark.parametrize(
    "junk", [None, "", "group:", "group:#member", "eng", "group:eng", "user:" + ADA]
)
def test_split_returns_none_for_non_group_ids(fmt, junk):
    assert split_group_id(junk) is None
    assert not group_belongs_to_tenant(junk, TENANT_A)


def test_a_bare_tenant_or_name_is_not_a_group_id(fmt):
    assert split_group_id(TENANT_A) is None
    assert split_group_id(f"_{TENANT_A}") is None
    assert split_group_id(f"{TENANT_A}_") is None


def test_group_id_needs_both_parts(fmt):
    with pytest.raises(ValueError):
        group_id("", TENANT_A)
    with pytest.raises(ValueError):
        group_id("eng", "")


GUID_V1 = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
GUID_V5 = "886313e1-3b8a-5372-9b90-0c9aee199e5d"
GUID_NIL = "00000000-0000-0000-0000-000000000000"


@pytest.mark.parametrize(
    "tenant",
    [GUID_V1, GUID_V5, GUID_NIL, "local-7", "tenant", TENANT_A[:-1], f"{TENANT_A}x"],
    ids=["v1", "v5", "nil", "local-id", "word", "truncated", "extended"],
)
def test_group_id_refuses_a_tenant_it_could_not_read_back(fmt, tenant):
    """`group_id` and `split_group_id` are a round trip. The parse anchors on
    the GUID v4 shape, so a tenant of any other shape would build an id
    nothing in the module can take apart again -- and a purge of that
    tenant would delete its memberships, find none of its groups, and
    report zero for them."""
    with pytest.raises(ValueError, match="GUID v4"):
        group_id("eng", tenant)
    with pytest.raises(ValueError, match="GUID v4"):
        group_object("tenant_administrator", tenant)


@pytest.mark.parametrize(
    "name",
    ["a b", "a\tb", " leading", "trailing ", "a#b", "a:b", "group:x", "x#member"],
    ids=[
        "space",
        "tab",
        "leading-space",
        "trailing-space",
        "hash",
        "colon",
        "prefixed",
        "suffixed",
    ],
)
def test_group_id_refuses_a_name_the_store_or_the_parser_cannot_take(fmt, name):
    """OpenFGA rejects object ids containing whitespace, `:` or `#`, and the
    parser strips `group:` and `#member` from a reference, so a name carrying
    either delimiter could not be inverted (`a#b` split to None under suffix
    and to `("a", A)` under prefix)."""
    with pytest.raises(ValueError, match="whitespace"):
        group_id(name, TENANT_A)
    with pytest.raises(ValueError, match="whitespace"):
        group_ref(name, TENANT_A)


# --- belongs-to-tenant ------------------------------------------------------


def test_belongs_to_tenant_in_every_reference_form(fmt):
    for form in (group_id, group_object, group_ref):
        assert group_belongs_to_tenant(form("dashboard_designer", TENANT_A), TENANT_A)
        assert not group_belongs_to_tenant(
            form("dashboard_designer", TENANT_A), TENANT_B
        )


def test_another_tenants_group_is_rejected(fmt):
    theirs = group_ref("dashboard_designer", TENANT_B)
    assert not group_belongs_to_tenant(theirs, TENANT_A)
    # ...even when the caller's tenant appears INSIDE the name.
    sneaky = group_ref(f"x_{TENANT_A}_y", TENANT_B)
    assert split_group_id(sneaky) == (f"x_{TENANT_A}_y", TENANT_B)
    assert not group_belongs_to_tenant(sneaky, TENANT_A)


def test_no_tenant_means_no_group_is_yours(fmt):
    assert not group_belongs_to_tenant(group_ref("eng", TENANT_A), None)
    assert not group_belongs_to_tenant(group_ref("eng", TENANT_A), "")


def test_display_name_falls_back_to_the_bare_id(fmt):
    assert group_display_name("group:eng#member") == "eng"
    assert group_display_name("eng") == "eng"


# --- groups_for_tenant, local backend -----------------------------------------


@dataclass
class Role:
    name: str


@dataclass
class User:
    id: int
    username: str
    roles: list = field(default_factory=list)
    email: str | None = None


class FakeSecurityManager:
    def __init__(self, users):
        self.users = users

    def get_all_users(self):
        return list(self.users)

    def find_user(self, username=None, email=None):
        for u in self.users:
            if username is not None and u.username == username:
                return u
        return None

    def find_role(self, name):
        for u in self.users:
            for r in u.roles:
                if r.name == name:
                    return r
        return None


@pytest.fixture
def superset_stub(monkeypatch):
    stub = types.ModuleType("superset")
    stub.security_manager = None
    stub.db = mock.Mock()
    monkeypatch.setitem(sys.modules, "superset", stub)
    return stub


def test_local_groups_for_tenant_filters_and_names_by_the_format(fmt, superset_stub):
    from superset_ownership.directory import LocalDirectory

    tenant_role_a = Role(f"tenant_{TENANT_A}")
    tenant_role_b = Role(f"tenant_{TENANT_B}")
    users = [
        User(
            1,
            ADA,
            [
                tenant_role_a,
                Role(group_id("dashboard_designer", TENANT_A)),
                Role(group_id("tenant_administrator", TENANT_A)),
                Role("Gamma"),
            ],
        ),
        User(
            2,
            BEN,
            [
                tenant_role_a,
                Role(group_id("chart_designer", TENANT_A)),
                # A role carrying tenant B's id on a tenant A member is
                # not one of A's groups, whichever end the GUID is on.
                Role(group_id("dashboard_designer", TENANT_B)),
            ],
        ),
        User(3, CLEO, [tenant_role_b, Role(group_id("dashboard_designer", TENANT_B))]),
    ]
    superset_stub.security_manager = FakeSecurityManager(users)

    groups = LocalDirectory().list_groups(TENANT_A)["items"]

    assert [g["id"] for g in groups] == sorted(
        [
            group_object("chart_designer", TENANT_A),
            group_object("dashboard_designer", TENANT_A),
            group_object("tenant_administrator", TENANT_A),
        ]
    )
    by_name = {g["display_name"]: g["members"] for g in groups}
    # Display names come from split_group_id, not from stripping a suffix.
    assert by_name == {
        "chart designer": 1,
        "dashboard designer": 1,
        "tenant administrator": 1,
    }
    # The tenant role itself is never reported as a group.
    assert "tenant" not in by_name


def test_local_groups_for_tenant_ignores_ids_in_the_other_format(
    superset_stub, monkeypatch
):
    monkeypatch.setenv(GROUP_ID_FORMAT_SETTING, GROUP_ID_FORMAT_PREFIX)
    from superset_ownership.directory import LocalDirectory

    users = [
        User(
            1, ADA, [Role(f"tenant_{TENANT_A}"), Role(f"dashboard_designer_{TENANT_A}")]
        ),
    ]
    superset_stub.security_manager = FakeSecurityManager(users)
    assert LocalDirectory().list_groups(TENANT_A)["items"] == []


# --- OpenFGADirectory._walk_groups, OpenFGA backend --------------------------


def test_walk_groups_filters_and_names_by_the_format(fmt, monkeypatch):
    """N4 (review round 1, PR #101, `review-sweep-pr101.md`): this tests
    `OpenFGADirectory()._walk_groups`, not the deleted `groups_for_tenant`
    (M3, PCS-10243 #91, residue 1) -- named and patched to match. Patches
    `directory.tenant_members`, not `fga.tenant_members`: `_walk_groups`
    now calls the module-level function directly rather than bouncing
    through `fga.py`'s deprecated re-export (M3), so patching the shim
    would silently stop reaching this read."""
    from superset_ownership import directory, fga

    memberships = {
        ADA: [
            group_object("dashboard_designer", TENANT_A),
            group_object("tenant_administrator", TENANT_A),
        ],
        BEN: [
            group_object("chart_designer", TENANT_A),
            group_object("dashboard_designer", TENANT_B),
            "group:eng",
        ],
    }
    monkeypatch.setattr(
        directory, "tenant_members", lambda tenant, strict=False: [ADA, BEN]
    )
    monkeypatch.setattr(
        fga,
        "list_objects",
        lambda user, relation, object_type, strict=False: memberships[
            user.split(":", 1)[1]
        ],
    )

    groups = directory.OpenFGADirectory()._walk_groups(TENANT_A)["items"]

    assert {g["id"] for g in groups} == {
        group_object("chart_designer", TENANT_A),
        group_object("dashboard_designer", TENANT_A),
        group_object("tenant_administrator", TENANT_A),
    }
    assert {g["display_name"]: g["members"] for g in groups} == {
        "chart designer": 1,
        "dashboard designer": 1,
        "tenant administrator": 1,
    }


def test_walk_groups_answers_nothing_in_the_other_format(monkeypatch):
    """N4: see `test_walk_groups_filters_and_names_by_the_format` above."""
    from superset_ownership import directory, fga

    monkeypatch.setenv(GROUP_ID_FORMAT_SETTING, GROUP_ID_FORMAT_PREFIX)
    monkeypatch.setattr(directory, "tenant_members", lambda tenant, strict=False: [ADA])
    monkeypatch.setattr(
        fga,
        "list_objects",
        lambda *a, **k: [f"group:dashboard_designer_{TENANT_A}"],
    )
    assert directory.OpenFGADirectory()._walk_groups(TENANT_A)["items"] == []


# --- the OpenFGA authorizer resolves the administrator group the same way ---


def test_openfga_authorizer_checks_the_configured_administrator_group(fmt, monkeypatch):
    from superset_ownership import fga
    from superset_ownership.authz import OpenFGAAuthorizer

    seen = []
    monkeypatch.setattr(
        fga,
        "check",
        lambda user, relation, obj: seen.append((user, relation, obj)) or True,
    )
    assert OpenFGAAuthorizer().user_in_group(ADA, tenant_administrator_group(TENANT_A))
    # The tenant userset: one check of the `admin` relation on the tenant.
    assert seen == [(f"user:{ADA}", "admin", f"tenant:{TENANT_A}")]


def test_local_authorizer_matches_the_administrator_role_by_configured_id(
    fmt, superset_stub
):
    from superset_ownership.authz import LocalAuthorizer

    # The local backend's convention for the tenant's admin relation is the
    # fixed `tenant_administrator_<guid>` role, whatever the group format.
    superset_stub.security_manager = FakeSecurityManager(
        [User(1, ADA, [Role(f"tenant_administrator_{TENANT_A}")])]
    )
    assert LocalAuthorizer().user_in_group(ADA, tenant_administrator_group(TENANT_A))
    assert not LocalAuthorizer().user_in_group(
        ADA, tenant_administrator_group(TENANT_B)
    )


# --- backfill: administrators of a tenant ------------------------------------


def test_backfill_asks_for_the_configured_administrator_group(
    fmt, superset_stub, monkeypatch
):
    from superset_ownership import backfill

    superset_stub.security_manager = FakeSecurityManager(
        [User(11, ADA, [], None), User(12, BEN, [])]
    )
    for u in superset_stub.security_manager.users:
        u.is_active = True
    store = mock.Mock()
    store.tenant_administrators.return_value = [
        {"guid": ADA, "display_name": ADA, "email": None, "superset_id": 11},
    ]
    monkeypatch.setattr(backfill, "get_directory", lambda: store)

    assert backfill._admins_of_tenant(TENANT_A) == [11]
    store.tenant_administrators.assert_called_once_with(TENANT_A)


# --- api: the group branch of _validate_subject and is_tenant_administrator ---


@pytest.fixture
def api_module(superset_stub):
    pytest.importorskip("flask")
    pytest.importorskip("flask_jwt_extended")
    pytest.importorskip("flask_login")
    from superset_ownership import api

    superset_stub.security_manager = mock.Mock()
    superset_stub.security_manager.is_admin.return_value = False
    return api


@pytest.fixture
def caller_in_tenant_a(monkeypatch):
    monkeypatch.setattr(identity, "resolve_tenant_guid", lambda user: TENANT_A)
    monkeypatch.setattr(identity, "resolve_member_guid", lambda user: ADA)
    return object()


def test_validate_subject_accepts_own_tenants_group(
    fmt, api_module, caller_in_tenant_a, monkeypatch
):
    store = mock.Mock()
    store.group_exists.return_value = True
    monkeypatch.setattr(api_module, "get_directory", lambda: store)

    ok, msg = api_module._validate_subject(
        group_ref("dashboard_designer", TENANT_A), caller_in_tenant_a
    )
    assert (ok, msg) == (True, "")
    store.group_exists.assert_called_once_with(group_id("dashboard_designer", TENANT_A))


def test_validate_subject_rejects_another_tenants_group(
    fmt, api_module, caller_in_tenant_a, monkeypatch
):
    store = mock.Mock()
    store.group_exists.return_value = True
    monkeypatch.setattr(api_module, "get_directory", lambda: store)

    ok, msg = api_module._validate_subject(
        group_ref("dashboard_designer", TENANT_B), caller_in_tenant_a
    )
    assert (ok, msg) == (False, "group does not belong to your tenant")
    # Refused before the store is consulted.
    store.group_exists.assert_not_called()


def test_validate_subject_rejects_a_group_in_the_other_format(
    api_module, caller_in_tenant_a, monkeypatch
):
    """A suffixed id on a prefix-configured instance is 'not yours', not a
    quiet match: the mis-configuration surfaces on the first share."""
    monkeypatch.setenv(GROUP_ID_FORMAT_SETTING, GROUP_ID_FORMAT_PREFIX)
    store = mock.Mock()
    store.group_exists.return_value = True
    monkeypatch.setattr(api_module, "get_directory", lambda: store)

    ok, msg = api_module._validate_subject(
        f"group:dashboard_designer_{TENANT_A}#member", caller_in_tenant_a
    )
    assert (ok, msg) == (False, "group does not belong to your tenant")


def test_validate_subject_rejects_an_untenanted_group_for_a_tenanted_caller(
    fmt, api_module, caller_in_tenant_a, monkeypatch
):
    store = mock.Mock()
    store.group_exists.return_value = True
    monkeypatch.setattr(api_module, "get_directory", lambda: store)
    ok, msg = api_module._validate_subject("group:eng#member", caller_in_tenant_a)
    assert (ok, msg) == (False, "group does not belong to your tenant")


def test_is_tenant_administrator_checks_the_configured_group(
    fmt, api_module, caller_in_tenant_a, monkeypatch
):
    # PCS-10243 §4.5.2: with no OWNERSHIP_IS_TENANT_ADMINISTRATOR hook
    # configured, membership is read through the Directory seam
    # (get_directory().user_in_group), not the Authorizer directly.
    store = mock.Mock()
    store.user_in_group.return_value = True
    monkeypatch.setattr(api_module, "get_directory", lambda: store)
    monkeypatch.setattr(api_module, "get_hook", lambda name: None)

    assert api_module.is_tenant_administrator(caller_in_tenant_a) is True
    store.user_in_group.assert_called_once_with(
        ADA, tenant_administrator_group(TENANT_A)
    )


# --- service: the label a share row shows for a group -------------------------


def test_subject_display_name_strips_the_tenant_in_either_format(fmt, superset_stub):
    from superset_ownership import service

    superset_stub.security_manager = mock.Mock()
    assert (
        service.subject_display_name(group_ref("dashboard_designer", TENANT_A))
        == "dashboard designer"
    )
    assert (
        service.subject_display_name(group_ref("finance_reporting", TENANT_B))
        == "finance reporting"
    )
    # A group outside the convention still renders as something.
    assert service.subject_display_name("group:eng#member") == "eng"


def test_subject_display_name_resolves_a_relocated_guid_through_the_reverse_hook(
    superset_stub, monkeypatch
):
    """R2-M2: `subject_display_name` used to do `find_user(username=ref)`
    only, so a share to a member GUID a `member_guid` hook relocated off
    the username (H-1) rendered as the raw GUID on the detail payload, the
    list page and every picker label -- `OWNERSHIP_DISPLAY_NAME` was never
    even reached for it. It must now resolve the account through the same
    reverse hook `_default_normalize_subject` consults
    (`identity.user_for_member_guid`, with the forward-mapping check),
    then `identity.display_name`, falling back to the raw GUID only when
    neither resolves."""
    from superset_ownership import identity as identity_module
    from superset_ownership import service

    new_guid = "6fbe1a2c-9d34-4a1b-8c2e-7f5a3b9d1e60"
    account = mock.Mock(username="ben", first_name="Ben", last_name="Franklin")

    class RelocatedIdentity:
        protocol_version = 1

        def member_guid(self, user):
            return new_guid if user is account else None

        def tenant_guid(self, user):
            return None

        def display_name(self, user):
            return f"{user.last_name}, {user.first_name}"

        def user_for_member_guid(self, guid):
            return account if guid == new_guid else None

        def normalize_subject(self, subject):
            raise NotImplementedError

    monkeypatch.setattr(identity_module, "_get_identity", lambda: RelocatedIdentity())
    superset_stub.security_manager = mock.Mock()
    # Neither username nor email carries the relocated GUID -- the
    # username lookup misses, same as it does live (H-1's whole premise).
    superset_stub.security_manager.find_user.return_value = None

    assert service.subject_display_name(f"user:{new_guid}") == "Franklin, Ben"
    # A GUID nobody has -- neither the username lookup nor the reverse
    # hook resolves it -- still falls back to the raw subject.
    assert (
        service.subject_display_name("user:00000000-0000-4000-8000-000000000000")
        == "user:00000000-0000-4000-8000-000000000000"
    )


def test_subject_display_name_never_raises_or_leaks_when_the_reverse_lookup_raises(
    superset_stub, monkeypatch, caplog
):
    """R3-M1: on the class-seam path the reverse lookup is the plug-in's own
    code. A raise there must render the raw subject, never a 500, and the
    exception text (which can carry a bind password) must not be logged --
    only its class name is."""
    from superset_ownership import identity as identity_module
    from superset_ownership import service

    class RaisingIdentity:
        protocol_version = 1

        def member_guid(self, user):
            return None

        def tenant_guid(self, user):
            return None

        def display_name(self, user):
            return "never"

        def user_for_member_guid(self, guid):
            raise RuntimeError("ldap bind failed password=hunter2")

        def normalize_subject(self, subject):
            raise NotImplementedError

    monkeypatch.setattr(identity_module, "_get_identity", lambda: RaisingIdentity())
    superset_stub.security_manager = mock.Mock()
    superset_stub.security_manager.find_user.return_value = None

    subject = "user:6fbe1a2c-9d34-4a1b-8c2e-7f5a3b9d1e60"
    with caplog.at_level("WARNING", logger="superset_ownership.service"):
        assert service.subject_display_name(subject) == subject
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in joined
    assert "hunter2" not in joined


# --- lifecycle: the tenant purge finds the tenant's groups by the format -----


@pytest.fixture
def superset_models_stub(superset_stub, monkeypatch):
    """`superset.models.dashboard` / `.slice`, imported inside lifecycle."""
    models = types.ModuleType("superset.models")
    dashboard = types.ModuleType("superset.models.dashboard")
    dashboard.Dashboard = type("Dashboard", (), {})
    slice_ = types.ModuleType("superset.models.slice")
    slice_.Slice = type("Slice", (), {})
    monkeypatch.setitem(sys.modules, "superset.models", models)
    monkeypatch.setitem(sys.modules, "superset.models.dashboard", dashboard)
    monkeypatch.setitem(sys.modules, "superset.models.slice", slice_)
    return superset_stub


@pytest.mark.parametrize(
    "spelling", [str.lower, str.upper], ids=["lower-cased", "upper-cased"]
)
def test_purge_tenant_collects_only_groups_in_the_configured_format(
    fmt, superset_models_stub, monkeypatch, spelling
):
    """The purge walks each member's group memberships and keeps the ones
    that are the tenant's. Which ones those are depends on the format: the
    tenant's group in the configured shape is doomed (and its nested
    membership tuples with it); the same tenant's id in the OTHER shape,
    another tenant's group and an untenanted group are left alone.

    Under either spelling of the tenant: OpenFGA object ids are
    case-sensitive and the store holds the GUID lower-cased, so the purge
    has to lower-case it before naming a single tuple."""
    from superset_ownership import audit, fga, lifecycle, sentinel, service

    ours = group_object("dashboard_designer", TENANT_A)
    admins = group_object("tenant_administrator", TENANT_A)
    other_shape = (
        f"group:dashboard_designer_{TENANT_A}"
        if fmt == GROUP_ID_FORMAT_PREFIX
        else f"group:{TENANT_A}_dashboard_designer"
    )
    theirs = group_object("dashboard_designer", TENANT_B)
    memberships = {ADA: [ours, admins, other_shape, theirs, "group:eng"]}

    monkeypatch.setattr(lifecycle, "_requires_openfga", lambda operation: None)
    monkeypatch.setattr(lifecycle, "all_rows", lambda model: [])
    monkeypatch.setattr(fga, "tenant_objects", lambda tenant, asset_type: [])
    monkeypatch.setattr(
        fga,
        "list_objects",
        lambda user, relation, object_type: (
            memberships[user.split(":", 1)[1]] if object_type == "group" else []
        ),
    )
    nested = {
        ours: [{"user": f"{admins}#member", "relation": "member", "object": ours}]
    }
    asked = []  # tenant guids `_member_guids` (via fga.read_all) asked about
    read = []  # objects the group-to-group walk read (fga.read_all with no relation)

    def fake_read_all(obj, relation=None, *, strict=False):
        if relation == "member" and obj.startswith("tenant:"):
            asked.append(obj.split(":", 1)[1])
            return [{"user": f"user:{ADA}", "relation": "member", "object": obj}]
        read.append(obj)
        return nested.get(obj, [])

    monkeypatch.setattr(fga, "read_all", fake_read_all)
    deleted = []
    monkeypatch.setattr(
        fga,
        "delete_each",
        lambda tuples: (
            deleted.extend(tuples)
            or {"deleted": len(tuples), "already_absent": 0, "failed": 0}
        ),
    )
    monkeypatch.setattr(sentinel, "get_sentinel_subject", lambda: None)
    monkeypatch.setattr(audit, "emit", lambda *a, **k: None)
    monkeypatch.setattr(service, "replay_invalidations", lambda: None)

    counts = lifecycle.purge_tenant(spelling(TENANT_A))

    # Every store call and every tuple names the tenant in the store's
    # spelling, whatever the caller's.
    assert asked and set(asked) == {TENANT_A}
    assert {t["object"] for t in deleted if t["user"] == f"user:{ADA}"} == {
        f"tenant:{TENANT_A}",
        ours,
        admins,
    }
    assert nested[ours][0] in deleted
    # The group-to-group walk asked about the tenant's groups only.
    assert set(read) == {ours, admins}
    assert counts["tuples_found"] == len(deleted) == 4
    assert counts["tuples_deleted"] == 4


NOT_A_TENANT = pytest.mark.parametrize(
    "tenant",
    [GUID_V1, GUID_NIL, "local-7", "not-a-guid", TENANT_A[:-1], ""],
    ids=["v1", "nil", "local-id", "word", "truncated", "empty"],
)


@NOT_A_TENANT
def test_purge_tenant_refuses_a_non_v4_tenant_before_reading_anything(
    tenant, monkeypatch
):
    """The check is in `lifecycle.purge_tenant` itself, so it holds for
    every door -- the CLI, the REST route and a direct call -- and it comes
    before the backend check and before the store is asked anything."""
    from superset_ownership import fga, lifecycle

    def never(*a, **k):
        raise AssertionError("purge went on for a tenant the format cannot find")

    monkeypatch.setattr(lifecycle, "_requires_openfga", never)
    monkeypatch.setattr(fga, "tenant_members", never)
    with pytest.raises(ValueError, match="tenant must be a GUID v4"):
        lifecycle.purge_tenant(tenant, dry_run=True)


def test_normalize_tenant_guid_lower_cases_a_v4():
    from superset_ownership.lifecycle import normalize_tenant_guid

    assert normalize_tenant_guid(TENANT_A.upper()) == TENANT_A
    assert normalize_tenant_guid(TENANT_A) == TENANT_A


# --- api: POST /tenant/<guid>/purge gets the same check as the CLI -----------


def _post_purge(api_module, monkeypatch, tenant, caller=None, query=""):
    flask = pytest.importorskip("flask")
    monkeypatch.setattr(api_module, "_authn", lambda mutating=False: caller or object())
    path = f"/api/v1/ownership/tenant/{tenant}/purge{query}"
    with flask.Flask(__name__).test_request_context(path, method="POST"):
        response = api_module.purge_tenant_route(tenant)
    if isinstance(response, tuple):
        body, status = response
        return status, body.get_json()
    return response.status_code, response.get_json()


@NOT_A_TENANT
def test_purge_route_answers_400_for_a_non_v4_tenant(
    tenant, api_module, superset_stub, monkeypatch
):
    from superset_ownership import lifecycle

    def never(*a, **k):
        raise AssertionError("purge ran for a tenant the format cannot find")

    monkeypatch.setattr(lifecycle, "purge_tenant", never)
    superset_stub.security_manager.is_admin.return_value = True

    status, body = _post_purge(api_module, monkeypatch, tenant)

    assert status == 400
    assert "tenant must be a GUID v4" in body["message"]
    assert repr(tenant) in body["message"]


def test_purge_route_lower_cases_the_tenant_for_an_admin(
    api_module, superset_stub, monkeypatch
):
    """The store's ids are case-sensitive: an offboarding job posting the
    GUID upper-cased used to get a 200 and a purge of nothing."""
    from superset_ownership import lifecycle

    received = []
    monkeypatch.setattr(
        lifecycle,
        "purge_tenant",
        lambda tenant, dry_run=False, actor=None, force_open=False: (
            received.append(tenant) or {"members": 1}
        ),
    )
    superset_stub.security_manager.is_admin.return_value = True

    status, body = _post_purge(api_module, monkeypatch, TENANT_A.upper())

    assert status == 200
    assert received == [TENANT_A]
    assert body["tenant"] == TENANT_A
    assert body["members"] == 1


def test_purge_route_compares_a_tenant_administrators_own_tenant_case_insensitively(
    api_module, caller_in_tenant_a, monkeypatch
):
    """A tenant administrator posting their own tenant upper-cased was told
    it was not theirs (403): the comparison was lower-cased on one side."""
    from superset_ownership import lifecycle

    received = []
    monkeypatch.setattr(
        lifecycle,
        "purge_tenant",
        lambda tenant, dry_run=False, actor=None, force_open=False: (
            received.append(tenant) or {}
        ),
    )
    monkeypatch.setattr(api_module, "is_tenant_administrator", lambda user: True)

    status, body = _post_purge(
        api_module, monkeypatch, TENANT_A.upper(), caller=caller_in_tenant_a
    )
    assert status == 200
    assert received == [TENANT_A]

    # Still someone else's tenant when it is.
    status, body = _post_purge(
        api_module, monkeypatch, TENANT_B, caller=caller_in_tenant_a
    )
    assert status == 403
    assert received == [TENANT_A]


# --- cli: purge-tenant refuses a tenant the format could not find -----------


@pytest.mark.parametrize(
    "tenant",
    [GUID_V1, GUID_NIL, "local-7", "not-a-guid", TENANT_A[:-1]],
    ids=["v1", "nil", "local-id", "word", "truncated"],
)
def test_purge_tenant_cli_refuses_a_non_v4_tenant_before_anything_runs(
    tenant, monkeypatch
):
    """A usage error (exit 2) with the argument named, not a traceback --
    the click callback turns `lifecycle.purge_tenant`'s ValueError into
    click's own refusal before the command body runs."""
    pytest.importorskip("flask")
    from click.testing import CliRunner
    from superset_ownership import cli, lifecycle

    def never(*a, **k):
        raise AssertionError("purge ran for a tenant the format cannot find")

    monkeypatch.setattr(lifecycle, "purge_tenant", never)
    result = CliRunner().invoke(cli.ownership, ["purge-tenant", tenant, "--yes"])
    assert result.exit_code == 2, result.output
    assert "tenant must be a GUID v4" in result.output
    assert tenant in result.output


def test_purge_tenant_cli_accepts_a_v4_tenant_lower_cased():
    pytest.importorskip("flask")
    from superset_ownership import cli

    assert cli._tenant_guid_argument(None, None, TENANT_A.upper()) == TENANT_A


# --- lifecycle: the consistency check sees shares in the other shape ---------


def _share_mirror(tmp_path, subjects):
    """A real `ownership_share` on SQLite holding one row per subject.

    The database carries the core table revision 0003 references, so the
    chain runs as it does on a Superset database (no deferred-constraint
    warning). The share rows stand alone -- `consistency_seams` stubs the
    objects away, so object rows would read as missing -- which SQLite
    permits: it enforces the share -> object constraint only on connections
    that turn PRAGMA foreign_keys on, and this one does not."""
    import sqlalchemy as sa
    from sqlalchemy.orm import Session
    from superset_ownership import migrate
    from superset_ownership.db import ownership_share

    uri = f"sqlite:///{tmp_path / 'mirror.db'}"
    engine = sa.create_engine(uri)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE ab_user (id INTEGER PRIMARY KEY, username VARCHAR(64))"
            )
        )
    migrate.upgrade("head", uri)
    session = Session(engine)
    for i, subject in enumerate(subjects):
        session.execute(
            sa.insert(ownership_share).values(
                asset_type="chart", object_id=i, subject=subject, role="viewer"
            )
        )
    session.commit()
    return session


def test_group_id_mismatches_lists_group_shares_the_format_cannot_parse(fmt, tmp_path):
    from superset_ownership.lifecycle import group_id_mismatches, group_share_buckets

    suffix = f"group:dashboard_designer_{TENANT_A}#member"
    prefix = f"group:{TENANT_A}_dashboard_designer#member"
    session = _share_mirror(
        tmp_path,
        [
            suffix,
            prefix,
            prefix,
            group_ref("chart_designer", TENANT_B),
            "group:eng#member",
            "group:Gamma#member",
            "group:eng#member",
            f"user:{ADA}",
        ],
    )
    other = prefix if fmt == GROUP_ID_FORMAT_SUFFIX else suffix
    if fmt == GROUP_ID_FORMAT_NEURONS:
        # Under Neurons' shape both underscore spellings are foreign.
        assert group_id_mismatches(session) == sorted([prefix, suffix])
        return
    # Distinct, sorted; users are never looked at. A tenanted id in the
    # other shape is a mismatch; a group with no tenant in its id is not a
    # mismatch under either format -- it is reported in its own bucket.
    assert group_id_mismatches(session) == [other]
    assert group_share_buckets(session) == {
        "group_id_mismatch": [other],
        "group_untenanted": ["group:Gamma#member", "group:eng#member"],
    }


def test_a_guid_elsewhere_in_the_name_still_parses(fmt, tmp_path):
    """The GUID criterion for the mismatch bucket cannot misfile a valid
    id: `<A>_dashboard_designer_<B>` carries two GUIDs and parses under
    both formats, so it is in neither bucket."""
    from superset_ownership.lifecycle import group_share_buckets

    twice = (
        f"group:{TENANT_A}.dashboard_designer_{TENANT_B}#member"
        if fmt == GROUP_ID_FORMAT_NEURONS
        else f"group:{TENANT_A}_dashboard_designer_{TENANT_B}#member"
    )
    session = _share_mirror(tmp_path, [twice])
    assert group_share_buckets(session) == {
        "group_id_mismatch": [],
        "group_untenanted": [],
    }


@pytest.fixture
def consistency_seams(superset_models_stub, monkeypatch):
    """`check_consistency` with everything but the share mirror stubbed
    away; returns a function that points it at a mirror."""
    from types import SimpleNamespace

    from superset_ownership import lifecycle, outbox, sentinel

    from superset_ownership import dashboard_patch

    monkeypatch.setattr(lifecycle, "all_rows", lambda model: [])
    monkeypatch.setattr(sentinel, "get_sentinel_subject", lambda: None)
    monkeypatch.setattr(outbox, "enabled", lambda: False)
    monkeypatch.setattr(dashboard_patch, "installed", lambda: True)

    def use(session):
        superset_models_stub.db = SimpleNamespace(session=session)

    return use


def test_check_consistency_fails_on_group_shares_in_the_other_format(
    fmt, consistency_seams, tmp_path, monkeypatch
):
    """A format switch on an instance with existing group shares is
    otherwise indistinguishable from an empty tenant; the check is where
    it becomes visible, and it fails the check."""
    from superset_ownership import lifecycle

    consistency_seams(
        _share_mirror(tmp_path, [group_ref("dashboard_designer", TENANT_A)])
    )

    report = lifecycle.check_consistency()
    assert report["group_id_mismatch"] == []
    assert report["group_untenanted"] == []
    assert report["ok"] is True, {
        k: v
        for k, v in report.items()
        if k
        in (
            "flags_agree",
            "dashboard_chart_patch_installed",
            "constraints_error",
            "outbox",
            "schema_behind",
            "untenanted_public_repairable",
            "constraints_missing",
            "constraints_unvalidated",
        )
    }

    # The same mirror read under the other format.
    other = (
        GROUP_ID_FORMAT_PREFIX
        if fmt == GROUP_ID_FORMAT_SUFFIX
        else GROUP_ID_FORMAT_SUFFIX
    )
    monkeypatch.setenv(GROUP_ID_FORMAT_SETTING, other)
    report = lifecycle.check_consistency()
    assert report["group_id_mismatch"] == [
        {
            GROUP_ID_FORMAT_SUFFIX: f"group:dashboard_designer_{TENANT_A}#member",
            GROUP_ID_FORMAT_PREFIX: f"group:{TENANT_A}_dashboard_designer#member",
            GROUP_ID_FORMAT_NEURONS: f"group:{TENANT_A}.dashboard_designer#member",
        }[fmt]
    ]
    assert report["ok"] is False


def test_check_consistency_reports_untenanted_group_shares_without_failing(
    fmt, consistency_seams, tmp_path, monkeypatch
):
    """A share to `group:eng` is one the module accepted from a caller with
    no tenant (every Superset admin). Nothing about the format is wrong
    with it, it does not change when the format changes, and the store
    honours it as written -- so it is listed, and the gate stays green.
    Counting it as a mismatch turned `check` red for good, with a remedy
    ("restore the format") that did not apply."""
    from superset_ownership import lifecycle

    consistency_seams(
        _share_mirror(tmp_path, ["group:eng#member", "group:Gamma#member"])
    )

    report = lifecycle.check_consistency()
    assert report["group_untenanted"] == ["group:Gamma#member", "group:eng#member"]
    assert report["group_id_mismatch"] == []
    assert report["ok"] is True, {
        k: v
        for k, v in report.items()
        if k
        in (
            "flags_agree",
            "dashboard_chart_patch_installed",
            "constraints_error",
            "outbox",
            "schema_behind",
            "untenanted_public_repairable",
            "constraints_missing",
            "constraints_unvalidated",
        )
    }

    # Nor under the other format: untenanted is format-independent.
    other = (
        GROUP_ID_FORMAT_PREFIX
        if fmt == GROUP_ID_FORMAT_SUFFIX
        else GROUP_ID_FORMAT_SUFFIX
    )
    monkeypatch.setenv(GROUP_ID_FORMAT_SETTING, other)
    report = lifecycle.check_consistency()
    assert report["group_untenanted"] == ["group:Gamma#member", "group:eng#member"]
    assert report["group_id_mismatch"] == []
    assert report["ok"] is True, {
        k: v
        for k, v in report.items()
        if k
        in (
            "flags_agree",
            "dashboard_chart_patch_installed",
            "constraints_error",
            "outbox",
            "schema_behind",
            "untenanted_public_repairable",
            "constraints_missing",
            "constraints_unvalidated",
        )
    }


# --- lifecycle: what the boot log says -------------------------------------


def test_startup_check_logs_mismatches_at_error_with_the_format_and_the_subjects(
    fmt, consistency_seams, tmp_path, monkeypatch, caplog
):
    """The boot log is the one place an operator learns a format switch
    happened; the line names the format in force and the subjects, and
    like the other startup lines it lists at most 20 of them."""
    import logging

    from superset_ownership import lifecycle

    # 22 shares written under the OTHER format, read under this one.
    other = (
        GROUP_ID_FORMAT_PREFIX
        if fmt == GROUP_ID_FORMAT_SUFFIX
        else GROUP_ID_FORMAT_SUFFIX
    )
    monkeypatch.setenv(GROUP_ID_FORMAT_SETTING, other)
    subjects = sorted(group_ref(f"role{i:02d}", TENANT_A) for i in range(22))
    monkeypatch.setenv(GROUP_ID_FORMAT_SETTING, fmt)
    consistency_seams(_share_mirror(tmp_path, subjects))

    caplog.set_level(logging.INFO, logger="superset_ownership.lifecycle")
    report = lifecycle.startup_check()

    assert report["ok"] is False
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    message = errors[0].getMessage()
    assert "22 tenanted group subject(s)" in message
    assert f"OWNERSHIP_GROUP_ID_FORMAT={fmt!r}" in message
    assert "Restore the format they were written in" in message
    for subject in subjects[:20]:
        assert subject in message
    for subject in subjects[20:]:
        assert subject not in message
    assert not any("startup check ok" in r.getMessage() for r in caplog.records)


def test_startup_check_mentions_untenanted_shares_at_info_and_stays_ok(
    fmt, consistency_seams, tmp_path, caplog
):
    import logging

    from superset_ownership import lifecycle

    consistency_seams(_share_mirror(tmp_path, ["group:eng#member"]))

    caplog.set_level(logging.INFO, logger="superset_ownership.lifecycle")
    report = lifecycle.startup_check()

    assert report["ok"] is True, {
        k: v
        for k, v in report.items()
        if k
        in (
            "flags_agree",
            "dashboard_chart_patch_installed",
            "constraints_error",
            "outbox",
            "schema_behind",
            "untenanted_public_repairable",
            "constraints_missing",
            "constraints_unvalidated",
        )
    }
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        "1 group subject(s) in the share mirror are outside the tenant convention" in m
        and "group:eng#member" in m
        for m in messages
    )
    assert any("startup check ok" in m for m in messages)


# --- issue #119: the destructive branch needs an explicit yes ------------------


def _purge_spy(monkeypatch):
    """Records how `purge_tenant` was called, so a test can tell a preview
    from a deletion."""
    from superset_ownership import lifecycle

    calls = []

    def spy(tenant, dry_run=False, actor=None, force_open=False):
        calls.append({"tenant": tenant, "dry_run": dry_run})
        return {"members": 0}

    monkeypatch.setattr(lifecycle, "purge_tenant", spy)
    return calls


def test_purge_route_previews_when_nothing_is_confirmed(
    api_module, superset_stub, monkeypatch
):
    """The blocker: no parameter at all used to DELETE. A tenant-wide,
    irreversible operation defaults to the safe branch."""
    calls = _purge_spy(monkeypatch)
    superset_stub.security_manager.is_admin.return_value = True

    status, body = _post_purge(api_module, monkeypatch, TENANT_A)

    assert status == 200
    assert calls == [{"tenant": TENANT_A, "dry_run": True}]
    assert body["dry_run"] is True
    assert "nothing was deleted" in body["message"]


@pytest.mark.parametrize(
    "query", ["?confirm=yes", "?confirm=1", "?dry_run=0", "?dry_run=false"]
)
def test_purge_route_deletes_only_when_told_to(
    query, api_module, superset_stub, monkeypatch
):
    calls = _purge_spy(monkeypatch)
    superset_stub.security_manager.is_admin.return_value = True

    status, body = _post_purge(api_module, monkeypatch, TENANT_A, query=query)

    assert status == 200
    assert calls == [{"tenant": TENANT_A, "dry_run": False}], query
    assert body["dry_run"] is False
    assert "message" not in body


@pytest.mark.parametrize(
    "query",
    ["?confirm=please", "?confirm=", "?dry_run=maybe"],
)
def test_purge_route_refuses_a_value_it_cannot_read_rather_than_guessing(
    query, api_module, superset_stub, monkeypatch
):
    """A misspelt parameter used to mean 'not a dry run', i.e. destroy.
    Anything unrecognised is now a 400 and nothing is called."""
    calls = _purge_spy(monkeypatch)
    superset_stub.security_manager.is_admin.return_value = True

    status, body = _post_purge(api_module, monkeypatch, TENANT_A, query=query)

    assert status == 400, (query, body)
    assert "nothing was deleted" in body["message"], query
    assert calls == [], query


@pytest.mark.parametrize(
    "query",
    [
        "?confirm=no&dry_run=0",
        "?confirm=yes&dry_run=1",
        "?confirm=1&dry_run=on",
        "?confirm=off&dry_run=false",
    ],
)
def test_purge_route_refuses_two_parameters_that_disagree(
    query, api_module, superset_stub, monkeypatch
):
    """Review round 1. The first version asked "is there ANY destructive
    signal", never "do these agree": `?confirm=no&dry_run=0` purged over an
    explicit refusal, and `?confirm=yes&dry_run=1` purged although
    `dry_run=1` is documented as a preview. Both are now the same 400 an
    unreadable value gets -- the route does not get to pick which half of a
    self-contradictory request the caller meant."""
    calls = _purge_spy(monkeypatch)
    superset_stub.security_manager.is_admin.return_value = True

    status, body = _post_purge(api_module, monkeypatch, TENANT_A, query=query)

    assert status == 400, (query, body)
    assert "opposite" in body["message"], query
    assert calls == [], query


@pytest.mark.parametrize(
    "query,dry",
    [
        ("?confirm=yes&dry_run=no", False),
        ("?confirm=no&dry_run=yes", True),
        ("?confirm=0&dry_run=1", True),
        ("?confirm=true&dry_run=off", False),
    ],
)
def test_purge_route_accepts_two_parameters_that_agree(
    query, dry, api_module, superset_stub, monkeypatch
):
    """Two spellings of one answer are not a conflict."""
    calls = _purge_spy(monkeypatch)
    superset_stub.security_manager.is_admin.return_value = True

    status, body = _post_purge(api_module, monkeypatch, TENANT_A, query=query)

    assert status == 200, (query, body)
    assert calls == [{"tenant": TENANT_A, "dry_run": dry}], query


@pytest.mark.parametrize("query", ["?dryrun=1", "?DRY_RUN=0", "?confirm_purge=yes"])
def test_purge_route_treats_an_unknown_parameter_name_as_no_confirmation(
    query, api_module, superset_stub, monkeypatch
):
    """`?dryrun=1` (the misspelling a QA tester reached for) is not a
    confirmation of anything, so it previews rather than destroys."""
    calls = _purge_spy(monkeypatch)
    superset_stub.security_manager.is_admin.return_value = True

    status, body = _post_purge(api_module, monkeypatch, TENANT_A, query=query)

    assert status == 200, (query, body)
    assert calls == [{"tenant": TENANT_A, "dry_run": True}], query

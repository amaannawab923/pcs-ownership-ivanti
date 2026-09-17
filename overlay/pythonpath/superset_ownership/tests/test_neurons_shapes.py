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
"""The id shapes Ivanti confirmed for Neurons, pinned.

  member GUID      the Superset username (the token's `sub` claim)
  tenant GUID      the `Tenant_<guid>_Role` FAB role (the `tid` claim)
  user subject     user:<tenant-guid>.<member-guid>
  tenant object    tenant:<tenant-guid>
  group id         group:<tenant-guid>.<group-local-id>, nested as-is,
                   no separate group-to-tenant tuple
  administrator    the `admin` relation on the tenant object, held by a
                   user or a group#member; platform-written, never ours

Every default in `identity.py` and the model follow these; the tests here
say so in the customer's own terms, next to the module tests that exercise
the mechanics.
"""

from __future__ import annotations

import types

import pytest

from superset_ownership import identity
from superset_ownership.identity import (
    compose_subject_id,
    member_guid_of_subject_id,
    split_subject_id,
    subject_ids_match,
    tenant_administrator_group,
    tenant_of_role_name,
    tenant_role_name,
)
from superset_ownership.model import MODEL, merge_into, missing_relations, render_dsl

TENANT = "a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40"
OTHER = "b7f28e5a-9c14-4d6b-a2f0-5e3d8c1a6b92"
MEMBER = "3f0a91c7-2d84-4e63-9b15-7c4e8a2f6d31"


# --- the tenant role ---------------------------------------------------------


@pytest.mark.parametrize(
    "name, expected",
    [
        (f"Tenant_{TENANT}_Role", TENANT),
        (f"tenant_{TENANT}_role", TENANT),
        (f"TENANT_{TENANT.upper()}_ROLE", TENANT),
        (f"tenant_{TENANT}", TENANT),
        (f"tenant_administrator_{TENANT}", None),
        (f"Tenant_{TENANT}_Admin", None),
        (f"dashboard_designer_{TENANT}", None),
        ("Gamma", None),
        ("", None),
        (None, None),
    ],
)
def test_the_membership_role_in_neurons_shape_and_the_bare_one(name, expected):
    assert tenant_of_role_name(name) == expected


def test_the_role_this_module_creates_is_neurons_shape():
    assert tenant_role_name(TENANT) == f"Tenant_{TENANT}_Role"
    assert tenant_of_role_name(tenant_role_name(TENANT)) == TENANT


def test_default_identity_reads_the_tenant_off_the_neurons_role():
    user = types.SimpleNamespace(
        id=7,
        username=MEMBER,
        email="ada@example.com",
        roles=[
            types.SimpleNamespace(name="Gamma"),
            types.SimpleNamespace(name=f"Tenant_{TENANT}_Role"),
        ],
    )
    assert identity._default_tenant_guid(user) == TENANT
    # And the store id it derives carries the tenant in front.
    assert identity._default_member_guid(user) == f"{TENANT}.{MEMBER}"


# --- the user subject -----------------------------------------------------------


def test_subject_id_composes_and_splits():
    assert compose_subject_id(TENANT, MEMBER) == f"{TENANT}.{MEMBER}"
    assert compose_subject_id(None, MEMBER) == MEMBER
    assert compose_subject_id(TENANT, None) is None
    assert split_subject_id(f"{TENANT}.{MEMBER}") == (TENANT, MEMBER)
    assert split_subject_id(f"user:{TENANT}.{MEMBER}#member") == (TENANT, MEMBER)
    assert split_subject_id(MEMBER) == (None, MEMBER)
    assert split_subject_id("local-7") == (None, "local-7")
    # An e-mail's dots are not a tenant separator: only a GUID head is.
    assert split_subject_id("ada.lovelace@example.com") == (
        None,
        "ada.lovelace@example.com",
    )
    assert member_guid_of_subject_id(f"{TENANT}.{MEMBER}") == MEMBER
    assert member_guid_of_subject_id(None) is None


def test_subject_ids_match_by_member_and_by_tenant_when_both_carry_one():
    assert subject_ids_match(f"{TENANT}.{MEMBER}", MEMBER)
    assert subject_ids_match(MEMBER, f"{TENANT}.{MEMBER}")
    assert subject_ids_match(f"{TENANT}.{MEMBER}", f"{TENANT.upper()}.{MEMBER.upper()}")
    assert not subject_ids_match(f"{TENANT}.{MEMBER}", f"{OTHER}.{MEMBER}")
    assert not subject_ids_match(f"{TENANT}.{MEMBER}", f"{TENANT}.{OTHER}")
    assert not subject_ids_match(None, MEMBER)


# --- the group id -----------------------------------------------------------------


def test_default_group_id_is_tenant_dot_local_id(monkeypatch):
    monkeypatch.delenv(identity.GROUP_ID_FORMAT_SETTING, raising=False)
    assert (
        identity.group_id("dashboard_designer", TENANT)
        == f"{TENANT}.dashboard_designer"
    )
    assert identity.group_ref("dashboard_designer", TENANT) == (
        f"group:{TENANT}.dashboard_designer#member"
    )
    assert identity.split_group_id(f"group:{TENANT}.dashboard_designer") == (
        "dashboard_designer",
        TENANT,
    )
    # A local id with a dot of its own still parses: the tenant is anchored
    # on the GUID shape and the first separator.
    assert identity.split_group_id(f"group:{TENANT}.ops.eu") == ("ops.eu", TENANT)
    assert identity.group_belongs_to_tenant(f"{TENANT}.dashboard_designer", TENANT)
    assert not identity.group_belongs_to_tenant(f"{TENANT}.dashboard_designer", OTHER)


# --- the administrator ----------------------------------------------------------


def test_the_administrator_is_the_admin_relation_on_the_tenant_object():
    assert tenant_administrator_group(TENANT) == f"tenant:{TENANT}#admin"
    assert identity.split_userset(f"tenant:{TENANT}#admin") == (
        f"tenant:{TENANT}",
        "admin",
    )
    assert identity.split_userset(f"group:{TENANT}.ops") == (
        f"group:{TENANT}.ops",
        "member",
    )


def test_the_model_declares_admin_on_tenant_and_never_more_than_it_reads():
    tenant = next(t for t in MODEL["type_definitions"] if t["type"] == "tenant")
    assert set(tenant["relations"]) == {"member", "admin"}
    assert tenant["metadata"]["relations"]["admin"]["directly_related_user_types"] == [
        {"type": "user"},
        {"type": "group", "relation": "member"},
    ]
    assert "define admin: [user, group#member]" in render_dsl(MODEL)


# --- installing into a published model ------------------------------------------


def _published() -> dict:
    """A model shaped like the customer's: their own types and relations,
    the three we reference present, `admin` already on the tenant, and
    nothing of ours."""
    return {
        "schema_version": "1.1",
        "type_definitions": [
            {"type": "user"},
            {
                "type": "tenant",
                "relations": {
                    "member": {"this": {}},
                    "admin": {"this": {}},
                    "billing": {"this": {}},
                },
                "metadata": {
                    "relations": {
                        "member": {"directly_related_user_types": [{"type": "user"}]},
                        "admin": {
                            "directly_related_user_types": [
                                {"type": "user"},
                                {"type": "group", "relation": "member"},
                            ]
                        },
                        "billing": {"directly_related_user_types": [{"type": "user"}]},
                    }
                },
            },
            {
                "type": "group",
                "relations": {"member": {"this": {}}},
                "metadata": {
                    "relations": {
                        "member": {
                            "directly_related_user_types": [
                                {"type": "user"},
                                {"type": "group", "relation": "member"},
                            ]
                        }
                    }
                },
            },
            {"type": "device", "relations": {"viewer": {"this": {}}}},
        ],
    }


def test_merge_adds_our_types_and_touches_nothing_of_theirs():
    merged = merge_into(_published())
    types_ = [t["type"] for t in merged["type_definitions"]]
    assert types_ == ["user", "tenant", "group", "device", "dashboard", "chart"]
    tenant = next(t for t in merged["type_definitions"] if t["type"] == "tenant")
    # Their relations, their definitions: untouched.
    assert set(tenant["relations"]) == {"member", "admin", "billing"}
    device = next(t for t in merged["type_definitions"] if t["type"] == "device")
    assert device == _published()["type_definitions"][3]
    assert missing_relations(merged) == []
    # Ours arrive whole.
    dashboard = next(t for t in merged["type_definitions"] if t["type"] == "dashboard")
    assert set(dashboard["relations"]) == {"owner", "editor", "viewer", "tenant"}


def test_merge_adds_only_a_missing_relation_to_a_referenced_type():
    published = _published()
    tenant = published["type_definitions"][1]
    del tenant["relations"]["admin"]
    del tenant["metadata"]["relations"]["admin"]
    assert "tenant#admin" in missing_relations(published)
    merged = merge_into(published)
    tenant = next(t for t in merged["type_definitions"] if t["type"] == "tenant")
    assert set(tenant["relations"]) == {"member", "billing", "admin"}
    assert tenant["relations"]["member"] == {"this": {}}
    assert missing_relations(merged) == []


def test_merge_of_an_empty_model_is_our_model():
    merged = merge_into({"schema_version": "1.1", "type_definitions": []})
    assert [t["type"] for t in merged["type_definitions"]] == [
        t["type"] for t in MODEL["type_definitions"]
    ]
    assert missing_relations(merged) == []


def test_merge_is_idempotent():
    once = merge_into(_published())
    assert merge_into(once) == once


# --- review round 1 of PR #113 -----------------------------------------------------


def test_merge_carries_a_published_conditions_block_through():
    """A schema-1.1 model with ABAC `conditions`: the block survives the
    merge (the userset refs inside `metadata.relations` name it, so a
    model without it is one OpenFGA refuses to write)."""
    published = _published()
    published["conditions"] = {
        "in_office_hours": {
            "name": "in_office_hours",
            "expression": "hour >= 9 && hour < 18",
            "parameters": {"hour": {"type_name": "TYPE_NAME_INT"}},
        }
    }
    published["id"] = "01HXYZ"
    merged = merge_into(published)
    assert merged["conditions"] == published["conditions"]
    assert "id" not in merged, "the store's own model id is never re-sent"
    assert merge_into(merged) == merged


def test_show_model_check_requires_tenant_admin():
    """The code reads `tenant#admin` on every non-owner request, so the
    model check that guards the upgrade ordering must list it."""
    from superset_ownership.model import REQUIRED

    assert "admin" in REQUIRED["tenant"]


def test_email_fallback_reads_the_member_guid_not_the_tenants():
    """Neurons rewrites the email to `<tenant>_<member>__<email>`; the
    FIRST GUID in it is the tenant's. An account whose username is not the
    GUID must still get `<tenant>.<member>`, never `<tenant>.<tenant>`."""
    user = types.SimpleNamespace(
        id=9,
        username="ada.lovelace",
        email=f"{TENANT}_{MEMBER}__ada@example.com",
        roles=[types.SimpleNamespace(name=f"Tenant_{TENANT}_Role")],
    )
    assert identity._default_member_guid(user) == f"{TENANT}.{MEMBER}"
    # A single-GUID email (a local seed) still works as before.
    user.email = f"{MEMBER}@example.com"
    assert identity._default_member_guid(user) == f"{TENANT}.{MEMBER}"
    assert identity._member_guid_of_email(None) is None
    assert identity._member_guid_of_email("plain@example.com") is None


def test_the_old_tenant_role_helper_is_gone():
    assert not hasattr(identity, "tenant_role"), "tenant_role_name is the one helper"

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
"""The `Identity` protocol (spec §6, `01-plugin-contract.md` §4.2).

Three things this file has to prove:

1. `DefaultIdentity` reproduces the pre-protocol module functions exactly,
   over the same shapes `test_group_id.py`/`test_api.py` already exercise.
2. The module-level facades (`resolve_member_guid`, `resolve_tenant_guid`,
   `display_name`, `normalize_subject`, `member_ref`) delegate to whichever
   `Identity` is loaded, so every existing call site -- which imports and
   calls the facade, never a concrete class -- picks up an override without
   an edit.
3. An override is honoured through `get_identity()`, including for the one
   method (`normalize_subject`) it is not expected to reimplement.

Pure: no Superset app. `superset` is stubbed in `sys.modules` for the two
functions (`member_ref`, `normalize_subject`) that query it, the same
pattern `test_api.py`/`test_group_id.py` use.

`superset_ownership.plugins` (spec §3/§4) is not on this branch; `identity.py`
imports it lazily and falls back to a module-level `DefaultIdentity()`
singleton so this suite -- and the package -- is green alone. Tests below
that need a *loaded* plug-in fake that import path in `sys.modules` rather
than assume the module exists.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from typing import Any
from unittest import mock

import pytest
from superset_ownership import identity
from superset_ownership.identity import DefaultIdentity, Identity

TENANT_A = "a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40"
TENANT_B = "b7f28e5a-9c14-4d6b-a2f0-5e3d8c1a6b92"
ADA = "3f0a91c7-2d84-4e63-9b15-7c4e8a2f6d31"
BEN = "6c2b48e9-5a71-4f92-8d03-2e9b7c1a4d53"


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
    is_active: bool = True
    extra: dict = field(default_factory=dict)


@pytest.fixture
def superset_stub(monkeypatch):
    """A `superset` module carrying a mock `security_manager`, for the two
    functions (`member_ref`, `normalize_subject`) that import it."""
    stub = types.ModuleType("superset")
    stub.security_manager = mock.Mock()
    monkeypatch.setitem(sys.modules, "superset", stub)
    return stub.security_manager


# --- DefaultIdentity.member_guid: parity with the old resolve_member_guid --


class TestMemberGuidParity:
    def test_guid_username_wins(self):
        u = User(1, username=ADA)
        assert DefaultIdentity().member_guid(u) == ADA

    def test_username_guid_is_lower_cased(self):
        u = User(1, username=ADA.upper())
        assert DefaultIdentity().member_guid(u) == ADA

    def test_falls_back_to_the_email_when_the_username_has_no_guid(self):
        u = User(1, username="ada", email=f"ada+{ADA}@example.com")
        assert DefaultIdentity().member_guid(u) == ADA

    def test_falls_back_to_a_local_id_with_neither(self):
        u = User(7, username="ada", email="ada@example.com")
        assert DefaultIdentity().member_guid(u) == "local-7"

    def test_none_for_no_user(self):
        assert DefaultIdentity().member_guid(None) is None

    def test_ignores_whether_the_account_is_deactivated(self):
        active = User(1, username=ADA, is_active=True)
        deactivated = User(1, username=ADA, is_active=False)
        assert (
            DefaultIdentity().member_guid(active)
            == DefaultIdentity().member_guid(deactivated)
            == ADA
        )


# --- DefaultIdentity.tenant_guid: parity with the old resolve_tenant_guid --


class TestTenantGuidParity:
    def test_present(self):
        u = User(1, username=ADA, roles=[Role(f"tenant_{TENANT_A}")])
        assert DefaultIdentity().tenant_guid(u) == TENANT_A

    def test_absent(self):
        u = User(1, username=ADA, roles=[Role("Gamma")])
        assert DefaultIdentity().tenant_guid(u) is None

    def test_no_roles_at_all(self):
        assert DefaultIdentity().tenant_guid(User(1, username=ADA)) is None

    def test_multiple_tenant_roles_the_first_in_the_list_wins(self):
        u = User(
            1,
            username=ADA,
            roles=[Role(f"tenant_{TENANT_A}"), Role(f"tenant_{TENANT_B}")],
        )
        assert DefaultIdentity().tenant_guid(u) == TENANT_A

    def test_the_administrator_group_role_is_not_mistaken_for_membership(self):
        # "tenant_administrator_<guid>" shares the "tenant_" prefix with the
        # membership role but is not an exact match for it.
        u = User(
            1,
            username=ADA,
            roles=[
                Role(f"tenant_administrator_{TENANT_A}"),
                Role(f"tenant_{TENANT_B}"),
            ],
        )
        assert DefaultIdentity().tenant_guid(u) == TENANT_B

    def test_none_for_no_user(self):
        assert DefaultIdentity().tenant_guid(None) is None

    def test_ignores_whether_the_account_is_deactivated(self):
        roles = [Role(f"tenant_{TENANT_A}")]
        active = User(1, username=ADA, roles=roles, is_active=True)
        deactivated = User(1, username=ADA, roles=roles, is_active=False)
        assert (
            DefaultIdentity().tenant_guid(active)
            == DefaultIdentity().tenant_guid(deactivated)
            == TENANT_A
        )


# --- DefaultIdentity.display_name -------------------------------------------


class TestDisplayNameParity:
    def test_full_name(self):
        u = User(1, username="ada", first_name="Ada", last_name="Lovelace")
        assert DefaultIdentity().display_name(u) == "Ada Lovelace"

    def test_first_name_only(self):
        u = User(1, username="ada", first_name="Ada")
        assert DefaultIdentity().display_name(u) == "Ada"

    def test_falls_back_to_the_username(self):
        u = User(1, username="ada")
        assert DefaultIdentity().display_name(u) == "ada"

    def test_pinned_equivalence_with_service_pys_former_or_ref(self):
        # service.py:1375 used to read `... or ref`, where `ref` is the
        # string that found the user (`security_manager.find_user(
        # username=ref)`) -- i.e. `ref == user.username` on that call path.
        # `DefaultIdentity.display_name` reads `... or user.username`; the
        # spec review (round 1, nit) flagged the two as equivalent only
        # because of that path, not in general -- pinned here rather than
        # left implicit.
        u = User(1, username="ada")
        ref = "ada"
        assert DefaultIdentity().display_name(u) == (
            f"{u.first_name} {u.last_name}".strip() or ref
        )


# --- DefaultIdentity.normalize_subject: parity with the old normalize_subject


class TestNormalizeSubjectParity:
    def test_group_passes_through(self, superset_stub):
        assert (
            DefaultIdentity().normalize_subject("group:eng#member")
            == "group:eng#member"
        )

    def test_a_non_user_non_group_kind_is_nobody(self, superset_stub):
        assert DefaultIdentity().normalize_subject("tenant:x") is None

    def test_no_colon_is_nobody(self, superset_stub):
        assert DefaultIdentity().normalize_subject("ada") is None

    def test_empty_is_nobody(self, superset_stub):
        assert DefaultIdentity().normalize_subject("") is None

    def test_digit_ref_looks_up_by_id(self, superset_stub):
        u = User(7, username=ADA)
        superset_stub.get_user_by_id.return_value = u
        assert DefaultIdentity().normalize_subject("user:7") == f"user:{ADA}"
        superset_stub.get_user_by_id.assert_called_once_with(7)

    def test_username_ref_looks_up_by_username(self, superset_stub):
        u = User(1, username=ADA)
        superset_stub.find_user.side_effect = lambda username=None, email=None: (
            u if username == ADA else None
        )
        assert DefaultIdentity().normalize_subject(f"user:{ADA}") == f"user:{ADA}"

    def test_falls_back_to_an_email_lookup(self, superset_stub):
        u = User(1, username="local-only", email="ada@example.com")

        def find_user(username: Any = None, email: Any = None):
            if email == "ada@example.com":
                return u
            return None

        superset_stub.find_user.side_effect = find_user
        assert (
            DefaultIdentity().normalize_subject("user:ada@example.com")
            == "user:local-1"
        )

    def test_unknown_user_is_nobody(self, superset_stub):
        superset_stub.find_user.return_value = None
        assert DefaultIdentity().normalize_subject("user:ghost") is None


# --- member_ref: the int-lookup half stays module logic; the GUID half -----
# routes through the active Identity.


class TestMemberRefParity:
    def test_builds_a_ref_from_a_user_object(self, superset_stub):
        u = User(1, username=ADA)
        assert identity.member_ref(u) == f"user:{ADA}"

    def test_looks_up_an_int_id_then_builds_the_ref(self, superset_stub):
        u = User(7, username=ADA)
        superset_stub.get_user_by_id.return_value = u
        assert identity.member_ref(7) == f"user:{ADA}"
        superset_stub.get_user_by_id.assert_called_once_with(7)

    def test_none_when_there_is_no_user(self, superset_stub):
        assert identity.member_ref(None) is None


# --- facades: every module function delegates to get_identity() ------------


class _FakeIdentity:
    protocol_version = 1

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def member_guid(self, user: Any) -> str | None:
        self.calls.append(("member_guid", user))
        return "fake-member"

    def tenant_guid(self, user: Any) -> str | None:
        self.calls.append(("tenant_guid", user))
        return "fake-tenant"

    def display_name(self, user: Any) -> str:
        self.calls.append(("display_name", user))
        return "Fake Name"

    def normalize_subject(self, raw: str) -> str | None:
        self.calls.append(("normalize_subject", raw))
        return "user:fake"


class TestFacadesDelegate:
    @pytest.fixture
    def fake(self, monkeypatch):
        fake_identity = _FakeIdentity()
        monkeypatch.setattr(identity, "_get_identity", lambda: fake_identity)
        return fake_identity

    def test_resolve_member_guid(self, fake):
        u = User(1, username="x")
        assert identity.resolve_member_guid(u) == "fake-member"
        assert fake.calls == [("member_guid", u)]

    def test_resolve_tenant_guid(self, fake):
        u = User(1, username="x")
        assert identity.resolve_tenant_guid(u) == "fake-tenant"
        assert fake.calls == [("tenant_guid", u)]

    def test_display_name(self, fake):
        u = User(1, username="x")
        assert identity.display_name(u) == "Fake Name"
        assert fake.calls == [("display_name", u)]

    def test_normalize_subject(self, fake):
        assert identity.normalize_subject("user:7") == "user:fake"
        assert fake.calls == [("normalize_subject", "user:7")]

    def test_member_ref_delegates_only_the_guid_half(self, fake, superset_stub):
        u = User(1, username="x")
        assert identity.member_ref(u) == "user:fake-member"
        assert fake.calls == [("member_guid", u)]

    def test_member_ref_looks_up_an_int_then_delegates(self, fake, superset_stub):
        u = User(1, username="x")
        superset_stub.get_user_by_id.return_value = u
        assert identity.member_ref(1) == "user:fake-member"
        superset_stub.get_user_by_id.assert_called_once_with(1)
        assert fake.calls == [("member_guid", u)]

    def test_current_member_guid_reads_flask_g(self, fake, monkeypatch):
        flask = pytest.importorskip("flask")
        user = User(1, username="x")
        monkeypatch.setattr(flask, "g", types.SimpleNamespace(user=user))
        assert identity.current_member_guid() == "fake-member"
        assert fake.calls == [("member_guid", user)]

    def test_current_tenant_guid_reads_flask_g(self, fake, monkeypatch):
        flask = pytest.importorskip("flask")
        user = User(1, username="x")
        monkeypatch.setattr(flask, "g", types.SimpleNamespace(user=user))
        assert identity.current_tenant_guid() == "fake-tenant"
        assert fake.calls == [("tenant_guid", user)]


# --- get_identity(): lazy import of superset_ownership.plugins, with a -----
# fallback to a module-level DefaultIdentity() singleton.


class TestGetIdentityLazyImport:
    def test_falls_back_to_a_default_identity_singleton_when_plugins_is_absent(
        self, monkeypatch
    ):
        # Forces the import to fail regardless of whether
        # superset_ownership.plugins actually exists on disk (it does not
        # on this branch; spec §3/§4 owns it).
        monkeypatch.setitem(sys.modules, "superset_ownership.plugins", None)
        got = identity._get_identity()
        assert isinstance(got, DefaultIdentity)
        assert got is identity._default_identity

    def test_delegates_to_plugins_get_identity_when_it_is_importable(self, monkeypatch):
        fake_module = types.ModuleType("superset_ownership.plugins")
        sentinel = _FakeIdentity()
        fake_module.get_identity = lambda: sentinel  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "superset_ownership.plugins", fake_module)
        assert identity._get_identity() is sentinel

    def test_default_identity_facades_use_the_singleton_absent_plugins(
        self, monkeypatch
    ):
        monkeypatch.setitem(sys.modules, "superset_ownership.plugins", None)
        u = User(1, username=ADA)
        assert identity.resolve_member_guid(u) == ADA


# --- an override is honoured through get_identity() -------------------------


class AttributeIdentity(DefaultIdentity):
    """The module docstring's worked example: the GUID comes off a user
    attribute when present, falling back to DefaultIdentity's body -- the
    "only relocates the GUID" override the spec names."""

    def member_guid(self, user: Any) -> str | None:
        guid = (getattr(user, "extra", None) or {}).get("member_guid")
        return guid or super().member_guid(user)


class TestOverrideIsHonouredThroughGetIdentity:
    def test_member_guid_reads_the_attribute(self, monkeypatch):
        override = AttributeIdentity()
        monkeypatch.setattr(identity, "_get_identity", lambda: override)
        u = User(1, username="ben", extra={"member_guid": BEN})
        assert identity.resolve_member_guid(u) == BEN

    def test_falls_back_to_the_inherited_default_body_without_the_attribute(
        self, monkeypatch
    ):
        override = AttributeIdentity()
        monkeypatch.setattr(identity, "_get_identity", lambda: override)
        u = User(1, username=ADA)
        assert identity.resolve_member_guid(u) == ADA

    def test_normalize_subject_is_inherited_and_still_honours_the_override(
        self, monkeypatch, superset_stub
    ):
        # AttributeIdentity implements only member_guid. normalize_subject
        # is inherited from DefaultIdentity, whose body calls back out
        # through member_ref -> resolve_member_guid -> get_identity()
        # .member_guid -- so the override answers what the account's own
        # canonical reference is, without AttributeIdentity reimplementing
        # the Superset lookups.
        #
        # R2-M1: this used to assert the opposite of what it does now --
        # that a share keyed by the Superset USERNAME still resolves even
        # once the override's `member_guid` answers a different value for
        # this same account. That was the bug: the forward-mapping check
        # already applied to the reverse hook's candidate (H-3/N-2) now
        # applies to the username/email candidate too, so a stale,
        # pre-relocation username is refused rather than silently
        # resolved. AttributeIdentity has no reverse hook of its own, so
        # this account is unaddressable by either representation here
        # (`test_reverse_mapping_missing_on_a_bare_override_fails_closed`
        # pins the other half, the GUID leg).
        override = AttributeIdentity()
        monkeypatch.setattr(identity, "_get_identity", lambda: override)
        u = User(1, username="ben", extra={"member_guid": BEN})
        superset_stub.find_user.side_effect = lambda username=None, email=None: (
            u if username == "ben" else None
        )
        assert identity.normalize_subject("user:ben") is None

    def test_display_name_is_inherited_unchanged(self, monkeypatch):
        override = AttributeIdentity()
        monkeypatch.setattr(identity, "_get_identity", lambda: override)
        u = User(1, username="ben", first_name="Ben", last_name="Franklin")
        assert identity.display_name(u) == "Ben Franklin"


# --- H-3: the reverse mapping (user_for_member_guid) round-trips -----------


class RelocatedGuidIdentity(DefaultIdentity):
    """The shape H-3's fix asks the shipped example for: an override that
    relocates the GUID off username/email implements `user_for_member_guid`
    too, so `normalize_subject` can resolve the very value the picker
    itself emits (`directory.py`'s `_user_labels` keys rows by
    `member_guid`)."""

    def member_guid(self, user: Any) -> str | None:
        return (getattr(user, "extra", None) or {}).get("member_guid")

    def user_for_member_guid(self, guid: str) -> Any:
        from superset import security_manager

        for u in security_manager.get_all_users():
            if (getattr(u, "extra", None) or {}).get("member_guid") == guid:
                return u
        return None


class TestReverseMappingRoundTrips:
    def test_normalize_subject_resolves_a_relocated_guid(
        self, monkeypatch, superset_stub
    ):
        # H-3's exact probe: the picker's own value (`user:<attribute GUID>`)
        # must validate even though neither username nor email carries it.
        override = RelocatedGuidIdentity()
        monkeypatch.setattr(identity, "_get_identity", lambda: override)
        u = User(1, username="ben", extra={"member_guid": BEN})
        superset_stub.find_user.return_value = None  # username/email never carry it
        superset_stub.get_all_users.return_value = [u]
        assert identity.normalize_subject(f"user:{BEN}") == f"user:{BEN}"

    def test_normalize_subject_no_longer_resolves_by_the_stale_username(
        self, monkeypatch, superset_stub
    ):
        # R2-M1: this used to assert the opposite -- that DefaultIdentity's
        # username lookup runs first and wins even once the override's
        # `member_guid` has relocated the account's canonical reference
        # off that same username (round-trip #2 in the original review
        # probe). That was the bug rule 3 (hook/override precedence) did
        # not honour in the negative direction: the forward-mapping check
        # already applied to the reverse hook's candidate now applies to
        # the username/email candidate too, so this stale, pre-relocation
        # identifier is refused rather than silently accepted.
        override = RelocatedGuidIdentity()
        monkeypatch.setattr(identity, "_get_identity", lambda: override)
        u = User(1, username="ben", extra={"member_guid": BEN})
        superset_stub.find_user.side_effect = lambda username=None, email=None: (
            u if username == "ben" else None
        )
        superset_stub.get_all_users.return_value = [u]
        assert identity.normalize_subject("user:ben") is None

    def test_reverse_mapping_missing_on_a_bare_override_fails_closed(
        self, monkeypatch, superset_stub
    ):
        # AttributeIdentity from the module docstring's FIRST example
        # (member_guid only, no reverse hook): the picker's own value
        # cannot be resolved -- the exact H-3 failure, still reproducible
        # on an override that has not been updated.
        override = AttributeIdentity()
        monkeypatch.setattr(identity, "_get_identity", lambda: override)
        superset_stub.find_user.return_value = None
        assert identity.normalize_subject(f"user:{BEN}") is None

    def test_default_identity_user_for_member_guid_by_username(self, superset_stub):
        u = User(1, username=ADA)
        superset_stub.find_user.side_effect = lambda username=None, email=None: (
            u if username == ADA else None
        )
        assert DefaultIdentity().user_for_member_guid(ADA) is u

    def test_default_identity_user_for_member_guid_falls_back_to_email(
        self, superset_stub
    ):
        u = User(1, username="local-only", email="ada@example.com")
        superset_stub.find_user.side_effect = lambda username=None, email=None: (
            u if email == "ada@example.com" else None
        )
        assert DefaultIdentity().user_for_member_guid("ada@example.com") is u

    def test_default_identity_user_for_member_guid_unknown_is_none(self, superset_stub):
        superset_stub.find_user.return_value = None
        assert DefaultIdentity().user_for_member_guid("ghost") is None


class LyingReverseHookIdentity(DefaultIdentity):
    """An override whose reverse hook answers *a* user for ANY GUID,
    never checking that it is *the* account the GUID names -- N-2's exact
    real-world shape: a `.first()` over a stale or duplicated attribute
    query. `member_guid` (inherited, unchanged) is the account's own,
    real forward mapping."""

    def __init__(self, liar_target: Any) -> None:
        self._liar_target = liar_target

    def user_for_member_guid(self, guid: str) -> Any:
        return self._liar_target


class TestReverseHookForwardMappingGuard:
    def test_normalize_subject_rejects_an_inconsistent_reverse_hook(
        self, monkeypatch, superset_stub
    ):
        """N-2: `_default_normalize_subject`'s reverse-hook branch must not
        trust `user_for_member_guid`'s answer unless that account's OWN
        `member_guid` agrees with the GUID being resolved -- otherwise a
        subject naming nobody the hook actually knows resolves to whoever
        the hook happens to return."""
        liar_target = User(1, username=ADA)  # a real account; its own GUID is ADA
        override = LyingReverseHookIdentity(liar_target)
        monkeypatch.setattr(identity, "_get_identity", lambda: override)
        superset_stub.find_user.return_value = None  # username/email never carry BEN

        # The hook answers `liar_target` (forward GUID: ADA) for BEN's
        # subject too -- an inconsistent answer that must be rejected.
        assert identity.normalize_subject(f"user:{BEN}") is None

    def test_normalize_subject_accepts_a_consistent_reverse_hook(
        self, monkeypatch, superset_stub
    ):
        # The positive control: the SAME override, asked for the GUID that
        # actually IS the account's forward mapping.
        liar_target = User(1, username=ADA)
        override = LyingReverseHookIdentity(liar_target)
        monkeypatch.setattr(identity, "_get_identity", lambda: override)
        superset_stub.find_user.return_value = None

        assert identity.normalize_subject(f"user:{ADA}") == f"user:{ADA}"


NEW_GUID = "10000000-0000-4000-8000-000000000001"


class TestForwardMappingGuardsTheUsernameCandidateToo:
    """R2-M1 (`qa/reviews/review-hooks-pr92.md`, round 2): Superset's own
    `find_user(username=...)` does not know a `member_guid` override has
    relocated the GUID off the username -- the account still HAS that
    username, so the lookup still finds it. Before this fix, that candidate
    was trusted unconditionally, so a subject the configured identity does
    not consider valid (an account's stale, pre-relocation username) was
    admitted through the default lookup instead of being refused, same as
    the reverse hook's candidate already is (H-3/N-2)."""

    def test_normalize_subject_rejects_the_stale_username_once_the_guid_moved(
        self, monkeypatch, superset_stub
    ):
        u = User(1, username="ben", extra={"member_guid": NEW_GUID})
        override = RelocatedGuidIdentity()
        monkeypatch.setattr(identity, "_get_identity", lambda: override)
        # Superset still finds the account by its (now stale) username --
        # the relocation lives only in the override's `member_guid`.
        superset_stub.find_user.side_effect = lambda username=None, email=None: (
            u if username == "ben" else None
        )
        superset_stub.get_all_users.return_value = [u]
        assert identity.normalize_subject("user:ben") is None

    def test_normalize_subject_still_resolves_the_relocated_guid(
        self, monkeypatch, superset_stub
    ):
        u = User(1, username="ben", extra={"member_guid": NEW_GUID})
        override = RelocatedGuidIdentity()
        monkeypatch.setattr(identity, "_get_identity", lambda: override)
        superset_stub.find_user.side_effect = lambda username=None, email=None: (
            u if username == "ben" else None
        )
        superset_stub.get_all_users.return_value = [u]
        assert identity.normalize_subject(f"user:{NEW_GUID}") == f"user:{NEW_GUID}"

    def test_the_plain_default_identity_is_unaffected(self, monkeypatch, superset_stub):
        # Under DefaultIdentity itself (no override at all) the username IS
        # the member_guid, so the forward-mapping check on the username
        # candidate is a no-op there and is skipped rather than applied --
        # pinning that this fix changes nothing on the pure default path.
        monkeypatch.setattr(identity, "_get_identity", lambda: DefaultIdentity())
        u = User(1, username=ADA)
        superset_stub.find_user.side_effect = lambda username=None, email=None: (
            u if username == ADA else None
        )
        assert identity.normalize_subject(f"user:{ADA}") == f"user:{ADA}"


class TestUserForMemberGuidFacadeDelegates:
    def test_delegates_to_get_identity(self, monkeypatch):
        fake_identity = _FakeIdentity()
        fake_identity.user_for_member_guid = lambda guid: ("looked-up", guid)  # type: ignore[attr-defined]
        monkeypatch.setattr(identity, "_get_identity", lambda: fake_identity)
        assert identity.user_for_member_guid(BEN) == ("looked-up", BEN)


# --- protocol conformance ----------------------------------------------------


class TestProtocolConformance:
    def test_default_identity_satisfies_the_protocol(self):
        assert isinstance(DefaultIdentity(), Identity)

    def test_protocol_version_is_1(self):
        assert DefaultIdentity.protocol_version == 1
        assert DefaultIdentity().protocol_version == 1

    def test_the_attribute_override_still_satisfies_the_protocol(self):
        assert isinstance(AttributeIdentity(), Identity)

    def test_a_class_missing_a_method_does_not_satisfy_the_protocol(self):
        class Incomplete:
            protocol_version = 1

            def member_guid(self, user):
                return None

            def tenant_guid(self, user):
                return None

            def display_name(self, user):
                return ""

            # normalize_subject deliberately omitted.

        assert not isinstance(Incomplete(), Identity)

    def test_identities_alias_table_has_the_default(self):
        assert identity._IDENTITIES["default"] is DefaultIdentity

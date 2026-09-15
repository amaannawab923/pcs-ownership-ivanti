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
"""Unit tests for `plugin_verify.py` (spec §10, the 24-check conformance kit
as the task was scoped -- the spec's own §10 table enumerates 22 distinct
checks, and that is what is implemented and counted here; see the PR
summary. The 24th, `vocabulary/manage_permission_template`, is not in the
spec's table -- it was added for issue #83's `{tenant}` placeholder in
`OWNERSHIP_MANAGE_PERMISSION`.

Mostly pure: no Flask app, no Superset, no Docker, no network. Every check
is driven against a `Context` built from small fakes/stubs, exactly the
seam the module docstring describes -- this file's own code never imports
`plugins`, `directory`, `fga`, `model`, `identity` or `authz` directly.
Three groups of tests need more than that, and each says so in its own
docstring: the ones carrying the `harness` fixture (conftest.py) drive a
real `_build_context`/`_make_invariant_probe` off a loaded Superset
application (the `superset` marker is added to these automatically by
conftest.py's `pytest_collection_modifyitems`, so the pure job skips them);
`test_seed_scratch_writes_the_clean_fixture_into_a_real_store` additionally
needs `OWNERSHIP_TEST_FGA_API_URL` set to a reachable OpenFGA and skips
itself otherwise -- opt-in only, never the ambient `OWNERSHIP_FGA_API_URL`
(PR90 review N-8); `test_empty_plugin_block_snapshot` needs neither -- it
drives
`plugins.load` against a bare stand-in object, on purpose, so its baseline
does not depend on Superset being importable either.

PCS-10243 hooks: this file also covers the 7 new `hooks` section checks
(spec §4.5) added on top of the baseline above -- `HooksResolved`,
`ShapePairRoundtrip`, `CallerSide`, `DirectorySide`, `ReadOnly`,
`CanManageNarrowOnly` and `FailClosed` -- in PASS/FAIL/SKIP form, mostly
pure (small inline hook functions passed straight as `Context` fields, the
same fakes-only style as the sections above), plus a few `harness` cases
for what only the real registry/database can prove: a hook configured on
`reg.hooks` end to end through `_build_context`, a write caught by
`plugins.counting()` against the REAL app registry (not just the no-app
fallback the pure test covers), and `OWNERSHIP_CAN_MANAGE`'s widening FAIL
against a real chart and share decision.
"""

from __future__ import annotations

import os

import pytest
from superset_ownership import plugin_verify as pv

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeAuthorizer:
    def __init__(self):
        self.tuples: set[tuple[str, str, str]] = set()
        self.membership: dict[tuple[str, str], bool] = {}
        self.write_calls = 0
        self.delete_calls = 0
        # Every `strict=` value this fake was called with, in call order --
        # a fake that just accepts and ignores the kwarg can't tell a test
        # "strict=True was dropped" from "it wasn't" (PR90 review N-6).
        self.strict_calls: list[bool] = []

    def write_tuple(self, user, relation, obj, *, strict=False):
        self.write_calls += 1
        self.strict_calls.append(strict)
        self.tuples.add((user, relation, obj))
        return True

    def delete_tuple(self, user, relation, obj, *, strict=False):
        self.delete_calls += 1
        self.strict_calls.append(strict)
        self.tuples.discard((user, relation, obj))
        return True

    def check(self, user, relation, obj):
        return (user, relation, obj) in self.tuples

    def user_in_group(self, user_guid, group_id):
        return self.membership.get((user_guid, group_id), False)


def _page(items, next_cursor=None):
    return {"items": items, "next_cursor": next_cursor}


class FakeDirectory:
    def __init__(self, groups_by_tenant=None, users_by_tenant=None):
        self.groups_by_tenant = groups_by_tenant or {}
        self.users_by_tenant = users_by_tenant or {}
        self.membership: dict[tuple[str, str], bool] = {}

    def list_groups(self, tenant, *, cursor=None):
        return _page(self.groups_by_tenant.get(tenant, []))

    def search_users(self, tenant, query, *, limit=100, cursor=None):
        # Respects `limit`/`cursor` (an offset, as a string) for real, so a
        # test driving `limit=2` vs `limit=100` walks genuinely exercises
        # pagination instead of trivially agreeing because `limit` was
        # ignored (PR90 review H-5/#25 "K/V").
        users = self.users_by_tenant.get(tenant, [])
        start = int(cursor) if cursor else 0
        page = users[start : start + limit]
        next_cursor = str(start + limit) if start + limit < len(users) else None
        return _page(page, next_cursor)

    def group_exists(self, group_id):
        for groups in self.groups_by_tenant.values():
            if any(g["id"] == group_id for g in groups):
                return True
        return False

    def group_members(self, group_id, *, cursor=None):
        return _page([])

    def user_in_group(self, user_guid, group_id):
        return self.membership.get((user_guid, group_id), False)

    def health(self):
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class DirectoryHealth:
            ok: bool
            detail: str = ""

        return DirectoryHealth(True, "fake")


class FakeIdentity:
    def __init__(self, guids=None, tenants=None):
        self.guids = guids or {}
        self.tenants = tenants or {}

    def member_guid(self, user):
        return self.guids.get(user)

    def tenant_guid(self, user):
        return self.tenants.get(user)


def make_ctx(**overrides) -> pv.Context:
    return pv.Context(**overrides)


# ---------------------------------------------------------------------------
# load
# ---------------------------------------------------------------------------


def test_plugins_load_passes_when_nothing_degraded():
    ctx = make_ctx(describe={"degraded": []})
    result = pv.PluginsLoad().run(ctx)
    assert result.status == "PASS"


def test_plugins_load_fails_and_names_the_seam():
    ctx = make_ctx(
        describe={"degraded": ["directory"], "failures": {"directory": "boom"}}
    )
    result = pv.PluginsLoad().run(ctx)
    assert result.status == "FAIL"
    assert "directory" in result.detail
    assert "boom" in result.detail


# ---------------------------------------------------------------------------
# connection
# ---------------------------------------------------------------------------


def test_reachable_skips_without_a_connection():
    assert pv.Reachable().run(make_ctx(connection=None)).status == "SKIP"


def test_reachable_passes_and_fails():
    ctx = make_ctx(connection=object(), fga_reachable=lambda: True)
    assert pv.Reachable().run(ctx).status == "PASS"
    ctx = make_ctx(connection=object(), fga_reachable=lambda: False)
    assert pv.Reachable().run(ctx).status == "FAIL"


def test_reachable_catches_a_raise():
    def boom():
        raise ConnectionError("no route to host")

    ctx = make_ctx(connection=object(), fga_reachable=boom)
    result = pv.Reachable().run(ctx)
    assert result.status == "FAIL"
    assert "no route to host" in result.detail


def test_model_pinned():
    class Conn:
        model_id = "01H..."

    assert pv.ModelPinned().run(make_ctx(connection=Conn())).status == "PASS"

    class Unpinned:
        model_id = None

    assert pv.ModelPinned().run(make_ctx(connection=Unpinned())).status == "WARN"


def test_required_relations_pass_missing_and_group_tenant_only():
    required = {"group": ["member", "tenant"], "dashboard": ["owner"]}

    def all_present():
        return frozenset({"group.member", "group.tenant", "dashboard.owner"})

    ctx = make_ctx(required_relations=required, fetch_model_relations=all_present)
    assert pv.RequiredRelations().run(ctx).status == "PASS"

    def missing_group_tenant():
        return frozenset({"group.member", "dashboard.owner"})

    ctx = make_ctx(
        required_relations=required, fetch_model_relations=missing_group_tenant
    )
    result = pv.RequiredRelations().run(ctx)
    assert result.status == "WARN"
    assert "group.tenant" in result.detail

    def missing_more():
        return frozenset({"group.member"})

    ctx = make_ctx(required_relations=required, fetch_model_relations=missing_more)
    result = pv.RequiredRelations().run(ctx)
    assert result.status == "FAIL"


def test_scratch_tuple_roundtrip_pass_and_fail():
    ctx = make_ctx(authorizer=FakeAuthorizer())
    result = pv.ScratchTupleRoundTrip().run(ctx)
    assert result.status == "PASS"
    authorizer = ctx.authorizer
    assert authorizer.write_calls == 2
    assert authorizer.delete_calls == 2

    class Raising:
        def write_tuple(self, *a, **kw):
            raise RuntimeError("store down")

        def delete_tuple(self, *a, **kw):
            raise RuntimeError("store down")

    result = pv.ScratchTupleRoundTrip().run(make_ctx(authorizer=Raising()))
    assert result.status == "FAIL"

    class RejectingButNotRaising:
        """The real, non-strict `fga.write` shape: a rejected write does not
        raise, it returns `False`. Locks in H-1: ignoring the booleans would
        report PASS here."""

        def write_tuple(self, *a, **kw):
            return False

        def delete_tuple(self, *a, **kw):
            return False

    result = pv.ScratchTupleRoundTrip().run(
        make_ctx(authorizer=RejectingButNotRaising())
    )
    assert result.status == "FAIL"
    assert "not all True" in result.detail


def test_scratch_tuple_roundtrip_cleans_up_even_when_the_write_raises():
    """A write that raises must still attempt the delete cleanup (H-1's
    "cleans up on failure") -- proven here by counting delete attempts, not
    just checking the final status."""
    delete_calls = []

    class RaisesOnWriteOnly:
        def write_tuple(self, *a, **kw):
            raise RuntimeError("store down")

        def delete_tuple(self, user, relation, obj, *, strict=False):
            delete_calls.append((user, relation, obj))
            return True

    result = pv.ScratchTupleRoundTrip().run(make_ctx(authorizer=RaisesOnWriteOnly()))
    assert result.status == "FAIL"
    assert len(delete_calls) == 2, "cleanup must still run after a write raised"


def test_scratch_tuple_roundtrip_calls_are_all_strict():
    """PR90 review N-6: a mutant dropping `strict=True` from the round
    trip's first write survives the rest of the suite untouched -- the
    fake accepts and ignores the kwarg regardless of its value, so only a
    test that inspects the calls THEMSELVES (not just the PASS/FAIL
    outcome, which a lenient fake reaches either way) can catch it."""
    authorizer = FakeAuthorizer()
    ctx = make_ctx(authorizer=authorizer)
    result = pv.ScratchTupleRoundTrip().run(ctx)
    assert result.status == "PASS"
    assert len(authorizer.strict_calls) == 4, authorizer.strict_calls
    assert all(authorizer.strict_calls), (
        f"every write_tuple/delete_tuple call must pass strict=True: "
        f"{authorizer.strict_calls}"
    )


# ---------------------------------------------------------------------------
# vocabulary
# ---------------------------------------------------------------------------


def test_group_has_tenant_tuple_pass_fail_skip():
    class Dir:
        # `(self, tenant, cursor)` -- the real `OpenFGADirectory._fast_groups`
        # signature; a fake that dropped `cursor` never caught the real
        # call raising `TypeError` (PR90 review B-4).
        def _fast_groups(self, tenant, cursor):
            assert cursor is None
            return _page([{"id": "group:a"}])

        def _walk_groups(self, tenant):
            return _page([{"id": "group:a"}])

    ctx = make_ctx(tenant="t1", directory=Dir())
    assert pv.GroupHasTenantTuple().run(ctx).status == "PASS"

    class DirMissing:
        def _fast_groups(self, tenant, cursor):
            return _page([])

        def _walk_groups(self, tenant):
            return _page([{"id": "group:untenanted"}])

    result = pv.GroupHasTenantTuple().run(make_ctx(tenant="t1", directory=DirMissing()))
    assert result.status == "FAIL"
    assert "group:untenanted" in result.detail

    # LocalDirectory has no fast-path helpers.
    class LocalLike:
        pass

    assert (
        pv.GroupHasTenantTuple()
        .run(make_ctx(tenant="t1", directory=LocalLike()))
        .status
        == "SKIP"
    )
    assert pv.GroupHasTenantTuple().run(make_ctx(tenant=None)).status == "SKIP"


def test_group_has_tenant_tuple_skips_not_fails_when_the_model_lacks_it():
    """PR90 review N-2a: a store whose model has no `group.tenant` relation
    must SKIP (the same verdict `required_relations` already gives it), not
    FAIL -- OpenFGA answers a Read for an undefined relation with `200 []`,
    not an error, so every walked group used to report as "missing" a
    tuple it was never possible to write in the first place."""

    class DirWithGroups:
        def _fast_groups(self, tenant, cursor):
            return _page([])  # the undefined-relation shape: 200, no items

        def _walk_groups(self, tenant):
            return _page([{"id": "group:a"}, {"id": "group:b"}])

    def missing_group_tenant():
        return frozenset({"group.member"})  # no "group.tenant"

    ctx = make_ctx(
        tenant="t1",
        directory=DirWithGroups(),
        fetch_model_relations=missing_group_tenant,
    )
    result = pv.GroupHasTenantTuple().run(ctx)
    assert result.status == "SKIP"
    assert "group.tenant" in result.detail


def test_member_guids_resolve_warn_and_pass():
    directory = FakeDirectory(
        users_by_tenant={
            "t1": [
                {"guid": "u1", "superset_id": 1},
                {"guid": "u2", "superset_id": None},
            ]
        }
    )
    result = pv.MemberGuidsResolve().run(make_ctx(tenant="t1", directory=directory))
    assert result.status == "WARN"
    assert "1/2" in result.detail

    directory_all_known = FakeDirectory(
        users_by_tenant={"t1": [{"guid": "u1", "superset_id": 1}]}
    )
    result = pv.MemberGuidsResolve().run(
        make_ctx(tenant="t1", directory=directory_all_known)
    )
    assert result.status == "PASS"


def test_administrator_group_exists():
    directory = FakeDirectory(groups_by_tenant={"t1": [{"id": "group:admin_t1"}]})
    ctx = make_ctx(
        tenant="t1",
        directory=directory,
        tenant_administrator_group=lambda t: f"group:admin_{t}",
    )
    assert pv.AdministratorGroupExists().run(ctx).status == "PASS"

    empty_directory = FakeDirectory()
    ctx = make_ctx(
        tenant="t1",
        directory=empty_directory,
        tenant_administrator_group=lambda t: f"group:admin_{t}",
    )
    assert pv.AdministratorGroupExists().run(ctx).status == "WARN"


def test_no_member_in_two_tenants():
    directory = FakeDirectory(
        users_by_tenant={"t1": [{"guid": "u1", "superset_id": 1}]}
    )
    authorizer = FakeAuthorizer()
    ctx = make_ctx(
        tenant="t1",
        candidate_tenants=["t1", "t2"],
        directory=directory,
        authorizer=authorizer,
    )
    assert pv.NoMemberInTwoTenants().run(ctx).status == "PASS"

    authorizer.tuples.add(("user:u1", "member", "tenant:t2"))
    result = pv.NoMemberInTwoTenants().run(ctx)
    assert result.status == "FAIL"
    assert "u1" in result.detail


def test_group_ids_parse():
    def split(group_id):
        # `identity.split_group_id`'s real contract: a bad id comes back as
        # None, never a raise (PR90 review B-4) -- a check that only caught
        # an exception never saw this.
        if group_id != "good_t1":
            return None
        return ("good", "t1")

    result = pv.GroupIdsParse().run(
        make_ctx(
            tenant="t1",
            split_group_id=split,
            list_tenant_group_ids=lambda t: ["good_t1", "bad-id"],
        )
    )
    assert result.status == "FAIL"
    assert "bad-id" in result.detail

    result = pv.GroupIdsParse().run(
        make_ctx(
            tenant="t1",
            split_group_id=split,
            list_tenant_group_ids=lambda t: ["good_t1"],
        )
    )
    assert result.status == "PASS"


def test_group_ids_parse_skips_without_a_lister():
    assert (
        pv.GroupIdsParse()
        .run(make_ctx(tenant="t1", split_group_id=lambda g: ("g", "t1")))
        .status
        == "SKIP"
    )


def test_group_ids_parse_fails_strict_on_a_store_error():
    """PR90 review N-4: a store failure while listing must FAIL, never
    collapse to an empty set and PASS -- the earlier shape (`fga.list_objects`'s
    own non-strict default swallowing the failure) reported `PASS every
    listed group id parses` on an unreachable or erroring store."""

    def boom(tenant):
        raise RuntimeError("store unreachable")

    ctx = make_ctx(
        tenant="t1", split_group_id=lambda g: ("g", "t1"), list_tenant_group_ids=boom
    )
    result = pv.GroupIdsParse().run(ctx)
    assert result.status == "FAIL"
    assert "store unreachable" in result.detail


def test_group_ids_parse_skips_a_tenant_too_large_to_walk():
    """PR90 review N-4: `--max-members` bounds the walk -- a tenant over
    the bound SKIPs with a reason instead of paying an unbounded store
    cost or silently truncating."""

    def too_many(tenant):
        raise pv.TooManyMembersError("501 members exceeds --max-members=500")

    ctx = make_ctx(
        tenant="t1",
        split_group_id=lambda g: ("g", "t1"),
        list_tenant_group_ids=too_many,
    )
    result = pv.GroupIdsParse().run(ctx)
    assert result.status == "SKIP"
    assert "max-members" in result.detail


# A real v4 GUID -- `is_structural_tenant_role` (imported inside the check)
# only recognises a GUID this shape, so the structural-role FAIL case below
# needs one, unlike the plain "t1"/"tenant-a" strings this file's other
# checks use for `--tenant`.
_A_TENANT_GUID = "0a1b2c3d-1111-4222-8333-444455556666"


def test_manage_permission_template_skips_when_unset():
    result = pv.ManagePermissionTemplate().run(
        make_ctx(tenant=_A_TENANT_GUID, manage_permission_role=None)
    )
    assert result.status == "SKIP"
    assert "not set" in result.detail
    assert "refused" not in result.detail


def test_manage_permission_template_skips_with_the_right_wording_when_refused():
    # R2-N3 (review round 2, PR #98, `review-manage-template-pr98.md`):
    # `manage_permission_role` alone cannot tell "not set" from "set but
    # refused by api.parse_manage_permission" -- both parse to `None`. A
    # malformed OWNERSHIP_MANAGE_PERMISSION (e.g. `sharing_manager_{Tenant}`)
    # WARNS at parse time and then used to have this check repeat "is not
    # set", which is not what happened -- `manage_permission_configured`
    # (set by `_build_context` from the RAW setting) lets it say the
    # accurate thing instead.
    result = pv.ManagePermissionTemplate().run(
        make_ctx(
            tenant=_A_TENANT_GUID,
            manage_permission_role=None,
            manage_permission_configured=True,
        )
    )
    assert result.status == "SKIP"
    assert "refused" in result.detail
    assert "is not set" not in result.detail


def test_manage_permission_template_skips_a_literal_role():
    # A non-templated value is already fully validated by
    # `api.parse_manage_permission` (a bad literal would never have made it
    # into `manage_permission_role` at all) -- nothing left for this check
    # to add.
    result = pv.ManagePermissionTemplate().run(
        make_ctx(tenant=_A_TENANT_GUID, manage_permission_role="sharing_manager")
    )
    assert result.status == "SKIP"
    assert "template" in result.detail


def test_manage_permission_template_skips_without_a_tenant():
    result = pv.ManagePermissionTemplate().run(
        make_ctx(tenant=None, manage_permission_role="sharing_manager_{tenant}")
    )
    assert result.status == "SKIP"
    assert "--tenant" in result.detail


def test_manage_permission_template_passes_and_echoes_the_rendering():
    result = pv.ManagePermissionTemplate().run(
        make_ctx(
            tenant=_A_TENANT_GUID, manage_permission_role="sharing_manager_{tenant}"
        )
    )
    assert result.status == "PASS"
    assert f"sharing_manager_{_A_TENANT_GUID}" in result.detail


def test_manage_permission_template_fails_on_a_structural_rendering():
    # Reachable whenever an OWNERSHIP_SPLIT_GROUP_ID hook's structural
    # answer depends on the GUID itself, not only on
    # OWNERSHIP_GROUP_ID_FORMAT -- `parse_manage_permission` already
    # refuses a template that would ALWAYS render structural, checked
    # against one probe GUID, but cannot see every GUID a hook might treat
    # differently. Still worth a hard FAIL: it means this tenant's holders
    # are, right now, a whole population -- the check's only non-PASS/SKIP
    # outcome, hence its declared severity is FAIL.
    result = pv.ManagePermissionTemplate().run(
        make_ctx(tenant=_A_TENANT_GUID, manage_permission_role="tenant_{tenant}")
    )
    assert result.status == "FAIL"
    assert pv.ManagePermissionTemplate.severity == "FAIL"
    assert "structural" in result.detail


# ---------------------------------------------------------------------------
# directory
# ---------------------------------------------------------------------------


def test_tenant_isolation():
    directory = FakeDirectory(
        groups_by_tenant={"t1": [{"id": "group:a"}], "t2": [{"id": "group:b"}]}
    )
    ctx = make_ctx(candidate_tenants=["t1", "t2"], directory=directory)
    assert pv.TenantIsolation().run(ctx).status == "PASS"

    overlapping = FakeDirectory(
        groups_by_tenant={
            "t1": [{"id": "group:shared"}],
            "t2": [{"id": "group:shared"}],
        }
    )
    result = pv.TenantIsolation().run(
        make_ctx(candidate_tenants=["t1", "t2"], directory=overlapping)
    )
    assert result.status == "FAIL"

    assert (
        pv.TenantIsolation()
        .run(make_ctx(candidate_tenants=["t1"], directory=directory))
        .status
        == "SKIP"
    )


def test_pagination_roundtrips():
    directory = FakeDirectory(users_by_tenant={"t1": [{"guid": "u1"}, {"guid": "u2"}]})
    result = pv.PaginationRoundTrips().run(make_ctx(tenant="t1", directory=directory))
    assert result.status == "PASS"


def test_pagination_roundtrips_mismatch():
    class Dir:
        def search_users(self, tenant, query, *, limit=100, cursor=None):
            if limit == 2:
                return _page([{"guid": "u1"}])
            return _page([{"guid": "u1"}, {"guid": "u2"}])

    result = pv.PaginationRoundTrips().run(make_ctx(tenant="t1", directory=Dir()))
    assert result.status == "FAIL"


def test_not_found_vs_empty():
    directory = FakeDirectory(
        groups_by_tenant={"t1": [{"id": "group:empty", "members": 0}]}
    )
    result = pv.NotFoundVsEmpty().run(make_ctx(tenant="t1", directory=directory))
    assert result.status == "PASS"


def test_not_found_vs_empty_fails_when_missing_group_reports_existing():
    class Dir(FakeDirectory):
        def group_exists(self, group_id):
            return True

    directory = Dir(groups_by_tenant={"t1": [{"id": "group:empty", "members": 0}]})
    result = pv.NotFoundVsEmpty().run(make_ctx(tenant="t1", directory=directory))
    assert result.status == "FAIL"


def test_user_in_group_agrees_and_disagrees():
    directory = FakeDirectory(
        users_by_tenant={"t1": [{"guid": "u1"}]},
        groups_by_tenant={"t1": [{"id": "group:a"}]},
    )
    directory.membership[("u1", "group:a")] = True
    authorizer = FakeAuthorizer()
    authorizer.membership[("u1", "group:a")] = True
    ctx = make_ctx(tenant="t1", directory=directory, authorizer=authorizer)
    assert pv.UserInGroupAgrees().run(ctx).status == "PASS"

    authorizer.membership[("u1", "group:a")] = False
    result = pv.UserInGroupAgrees().run(ctx)
    assert result.status == "FAIL"


def test_health_pass_and_fail():
    directory = FakeDirectory()
    assert pv.DirectoryHealthCheck().run(make_ctx(directory=directory)).status == "PASS"

    class Unhealthy:
        def health(self):
            from dataclasses import dataclass

            @dataclass(frozen=True)
            class H:
                ok: bool
                detail: str = ""

            return H(False, "store unreachable")

    result = pv.DirectoryHealthCheck().run(make_ctx(directory=Unhealthy()))
    assert result.status == "FAIL"


def test_latency_pass_warn_skip():
    assert pv.Latency().run(make_ctx()).status == "SKIP"
    ctx = make_ctx(latency_ms=300, latencies_ms=[10.0, 20.0, 30.0])
    assert pv.Latency().run(ctx).status == "PASS"
    ctx = make_ctx(latency_ms=5, latencies_ms=[10.0, 20.0, 30.0])
    assert pv.Latency().run(ctx).status == "WARN"


def test_context_timed_records_latency():
    ctx = make_ctx()
    ctx.timed(lambda: 1 + 1)
    assert len(ctx.latencies_ms) == 1
    assert ctx.latencies_ms[0] >= 0


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------


def test_guid_v4_pass_and_fail():
    identity = FakeIdentity(guids={"u1": "3f0a91c7-2d84-4e63-9b15-7c4e8a2f6d31"})
    ctx = make_ctx(jit_users=["u1"], identity=identity)
    assert pv.GuidV4().run(ctx).status == "PASS"

    identity = FakeIdentity(guids={"u1": "not-a-guid"})
    ctx = make_ctx(jit_users=["u1"], identity=identity)
    result = pv.GuidV4().run(ctx)
    assert result.status == "FAIL"


def test_tenant_known_to_store():
    identity = FakeIdentity(guids={"u1": "guid-1"}, tenants={"u1": "tenant-1"})
    authorizer = FakeAuthorizer()
    authorizer.tuples.add(("user:guid-1", "member", "tenant:tenant-1"))
    ctx = make_ctx(jit_users=["u1"], identity=identity, authorizer=authorizer)
    assert pv.TenantKnownToStore().run(ctx).status == "PASS"

    authorizer.tuples.clear()
    result = pv.TenantKnownToStore().run(ctx)
    assert result.status == "FAIL"


def test_normalize_subject_roundtrip():
    ctx = make_ctx(
        jit_users=["u1"],
        member_ref=lambda u: f"ref-{u}",
        normalize_subject=lambda s: s if s.startswith("ref-") else "ref-u1",
    )
    assert pv.NormalizeSubjectRoundTrip().run(ctx).status == "PASS"

    ctx = make_ctx(
        jit_users=["u1"],
        member_ref=lambda u: f"ref-{u}",
        normalize_subject=lambda s: "something-else",
    )
    result = pv.NormalizeSubjectRoundTrip().run(ctx)
    assert result.status == "FAIL"


def test_normalize_subject_roundtrip_probes_the_user_prefixed_id_form():
    """`normalize_subject`'s own contract (identity.py) rejects any bare,
    colon-less string -- calling it with `str(u.id)` alone (no `user:`
    prefix) fails for every sampled user regardless of the plug-ins, which
    is exactly the bug the spec table's own wording caused (PR90 review
    B-4). Locks in the corrected call shape: `f"user:{u.id}"`."""
    calls = []

    class JitUser:
        id = 42

    def spy_normalize(s):
        calls.append(s)
        return "ref-42" if s in ("ref-42", "user:42") else None

    ctx = make_ctx(
        jit_users=[JitUser()],
        member_ref=lambda u: "ref-42",
        normalize_subject=spy_normalize,
    )
    result = pv.NormalizeSubjectRoundTrip().run(ctx)
    assert result.status == "PASS", result.detail
    assert "user:42" in calls, f"expected a user:<id>-prefixed probe, got calls={calls}"
    assert "42" not in calls, "a bare, colon-less id must never be passed"


# ---------------------------------------------------------------------------
# invariants
# ---------------------------------------------------------------------------


def test_invariants_skip_without_object_and_as():
    ctx = make_ctx()
    assert pv.OwnerZeroCalls().run(ctx).status == "SKIP"
    assert pv.NonOwnerOneCheck().run(ctx).status == "SKIP"
    assert pv.PublicZeroCalls().run(ctx).status == "SKIP"


def test_owner_zero_calls():
    ctx = make_ctx(invariant_probe=lambda kind: ())
    assert pv.OwnerZeroCalls().run(ctx).status == "PASS"
    ctx = make_ctx(invariant_probe=lambda kind: ("check",))
    assert pv.OwnerZeroCalls().run(ctx).status == "FAIL"


def test_non_owner_one_check_accepts_object_tenant():
    ctx = make_ctx(invariant_probe=lambda kind: ("check",))
    assert pv.NonOwnerOneCheck().run(ctx).status == "PASS"
    ctx = make_ctx(invariant_probe=lambda kind: ("check", "object_tenant"))
    assert pv.NonOwnerOneCheck().run(ctx).status == "PASS"
    ctx = make_ctx(invariant_probe=lambda kind: ("check", "check"))
    assert pv.NonOwnerOneCheck().run(ctx).status == "FAIL"


def test_non_owner_one_check_on_a_public_object_expects_zero_calls():
    """PR90 review N-2b: a public object grants everyone the viewer
    relation with no store round trip at all -- exactly what
    `public_zero_calls` asserts for the same object when the caller is
    public. A non-owner's decision on it costs 0 calls too, not one
    `check` (reproduced live: `--object dashboard:5 --as <ben>`, a public
    object, FAILed `expected exactly one check (+object_tenant): ()`,
    which is backwards)."""
    ctx = make_ctx(invariant_probe=lambda kind: (), object_is_public=lambda: True)
    result = pv.NonOwnerOneCheck().run(ctx)
    assert result.status == "PASS", result.detail

    ctx = make_ctx(
        invariant_probe=lambda kind: ("check",), object_is_public=lambda: True
    )
    result = pv.NonOwnerOneCheck().run(ctx)
    assert result.status == "FAIL", "a public object making any call is wrong"

    # A private object is unaffected: the original one-check contract.
    ctx = make_ctx(
        invariant_probe=lambda kind: ("check",), object_is_public=lambda: False
    )
    assert pv.NonOwnerOneCheck().run(ctx).status == "PASS"


def test_public_zero_calls_skips_when_not_applicable():
    ctx = make_ctx(invariant_probe=lambda kind: None)
    assert pv.PublicZeroCalls().run(ctx).status == "SKIP"
    ctx = make_ctx(invariant_probe=lambda kind: ())
    assert pv.PublicZeroCalls().run(ctx).status == "PASS"


# ---------------------------------------------------------------------------
# hooks (spec §4.5)
# ---------------------------------------------------------------------------


def _hooks_describe(*, config=(), degraded=(), other=()) -> dict:
    """A `plugins.describe()["hooks"]`-shaped dict: `config` settings get
    `source="config"`, `degraded` ones `source="default (degraded)"`, and
    `other` ones (or anything a test wants present but unconfigured)
    `source="default"`."""
    out: dict = {}
    for name in config:
        out[name] = {"source": "config", "path": f"test:{name}"}
    for name in degraded:
        out[name] = {"source": "default (degraded)"}
    for name in other:
        out.setdefault(name, {"source": "default"})
    return out


def _default_group_id(name, tenant):
    return f"{name}_{tenant}"


def _default_split_group_id(group_id_or_ref):
    text = group_id_or_ref
    if text.startswith("group:"):
        text = text[len("group:") :]
        text = text.split("#", 1)[0]
    if "_" not in text:
        return None
    name, _, tenant = text.rpartition("_")
    return name, tenant


# -- hooks/resolved -----------------------------------------------------


def test_hooks_resolved_skips_with_nothing_configured():
    assert pv.HooksResolved().run(make_ctx(hooks_describe={})).status == "SKIP"
    ctx = make_ctx(hooks_describe=_hooks_describe(other=["OWNERSHIP_MEMBER_GUID"]))
    assert pv.HooksResolved().run(ctx).status == "SKIP"


def test_hooks_resolved_passes_and_lists_every_hook_source():
    ctx = make_ctx(
        hooks_describe=_hooks_describe(
            config=["OWNERSHIP_MEMBER_GUID"], other=["OWNERSHIP_TENANT_GUID"]
        )
    )
    result = pv.HooksResolved().run(ctx)
    assert result.status == "PASS"
    assert "OWNERSHIP_MEMBER_GUID=config" in result.detail
    assert "OWNERSHIP_TENANT_GUID=default" in result.detail


def test_hooks_resolved_fails_and_names_the_degraded_hook():
    ctx = make_ctx(
        hooks_describe=_hooks_describe(
            config=["OWNERSHIP_MEMBER_GUID"], degraded=["OWNERSHIP_TENANT_GUID"]
        ),
        hook_failures={"OWNERSHIP_TENANT_GUID": "HookError"},
    )
    result = pv.HooksResolved().run(ctx)
    assert result.status == "FAIL"
    assert "OWNERSHIP_TENANT_GUID" in result.detail
    assert "HookError" in result.detail


# -- hooks/shape_pair_roundtrip ------------------------------------------


def test_shape_pair_roundtrip_never_skips_and_passes_by_default():
    ctx = make_ctx(group_id=_default_group_id, split_group_id=_default_split_group_id)
    result = pv.ShapePairRoundtrip().run(ctx)
    assert result.status == "PASS", result.detail


def test_shape_pair_roundtrip_checks_every_directory_listed_group():
    tenant = "t1"
    directory = FakeDirectory(
        groups_by_tenant={tenant: [{"id": f"group:designer_{tenant}"}]}
    )
    ctx = make_ctx(
        tenant=tenant,
        directory=directory,
        group_id=_default_group_id,
        split_group_id=_default_split_group_id,
        tenant_administrator_group=lambda t: f"group:tenant_administrator_{t}",
    )
    result = pv.ShapePairRoundtrip().run(ctx)
    assert result.status == "PASS", result.detail


def test_shape_pair_roundtrip_fails_when_split_group_id_returns_none():
    ctx = make_ctx(group_id=_default_group_id, split_group_id=lambda g: None)
    result = pv.ShapePairRoundtrip().run(ctx)
    assert result.status == "FAIL"
    assert "split_group_id returned None" in result.detail


def test_shape_pair_roundtrip_fails_when_the_pair_does_not_round_trip():
    def broken_group_id(name, tenant):
        return name  # drops the tenant -- not the inverse of the split half

    ctx = make_ctx(group_id=broken_group_id, split_group_id=_default_split_group_id)
    result = pv.ShapePairRoundtrip().run(ctx)
    assert result.status == "FAIL"


# -- hooks/caller_side ----------------------------------------------------


def test_caller_side_skips_with_no_hook_configured():
    ctx = make_ctx(hooks_describe={}, get_hook=lambda name: None, jit_users=[object()])
    assert pv.CallerSide().run(ctx).status == "SKIP"


def test_caller_side_skips_without_sample_users():
    ctx = make_ctx(
        hooks_describe=_hooks_describe(config=["OWNERSHIP_MEMBER_GUID"]),
        get_hook=lambda name: lambda u: None,
        jit_users=[],
    )
    result = pv.CallerSide().run(ctx)
    assert result.status == "SKIP"
    assert "sample" in result.detail


def test_caller_side_passes_for_well_shaped_answers():
    users = [object(), object()]
    hooks = {
        "OWNERSHIP_MEMBER_GUID": lambda u: "3f0a91c7-2d84-4e63-9b15-7c4e8a2f6d31",
        "OWNERSHIP_DISPLAY_NAME": lambda u: "A Name",
        "OWNERSHIP_IS_TENANT_ADMINISTRATOR": lambda u: False,
    }
    ctx = make_ctx(
        hooks_describe=_hooks_describe(config=list(hooks)),
        get_hook=hooks.get,
        jit_users=users,
        sample_users=2,
    )
    result = pv.CallerSide().run(ctx)
    assert result.status == "PASS", result.detail


def test_caller_side_fails_on_a_bad_shape():
    ctx = make_ctx(
        hooks_describe=_hooks_describe(config=["OWNERSHIP_DISPLAY_NAME"]),
        get_hook=lambda name: lambda u: "",
        jit_users=[object()],
    )
    result = pv.CallerSide().run(ctx)
    assert result.status == "FAIL"
    assert "not a non-empty str" in result.detail


def test_caller_side_fails_and_never_logs_the_exception_message():
    """§4.5.1 item 4: a raise is reported by exception CLASS only -- the
    message (which may carry a credential) must never reach the Result."""

    def boom(u):
        raise RuntimeError("super-secret-token-abc123")

    ctx = make_ctx(
        hooks_describe=_hooks_describe(config=["OWNERSHIP_MEMBER_GUID"]),
        get_hook=lambda name: boom,
        jit_users=[object()],
    )
    result = pv.CallerSide().run(ctx)
    assert result.status == "FAIL"
    assert "RuntimeError" in result.detail
    assert "super-secret-token-abc123" not in result.detail


# -- hooks/directory_side --------------------------------------------------


def test_directory_side_skips_with_no_hook_configured():
    ctx = make_ctx(hooks_describe={}, tenant="t1")
    assert pv.DirectorySide().run(ctx).status == "SKIP"


def test_directory_side_skips_without_tenant():
    ctx = make_ctx(
        hooks_describe=_hooks_describe(config=["OWNERSHIP_USERS_OF_TENANT"]),
        get_hook=lambda name: lambda *a: _page([]),
    )
    result = pv.DirectorySide().run(ctx)
    assert result.status == "SKIP"
    assert "--tenant" in result.detail


def test_directory_side_passes_for_a_well_shaped_page():
    def users_of_tenant(tenant, query, limit, cursor):
        return _page(
            [{"guid": "g1", "display_name": "G", "email": None, "superset_id": None}]
        )

    ctx = make_ctx(
        tenant="t1",
        hooks_describe=_hooks_describe(config=["OWNERSHIP_USERS_OF_TENANT"]),
        get_hook=lambda name: users_of_tenant,
    )
    result = pv.DirectorySide().run(ctx)
    assert result.status == "PASS", result.detail
    assert "OWNERSHIP_USERS_OF_TENANT" in result.detail


def test_directory_side_fails_when_the_hook_raises_and_never_logs_the_message():
    def boom(tenant, query, limit, cursor):
        raise ValueError("postgresql://user:hunter2@host/dsn")

    ctx = make_ctx(
        tenant="t1",
        hooks_describe=_hooks_describe(config=["OWNERSHIP_USERS_OF_TENANT"]),
        get_hook=lambda name: boom,
    )
    result = pv.DirectorySide().run(ctx)
    assert result.status == "FAIL"
    assert "ValueError" in result.detail
    assert "hunter2" not in result.detail


def test_directory_side_fails_on_a_malformed_page():
    ctx = make_ctx(
        tenant="t1",
        hooks_describe=_hooks_describe(config=["OWNERSHIP_USERS_OF_TENANT"]),
        get_hook=lambda name: (
            lambda *a: {"items": [{"guid": "g1"}], "next_cursor": None}
        ),
    )
    result = pv.DirectorySide().run(ctx)
    assert result.status == "FAIL"


def test_directory_side_flags_an_administrator_consistency_mismatch():
    admins = [{"guid": "g1", "display_name": "G", "email": None, "superset_id": 42}]
    ctx = make_ctx(
        tenant="t1",
        hooks_describe=_hooks_describe(config=["OWNERSHIP_ADMINISTRATORS_OF_TENANT"]),
        get_hook=lambda name: lambda tenant: admins,
        user_by_id=lambda uid: object(),
        is_tenant_administrator=lambda user: False,
    )
    result = pv.DirectorySide().run(ctx)
    assert result.status == "FAIL"
    assert "g1" in result.detail


def test_directory_side_skips_when_nothing_could_be_sampled():
    ctx = make_ctx(
        tenant="t1",
        hooks_describe=_hooks_describe(config=["OWNERSHIP_USER_IN_GROUP"]),
        get_hook=lambda name: lambda *a: True,
        directory=None,
    )
    result = pv.DirectorySide().run(ctx)
    assert result.status == "SKIP"
    assert "sampled" in result.detail


# -- hooks/read_only --------------------------------------------------------


def test_read_only_skips_with_no_hook_configured():
    assert pv.ReadOnly().run(make_ctx(hooks_describe={})).status == "SKIP"


def test_read_only_passes_for_a_pure_hook():
    ctx = make_ctx(
        hooks_describe=_hooks_describe(config=["OWNERSHIP_GROUP_EXISTS"]),
        get_hook=lambda name: lambda group_id: True,
    )
    result = pv.ReadOnly().run(ctx)
    assert result.status == "PASS", result.detail


def test_read_only_fails_when_a_hook_writes_to_the_authorizer():
    """Runs against the REAL `plugins.get_authorizer()`/`plugins.counting()`
    (the no-app fallback path -- this file's own tests never build a Flask
    app), so the write is a real `LocalAuthorizer.write_tuple` call, caught
    by the SAME counting proxy `test_endpoints.py`'s invariants rely on."""

    def writer(group_id):
        from superset_ownership import plugins

        plugins.get_authorizer().write_tuple(
            "user:verify-hooks-ro", "member", "group:verify-hooks-ro"
        )
        return True

    ctx = make_ctx(
        hooks_describe=_hooks_describe(config=["OWNERSHIP_GROUP_EXISTS"]),
        get_hook=lambda name: writer,
    )
    result = pv.ReadOnly().run(ctx)
    assert result.status == "FAIL"
    assert "write_tuple" in result.detail


def test_read_only_fails_when_a_hook_writes_directly_through_fga(monkeypatch):
    """M-8: `plugins.counting()` wraps the Authorizer OBJECT's own methods
    only -- a hook that reaches `superset_ownership.fga`'s free functions
    directly (the reference hooks already do exactly this for READS,
    `fga.read_page`) is invisible to it. `_watch_fga_writes` wraps
    `fga._request`, the one choke point every fga write passes through, so
    this must FAIL even though nothing touched `plugins.get_authorizer()`
    at all."""
    from superset_ownership import fga

    class _FakeResponse:
        status_code = 200

        def json(self):
            return {}

        def raise_for_status(self):
            return None

    monkeypatch.setattr(fga, "_request", lambda *a, **k: _FakeResponse())

    def writer(group_id):
        fga.write_tuple("user:verify-hooks-fga", "member", "group:verify-hooks-fga")
        return True

    ctx = make_ctx(
        hooks_describe=_hooks_describe(config=["OWNERSHIP_GROUP_EXISTS"]),
        get_hook=lambda name: writer,
    )
    result = pv.ReadOnly().run(ctx)
    assert result.status == "FAIL", result.detail
    assert "fga" in result.detail
    assert "/write" in result.detail


def test_watch_fga_writes_restores_the_real_request_function():
    """The monkeypatch must not leak past the `with` block, success or
    raise -- the next caller (another check, or a real request) must reach
    the real `fga._request`, not a stale watcher."""
    from superset_ownership import fga

    real = fga._request  # noqa: SLF001
    with pv._watch_fga_writes():
        assert fga._request is not real  # noqa: SLF001
    assert fga._request is real  # noqa: SLF001

    with pytest.raises(RuntimeError):
        with pv._watch_fga_writes():
            raise RuntimeError("boom")
    assert fga._request is real  # noqa: SLF001


# -- hooks/can_manage_narrow_only -------------------------------------------


def test_can_manage_narrow_only_skips_with_no_hook_configured():
    ctx = make_ctx(get_hook=lambda name: None)
    assert pv.CanManageNarrowOnly().run(ctx).status == "SKIP"


def test_can_manage_narrow_only_skips_without_object_and_as():
    ctx = make_ctx(get_hook=lambda name: lambda u, s: None, can_manage_probe=None)
    result = pv.CanManageNarrowOnly().run(ctx)
    assert result.status == "SKIP"
    assert "--object" in result.detail


def test_can_manage_narrow_only_skips_when_the_probe_finds_nothing():
    ctx = make_ctx(
        get_hook=lambda name: lambda u, s: None, can_manage_probe=lambda: None
    )
    result = pv.CanManageNarrowOnly().run(ctx)
    assert result.status == "SKIP"


def test_can_manage_narrow_only_passes_when_the_hook_only_narrows():
    def hook(user, object_state):
        return (
            None
            if object_state["default_reason"] is None
            else object_state["default_reason"]
        )

    ctx = make_ctx(
        get_hook=lambda name: hook,
        can_manage_probe=lambda: ("user-1", {"default_reason": "owner"}),
    )
    result = pv.CanManageNarrowOnly().run(ctx)
    assert result.status == "PASS", result.detail


def test_can_manage_narrow_only_fails_when_the_hook_widens_with_no_default():
    ctx = make_ctx(
        get_hook=lambda name: lambda user, state: "admin",
        can_manage_probe=lambda: ("user-1", {"default_reason": None}),
    )
    result = pv.CanManageNarrowOnly().run(ctx)
    assert result.status == "FAIL"
    assert "widened" in result.detail


def test_can_manage_narrow_only_fails_when_the_hook_widens_beyond_the_default():
    def hook(user, object_state):
        default = object_state["default_reason"]
        return default if default is None else "admin"

    ctx = make_ctx(
        get_hook=lambda name: hook,
        can_manage_probe=lambda: ("user-1", {"default_reason": "owner"}),
    )
    result = pv.CanManageNarrowOnly().run(ctx)
    assert result.status == "FAIL"
    assert "widened" in result.detail


def test_can_manage_narrow_only_fails_and_never_logs_the_exception_message():
    def hook(user, object_state):
        raise RuntimeError("super-secret-token-xyz")

    ctx = make_ctx(
        get_hook=lambda name: hook,
        can_manage_probe=lambda: ("user-1", {"default_reason": "owner"}),
    )
    result = pv.CanManageNarrowOnly().run(ctx)
    assert result.status == "FAIL"
    assert "RuntimeError" in result.detail
    assert "super-secret-token-xyz" not in result.detail


# -- hooks/fail_closed -------------------------------------------------------


def test_fail_closed_skips_with_no_relevant_hook_configured():
    assert pv.FailClosed().run(make_ctx(hooks_describe={})).status == "SKIP"
    # A shape-side hook is out of this check's scope (call_or_default falls
    # back to the built-in format string on a raise, never a fixed
    # "unknown" -- there is nothing for fail_closed to assert about it).
    ctx = make_ctx(
        hooks_describe=_hooks_describe(config=["OWNERSHIP_GROUP_ID"]),
        get_hook=lambda name: lambda n, t: f"{n}_{t}",
    )
    assert pv.FailClosed().run(ctx).status == "SKIP"


def test_fail_closed_passes_when_a_raising_hook_still_answers_unknown():
    """The point of the check: `plugin_hooks.call` converts a raise into
    the documented "unknown" value -- this hook raises for user=None, and
    the registry must still answer None, never propagate."""

    def raises_on_none(user):
        if user is None:
            raise RuntimeError("no user")
        return "some-guid"

    ctx = make_ctx(
        hooks_describe=_hooks_describe(config=["OWNERSHIP_MEMBER_GUID"]),
        get_hook=lambda name: raises_on_none,
    )
    result = pv.FailClosed().run(ctx)
    assert result.status == "PASS", result.detail


def test_fail_closed_fails_when_the_hook_answers_something_other_than_unknown():
    ctx = make_ctx(
        hooks_describe=_hooks_describe(config=["OWNERSHIP_GROUP_EXISTS"]),
        get_hook=lambda name: lambda group_id: True,  # never False, even unknown
    )
    result = pv.FailClosed().run(ctx)
    assert result.status == "FAIL"
    assert "OWNERSHIP_GROUP_EXISTS" in result.detail


def test_fail_closed_never_probes_a_directory_side_hook_with_the_real_tenant():
    """H-2a: `--tenant` is real and, on a live run, populated -- a correct
    `OWNERSHIP_USERS_OF_TENANT` hook answers REAL data for it, which is not
    "unknown" and must not FAIL this check. Before the fix, `_probe_args`
    used `ctx.tenant` whenever it was set; this hook only answers something
    non-empty for that exact real tenant, so the old behaviour would FAIL
    a hook that is working correctly."""

    def users_of_tenant(tenant, query, limit, cursor):
        if tenant == "real-tenant-guid":
            return _page([{"guid": "u1", "display_name": "U", "email": None}])
        return _page([], next_cursor=None)  # the documented "unknown" shape

    ctx = make_ctx(
        hooks_describe=_hooks_describe(config=["OWNERSHIP_USERS_OF_TENANT"]),
        get_hook=lambda name: users_of_tenant,
        tenant="real-tenant-guid",
    )
    result = pv.FailClosed().run(ctx)
    assert result.status == "PASS", result.detail


def test_fail_closed_excludes_display_name():
    """H-2b: `OWNERSHIP_DISPLAY_NAME` is `call_or_default`, not `call` --
    `_coerce` turns every answer into a `str` (never `None`, `spec.unknown()`
    for this hook), so it can never pass a "must equal unknown()" check. A
    correctly answering `display_name` hook must not make `fail_closed` FAIL
    (it must not even be considered -- SKIP when it is the only hook
    configured)."""
    ctx = make_ctx(
        hooks_describe=_hooks_describe(config=["OWNERSHIP_DISPLAY_NAME"]),
        get_hook=lambda name: lambda user: "Last, First",
    )
    result = pv.FailClosed().run(ctx)
    assert result.status == "SKIP", result.detail


def test_fail_closed_accepts_a_plain_empty_page_for_a_degraded_unknown_spec():
    """`OWNERSHIP_USERS_OF_TENANT`/`OWNERSHIP_GROUPS_OF_TENANT`'s `unknown()`
    carries `"degraded": True` -- a signal `plugin_hooks.call` itself
    attaches only when the hook RAISES, never something a correctly
    answering hook (an ordinary "no such tenant", no exception) is expected
    to set. A plain empty page must PASS."""
    ctx = make_ctx(
        hooks_describe=_hooks_describe(config=["OWNERSHIP_USERS_OF_TENANT"]),
        get_hook=lambda name: lambda tenant, query, limit, cursor: _page([]),
        tenant="real-tenant-guid",
    )
    result = pv.FailClosed().run(ctx)
    assert result.status == "PASS", result.detail


def test_caller_side_accepts_the_local_id_placeholder_for_member_guid():
    """H-2c: the contract's own default (`DefaultIdentity.member_guid`)
    answers `local-<id>` for a non-GUID account (§4.5.2 row 1) -- a correct
    reference hook that falls back to the same computation must PASS, not
    FAIL, `caller_side`."""
    ctx = make_ctx(
        hooks_describe=_hooks_describe(config=["OWNERSHIP_MEMBER_GUID"]),
        get_hook=lambda name: lambda u: "local-7",
        jit_users=[object()],
    )
    result = pv.CallerSide().run(ctx)
    assert result.status == "PASS", result.detail


# ---------------------------------------------------------------------------
# hooks -- harness-driven (spec §4.5, real registry)
# ---------------------------------------------------------------------------


def test_hooks_context_wiring_against_the_real_registry(harness):
    """`_build_context`'s new `hooks` fields read the SAME real `Registry`
    `plugins.get_hook`/`plugins.describe()` already serve -- configuring a
    hook the way `test_plugin_hooks.py`'s harness section does (direct
    `reg.hooks.callables`/`.sources` assignment) is visible to `HooksResolved`
    end to end, not just to a hand-built `Context`."""
    from flask import current_app
    from superset_ownership import plugins

    with harness.ctx():
        reg = current_app.extensions[plugins.EXT_KEY]
        reg.hooks.callables["OWNERSHIP_DISPLAY_NAME"] = lambda u: "Wired Name"
        reg.hooks.sources["OWNERSHIP_DISPLAY_NAME"] = "<callable>"
        try:
            ctx = pv._build_context(
                tenant=None,
                sample_users=0,
                object_ref=None,
                as_user=None,
                latency_ms=300,
                force_walk=False,
            )
            assert ctx.hooks_describe["OWNERSHIP_DISPLAY_NAME"]["source"] == "config"
            assert ctx.get_hook("OWNERSHIP_DISPLAY_NAME")(None) == "Wired Name"
            result = pv.HooksResolved().run(ctx)
        finally:
            reg.hooks.callables.pop("OWNERSHIP_DISPLAY_NAME", None)
            reg.hooks.sources.pop("OWNERSHIP_DISPLAY_NAME", None)

    assert result.status == "PASS", result.detail
    assert "OWNERSHIP_DISPLAY_NAME=config" in result.detail


def test_read_only_fails_for_a_hook_that_writes_through_the_real_registry(harness):
    """The SAME `plugins.counting()` mechanism, exercised against the real
    app `Registry` (`reg.authorizer` swapped for the duration) rather than
    the no-app fallback cache the pure test above covers."""
    from flask import current_app
    from superset_ownership import plugins

    def writer(group_id):
        plugins.get_authorizer().write_tuple(
            "user:verify-hooks-ro2", "member", "group:verify-hooks-ro2"
        )
        return True

    with harness.ctx():
        reg = current_app.extensions[plugins.EXT_KEY]
        reg.hooks.callables["OWNERSHIP_GROUP_EXISTS"] = writer
        reg.hooks.sources["OWNERSHIP_GROUP_EXISTS"] = "<callable>"
        try:
            ctx = pv._build_context(
                tenant=None,
                sample_users=0,
                object_ref=None,
                as_user=None,
                latency_ms=300,
                force_walk=False,
            )
            result = pv.ReadOnly().run(ctx)
        finally:
            reg.hooks.callables.pop("OWNERSHIP_GROUP_EXISTS", None)
            reg.hooks.sources.pop("OWNERSHIP_GROUP_EXISTS", None)

    assert result.status == "FAIL"
    assert "write_tuple" in result.detail


def test_can_manage_narrow_only_widening_fail_against_the_real_registry(harness):
    """§5's non-overridable invariant, from `hooks/can_manage_narrow_only`'s
    own side: an outsider with no ground (`_default_manage_reason` computes
    None) still gets caught when a configured `OWNERSHIP_CAN_MANAGE` hook
    tries to grant `"owner"` anyway -- the same widening
    `test_plugin_hooks.py`'s
    `test_can_manage_hook_widening_is_ignored_and_logged_on_the_share_route`
    proves is ignored on the real share route, verified here from the
    conformance kit's own vantage point instead."""
    from flask import current_app
    from superset_ownership import plugins

    tenant = "c0ffee00-0000-4000-8000-0000000000c1"
    role = f"tenant_{tenant}"
    owner = harness.add_person(
        "hookcmvowner", "HookCmVOwner", "Gamma", "sales_readers", role
    )
    outsider = harness.add_person("hookcmvoutsider", "HookCmVOutsider", "Gamma", role)
    chart = harness.create_chart(owner, "hook-can-manage-verify-widen")

    with harness.ctx():
        reg = current_app.extensions[plugins.EXT_KEY]
        reg.hooks.callables["OWNERSHIP_CAN_MANAGE"] = lambda user, state: "owner"
        try:
            ctx = pv._build_context(
                tenant=None,
                sample_users=0,
                object_ref=f"chart:{chart.id}",
                as_user=outsider.username,
                latency_ms=300,
                force_walk=False,
            )
            result = pv.CanManageNarrowOnly().run(ctx)
        finally:
            reg.hooks.callables.pop("OWNERSHIP_CAN_MANAGE", None)

    assert result.status == "FAIL"
    assert "widened" in result.detail


# ---------------------------------------------------------------------------
# Report / orchestration
# ---------------------------------------------------------------------------


def test_report_json_shape():
    report = pv.Report()
    report.add(pv.Result("load", "plugins_load", "PASS", ""))
    report.add(pv.Result("connection", "reachable", "FAIL", "down"))
    report.exit_code = pv.EXIT_FAIL
    payload = report.to_json()
    assert payload["results"] == [
        {"section": "load", "name": "plugins_load", "status": "PASS", "detail": ""},
        {
            "section": "connection",
            "name": "reachable",
            "status": "FAIL",
            "detail": "down",
        },
    ]
    assert payload["summary"] == {"PASS": 1, "WARN": 0, "FAIL": 1, "SKIP": 0}
    assert payload["exit_code"] == pv.EXIT_FAIL


def test_report_render_text_has_a_summary_line():
    report = pv.Report()
    report.add(pv.Result("load", "plugins_load", "PASS", ""))
    text = report.render_text()
    assert "== load ==" in text
    assert "SUMMARY pass=1 warn=0 fail=0 skip=0" in text


def test_run_against_green_end_to_end():
    """A fully wired, all-fakes Context that should pass or warn everywhere,
    the way `--tenant ... --object ... --as ...` is expected to on a
    correctly configured deployment (spec §13(e)1)."""
    directory = FakeDirectory(
        groups_by_tenant={"t1": [{"id": "group:admin_t1", "members": 0}]},
        users_by_tenant={"t1": [{"guid": "u1", "superset_id": 1}]},
    )
    authorizer = FakeAuthorizer()
    identity = FakeIdentity(
        guids={"u1": "3f0a91c7-2d84-4e63-9b15-7c4e8a2f6d31"}, tenants={"u1": "t1"}
    )
    authorizer.tuples.add(
        ("user:3f0a91c7-2d84-4e63-9b15-7c4e8a2f6d31", "member", "tenant:t1")
    )

    class Conn:
        model_id = "01H..."

    ctx = make_ctx(
        tenant="t1",
        object_ref="chart:1",
        as_user="ben",
        describe={"degraded": []},
        connection=Conn(),
        fga_reachable=lambda: True,
        required_relations={},
        directory=directory,
        authorizer=authorizer,
        identity=identity,
        candidate_tenants=["t1"],
        jit_users=["u1"],
        tenant_administrator_group=lambda t: f"group:admin_{t}",
        group_id=_default_group_id,
        split_group_id=_default_split_group_id,
        list_tenant_group_ids=lambda t: ["group:admin_t1"],
        member_ref=lambda u: "ref-u1",
        normalize_subject=lambda s: "ref-u1",
        invariant_probe=lambda kind: () if kind != "non_owner" else ("check",),
    )
    report = pv.run_against(ctx)
    assert report.exit_code == pv.EXIT_OK
    assert report.counts["FAIL"] == 0


def test_run_against_load_failure_skips_everything_and_exits_2():
    ctx = make_ctx(
        describe={"degraded": ["directory"], "failures": {"directory": "boom"}}
    )
    report = pv.run_against(ctx)
    assert report.exit_code == pv.EXIT_CANNOT_CONTINUE
    by_name = {r.name: r for r in report.results}
    assert by_name["plugins_load"].status == "FAIL"
    assert by_name["reachable"].status == "SKIP"
    assert by_name["owner_zero_calls"].status == "SKIP"
    assert len(report.results) == len(pv.CHECKS)


def test_run_against_unreachable_skips_the_rest_and_exits_2():
    ctx = make_ctx(
        describe={"degraded": []}, connection=object(), fga_reachable=lambda: False
    )
    report = pv.run_against(ctx)
    assert report.exit_code == pv.EXIT_CANNOT_CONTINUE
    by_name = {r.name: r for r in report.results}
    assert by_name["plugins_load"].status == "PASS"
    assert by_name["reachable"].status == "FAIL"
    assert by_name["model_pinned"].status == "SKIP"
    assert by_name["owner_zero_calls"].status == "SKIP"


def test_run_against_any_fail_exits_1():
    ctx = make_ctx(
        describe={"degraded": []},
        connection=None,
        invariant_probe=lambda kind: ("check", "check"),
    )
    report = pv.run_against(ctx)
    assert report.exit_code == pv.EXIT_FAIL
    assert report.counts["FAIL"] >= 1


def test_run_against_survives_a_raising_check(monkeypatch):
    """H-2's per-check guard, actually exercised (PR90 review N-6): a
    mutant that removes `_run_one`'s try/except survives the rest of the
    suite untouched, because nothing in it makes a check raise from inside
    `run_against` -- this test adds one that always does, and checks the
    guard turns that into a FAIL row instead of propagating."""

    class Boom(pv.Check):
        section = "vocabulary"
        name = "boom_check"

        def run(self, ctx):
            raise RuntimeError("kaboom")

    monkeypatch.setattr(pv, "CHECKS", (*pv.CHECKS, Boom()))
    ctx = make_ctx(describe={"degraded": []}, connection=None)
    report = pv.run_against(ctx)
    boom_result = next(r for r in report.results if r.name == "boom_check")
    assert boom_result.status == "FAIL"
    assert "kaboom" in boom_result.detail
    # And the rest of the report still completed -- the point of the guard.
    assert len(report.results) == len(pv.CHECKS)


def test_run_requires_a_context_or_an_app():
    """`run()` with no ctx lazily imports the sibling slices and needs a
    Flask application context to read the loaded registry -- calling it
    bare raises rather than silently doing nothing. Proves the laziness:
    importing `plugin_verify` itself never raised (see module import at the
    top of this file); only calling `run()` without a Context does. The
    exact exception differs by tier: the pure job has no `superset` package
    at all (ImportError); the container job has `superset` importable but
    no application context around this call (AttributeError/RuntimeError
    resolving `security_manager` outside one) -- both are "did not silently
    proceed", which is what this pins."""
    with pytest.raises((ImportError, AttributeError, RuntimeError)):
        pv.run(tenant="t1")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def test_fixtures_tenant_tuple_orientation_matches_the_shipped_model():
    """model/ownership.fga: `type group ... define tenant: [tenant#member]`
    -- the SUBJECT of a `tenant` tuple is the tenant's member set, the
    OBJECT is the group. An earlier version of this fixture had it
    backwards (`{user: group, object: tenant#member}`), which
    `OpenFGADirectory`/the shipped model reject outright (PR90 review B-2);
    this pins the correct reading so a regression fails here, in a unit
    test, rather than as a live `400 validation_error`."""
    rows = pv.fixtures.tuples()
    tenant_rows = [r for r in rows if r["relation"] == "tenant"]
    assert tenant_rows, "the clean fixture set must carry at least one tenant tuple"
    for r in tenant_rows:
        assert r["user"].startswith("tenant:"), r
        assert r["user"].endswith("#member"), r
        assert r["object"].startswith("group:"), r


def test_fixtures_clean_set_has_a_tenant_tuple_per_group():
    rows = pv.fixtures.tuples()
    group_ids = {
        r["object"]
        for r in rows
        if r["relation"] == "member" and r["object"].startswith("group:")
    }
    tenant_tuples = {r["object"] for r in rows if r["relation"] == "tenant"}
    for gid in group_ids:
        assert gid in tenant_tuples, (
            f"{gid} has no tenant tuple in the clean fixture set"
        )


def test_fixtures_broken_group_without_tenant_tuple():
    clean = pv.fixtures.tuples()
    broken = pv.fixtures.tuples(broken="group_without_tenant_tuple")
    assert len(broken) == len(clean) - 1
    dd_a = pv.fixtures.group_id("dashboard_designer", pv.fixtures.TENANT_A)
    assert not any(r["relation"] == "tenant" and r["object"] == dd_a for r in broken)


def test_fixtures_broken_cross_tenant_member():
    broken = pv.fixtures.tuples(broken="cross_tenant_member")
    ada = f"user:{pv.fixtures.ADA}"
    tenants = {
        r["object"] for r in broken if r["user"] == ada and r["relation"] == "member"
    }
    assert f"tenant:{pv.fixtures.TENANT_A}" in tenants
    assert f"tenant:{pv.fixtures.TENANT_B}" in tenants


def test_fixtures_broken_wrong_format_id():
    clean_ids = {r["object"] for r in pv.fixtures.tuples()}
    broken_rows = pv.fixtures.tuples(broken="wrong_format_id")
    broken_ids = {r["object"] for r in broken_rows}
    assert broken_ids - clean_ids, "the wrong-format variant adds a new group id"


def test_fixtures_rejects_an_unknown_broken_variant():
    with pytest.raises(ValueError, match="unknown --broken variant"):
        pv.fixtures.tuples(broken="not-a-real-variant")


def test_fixtures_seed_writes_through_the_given_conn():
    authorizer = FakeAuthorizer()
    rows = pv.fixtures.seed(authorizer)
    assert authorizer.write_calls == len(rows)


# ---------------------------------------------------------------------------
# Deferred-to-integration marker
# ---------------------------------------------------------------------------


def test_plugin_verify_end_to_end_against_the_real_registry(harness, monkeypatch):
    """`_build_context` wires a real Context off the harness's loaded
    registry (the `counting` authorizer/directory -- the same seam
    `test_access_model.py`/`test_endpoints.py` drive the access-model suite
    through) and `run()` completes end-to-end without a Context passed in.
    This is a wiring smoke test, not a behavioural one: `counting` has no
    real OpenFGA store behind it, so the connection section is made to fail
    at `reachable` (an explicit dead address -- never the ambient
    `OWNERSHIP_FGA_API_URL`, which in this container points at a real,
    reachable store and would make this test's outcome depend on what else
    is running) and short-circuit everything after `connection` to SKIP --
    that specific, deterministic shape is asserted below, not a tautological
    "some exit code came back" (PR90 review H-5: the exit code the un-fixed
    version of this test accepted was `in (0, 1, 2)`, i.e. every possible
    value). The full run against a real, reachable OpenFGA store,
    `--tenant`/`--object`/`--as` included, is exercised by the live proof in
    `qa/reviews/` and the manual round
    (`qa/design/directory-hook/04-manual-test-round.md`), not by a pure/
    container unit test that must pass with no network at all."""
    monkeypatch.setenv("OWNERSHIP_FGA_API_URL", "http://host.docker.internal:1")
    # test_endpoints.py's deployed-config tests run the REAL
    # FLASK_APP_MUTATOR against `harness.app` earlier in this session
    # (`deployed.FLASK_APP_MUTATOR(_MutatorProbe(harness.app))`), which
    # calls `plugins.load()` for real and leaves a `Registry` -- with a
    # real, already-built connection -- cached on
    # `harness.app.extensions["ownership"]`. `get_fga_connection()` prefers
    # that cached registry over rebuilding from the environment, so the env
    # override above would silently do nothing if that registry were still
    # there when this test happens to run after one of those. Clear it for
    # the duration of this test; restore whatever was there (nothing, in
    # every ordering this suite actually uses) afterward.
    from superset_ownership.plugins import EXT_KEY

    previous_registry = harness.app.extensions.pop(EXT_KEY, None)
    try:
        with harness.app.app_context():
            report = pv.run(sample_users=1)
    finally:
        if previous_registry is not None:
            harness.app.extensions[EXT_KEY] = previous_registry

    by_name = {r.name: r for r in report.results}
    assert by_name["plugins_load"].status == "PASS", by_name["plugins_load"].detail
    assert by_name["reachable"].status == "FAIL", by_name["reachable"].detail
    # "cannot continue" ordering (spec §10): everything after `connection`'s
    # `reachable` check SKIPs, named with the reason, and nothing further ran.
    for r in report.results:
        if r.name in ("plugins_load", "reachable"):
            continue
        assert r.status == "SKIP", (r.name, r.status, r.detail)
        assert r.detail == "connection unreachable", (r.name, r.detail)
    assert len(report.results) == len(pv.CHECKS)
    assert report.exit_code == pv.EXIT_CANNOT_CONTINUE


# ---------------------------------------------------------------------------
# Harness-driven: invariants (spec §11 "invariants section reproduces
# test_endpoints counts through plugins.counting()")
# ---------------------------------------------------------------------------


def test_invariant_probe_reproduces_the_access_model_counts_on_the_harness(harness):
    """Drives `_make_invariant_probe` -- the exact function `verify`'s
    invariants section calls -- through the SAME world-builder and counting
    mechanism `test_access_model.py` uses (the `harness` fixture,
    `plugins.counting()`), not a re-implementation: owner sees 0 store
    calls, a non-owner's denial of a PRIVATE object costs zero store calls
    too (issue #93's hard deny, and this chart has no editor share for
    M-3's lazy `object_tenant` read -- review round 2, PR #100 -- to reach
    either), and a public object costs 0 -- the same invariants
    `test_access_model.py`'s OWNER case and `test_endpoints.py:237-292`'s
    private block assert (PR90 review B-3)."""
    w = harness.world
    chart = harness.create_chart(w.ada, "verify-invariants-owner")
    public_chart = harness.create_chart(w.ada, "verify-invariants-public")
    harness.set_visibility(w.ada, public_chart, "public")

    with harness.ctx():
        owner_probe = pv._make_invariant_probe(f"chart:{chart.id}", w.ada.username)
        assert owner_probe("owner") == (), "owner: zero store calls"
        assert owner_probe("public") is None, "chart is private, not public"

        non_owner_probe = pv._make_invariant_probe(f"chart:{chart.id}", w.ben.username)
        calls = non_owner_probe("non_owner")
        assert calls == (), (
            "private: the hard deny asks the store nothing; no editor share "
            "here for the resolver's object_tenant read to reach either (M-3)"
        )

        public_probe = pv._make_invariant_probe(
            f"chart:{public_chart.id}", w.ben.username
        )
        assert public_probe("public") == (), "public: zero store calls"


def test_invariant_probe_fails_when_the_directory_touches_the_access_path(
    harness, monkeypatch
):
    """The regression guard has teeth: patch a directory call onto the
    access path (exactly the shape `owner_zero_calls`/`non_owner_one_check`
    exist to catch) and the probe's own call log picks it up, driving
    `OwnerZeroCalls` to FAIL -- not just proving the check can PASS on an
    already-correct tree."""
    from superset import security_manager
    from superset_ownership import plugins

    w = harness.world
    chart = harness.create_chart(w.ada, "verify-invariants-mutated")
    original_raise_for_access = security_manager.raise_for_access

    def mutated(*args, **kwargs):
        # Exactly the regression `owner_zero_calls` exists to catch: a
        # directory read reached onto the access-decision path.
        plugins.get_directory().group_exists("regression-probe")
        return original_raise_for_access(*args, **kwargs)

    monkeypatch.setattr(security_manager, "raise_for_access", mutated)

    with harness.ctx():
        probe = pv._make_invariant_probe(f"chart:{chart.id}", w.ada.username)
        calls = probe("owner")

    assert "group_exists" in calls, (
        f"the mutated directory call must show up in the probe's own log: {calls}"
    )
    result = pv.OwnerZeroCalls().run(pv.Context(invariant_probe=lambda kind: calls))
    assert result.status == "FAIL"


def test_discover_candidate_tenants_only_collects_structural_tenant_roles(harness):
    """PR90 review N-6: a mutant that re-inverts `_discover_candidate_tenants`'s
    `is_structural_tenant_role` filter survives the rest of the suite
    untouched -- nothing exercised the function directly against a real
    role set. B-4's own bug was exactly this: with the filter inverted,
    `tenant_isolation` compared FAB roles like `Admin`/`Public` instead of
    two real tenants."""
    from superset import security_manager

    tenant_guid = "c9a1b2d3-4e5f-4061-8a2b-3c4d5e6f70aa"
    with harness.ctx():
        security_manager.find_role("Admin") or security_manager.add_role("Admin")
        role_name = f"tenant_{tenant_guid}"
        security_manager.find_role(role_name) or security_manager.add_role(role_name)
        tenants = pv._discover_candidate_tenants()
    assert tenant_guid in tenants
    assert "Admin" not in tenants
    assert "Public" not in tenants
    assert "Gamma" not in tenants


def test_discover_jit_users_sample_zero_means_none(harness):
    """PR90 review N-3: `_discover_jit_users(tenant, 0)` must return `[]`,
    not exactly one user -- the loop's own `len(users) >= sample_users`
    break condition is already true after the FIRST append when
    `sample_users == 0`, so `--sample-users 0` (the documented way to quiet
    the identity section against a scratch store, README §6/M-7) sampled
    one user anyway and the identity checks PASSed vacuously on it instead
    of SKIPping."""
    with harness.ctx():
        assert pv._discover_jit_users(None, 0) == []
        assert pv._discover_jit_users(None, -1) == []


# ---------------------------------------------------------------------------
# Live: against a real OpenFGA store (B-1's own red path, closed)
# ---------------------------------------------------------------------------


def test_scratch_write_tuple_raises_on_a_rejected_write(monkeypatch):
    """PR90 review N-6: a mutant that makes `_scratch_write_tuple` swallow
    a rejected write survives the rest of the suite untouched -- the live
    test above always seeds a store WITH a model installed, so nothing it
    writes is ever actually rejected; this drives the rejection path
    directly against a mocked, non-200, not-already-satisfied response."""
    from unittest.mock import MagicMock

    from superset_ownership import fga
    from superset_ownership.fga_connection import FgaConnection

    resp = MagicMock(status_code=400, text="validation_error: bad tuple")
    monkeypatch.setattr(fga, "_request", lambda *a, **kw: resp)
    monkeypatch.setattr(fga, "_is_already_satisfied", lambda r: False)

    connection = FgaConnection(
        api_url="http://example.invalid", store_id="s1", model_id=None
    )
    writer = pv._ScratchConnectionWriter(connection)
    with pytest.raises(RuntimeError, match="rejected"):
        writer.write_tuple("user:a", "member", "group:b")


def test_seed_scratch_writes_the_clean_fixture_into_a_real_store():
    """`seed-scratch`'s own red path, closed (PR90 review B-1): it imports,
    builds its own connection bound to `--store` (never the registry's, per
    `_ScratchConnectionWriter`), and the clean fixture set -- with B-2's
    corrected `group.tenant` orientation -- is accepted by the shipped
    model with a real write.

    Opt-in only, via `OWNERSHIP_TEST_FGA_API_URL` (PR90 review N-8): this
    must NEVER fall back to `OWNERSHIP_FGA_API_URL` (the store the process
    is actually configured against, e.g. a shared or production instance)
    -- an earlier version of this test did, and created and deleted a
    store on `events-openfga-1` on every run of the container job as a
    side effect of nobody having set the opt-in var. Skips (does not touch
    any store) unless the opt-in var names one; creates and deletes its
    own scratch store there, never a pre-existing one."""
    import requests
    from superset_ownership import model as model_mod

    api_url = os.environ.get("OWNERSHIP_TEST_FGA_API_URL")
    if not api_url:
        pytest.skip(
            "OWNERSHIP_TEST_FGA_API_URL not set -- this test never falls "
            "back to the ambient OWNERSHIP_FGA_API_URL (PR90 review N-8)"
        )
    try:
        health = requests.get(f"{api_url}/healthz", timeout=1.0)
        assert health.status_code == 200
    except Exception as exc:  # noqa: BLE001 - "unreachable" is a skip, not a failure
        pytest.skip(f"no OpenFGA reachable at {api_url}: {exc}")

    created = requests.post(
        f"{api_url}/stores", json={"name": "pv-unit-test-scratch"}, timeout=5
    )
    assert created.status_code == 201, created.text
    store_id = created.json()["id"]
    try:
        model_resp = requests.post(
            f"{api_url}/stores/{store_id}/authorization-models",
            json=model_mod.MODEL,
            timeout=5,
        )
        assert model_resp.status_code == 201, model_resp.text

        rows = pv.fixtures.tuples()
        result = pv.fixtures.seed_scratch(api_url=api_url, store=store_id)
        assert result == {
            "store": store_id,
            "api_url": api_url,
            "broken": None,
            "written": len(rows),
        }

        # Prove the store actually holds the vocabulary's new tuple, not
        # merely "seed_scratch raised nothing": read one of the
        # `group.tenant` tuples back.
        tenant_row = next(r for r in rows if r["relation"] == "tenant")
        read = requests.post(
            f"{api_url}/stores/{store_id}/read",
            json={
                "tuple_key": {
                    "user": tenant_row["user"],
                    "relation": "tenant",
                    "object": tenant_row["object"],
                }
            },
            timeout=5,
        )
        assert read.status_code == 200, read.text
        assert read.json()["tuples"], "the group.tenant tuple was not written"
    finally:
        requests.delete(f"{api_url}/stores/{store_id}", timeout=5)


def test_seed_scratch_refuses_to_target_the_configured_store(monkeypatch):
    """B-1's safety net: `seed-scratch` must not silently write fixtures
    into whatever store the process happens to be configured against.
    `monkeypatch.setenv` (PR90 review N-8), not a raw `os.environ[...] =`
    that a bare `del` restores by removing rather than by putting back
    whatever `OWNERSHIP_FGA_STORE` a real environment already had set."""
    monkeypatch.setenv("OWNERSHIP_FGA_STORE", "store-under-test")
    with pytest.raises(ValueError, match="configured to use"):
        pv.fixtures.seed_scratch(
            api_url="http://example.invalid", store="store-under-test"
        )


# ---------------------------------------------------------------------------
# Snapshot: "empty plug-in block = today" (spec §12, PR90 review H-5)
# ---------------------------------------------------------------------------


class _EmptyBlockApp:
    """The bare minimum `plugins.load` needs: a `.config` mapping and an
    `.extensions` dict. Deliberately NOT a real Flask application -- the
    whole point of this snapshot is that "empty block" behaviour must not
    depend on Superset being importable, only on `superset_ownership`
    itself."""

    def __init__(self) -> None:
        self.config: dict = {}
        self.extensions: dict = {}


_EMPTY_BLOCK_BASELINE_PATH = os.path.join(
    os.path.dirname(__file__), "data", "empty_plugin_block_baseline.json"
)


def _snapshot_empty_block() -> dict:
    """`plugins.load` off a genuinely empty config, plus the real reader
    functions for the settings keys that matter under it (`local`/`local`/
    `default`, no OpenFGA in play) -- called through their actual reader
    functions, not a re-guessed `settings.get(..., default=...)` per key,
    so this snapshot can never silently drift from what the real call
    sites do. `OWNERSHIP_OUTBOX_CELERY`/`OWNERSHIP_REPAIR_ON_START` are read
    at the deployed-config layer (`superset_config_docker_light.py`), not
    by this package directly, so they are out of scope for a
    `superset_ownership`-only snapshot; `OWNERSHIP_PRIVATE_ERROR` has no
    reader anywhere (a hard-coded `hooks.py` constant) and is intentionally
    excluded for the same reason spec §12/UPDATING.md note it has none."""
    from superset_ownership import identity, plugins, settings

    app = _EmptyBlockApp()
    reg = plugins.load(app, strict=True)
    connection = None
    if reg.connection is not None:
        connection = {
            "api_url": reg.connection.api_url,
            "store_id": reg.connection.store_id,
            "model_id": reg.connection.model_id,
        }
    effective_settings = {
        # api.py:513 `manage_permission_role()`.
        "OWNERSHIP_MANAGE_PERMISSION": settings.get(
            "OWNERSHIP_MANAGE_PERMISSION", None, settings.as_str, layer={}
        ),
        # identity.py's own default substitution, not settings.get's.
        "OWNERSHIP_GROUP_ID_FORMAT": identity.group_id_format(),
        # outbox.py:183.
        "OWNERSHIP_OUTBOX_ENABLED": settings.get(
            "OWNERSHIP_OUTBOX_ENABLED", True, settings.as_legacy_bool, layer={}
        ),
        # db.py:158.
        "OWNERSHIP_AUTO_MIGRATE": settings.get(
            "OWNERSHIP_AUTO_MIGRATE", True, settings.as_legacy_bool, layer={}
        ),
        # service.py:323.
        "OWNERSHIP_LOOKUP_CACHE_TTL": settings.get(
            "OWNERSHIP_LOOKUP_CACHE_TTL", None, settings.as_str, layer={}
        ),
        # directory.py's OpenFGADirectory.__init__ -- read here directly
        # since the empty block resolves `local`, which never constructs one.
        "OWNERSHIP_DIRECTORY_GROUP_WALK": settings.get(
            "OWNERSHIP_DIRECTORY_GROUP_WALK", "auto", settings.as_str, layer={}
        ),
        "OWNERSHIP_DIRECTORY_GROUP_WALK_TTL": settings.get(
            "OWNERSHIP_DIRECTORY_GROUP_WALK_TTL", 300, settings.as_int, layer={}
        ),
    }
    return {
        "resolved": dict(reg.resolved),
        "failures": dict(reg.failures),
        "connection": connection,
        "effective_settings": effective_settings,
    }


def test_empty_plugin_block_snapshot(monkeypatch):
    """A FORWARD regression guard for "empty block reproduces the baseline"
    (spec §12, §13(a)), not evidence that today's behaviour matches the
    pre-PR tree: the committed baseline
    (`tests/data/empty_plugin_block_baseline.json`) was generated from
    THIS PR's own boot, so this test proves "empty block = this PR at
    commit time" going forward, from here on -- a real, intentional
    default change updates the baseline in the same commit; an
    unintentional one is exactly what this test exists to catch (PR90
    review H-5 asked for the snapshot; review round 2's N-5 is the scope
    note above, and the reason this uses stdlib `json`, not
    `superset.utils.json`, below -- the whole point of driving
    `plugins.load` against a bare stand-in object instead of a real Flask
    app is that this comparison must not need Superset importable
    either)."""
    for key in list(os.environ):
        if key.startswith("OWNERSHIP_"):
            monkeypatch.delenv(key, raising=False)

    snapshot = _snapshot_empty_block()

    import json  # noqa: TID251 - stdlib, deliberately: this test must not
    # need Superset importable (see the docstring above); superset.utils.json
    # would defeat the one thing this test is testing for.

    with open(_EMPTY_BLOCK_BASELINE_PATH, encoding="utf-8") as f:
        baseline = json.loads(f.read())
    assert snapshot == baseline, (
        "the empty-block boot no longer matches the committed baseline -- "
        "if this is an intentional default change, update "
        f"{_EMPTY_BLOCK_BASELINE_PATH} in the same commit"
    )


def _snapshot_openfga_block() -> dict:
    """I-1: the SAME shape `_snapshot_empty_block` produces, but for the
    deployed "empty block" the runbook's (a) actually means --
    `OWNERSHIP_AUTHORIZER=openfga` and nothing else set. The bare
    `local`/`local` snapshot above never resolves `OpenFGADirectory` and
    never builds a connection, so it cannot guard the picker path
    (`/subjects` -> `search_users`) I-1's page-size regression lived in,
    undetected by that snapshot, until a real store caught it."""
    from superset_ownership import plugins

    app = _EmptyBlockApp()
    app.config["OWNERSHIP_AUTHORIZER"] = "openfga"
    reg = plugins.load(app, strict=True)
    connection = None
    if reg.connection is not None:
        connection = {
            "api_url": reg.connection.api_url,
            "store_id": reg.connection.store_id,
            "model_id": reg.connection.model_id,
        }
    return {
        "resolved": dict(reg.resolved),
        "failures": dict(reg.failures),
        "connection": connection,
    }


def test_openfga_authorizer_block_resolves_openfga_directory_with_default_connection(
    monkeypatch,
):
    """I-1: `OWNERSHIP_AUTHORIZER=openfga` alone (no `OWNERSHIP_DIRECTORY`,
    no `OWNERSHIP_FGA_*`) resolves BOTH the authorizer and the directory to
    the OpenFGA classes -- the configuration the picker path actually runs
    under in production, which the bare `local`/`local` snapshot above
    never exercises. Recommended by the integrated review (`review-plugin-
    pr90-integrated.md` §2) as the missing forward guard for that gap; not
    a `/subjects`-route-level test itself (`test_api.py`'s
    `test_subjects_default_limit_returns_users_past_a_hundred_members`
    covers the route end to end against a paging fake store) -- this one
    guards the boot resolution and the connection defaults underneath it."""
    for key in list(os.environ):
        if key.startswith("OWNERSHIP_"):
            monkeypatch.delenv(key, raising=False)

    snapshot = _snapshot_openfga_block()

    assert snapshot["failures"] == {}
    assert snapshot["resolved"]["authorizer"].endswith(":OpenFGAAuthorizer")
    assert snapshot["resolved"]["directory"].endswith(":OpenFGADirectory")
    assert snapshot["connection"] == {
        "api_url": "http://openfga:8080",
        "store_id": "01M1TT1CJ9PWQVF6KWBEJNSKB8",
        "model_id": None,
    }

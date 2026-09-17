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
"""The authorization model (spec section 12, the tests Ivanti mandates).

Against a real Superset application with the ownership hooks wired and a
counting fake where OpenFGA would be -- see superset_harness.py for the seam
and why. Each test states the property, then proves it at every layer the
property has to hold at: Superset's own `raise_for_access` (the decision),
the list filter (what a user is shown), and the REST routes (what a client
gets back). Skipped where Superset is not importable; runs in the container.

  1. ADMIN          has access on every relation -- view, edit, manage
                    sharing -- on private, shared and public objects
  2. PRIVATE        is invisible to a non-owner who holds the dataset grant:
                    excluded from the list filter and the list route, 404 on
                    GET, denied by raise_for_access, 403 from /chart/data
  3. GROUP GRANT    a member of a shared group gains access, and the store's
                    `check` is what says so
  4. OWNER          implies every enforced relation: view (the bypass hook)
                    and edit (the editors resolver, so Superset's own
                    is_editor and a native PUT agree)
  5. LIST           every relation implies list membership: owner, viewer
                    share, editor share and group share all appear in the
                    list filter's id set and in GET /api/v1/<type>/
  6. TENANT         a tenant administrator manages an object in their own
                    tenant and cannot see one in another tenant

Every call-count assertion is on the FULL list of authorizer calls (all
methods, `Decision.calls`/`names`), not on `check` alone: `object_tenant`
and the directory reads are store round trips on the deployed backend too.
Where a method other than `check` is allowed, the assertion says which and
why.
"""

from __future__ import annotations

import pytest
from superset_harness import ALLOW, DENY, GROUP_SUBJECT, guid, SENTINEL

pytestmark = pytest.mark.superset


# --- 1. ADMIN ----------------------------------------------------------------


@pytest.mark.parametrize("visibility", ["private", "shared", "public"])
def test_admin_has_access_on_every_relation(harness, visibility):
    w = harness.world
    chart = harness.create_chart(w.ada, f"admin-{visibility}")
    dash = harness.create_dashboard(w.ada, chart, title=f"admin-{visibility}")
    for ref in (chart, dash):
        if visibility != "private":
            harness.set_visibility(w.ada, ref, visibility)
        if visibility == "shared":
            harness.share(w.ada, ref, w.ben.ref)
    assert harness.row(chart).visibility == visibility
    assert harness.row(dash).visibility == visibility

    for ref in (chart, dash):
        # view: the decision, the detail route, the list route
        assert harness.decide(w.admin, ref).verdict == ALLOW
        assert harness.detail(w.admin, ref).status_code == 200
        assert ref.id in harness.listed_ids(w.admin, ref.asset_type)
        # edit: Superset's own write path
        r = harness.rename(w.admin, ref, f"{visibility} (admin)")
        assert r.status_code == 200, r.get_json()
        # manage sharing: the ownership routes admit an admin who is not the
        # owner -- but H1 (review round 1, issue #93) refuses a share write
        # on a `private` object outright, before any caller-role check, so
        # a `private` object cannot prove this by actually writing a share;
        # `can_manage` below still proves the admin was admitted to manage.
        if visibility != "private":
            r = harness.post(
                w.admin,
                f"/api/v1/ownership/{ref.asset_type}/{ref.id}/shares",
                {"subject": w.dee.ref, "role": "viewer"},
            )
            assert r.status_code == 200, r.get_json()
        r = harness.get(w.admin, f"/api/v1/ownership/{ref.asset_type}/{ref.id}")
        assert r.status_code == 200
        assert r.get_json()["can_manage"] is True
    # and the data behind a chart
    assert harness.get(w.admin, f"/api/v1/chart/{chart.id}/data/").status_code == 200


# --- 2. PRIVATE --------------------------------------------------------------


def test_private_object_is_invisible_to_a_non_owner_with_dataset_access(harness):
    """Ben holds the dataset grant, which under stock Superset would open
    both objects. Under the ownership model a freshly created object is
    private to its creator, and Ben cannot see it anywhere."""
    w = harness.world
    chart = harness.create_chart(w.ada, "private")
    dash = harness.create_dashboard(w.ada, chart, title="private")
    assert harness.row(chart).visibility == "private"
    assert harness.viewers(chart) == [SENTINEL], "born denied"

    for ref in (chart, dash):
        # the decision
        assert harness.decide(w.ben, ref).verdict == DENY
        # the list filter, and the list route it feeds
        assert ref.id not in harness.visible_ids(w.ben, ref.asset_type)
        assert ref.id not in harness.listed_ids(w.ben, ref.asset_type)
        # the detail route (404: the base filter hides it, as Superset does for
        # anything a user may not list)
        assert harness.detail(w.ben, ref).status_code == 404

    # the data path: a private chart's tile returns the module's placeholder
    # error, never rows, even to someone who could query the dataset directly
    r = harness.get(w.ben, f"/api/v1/chart/{chart.id}/data/")
    assert r.status_code == 403
    (error,) = r.get_json()["errors"]
    assert error["error_type"] == "CHART_SECURITY_ACCESS_ERROR"
    assert error["extra"]["ownership"] == "private"
    assert error["extra"]["chart_id"] == chart.id
    assert error["extra"]["owners"], "the denial names who to ask"
    # the owner, for contrast, gets the rows
    r = harness.get(w.ada, f"/api/v1/chart/{chart.id}/data/")
    assert r.status_code == 200
    assert r.get_json()["result"][0]["data"] == [
        {"region": "north", "amount": 1},
        {"region": "south", "amount": 2},
    ]


# --- 3. GROUP GRANT ----------------------------------------------------------


def test_group_grant_confers_access_to_a_member_through_check(harness):
    """`group:eng#member` exists in the authorization store only (no Superset
    role). Sharing with it admits Dee, a member, and not Ben, who is not --
    and the store's `check` is the call that decides."""
    w = harness.world
    chart = harness.create_chart(w.ada, "group")
    dash = harness.create_dashboard(w.ada, chart, title="group")
    for ref in (chart, dash):
        assert harness.decide(w.dee, ref).verdict == DENY, "not before the share"
        # Issue #93: a `private` object is a hard deny for every non-owner,
        # regardless of any share on it -- the group grant has to confer
        # access on a `shared` object to prove the property this test is
        # named for, rather than exercising the (now separate) private
        # hard-deny.
        harness.set_visibility(w.ada, ref, "shared")
        harness.share(w.ada, ref, GROUP_SUBJECT)
        assert (GROUP_SUBJECT, "viewer", ref.obj) in harness.authorizer.tuples, (
            "delivered by the drain"
        )

        d = harness.decide(w.dee, ref)
        assert d.verdict == ALLOW
        assert d.calls == (("check", w.dee.ref, "viewer", ref.obj),), (
            "exactly one call: a check for the member's own reference; the group "
            "is expanded by the store, never by this module"
        )
        d = harness.decide(w.ben, ref)
        assert d.verdict == DENY, "not a member"
        assert d.names == ("check",), (
            "one check; the group share is viewer, not editor, so M-3's lazy "
            "object_tenant read (review round 2, PR #100) is never reached"
        )

        assert ref.id in harness.visible_ids(w.dee, ref.asset_type)
        assert harness.detail(w.dee, ref).status_code == 200
        assert harness.detail(w.ben, ref).status_code == 404
    assert harness.get(w.dee, f"/api/v1/chart/{chart.id}/data/").status_code == 200


# --- 4. OWNER ----------------------------------------------------------------


def test_owner_implies_every_enforced_relation(harness):
    """Ownership is recorded in this module's table, not in the object's own
    `owners`/`editors`; both of Superset's gates have to learn it from the
    hooks. View comes from the bypass hook, edit from the editors resolver."""
    w = harness.world
    chart = harness.create_chart(w.ada, "owner")
    dash = harness.create_dashboard(w.ada, chart, title="owner")
    ada_subject = harness.subject_id(w.ada)

    for ref in (chart, dash):
        # view: no store call of any kind
        d = harness.decide(w.ada, ref)
        assert (d.verdict, d.calls) == (ALLOW, ())
        assert harness.detail(w.ada, ref).status_code == 200
        # edit: the resolver Superset's is_editor consults names the owner...
        assert harness.editor_subject_ids(ref) == {ada_subject}
        # ...so a native write by the owner succeeds
        r = harness.rename(w.ada, ref, "renamed by the owner")
        assert r.status_code == 200, r.get_json()
        # ...and the object's own editors collection is NOT how (a stock
        # create records the creator there; the guard strips it to the owner)
        assert harness.editors(ref) in ([], [f"user:{w.ada.username}"])
        # manage sharing
        r = harness.get(w.ada, f"/api/v1/ownership/{ref.asset_type}/{ref.id}")
        assert r.status_code == 200
        assert r.get_json()["can_manage"] is True
        # Issue #93: `private` is a hard deny for every non-owner, viewer
        # share or not -- H1 (review round 1) extends that to the share
        # WRITE itself, refused outright on a still-private object. The
        # object has to be `shared` before the share, both so the write
        # itself succeeds and so testing "a VIEWER share is not edit"
        # later has the object reachable at all (`shared`, not `private`,
        # which would make Ben's later write 404 for having no access
        # whatsoever, not 403 for lacking edit specifically).
        harness.set_visibility(w.ada, ref, "shared")
        harness.share(w.ada, ref, w.ben.ref)
    assert harness.get(w.ada, f"/api/v1/chart/{chart.id}/data/").status_code == 200

    # A share confers what it says and nothing above it: a VIEWER share is not
    # edit, so Ben (viewer on both) is refused the same write.
    for ref in (chart, dash):
        assert ada_subject in harness.editor_subject_ids(ref)
        r = harness.rename(w.ben, ref, "renamed by a viewer")
        assert r.status_code == 403, r.get_json()


# --- 5. LIST -----------------------------------------------------------------


def test_every_relation_implies_list_membership(harness):
    """Owner, viewer share, editor share, group share: each is a relation the
    model enforces, and each puts the object in the holder's list. Nobody
    else's list gains it."""
    w = harness.world
    for asset_type in ("chart", "dashboard"):
        by_viewer = harness.create(asset_type, w.ada, f"list-viewer-{asset_type}")
        by_editor = harness.create(asset_type, w.ada, f"list-editor-{asset_type}")
        by_group = harness.create(asset_type, w.ada, f"list-group-{asset_type}")
        # H2 (review round 1, issue #93): a share on a still-`private` object
        # confers nothing, list membership included -- so each object has to
        # be made `shared` before the share can be expected to put it in
        # anyone's list. Left `private`, this test passed only because
        # `owned_or_shared_rows` did not test visibility beyond `public`.
        harness.set_visibility(w.ada, by_viewer, "shared")
        harness.set_visibility(w.ada, by_editor, "shared")
        harness.set_visibility(w.ada, by_group, "shared")
        harness.share(w.ada, by_viewer, w.ben.ref, "viewer")
        harness.share(w.ada, by_editor, w.ben.ref, "editor")
        harness.share(w.ada, by_group, GROUP_SUBJECT, "viewer")
        mine = {by_viewer.id, by_editor.id, by_group.id}

        # the filter (what EXTRA_ACCESS_QUERY_FILTERS adds to the query)
        assert mine <= harness.visible_ids(w.ada, asset_type), "owner"
        assert harness.visible_ids(w.ben, asset_type) & mine == {
            by_viewer.id,
            by_editor.id,
        }
        assert harness.visible_ids(w.dee, asset_type) & mine == {by_group.id}
        assert harness.visible_ids(w.cy, asset_type) & mine == set(), (
            "no share, no grant"
        )

        # the route, which ORs that set into Superset's own list query
        assert mine <= harness.listed_ids(w.ada, asset_type)
        assert harness.listed_ids(w.ben, asset_type) & mine == {
            by_viewer.id,
            by_editor.id,
        }
        assert harness.listed_ids(w.dee, asset_type) & mine == {by_group.id}
        assert harness.listed_ids(w.cy, asset_type) & mine == set()

        # the reverse lookup is one store call per subject per list, whoever
        # asks -- but only the FIRST time within OWNERSHIP_LOOKUP_CACHE_TTL
        # (issue #82's cache, `service.cached_list_objects`); Ben's list was
        # already read for this asset type above (`harness.visible_ids`,
        # `harness.listed_ids`), so this repeat is served from that cache
        # and costs nothing.
        harness.visible_ids(w.ben, asset_type)
        assert harness.authorizer.calls == [], (
            "issue #82: list_objects served from the TTL cache on a repeat "
            "request for the same (asset_type, subject), asked for `viewer` "
            "-- the store's model implies it from editor and owner -- only "
            "on the FIRST such request"
        )


# --- 6. TENANT ---------------------------------------------------------------


def test_tenant_administrator_manages_own_tenant_only(harness):
    """Tenancy is a `tenant_<guid>` role on the user and a `tenant` tuple on
    the object, recorded at creation from its creator's tenant; a tenant
    administrator is a member of the store's `tenant_administrator_<guid>`
    group. Tam administers tenant A: Tam manages Ana's object -- reads its
    ownership detail and may TRANSFER it, to a member or to Tam -- but the
    object's sharing is its owner's alone: a share or a visibility change by
    Tam is refused (403) until Tam has taken ownership, so a change of
    sharing can only ever have been made by the name on the owner line.
    Tam cannot see or manage Bo's object in tenant B. A tenant
    administrator READS every object of their own tenant (private, shared
    or unowned) so the object they may re-home is reachable -- from the
    row's mirrored tenant and one cached administrator check, never a
    store `check` -- and remains an ordinary non-owner, denied, on the
    other tenant's."""
    w = harness.world
    tenant_a, tenant_b = guid(0xA), guid(0xB)
    role_a, role_b = f"tenant_{tenant_a}", f"tenant_{tenant_b}"
    ana = harness.add_person(guid(0x30), "Ana", "Gamma", "sales_readers", role_a)
    amy = harness.add_person(guid(0x31), "Amy", "Gamma", "sales_readers", role_a)
    bo = harness.add_person(guid(0x32), "Bo", "Gamma", "sales_readers", role_b)
    tam = harness.add_person(guid(0x33), "Tam", "Gamma", "sales_readers", role_a)
    harness.authorizer.make_tenant_administrator(tam.ref, tenant_a)
    own = harness.create_chart(ana, "tenant-a")
    other = harness.create_chart(bo, "tenant-b")
    harness.drain()
    assert (f"tenant:{tenant_a}#member", "tenant", own.obj) in harness.authorizer.tuples
    assert (
        f"tenant:{tenant_b}#member",
        "tenant",
        other.obj,
    ) in harness.authorizer.tuples

    # in-tenant: manage (transfer), but not share
    r = harness.get(tam, f"/api/v1/ownership/chart/{own.id}")
    assert r.status_code == 200
    assert r.get_json()["can_manage"] is True
    assert r.get_json()["can_share"] is False, "transfer only: the drawer's cue"
    assert r.get_json()["manage_reason"] == "tenant_admin"
    assert harness.authorizer.names == (
        "user_in_group",
        "object_tenant",
        "list_grants",
    ), "the administrator check and the object's tenant, both from the store"
    r = harness.put(
        tam, f"/api/v1/ownership/chart/{own.id}/visibility", {"visibility": "shared"}
    )
    assert r.status_code == 403, r.get_json()
    assert "take ownership" in r.get_json()["message"]
    r = harness.post(
        tam, f"/api/v1/ownership/chart/{own.id}/shares", {"subject": amy.ref}
    )
    assert r.status_code == 403, r.get_json()
    assert "take ownership" in r.get_json()["message"]
    assert harness.row(own).visibility == "private", "nothing was written"
    assert harness.decide(amy, own).verdict == DENY

    # Tam takes ownership -- a transfer to Tam -- and the sharing is Tam's.
    r = harness.put(
        tam, f"/api/v1/ownership/chart/{own.id}/owner", {"subject": tam.ref}
    )
    assert r.status_code == 200, r.get_json()
    assert harness.row(own).owner_user_id == tam.id
    r = harness.get(tam, f"/api/v1/ownership/chart/{own.id}")
    assert (r.get_json()["manage_reason"], r.get_json()["can_share"]) == (
        "owner",
        True,
    )
    r = harness.post(
        tam, f"/api/v1/ownership/chart/{own.id}/shares", {"subject": w.ben.ref}
    )
    assert r.status_code == 400, "Ben belongs to no tenant: not shareable into tenant A"
    assert "not a member of your tenant" in r.get_json()["message"]
    # Issue #93: `own` is still `private` at this point (never transitioned),
    # under which a non-owner is denied unconditionally regardless of any
    # share -- transition it so the share Tam is about to make actually
    # confers the access this test asserts on Amy next.
    harness.set_visibility(tam, own, "shared")
    harness.share(tam, own, amy.ref)
    assert harness.decide(amy, own).verdict == ALLOW
    # Tam owns it now, so Tam reads it as its owner; and a transfer back to
    # Ana makes Tam a plain non-owner again (owner fast path gone).
    assert harness.decide(tam, own).verdict == ALLOW
    r = harness.put(
        tam, f"/api/v1/ownership/chart/{own.id}/owner", {"subject": ana.ref}
    )
    assert r.status_code == 200, r.get_json()
    assert harness.row(own).owner_user_id == ana.id
    harness.drain()  # the transfer's owner-tuple revocation, delivered
    d = harness.decide(tam, own)
    assert d.verdict == ALLOW, "Tam administers tenant A: reads Ana's object"
    assert "check" not in d.names, (
        "the administrator ground is decided from the row's tenant and the "
        "administrator check, never by asking the store about the object"
    )

    # cross-tenant: invisible and unmanageable
    assert harness.get(tam, f"/api/v1/ownership/chart/{other.id}").status_code == 404
    r = harness.post(
        tam, f"/api/v1/ownership/chart/{other.id}/shares", {"subject": amy.ref}
    )
    assert r.status_code == 403
    assert harness.decide(tam, other).verdict == DENY
    assert harness.detail(tam, other).status_code == 404
    assert other.id not in harness.listed_ids(tam, "chart")
    assert harness.listed_ids(bo, "chart") & {own.id, other.id} == {other.id}

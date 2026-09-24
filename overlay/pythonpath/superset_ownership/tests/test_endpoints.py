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
"""Endpoint and behaviour guarantees (spec section 12, second half).

Same seam as test_access_model.py: a real Superset application, the
ownership hooks wired, a counting fake for the authorization store. The
call counts here are what the code under test actually did, and they count
EVERY authorizer method: `object_tenant` is a store round trip on the
deployed backend as much as `check` is.

  6. OWNER FAST PATH    the access decision for an owner makes ZERO store
                        calls; the data routes make no `check`
  7. PUBLIC             a public object's decision makes ZERO store calls,
                        for everyone, and does not depend on the store at all
  8. NON-OWNER          private or shared, the decision costs EXACTLY ONE
                        `check`, for the caller's own reference and `viewer`;
                        a denial also costs one `object_tenant` read, made by
                        the editors resolver after the bypass hook refuses
  8b. REVOCATION        a revoked share stops granting the moment it is
                        queued, before the outbox drains and while the store
                        still holds the tuple
  8c. BULK DATA ROUTE   POST /api/v1/chart/data, what a dashboard tile sends,
                        is gated exactly like GET /api/v1/chart/<pk>/data/
  8d. DEACTIVATED OWNER existing shares stop conferring access when the
                        owner's account is deactivated
  9. STORE DOWN         private and shared fail closed -- for the contract
                        the real adapter keeps (False / []) and for a backend
                        that raises -- and public is unaffected
 10. SHARING FIELDS     a non-owner editor who strips or widens `viewers` or
                        `editors` through the stock PUT route has the change
                        undone in the same transaction (the flush guard) and
                        audited; a viewer cannot write at all. Chart and
                        dashboard, private and shared.
 11. TRANSFER           the recipient's baseline permission is re-checked on
                        the real route (the matrix is in test_transfer.py)
 12. DELETE             a hard delete queues purge_object; the drain removes
                        every tuple on the object from the store. Chart and
                        dashboard, private and shared.
 13. DATASET            a share never widens dataset access: no grant, no
                        access, whatever was shared, with zero store calls
 14. FLAG OFF           OWNERSHIP_ENABLED=false: the module is never imported
                        (bar flags.py, which reports the switches, registers
                        `superset ownership status|check` and wires nothing),
                        no hook is wired, no listener installed, no table
                        created, the UI flag is false, and stock access
                        behaviour holds -- asserted in a fresh interpreter,
                        on a fresh database and on a copy of a database the
                        feature governed (the documented lock-out when the
                        flag is flipped without lifecycle.disable()). The
                        boot log is pinned against the deployed file; an
                        operator's earlier config layer (its mutator, its
                        flag, its switch, a feature-flag hook) is honoured or
                        reported as ignored; a config without the package
                        still boots
 15. WIRING             the deployed config wires the same functions this
                        suite wires, and its FLASK_APP_MUTATOR installs the
                        same guard and data gate and makes the same report
 16. ONE SWITCH         FEATURE_FLAGS["OBJECT_OWNERSHIP"] is derived from
                        OWNERSHIP_ENABLED; the mutator logs both, WARNING
                        when a later layer overrode the flag or a runtime
                        hook moved it; `superset ownership status` and
                        `check` report both, the runtime value and the hook,
                        and `check` fails when any of them disagree
 17. USER HARD-DELETE   deleting a FAB user through the ORM leaves one
                        `owner_removed_by_user_delete` event per object they
                        owned (revision 0003's SET NULL leaves no other
                        trace); with the ownership tables absent (auto-
                        migrate off, or after a teardown) the delete still
                        goes through and the listener issues no statement
                        against them; the next backfill re-homes such an
                        object to its creator, while a DEACTIVATED owner's
                        object is left with its owner; a default owner that
                        names no user is ignored with a warning, never a raw
                        IntegrityError
 18. SHARE INSERT       an IntegrityError on the share insert that is neither
     DEFECT             the unique race nor the object foreign key is a
                        defect: the route answers 500 with a fixed sentence
                        (never the driver message, the statement or its
                        parameters), writes nothing, queues nothing, and logs
                        the driver text at ERROR

The REST layer's `list_objects` is recorded separately from the decision:
FAB applies the list base filter to a single-item GET as well, so an
owner's GET /api/v1/chart/<id> costs one reverse lookup that the decision
itself does not (issue #82). What "zero" means, per the SOW's section 6.2
criterion: zero on the DECISION, always (section 6, above); at most one
`list_objects` per subject per OWNERSHIP_LOOKUP_CACHE_TTL for the ROUTE,
never one per request -- see
test_owner_get_route_costs_one_list_objects_per_ttl_not_per_request,
test_share_then_immediate_list_shows_it_without_waiting_the_ttl and
test_degraded_store_is_never_cached_and_the_next_request_retries.
"""

from __future__ import annotations

# The standard library, not superset.utils.json: collected by the pure job,
# where Superset is absent.
import importlib.util
import json  # noqa: TID251
import logging
import os
import runpy
import shutil
import subprocess
import sys
import textwrap
import types
from typing import Any, Optional
from unittest.mock import patch

import pytest
from superset_harness import (
    ALLOW,
    DENY,
    ERROR,
    FLAG_OFF_SCRIPT,
    GROUP_SUBJECT,
    guid,
    SENTINEL,
)

pytestmark = pytest.mark.superset

ASSET_TYPES = ("chart", "dashboard")
GOVERNED = ("private", "shared")

ISSUE_80 = "https://github.com/amaannawab923/client-test/issues/80"
ISSUE_81 = "https://github.com/amaannawab923/client-test/issues/81"
ISSUE_82 = "https://github.com/amaannawab923/client-test/issues/82"
ISSUE_84 = "https://github.com/amaannawab923/client-test/issues/84"


def _data_error(response: Any) -> dict[str, Any]:
    (error,) = response.get_json()["errors"]
    return error


def _governed(harness: Any, asset_type: str, visibility: str, name: str) -> Any:
    """An object of `asset_type` owned by Ada at `visibility`."""
    ref = harness.create(
        asset_type, harness.world.ada, f"{name}-{asset_type}-{visibility}"
    )
    if visibility != "private":
        harness.set_visibility(harness.world.ada, ref, visibility)
    assert harness.row(ref).visibility == visibility
    return ref


# --- 6. OWNER FAST PATH ------------------------------------------------------


def test_owner_fast_path_makes_no_authorizer_calls(harness):
    from superset_ownership import outbox

    w = harness.world
    chart = harness.create_chart(w.ada, "fast-path")
    dash = harness.create_dashboard(w.ada, chart, title="fast-path")
    for ref in (chart, dash):
        d = harness.decide(w.ada, ref)
        assert d.verdict == ALLOW
        assert d.calls == (), "one cached row lookup, nothing asked of the store"
    # The data routes: the decision inside the before_request guard is the
    # same function and makes no store call. The only call an owner's route
    # may make is the list base filter's `list_objects` (issue #82), and only
    # where FAB applies that filter: the pk route and the bulk route's
    # `form_data` shape, not the bulk route's `queries[]` shape. #82's cache
    # is keyed (asset_type, subject) -- both calls below ask for Ada on
    # "chart" -- and shared across requests for OWNERSHIP_LOOKUP_CACHE_TTL,
    # so the SECOND one is free: one `list_objects` to warm the cache, not
    # one per request.
    #
    # PR #94 review, M-1: an owner-only request also has no SHARED
    # candidate for `pending_revocations_for` to subtract, so
    # `owned_or_shared_rows` never calls it here -- the list base filter's
    # cost is `list_objects` alone, never a revocation check on top.
    with patch.object(
        outbox, "pending_revocations_for", wraps=outbox.pending_revocations_for
    ) as spy:
        r = harness.get(w.ada, f"/api/v1/chart/{chart.id}/data/")
        assert r.status_code == 200
        assert harness.authorizer.names == ("list_objects",), ISSUE_82
        r = harness.post(w.ada, "/api/v1/chart/data", harness.query_context(chart.id))
        assert r.status_code == 200
        assert harness.authorizer.names == (), (
            f"{ISSUE_82}: same (asset_type, subject) as the request above, inside "
            "the TTL -- served from #82's cache, not a second list_objects"
        )
        body = harness.query_context(chart.id, shape="queries")
        r = harness.post(w.ada, "/api/v1/chart/data", body)
        assert r.status_code == 200
        assert harness.authorizer.names == ()
        assert spy.call_count == 0, (
            "M-1: owner-only, nothing a revocation check could remove"
        )


def test_owner_get_route_costs_one_list_objects_per_ttl_not_per_request(harness):
    """ISSUE_82 / SOW section 6.2. FAB applies the list base filter
    (ChartFilter/DashboardAccessFilter) to a single-item GET as well as to
    the list routes -- `EXTRA_ACCESS_QUERY_FILTERS` -> `hooks._query_filter`
    -> `service.owned_or_shared_rows` -- because the hook has only
    `user_id`, no object context, and cannot special-case "this is one
    object I already own". That filter's own cost is one
    `list_objects` (`service.cached_list_objects`); the access DECISION
    itself is free (section 6, above) and does not run it at all.

    What "zero" means, stated explicitly, is three separate claims and this
    test proves the middle one:

      * ZERO ON THE DECISION, always -- `test_owner_fast_path_makes_no_
        authorizer_calls`, above: `d.calls == ()`.
      * ONE PER SUBJECT PER TTL for the route -- THIS test: the first
        request in a TTL window costs exactly one `list_objects`; every
        request after it, inside `OWNERSHIP_LOOKUP_CACHE_TTL`, costs zero
        MORE `list_objects` calls.
      * ZERO NETWORK on a repeat -- same claim as the line above, stated the
        way the SOW's criterion states it: no store round trip, not "no
        Python call to something named list_objects".

    M-3 (review round 2, PR #100): the editors resolver's `object_tenant`
    read used to run UNCONDITIONALLY before checking for any editor `user:`
    share, so this owner-only object (no editor share at all) paid it on
    EVERY call regardless -- contradicting the PR body's own "0" column for
    a repeat request. `editor_share_user_ids` now reads it LAZILY, the
    first time the tenant symmetry check is actually needed (inside the
    loop, once a resolvable editor `user:` share is found), so an
    owner-only object costs zero `object_tenant` calls, on the first
    request and every repeat. `test_owner_get_route_pays_object_tenant_
    once_per_request_with_an_editor_share`, below, proves the read still
    happens -- once per request, never cached, never bounded by the TTL --
    for an object that actually HAS an editor share to check tenancy for.

    The same #82 cache also covers `/data/`, dashboard GET and `/charts`
    (they all resolve through the same `_query_filter`), and turns the list
    route's two calls per request (FAB applies the base filter to both its
    count query and its item query) into one per TTL, not two.
    """
    w = harness.world
    chart = harness.create_chart(w.ada, "fast-path-route")

    r = harness.get(w.ada, f"/api/v1/chart/{chart.id}")
    assert r.status_code == 200
    assert harness.authorizer.names == ("list_objects",), (
        f"{ISSUE_82}: the list base filter's own cost, not the decision's -- "
        "one reverse-index read to warm the cache; M-3: zero object_tenant "
        "calls -- no editor share on this object for the tenant symmetry "
        "check to run for"
    )

    r = harness.get(w.ada, f"/api/v1/chart/{chart.id}")
    assert r.status_code == 200
    assert harness.authorizer.names == (), (
        "repeated inside the TTL: list_objects served from #82's cache, "
        "and M-3's lazy object_tenant read is still never reached -- zero "
        "store calls, matching the PR body's repeat-request column exactly"
    )


@pytest.mark.parametrize("asset_type", ASSET_TYPES)
def test_owner_get_route_pays_object_tenant_once_per_request_with_an_editor_share(
    harness, asset_type
):
    """M-3's other half: an object that DOES carry a resolvable editor
    `user:` share still pays the editors resolver's `object_tenant` read,
    once per request -- this read is not issue #82's, has no per-subject
    reverse index, and is not bounded by `OWNERSHIP_LOOKUP_CACHE_TTL`, so
    it does not drop out on a repeat the way `list_objects` does."""
    w = harness.world
    ref = harness.create(asset_type, w.ada, f"editor-share-{asset_type}")
    harness.set_visibility(w.ada, ref, "shared")
    harness.share(w.ada, ref, w.ben.ref, role="editor")

    r = harness.detail(w.ada, ref)
    assert r.status_code == 200
    assert harness.authorizer.names == ("list_objects", "object_tenant"), (
        "M-3: the object_tenant read happens once an editor user: share is "
        "found, on top of #82's list_objects"
    )

    r = harness.detail(w.ada, ref)
    assert r.status_code == 200
    assert harness.authorizer.names == ("object_tenant",), (
        "repeated inside the TTL: list_objects served from #82's cache, "
        "object_tenant is a separate, uncached cost this fix does not touch"
    )


def test_share_then_immediate_list_shows_it_without_waiting_the_ttl(harness):
    """ISSUE_82's cache is invalidated on this process's OWN write, before
    the write itself takes effect (`outbox.write_tuple` / `delete_tuple` /
    `revoke_subject` -> `service.invalidate_list_objects`, for the subject
    and asset type the write concerns): a share made in one request must be
    visible to a list rendered in the very next one, never held back by the
    TTL that bounds a DIFFERENT process's write."""
    w = harness.world
    chart = harness.create_chart(w.ada, "share-then-list")
    harness.set_visibility(w.ada, chart, "shared")

    # Warm #82's cache for Ben with "nothing shared" -- the answer a stale
    # cache would keep serving for the rest of the TTL if the share below
    # did not invalidate it.
    assert chart.id not in harness.visible_ids(w.ben, "chart")

    harness.share(w.ada, chart, w.ben.ref)  # drains by default: the store has the tuple
    assert chart.id in harness.visible_ids(w.ben, "chart"), (
        "no TTL wait: the share invalidated Ben's cached list_objects answer"
    )


def test_degraded_store_is_never_cached_and_the_next_request_retries(harness):
    """ISSUE_82: `fga.list_objects` fails closed and silent -- an
    unreachable store answers `[]`, indistinguishable from a subject with
    nothing. Publishing that to the shared cache would hide every one of a
    subject's real shares for a full TTL after the store recovered, which a
    fresh per-request failure does not do. `Authorizer.reachable()` is what
    tells the two apart (`CountingAuthorizer.reachable` returns `up`
    directly and does not record itself -- it costs nothing against any
    call-count assertion in this suite, here or elsewhere)."""
    w = harness.world
    chart = harness.create_chart(w.ada, "degraded-not-cached")
    harness.set_visibility(w.ada, chart, "shared")
    harness.share(w.ada, chart, w.ben.ref)

    harness.authorizer.up = False
    assert chart.id not in harness.visible_ids(w.ben, "chart"), (
        "store down: fails closed, nothing shared (public objects the filter "
        "adds from the rows alone under tenant scope are not shares)"
    )
    harness.authorizer.up = True
    assert chart.id in harness.visible_ids(w.ben, "chart"), (
        "the store recovered and was asked again immediately -- the failed "
        "answer was never published to the TTL layer"
    )


def test_revoke_never_waits_on_the_list_objects_cache(harness):
    """The security-relevant direction: a revocation must never wait on
    #82's TTL, only on the outbox drain. `test_revocation_denies_before_
    the_outbox_drains` (issue #84, section 8b) already proves this end to
    end -- its `visible_ids` call runs immediately after `harness.revoke(...,
    drain=False)`, well inside any TTL, and the revoked object is already
    gone. What lets #82's cache exist at all without reopening that window
    is `owned_or_shared_rows`'s `pending_revocations_for` subtraction
    (PR #94 review, M-1): it runs FRESH on every call, whether or not the
    `shared_tuples` behind it came from #82's cache -- the cache wraps
    `shared_tuples` only, never that subtraction's result. This test pins
    the belt-and-braces alongside it: `outbox.revoke_subject` also
    invalidates #82's cache immediately (same call as a share), so even the
    very next read re-asks the store rather than serving a warm answer from
    before the revoke -- belt AND braces, either one closing the window on
    its own."""
    w = harness.world
    chart = harness.create_chart(w.ada, "revoke-vs-ttl")
    harness.set_visibility(w.ada, chart, "shared")
    harness.share(w.ada, chart, w.ben.ref)
    assert chart.id in harness.visible_ids(w.ben, "chart"), "warm #82's cache"

    harness.revoke(w.ada, chart, w.ben.ref, drain=False)
    assert (w.ben.ref, "viewer", chart.obj) in harness.authorizer.tuples, (
        "the store has not heard yet: list_objects would still name it"
    )
    assert chart.id not in harness.visible_ids(w.ben, "chart"), (
        ISSUE_84 + ": denied by the fresh pending-revocation subtraction, not by luck"
    )


# --- 6b. H-1 (PR #100 review round 2): EXTERNAL DRAIN, THE PRODUCTION TOPOLOGY
#
# Every delivery-time assertion elsewhere in this file drains with the
# `harness`'s default `drain()` -- IN this test process, IN this process's
# own memory -- the one topology production never has: `flask run` /
# `gunicorn -w 1` + Celery beat draining in a SEPARATE process, on a
# per-process cache backend (SimpleCache, `superset_harness.CACHE_CONFIG`).
# `harness.drain(external=True)` hides `service._shared_cache()` for the
# drain's own duration, so `outbox._apply`'s post-delivery invalidation
# cannot see -- let alone touch -- the #82 entry this process published
# before the write. Before the H-1 fix in `service.owned_or_shared_rows`,
# that left a revoked, group-revoked or transferred-away subject listed, and
# `GET /api/v1/chart/<pk>` (no `raise_for_access` gate of its own; the list
# base filter, `ChartFilter`, IS the gate for that route) answering 200, for
# up to a full `OWNERSHIP_LOOKUP_CACHE_TTL` after the drain delivered.


def test_external_drain_revoke_closes_the_list_and_the_get(harness):
    w = harness.world
    chart = harness.create_chart(w.ada, "external-drain-revoke")
    harness.set_visibility(w.ada, chart, "shared")
    harness.share(w.ada, chart, w.ben.ref)
    assert chart.id in harness.visible_ids(w.ben, "chart"), "warm #82's cache"
    assert harness.detail(w.ben, chart).status_code == 200

    harness.revoke(w.ada, chart, w.ben.ref, drain=False)
    assert chart.id not in harness.visible_ids(w.ben, "chart"), (
        ISSUE_84 + ": denied by the fresh subtraction before the drain, as always"
    )

    stats = harness.drain(external=True)
    assert stats["delivered"] >= 1
    assert (w.ben.ref, "viewer", chart.obj) not in harness.authorizer.tuples, (
        "delivered: the store no longer has the tuple"
    )

    # The proof: read AGAIN in the (unpatched) process that published the
    # warm #82 entry above. Before H-1 this is exactly where the bug
    # showed -- the outbox row is DONE, not undelivered, so the fresh
    # subtraction has nothing left to subtract, and a stale #82 entry the
    # externally-run drain's own invalidation never reached would still
    # name the chart.
    assert chart.id not in harness.visible_ids(w.ben, "chart"), (
        "H-1: not left listed after an externally-delivered revoke"
    )
    assert harness.detail(w.ben, chart).status_code == 404, (
        "H-1: GET must not answer 200 for a revoked, delivered share"
    )


def test_external_drain_group_revoke_closes_the_list_and_the_get(harness):
    w = harness.world
    chart = harness.create_chart(w.ada, "external-drain-group-revoke")
    harness.set_visibility(w.ada, chart, "shared")
    harness.share(w.ada, chart, GROUP_SUBJECT)
    assert chart.id in harness.visible_ids(w.dee, "chart"), (
        "Dee reaches it via the group"
    )
    assert harness.detail(w.dee, chart).status_code == 200

    harness.revoke(w.ada, chart, GROUP_SUBJECT, drain=False)
    assert chart.id not in harness.visible_ids(w.dee, "chart"), ISSUE_84

    stats = harness.drain(external=True)
    assert stats["delivered"] >= 1
    assert (GROUP_SUBJECT, "viewer", chart.obj) not in harness.authorizer.tuples

    assert chart.id not in harness.visible_ids(w.dee, "chart"), (
        "H-1: a group revoke, externally delivered, must not leave a member listed"
    )
    assert harness.detail(w.dee, chart).status_code == 404, (
        "H-1: GET must not answer 200 for a member of a revoked, delivered group share"
    )


def test_external_drain_transfer_away_closes_the_list_and_the_get(harness):
    """The previous owner's lingering `owner` tuple (an undelivered
    `delete`) is what the store still answers `list_objects` with -- the
    deployed model has `viewer` include `editor` include `owner` -- exactly
    the "shared candidate" `pending_revocations_for` exists to subtract."""
    w = harness.world
    chart = harness.create_chart(w.ada, "external-drain-transfer")
    harness.set_visibility(w.ada, chart, "shared")  # off the private hard-deny path

    r = harness.put(
        w.ada, f"/api/v1/ownership/chart/{chart.id}/owner", {"subject": w.ben.ref}
    )
    assert r.status_code == 200, r.get_json()
    assert chart.id not in harness.visible_ids(w.ada, "chart"), (
        "the fresh subtraction denies the previous owner before the drain"
    )

    stats = harness.drain(external=True)
    assert stats["delivered"] >= 1
    assert (w.ada.ref, "owner", chart.obj) not in harness.authorizer.tuples

    assert chart.id not in harness.visible_ids(w.ada, "chart"), (
        "H-1: the previous owner, transferred away and externally delivered, "
        "must not be left listed"
    )
    assert harness.detail(w.ada, chart).status_code == 404, (
        "H-1: the previous owner's GET must not answer 200 after an "
        "externally-delivered transfer"
    )


# --- 7. PUBLIC ---------------------------------------------------------------


def test_public_object_makes_no_authorizer_calls(harness):
    """Public costs no store call and does not depend on the store being up.

    Under OWNERSHIP_PUBLIC_SCOPE=tenant (the default) a public object is
    governed -- the sentinel stays armed and the read gate decides -- but
    it decides from the ownership row alone: an object outside every tenant
    (this world's people carry no tenant role, so the row has no tenant) is
    readable by callers who are themselves outside every tenant -- this
    world's -- holding the dataset grant, and the store is never consulted."""
    w = harness.world
    chart = harness.create_chart(w.ada, "public")
    dash = harness.create_dashboard(w.ada, chart, title="public")
    expected = (
        (w.ada, ALLOW),
        (w.ben, ALLOW),
        (w.dee, ALLOW),
        (w.cy, DENY),
        (w.admin, ALLOW),
    )
    for ref in (chart, dash):
        harness.set_visibility(w.ada, ref, "public")
        assert harness.viewers(ref) == [SENTINEL], (
            "public is governed under tenant scope: the sentinel stays armed"
        )
        assert harness.row(ref).tenant_guid is None, "no tenant role on the creator"
        for person, verdict in expected:
            d = harness.decide(person, ref)
            assert (d.verdict, d.calls) == (verdict, ()), (person.label, ref.asset_type)
        harness.authorizer.up = False
        for person, verdict in ((w.ben, ALLOW), (w.cy, DENY)):
            d = harness.decide(person, ref)
            assert (d.verdict, d.calls) == (verdict, ())
        harness.authorizer.up = True
    # The data route, Ben holding the grant and Cy not: stock Superset. The
    # only store call is the list base filter's (issue #82); no check.
    r = harness.get(w.ben, f"/api/v1/chart/{chart.id}/data/")
    assert r.status_code == 200
    assert set(harness.authorizer.names) <= {"list_objects"}, ISSUE_82
    r = harness.get(w.cy, f"/api/v1/chart/{chart.id}/data/")
    assert r.status_code == 404, "stock: the base filter hides a chart Cy may not query"
    assert set(harness.authorizer.names) <= {"list_objects"}, ISSUE_82
    assert "ownership" not in json.dumps(r.get_json()), "not the ownership placeholder"


def test_public_under_instance_scope_is_stock_again(harness, monkeypatch):
    """OWNERSHIP_PUBLIC_SCOPE=instance: the pre-0004 behaviour -- public
    hands the object back to Superset's rules, the sentinel is removed.
    (The config layer wins over the environment, so it is set there.)"""
    monkeypatch.setitem(harness.app.config, "OWNERSHIP_PUBLIC_SCOPE", "instance")
    w = harness.world
    chart = harness.create_chart(w.ada, "public-instance")
    harness.set_visibility(w.ada, chart, "public")
    assert harness.viewers(chart) == [], "instance scope: no sentinel on public"
    for person, verdict in ((w.ben, ALLOW), (w.cy, DENY), (w.admin, ALLOW)):
        d = harness.decide(person, chart)
        assert (d.verdict, d.calls) == (verdict, ()), person.label
    # Leave the shared world consistent for the tenant-scoped tests that
    # follow: a public row with no sentinel is exactly what `check` calls
    # silently_open under tenant scope.
    harness.set_visibility(w.ada, chart, "private")


def _two_tenant_world(harness, base: int):
    """Ana and Amy in tenant A, Bo in tenant B, Nat in no tenant -- all with
    the dataset grant -- and one public chart of Ana's, stamped with tenant A
    at creation from her `tenant_<guid>` role. `base` keeps the people of
    each test distinct in the session-scoped world."""
    tenant_a, tenant_b = guid(0xA), guid(0xB)
    role_a, role_b = f"tenant_{tenant_a}", f"tenant_{tenant_b}"
    # Labels carry `base` too: the harness derives the e-mail from the label.
    ana = harness.add_person(guid(base), f"Ana{base}", "Gamma", "sales_readers", role_a)
    amy = harness.add_person(
        guid(base + 1), f"Amy{base}", "Gamma", "sales_readers", role_a
    )
    bo = harness.add_person(
        guid(base + 2), f"Bo{base}", "Gamma", "sales_readers", role_b
    )
    nat = harness.add_person(guid(base + 3), f"Nat{base}", "Gamma", "sales_readers")
    chart = harness.create_chart(ana, f"tenant-public-{base}")
    harness.set_visibility(ana, chart, "public")
    return tenant_a, ana, amy, bo, nat, chart


def test_public_is_within_the_tenant(harness):
    """The SOW rule: a public object of tenant A is visible to tenant A --
    its owner, another member, the admin -- and to nobody in tenant B or
    outside every tenant, dataset grant or not. Decided from the row: zero
    store calls for every verdict, so it holds through a store outage."""
    tenant_a, ana, amy, bo, nat, chart = _two_tenant_world(harness, 0xC0)
    w = harness.world
    assert harness.row(chart).tenant_guid == tenant_a, "stamped at creation"
    assert harness.viewers(chart) == [SENTINEL]
    for person, verdict in (
        (ana, ALLOW),
        (amy, ALLOW),
        (w.admin, ALLOW),
        (bo, DENY),
        (nat, DENY),
    ):
        d = harness.decide(person, chart)
        assert (d.verdict, d.calls) == (verdict, ()), person.label
    harness.authorizer.up = False
    for person, verdict in ((amy, ALLOW), (bo, DENY)):
        d = harness.decide(person, chart)
        assert (d.verdict, d.calls) == (verdict, ()), person.label
    harness.authorizer.up = True

    # Superset's own routes agree with the gate: list, detail, data.
    assert chart.id in harness.listed_ids(amy, "chart")
    assert chart.id not in harness.listed_ids(bo, "chart")
    assert chart.id not in harness.listed_ids(nat, "chart")
    assert harness.detail(amy, chart).status_code == 200
    assert harness.detail(bo, chart).status_code == 404
    assert harness.get(amy, f"/api/v1/chart/{chart.id}/data/").status_code == 200
    r = harness.get(bo, f"/api/v1/chart/{chart.id}/data/")
    assert r.status_code in (403, 404), r.status_code

    # And the ownership metadata routes: the other tenant does not learn
    # the object's owner or visibility either.
    assert harness.get(amy, f"/api/v1/ownership/chart/{chart.id}").status_code == 200
    assert harness.get(bo, f"/api/v1/ownership/chart/{chart.id}").status_code == 404
    listed = harness.get(bo, "/api/v1/ownership/charts").get_json()["result"]
    assert chart.id not in {row["object_id"] for row in listed}


def test_public_dashboard_and_tile_are_within_the_tenant(harness):
    """Same rule on a dashboard, and on a public chart's tile inside a
    dashboard the other tenant somehow reaches: the tile is the placeholder
    (has_access false, no form_data) for tenant B, data for tenant A."""
    from superset.dashboards.api import DashboardRestApi

    tenant_a, ana, amy, bo, nat, chart = _two_tenant_world(harness, 0xC8)
    dash = harness.create_dashboard(ana, chart, title="tenant-public-dash")
    harness.set_visibility(ana, dash, "public")
    assert harness.row(dash).tenant_guid == tenant_a
    assert harness.decide(amy, dash).verdict == ALLOW
    assert harness.decide(bo, dash).verdict == DENY
    assert dash.id in harness.listed_ids(amy, "dashboard")
    assert dash.id not in harness.listed_ids(bo, "dashboard")

    with harness.acting_as(bo):
        tile = DashboardRestApi._serialize_dashboard_chart(
            DashboardRestApi(), harness._load(chart)
        )
    assert tile["has_access"] is False
    assert "form_data" not in tile
    with harness.acting_as(amy):
        tile = DashboardRestApi._serialize_dashboard_chart(
            DashboardRestApi(), harness._load(chart)
        )
    assert "has_access" not in tile
    assert "form_data" in tile


def _blank_row_tenant(harness, ref) -> None:
    """The pre-0004 / not-yet-backfilled row state: no tenant mirrored."""
    from sqlalchemy import update

    from superset_ownership import service
    from superset_ownership.db import ownership_object

    with harness.ctx():
        from superset import db

        db.session.execute(
            update(ownership_object)
            .where(
                ownership_object.c.object_id == ref.id,
                ownership_object.c.asset_type == ref.asset_type,
            )
            .values(tenant_guid=None)
        )
        db.session.commit()
        # Inside the context: the shared layer's generation bump needs the
        # app, and a row cached by an earlier read would otherwise survive.
        service.invalidate_all()


def _check(harness) -> dict:
    with harness.ctx():
        from superset_ownership.lifecycle import check_consistency

        return check_consistency()


def test_untenanted_public_object_is_closed_to_every_tenant(harness):
    """Fail closed: a public object with no tenant on its row is readable by
    its owner and by callers outside every tenant -- an operator's, a
    service account's -- and by NOBODY in a tenant, dataset grant or not.
    An object whose owner belongs to no tenant is exactly that and `check`
    only lists it (`untenanted_public`); one whose owner DOES belong to a
    tenant is a gap -- invisible to its own tenant for want of a stamp --
    and fails the check (`untenanted_public_repairable`) until the tenant
    backfill fills the mirror in."""
    tenant_a, ana, amy, bo, nat, chart = _two_tenant_world(harness, 0xD0)
    w = harness.world
    instance_wide = harness.create_chart(w.admin, "instance-wide")
    harness.set_visibility(w.admin, instance_wide, "public")
    assert harness.row(instance_wide).tenant_guid is None
    for person, verdict in ((w.admin, ALLOW), (nat, ALLOW), (amy, DENY), (bo, DENY)):
        d = harness.decide(person, instance_wide)
        assert (d.verdict, d.calls) == (verdict, ()), person.label
    assert instance_wide.id in harness.listed_ids(nat, "chart")
    assert instance_wide.id not in harness.listed_ids(amy, "chart")
    report = _check(harness)
    assert f"chart:{instance_wide.id}" in report["untenanted_public"]
    assert f"chart:{instance_wide.id}" not in report.get(
        "untenanted_public_repairable", []
    )

    # Ana's chart with its mirror blanked: closed to her own tenant, and a
    # failure `check` names -- the owner is in tenant A, so it can be fixed.
    _blank_row_tenant(harness, chart)
    assert harness.decide(ana, chart).verdict == ALLOW, "the owner always reads"
    assert harness.decide(amy, chart).verdict == DENY, "closed until stamped"
    assert harness.decide(bo, chart).verdict == DENY
    assert chart.id not in harness.listed_ids(amy, "chart")
    report = _check(harness)
    assert f"chart:{chart.id}" in report["untenanted_public_repairable"]
    assert report["ok"] is False

    with harness.ctx():
        from superset_ownership.lifecycle import backfill_object_tenants

        counts = backfill_object_tenants()
    assert counts["written"] + counts.get("mirrored", 0) >= 1, counts
    assert harness.row(chart).tenant_guid == tenant_a
    assert harness.decide(amy, chart).verdict == ALLOW
    assert harness.decide(bo, chart).verdict == DENY
    report = _check(harness)
    assert f"chart:{chart.id}" not in report.get("untenanted_public_repairable", [])


def test_startup_repair_fills_the_tenant_mirror(harness):
    """OWNERSHIP_REPAIR_ON_START covers the mirror too: a start that finds
    a repairable untenanted public row stamps it, so a cutover that arms
    the sentinels does not leave a tenant locked out of its own public
    objects until someone remembers backfill-tenants."""
    tenant_a, ana, amy, bo, nat, chart = _two_tenant_world(harness, 0xE8)
    from superset_ownership import guard, sentinel

    # The full cutover shape: unarmed AND unmirrored. The repair fills the
    # mirror first and arms second, so the row is never armed-and-closed
    # to its own tenant in between.
    with harness.ctx():
        from superset import db

        with guard.suppressed(db.session):
            sentinel.remove_sentinel(harness._load(chart))
            db.session.flush()
        db.session.commit()
    _blank_row_tenant(harness, chart)
    assert harness.viewers(chart) == []
    with harness.ctx():
        from superset_ownership.lifecycle import startup_check

        report = startup_check(repair=True)
    assert report.get("repaired") is True
    assert f"chart:{chart.id}" not in report.get("untenanted_public_repairable", [])
    assert f"chart:{chart.id}" not in report["silently_open"]
    assert harness.viewers(chart) == [SENTINEL]
    assert harness.row(chart).tenant_guid == tenant_a
    assert harness.decide(amy, chart).verdict == ALLOW
    assert harness.decide(bo, chart).verdict == DENY


def test_tenant_comparison_is_case_insensitive(harness):
    """A stamp from a hook that does not lower-case, or a caller resolved
    by one, still matches: the row keeps the GUID lower-cased and the read
    gate compares case-insensitively."""
    tenant_a, ana, amy, bo, nat, chart = _two_tenant_world(harness, 0xF0)
    from superset_ownership import service

    with harness.ctx():
        from superset import db

        service.set_row_tenant(
            "chart", harness.row(chart).object_uuid, tenant_a.upper()
        )
        db.session.commit()
    service.invalidate_all()
    assert harness.row(chart).tenant_guid == tenant_a, "stored lower-cased"
    from superset_ownership import identity

    original = identity.resolve_tenant_guid
    try:
        identity.resolve_tenant_guid = lambda user: (
            (original(user) or "").upper() or None
        )
        service_resolve = service.public_row_visible_to
        with harness.ctx():
            assert service_resolve(harness.row(chart), harness._user(amy)) is True
            assert service_resolve(harness.row(chart), harness._user(bo)) is False
    finally:
        identity.resolve_tenant_guid = original


def test_unowned_public_object_stays_readable_within_its_tenant(harness):
    """Deactivating the owner does not change a public object's audience:
    tenant members keep reading it (it is the OWNER that is missing, not
    the tenant), the other tenant still does not."""
    tenant_a, ana, amy, bo, nat, chart = _two_tenant_world(harness, 0xD8)
    harness.set_active(ana, False)
    try:
        assert harness.decide(amy, chart).verdict == ALLOW
        assert harness.decide(bo, chart).verdict == DENY
        assert chart.id in harness.listed_ids(amy, "chart")
    finally:
        harness.set_active(ana, True)


def test_backfill_arms_and_stamps_public_rows(harness):
    """A public row from before tenant scope -- no sentinel, no tenant on
    the row -- is armed and stamped by the next backfill sweep, so a
    migrated instance closes cross-tenant reads on its first cutover run."""
    tenant_a, ana, amy, bo, nat, chart = _two_tenant_world(harness, 0xE0)
    from superset_ownership import backfill, sentinel
    from superset_ownership.db import ownership_object
    from sqlalchemy import update

    from superset_ownership import guard

    with harness.ctx():
        from superset import db

        obj = harness._load(chart)
        # Under the guard's suppression, as disable() strips: the flush
        # guard would otherwise re-arm a governed row at once.
        with guard.suppressed(db.session):
            sentinel.remove_sentinel(obj)
            db.session.flush()
        db.session.execute(
            update(ownership_object)
            .where(
                ownership_object.c.object_id == chart.id,
                ownership_object.c.asset_type == "chart",
            )
            .values(tenant_guid=None)
        )
        db.session.commit()
    from superset_ownership import service

    service.invalidate_all()
    assert harness.viewers(chart) == []
    # Pre-backfill: no sentinel, so Superset's own rule decides -- and it
    # knows no tenant. Both tenants read it through the dataset grant.
    assert harness.decide(bo, chart).verdict == ALLOW
    assert harness.decide(amy, chart).verdict == ALLOW

    with harness.ctx():
        backfill.run()
    service.invalidate_all()
    assert harness.viewers(chart) == [SENTINEL]
    assert harness.row(chart).tenant_guid == tenant_a
    assert harness.decide(amy, chart).verdict == ALLOW
    assert harness.decide(bo, chart).verdict == DENY


def test_backfill_rerun_mirrors_the_stored_tenant_not_the_derived_one(harness):
    """The store is the source of the mirror. Ana's public chart is stamped
    A in the store and on the row. Ana is re-provisioned into tenant B (her
    role moves); the backfill's derivation now says B, but the store still
    says A -- and the store is what every manage and transfer decision
    reads. A re-run must leave the row at A: tenant A keeps reading its
    object, and the row never disagrees with the store."""
    tenant_a, ana, amy, bo, nat, chart = _two_tenant_world(harness, 0xF8)
    from superset_ownership import backfill, service

    tenant_b = guid(0xB)
    uuid = harness.row(chart).object_uuid
    harness.drain()
    assert harness.authorizer.object_tenant("chart", uuid) == tenant_a
    with harness.ctx():
        from superset import db, security_manager

        user = harness._user(ana)
        user.roles = [r for r in user.roles if r.name != f"tenant_{tenant_a}"] + [
            security_manager.find_role(f"tenant_{tenant_b}")
        ]
        db.session.commit()
    try:
        with harness.ctx():
            backfill.run()
        service.invalidate_all()
        assert harness.authorizer.object_tenant("chart", uuid) == tenant_a
        assert harness.row(chart).tenant_guid == tenant_a, "mirrors the store"
        assert harness.decide(amy, chart).verdict == ALLOW
        assert harness.decide(bo, chart).verdict == DENY

        # And through a store outage the same: the stamp read is strict, so
        # "could not read" never selects the derive branch -- nothing is
        # written to the row, which keeps the store's tenant.
        harness.authorizer.raising = True
        try:
            with harness.ctx():
                backfill.run()
        finally:
            harness.authorizer.raising = False
        service.invalidate_all()
        assert harness.row(chart).tenant_guid == tenant_a, "untouched in an outage"
        assert harness.decide(amy, chart).verdict == ALLOW
        assert harness.decide(bo, chart).verdict == DENY
    finally:
        with harness.ctx():
            from superset import db, security_manager

            user = harness._user(ana)
            user.roles = [r for r in user.roles if r.name != f"tenant_{tenant_b}"] + [
                security_manager.find_role(f"tenant_{tenant_a}")
            ]
            db.session.commit()


def test_backfill_stamps_the_owners_tenant_not_the_creators(harness):
    """A brownfield row whose owner is not its creator -- attribution moved
    it to Amy (A) before anything was stamped, but `created_by_fk` still
    names Bo (B) -- is stamped with the OWNER's tenant. The creator-first
    derivation put such an object in tenant B with an owner from A: a
    public object invisible to its owner's whole tenant, readable by the
    other. The owner's tenant is the invariant every transfer and claim
    already enforces; the creator only decides for an owner outside every
    tenant."""
    from superset_ownership import backfill, service
    from superset_ownership.db import ownership_object
    from sqlalchemy import update

    tenant_a, ana, amy, bo, nat, _ = _two_tenant_world(harness, 0x108)
    chart = harness.create_chart(bo, "made-by-bo")
    harness.set_visibility(bo, chart, "public")
    harness.drain()
    uuid = harness.row(chart).object_uuid
    # The brownfield shape: the row names Amy, nothing is stamped anywhere.
    harness.authorizer.tuples = {
        t
        for t in harness.authorizer.tuples
        if not (t[1] == "tenant" and t[2] == f"chart:{uuid}")
    }
    with harness.ctx():
        from superset import db

        db.session.execute(
            update(ownership_object)
            .where(
                ownership_object.c.asset_type == "chart",
                ownership_object.c.object_id == chart.id,
            )
            .values(owner_user_id=amy.id, tenant_guid=None)
        )
        db.session.commit()
        service.invalidate_all()
    assert harness.authorizer.object_tenant("chart", uuid) is None

    with harness.ctx():
        backfill.run()
    service.invalidate_all()
    assert harness.row(chart).owner_user_id == amy.id, "never re-attributed"
    assert harness.row(chart).tenant_guid == tenant_a, "the owner's tenant"
    assert harness.authorizer.object_tenant("chart", uuid) == tenant_a
    assert harness.decide(amy, chart).verdict == ALLOW
    assert harness.decide(ana, chart).verdict == ALLOW, "the owner's tenant reads it"
    assert harness.decide(nat, chart).verdict == DENY, "outside every tenant"
    # (Bo still opens it: Superset made him a native owner at creation, and
    # a native owner is admitted by stock before the tenant rule is asked.)


def test_neurons_shapes_end_to_end(harness):
    """Ivanti's id shapes, through the real routes and the store fake:
    a person provisioned with `Tenant_<guid>_Role` resolves to the tenant;
    every tuple this module writes for them is `user:<tenant>.<member>`;
    a share to a `group:<tenant>.<local-id>` group reaches a nested
    member; and the tenant's administrator is the `admin` relation on the
    tenant object -- a user directly, or a group named as admin."""
    tenant = guid(0x1A)
    role = f"Tenant_{tenant}_Role"
    nia = harness.add_person(guid(0x110), "Nia", "Gamma", "sales_readers", role)
    noa = harness.add_person(guid(0x111), "Noa", "Gamma", "sales_readers", role)
    nat = harness.add_person(guid(0x112), "Nat", "Gamma", "sales_readers", role)
    tam = harness.add_person(guid(0x113), "TamN", "Gamma", "sales_readers", role)
    assert nia.ref == f"user:{tenant}.{guid(0x110)}", "the store spelling"
    with harness.ctx():
        from superset_ownership.identity import member_ref, resolve_tenant_guid

        assert resolve_tenant_guid(harness._user(nia)) == tenant
        assert member_ref(nia.id) == nia.ref

    chart = harness.create_chart(nia, "neurons-shaped")
    harness.set_visibility(nia, chart, "shared")
    harness.drain()
    assert (nia.ref, "owner", chart.obj) in harness.authorizer.tuples
    assert (f"tenant:{tenant}#member", "tenant", chart.obj) in harness.authorizer.tuples

    # A direct share, written and checked in the dotted spelling; the
    # picker's bare member GUID names the same person.
    harness.share(nia, chart, f"user:{guid(0x111)}")
    assert (noa.ref, "viewer", chart.obj) in harness.authorizer.tuples
    assert harness.decide(noa, chart).verdict == ALLOW

    # A Neurons-shaped group, nested one level: parent <- child <- Nat.
    parent, child = f"{tenant}.designers", f"{tenant}.designers.eu"
    harness.authorizer.groups[child] = {nat.ref}
    harness.authorizer.groups[parent] = {f"group:{child}#member"}
    harness.authorizer.tuples.add(
        (f"group:{child}#member", "member", f"group:{parent}")
    )
    harness.share(nia, chart, f"group:{parent}#member")
    assert harness.decide(nat, chart).verdict == ALLOW

    # The administrator: the admin relation on the tenant object, no group.
    assert harness.get(tam, f"/api/v1/ownership/chart/{chart.id}").status_code == 404
    harness.authorizer.make_tenant_administrator(tam.ref, tenant)
    r = harness.get(tam, f"/api/v1/ownership/chart/{chart.id}")
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["manage_reason"] == "tenant_admin"
    with harness.ctx():
        from superset_ownership.plugins import get_directory

        admins = get_directory().tenant_administrators(tenant)
    assert [a["guid"] for a in admins] == [f"{tenant}.{guid(0x113)}"]


def test_a_pre_upgrade_bare_guid_share_is_listed_reported_and_removable(harness):
    """PR #113 review B1: a share written before the store id carried the
    tenant sits in the mirror as `user:<member>` with a matching tuple.
    It grants nothing under the new spelling (the check asks
    `user:<tenant>.<member>`), `check` lists it under `user_id_mismatch`,
    and `DELETE .../shares/user:<member>` revokes under the ROW's own
    spelling -- row and tuple both go, 200, not a 202 against a subject
    that has neither. A re-share then lists the person once."""
    tenant = guid(0x1B)
    role = f"Tenant_{tenant}_Role"
    ola = harness.add_person(guid(0x120), "Ola", "Gamma", "sales_readers", role)
    oli = harness.add_person(guid(0x121), "Oli", "Gamma", "sales_readers", role)
    chart = harness.create_chart(ola, "legacy-bare-share")
    harness.set_visibility(ola, chart, "shared")
    harness.drain()

    legacy = f"user:{guid(0x121)}"
    with harness.ctx():
        from superset import db

        from superset_ownership import service

        service.add_share_row("chart", chart.id, legacy, "viewer")
        db.session.commit()
    harness.authorizer.tuples.add((legacy, "viewer", chart.obj))

    assert harness.decide(oli, chart).verdict != ALLOW, (
        "the old spelling grants nothing"
    )
    r = harness.get(ola, f"/api/v1/ownership/chart/{chart.id}")
    assert [sh["subject"] for sh in r.get_json()["shares"]] == [legacy]
    with harness.ctx():
        from superset_ownership.lifecycle import check_consistency

        report = check_consistency()
    assert report["user_id_mismatch"] == [legacy]
    assert report["ok"] is False

    r = harness.revoke(ola, chart, legacy)
    assert r.status_code == 200
    assert "queued" not in r.get_json(), r.get_json()
    assert (legacy, "viewer", chart.obj) not in harness.authorizer.tuples
    r = harness.get(ola, f"/api/v1/ownership/chart/{chart.id}")
    assert r.get_json()["shares"] == []
    with harness.ctx():
        assert check_consistency()["user_id_mismatch"] == []

    harness.share(ola, chart, legacy)
    assert harness.decide(oli, chart).verdict == ALLOW
    r = harness.get(ola, f"/api/v1/ownership/chart/{chart.id}")
    assert [sh["subject"] for sh in r.get_json()["shares"]] == [oli.ref]


def test_unshare_names_the_row_the_caller_spelt_when_both_spellings_exist(harness):
    """PR #113 review round 2: with a legacy `user:<member>` row AND the
    canonical `user:<tenant>.<member>` row for one person (re-shared before
    the old row was cleared), `DELETE .../shares/user:<member>` removes the
    legacy row -- the raw subject's own row wins before normalisation --
    and leaves the live one; the person keeps access. A second delete by
    the canonical spelling clears that one."""
    tenant = guid(0x1E)
    role = f"Tenant_{tenant}_Role"
    ria = harness.add_person(guid(0x140), "Ria", "Gamma", "sales_readers", role)
    rio = harness.add_person(guid(0x141), "Rio", "Gamma", "sales_readers", role)
    chart = harness.create_chart(ria, "both-spellings")
    harness.set_visibility(ria, chart, "shared")
    harness.share(ria, chart, rio.ref)
    legacy = f"user:{guid(0x141)}"
    with harness.ctx():
        from superset import db

        from superset_ownership import service

        service.add_share_row("chart", chart.id, legacy, "viewer")
        db.session.commit()
    harness.authorizer.tuples.add((legacy, "viewer", chart.obj))

    r = harness.revoke(ria, chart, legacy)
    assert r.get_json()["subject"] == legacy
    assert (legacy, "viewer", chart.obj) not in harness.authorizer.tuples
    assert (rio.ref, "viewer", chart.obj) in harness.authorizer.tuples
    assert harness.decide(rio, chart).verdict == ALLOW
    r = harness.get(ria, f"/api/v1/ownership/chart/{chart.id}")
    assert [sh["subject"] for sh in r.get_json()["shares"]] == [rio.ref]

    harness.revoke(ria, chart, rio.ref)
    assert harness.decide(rio, chart).verdict != ALLOW


def test_another_tenants_spelling_of_a_member_names_nobody(harness):
    """PR #113 review: `user:<B>.<m>` for a member of A resolved by the
    member half and came back re-spelt as `user:<A>.<m>` -- a share request
    naming tenant B's spelling was answered for tenant A's, and the client
    could not tell. The default path now applies the same forward check as
    the hooked one: the tenant halves must agree, so the subject is a 400."""
    tenant = guid(0x1C)
    other = guid(0x1D)
    role = f"Tenant_{tenant}_Role"
    pia = harness.add_person(guid(0x130), "Pia", "Gamma", "sales_readers", role)
    pim = harness.add_person(guid(0x131), "Pim", "Gamma", "sales_readers", role)
    with harness.ctx():
        from superset_ownership.identity import normalize_subject

        assert normalize_subject(f"user:{guid(0x131)}") == pim.ref
        assert normalize_subject(pim.ref) == pim.ref
        assert normalize_subject(f"user:{other}.{guid(0x131)}") is None
    chart = harness.create_chart(pia, "foreign-spelling")
    harness.set_visibility(pia, chart, "shared")
    r = harness.post(
        pia,
        f"/api/v1/ownership/chart/{chart.id}/shares",
        {"subject": f"user:{other}.{guid(0x131)}", "role": "viewer"},
    )
    assert r.status_code == 400, (r.status_code, r.get_json())
    harness.drain()
    assert not any(
        t[2] == chart.obj and t[1] == "viewer" for t in harness.authorizer.tuples
    )


def test_list_filter_cost_does_not_grow_with_the_tenants_public_objects(harness):
    """The public objects of the caller's tenant are decided in SQL, as
    stock decides them: the statements the list filter issues are the same
    for a tenant with four public charts as with twelve. Nothing is loaded
    per object and no per-object permission check runs."""
    tenant_a, ana, amy, bo, nat, chart = _two_tenant_world(harness, 0x100)
    from sqlalchemy import event

    from superset_ownership import hooks

    def statements_for(person) -> int:
        from superset import db

        count = 0

        def tick(*_args, **_kwargs):
            nonlocal count
            count += 1

        with harness.acting_as(person):
            engine = db.engine
            event.listen(engine, "before_cursor_execute", tick)
            try:
                hooks.chart_query_filter(person.id)
            finally:
                event.remove(engine, "before_cursor_execute", tick)
        return count

    for i in range(3):
        harness.set_visibility(ana, harness.create_chart(ana, f"pub-{i}"), "public")
    assert len(harness.listed_ids(amy, "chart")) >= 4
    small = statements_for(amy)
    for i in range(8):
        harness.set_visibility(
            ana, harness.create_chart(ana, f"pub-more-{i}"), "public"
        )
    assert len(harness.listed_ids(amy, "chart")) >= 12
    assert statements_for(amy) == small, "one statement for the whole public set"


# --- 8. NON-OWNER: exactly one check -----------------------------------------


def test_private_and_shared_non_owner_costs_exactly_one_check(harness):
    """A `shared` decision costs one `check`, for the caller's own reference
    and `viewer`. An admitted decision returns from the bypass hook and
    makes no other call. A `private` decision costs ZERO calls for a
    non-owner (issue #93's defence in depth, `raise_for_access_bypass`'s
    hard deny on `visibility == "private"`). None of the shares in this
    test are `editor`, so M-3's lazy `object_tenant` read (review round 2,
    PR #100 -- see `test_owner_get_route_pays_object_tenant_once_per_
    request_with_an_editor_share` for that case) is never reached here
    either: every count below is asserted on every call the store saw, not
    on `check` alone, and that includes proving `object_tenant` stays
    absent throughout."""
    w = harness.world
    chart = harness.create_chart(w.ada, "one-check")
    dash = harness.create_dashboard(w.ada, chart, title="one-check")
    for ref in (chart, dash):
        one_check = ("check", w.ben.ref, "viewer", ref.obj)
        # private: denied before any store call -- zero calls, full stop
        # (M-3: no editor share on this object, so the editors resolver's
        # own object_tenant read is never reached either)
        d = harness.decide(w.ben, ref)
        assert d.verdict == DENY
        assert d.checks == [], "the private hard-deny asks the store nothing"
        assert d.calls == (), (
            "M-3: no editor share here, so object_tenant is not reached"
        )

        # shared with Ben: one check, allowed, nothing else
        harness.set_visibility(w.ada, ref, "shared")
        harness.share(w.ada, ref, w.ben.ref)
        assert harness.row(ref).visibility == "shared"
        d = harness.decide(w.ben, ref)
        assert d.verdict == ALLOW
        assert d.calls == (one_check,)

        # shared with Ben (a viewer share, not editor), asked by Dee: one
        # check, denied, nothing else -- M-3: still no editor share to read
        # object_tenant for
        d = harness.decide(w.dee, ref)
        assert d.verdict == DENY
        assert d.checks == [("check", w.dee.ref, "viewer", ref.obj)]
        assert d.names == ("check",)

        # an admin who is not the owner pays the same single check before
        # Superset's own is_admin admits them, and nothing else
        d = harness.decide(w.admin, ref)
        assert d.verdict == ALLOW
        assert d.names == ("check",)

    # the data route for the share-holder: one check, rows returned; the
    # list_objects is the base filter's (issue #82)
    r = harness.get(w.ben, f"/api/v1/chart/{chart.id}/data/")
    assert r.status_code == 200
    assert harness.authorizer.count("check") == 1
    assert set(harness.authorizer.names) <= {"check", "list_objects"}, ISSUE_82
    # and for the non-holder: one check, the placeholder, nothing else
    r = harness.get(w.dee, f"/api/v1/chart/{chart.id}/data/")
    assert r.status_code == 403
    assert harness.authorizer.names == ("check",)
    assert _data_error(r)["extra"]["ownership"] == "private"


# --- 8b. REVOCATION ----------------------------------------------------------


@pytest.mark.parametrize("asset_type", ASSET_TYPES)
def test_revocation_denies_before_the_outbox_drains(harness, asset_type):
    """The store lags the mirror until the outbox is drained. For a grant
    that lag fails safe (no tuple yet). For a revocation it does not: the
    store still holds the tuple and `check` would still say yes. The bypass
    hook therefore denies while a revocation for this object and subject is
    undelivered -- WITHOUT asking the store -- and the data gate, which is the
    same decision, denies with it.

    Issue #84: the list filter and, through FAB's list base filter,
    single-object GET admitted the revoked subject anyway, because both are
    built from the store's `list_objects` answer, which lags the outbox the
    same way `check` does. `owned_or_shared_rows` now subtracts a candidate
    with an undelivered revocation before either can see it, at the cost of
    exactly one extra, indexed DB query -- never an extra store call, and
    never one query per candidate object."""
    from superset_ownership import outbox

    w = harness.world
    ref = harness.create(asset_type, w.ada, f"revoke-{asset_type}")
    # Issue #93: `private` is now a hard deny for every non-owner, so the
    # share has to actually confer access for this test's premise (a
    # granted subject, then revoked) to hold -- transition to `shared`
    # first.
    harness.set_visibility(w.ada, ref, "shared")
    harness.share(w.ada, ref, w.ben.ref)
    d = harness.decide(w.ben, ref)
    assert d.verdict == ALLOW
    assert d.names == ("check",)

    assert ref.id in harness.visible_ids(w.ben, asset_type), (
        "shared: visible before the revoke"
    )
    assert harness.detail(w.ben, ref).status_code == 200

    harness.revoke(w.ada, ref, w.ben.ref, drain=False)
    assert (w.ben.ref, "viewer", ref.obj) in harness.authorizer.tuples, (
        "the store has not heard: the tuple that would still grant is there"
    )
    pending = [
        o
        for o in harness.outbox()
        if o["object"] == ref.obj and o["status"] == "pending"
    ]
    assert [o["op"] for o in pending] == ["revoke_subject"]

    d = harness.decide(w.ben, ref)
    assert d.verdict == DENY
    assert d.checks == [], "denied by the pending revocation, the store not asked"
    assert d.names == (), (
        "M-3: Ben's share is viewer, not editor -- the editors resolver's "
        "object_tenant read is never reached"
    )
    if asset_type == "chart":
        r = harness.get(w.ben, f"/api/v1/chart/{ref.id}/data/")
        assert r.status_code == 403
        assert harness.authorizer.count("check") == 0

    # LIST and single-GET: still admitted the store's stale list_objects
    # answer before #84. Denied now, before the drain, WITHOUT an extra
    # store call -- one extra DB query (pending_revocations_for) instead.
    with patch.object(
        outbox, "pending_revocations_for", wraps=outbox.pending_revocations_for
    ) as spy:
        harness.authorizer.calls.clear()
        visible = harness.visible_ids(w.ben, asset_type)
        assert ref.id not in visible, ISSUE_84
        assert set(harness.authorizer.names) <= {"list_objects"}, (
            "no extra store call: the revocation check never leaves the DB"
        )
        assert spy.call_count == 1, (
            "one Python call to pending_revocations_for, not one per candidate object "
            "-- the statement count it issues underneath is a separate claim, asserted "
            "in test_lookup_cache.py's CountingDB tests, not here"
        )
    assert harness.detail(w.ben, ref).status_code == 404, ISSUE_84
    # M-2: the ownership metadata route delegates the same reachability
    # check (`api._reachable_ids` -> `owned_or_shared_object_ids`), so the
    # revoked holder gets 404 there too, before the drain -- not just on
    # Superset's own detail route above.
    ownership_detail = f"/api/v1/ownership/{asset_type}/{ref.id}"
    assert harness.get(w.ben, ownership_detail).status_code == 404, ISSUE_84

    # The mirror row is gone; the detail route, which merges the store's
    # grants in, still shows the undelivered one -- marked, so the owner can
    # see the store has not caught up yet.
    detail = f"/api/v1/ownership/{asset_type}/{ref.id}"
    (share,) = harness.get(w.ada, detail).get_json()["shares"]
    assert (share["subject"], share.get("unmirrored")) == (w.ben.ref, True)

    # After the drain the tuple is gone and the ordinary path denies too.
    harness.drain()
    assert (w.ben.ref, "viewer", ref.obj) not in harness.authorizer.tuples
    assert harness.get(w.ada, detail).get_json()["shares"] == []
    d = harness.decide(w.ben, ref)
    assert d.verdict == DENY
    assert d.names == ("check",), (
        "M-3: still no editor share, object_tenant not reached"
    )
    assert ref.id not in harness.visible_ids(w.ben, asset_type), (
        "still absent after the drain"
    )
    assert harness.detail(w.ben, ref).status_code == 404


@pytest.mark.parametrize("asset_type", ASSET_TYPES)
def test_group_revocation_denies_list_and_get_before_the_outbox_drains(
    harness, asset_type
):
    """A revoke of `group:eng#member` must drop an object a member (Dee)
    reached ONLY through that group from the list and from single-object
    GET too, the moment it is queued -- not just from the access decision.
    `pending_revocations_for` cannot tell whether Dee's own reachability
    came from this group or a direct share, so (like `has_pending_
    revocation`) it treats any undelivered revoke of a non-user subject as
    revoking every member, Dee included, until the drain delivers it."""
    w = harness.world
    ref = harness.create(asset_type, w.ada, f"group-revoke-{asset_type}")
    harness.set_visibility(w.ada, ref, "shared")
    harness.share(w.ada, ref, GROUP_SUBJECT)
    assert ref.id in harness.visible_ids(w.dee, asset_type)
    assert harness.detail(w.dee, ref).status_code == 200

    harness.revoke(w.ada, ref, GROUP_SUBJECT, drain=False)
    assert (GROUP_SUBJECT, "viewer", ref.obj) in harness.authorizer.tuples, (
        "the store has not heard yet"
    )
    pending = [
        o
        for o in harness.outbox()
        if o["object"] == ref.obj and o["status"] == "pending"
    ]
    assert [o["op"] for o in pending] == ["revoke_subject"]

    d = harness.decide(w.dee, ref)
    assert d.verdict == DENY
    assert d.checks == [], "denied by the pending group revocation, the store not asked"

    assert ref.id not in harness.visible_ids(w.dee, asset_type), ISSUE_84
    assert harness.detail(w.dee, ref).status_code == 404, ISSUE_84

    harness.drain()
    assert (GROUP_SUBJECT, "viewer", ref.obj) not in harness.authorizer.tuples
    assert ref.id not in harness.visible_ids(w.dee, asset_type)
    assert harness.detail(w.dee, ref).status_code == 404


# --- 8b-2. PRIVATE REVOKES SHARES (issue #93) --------------------------------


@pytest.mark.parametrize("asset_type", ASSET_TYPES)
def test_shared_to_private_revokes_every_share(harness, asset_type):
    """Setting visibility to `private` revokes every existing share as a
    side effect of the SAME write (issue #93's fix): the mirror row is
    deleted and a `revoke_subject` is queued through the outbox for each
    one, in the same transaction as the visibility change -- so a
    previously shared user is denied both immediately (before the drain,
    on the private hard-deny -- ZERO store calls; M-3: no editor share
    here, so the editors resolver's own `object_tenant` read is not
    reached either) and after it, exactly the two-phase shape
    `test_revocation_denies_before_the_outbox_drains`
    proves for an explicit unshare, above. One `ownership.share_removed` event is
    emitted per share revoked this way, `admitted_by == "visibility_
    change"` rather than whichever ground admitted the caller to make the
    change (that ground is on the `ownership.visibility_changed` event the
    same request also emits). `private -> shared` afterwards starts with
    zero shares: nothing repopulates them."""
    w = harness.world
    ref = harness.create(asset_type, w.ada, f"private-revoke-{asset_type}")
    harness.set_visibility(w.ada, ref, "shared")
    harness.share(w.ada, ref, w.ben.ref)
    assert harness.decide(w.ben, ref).verdict == ALLOW, "shared and reachable"

    harness.audit.clear()
    harness.set_visibility(w.ada, ref, "private")

    # Denied immediately: the mirror row is gone (deleted in the same
    # transaction as the visibility write); the detail route, which merges
    # the store's grants in, still shows the undelivered revocation until
    # the drain runs -- marked, exactly as an explicit unshare's does.
    detail = f"/api/v1/ownership/{asset_type}/{ref.id}"
    (share,) = harness.get(w.ada, detail).get_json()["shares"]
    assert (share["subject"], share.get("unmirrored")) == (w.ben.ref, True)
    d = harness.decide(w.ben, ref)
    assert d.verdict == DENY
    assert d.checks == [], (
        "private: the hard deny refuses before any store CHECK, whatever "
        "the store still says pre-drain"
    )
    assert d.names == (), "M-3: no editor share, object_tenant not reached"
    pending = [
        o
        for o in harness.outbox()
        if o["object"] == ref.obj and o["status"] == "pending"
    ]
    assert [o["op"] for o in pending] == ["revoke_subject"]

    # One share_removed event, admitted by the visibility change itself,
    # alongside the visibility_changed event the same request emits.
    (removed,) = harness.events("ownership.share_removed")
    assert removed["before"] == {"subject": w.ben.ref, "mirror_row": True}
    assert removed["admitted_by"] == "visibility_change"
    (changed,) = harness.events("ownership.visibility_changed")
    assert changed["after"]["visibility"] == "private"

    # After the drain the tuple is gone and the ordinary path denies too
    # (private makes no store call either way, but the store itself must
    # actually have forgotten the subject).
    harness.drain()
    assert (w.ben.ref, "viewer", ref.obj) not in harness.authorizer.tuples
    assert harness.get(w.ada, detail).get_json()["shares"] == []
    d = harness.decide(w.ben, ref)
    assert d.verdict == DENY
    assert d.checks == []
    assert d.names == (), "M-3: no editor share, object_tenant not reached"

    # private -> shared afterwards starts empty: nothing repopulates it.
    harness.set_visibility(w.ada, ref, "shared")
    assert harness.get(w.ada, detail).get_json()["shares"] == []


@pytest.mark.parametrize("asset_type", ASSET_TYPES)
def test_editor_share_on_private_confers_nothing(harness, asset_type):
    """H1 (review round 1, issue #93): the private hard-deny used to cover
    only the READ side -- Superset's own `is_editor` consults
    `hooks.editor_subject_ids` directly, which did not know the private
    rule, so an `editor` share on a still-private object opened AND edited
    it while a `viewer` share on the same object was correctly denied.
    Both roles now confer nothing, before and after the outbox drains."""
    w = harness.world
    ref = harness.create(asset_type, w.ada, f"editor-share-private-{asset_type}")
    harness.set_visibility(w.ada, ref, "shared")
    harness.share(w.ada, ref, w.ben.ref, "editor")
    assert harness.decide(w.ben, ref).verdict == ALLOW, "shared: the share works"
    assert harness.rename(w.ben, ref, "edited while shared").status_code == 200

    harness.set_visibility(w.ada, ref, "private")

    d = harness.decide(w.ben, ref)
    assert d.verdict == DENY
    assert d.checks == [], "denied before any store check"
    r = harness.rename(w.ben, ref, "edited while private, pre-drain")
    assert r.status_code in (403, 404), r.get_json()

    harness.drain()
    d = harness.decide(w.ben, ref)
    assert d.verdict == DENY
    r = harness.rename(w.ben, ref, "edited while private, post-drain")
    assert r.status_code in (403, 404), r.get_json()


@pytest.mark.parametrize("asset_type", ASSET_TYPES)
def test_stray_editor_row_on_private_confers_nothing(harness, asset_type, monkeypatch):
    """H1: the same defence in depth as
    `test_private_hard_deny_makes_no_store_check_even_with_a_stray_share_row`
    above, for an `editor` role specifically -- a share row that reaches
    the mirror and the store WITHOUT ever going through the route (planted
    directly at the service layer, as a stray write or an out-of-band
    grant might) confers neither read nor native edit on a `private`
    object."""
    w = harness.world
    ref = harness.create(asset_type, w.ada, f"stray-editor-private-{asset_type}")
    assert harness.row(ref).visibility == "private"

    with harness.ctx():
        from superset import db
        from superset_ownership import service

        service.add_share(asset_type, ref.id, ref.uuid, w.ben.ref, "editor")
        db.session.commit()
    harness.drain()
    assert (w.ben.ref, "editor", ref.obj) in harness.authorizer.tuples, (
        "the store genuinely holds the tuple, not merely the mirror"
    )
    assert harness.row(ref).visibility == "private", "never transitioned"

    d = harness.decide(w.ben, ref)
    assert d.verdict == DENY
    assert d.checks == [], "denied before any store check"

    # R2-N1 (review round 2, PR #96, `review-private-revokes-pr96.md`):
    # `editor_subject_ids`'s private short-circuit runs BEFORE
    # `editor_share_user_ids` now, not after -- with a resolvable `user:`
    # editor share actually present (this stray row), the pre-fix ordering
    # would still reach `in_tenant`'s lazy `object_tenant` read (PR #100)
    # while resolving it, before discarding the answer for being on a
    # private object. Resetting here isolates the `rename` call below from
    # whatever the `decide` probe above and `harness.drain()` already
    # recorded.
    harness.authorizer.reset()
    r = harness.rename(w.ben, ref, "edited via a stray editor row")
    assert r.status_code in (403, 404), r.get_json()
    assert "object_tenant" not in harness.authorizer.names, (
        "private short-circuits before editor_share_user_ids ever asks "
        "the mirror or the store about the stray editor share"
    )

    # M1 (review round 2, PR #101, `review-sweep-pr101.md`): the route
    # assertion above passes unchanged against main's `hooks.py` too -- the
    # native PUT is refused by the private hard deny before Superset ever
    # consults `is_editor`, so `editor_subject_ids` is not on that route's
    # path in EITHER ordering, and the assertion above is vacuous there.
    # Call `editor_subject_ids` directly, on the same stray row, to pin the
    # half a route probe cannot reach: `editor_share_user_ids` (the mirror
    # query) is never even called for a private object, and the store sees
    # nothing. Against main's `hooks.py`, which lacks the short-circuit,
    # this calls `editor_share_user_ids` and reads `object_tenant` from the
    # store, so this assertion -- unlike the route one -- fails there.
    from superset_ownership import hooks, service

    spy_calls: list[Any] = []

    def _spy(*args: Any, **kwargs: Any) -> list[int]:
        spy_calls.append((args, kwargs))
        return []

    monkeypatch.setattr(hooks, "editor_share_user_ids", _spy)
    harness.authorizer.reset()
    with harness.ctx():
        private_row = service.lookup(asset_type, ref.id, fresh=True)
        hooks.editor_subject_ids(asset_type, ref.id, private_row)
    assert spy_calls == [], (
        "private short-circuits before editor_share_user_ids is ever called"
    )
    assert harness.authorizer.names == (), (
        "no store call at all for a private object's stray editor share"
    )


@pytest.mark.parametrize("asset_type", ASSET_TYPES)
def test_share_route_refuses_a_private_object(harness, asset_type):
    """H1: `POST /shares` on a still-`private` object is refused outright
    (409) rather than accepted and left inert -- a grant the read gate
    ignores for a viewer and, before the hooks.py half of this fix,
    honoured for an editor. The drawer never sends this (share writes only
    happen while `shared`), so nothing user-facing changes; a script gets
    a truthful answer instead of a share the object silently withholds."""
    w = harness.world
    ref = harness.create(asset_type, w.ada, f"share-route-private-{asset_type}")
    assert harness.row(ref).visibility == "private"

    for role in ("viewer", "editor"):
        r = harness.post(
            w.ada,
            f"/api/v1/ownership/{asset_type}/{ref.id}/shares",
            {"subject": w.ben.ref, "role": role},
        )
        assert r.status_code == 409, (role, r.get_json())
        assert "shared" in r.get_json()["message"]
    assert (
        harness.get(w.ada, f"/api/v1/ownership/{asset_type}/{ref.id}").get_json()[
            "shares"
        ]
        == []
    )


def test_inline_mode_store_refusal_during_private_revoke_rolls_back(
    harness, monkeypatch
):
    """M2 (review round 1): with the outbox DISABLED
    (`OWNERSHIP_OUTBOX_ENABLED=false` -- the documented setting for the
    local authorizer), a store refusal mid-revoke-loop must not be
    swallowed. Before this fix `_revoke_shares_for_private` discarded
    `service.remove_share`'s return value: the loop continued, `private`
    committed, the mirror row was gone and a `share_removed` event was
    emitted for a tuple the store still held in force. Now it answers the
    unshare route's own 502 and rolls the whole transaction back --
    visibility included -- before the sentinel write."""
    w = harness.world
    ref = harness.create_chart(w.ada, "inline-revoke-refusal")
    harness.set_visibility(w.ada, ref, "shared")
    harness.share(w.ada, ref, w.ben.ref)
    assert harness.decide(w.ben, ref).verdict == ALLOW

    harness.audit.clear()
    # The harness's generated test config sets OWNERSHIP_OUTBOX_ENABLED in
    # the CONFIG layer, which settings.get reads ahead of the environment
    # (settings.py §3) -- monkeypatch.setenv alone would have no effect.
    # `outbox.enabled` is the seam every other inline-mode test in this
    # suite patches instead (test_group_id.py, test_migrations.py).
    from superset_ownership import outbox

    monkeypatch.setattr(outbox, "enabled", lambda: False)
    harness.authorizer.up = False
    try:
        r = harness.put(
            w.ada,
            f"/api/v1/ownership/{ref.asset_type}/{ref.id}/visibility",
            {"visibility": "private"},
        )
    finally:
        harness.authorizer.up = True

    assert r.status_code == 502
    assert harness.row(ref).visibility == "shared", (
        "the visibility write rolled back with the failed revoke"
    )
    detail = f"/api/v1/ownership/{ref.asset_type}/{ref.id}"
    (share,) = harness.get(w.ada, detail).get_json()["shares"]
    assert share["subject"] == w.ben.ref, "mirror row intact"
    assert harness.events("ownership.share_removed") == []
    assert harness.events("ownership.visibility_changed") == []
    assert harness.decide(w.ben, ref).verdict == ALLOW, (
        "the grant is still in force; nothing was changed"
    )


def test_inline_mode_private_revoke_does_not_stop_at_the_first_refusal(
    harness, monkeypatch
):
    """R2-N2 (review round 2, PR #96, `review-private-revokes-pr96.md`):
    with two or more shares and the store refusing only a LATER one,
    `_revoke_shares_for_private` keeps trying the REST rather than stopping
    at the first refusal (fail-safe direction: the store ends up narrower
    than the mirror, never wider). Ben's share is revoked for real before
    Dee's is refused and the whole mirror transaction rolls back -- so the
    502's message must not claim "nothing was changed" the way the
    single-share case genuinely can; it says some grants may already be
    revoked instead."""
    w = harness.world
    ref = harness.create_chart(w.ada, "inline-revoke-partial")
    harness.set_visibility(w.ada, ref, "shared")
    harness.share(w.ada, ref, w.ben.ref)
    harness.share(w.ada, ref, w.dee.ref)
    assert harness.decide(w.ben, ref).verdict == ALLOW
    assert harness.decide(w.dee, ref).verdict == ALLOW

    from superset_ownership import outbox, service

    monkeypatch.setattr(outbox, "enabled", lambda: False)  # inline mode (M2)

    calls: list[str] = []
    real_remove_share = service.remove_share

    def _remove_share(asset_type, pk, object_uuid, subject):
        calls.append(subject)
        if subject == w.dee.ref:
            return False  # the store refuses Dee's revoke specifically
        return real_remove_share(asset_type, pk, object_uuid, subject)

    monkeypatch.setattr(service, "remove_share", _remove_share)

    harness.audit.clear()
    r = harness.put(
        w.ada,
        f"/api/v1/ownership/{ref.asset_type}/{ref.id}/visibility",
        {"visibility": "private"},
    )

    assert r.status_code == 502
    assert sorted(calls) == sorted([w.ben.ref, w.dee.ref]), (
        "both shares were attempted -- the loop did not stop at Ben's success "
        "or Dee's refusal"
    )
    assert "some grants may already be revoked" in r.get_json()["message"]
    assert harness.row(ref).visibility == "shared", (
        "the visibility write rolled back with the failed revoke"
    )
    detail = f"/api/v1/ownership/{ref.asset_type}/{ref.id}"
    subjects = {s["subject"] for s in harness.get(w.ada, detail).get_json()["shares"]}
    assert subjects == {w.ben.ref, w.dee.ref}, "both mirror rows restored by rollback"
    assert harness.events("ownership.share_removed") == []
    assert harness.events("ownership.visibility_changed") == []
    # The actual store call DID go through for Ben (the real remove_share
    # ran for him before Dee's refusal) -- the mirror row is back, but the
    # grant genuinely is not, exactly what the corrected message says might
    # be true and the old "nothing was changed" sentence denied.
    assert harness.decide(w.ben, ref).verdict == DENY
    assert harness.decide(w.dee, ref).verdict == ALLOW


def test_private_hard_deny_makes_no_store_check_even_with_a_stray_share_row(harness):
    """The private hard-deny in `raise_for_access_bypass` (issue #93) is
    defence in depth, independent of `_set_asset_visibility`'s revoke-on-
    write above: even a share row that reaches the mirror and the store
    WITHOUT ever going through the shared -> private transition -- planted
    directly at the service layer here, as a stray write or an
    out-of-band grant might -- confers nothing on a `private` object. A
    non-owner is denied with ZERO store calls, full stop: the stray share
    is `viewer`, not `editor`, so M-3's lazy `object_tenant` read (review
    round 2, PR #100) is not reached either."""
    w = harness.world
    chart = harness.create_chart(w.ada, "private-defence-in-depth")
    assert harness.row(chart).visibility == "private"

    with harness.ctx():
        from superset import db
        from superset_ownership import service

        service.add_share("chart", chart.id, chart.uuid, w.ben.ref, "viewer")
        db.session.commit()
    harness.drain()
    assert (w.ben.ref, "viewer", chart.obj) in harness.authorizer.tuples, (
        "the store genuinely holds the tuple, not merely the mirror"
    )
    assert harness.row(chart).visibility == "private", "never transitioned"

    d = harness.decide(w.ben, chart)
    assert d.verdict == DENY
    assert d.checks == [], "denied before any store check"
    assert d.names == (), "M-3: the stray share is viewer, object_tenant not reached"


@pytest.mark.parametrize("asset_type", ASSET_TYPES)
def test_shared_to_private_removes_list_membership_and_get_before_the_outbox_drains(
    harness, asset_type
):
    """H2 (review round 1, issue #93): `owned_or_shared_rows` must agree
    with the hard deny, not only the access decision -- a `-> private`
    write has to drop the previously shared user from their OWN list
    (`visible_ids`) and answer 404 on their OWN `GET` of the object, the
    moment the visibility write commits, not only once the drain
    delivers the queued revocation. Before this fix the list filter and
    single-object GET had no visibility test beyond `public`, so Ben kept
    seeing the object -- reachable, not merely present in a stale detail
    payload -- for the whole drain window."""
    w = harness.world
    ref = harness.create(asset_type, w.ada, f"list-parity-{asset_type}")
    harness.set_visibility(w.ada, ref, "shared")
    harness.share(w.ada, ref, w.ben.ref)
    assert ref.id in harness.visible_ids(w.ben, asset_type)
    assert harness.detail(w.ben, ref).status_code == 200

    harness.set_visibility(w.ada, ref, "private")

    assert ref.id not in harness.visible_ids(w.ben, asset_type), (
        "undrained: the private row must not be listed"
    )
    assert harness.detail(w.ben, ref).status_code == 404

    harness.drain()
    assert ref.id not in harness.visible_ids(w.ben, asset_type)
    assert harness.detail(w.ben, ref).status_code == 404


@pytest.mark.parametrize("asset_type", ASSET_TYPES)
def test_stray_viewer_row_on_private_is_never_listed(harness, asset_type):
    """H2: the list-membership counterpart to
    `test_private_hard_deny_makes_no_store_check_even_with_a_stray_share_row`
    above -- a share row that reaches the mirror and the store WITHOUT
    ever going through the `shared -> private` transition (planted
    directly at the service layer, as a stray write or an out-of-band
    grant might) must not put the object in the subject's list either,
    even though the store genuinely holds the tuple."""
    w = harness.world
    ref = harness.create(asset_type, w.ada, f"stray-viewer-private-{asset_type}")
    assert harness.row(ref).visibility == "private"

    with harness.ctx():
        from superset import db
        from superset_ownership import service

        service.add_share(asset_type, ref.id, ref.uuid, w.ben.ref, "viewer")
        db.session.commit()
    harness.drain()
    assert (w.ben.ref, "viewer", ref.obj) in harness.authorizer.tuples, (
        "the store genuinely holds the tuple, not merely the mirror"
    )

    assert ref.id not in harness.visible_ids(w.ben, asset_type)
    assert harness.detail(w.ben, ref).status_code == 404


def test_unsharing_an_account_that_no_longer_resolves_still_succeeds(harness):
    """Issue #93: `DELETE .../shares/<subject>` is validated against the
    MIRROR ROW first. A subject this object was actually shared with is
    accepted for unsharing even when it no longer resolves through the
    identity seam -- an account hard-deleted after the share was made
    stands in here for the renamed-account / relocated-GUID / group-
    removed-from-the-store shapes the fix is aimed at, all of which leave
    `identity.normalize_subject` unable to name the account. Before this
    fix such a share could never be removed through the API:
    `_validate_subject` answered `400 subject does not name a known
    account` forever. The mirror-row check has to be on the RAW
    `ownership_share` table (what `service.list_share_rows` reads), not
    the store-merged `service.list_shares`."""
    w = harness.world
    ghost = harness.add_person(guid(0x50), "Ghost", "Gamma", "sales_readers")
    chart = harness.create_chart(w.ada, "unshare-unresolvable")
    harness.set_visibility(w.ada, chart, "shared")
    harness.share(w.ada, chart, ghost.ref)
    assert harness.decide(ghost, chart).verdict == ALLOW

    with harness.ctx():
        from superset import db, security_manager

        user = security_manager.get_user_by_id(ghost.id)
        db.session.delete(user)
        db.session.commit()
        assert security_manager.get_user_by_id(ghost.id) is None

    from superset_ownership import identity

    with harness.ctx():
        assert identity.normalize_subject(ghost.ref) is None, (
            "the account is gone; nothing resolves this reference anymore"
        )
        from superset_ownership import service

        rows = service.list_share_rows("chart", chart.id)
    assert [r["subject"] for r in rows] == [ghost.ref], (
        "the raw mirror row survives the account's deletion"
    )

    harness.revoke(w.ada, chart, ghost.ref, drain=False)
    with harness.ctx():
        from superset_ownership import service

        assert service.list_share_rows("chart", chart.id) == [], (
            "the mirror row is gone"
        )
    pending = [
        o
        for o in harness.outbox()
        if o["object"] == chart.obj and o["status"] == "pending"
    ]
    assert [o["op"] for o in pending] == ["revoke_subject"]

    harness.drain()
    assert (ghost.ref, "viewer", chart.obj) not in harness.authorizer.tuples


# --- 8c. BULK DATA ROUTE -----------------------------------------------------


@pytest.mark.parametrize("shape", ["form_data", "queries"])
def test_bulk_chart_data_route_is_guarded_like_the_tile(harness, shape):
    """Dashboard tiles and explore do not call GET /api/v1/chart/<pk>/data/;
    they POST a query context to /api/v1/chart/data and name the saved chart
    in `form_data.slice_id` or in `queries[].form_data.slice_id`. Superset
    gates that route on the DATASOURCE only, so the before_request guard has
    to recognise both shapes. Denied: the same 403 and the same error type
    as the pk route; admitted: rows."""
    w = harness.world
    chart = harness.create_chart(w.ada, f"bulk-{shape}")
    # Issue #93: `private` denies every non-owner unconditionally, so Ben's
    # share has to be on a `shared` chart to confer the access this test
    # checks below.
    harness.set_visibility(w.ada, chart, "shared")
    harness.share(w.ada, chart, w.ben.ref)
    body = harness.query_context(chart.id, shape=shape)

    # Dee holds the dataset grant and no share: denied, one check.
    r = harness.post(w.dee, "/api/v1/chart/data", body)
    assert r.status_code == 403
    assert harness.authorizer.checks == [("check", w.dee.ref, "viewer", chart.obj)]
    error = _data_error(r)
    assert error["error_type"] == "CHART_SECURITY_ACCESS_ERROR"
    assert error["extra"] == {
        "ownership": "private",
        "chart_id": chart.id,
        "slice_name": f"bulk-{shape}",
        "owners": ["Ada Test"],
    }
    # Cy holds no grant: denied before the store is asked.
    r = harness.post(w.cy, "/api/v1/chart/data", body)
    assert r.status_code == 403
    assert harness.authorizer.count("check") == 0
    assert _data_error(r)["error_type"] == "CHART_SECURITY_ACCESS_ERROR"
    # Ben, shared: one check, rows.
    r = harness.post(w.ben, "/api/v1/chart/data", body)
    assert r.status_code == 200, r.get_json()
    assert harness.authorizer.count("check") == 1
    assert r.get_json()["result"][0]["data"] == [
        {"region": "north", "amount": 1},
        {"region": "south", "amount": 2},
    ]
    # An ad-hoc query context with no saved chart is left to Superset.
    r = harness.post(w.dee, "/api/v1/chart/data", harness.query_context())
    assert r.status_code == 200
    assert harness.authorizer.count("check") == 0


# --- 8d. DEACTIVATED OWNER ---------------------------------------------------


def test_deactivated_owner_stops_existing_shares(harness):
    """An object whose owner's account is deactivated is unowned: it falls
    back to administrators, and the shares its owner granted stop conferring
    access -- the bypass hook refuses before it would ask the store."""
    w = harness.world
    olga = harness.add_person(guid(0x20), "Olga", "Gamma", "sales_readers")
    chart = harness.create_chart(olga, "deactivated-owner")
    # Issue #93: the share must confer access to prove the point below (that
    # deactivation, not the private label, is what withdraws it) -- a still
    # `private` chart would deny Ben unconditionally regardless of Olga's
    # activation state.
    harness.set_visibility(olga, chart, "shared")
    harness.share(olga, chart, w.ben.ref)
    assert harness.decide(w.ben, chart).verdict == ALLOW
    assert chart.id in harness.visible_ids(w.ben, "chart")

    harness.set_active(olga, False)
    d = harness.decide(w.ben, chart)
    assert d.verdict == DENY
    assert d.checks == [], "the share is not even consulted"
    assert d.names == (), "M-3: Ben's share is viewer, object_tenant not reached"
    assert chart.id not in harness.visible_ids(w.ben, "chart")
    assert harness.get(w.ben, f"/api/v1/chart/{chart.id}").status_code == 404
    r = harness.get(w.ben, f"/api/v1/chart/{chart.id}/data/")
    assert r.status_code == 403
    assert _data_error(r)["extra"]["ownership"] == "private"
    # An administrator still reaches it, and sees why nobody else does.
    d = harness.decide(w.admin, chart)
    assert (d.verdict, d.calls) == (ALLOW, ())
    r = harness.get(w.admin, f"/api/v1/ownership/chart/{chart.id}")
    assert r.status_code == 200
    assert r.get_json()["unowned"] is True
    assert r.get_json()["can_share"] is False
    # Reactivation restores the share; nothing was removed.
    harness.set_active(olga, True)
    assert harness.decide(w.ben, chart).verdict == ALLOW


# --- 9. STORE DOWN -----------------------------------------------------------


def test_store_unavailable_fails_closed_for_private_and_shared_not_public(harness):
    w = harness.world
    private = harness.create_chart(w.ada, "down-private")
    shared = harness.create_chart(w.ada, "down-shared")
    public = harness.create_chart(w.ada, "down-public")
    # Issue #93: `shared`'s own share has to actually confer access -- a
    # still-`private` chart denies Ben unconditionally, store up or down.
    harness.set_visibility(w.ada, shared, "shared")
    harness.share(w.ada, shared, w.ben.ref)
    harness.set_visibility(w.ada, public, "public")
    assert harness.decide(w.ben, shared).verdict == ALLOW, (
        "granted while the store is up"
    )

    # The real adapter's contract when OpenFGA cannot be reached: check
    # answers False, list_objects answers []. Nothing is granted.
    harness.authorizer.up = False
    assert harness.decide(w.ben, private).verdict == DENY
    assert harness.decide(w.ben, shared).verdict == DENY
    assert harness.visible_ids(w.ben, "chart") & {private.id, shared.id} == set()
    assert harness.get(w.ben, f"/api/v1/chart/{shared.id}").status_code == 404
    r = harness.get(w.ben, f"/api/v1/chart/{shared.id}/data/")
    assert r.status_code == 403
    assert _data_error(r)["extra"]["ownership"] == "private"
    # public: unaffected, and the owner: unaffected (no store call to fail)
    assert harness.decide(w.ben, public).verdict == ALLOW
    assert harness.get(w.ben, f"/api/v1/chart/{public.id}/data/").status_code == 200
    owner = harness.decide(w.ada, shared)
    assert (owner.verdict, owner.calls) == (ALLOW, ())

    # A backend that raises instead of answering. The decision must never
    # come out as ALLOW: an exception that reaches the store propagates out
    # of raise_for_access, and the data guard turns any failure into the
    # same 403.
    harness.authorizer.reset()
    harness.authorizer.raising = True

    # M-3 (review round 2, PR #100): the private hard-deny now costs
    # literally ZERO store calls -- before this fix, the editors resolver's
    # own `object_tenant` read ran UNCONDITIONALLY, even for a private
    # object carrying no editor share at all, and a raising backend
    # surfaced THAT read as this decision's ERROR. The fix makes the
    # private hard-deny exactly as robust as the owner fast path above: a
    # broken backend is never even asked, so it denies cleanly, not ERROR.
    d = harness.decide(w.ben, private)
    assert d.verdict == DENY, (
        "M-3: private costs zero store calls, so a raising backend cannot "
        "turn the hard-deny into an ERROR"
    )
    assert d.calls == ()
    r = harness.get(w.ben, f"/api/v1/chart/{private.id}/data/")
    assert r.status_code == 403

    # shared: Ben's own share is `viewer`, not `editor`, so M-3's lazy
    # object_tenant read is not reached here either -- but
    # `_non_owner_may_open_governed` still asks the store `check`
    # unconditionally, and a raising backend surfaces THAT as ERROR.
    d = harness.decide(w.ben, shared)
    assert d.verdict == ERROR
    assert isinstance(d.error, RuntimeError)
    r = harness.get(w.ben, f"/api/v1/chart/{shared.id}/data/")
    assert r.status_code == 403

    assert harness.decide(w.ben, public).verdict == ALLOW
    assert harness.decide(w.ada, shared).verdict == ALLOW


# --- I-2: the Identity seam gets the Directory seam's M-1 guard -------------


def test_share_route_never_leaks_a_raising_identity_plugins_text(harness):
    """I-2 (integrated review, `qa/reviews/review-plugin-pr90-integrated.md`):
    the Directory seam already fails closed on ANY exception a plug-in
    raises (M-1); the Identity seam had no such guard. `user_for_member_guid`
    -- consulted, after the direct account lookup resolves the subject, to
    confirm the forward mapping agrees (H-3/N-2) -- can raise: the shipped
    example's reverse hook is a DB query behind a lazily created table, so a
    first-boot permissions error is a realistic trigger; reproduced here
    with `RuntimeError("... password=hunter2")`, the review's own probe.

    An ADMITTED caller (the owner) must get the fixed "cannot verify"
    sentence, never the plug-in's own text -- it can carry a password, as
    here. A REFUSED caller must still get the SAME uniform 403 every other
    refused caller gets, not a 500: the share route runs a `store=False`
    subject probe BEFORE the admission check (so a refused caller cannot
    time whether the store settles their subject), and that pre-403 probe
    reaching a raising identity hook must not be able to reopen the gate
    the admission check is about to close.
    """
    from flask import current_app
    from superset_ownership import plugins
    from superset_ownership.identity import DefaultIdentity

    class _RaisingReverseHook(DefaultIdentity):
        protocol_version = 1

        def user_for_member_guid(self, member_guid):
            raise RuntimeError("ldap bind failed password=hunter2")

    tenant = guid(0x60)
    role = f"tenant_{tenant}"
    owner = harness.add_person(guid(0x61), "I2Owner", "Gamma", "sales_readers", role)
    outsider = harness.add_person(
        guid(0x62), "I2Outsider", "Gamma", "sales_readers", role
    )
    target = harness.add_person(guid(0x63), "I2Target", "Gamma", "sales_readers", role)
    chart = harness.create_chart(owner, "i2-identity-guard")
    subject = target.ref
    path = f"/api/v1/ownership/chart/{chart.id}/shares"

    with harness.ctx():
        reg = current_app.extensions[plugins.EXT_KEY]
        original_identity = reg.identity
        reg.identity = _RaisingReverseHook()
    try:
        # Admitted: owner ground needs no identity resolution of its own,
        # so admission succeeds; the SUBJECT then cannot be verified.
        r = harness.post(owner, path, {"subject": subject, "role": "viewer"})
        assert r.status_code == 400, (r.status_code, r.get_json())
        body_text = r.get_data(as_text=True)
        assert "hunter2" not in body_text
        assert "ldap bind failed" not in body_text
        assert "cannot verify the subject" in r.get_json()["message"]

        # Refused: the outsider holds no ground on this object at all --
        # the SAME uniform 403 as w/o any identity failure in the mix.
        from superset_ownership.api import _SHARES_403

        r = harness.post(outsider, path, {"subject": subject, "role": "viewer"})
        assert r.status_code == 403, (r.status_code, r.get_json())
        body_text = r.get_data(as_text=True)
        assert "hunter2" not in body_text
        assert "ldap bind failed" not in body_text
        assert r.get_json()["message"] == _SHARES_403
    finally:
        with harness.ctx():
            current_app.extensions[plugins.EXT_KEY].identity = original_identity


def test_share_route_classifies_a_raising_hook_reached_from_normalize_subject(harness):
    """R-1 (`qa/reviews/review-plugin-pr90-integrated.md`, "Targeted
    re-check (fa3a5c6)"): the SAME raising reverse hook as the test above,
    but for a subject whose reference names no real Superset account by
    username or email at all -- so `identity.py`'s own
    `_default_normalize_subject` has to consult the reverse hook (H-3)
    just to resolve the subject, rather than the post-normalize
    forward-mapping check the previous test exercises (there, the
    subject's ref WAS the target's username, so `normalize_subject`
    resolved it without ever calling the hook).

    Before this fix, that inner branch swallowed the raise exactly like
    "no candidate" and answered `400 subject does not name a known
    account` -- indistinguishable from a caller who simply mistyped a
    GUID. An admitted owner must instead get the same fixed "cannot
    verify the subject" sentence the outer guard already produces for a
    hook that raises elsewhere, and it must still never carry the plug-in's
    own text.
    """
    from flask import current_app
    from superset_ownership import plugins
    from superset_ownership.identity import DefaultIdentity

    class _RaisingReverseHook(DefaultIdentity):
        protocol_version = 1

        def user_for_member_guid(self, member_guid):
            raise RuntimeError("ldap bind failed password=hunter2")

    tenant = guid(0x70)
    role = f"tenant_{tenant}"
    owner = harness.add_person(guid(0x71), "R1Owner", "Gamma", "sales_readers", role)
    chart = harness.create_chart(owner, "r1-normalize-subject-guard")
    # No account anywhere carries this GUID as a username or an email, so
    # `_default_normalize_subject` cannot resolve it directly and must
    # fall through to `user_for_member_guid` -- which raises.
    subject = f"user:{guid(0x72)}"
    path = f"/api/v1/ownership/chart/{chart.id}/shares"

    with harness.ctx():
        reg = current_app.extensions[plugins.EXT_KEY]
        original_identity = reg.identity
        reg.identity = _RaisingReverseHook()
    try:
        r = harness.post(owner, path, {"subject": subject, "role": "viewer"})
        assert r.status_code == 400, (r.status_code, r.get_json())
        body_text = r.get_data(as_text=True)
        assert "hunter2" not in body_text
        assert "ldap bind failed" not in body_text
        message = r.get_json()["message"]
        assert "cannot verify the subject" in message
        assert "RuntimeError" in message
        assert "does not name a known account" not in message
    finally:
        with harness.ctx():
            current_app.extensions[plugins.EXT_KEY].identity = original_identity


def test_subjects_route_never_leaks_a_raising_tenant_guid(harness):
    """I-2: `/subjects` resolves the CALLER's own tenant through the
    Identity seam (`resolve_tenant_guid`) before it ever reaches the
    Directory -- a raising `tenant_guid` (an LDAP/attribute lookup gone
    wrong) must answer the fixed "cannot verify your tenant" sentence,
    never the plug-in's own text, and must not be a bare 500."""
    from flask import current_app
    from superset_ownership import plugins
    from superset_ownership.identity import DefaultIdentity

    class _RaisingTenantGuid(DefaultIdentity):
        protocol_version = 1

        def tenant_guid(self, user):
            raise RuntimeError("ldap query failed password=hunter2")

    tenant = guid(0x64)
    role = f"tenant_{tenant}"
    caller = harness.add_person(guid(0x65), "I2Subjects", "Gamma", role)

    with harness.ctx():
        reg = current_app.extensions[plugins.EXT_KEY]
        original_identity = reg.identity
        reg.identity = _RaisingTenantGuid()
    try:
        r = harness.get(caller, "/api/v1/ownership/subjects")
        assert r.status_code == 400, (r.status_code, r.get_json())
        body_text = r.get_data(as_text=True)
        assert "hunter2" not in body_text
        assert "ldap query failed" not in body_text
        assert "cannot verify your tenant" in r.get_json()["message"]
    finally:
        with harness.ctx():
            current_app.extensions[plugins.EXT_KEY].identity = original_identity


def test_editor_share_is_served_from_the_mirror_while_the_store_is_down(harness):
    """An `editor` share is honoured by Superset's own is_editor through the
    editors resolver, which reads this module's share table (written in the
    same transaction as the outbox row, so never staler than the store).
    Documented here because it is the one relation that survives a store
    outage; a viewer share does not (above)."""
    w = harness.world
    chart = harness.create_chart(w.ada, "down-editor")
    # Issue #93: a still-`private` chart would deny Ben with ZERO store
    # calls (the hard deny), never reaching the `check` this test asserts
    # was made and refused -- `shared` is what puts the bypass hook on the
    # store-asking path in the first place, which is the property this test
    # is actually about (is_editor admitting from the mirror once the
    # bypass hook itself is refused).
    harness.set_visibility(w.ada, chart, "shared")
    harness.share(w.ada, chart, w.ben.ref, "editor")
    harness.authorizer.up = False
    d = harness.decide(w.ben, chart)
    assert d.verdict == ALLOW
    assert d.checks == [("check", w.ben.ref, "viewer", chart.obj)], (
        "the bypass hook asked and was refused; is_editor admitted from the mirror"
    )
    assert d.names == ("check", "object_tenant"), "the resolver's tenant read"
    harness.authorizer.up = True
    # Revoking the share closes it immediately, store or no store.
    harness.revoke(w.ada, chart, w.ben.ref, drain=False)
    harness.authorizer.up = False
    assert harness.decide(w.ben, chart).verdict == DENY
    assert harness.decide(w.ben, chart).verdict == DENY


# --- 10. SHARING FIELDS ------------------------------------------------------


@pytest.mark.parametrize("visibility", ["shared"])
@pytest.mark.parametrize("asset_type", ASSET_TYPES)
def test_sharing_field_change_by_a_non_owner_editor_is_undone_via_the_api(
    harness, asset_type, visibility
):
    """Ben holds an `editor` share, so Superset's own update command lets him
    PUT the object. `viewers` and `editors` are ordinary fields on that
    route. Stripping the sentinel would open the object to everyone with the
    dataset grant; adding a subject would be a grant outside the ownership
    model. Both are undone by the flush guard inside the same transaction
    and recorded as access_corrected; the rename itself goes through. The
    guard must hold for both asset types.

    H1 (review round 1, issue #93): `shared` only, not both of `GOVERNED`
    -- an editor share on a `private` object now confers no edit access at
    all (`test_editor_share_on_private_confers_nothing`), so Ben cannot
    reach this test's own premise ("Ben holds an editor share, so Superset's
    own update command lets him PUT the object") on a `private` one; the
    PUT itself would now be refused before the flush guard this test is
    about ever runs."""
    w = harness.world
    ref = _governed(harness, asset_type, visibility, "guard")
    harness.share(w.ada, ref, w.ben.ref, "editor")
    cy_subject = harness.subject_id(w.cy)
    sentinel = [SENTINEL]
    assert harness.viewers(ref) == sentinel
    route = f"/api/v1/{asset_type}/{ref.id}"
    title = "slice_name" if asset_type == "chart" else "dashboard_title"

    # strip the denial
    r = harness.put(w.ben, route, {"viewers": [], title: "stripped?"})
    assert r.status_code == 200, r.get_json()
    assert harness.viewers(ref) == sentinel, "re-armed in the same transaction"
    assert harness.detail(w.cy, ref).status_code == 404
    assert harness.decide(w.cy, ref).verdict == DENY
    (event,) = harness.events("ownership.access_corrected")
    assert event["before"]["enforced"] is False
    assert event["after"] == {
        "enforced": True,
        "visibility": visibility,
        "direct_viewers": 0,
        "direct_editors": 0,
    }
    assert event["object"] == {"type": asset_type, "id": ref.id, "uuid": ref.uuid}
    assert event["actor"]["superset_id"] == w.ben.id, "the editor who tried is named"
    r = harness.detail(w.ada, ref)
    assert r.get_json()["result"][title] == "stripped?", "the legitimate write stands"

    # widen: a direct viewer grant to Cy, who has no share (the PUT replaces
    # the collection, so this both drops the sentinel and adds a viewer)
    harness.audit.clear()
    r = harness.put(w.ben, route, {"viewers": [cy_subject]})
    assert r.status_code == 200, r.get_json()
    assert harness.viewers(ref) == sentinel, "the extra viewer is stripped"
    (event,) = harness.events("ownership.access_corrected")
    assert event["before"] == {
        "enforced": False,
        "direct_viewers": 1,
        "direct_editors": 0,
    }

    # widen further: a direct editor grant
    harness.audit.clear()
    r = harness.put(w.ben, route, {"editors": [cy_subject]})
    assert r.status_code == 200, r.get_json()
    assert f"user:{w.cy.username}" not in harness.editors(ref)
    (event,) = harness.events("ownership.access_corrected")
    assert event["before"]["direct_editors"] == 1
    assert harness.decide(w.cy, ref).verdict == DENY

    # A viewer share is not edit: the same PUT by a viewer is refused outright.
    viewer_only = _governed(harness, asset_type, visibility, "guard-viewer")
    harness.share(w.ada, viewer_only, w.ben.ref, "viewer")
    r = harness.put(w.ben, f"/api/v1/{asset_type}/{viewer_only.id}", {"viewers": []})
    assert r.status_code == 403
    assert harness.viewers(viewer_only) == sentinel

    # And the ownership routes themselves: an editor may not manage sharing.
    r = harness.post(
        w.ben,
        f"/api/v1/ownership/{asset_type}/{ref.id}/shares",
        {"subject": w.cy.ref, "role": "viewer"},
    )
    assert r.status_code == 403
    r = harness.put(
        w.ben,
        f"/api/v1/ownership/{asset_type}/{ref.id}/visibility",
        {"visibility": "public"},
    )
    assert r.status_code == 403
    assert harness.row(ref).visibility == visibility


# --- 11. TRANSFER ------------------------------------------------------------


def test_transfer_re_checks_the_recipient_baseline_on_the_real_route(harness):
    """The refusal matrix (wording, audit, unverifiable, nothing written) is
    test_transfer.py's; this is the one end-to-end run on the real
    application: a recipient without the dataset grant is refused, one with
    it is accepted, and the outbox carries the handover."""
    w = harness.world
    chart = harness.create_chart(w.ada, "transfer")
    # Issue #93: without this, the previous owner (Ada, once she is no
    # longer owner) is denied by the private hard-deny with ZERO store
    # calls, not by the ordinary one-`check` non-owner path this test's
    # final assertion below is about -- `shared` puts her on the path
    # `raise_for_access_bypass` actually asks the store on.
    harness.set_visibility(w.ada, chart, "shared")
    owner_route = f"/api/v1/ownership/chart/{chart.id}/owner"
    r = harness.put(w.ada, owner_route, {"subject": w.cy.ref})
    assert r.status_code == 409
    assert "does not have access to this chart's dataset" in r.get_json()["message"]
    assert harness.row(chart).owner_user_id == w.ada.id
    (event,) = harness.events("ownership.owner_refused")
    assert event["attempted"] == {"owner_user_id": w.cy.id, "reason": "missing_grant"}

    r = harness.put(w.ada, owner_route, {"subject": w.ben.ref})
    assert r.status_code == 200, r.get_json()
    assert harness.row(chart).owner_user_id == w.ben.id
    harness.drain()
    assert (w.ben.ref, "owner", chart.obj) in harness.authorizer.tuples
    assert (w.ada.ref, "owner", chart.obj) not in harness.authorizer.tuples
    assert harness.decide(w.ben, chart).calls == (), "the new owner is on the fast path"
    d = harness.decide(w.ada, chart)
    assert len(d.checks) == 1, (
        "the previous owner is off the fast path: a non-owner now"
    )


@pytest.mark.parametrize("asset_type", ASSET_TYPES)
def test_transfer_removes_the_previous_owner_from_native_editors(harness, asset_type):
    """issue #80: a stock create records the creator in the object's own
    `editors`; a transfer must move that collection in the same transaction
    as the ownership row and the store, not leave it for the flush guard's
    next unrelated write to notice. Once moved, the previous owner is off
    every relation Superset's own gates enforce: open, list and edit."""
    w = harness.world
    ref = harness.create(asset_type, w.ada, f"transfer-editors-{asset_type}")
    r = harness.put(
        w.ada, f"/api/v1/ownership/{asset_type}/{ref.id}/owner", {"subject": w.ben.ref}
    )
    assert r.status_code == 200, r.get_json()
    harness.drain()
    assert f"user:{w.ada.username}" not in harness.editors(ref)
    assert f"user:{w.ben.username}" in harness.editors(ref), (
        "the new owner takes the row"
    )

    # open: is_editor no longer admits the previous owner, and neither does
    # anything else -- they are a plain non-owner, non-shared caller now.
    assert harness.decide(w.ada, ref).verdict == DENY
    assert harness.detail(w.ada, ref).status_code == 404

    # list: EXTRA_ACCESS_QUERY_FILTERS no longer surfaces it for them.
    assert ref.id not in harness.visible_ids(w.ada, asset_type)

    # edit: the native write path is closed too -- not just the ownership
    # API's own view of who may manage the object.
    r = harness.rename(w.ada, ref, "edited by the previous owner")
    assert r.status_code in (403, 404), r.get_json()


@pytest.mark.parametrize("asset_type", ASSET_TYPES)
def test_transfer_keeps_the_previous_owner_editing_via_an_explicit_share(
    harness, asset_type
):
    """The removal a transfer performs is narrow: only the previous owner's
    NATIVE editors row. Editor shares are not native rows at all (review
    H-1/H-2, round 1: the native `editors` collection is the owner ONLY --
    an editor share is honoured dynamically, through
    EXTRA_EDITORS_RESOLVER, never materialised there), so Ada's `editor`
    share on the object she just transferred away is not something a
    transfer could revoke even if it wanted to: it keeps working, exactly
    as it did before this collection existed, and exactly as it does for
    any other share holder."""
    w = harness.world
    ref = harness.create(asset_type, w.ada, f"transfer-kept-share-{asset_type}")
    # H1 (review round 1, issue #93): a share write is refused outright on a
    # still-private object, so the object has to be `shared` before Ada can
    # share it at all.
    harness.set_visibility(w.ada, ref, "shared")
    # Ada, still the owner at this point, shares the object with herself --
    # the self-grant restriction is scoped to the manage-sharing permission
    # ground, not to the owner (see `_self_grant_under_manage_permission`).
    harness.share(w.ada, ref, w.ada.ref, "editor")
    r = harness.put(
        w.ada, f"/api/v1/ownership/{asset_type}/{ref.id}/owner", {"subject": w.ben.ref}
    )
    assert r.status_code == 200, r.get_json()
    harness.drain()
    assert f"user:{w.ada.username}" not in harness.editors(ref), (
        "not a native row -- the share is dynamic, not materialised (H-1/H-2)"
    )
    assert harness.decide(w.ada, ref).verdict == ALLOW
    r = harness.rename(w.ada, ref, "edited via the kept share")
    assert r.status_code == 200, r.get_json()


@pytest.mark.parametrize("asset_type", ASSET_TYPES)
def test_transfer_preserves_unrelated_native_editors(harness, asset_type):
    """REWRITTEN (review H-1, round 1): the original version of this test
    shared the object with Cy -- the harness's GRANT-LESS user -- and then
    asserted Cy ended up a native editor after the transfer. That was not
    "an unrelated editor survives a transfer", it was H-1 itself: a
    grant-less share-holder admitted the moment anything wrote the object,
    bypassing the dataset symmetry `hooks.editor_subject_ids` applies for
    the dynamic resolver. `is_editor` trusts native `editors` outright, so
    a share must never be materialised into it (see
    service.sync_native_editors).

    The invariant actually worth locking in: a transfer's narrow removal
    (only the named previous owner) does not disturb an unrelated editor
    SHARE either -- rewritten around Ben, who holds the dataset grant, so
    the share keeps working and is not a regression waiting to happen. His
    presence is asserted through his own working `rename`, not through a
    row in `editors` (there is not one, and must not be)."""
    w = harness.world
    ref = harness.create(asset_type, w.ada, f"transfer-unrelated-{asset_type}")
    # H1 (review round 1, issue #93): a share write is refused outright on a
    # still-private object.
    harness.set_visibility(w.ada, ref, "shared")
    harness.share(w.ada, ref, w.ben.ref, "editor")
    assert harness.rename(w.ben, ref, "seed a write via the share").status_code == 200
    assert f"user:{w.ben.username}" not in harness.editors(ref), (
        "never materialised, before the transfer either"
    )

    r = harness.put(
        w.ada, f"/api/v1/ownership/{asset_type}/{ref.id}/owner", {"subject": w.dee.ref}
    )
    assert r.status_code == 200, r.get_json()
    harness.drain()
    editors = harness.editors(ref)
    assert f"user:{w.ada.username}" not in editors, "the previous owner is removed"
    assert f"user:{w.dee.username}" in editors, "the new owner is added"
    assert f"user:{w.ben.username}" not in editors, "still not a native row"

    assert harness.decide(w.ben, ref).verdict == ALLOW, (
        "the unrelated share still works"
    )
    r = harness.rename(w.ben, ref, "edited via the share after the transfer")
    assert r.status_code == 200, r.get_json()


@pytest.mark.parametrize("asset_type", ASSET_TYPES)
def test_claim_adds_the_claimer_to_native_editors(harness, asset_type):
    """Same invariant as a transfer, the other route that moves ownership:
    POST .../claim makes the caller the owner, and the caller a native
    editor in the same transaction."""
    w = harness.world
    ref = _governed(harness, asset_type, "public", "claim-editors")
    assert harness.row(ref).owner_user_id == w.ada.id

    # Released first so it is claimable: a claim by someone who is not
    # already the owner, a tenant administrator or an admin is refused (see
    # test_transfer.py's matrix), and releasing keeps this test about the
    # editors invariant rather than the claim eligibility one.
    r = harness.put(
        w.ada, f"/api/v1/ownership/{asset_type}/{ref.id}/owner", {"subject": None}
    )
    assert r.status_code == 200, r.get_json()
    assert f"user:{w.ada.username}" not in harness.editors(ref)

    # The admin ground, not owner/tenant-admin: admits every action and
    # needs no tenant/role setup of its own, keeping this test about the
    # editors invariant a claim enforces.
    r = harness.post(w.admin, f"/api/v1/ownership/{asset_type}/{ref.id}/claim")
    assert r.status_code == 200, r.get_json()
    harness.drain()
    assert harness.row(ref).owner_user_id == w.admin.id
    assert f"user:{w.admin.username}" in harness.editors(ref), (
        "the claimer takes the row"
    )
    assert harness.decide(w.admin, ref).verdict == ALLOW
    r = harness.rename(w.admin, ref, "edited after claiming")
    assert r.status_code == 200, r.get_json()


def test_public_draft_stays_visible_to_its_owner_after_transfer(harness):
    """F-3 (qa/design/directory-hook/../ivanti-acceptance-run.md): Public
    hands enforcement back to Superset's own rules, which hide an
    unpublished (Draft) dashboard from anyone who is not a native
    owner/editor. A transferred-to owner who never picked up a native
    editors row could go Public on their own Draft dashboard and 404
    themselves -- `can_manage: true` and no way to see it. Dashboard-only:
    "published" (Draft vs. not) is a dashboard concept, and it is dashboard
    13 in the acceptance run. Same root cause as the rest of this section;
    the fix is the same helper."""
    w = harness.world
    ref = harness.create_dashboard(w.ada, title="draft-public")
    r = harness.put(w.ada, f"/api/v1/dashboard/{ref.id}", {"published": False})
    assert r.status_code == 200, r.get_json()

    r = harness.put(
        w.ada, f"/api/v1/ownership/dashboard/{ref.id}/owner", {"subject": w.ben.ref}
    )
    assert r.status_code == 200, r.get_json()
    harness.drain()

    r = harness.set_visibility(w.ben, ref, "public")
    assert r.status_code == 200, r.get_json()
    assert harness.row(ref).visibility == "public"

    assert harness.decide(w.ben, ref).verdict == ALLOW
    assert harness.detail(w.ben, ref).status_code == 200
    # `visible_ids` is this module's OWN extra filter, which deliberately
    # excludes public rows (service.owned_or_shared_rows: "Superset's own
    # default query filters already surface public dashboards/charts; this
    # module has nothing to add there") -- for a Draft/public object it is
    # Superset's STOCK list filter that must see the native editors row,
    # so the real list route (`listed_ids`) is what F-3 was actually about
    # ("it leaves her list").
    assert ref.id in harness.listed_ids(w.ben, "dashboard")


@pytest.mark.parametrize("asset_type", ASSET_TYPES)
def test_grant_less_editor_share_holder_stays_denied_after_owner_write(
    harness, asset_type
):
    """H-1 (review round 1), the reviewer's own probe. Cy holds NO dataset
    grant -- "a share must never let them in" (World docstring) -- and an
    `editor` share on the object. Before this fix, any unrelated owner
    write (here, Ada renaming the object) ran the flush guard, which used
    to materialise Cy into native `editors`; `is_editor` trusts that
    collection outright, with no further check, so Cy was admitted:
    `decide` allow, `detail` 200, `rename` 200 -- despite `decide` denying
    them outright before that write. The fix (service.sync_native_editors:
    owner only) means the write changes nothing about Cy's standing."""
    w = harness.world
    # A chart (with its dataset), and a dashboard that actually holds one:
    # an empty dashboard has no dataset to check Cy against at all, and the
    # dataset-symmetry trim this probe is about has nothing to vacuously
    # deny -- the same reason test_shared_chart_does_not_widen_dataset_access
    # attaches a chart to its dashboard.
    chart = harness.create_chart(w.ada, "h1-grant-less-chart")
    dash = harness.create_dashboard(w.ada, chart, title="h1-grant-less-dash")
    ref = chart if asset_type == "chart" else dash
    # H1 (review round 1, issue #93): a share write is refused outright on a
    # still-private object.
    harness.set_visibility(w.ada, ref, "shared")
    harness.share(w.ada, ref, w.cy.ref, "editor")
    assert harness.decide(w.cy, ref).verdict == DENY, "denied before the write too"

    r = harness.rename(w.ada, ref, "an unrelated owner write")
    assert r.status_code == 200, r.get_json()
    harness.drain()

    assert harness.editors(ref) == [f"user:{w.ada.username}"], "owner only, still"
    assert harness.decide(w.cy, ref).verdict == DENY
    assert harness.detail(w.cy, ref).status_code == 404
    r = harness.rename(w.cy, ref, "edited without the grant")
    assert r.status_code in (403, 404), r.get_json()


@pytest.mark.parametrize("asset_type", ASSET_TYPES)
def test_revoking_an_editor_share_revokes_native_edit_immediately(harness, asset_type):
    """H-2 (review round 1), the reviewer's own probe. Before this fix, once
    a share-holder had been written into native `editors` by ANY unrelated
    owner write (here, Ada renaming the object after sharing it with Ben),
    revoking the share removed the mirror row and queued the tuple delete
    but never touched `asset.editors` -- so `is_editor` kept admitting Ben
    after the revoke, with no share left to justify it. The fix means
    there is no native row a revoke could fail to undo: Ben's edit right
    was always dynamic (EXTRA_EDITORS_RESOLVER), so revoking the share ends
    it the same way it always ended `decide`."""
    w = harness.world
    ref = _governed(harness, asset_type, "shared", "h2-revoke-editor-share")
    harness.share(w.ada, ref, w.ben.ref, "editor")
    assert harness.rename(w.ada, ref, "an unrelated owner write").status_code == 200
    harness.drain()
    assert harness.decide(w.ben, ref).verdict == ALLOW
    assert harness.rename(w.ben, ref, "edited via the share").status_code == 200
    assert f"user:{w.ben.username}" not in harness.editors(ref), "never materialised"

    harness.revoke(w.ada, ref, w.ben.ref)
    assert harness.decide(w.ben, ref).verdict == DENY
    r = harness.rename(w.ben, ref, "edited after the share was revoked")
    assert r.status_code in (403, 404), r.get_json()


def test_sync_native_editors_makes_no_store_or_identity_calls(harness):
    """M-2 (review round 1): before H-1/H-2, the flush guard's editors
    correction resolved editor shares from INSIDE `before_flush` -- an
    OpenFGA `object_tenant` read plus an identity-plug-in call per share,
    on every write to a governed object (Superset's own dashboard/chart
    saves included, not only this module's routes), and a fail-open
    tenant answer on an unreadable store could get written into native
    `editors` and stick there. Gone with the owner-only sync: it only ever
    reads `row.owner_user_id`, so calling it -- strict mode, the guard's
    own mode, included -- costs nothing from the store or the identity
    plug-in, however many editor shares the object has."""
    from superset.models.slice import Slice

    w = harness.world
    ref = _governed(harness, "chart", "private", "m2-no-store-calls")
    # H1 (review round 1, issue #93): a share write is refused outright on a
    # `private` object, so the editor shares this test needs -- on a
    # still-private object, deliberately -- are planted directly at the
    # service layer (a stray/out-of-band grant stands in for the route).
    with harness.ctx():
        from superset import db
        from superset_ownership import service

        service.add_share("chart", ref.id, ref.uuid, w.ben.ref, "editor")
        service.add_share("chart", ref.id, ref.uuid, w.dee.ref, "editor")
        db.session.commit()
    harness.drain()
    row = harness.row(ref)
    with harness.ctx():
        from superset import db
        from superset_ownership import service

        asset = db.session.get(Slice, ref.id)
        harness.authorizer.calls.clear()
        changed = service.sync_native_editors(
            asset, row, strict=True, create_missing=False
        )
        assert changed is False, "nothing to change: owner-only, and Ada is the owner"
        assert harness.authorizer.calls == []


def test_transfer_rolls_back_when_the_native_editors_sync_raises(harness, monkeypatch):
    """M-1 (review round 1): `service.sync_native_editors` now runs INSIDE
    the transfer's transaction, before the commit -- a failure there rolls
    the whole write back rather than leaving a committed ownership change
    next to a native `editors` collection that still names the previous
    owner (the earlier, best-effort version's premise -- "the ownership row
    and the store tuple are already written" -- did not hold on any of
    these routes: `db.session.commit()` is always after this call). The
    caller reads a fixed sentence, never the exception text, and the
    ownership row is unchanged."""
    from superset_ownership import service

    w = harness.world
    ref = _governed(harness, "chart", "shared", "m1-sync-raises")

    def _raise(*args, **kwargs):
        raise RuntimeError("boom -- disk full, /var/lib/postgresql/data")

    monkeypatch.setattr(service, "sync_native_editors", _raise)
    r = harness.put(
        w.ada, f"/api/v1/ownership/chart/{ref.id}/owner", {"subject": w.ben.ref}
    )
    assert r.status_code == 503, r.get_json()
    body_text = r.get_data(as_text=True)
    assert "boom" not in body_text
    assert "disk full" not in body_text
    assert "nothing was changed" in r.get_json()["message"]
    monkeypatch.undo()

    assert harness.row(ref).owner_user_id == w.ada.id, "the transfer was rolled back"
    assert harness.editors(ref) == [f"user:{w.ada.username}"], (
        "native editors untouched"
    )
    assert harness.decide(w.ada, ref).verdict == ALLOW
    assert harness.decide(w.ben, ref).verdict == DENY


def test_tenant_admin_claiming_a_rowless_draft_dashboard_stays_visible_to_them(harness):
    """A pre-module Draft dashboard -- no ownership row at all yet -- taken
    by a tenant administrator. Once by adoption through the visibility
    route (M-3, review round 1); now by a CLAIM, since a tenant
    administrator does not change the sharing of an object they do not own
    and the visibility write is refused. Either way the taker must land in
    the object's native editors (F-3, service.sync_native_editors) or they
    404 themselves on their own Draft."""
    import sqlalchemy as sa

    from superset_ownership import service
    from superset_ownership.db import ownership_object

    tenant = guid(0x53)
    role = f"tenant_{tenant}"
    ana = harness.add_person(guid(0x54), "M3Ana", "Gamma", "sales_readers", role)
    tam = harness.add_person(guid(0x55), "M3Tam", "Gamma", "sales_readers", role)
    harness.authorizer.make_tenant_administrator(tam.ref, tenant)

    ref = harness.create_dashboard(ana, title="rowless-draft")
    r = harness.put(ana, f"/api/v1/dashboard/{ref.id}", {"published": False})
    assert r.status_code == 200, r.get_json()
    harness.drain()

    # Simulate the pre-module shape M-3 is about: no ownership row at all,
    # not merely an ownerless one (that is the `elif` branch, already
    # covered by F-3's own test).
    with harness.ctx():
        from superset import db

        db.session.execute(
            sa.delete(ownership_object).where(
                ownership_object.c.asset_type == ref.asset_type,
                ownership_object.c.object_id == ref.id,
            )
        )
        db.session.commit()
        service.invalidate_all()
    assert harness.row(ref) is None, "no row at all -- the `row is None` branch"

    # A tenant administrator does not change sharing on an object they do
    # not own -- not even a rowless one: the visibility write is refused,
    # and the way in is to take the object (claim, "Assign owner to me").
    r = harness.put(
        tam,
        f"/api/v1/ownership/dashboard/{ref.id}/visibility",
        {"visibility": "public"},
    )
    assert r.status_code == 403, r.get_json()
    assert harness.row(ref) is None, "nothing adopted on a refusal"
    r = harness.post(tam, f"/api/v1/ownership/dashboard/{ref.id}/claim", {})
    assert r.status_code == 200, r.get_json()
    assert harness.row(ref).owner_user_id == tam.id
    assert f"user:{tam.username}" in harness.editors(ref), "the claimer takes the row"
    harness.set_visibility(tam, ref, "public")
    assert harness.row(ref).visibility == "public"

    assert harness.decide(tam, ref).verdict == ALLOW
    assert harness.detail(tam, ref).status_code == 200
    assert ref.id in harness.listed_ids(tam, "dashboard")


def test_admin_adopting_a_rowless_draft_dashboard_stays_visible_to_them(harness):
    """The `row is None` adoption branch of `_set_asset_visibility` (M-3),
    kept for a Superset admin: setting a pre-module Draft dashboard Public
    assigns the admin as owner and lands them in its native editors (F-3),
    in one write. Ada's dashboard carries no tenant (this world's people
    have none), so the tenantless admin is not refused as a mover."""
    import sqlalchemy as sa

    from superset_ownership import service
    from superset_ownership.db import ownership_object

    w = harness.world
    ref = harness.create_dashboard(w.ada, title="rowless-draft-admin")
    r = harness.put(w.ada, f"/api/v1/dashboard/{ref.id}", {"published": False})
    assert r.status_code == 200, r.get_json()
    harness.drain()
    with harness.ctx():
        from superset import db

        db.session.execute(
            sa.delete(ownership_object).where(
                ownership_object.c.asset_type == ref.asset_type,
                ownership_object.c.object_id == ref.id,
            )
        )
        db.session.commit()
        service.invalidate_all()
    assert harness.row(ref) is None

    harness.set_visibility(w.admin, ref, "public")
    assert harness.row(ref).owner_user_id == w.admin.id, "adopted"
    assert harness.row(ref).visibility == "public"
    assert f"user:{w.admin.username}" in harness.editors(ref), (
        "the adopter takes the row"
    )
    assert harness.decide(w.admin, ref).verdict == ALLOW
    assert ref.id in harness.listed_ids(w.admin, "dashboard")


# --- 12. DELETE --------------------------------------------------------------


@pytest.mark.parametrize("visibility", GOVERNED)
@pytest.mark.parametrize("asset_type", ASSET_TYPES)
def test_delete_removes_tuples_through_the_outbox(
    harness, monkeypatch, asset_type, visibility
):
    """A hard delete goes through the session (`db.session.delete`, what the
    DELETE route does with SOFT_DELETE off). The flush guard sees it, removes
    the local rows and queues purge_object in the same transaction; the drain
    delivers it and the store forgets the object. For every asset type the
    guard recognises, at either governed visibility."""
    from superset.extensions import feature_flag_manager

    w = harness.world
    ref = _governed(harness, asset_type, visibility, "delete")
    # H1 (review round 1, issue #93): a share WRITE is refused outright on a
    # still-private object, so a `private` row's viewer tuple is planted
    # directly at the service layer (a stray/out-of-band grant stands in
    # for the route) -- the purge below has to clear it regardless of how
    # it got there, at either governed visibility.
    with harness.ctx():
        from superset import db
        from superset_ownership import service

        service.add_share(asset_type, ref.id, ref.uuid, w.ben.ref, "viewer")
        db.session.commit()
    harness.drain()
    before = harness.authorizer.tuples_on(ref.obj)
    assert {t[1] for t in before} == {"owner", "viewer"}

    monkeypatch.setitem(feature_flag_manager._feature_flags, "SOFT_DELETE", False)
    r = harness.delete(w.ada, f"/api/v1/{asset_type}/{ref.id}")
    assert r.status_code == 200, r.get_json()
    assert harness.row(ref) is None, "ownership row gone with the object"
    (purge,) = [
        o
        for o in harness.outbox()
        if o["op"] == "purge_object" and o["object"] == ref.obj
    ]
    assert purge["status"] == "pending"
    assert harness.authorizer.tuples_on(ref.obj) == before, (
        "the store lags until the drain"
    )
    assert harness.authorizer.count("purge_object") == 0

    stats = harness.drain()
    assert stats["delivered"] >= 1
    assert (stats["failed"], stats["dead"]) == (0, 0)
    assert harness.authorizer.count("purge_object") == 1
    assert harness.authorizer.tuples_on(ref.obj) == set(), (
        "every tuple on the object is gone"
    )
    assert [o["status"] for o in harness.outbox() if o["object"] == ref.obj][
        -1
    ] == "done"


def test_soft_delete_keeps_the_relationships(harness):
    """Under SOFT_DELETE (this build's default) DELETE archives: the object
    still exists and so must its ownership row and tuples."""
    w = harness.world
    chart = harness.create_chart(w.ada, "archive")
    harness.set_visibility(w.ada, chart, "shared")
    harness.share(w.ada, chart, w.ben.ref)
    before = harness.authorizer.tuples_on(chart.obj)
    r = harness.delete(w.ada, f"/api/v1/chart/{chart.id}")
    assert r.status_code == 200, r.get_json()
    assert harness.row(chart) is not None
    assert harness.authorizer.tuples_on(chart.obj) == before
    purges = [
        o
        for o in harness.outbox()
        if o["op"] == "purge_object" and o["object"] == chart.obj
    ]
    assert purges == []


def _share_subjects(harness, ref) -> list[str]:
    """The mirror rows on an object, by subject."""
    import sqlalchemy as sa

    from superset_ownership.db import ownership_share

    with harness.ctx():
        from superset import db

        return [
            r["subject"]
            for r in db.session.execute(
                sa.select(ownership_share)
                .where(
                    ownership_share.c.asset_type == ref.asset_type,
                    ownership_share.c.object_id == ref.id,
                )
                .order_by(ownership_share.c.id)
            ).mappings()
        ]


def test_permanent_delete_of_an_archived_object_removes_tuples(harness):
    """`POST /<uuid>/purge` -- and the retention sweep behind it -- hard-deletes
    with a Core `sa.delete`, which never puts the object in `session.deleted`:
    the flush guard's `_collect_deleted` did not run, and the ownership row,
    the share rows and every tuple were orphaned for good (issue #120, first
    filed as #81). Under this build's SOFT_DELETE default that is the only
    hard-delete path production takes.

    `guard._before_core_delete` now catches it. The object is soft-deleted
    first, so this also pins that the ids are read past Superset's own
    soft-delete visibility filter -- the rows being purged are exactly the
    ones an ordinary query cannot see.
    """
    w = harness.world
    chart = harness.create_chart(w.ada, "purge")
    harness.set_visibility(w.ada, chart, "shared")
    harness.share(w.ada, chart, w.ben.ref)
    harness.drain()
    assert harness.authorizer.tuples_on(chart.obj) != set()

    assert harness.delete(w.ada, f"/api/v1/chart/{chart.id}").status_code == 200
    r = harness.post(w.ada, f"/api/v1/chart/{chart.uuid}/purge")
    assert r.status_code == 200, r.get_json()

    assert harness.row(chart) is None, "the ownership row went with the object"
    assert _share_subjects(harness, chart) == [], "and so did its shares"
    (purge,) = [
        o
        for o in harness.outbox()
        if o["op"] == "purge_object" and o["object"] == chart.obj
    ]
    assert purge["status"] == "pending", "queued in the purge's own transaction"

    harness.drain()
    assert harness.authorizer.tuples_on(chart.obj) == set()


def test_a_bulk_core_delete_collects_every_object_it_removes(harness):
    """The listener keys off the statement, not the route: a bulk
    `session.execute(sa.delete(...))` over several charts -- what a data
    migration or a cleanup script writes -- collects each of them."""
    w = harness.world
    charts = [harness.create_chart(w.ada, f"bulk-{i}") for i in range(3)]
    for chart in charts:
        harness.set_visibility(w.ada, chart, "shared")
        harness.share(w.ada, chart, w.ben.ref)
    harness.drain()
    survivor = harness.create_chart(w.ada, "bulk-survivor")

    with harness.ctx():
        from sqlalchemy import delete

        from superset import db
        from superset.models.slice import Slice

        db.session.execute(
            delete(Slice.__table__).where(
                Slice.__table__.c.id.in_([c.id for c in charts])
            )
        )
        db.session.commit()

    for chart in charts:
        assert harness.row(chart) is None, f"{chart.id} still has an ownership row"
        assert _share_subjects(harness, chart) == []
    assert harness.row(survivor) is not None, "an untouched object is untouched"

    harness.drain()
    for chart in charts:
        assert harness.authorizer.tuples_on(chart.obj) == set()


def test_a_delete_that_loses_its_race_keeps_the_ownership_row(harness):
    """The collection runs inside the caller's transaction, before the DELETE.
    `cascade_hard_delete` does its work in a savepoint and rolls it back when
    it loses the race for the row -- so the ownership rows have to come back
    with it, or a purge that deleted nothing would still have destroyed the
    object's ownership.

    Review round 1: the first version of this rolled back a savepoint that
    the listener had never written into, so it passed with the listener
    removed entirely. It now asserts the two halves separately -- that the
    collection HAPPENED inside the savepoint, and that the rollback undid it
    -- so neither can pass by accident.
    """
    w = harness.world
    chart = harness.create_chart(w.ada, "rollback")
    harness.set_visibility(w.ada, chart, "shared")
    harness.share(w.ada, chart, w.ben.ref)
    harness.drain()
    tuples_before = harness.authorizer.tuples_on(chart.obj)

    with harness.ctx():
        from sqlalchemy import delete

        from superset import db
        from superset.models.slice import Slice
        from superset_ownership import service
        from superset_ownership.db import ownership_object

        db.session.begin_nested()
        db.session.execute(
            delete(Slice.__table__).where(Slice.__table__.c.id == chart.id)
        )
        # Inside the savepoint, before the rollback: the collection really
        # ran. Read through the session, not the cache.
        inside = db.session.execute(
            ownership_object.select().where(
                ownership_object.c.asset_type == "chart",
                ownership_object.c.object_id == chart.id,
            )
        ).first()
        assert inside is None, "the listener collected inside the savepoint"
        db.session.rollback()
        service.invalidate(chart.asset_type, chart.id)

    assert harness.row(chart) is not None, "the row came back with the savepoint"
    assert _share_subjects(harness, chart) == [w.ben.ref]
    assert [
        o
        for o in harness.outbox()
        if o["object"] == chart.obj and o["op"] == "purge_object"
    ] == [], "and no purge was left queued for an object that still exists"
    assert harness.authorizer.tuples_on(chart.obj) == tuples_before


def test_a_delete_whose_criteria_carries_a_bound_parameter_still_works(harness):
    """Review round 1. The parameters live on the EXECUTION, not on the
    statement, and the listener re-ran the whereclause without them: the
    inner SELECT raised `A value is required for bind parameter` and, from
    inside the event, killed the caller's DELETE. A listener standing in
    front of every statement in the application must not be able to turn a
    legal one into an error."""
    w = harness.world
    chart = harness.create_chart(w.ada, "bound-param")
    harness.set_visibility(w.ada, chart, "shared")
    harness.share(w.ada, chart, w.ben.ref)
    harness.drain()

    with harness.ctx():
        from sqlalchemy import bindparam, delete

        from superset import db
        from superset.models.slice import Slice

        db.session.execute(
            delete(Slice.__table__).where(Slice.__table__.c.id == bindparam("cid")),
            {"cid": chart.id},
        )
        db.session.commit()

    assert harness.row(chart) is None, "the delete ran AND the ownership went with it"
    assert _share_subjects(harness, chart) == []
    harness.drain()
    assert harness.authorizer.tuples_on(chart.obj) == set()


def test_a_failure_inside_the_collection_does_not_kill_the_delete(harness, monkeypatch):
    """Review round 1. The ORM path deliberately swallows an unexpected error
    from the hook -- "must not stop Superset from deleting the object" -- and
    this path did not, so a raising hook (an unreachable cache, a removed
    ownership table after a teardown) would have failed EVERY chart and
    dashboard hard delete in the instance. Same policy on both paths now: the
    delete goes ahead and the row is `reconcile`'s to sweep."""
    from superset_ownership import guard

    w = harness.world
    chart = harness.create_chart(w.ada, "hook-raises")
    harness.set_visibility(w.ada, chart, "shared")

    def _boom(*args, **kwargs):
        raise RuntimeError("ownership_object is gone")

    monkeypatch.setattr(guard, "_collect_core_deleted", _boom)

    with harness.ctx():
        from sqlalchemy import delete

        from superset import db
        from superset.models.slice import Slice

        db.session.execute(
            delete(Slice.__table__).where(Slice.__table__.c.id == chart.id)
        )
        db.session.commit()

        remaining = db.session.query(Slice).filter_by(id=chart.id).first()
    assert remaining is None, "the caller's delete still ran"


def test_a_delete_whose_criteria_spans_two_tables_is_not_guessed_at(harness, caplog):
    """A criteria dragging in another table makes the listener's SELECT a
    cartesian product whose ids name objects the DELETE does not touch.
    PostgreSQL compiles such a DELETE as `DELETE ... USING`, so it is a real
    statement shape (review round 1). Collect nothing and say so, rather than
    remove the ownership of an object that survives."""
    import logging

    w = harness.world
    chart = harness.create_chart(w.ada, "two-tables")
    harness.set_visibility(w.ada, chart, "shared")

    with harness.ctx():
        from sqlalchemy import delete

        from superset import db
        from superset.models.dashboard import Dashboard
        from superset.models.slice import Slice

        with caplog.at_level(logging.WARNING, logger="superset_ownership.guard"):
            try:
                db.session.execute(
                    delete(Slice.__table__)
                    .where(Slice.__table__.c.id == chart.id)
                    .where(Dashboard.__table__.c.id == Dashboard.__table__.c.id)
                )
            except Exception:  # noqa: BLE001 - SQLite refuses the DELETE itself
                db.session.rollback()

    assert any("spans" in rec.getMessage() for rec in caplog.records), (
        "it says it did not collect, so an operator can reconcile"
    )
    assert harness.row(chart) is not None, "and it removed nothing on a guess"


def test_reconcile_clears_an_ownership_row_whose_object_is_already_gone(harness):
    """The repair half of issue #120. An instance that hard-deleted objects
    before the listener existed still carries their rows, their shares and
    their tuples, and nothing could clear them. `rows_without_object` -- which
    `reconcile` now runs -- does, through the same hook the live path uses.
    """
    from superset_ownership import guard, lifecycle

    w = harness.world
    chart = harness.create_chart(w.ada, "orphan")
    harness.set_visibility(w.ada, chart, "shared")
    harness.share(w.ada, chart, w.ben.ref)
    harness.drain()
    assert harness.authorizer.tuples_on(chart.obj) != set()

    # The object goes the way it went before the listener: with the guard
    # standing down, so the ownership row and the tuples are left behind.
    with harness.ctx():
        from sqlalchemy import delete

        from superset import db
        from superset.models.slice import Slice

        with guard.suppressed():
            db.session.execute(
                delete(Slice.__table__).where(Slice.__table__.c.id == chart.id)
            )
            db.session.commit()
    assert harness.row(chart) is not None, "the orphan this repairs"

    with harness.ctx():
        preview = lifecycle.rows_without_object(dry_run=True)
    assert f"chart:{chart.id}" in preview["rows_without_object"]
    assert "rows_removed" not in preview
    assert harness.row(chart) is not None, "a dry run changes nothing"

    with harness.ctx():
        repaired = lifecycle.rows_without_object(dry_run=False)
    assert repaired["rows_removed"] == 1
    assert harness.row(chart) is None
    assert _share_subjects(harness, chart) == []

    harness.drain()
    assert harness.authorizer.tuples_on(chart.obj) == set()

    with harness.ctx():
        assert lifecycle.rows_without_object(dry_run=True)["rows_without_object"] == []


def test_the_orphan_sweep_refuses_when_it_cannot_see_soft_deleted_rows(
    harness, monkeypatch, caplog
):
    """Review round 1. "Absent from `all_rows`" only means "gone" when
    `all_rows` can see everything, and its soft-delete bypass sits behind
    `except ImportError: pass`. One upstream rename and the sweep would have
    destroyed the ownership -- and the live viewer tuples -- of every
    soft-deleted object in the instance. It now proves it can see them
    first, and skips the asset type rather than guessing."""
    import logging

    from superset_ownership import lifecycle

    w = harness.world
    chart = harness.create_chart(w.ada, "blind-sweep")
    harness.set_visibility(w.ada, chart, "shared")
    assert harness.delete(w.ada, f"/api/v1/chart/{chart.id}").status_code == 200

    monkeypatch.setattr(lifecycle, "sweeps_soft_deleted", lambda model: False)
    # And the sweep must not lean on `all_rows` at all in that state.
    monkeypatch.setattr(
        lifecycle, "all_rows", lambda model: pytest.fail("read the partial view")
    )

    with harness.ctx():
        with caplog.at_level(logging.ERROR, logger="superset_ownership.lifecycle"):
            report = lifecycle.rows_without_object(dry_run=False)

    assert report["rows_without_object"] == []
    assert report["rows_without_object_skipped"] == ["chart", "dashboard"]
    assert any("cannot tell" in rec.getMessage() for rec in caplog.records)
    assert harness.row(chart) is not None, "the soft-deleted object kept its ownership"


def test_sweeps_soft_deleted_is_true_for_this_build(harness):
    """The guard above is only worth having if it says yes on a healthy
    build -- otherwise it would silently disable the sweep."""
    from superset.models.slice import Slice

    from superset_ownership import lifecycle

    with harness.ctx():
        assert lifecycle.sweeps_soft_deleted(Slice) is True


def test_reconcile_follows_a_diverged_uuid_before_it_touches_any_tuple(
    harness, monkeypatch
):
    """Review round 2: the helper had a test, the WIRING did not -- removing
    `reconcile`'s call to it left the suite green. Order matters as much as
    the call: reconcile builds every store reference from the row, so a row
    that still names a dead uuid has to be followed BEFORE the loop that
    writes owner tuples from it."""
    from superset_ownership import lifecycle

    order: list[str] = []

    def _repoint(dry_run=True):
        order.append(f"repoint(dry_run={dry_run})")
        return {"rows_with_a_diverged_uuid": []}

    def _no_store_needed(operation):
        order.append("gate")
        return None

    monkeypatch.setattr(lifecycle, "_repoint_diverged_rows", _repoint)
    monkeypatch.setattr(lifecycle, "_requires_openfga", _no_store_needed)

    from superset_ownership import fga

    monkeypatch.setattr(fga, "read_all", lambda *a, **k: order.append("fga") or [])

    with harness.ctx():
        report = lifecycle.reconcile(dry_run=True)

    assert "repoint(dry_run=True)" in order, "reconcile must run it at all"
    assert order.index("repoint(dry_run=True)") < (
        order.index("fga") if "fga" in order else len(order)
    ), "and before it reads or writes a single tuple"
    assert "rows_with_a_diverged_uuid" in report, "and fold its report into its own"


def test_a_soft_deleted_object_is_not_an_orphan(harness):
    """A soft-deleted object still exists and still owns its ownership row --
    restoring it from the trash has to restore what it was. The sweep reads
    past Superset's visibility filter for exactly this reason."""
    from superset_ownership import lifecycle

    w = harness.world
    chart = harness.create_chart(w.ada, "archived")
    harness.set_visibility(w.ada, chart, "shared")
    assert harness.delete(w.ada, f"/api/v1/chart/{chart.id}").status_code == 200

    with harness.ctx():
        report = lifecycle.rows_without_object(dry_run=False)
    assert f"chart:{chart.id}" not in report["rows_without_object"]
    assert harness.row(chart) is not None


def test_an_object_whose_uuid_changes_takes_its_ownership_with_it(harness):
    """Issue #127: Superset's dashboard import validates collisions by uuid
    but resolves by `slug`, so it can replace a LIVE object in place and give
    it the imported file's uuid. The ownership row and every tuple then
    described an object with a uuid nothing has -- and `check` said ok.

    The store is addressed by uuid, so following the object means moving its
    grants: the old reference is purged and the owner, the tenant and every
    share are re-queued under the new one.
    """
    import uuid as uuid_module

    from superset_ownership import lifecycle

    w = harness.world
    chart = harness.create_chart(w.ada, "reuuid")
    harness.set_visibility(w.ada, chart, "shared")
    harness.share(w.ada, chart, w.ben.ref)
    harness.drain()
    old_obj = chart.obj
    assert {t[1] for t in harness.authorizer.tuples_on(old_obj)} == {"owner", "viewer"}

    new_uuid = str(uuid_module.uuid4())
    with harness.ctx():
        from superset import db
        from superset.models.slice import Slice

        live = db.session.query(Slice).filter_by(id=chart.id).one()
        live.uuid = new_uuid
        db.session.commit()

    row = harness.row(chart)
    assert row is not None and str(row.object_uuid) == new_uuid, (
        "the row followed the object"
    )

    harness.drain()
    assert harness.authorizer.tuples_on(old_obj) == set(), "the old reference is gone"
    moved = harness.authorizer.tuples_on(f"chart:{new_uuid}")
    assert {t[1] for t in moved} == {"owner", "viewer"}, (
        "and the owner and the share came with it"
    )

    with harness.ctx():
        assert lifecycle.check_consistency()["uuid_divergence"] == []


def test_check_reports_a_row_and_an_object_that_disagree_about_the_uuid(harness):
    """The repair half of #127: an instance that already diverged (the import
    happened before the module followed it) could not see it -- `check`
    compared everything about a row except the one identifier the
    authorization store actually addresses the object by."""
    import uuid as uuid_module

    from superset_ownership import guard, lifecycle

    w = harness.world
    chart = harness.create_chart(w.ada, "diverged")
    harness.set_visibility(w.ada, chart, "shared")
    new_uuid = str(uuid_module.uuid4())

    with harness.ctx():
        from superset import db
        from superset.models.slice import Slice

        # With the guard standing down, the way it went before it followed.
        with guard.suppressed():
            live = db.session.query(Slice).filter_by(id=chart.id).one()
            live.uuid = new_uuid
            db.session.commit()

    with harness.ctx():
        report = lifecycle.check_consistency()
    assert report["ok"] is False
    assert report["uuid_divergence"] == [
        {"object": f"chart:{chart.id}", "mirrored": chart.uuid, "live": new_uuid}
    ]

    # The world is shared with every other test in this module, and a
    # diverged row fails `check` for all of them. Put it back the way the
    # live path would have.
    with harness.ctx():
        from superset import db
        from superset_ownership import service

        service.repoint_object_uuid("chart", chart.id, chart.uuid, new_uuid)
        db.session.commit()
    with harness.ctx():
        assert lifecycle.check_consistency()["uuid_divergence"] == []


def test_reconcile_repairs_a_diverged_uuid_instead_of_writing_onto_a_dead_one(
    harness,
):
    """Review round 1. `check` reported `uuid_divergence` and UPDATING named
    `reconcile --write` as the remedy -- but reconcile built its store
    reference from the STALE mirrored uuid, so it wrote a fresh owner tuple
    onto the dead reference and reported `owner_tuple_written` as though it
    had repaired something. The pre-existing damage was detectable and
    unrepairable."""
    import uuid as uuid_module

    from superset_ownership import guard, lifecycle

    w = harness.world
    chart = harness.create_chart(w.ada, "reconcile-diverged")
    harness.set_visibility(w.ada, chart, "shared")
    harness.share(w.ada, chart, w.ben.ref)
    harness.drain()
    dead_ref = chart.obj
    new_uuid = str(uuid_module.uuid4())

    with harness.ctx():
        from superset import db
        from superset.models.slice import Slice

        with guard.suppressed():
            live = db.session.query(Slice).filter_by(id=chart.id).one()
            live.uuid = new_uuid
            db.session.commit()

    with harness.ctx():
        preview = lifecycle._repoint_diverged_rows(dry_run=True)
    assert preview["rows_with_a_diverged_uuid"] == [f"chart:{chart.id}"]
    assert harness.row(chart).object_uuid != new_uuid, "a dry run changes nothing"

    with harness.ctx():
        repaired = lifecycle._repoint_diverged_rows(dry_run=False)
    assert repaired["rows_repointed"] == 1
    assert str(harness.row(chart).object_uuid) == new_uuid

    harness.drain()
    assert harness.authorizer.tuples_on(dead_ref) == set()
    moved = {t[1] for t in harness.authorizer.tuples_on(f"chart:{new_uuid}")}
    assert moved == {"owner", "viewer"}
    with harness.ctx():
        assert lifecycle.check_consistency()["uuid_divergence"] == []


def test_a_diverged_instance_does_not_boot_in_silence(harness, caplog):
    """`uuid_divergence` counted against `ok` but `startup_check` had no
    branch for it, so a diverged instance lost the "startup check ok" line
    and gained no error -- the one state where the boot log says nothing at
    all (review round 1)."""
    import logging
    import uuid as uuid_module

    from superset_ownership import guard, lifecycle, service

    w = harness.world
    chart = harness.create_chart(w.ada, "silent-boot")
    harness.set_visibility(w.ada, chart, "shared")
    new_uuid = str(uuid_module.uuid4())
    old_uuid = chart.uuid

    with harness.ctx():
        from superset import db
        from superset.models.slice import Slice

        with guard.suppressed():
            live = db.session.query(Slice).filter_by(id=chart.id).one()
            live.uuid = new_uuid
            db.session.commit()
    try:
        with harness.ctx():
            with caplog.at_level(logging.ERROR, logger="superset_ownership.lifecycle"):
                report = lifecycle.startup_check()
        assert report["ok"] is False
        messages = [rec.getMessage() for rec in caplog.records]
        assert any("no longer carries" in m for m in messages)
        assert any("reconcile --write" in m for m in messages)
    finally:
        with harness.ctx():
            from superset import db

            service.repoint_object_uuid("chart", chart.id, old_uuid, new_uuid)
            db.session.commit()


def test_the_orm_entity_form_of_a_bulk_delete_is_collected_too(harness):
    """Review round 2. `delete(Slice)` -- the entity form, and what
    `Query.delete()` compiles to -- carries its target as an
    `AnnotatedTable`, which is `==` the plain `Table` but not `is` it. The
    multi-FROM guard compared by identity and declined every entity-form
    delete as "spans 1 table", so `superset/commands/security/reset.py`
    would have deleted every chart and dashboard and left every ownership
    row and every live `viewer` tuple behind."""
    w = harness.world
    charts = [harness.create_chart(w.ada, f"entity-{i}") for i in range(2)]
    for chart in charts:
        harness.set_visibility(w.ada, chart, "shared")
        harness.share(w.ada, chart, w.ben.ref)
    harness.drain()

    with harness.ctx():
        from sqlalchemy import delete

        from superset import db
        from superset.models.slice import Slice

        db.session.execute(delete(Slice).where(Slice.id.in_([c.id for c in charts])))
        db.session.commit()

    for chart in charts:
        assert harness.row(chart) is None, f"{chart.id} kept its ownership row"
        assert _share_subjects(harness, chart) == []
    harness.drain()
    for chart in charts:
        assert harness.authorizer.tuples_on(chart.obj) == set()


def test_a_failure_part_way_through_the_collection_leaves_nothing_half_done(
    harness, monkeypatch
):
    """Review round 2. The collection removes the local rows first and only
    then enqueues the store purge, so containing a failure in a later step
    used to commit the worst state available: the object gone, the row gone,
    and the store still holding `owner` and `viewer` -- undetectable,
    because `check` and `reconcile` both enumerate from `ownership_object`.
    Its own savepoint means a contained failure contains all of it."""
    from superset_ownership import outbox

    w = harness.world
    chart = harness.create_chart(w.ada, "half-done")
    harness.set_visibility(w.ada, chart, "shared")
    harness.share(w.ada, chart, w.ben.ref)
    harness.drain()
    tuples_before = harness.authorizer.tuples_on(chart.obj)

    def _boom(*args, **kwargs):
        raise RuntimeError("the store intent could not be queued")

    monkeypatch.setattr(outbox, "purge_object", _boom)

    with harness.ctx():
        from sqlalchemy import delete

        from superset import db
        from superset.models.slice import Slice

        db.session.execute(
            delete(Slice.__table__).where(Slice.__table__.c.id == chart.id)
        )
        db.session.commit()

        gone = db.session.query(Slice).filter_by(id=chart.id).first()

    assert gone is None, "the caller's delete still ran"
    # The ownership row survives the contained failure, so `check` reports
    # `missing_object` and `reconcile --write` can finish the job. The
    # alternative -- row deleted, tuples kept -- is the one nothing can see.
    assert harness.row(chart) is not None, "the half-done state was rolled back"
    assert harness.authorizer.tuples_on(chart.obj) == tuples_before

    monkeypatch.undo()  # the store is reachable again
    with harness.ctx():
        from superset_ownership import lifecycle

        assert f"chart:{chart.id}" in lifecycle.check_consistency()["missing_object"]
        lifecycle.rows_without_object(dry_run=False)
    harness.drain()
    assert harness.authorizer.tuples_on(chart.obj) == set(), "and reconcile finishes it"


def test_the_core_delete_path_does_not_take_the_modules_own_lock(harness, monkeypatch):
    """Review round 2 asked for this to be pinned. `cascade_hard_delete`
    already holds the asset row FOR UPDATE before the statement the listener
    stands in front of, so taking this module's per-object lock there is the
    one place that locks AFTER the asset row -- the ABBA against the
    visibility route. The ORM path, which runs before Superset's own
    statements, still takes it."""
    from superset_ownership import hooks

    calls: list[bool] = []
    real = hooks.after_asset_delete

    def _record(asset_type, object_id, object_uuid, *, lock=True):
        calls.append(lock)
        return real(asset_type, object_id, object_uuid, lock=lock)

    monkeypatch.setattr(hooks, "after_asset_delete", _record)

    w = harness.world
    core_chart = harness.create_chart(w.ada, "lock-core")
    orm_chart = harness.create_chart(w.ada, "lock-orm")

    with harness.ctx():
        from sqlalchemy import delete

        from superset import db
        from superset.models.slice import Slice

        db.session.execute(
            delete(Slice.__table__).where(Slice.__table__.c.id == core_chart.id)
        )
        db.session.commit()
    assert calls == [False], "the Core path does not lock after the asset row"

    calls.clear()
    with harness.ctx():
        from superset import db
        from superset.models.slice import Slice

        db.session.delete(db.session.query(Slice).filter_by(id=orm_chart.id).one())
        db.session.commit()
    assert calls == [True], "the ORM path, which locks first, still does"


def test_a_re_point_stamps_the_row_it_names_and_not_a_bystander(harness):
    """Review round 2, security. `ownership_object.object_uuid` carries no
    unique constraint, and a `uuid_divergence` is precisely the state where
    two rows can hold the same one -- which is the state this function
    exists to repair. Matching the tenant mirror by uuid stamped a BYSTANDER
    row's `tenant_guid` with the moving object's tenant, which is a read
    grant: a tenant administrator reads every object of their own tenant, so
    an administrator of an unrelated tenant went from 404 to 200 on the
    victim's chart. The row half is matched by id now; the store half stays
    on uuid, because that is how the store addresses objects.
    """
    import uuid as uuid_module

    from superset_ownership import guard, service
    from superset_ownership.identity import tenant_role_name

    tenant_mover = guid(0x310)
    tenant_victim = guid(0x320)
    mover_owner = harness.add_person(
        guid(0x311),
        "Mover311",
        "Gamma",
        "sales_readers",
        tenant_role_name(tenant_mover),
    )
    victim_owner = harness.add_person(
        guid(0x321),
        "Victim321",
        "Gamma",
        "sales_readers",
        tenant_role_name(tenant_victim),
    )
    mover = harness.create_chart(mover_owner, "mover-310")
    victim = harness.create_chart(victim_owner, "victim-320")
    harness.drain()

    mover_row = harness.row(mover)
    victim_before = harness.row(victim)
    assert mover_row.tenant_guid, "the moving object must carry a tenant to stamp"
    assert service.normalize_tenant(
        victim_before.tenant_guid
    ) != service.normalize_tenant(mover_row.tenant_guid), "two different tenants"

    # The shape an instance is left in after an import rewrote one object's
    # uuid: the victim's ROW claims a uuid, and the object being repaired is
    # about to be re-pointed onto that same one.
    collision = str(uuid_module.uuid4())
    with harness.ctx():
        from superset import db
        from superset_ownership.db import ownership_object

        with guard.suppressed():
            db.session.execute(
                ownership_object.update()
                .where(
                    ownership_object.c.asset_type == "chart",
                    ownership_object.c.object_id == victim.id,
                )
                .values(object_uuid=collision)
            )
            db.session.commit()
        service.invalidate("chart", victim.id)

        service.repoint_object_uuid("chart", mover.id, mover.uuid, collision)
        db.session.commit()
    service.invalidate("chart", victim.id)

    victim_after = harness.row(victim)
    assert service.normalize_tenant(
        victim_after.tenant_guid
    ) == service.normalize_tenant(victim_before.tenant_guid), (
        "a re-point of another object rewrote this row's tenant, which is a "
        "read grant to that tenant's administrators"
    )
    assert service.normalize_tenant(
        harness.row(mover).tenant_guid
    ) == service.normalize_tenant(mover_row.tenant_guid), "and the mover kept its own"

    # The world outlives this test, and a row whose uuid disagrees with its
    # object fails `check` for every test after it. Put both back.
    with harness.ctx():
        from superset import db
        from superset_ownership.db import ownership_object

        with guard.suppressed():
            db.session.execute(
                ownership_object.update()
                .where(
                    ownership_object.c.asset_type == "chart",
                    ownership_object.c.object_id == victim.id,
                )
                .values(object_uuid=victim.uuid)
            )
            db.session.commit()
        service.repoint_object_uuid("chart", mover.id, collision, mover.uuid)
        db.session.commit()
    service.invalidate("chart", victim.id)
    service.invalidate("chart", mover.id)


def test_an_object_repointed_and_deleted_in_one_flush_leaves_no_tuple_behind(harness):
    """Review round 3. SQLAlchemy drops a deleted object out of
    `session.dirty`, so `_follow_uuid_changes` never sees an object that was
    re-pointed AND deleted in the same flush -- while `_collect_deleted`
    reads the object's already-reassigned in-memory uuid and purged a
    reference that never held a tuple. The real ones stayed filed under the
    old uuid forever, and with the row gone nothing could ever find them:
    `check` and `reconcile` both enumerate from `ownership_object`, so it
    reported `ok: true` over a live `owner` and `viewer` grant.

    Superset's own import does exactly this combination.
    """
    import uuid as uuid_module

    from superset_ownership import lifecycle

    w = harness.world
    chart = harness.create_chart(w.ada, "repoint-and-delete")
    harness.set_visibility(w.ada, chart, "shared")
    harness.share(w.ada, chart, w.ben.ref)
    harness.drain()
    old_ref = chart.obj
    assert {t[1] for t in harness.authorizer.tuples_on(old_ref)} == {"owner", "viewer"}

    new_uuid = str(uuid_module.uuid4())
    with harness.ctx():
        from superset import db
        from superset.models.slice import Slice

        live = db.session.query(Slice).filter_by(id=chart.id).one()
        live.uuid = new_uuid  # in `session.dirty` ...
        db.session.delete(live)  # ... and now NOT, because it is `deleted`
        db.session.commit()

    assert harness.row(chart) is None, "the ownership row went with the object"
    harness.drain()
    assert harness.authorizer.tuples_on(old_ref) == set(), (
        "the tuples the store actually held were left behind forever"
    )
    assert harness.authorizer.tuples_on(f"chart:{new_uuid}") == set()
    with harness.ctx():
        assert lifecycle.check_consistency()["ok"] is True


def test_reconcile_does_not_resurrect_an_object_deleted_under_it(harness, monkeypatch):
    """Review round 3. `reconcile` reads every row once and then spends a
    store round trip per row. An object deleted in that window has had its
    row and its tuples removed already -- and a repair written afterwards
    from the stale snapshot files a live `owner` grant under a reference
    nothing names any more, which neither `check` nor `reconcile` can ever
    see again, because both enumerate FROM `ownership_object`.

    The window is reproduced exactly where it really opens: inside the
    store read, between the snapshot and the write.
    """
    from superset_ownership import fga, lifecycle

    w = harness.world
    victim = harness.create_chart(w.ada, "deleted-mid-reconcile")
    harness.set_visibility(w.ada, victim, "shared")
    harness.drain()
    ref = victim.obj

    written: list = []
    monkeypatch.setattr(lifecycle, "_requires_openfga", lambda operation: None)
    from superset_ownership import outbox as outbox_module

    monkeypatch.setattr(
        outbox_module,
        "write_tuple",
        lambda subject, relation, obj: written.append((subject, relation, obj)) or True,
    )

    def read_all_and_delete(obj, relation=None):
        """The store answers "no owner tuple" -- and, in the same moment, the
        object is hard-deleted by another request."""
        if obj == ref:
            with harness.ctx():
                from sqlalchemy import delete

                from superset import db
                from superset.models.slice import Slice

                db.session.execute(
                    delete(Slice.__table__).where(Slice.__table__.c.id == victim.id)
                )
                db.session.commit()
        return []

    monkeypatch.setattr(fga, "read_all", read_all_and_delete)

    with harness.ctx():
        lifecycle.reconcile(dry_run=False)

    assert not [t for t in written if t[2] == ref], (
        "reconcile wrote an owner tuple for an object deleted under it, under a "
        f"reference nothing names any more: {written}"
    )


def test_a_re_point_on_an_id_keyed_store_moves_nothing_and_touches_nobody(
    harness, monkeypatch
):
    """Review round 3, the bystander purge. This module's local backend
    answers from the share ROWS, keyed by (asset_type, object_id), which do
    not move when a uuid changes -- so a re-point there has nothing to purge
    and nothing to re-write. Purging anyway resolved the STALE uuid through
    `lookup_by_uuid`, an unordered `.first()` with no uniqueness behind it,
    and under the outbox the subject row has already moved, so it could only
    land on some OTHER row still holding that uuid -- whose share rows the
    local purge then deletes.

    Pinned at the seam rather than through the fake store: the harness's
    authorizer keeps tuples, not rows, so only an id-keyed backend shows the
    damage. What the fix has to guarantee is that such a backend is asked to
    do nothing at all.
    """
    from superset_ownership import authz, service

    # The local backend is the one that resolves a reference back to a row.
    assert authz.LocalAuthorizer.addresses_objects_by_uuid is False

    w = harness.world
    mover = harness.create_chart(w.ada, "mover-id-keyed")
    harness.set_visibility(w.ada, mover, "shared")
    harness.share(w.ada, mover, w.ben.ref)
    harness.drain()

    purges: list = []
    writes: list = []
    monkeypatch.setattr(
        service.outbox,
        "purge_object",
        lambda asset_type, uuid: purges.append((asset_type, uuid)) or True,
    )
    monkeypatch.setattr(
        service.outbox,
        "write_tuple",
        lambda subject, relation, obj: writes.append((subject, relation, obj)) or True,
    )

    class _IdKeyed:
        addresses_objects_by_uuid = False

    monkeypatch.setattr(service, "get_authorizer", lambda: _IdKeyed())

    import uuid as uuid_module

    new_uuid = str(uuid_module.uuid4())
    with harness.ctx():
        from superset import db

        assert service.repoint_object_uuid("chart", mover.id, mover.uuid, new_uuid)
        db.session.commit()

    assert purges == [], "an id-keyed store has nothing filed under the old uuid"
    assert writes == [], "and nothing to re-file under the new one"
    assert str(harness.row(mover).object_uuid) == new_uuid, "the row still followed"

    # Put it back, so the rest of the suite sees the world it expects.
    with harness.ctx():
        from superset import db

        service.repoint_object_uuid("chart", mover.id, new_uuid, mover.uuid)
        db.session.commit()


def test_the_listener_does_not_flush_the_callers_pending_work(harness):
    """Review round 4: fix 5 had no test, on the one line whose previous two
    attempts were both wrong.

    `session.begin_nested()` is not "open a SAVEPOINT" --
    `SessionTransaction._take_snapshot` runs a full `session.flush()` first.
    A governed Core DELETE autoflushes nothing on its own, so wrapping the
    collection that way made this listener flush the CALLER's entire
    pending unit of work in front of every such statement. When that flush
    failed, the `IntegrityError` is a `DBAPIError`, so the collection's
    re-raise carried it out of the caller's own
    `session.execute(delete(...))`.

    Pinned by giving the session a pending ORM object the database will
    reject, and then issuing an unrelated governed delete: the delete must
    not be the statement that discovers it.
    """
    w = harness.world
    chart = harness.create_chart(w.ada, "no-flush-please")
    harness.set_visibility(w.ada, chart, "shared")
    harness.drain()

    with harness.ctx():
        from sqlalchemy import delete

        from superset import db
        from superset.models.slice import Slice

        # PENDING ORM work the database will reject: a second chart claiming
        # a uuid that is already taken. It reaches the database only on a
        # flush -- which a governed Core DELETE must not cause.
        doomed = Slice(slice_name="duplicate-uuid", uuid=chart.uuid)
        db.session.add(doomed)
        assert db.session.new, "the doomed object is pending, not yet written"

        # The collection runs in front of THIS statement. It must not be the
        # thing that surfaces the pending row above.
        db.session.execute(
            delete(Slice.__table__).where(Slice.__table__.c.id == chart.id)
        )
        # Read it back with Core, not the ORM: an ORM query autoflushes,
        # which would surface the pending row itself and prove nothing.
        from sqlalchemy import select

        gone = db.session.execute(
            select(Slice.__table__.c.id).where(Slice.__table__.c.id == chart.id)
        ).first()
        assert gone is None, "the caller's own delete ran"

        # Drop the doomed object and let the delete stand, so the world is
        # left consistent (the chart and its ownership went together) rather
        # than rolled back to a chart whose ownership row had been collected.
        db.session.expunge(doomed)
        db.session.commit()

    assert harness.row(chart) is None


def test_an_id_keyed_store_is_told_nothing_when_an_object_is_hard_deleted(
    harness, monkeypatch
):
    """Review round 4's B1, at the delete instead of the re-point. On a
    backend keyed by object id the relationships ARE the share rows
    `after_asset_delete` removes itself, and by the time the purge ran the
    row was gone -- so the reference resolved to whichever OTHER row still
    held that uuid and deleted THAT object's shares, permanently, with
    `check` still green.

    The gate has to be patched on `hooks`, not `service`: `hooks.py` imports
    `get_authorizer` into its own namespace.
    """
    from superset_ownership import hooks

    w = harness.world
    chart = harness.create_chart(w.ada, "id-keyed-delete")
    harness.set_visibility(w.ada, chart, "shared")
    harness.share(w.ada, chart, w.ben.ref)
    harness.drain()

    purges: list = []
    monkeypatch.setattr(
        hooks.outbox,
        "purge_object",
        lambda asset_type, uuid: purges.append((asset_type, uuid)) or True,
    )

    class _IdKeyed:
        addresses_objects_by_uuid = False

    monkeypatch.setattr(hooks, "get_authorizer", lambda: _IdKeyed())

    with harness.ctx():
        from sqlalchemy import delete

        from superset import db
        from superset.models.slice import Slice

        db.session.execute(
            delete(Slice.__table__).where(Slice.__table__.c.id == chart.id)
        )
        db.session.commit()

    assert purges == [], f"an id-keyed store has nothing filed under the uuid: {purges}"
    assert harness.row(chart) is None, "and the local rows went anyway"


def test_reconcile_skips_a_row_transferred_under_it(harness):
    """Review round 4. `expected` comes from the snapshot's `owner_user_id`,
    so a transfer landing in reconcile's window had the PREVIOUS owner's
    tuple written back and the new owner's deleted -- with `remaining: {}`
    and `check` ok. The re-read has to cover the owner, and must not start
    rejecting rows whose concurrent write does not touch the owner tuple."""
    from superset_ownership import lifecycle
    from superset_ownership.db import ownership_object

    w = harness.world
    chart = harness.create_chart(w.ada, "transferred-mid-reconcile")
    harness.set_visibility(w.ada, chart, "private")
    harness.drain()

    with harness.ctx():
        from superset import db

        snap = dict(
            db.session.execute(
                ownership_object.select().where(
                    ownership_object.c.asset_type == "chart",
                    ownership_object.c.object_id == chart.id,
                )
            )
            .mappings()
            .first()
        )
        assert lifecycle._row_still_there(snap) is True

        def _write(**values):
            db.session.execute(
                ownership_object.update()
                .where(
                    ownership_object.c.asset_type == "chart",
                    ownership_object.c.object_id == chart.id,
                )
                .values(**values)
            )
            db.session.commit()

        # Benign: neither of these is what the repair writes.
        _write(visibility="shared")
        assert lifecycle._row_still_there(snap) is True, (
            "a visibility write is not a transfer"
        )
        _write(tenant_guid="tenant-xyz")
        assert lifecycle._row_still_there(snap) is True, (
            "a tenant stamp is not a transfer"
        )

        # The one that is.
        _write(owner_user_id=snap["owner_user_id"] + 1)
        assert lifecycle._row_still_there(snap) is False, (
            "a transfer must stop the repair"
        )


def test_a_reference_two_rows_answer_to_resolves_to_neither(harness):
    """Review round 5, and the sixth member of this class -- the first on the
    READ side. `object_uuid` carries no unique constraint, and a divergence
    is exactly the state where two rows hold one uuid. The default backend
    resolves every object reference through `lookup_by_uuid`, so an
    unordered `.first()` handed the reference to whichever row the database
    felt like returning: measured, that let the read gate admit a user to an
    object never shared with them, and let the drain deliver a REVOCATION to
    a bystander -- leaving the subject it was meant to cut off with their
    grant, the outbox row marked delivered, and nothing able to see it.

    Ambiguity answers "no row", which denies, refuses or skips at every
    caller.
    """
    from superset_ownership import guard, service

    w = harness.world
    one = harness.create_chart(w.ada, "ambiguous-one")
    two = harness.create_chart(w.ada, "ambiguous-two")
    harness.drain()

    assert service_lookup_uuid(harness, str(one.uuid)) is not None, "unambiguous first"

    with harness.ctx():
        from superset import db
        from superset_ownership.db import ownership_object

        with guard.suppressed():
            db.session.execute(
                ownership_object.update()
                .where(
                    ownership_object.c.asset_type == "chart",
                    ownership_object.c.object_id == two.id,
                )
                .values(object_uuid=str(one.uuid))
            )
            db.session.commit()
        service.invalidate("chart", two.id)

    assert service_lookup_uuid(harness, str(one.uuid)) is None, (
        "two rows answer to this reference, so it names none of them"
    )

    # Put it back.
    with harness.ctx():
        from superset import db
        from superset_ownership.db import ownership_object

        with guard.suppressed():
            db.session.execute(
                ownership_object.update()
                .where(
                    ownership_object.c.asset_type == "chart",
                    ownership_object.c.object_id == two.id,
                )
                .values(object_uuid=str(two.uuid))
            )
            db.session.commit()
        service.invalidate("chart", two.id)


def service_lookup_uuid(harness, uuid_value):
    from superset_ownership import service

    with harness.ctx():
        return service.lookup_by_uuid("chart", uuid_value)


def test_a_delete_of_something_ungoverned_costs_nothing(harness):
    """The listener fires on every Session.execute in the application. A
    DELETE against any other table must not reach the ownership tables at
    all."""
    w = harness.world
    chart = harness.create_chart(w.ada, "untouched")
    harness.set_visibility(w.ada, chart, "shared")
    before = harness.authorizer.names

    with harness.ctx():
        from sqlalchemy import delete

        from superset import db
        from superset_ownership.db import ownership_outbox

        db.session.execute(delete(ownership_outbox).where(ownership_outbox.c.id < 0))
        db.session.commit()

    assert harness.row(chart) is not None
    assert harness.authorizer.names == before


# --- 13. DATASET -------------------------------------------------------------


def test_shared_chart_does_not_widen_dataset_access(harness):
    """Cy has no grant on the dataset. Sharing the chart, and a dashboard
    holding it, with Cy changes nothing: the bypass hook re-asserts
    Superset's own can_access_datasource before it will consider a share,
    so RLS and the dataset grant stay authoritative
    (VIEWER_PROMISCUOUS_MODE is False)."""
    w = harness.world
    chart = harness.create_chart(w.ada, "no-widening")
    dash = harness.create_dashboard(w.ada, chart, title="no-widening")
    for ref in (chart, dash):
        # H1 (review round 1, issue #93): a share write is refused outright
        # on a still-private object -- `shared` here so the share below can
        # be made at all; Cy's denial is about the dataset grant, which this
        # transition does not touch.
        harness.set_visibility(w.ada, ref, "shared")
        harness.share(w.ada, ref, w.cy.ref, "viewer")
        assert (w.cy.ref, "viewer", ref.obj) in harness.authorizer.tuples, (
            "the share exists"
        )
        d = harness.decide(w.cy, ref)
        assert d.verdict == DENY
        assert d.checks == [], (
            "refused before the store is asked: the grant comes first"
        )
        assert ref.id not in harness.visible_ids(w.cy, ref.asset_type)
        assert harness.detail(w.cy, ref).status_code == 404
    r = harness.get(w.cy, f"/api/v1/chart/{chart.id}/data/")
    assert r.status_code == 403
    assert harness.authorizer.count("check") == 0

    # An editor share is no different: edit-by-share is dropped for a user
    # who could not read the object.
    harness.share(w.ada, chart, w.cy.ref, "editor")
    assert harness.decide(w.cy, chart).verdict == DENY
    r = harness.rename(w.cy, chart, "edited without the grant")
    assert r.status_code in (403, 404)

    # Ben, who holds the grant, is admitted by the same kind of share on the
    # same chart -- transitioned to `shared` first (issue #93: `private`
    # would deny Ben too, dataset grant or not, which is not what this
    # assertion is about).
    harness.set_visibility(w.ada, chart, "shared")
    harness.share(w.ada, chart, w.ben.ref)
    assert harness.decide(w.ben, chart).verdict == ALLOW


# --- 14. FLAG OFF ------------------------------------------------------------


def _flag_off_subprocess(
    directory: str, *, cwd: Optional[str] = None, **env: str
) -> subprocess.Popen:
    """Start the flag-off interpreter on `directory`; see FLAG_OFF_SCRIPT.
    `env` wins over the defaults, PYTHONPATH included."""
    tests_dir = os.path.dirname(os.path.abspath(__file__))
    pythonpath = os.pathsep.join(
        p
        for p in (
            os.path.dirname(os.path.dirname(tests_dir)),
            os.environ.get("PYTHONPATH", ""),
        )
        if p
    )
    return subprocess.Popen(  # noqa: S603 - a fixed argv: this interpreter, this script
        [sys.executable, "-c", FLAG_OFF_SCRIPT],
        cwd=cwd,
        env={
            **os.environ,
            "OWNERSHIP_ENABLED": "false",
            "OWNERSHIP_HARNESS_DIR": tests_dir,
            "OWNERSHIP_FLAG_OFF_DIR": directory,
            "PYTHONPATH": pythonpath,
            **env,
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _pythonpath_without_the_package() -> list[str]:
    """Every PYTHONPATH entry that does not carry `superset_ownership`. The
    subprocess reports whether the package was importable regardless, so a
    package reachable some other way (installed) fails the no-package boot
    with that report rather than passing vacuously."""
    return [
        p
        for p in os.environ.get("PYTHONPATH", "").split(os.pathsep)
        if p and not os.path.isdir(os.path.join(p, "superset_ownership"))
    ]


def _deployed_config_dir() -> Optional[str]:
    """The directory of the deployed `superset_config` module (the layer the
    light config star-imports), or None when there is none to copy."""
    spec = importlib.util.find_spec("superset_config")
    if spec is None or not spec.origin:
        return None
    return os.path.dirname(os.path.abspath(spec.origin))


def _flag_off_report(proc: subprocess.Popen) -> dict[str, Any]:
    stdout, stderr = proc.communicate(timeout=600)
    lines = [
        ln for ln in stdout.splitlines() if ln.startswith("OWNERSHIP_FLAG_OFF_REPORT ")
    ]
    assert proc.returncode == 0, stderr[-4000:]
    assert lines, stderr[-4000:]
    return json.loads(lines[-1].split(" ", 1)[1])


# The ownership modules a feature-off process loads: `configure`, which the
# deployed config calls to resolve the switch and install the OFF mutator
# (every other import inside it is deferred into the ON block), and `flags`,
# which that mutator imports to report that the switches agree and to
# register the flags-only `superset ownership status|check` (issue #69).
# Neither may import anything else from the package at module level, so
# loading them wires nothing; the structural half of that is asserted here
# from their source, the behavioural half by everything else
# _assert_nothing_wired checks. EXACTLY these three: an OFF boot that loaded
# fewer did not make the report (the OFF mutator is gone), one that loaded
# more wired something.
FLAG_OFF_MODULES = [
    "superset_ownership",
    "superset_ownership.configure",
    "superset_ownership.flags",
]

INFO_LINE_OFF = (
    "OWNERSHIP_ENABLED=False FEATURE_FLAGS['OBJECT_OWNERSHIP']=False (one switch"
)


PACKAGE_IMPORTS = ("from superset_ownership", "import superset_ownership")


def _assert_flags_module_is_standalone() -> None:
    """`flags` imports nothing from the package; `configure` imports nothing
    from it at MODULE level (its body imports are deferred, and indented)."""
    from superset_ownership import configure, flags

    with open(flags.__file__, encoding="utf-8") as fh:
        source = fh.read()
    offenders = [
        ln.strip()
        for ln in source.splitlines()
        if ln.lstrip().startswith(PACKAGE_IMPORTS)
    ]
    assert offenders == [], offenders
    with open(configure.__file__, encoding="utf-8") as fh:
        source = fh.read()
    offenders = [
        ln.strip()
        for ln in source.splitlines()
        if ln.startswith(PACKAGE_IMPORTS) or ln.startswith("from superset")
    ]
    assert offenders == [], offenders


def _log_lines(report: dict[str, Any], level: str, needle: str) -> list[str]:
    """Captured `superset_ownership.*` boot records at `level` containing
    `needle`."""
    return [msg for lvl, _name, msg in report["log"] if lvl == level and needle in msg]


def _assert_nothing_wired(report: dict[str, Any], *, package: bool = True) -> None:
    if package:
        assert report["package_importable"] is True
        assert report["ownership_modules"] == FLAG_OFF_MODULES, (
            "exactly the flag report is loaded with the feature off"
        )
        _assert_flags_module_is_standalone()
        # The flags-only CLI, registered by the OFF mutator: the two commands
        # the issue names and nothing that needs the backend.
        assert report["ownership_cli"] == ["check", "status"]
    else:
        assert report["package_importable"] is False
        assert report["ownership_modules"] == []
        assert report["ownership_cli"] is None
    # Both switches off, in the deployed config's namespace and in the app:
    # the flag was derived from the setting, not left to a default that
    # happens to agree, and nothing later turned it on.
    assert report["deployed_switches"] == {
        "OWNERSHIP_ENABLED": False,
        "OBJECT_OWNERSHIP": False,
    }
    assert report["effective_switches"] == {
        "OWNERSHIP_ENABLED": False,
        "OBJECT_OWNERSHIP": False,
    }
    assert report["config"] == {
        "EXTRA_RAISE_FOR_ACCESS_BYPASS": None,
        "EXTRA_EDITORS_RESOLVER": None,
        "AFTER_ASSET_CREATE": None,
        "EXTRA_OWNERS_RESOLVER": None,
    }
    assert report["extra_access_query_filters"] == {}
    assert report["object_ownership_flag"] is False
    assert "superset_ownership" not in report["blueprints"]
    assert not [f for f in report["before_request"] if "ownership" in f]
    for name, listeners in report["session_listeners"].items():
        assert not [ln for ln in listeners if "ownership" in ln], (name, listeners)


STOCK = {
    "decisions": {
        "Admin": "allow",
        "Ada": "allow",
        "Ben": "allow",
        "Cy": "deny",
        "Dee": "allow",
    },
    "get_status": {"Admin": 200, "Ada": 200, "Ben": 200, "Cy": 404, "Dee": 200},
    "data_status": {"Admin": 200, "Ada": 200, "Ben": 200, "Cy": 404, "Dee": 200},
}


def _assert_stock(observed: dict[str, Any]) -> None:
    """Stock behaviour for a chart nobody governs: no sentinel, the dataset
    grant decides, the base filter hides it from anyone without the grant."""
    assert observed["viewers"] == []
    for key, expected in STOCK.items():
        assert observed[key] == expected, key


def test_flag_off_parity_the_module_is_never_loaded(harness, tmp_path):
    """OWNERSHIP_ENABLED=false in a FRESH interpreter, the same Superset,
    the same environment config: `superset_ownership` never enters
    sys.modules, no extension point is wired, no session listener is
    installed, no before_request added, no ownership table created -- and
    the core access behaviour is stock Superset's: a new chart has no
    viewers, anyone with the dataset grant may open it, nobody else may.
    The one exception, by design, is `superset_ownership.flags`: the deployed
    mutator loads it in both states of the switch to log that the derived
    UI flag agrees with the setting; it imports nothing else from the
    package (asserted from its source) and wires nothing (asserted by every
    other check here). FEATURE_FLAGS["OBJECT_OWNERSHIP"] is false in that
    process, derived from OWNERSHIP_ENABLED=false, so the list pages show no
    ownership surface against the backend that is not there.

    This is how "the core dashboard/chart access tests pass unchanged" is
    asserted without running Superset's own suite twice: with the flag off
    there is no ownership code in the process for them to run against. That
    is a proxy for running those tests, and is stated as one.

    A second interpreter, started alongside, opens a COPY of this session's
    database, which the feature has governed: ownership tables, a private
    chart with the sentinel in its `viewers`, a share. The operator note in
    the deployed config and lifecycle.disable()'s docstring document what
    the flag alone does to such an object: the sentinel stays behind with no
    code left to interpret it, so stock Superset keeps the object closed --
    a one-way door, which only disable() (strip every sentinel first) turns
    into a rollback. Asserted here as documented: the sentinel survives, the
    object is served to Superset's own editors and administrators and
    denied to everyone else, dataset grant or not; a chart created under the
    flag-off process in the same database behaves stock.
    """
    w = harness.world
    seed = harness.create_chart(w.ada, "flag-off-seed")
    # H1 (review round 1, issue #93): a share write is refused outright on a
    # still-private object -- this test's premise needs the chart to STAY
    # private (a share alongside the sentinel, not a `shared` object), so
    # the share is planted directly at the service layer instead of through
    # the now-blocked route.
    with harness.ctx():
        from superset import db
        from superset_ownership import service

        service.add_share("chart", seed.id, seed.uuid, w.ben.ref, "editor")
        db.session.commit()
    harness.drain()
    assert harness.viewers(seed) == [SENTINEL]
    native_editors = harness.editors(seed)
    locked = harness.snapshot_database(str(tmp_path / "locked"))

    fresh_dir = tmp_path / "fresh"
    fresh_dir.mkdir()
    fresh_proc = _flag_off_subprocess(str(fresh_dir))
    # The seeded run also carries the generic env spelling of the flag, so
    # the derivation order (the switch wins over SUPERSET_FEATURE_*) and the
    # "ignored" WARNING are pinned against the deployed file, not a mapping.
    seeded_proc = _flag_off_subprocess(
        locked,
        OWNERSHIP_FLAG_OFF_SEED=json.dumps({"chart_id": seed.id}),
        SUPERSET_FEATURE_OBJECT_OWNERSHIP="true",
    )
    fresh = _flag_off_report(fresh_proc)
    seeded = _flag_off_report(seeded_proc)

    # Flag-off artefact (round 2): this test's premise is that
    # `OWNERSHIP_ENABLED=false` in the subprocess's own environment turns the
    # feature off. A deployment config layer (the operator's
    # `superset_config_docker.py`, star-imported ahead of the light config)
    # can instead pin `OWNERSHIP_ENABLED` as a Python setting -- which wins
    # over the environment by the light config's own documented precedence
    # (`superset_config_docker_light.py`'s `_inherited_switch` comment) -- so
    # the subprocess boots with the feature ON regardless of what this test
    # asked for. `OWNERSHIP_ENABLED_SOURCE == "config"` in that subprocess is
    # exactly that: not a regression in this package, and not something this
    # test's OFF-state assertions can pass against. Skip with the reason
    # rather than fail with a misleading "the module was never loaded" that
    # is actually "the deployment turned the feature on before this test
    # could turn it off".
    if fresh["switch_source"] == "config":
        pytest.skip(
            "OWNERSHIP_ENABLED_SOURCE == 'config': a deployment config layer "
            "pins OWNERSHIP_ENABLED as a Python setting, which wins over "
            "this test's OWNERSHIP_ENABLED=false environment override, so "
            "the flag-off state cannot be exercised against this config"
        )

    # fresh database: nothing wired, nothing created, stock behaviour
    _assert_nothing_wired(fresh)
    assert fresh["ownership_tables"] == []
    _assert_stock(fresh["fresh"])
    assert fresh["seeded"] is None
    # The boot-log contract, on the deployed OFF mutator: one INFO line
    # naming both values, no WARNING of any kind, no hook, and the runtime
    # value the bootstrap payload will carry agrees.
    assert _log_lines(fresh, "INFO", INFO_LINE_OFF), fresh["log"]
    assert [r for r in fresh["log"] if r[0] == "WARNING"] == [], fresh["log"]
    assert fresh["flag_hooks"] == {
        "GET_FEATURE_FLAGS_FUNC": False,
        "IS_FEATURE_ENABLED_FUNC": False,
    }
    assert fresh["runtime_bootstrap_flag"] is False
    assert fresh["switch_source"] == "environment"
    assert fresh["inherited_ui_flag"] is None
    # `superset ownership status` / `check` in the OFF state: the switch
    # report, `check` ok, and both say the backend is not loaded.
    code, status = fresh["cli"]["status"]
    assert code == 0, status
    assert status["backend_loaded"] is False
    assert (status["enabled_backend"], status["enabled_ui"]) == (False, False)
    assert status["enabled_ui_runtime"] is False
    assert status["ui_flag_hooked"] is False
    assert status["flags_agree"] is True
    code, check = fresh["cli"]["check"]
    assert code == 0, check
    assert check["ok"] is True
    assert check["backend_loaded"] is False

    # the governed copy: nothing wired either; the tables the feature created
    # are data and stay, as does the sentinel
    _assert_nothing_wired(seeded)
    # SUPERSET_FEATURE_OBJECT_OWNERSHIP=true did not set the flag (the
    # derivation runs after the merge) and the boot log says it was ignored.
    assert seeded["effective_switches"]["OBJECT_OWNERSHIP"] is False
    assert seeded["runtime_bootstrap_flag"] is False
    assert _log_lines(seeded, "INFO", INFO_LINE_OFF), seeded["log"]
    ignored = _log_lines(seeded, "WARNING", "ignored")
    assert len(ignored) == 1, seeded["log"]
    assert "SUPERSET_FEATURE_OBJECT_OWNERSHIP='true'" in ignored[0]
    assert "Set OWNERSHIP_ENABLED instead" in ignored[0]
    assert seeded["cli"]["check"][0] == 0, seeded["cli"]
    assert seeded["ownership_tables"] == [
        "ownership_object",
        "ownership_outbox",
        "ownership_share",
    ]
    _assert_stock(seeded["fresh"])  # a chart created with the flag off is stock
    locked_out = seeded["seeded"]
    assert locked_out["viewers"] == [SENTINEL], (
        "the flag alone leaves the sentinel behind"
    )
    assert locked_out["editors"] == native_editors, (
        "the object's own editors, unchanged"
    )
    # Stock Superset with a closed `viewers`: an editor of the object and an
    # administrator get in; the dataset grant no longer opens it to anyone.
    served = {"Admin"} | {
        p.label for p in w.everyone if f"user:{p.username}" in native_editors
    }
    assert served == {"Admin", "Ada"}, (
        "the creator is a native editor after a stock create"
    )
    for person in w.everyone:
        expected = "allow" if person.label in served else "deny"
        assert locked_out["decisions"][person.label] == expected, person.label
    assert locked_out["get_status"] == {
        "Admin": 200,
        "Ada": 200,
        "Ben": 404,
        "Cy": 404,
        "Dee": 404,
    }
    assert locked_out["data_status"] == {
        "Admin": 200,
        "Ada": 200,
        "Ben": 404,
        "Cy": 404,
        "Dee": 404,
    }
    # The mirror `editor` share confers nothing without the module: Ben, an
    # editor by share, is a stranger to stock Superset.
    assert locked_out["decisions"]["Ben"] == "deny"


# The operator's override layer, as superset_config.py star-imports it: a
# mutator of its own (custom auth, a metrics hook), the switch as a Python
# setting (a STRING, spelled the way the environment spells it), the flag
# set by hand, and Superset's per-request feature-flag hook forcing the flag
# on. The light config must keep the mutator, honour the setting over the
# environment, report the flag and the env spellings as ignored, and report
# the hooked runtime value the UI will read.
OPERATOR_LAYER = """
# Generated by test_endpoints.py: the operator's superset_config_docker.py.
OWNERSHIP_ENABLED = "false"
FEATURE_FLAGS = {"OBJECT_OWNERSHIP": True}


def FLASK_APP_MUTATOR(app):
    app.config["OPERATOR_MUTATOR_RAN"] = True


def GET_FEATURE_FLAGS_FUNC(feature_flags):
    feature_flags["OBJECT_OWNERSHIP"] = True
    return feature_flags
"""


def test_flag_off_boot_with_an_operator_layer_and_without_the_package(tmp_path):
    """Two more OFF boots of the deployed config, in fresh interpreters.

    LAYERED: `superset_config_docker.py` (the gitignored operator hook,
    first on sys.path) brings a FLASK_APP_MUTATOR, OWNERSHIP_ENABLED as a
    Python setting, a hand-set flag and GET_FEATURE_FLAGS_FUNC into the
    namespace, and the environment says OWNERSHIP_ENABLED=true and
    SUPERSET_FEATURE_OBJECT_OWNERSHIP=true. The operator's mutator runs
    (the OFF mutator chains what the layering brought, not superset.config's
    None); the Python setting wins and the env spelling is reported as
    ignored; the earlier layer's flag is overwritten by the derivation and
    reported as ignored; and the hook -- which the frontend reads through
    -- is caught: the bootstrap payload carries the flag ON against no
    backend, the boot log says so at WARNING naming the hook, and `superset
    ownership check` fails on the runtime disagreement while `status` still
    reports it. Nothing is wired all the same.

    NO PACKAGE: the config on a PYTHONPATH that does not carry
    `superset_ownership` (an image with the config and not the package).
    Stock boot must not depend on the package: the process starts, nothing
    of the package is loaded, the boot log carries the reduced inline report
    and a WARNING that the package is absent, and there is no `superset
    ownership` group to register.
    """
    layered_dir = tmp_path / "layered"
    layered_dir.mkdir()
    (layered_dir / "superset_config_docker.py").write_text(
        textwrap.dedent(OPERATOR_LAYER), encoding="utf-8"
    )
    layered_proc = _flag_off_subprocess(
        str(layered_dir),
        OWNERSHIP_ENABLED="true",
        SUPERSET_FEATURE_OBJECT_OWNERSHIP="true",
    )

    config_dir = _deployed_config_dir()
    if config_dir is None:
        pytest.skip("no deployed superset_config module to boot without the package")
    nopkg_dir = tmp_path / "nopkg"
    nopkg_dir.mkdir()
    # The layer the light config star-imports, and nothing else; cwd and
    # PYTHONPATH both without the package.
    shutil.copyfile(
        os.path.join(config_dir, "superset_config.py"),
        nopkg_dir / "superset_config.py",
    )
    nopkg_proc = _flag_off_subprocess(
        str(nopkg_dir),
        cwd=str(nopkg_dir),
        PYTHONPATH=os.pathsep.join(
            [str(nopkg_dir), *_pythonpath_without_the_package()]
        ),
    )
    layered = _flag_off_report(layered_proc)
    nopkg = _flag_off_report(nopkg_proc)

    # --- layered
    _assert_nothing_wired(layered)
    assert layered["operator_mutator_ran"] is True, (
        "the OFF mutator chains the operator's mutator from the earlier layer"
    )
    assert layered["switch_source"] == "config"
    assert layered["inherited_ui_flag"] is True
    assert layered["flag_hooks"] == {
        "GET_FEATURE_FLAGS_FUNC": True,
        "IS_FEATURE_ENABLED_FUNC": False,
    }
    assert layered["runtime_bootstrap_flag"] is True, (
        "what the frontend reads: the hook forced the flag on"
    )
    _assert_stock(layered["fresh"])
    # Every dropped instruction is named, at WARNING, and the runtime split
    # is a WARNING of its own naming the hook and the failure state.
    warnings = [msg for lvl, _n, msg in layered["log"] if lvl == "WARNING"]
    assert not _log_lines(layered, "INFO", INFO_LINE_OFF), "not an agreement"
    (runtime_split,) = [m for m in warnings if "at runtime" in m]
    assert "GET_FEATURE_FLAGS_FUNC evaluates the flag to True at runtime" in (
        runtime_split
    )
    assert "404s" in runtime_split
    (hooked,) = [m for m in warnings if "evaluated per request" in m]
    assert "GET_FEATURE_FLAGS_FUNC is set" in hooked
    ignored = [m for m in warnings if "ignored" in m]
    assert len(ignored) == 3, warnings
    assert any("SUPERSET_FEATURE_OBJECT_OWNERSHIP='true'" in m for m in ignored)
    assert any(
        "FEATURE_FLAGS['OBJECT_OWNERSHIP']=True set by an earlier config layer" in m
        for m in ignored
    )
    assert any(
        "OWNERSHIP_ENABLED='true' in the environment is ignored" in m
        and "config layer wins" in m
        for m in ignored
    )
    code, check = layered["cli"]["check"]
    assert code == 1, check
    assert check["ok"] is False
    assert (check["enabled_backend"], check["enabled_ui"]) == (False, False)
    assert check["enabled_ui_runtime"] is True
    assert check["ui_flag_hooked"] is True
    assert check["ui_flag_hooks"] == ["GET_FEATURE_FLAGS_FUNC"]
    assert check["flags_agree"] is False
    code, status = layered["cli"]["status"]
    assert code == 0, "status is a report, not a gate"
    assert status["enabled_ui_runtime"] is True

    # --- no package
    _assert_nothing_wired(nopkg, package=False)
    assert nopkg["ownership_tables"] == []
    _assert_stock(nopkg["fresh"])
    assert nopkg["runtime_bootstrap_flag"] is False
    (absent,) = _log_lines(nopkg, "WARNING", "package not importable")
    assert "superset ownership" in absent
    (inline,) = _log_lines(nopkg, "INFO", INFO_LINE_OFF)
    assert "package absent" in inline
    assert [r[1] for r in nopkg["log"]] == ["superset_ownership.config"] * 2


def test_flag_on_wires_every_extension_point_in_this_process(harness):
    """The complement of the subprocess: with the flag on, everything the
    flag-off run found absent is present, and is this module's code."""
    from superset import db
    from superset_ownership import flags, guard, hooks
    from superset_ownership.api import ownership_bp

    app = harness.app
    assert app.config["EXTRA_RAISE_FOR_ACCESS_BYPASS"] is hooks.raise_for_access_bypass
    assert app.config["EXTRA_EDITORS_RESOLVER"] is hooks.extra_editors
    assert app.config["AFTER_ASSET_CREATE"] is hooks.after_asset_create
    assert app.config["EXTRA_OWNERS_RESOLVER"] is hooks.owners_resolver
    assert app.config["EXTRA_ACCESS_QUERY_FILTERS"] == {
        "dashboards": hooks.dashboard_query_filter,
        "charts": hooks.chart_query_filter,
    }
    # The UI flag is on because the switch is: derived, agreeing, and the
    # report the CLI and check print says so.
    assert app.config["OWNERSHIP_ENABLED"] is True
    assert app.config["FEATURE_FLAGS"]["OBJECT_OWNERSHIP"] is True
    assert flags.report(app) == {
        "enabled_backend": True,
        "enabled_ui": True,
        "enabled_ui_runtime": True,
        "ui_flag_hooked": False,
        "ui_flag_hooks": [],
        "flags_agree": True,
    }
    assert ownership_bp.name in app.blueprints
    assert hooks.enforce_chart_data_access in app.before_request_funcs[None]
    assert guard._INSTALLED
    from sqlalchemy import event, inspect

    for name, fn in (
        ("before_flush", guard._before_flush),
        ("before_commit", guard._before_commit),
        ("after_commit", guard._after_commit),
        ("after_soft_rollback", guard._after_rollback),
    ):
        assert event.contains(db.session, name, fn), name
    with harness.ctx():
        tables = set(inspect(db.engine).get_table_names())
    assert {"ownership_object", "ownership_share", "ownership_outbox"} <= tables


# --- 15. WIRING --------------------------------------------------------------


class _CliRecorder:
    """Stands in for `app.cli`: records `add_command` instead of registering
    on the live app (the OFF mutator would replace the full group)."""

    def __init__(self) -> None:
        self.commands: dict[str, Any] = {}

    def add_command(self, cmd: Any, name: Optional[str] = None) -> None:
        self.commands[name or cmd.name] = cmd


class _MutatorProbe:
    """What FLASK_APP_MUTATOR is handed: the harness app, except that
    `before_request` is recorded instead of registered (Flask refuses new
    setup on an app that has served a request) and so is `cli.add_command`,
    so the deployed mutator can be run against the live application without
    changing it."""

    def __init__(self, app: Any) -> None:
        self._app = app
        self.before_request_funcs: list[Any] = []
        self.cli = _CliRecorder()

    def before_request(self, fn: Any) -> Any:
        self.before_request_funcs.append(fn)
        return fn

    def __getattr__(self, name: str) -> Any:
        return getattr(self._app, name)


def test_deployed_config_wires_the_same_hooks_as_the_suite(harness, monkeypatch):
    """The generated test config wires the hooks explicitly. The deployed
    config (SUPERSET_CONFIG_PATH, superset_config_docker_light.py in the
    container) must wire the SAME functions, or the suite would be proving
    properties of a wiring nobody runs. Compared by identity, not by name.

    The two controls the hooks do not cover are installed by the deployed
    FLASK_APP_MUTATOR: the flush guard and the chart-data before_request.
    Running that mutator against the live application (with `before_request`
    recorded rather than registered) shows what it installs; a deployed
    mutator that dropped either would leave the suite proving a guard and a
    gate that production does not have.

    Skipped where the environment's config does not enable the feature
    (nothing to compare against)."""
    import superset.config as deployed
    from superset_ownership import flags, guard, hooks, outbox
    from superset_ownership.api import ownership_bp

    if getattr(deployed, "EXTRA_RAISE_FOR_ACCESS_BYPASS", None) is None:
        pytest.skip(
            "the environment's Superset config does not wire the ownership hooks"
        )
    assert deployed.EXTRA_RAISE_FOR_ACCESS_BYPASS is hooks.raise_for_access_bypass
    assert deployed.EXTRA_EDITORS_RESOLVER is hooks.extra_editors
    assert deployed.AFTER_ASSET_CREATE is hooks.after_asset_create
    assert deployed.EXTRA_OWNERS_RESOLVER is hooks.owners_resolver
    assert deployed.EXTRA_ACCESS_QUERY_FILTERS == {
        "dashboards": hooks.dashboard_query_filter,
        "charts": hooks.chart_query_filter,
    }
    assert ownership_bp in deployed.BLUEPRINTS
    # One switch: the deployed config derives the UI flag from the setting
    # (issue #69), and the suite's generated config derives it the same way.
    assert deployed.OWNERSHIP_ENABLED is True
    assert deployed.FEATURE_FLAGS.get("OBJECT_OWNERSHIP") is deployed.OWNERSHIP_ENABLED
    assert (
        harness.app.config["FEATURE_FLAGS"]["OBJECT_OWNERSHIP"]
        is harness.app.config["OWNERSHIP_ENABLED"]
    )

    # The outbox: the suite runs with it on (every share and revocation is
    # observed as an outbox row and a later drain). The deployed value is the
    # config's if it sets one, else the environment's, else on -- the same
    # resolution outbox.enabled() makes at runtime.
    deployed_outbox = getattr(deployed, "OWNERSHIP_OUTBOX_ENABLED", None)
    if deployed_outbox is None:
        deployed_outbox = os.environ.get("OWNERSHIP_OUTBOX_ENABLED", "true")
    with harness.ctx():
        suite_outbox = outbox.enabled()
    assert suite_outbox is True
    assert str(deployed_outbox).lower() not in ("0", "false", "no"), (
        "the deployed config turns the outbox off; the suite's revocation and "
        "delete guarantees are stated about outbox mode"
    )
    # The shared lookup cache: on in both (the suite's one-cached-lookup
    # claims are about a deployment that has the layer).
    assert deployed.OWNERSHIP_LOOKUP_CACHE_TTL > 0
    assert harness.app.config["OWNERSHIP_LOOKUP_CACHE_TTL"] > 0

    # The mutator. Spy on what it may install; the guard's own install() is
    # idempotent and already ran for this process, so the CALL is what is
    # observed, not the flag.
    from superset.extensions import csrf

    installed: list[str] = []
    exempted: list[Any] = []
    real_install = guard.install
    monkeypatch.setattr(
        guard, "install", lambda: (installed.append("guard"), real_install())[1]
    )
    real_exempt = csrf.exempt
    monkeypatch.setattr(
        csrf, "exempt", lambda view: (exempted.append(view), real_exempt(view))[1]
    )
    reported: list[Any] = []
    real_warn = flags.warn_if_flags_disagree
    monkeypatch.setattr(
        flags,
        "warn_if_flags_disagree",
        lambda app: (reported.append(app), real_warn(app))[1],
    )
    probe = _MutatorProbe(harness.app)
    deployed.FLASK_APP_MUTATOR(probe)

    assert installed == ["guard"], "the deployed mutator installs the flush guard"
    assert reported == [probe], (
        "the deployed mutator reports the two switches once, after every "
        "config layer has run"
    )
    assert probe.before_request_funcs == [hooks.enforce_chart_data_access], (
        "the deployed mutator registers the chart-data gate, and nothing else"
    )
    assert ownership_bp in exempted, "the blueprint is CSRF-exempt (bearer auth)"
    assert guard._INSTALLED


# --- 16. ONE SWITCH ----------------------------------------------------------


def _cli(harness, *args: str):
    from superset_ownership import cli

    result = harness.app.test_cli_runner().invoke(cli.ownership, list(args))
    payload = json.loads(result.output) if result.output.strip() else None
    return result.exit_code, payload


SWITCH_KEYS = (
    "enabled_backend",
    "enabled_ui",
    "enabled_ui_runtime",
    "ui_flag_hooked",
    "flags_agree",
)


def _switches(payload: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(payload[k] for k in SWITCH_KEYS)


def test_status_and_check_report_both_switches(harness):
    """`superset ownership status` and `check` print `enabled_backend`
    (OWNERSHIP_ENABLED), `enabled_ui` (the feature flag as the config left
    it) and `enabled_ui_runtime` (the flag through the feature-flag manager,
    what the UI reads) side by side, with `ui_flag_hooked` and
    `flags_agree`; on a correctly derived config with no hook all three are
    on and nothing is hooked."""
    code, status = _cli(harness, "status")
    assert code == 0, status
    assert "enabled" not in status, "replaced by the named values"
    assert _switches(status) == (True, True, True, False, True)
    assert status["ui_flag_hooks"] == []
    assert status["backend_loaded"] is True

    code, report = _cli(harness, "check")
    assert code == 0, {k: v for k, v in report.items() if isinstance(v, list) and v}
    assert report["ok"] is True
    assert _switches(report) == (True, True, True, False, True)
    assert report["backend_loaded"] is True


def test_check_fails_when_a_later_layer_overrides_the_ui_flag(harness, monkeypatch):
    """The flag set by hand to the other value after the config derived
    it: `check` reports both, `flags_agree` false, `ok` false, exit 1 --
    so a deployment gate catches the backend enforcing with no UI to
    manage it. `status` reports the same values and stays exit 0 (it is a
    report, not a gate)."""
    overridden = {**harness.app.config["FEATURE_FLAGS"], "OBJECT_OWNERSHIP": False}
    monkeypatch.setitem(harness.app.config, "FEATURE_FLAGS", overridden)

    code, report = _cli(harness, "check")
    assert code == 1, report
    assert report["ok"] is False
    assert (report["enabled_backend"], report["enabled_ui"], report["flags_agree"]) == (
        True,
        False,
        False,
    )
    assert report["silently_open"] == [], "the switches alone failed the check"
    assert report["orphaned_sentinel"] == []

    code, status = _cli(harness, "status")
    assert code == 0
    assert (status["enabled_backend"], status["enabled_ui"]) == (True, False)


def test_check_fails_when_a_runtime_hook_moves_the_ui_flag(
    harness, monkeypatch, caplog
):
    """Probe D2 of the review: the static config agrees (both on), but
    IS_FEATURE_ENABLED_FUNC answers False for the flag -- and that is the
    value the frontend reads (`get_feature_flags()` in the bootstrap
    payload), so the instance enforces with no UI. `check` reads the flag
    through the feature-flag manager as well, reports `enabled_ui_runtime`
    False and `ui_flag_hooked` True, and fails; `status` reports the same
    and stays a report; the deployed mutator logs the split at WARNING
    naming the hook, plus the WARNING that a hook is configured at all."""
    import superset.config as deployed
    from superset.extensions import feature_flag_manager
    from superset_ownership import guard

    def hook(name: str, default: bool) -> bool:
        return False if name == "OBJECT_OWNERSHIP" else default

    monkeypatch.setitem(harness.app.config, "IS_FEATURE_ENABLED_FUNC", hook)
    monkeypatch.setattr(feature_flag_manager, "_is_feature_enabled_func", hook)
    with harness.ctx():
        # What common_bootstrap_payload() hands the frontend.
        assert feature_flag_manager.get_feature_flags()["OBJECT_OWNERSHIP"] is False
    assert harness.app.config["FEATURE_FLAGS"]["OBJECT_OWNERSHIP"] is True

    code, report = _cli(harness, "check")
    assert code == 1, report
    assert report["ok"] is False
    assert _switches(report) == (True, True, False, True, False)
    assert report["ui_flag_hooks"] == ["IS_FEATURE_ENABLED_FUNC"]
    assert report["silently_open"] == [], "the switches alone failed the check"

    code, status = _cli(harness, "status")
    assert code == 0
    assert _switches(status) == (True, True, False, True, False)

    monkeypatch.setattr(guard, "install", lambda: None)
    caplog.set_level(logging.INFO, logger="superset_ownership")
    deployed.FLASK_APP_MUTATOR(_MutatorProbe(harness.app))
    lines = [r for r in caplog.records if r.name == "superset_ownership.flags"]
    assert {r.levelno for r in lines} == {logging.WARNING}, [
        r.getMessage() for r in lines
    ]
    messages = [r.getMessage() for r in lines]
    (split,) = [m for m in messages if "at runtime" in m]
    assert (
        "OWNERSHIP_ENABLED=True and FEATURE_FLAGS['OBJECT_OWNERSHIP']=True agree"
    ) in split
    assert "IS_FEATURE_ENABLED_FUNC evaluates the flag to False at runtime" in split
    assert "born private" in split
    (hooked,) = [m for m in messages if "evaluated per request" in m]
    assert "IS_FEATURE_ENABLED_FUNC is set" in hooked
    (not_ok,) = [
        r.getMessage()
        for r in caplog.records
        if r.name == "superset_ownership.lifecycle" and "not ok" in r.getMessage()
    ]
    assert "at runtime False" in not_ok


def test_check_fails_when_a_hook_cannot_be_evaluated_headless(
    harness, monkeypatch, caplog
):
    """Probe I4 of the review: an IS_FEATURE_ENABLED_FUNC that reads g.user
    for this flag -- a per-user rollout hook -- raises under the bare app
    context the boot report and `check` evaluate it in (no request, no
    user). The boot goes on and says the value is unknown; `status` reports
    `enabled_ui_runtime` null and `flags_agree` true (nothing disagreed);
    `check` FAILS, because the half it exists to check is unknown, with
    `runtime_unknown` saying the hook raised and what to verify by hand."""
    import superset.config as deployed
    from flask import g
    from superset.extensions import feature_flag_manager

    def hook(name: str, default: bool) -> bool:
        return bool(g.user) if name == "OBJECT_OWNERSHIP" else default

    monkeypatch.setitem(harness.app.config, "IS_FEATURE_ENABLED_FUNC", hook)
    monkeypatch.setattr(feature_flag_manager, "_is_feature_enabled_func", hook)
    with harness.ctx():
        with pytest.raises(AttributeError):
            feature_flag_manager.is_feature_enabled("OBJECT_OWNERSHIP")

    code, status = _cli(harness, "status")
    assert code == 0, status
    assert _switches(status) == (True, True, None, True, True)
    assert "runtime_unknown" not in status, "status is a report"

    code, report = _cli(harness, "check")
    assert code == 1, report
    assert report["ok"] is False
    assert _switches(report) == (True, True, None, True, True)
    assert report["silently_open"] == [], "the unknown runtime value alone failed it"
    assert "IS_FEATURE_ENABLED_FUNC is configured" in report["runtime_unknown"]
    assert "the hook raised" in report["runtime_unknown"]
    assert "Verify the flag per user" in report["runtime_unknown"]

    # The boot log: the evaluation WARNING, the INFO that the static values
    # agree, the standing hook WARNING -- and the startup check's own
    # re-evaluation of the report, which raises the same way.
    from superset_ownership import guard

    monkeypatch.setattr(guard, "install", lambda: None)
    caplog.set_level(logging.INFO, logger="superset_ownership")
    caplog.clear()
    deployed.FLASK_APP_MUTATOR(_MutatorProbe(harness.app))
    lines = [r for r in caplog.records if r.name == "superset_ownership.flags"]
    assert [r.levelno for r in lines] == [
        logging.WARNING,
        logging.INFO,
        logging.WARNING,
        logging.WARNING,
    ], [r.getMessage() for r in lines]
    unevaluated, agree, standing, again = (r.getMessage() for r in lines)
    assert "could not be evaluated" in unevaluated
    assert "AttributeError" in unevaluated
    assert "one switch" in agree
    assert "anonymous caller (not evaluated)" in standing
    assert "could not be evaluated" in again, "the startup check asked once more"
    assert not [
        r
        for r in caplog.records
        if r.name == "superset_ownership.lifecycle" and "not ok" in r.getMessage()
    ], "the startup check is not the gate; it reports, check fails"


def test_a_hook_that_agrees_is_reported_but_does_not_fail_check(
    harness, monkeypatch, caplog
):
    """A GET_FEATURE_FLAGS_FUNC that leaves the flag alone: `check` passes,
    `ui_flag_hooked` is true, and the boot log still WARNs that a hook is
    configured -- the report sees the hook's answer for an anonymous caller
    only, and says so."""
    import superset.config as deployed
    from superset.extensions import feature_flag_manager
    from superset_ownership import guard

    def hook(feature_flags: dict[str, bool]) -> dict[str, bool]:
        return feature_flags

    monkeypatch.setitem(harness.app.config, "GET_FEATURE_FLAGS_FUNC", hook)
    monkeypatch.setattr(feature_flag_manager, "_get_feature_flags_func", hook)

    code, report = _cli(harness, "check")
    assert code == 0, report
    assert _switches(report) == (True, True, True, True, True)
    assert report["ui_flag_hooks"] == ["GET_FEATURE_FLAGS_FUNC"]

    monkeypatch.setattr(guard, "install", lambda: None)
    caplog.set_level(logging.INFO, logger="superset_ownership.flags")
    deployed.FLASK_APP_MUTATOR(_MutatorProbe(harness.app))
    lines = [r for r in caplog.records if r.name == "superset_ownership.flags"]
    assert [r.levelno for r in lines] == [logging.INFO, logging.WARNING]
    assert "one switch" in lines[0].getMessage()
    assert "GET_FEATURE_FLAGS_FUNC is set" in lines[1].getMessage()
    assert "anonymous caller (True)" in lines[1].getMessage()


class _Ran:
    """An operator's FLASK_APP_MUTATOR: records the app it was handed."""

    def __init__(self) -> None:
        self.apps: list[Any] = []

    def __call__(self, app: Any) -> None:
        self.apps.append(app)


@pytest.mark.parametrize("enabled", [False, True], ids=["off", "on"])
def test_deployed_config_chains_an_earlier_layer_in_both_states(
    harness, monkeypatch, caplog, enabled
):
    """The light config is exec'd after `from superset_config import *`, so
    whatever an earlier layer set is a name in its namespace. Exec the
    deployed file with a stand-in for that layer (a `superset_config` module
    carrying an operator mutator and blueprints, OWNERSHIP_ENABLED as a
    Python setting and the flag set by hand) and the environment saying the
    opposite about the switch, in both states:

      * the operator's mutator is chained -- by BOTH mutators, not
        superset.config's None -- and its blueprints survive;
      * the Python setting wins over the environment and is recorded as
        the source; the env spelling is reported as ignored;
      * the earlier layer's flag is overwritten by the derivation and
        recorded for the "ignored" report;
      * the OFF mutator registers the flags-only group, the ON one the full
        group.
    """
    from superset_ownership import cli, guard
    from superset_ownership.api import ownership_bp

    if "SUPERSET_CONFIG_PATH" not in os.environ:
        pytest.skip("no SUPERSET_CONFIG_PATH: nothing deployed to exec")
    operator = _Ran()
    marker_bp = object()
    layer = types.ModuleType("superset_config")
    layer.FEATURE_FLAGS = {"OBJECT_OWNERSHIP": not enabled, "SOFT_DELETE": True}
    layer.OWNERSHIP_ENABLED = enabled
    layer.FLASK_APP_MUTATOR = operator
    layer.BLUEPRINTS = [marker_bp]
    monkeypatch.setitem(sys.modules, "superset_config", layer)
    monkeypatch.setenv("OWNERSHIP_ENABLED", "false" if enabled else "true")
    monkeypatch.delenv("SUPERSET_FEATURE_OBJECT_OWNERSHIP", raising=False)
    ns = runpy.run_path(os.environ["SUPERSET_CONFIG_PATH"])

    assert ns["OWNERSHIP_ENABLED"] is enabled, "the Python setting wins"
    assert ns["OWNERSHIP_ENABLED_SOURCE"] == "config"
    assert ns["OWNERSHIP_INHERITED_UI_FLAG"] is (not enabled)
    assert ns["FEATURE_FLAGS"]["OBJECT_OWNERSHIP"] is enabled, "derived, once"
    assert ns["FEATURE_FLAGS"]["SOFT_DELETE"] is True, "the layer's other flags survive"
    if enabled:
        assert ns["BLUEPRINTS"] == [marker_bp, ownership_bp]
    else:
        assert ns["BLUEPRINTS"] == [marker_bp]

    monkeypatch.setattr(guard, "install", lambda: None)
    caplog.set_level(logging.INFO, logger="superset_ownership")
    probe = _MutatorProbe(harness.app)
    ns["FLASK_APP_MUTATOR"](probe)
    assert operator.apps == [probe], "the operator's mutator ran, once, first"
    (group,) = probe.cli.commands.values()
    assert group.name == "ownership"
    if enabled:
        assert group is cli.ownership
    else:
        assert sorted(group.commands) == ["check", "status"]
    # The report ran against the harness app (its config says the switch
    # came from a config layer); with the environment saying the opposite
    # the line naming the env spelling is there. Env "false" against a layer
    # that turned the feature on is compose's default, not a conflict: an
    # INFO account with no remedy (probe F), never a standing WARNING.
    lines = [
        r
        for r in caplog.records
        if r.name == "superset_ownership.flags"
        and "config layer wins" in r.getMessage()
    ]
    if enabled:
        (line,) = lines
        assert line.levelno == logging.INFO
        assert "OWNERSHIP_ENABLED='false' in the environment is the default" in (
            line.getMessage()
        )
        assert "Remove" not in line.getMessage()
    else:
        assert lines == [], "env 'true' agrees with the harness app's switch"


def test_deployed_mutator_logs_both_switches_and_warns_on_a_disagreement(
    harness, monkeypatch, caplog
):
    """Boot-log contract, on the deployed FLASK_APP_MUTATOR: one INFO line
    naming both values when they agree; one WARNING naming both, the state
    that results and the remedy when they do not."""
    import logging

    import superset.config as deployed
    from superset_ownership import guard

    if getattr(deployed, "FLASK_APP_MUTATOR", None) is None:
        pytest.skip("the environment's Superset config defines no mutator")
    # The rest of the deployed mutator is exercised by the wiring test; here
    # only the report matters, so the guard's install (idempotent) and the
    # startup check (already run for this process) are left as they are.
    monkeypatch.setattr(guard, "install", lambda: None)
    caplog.set_level(logging.INFO, logger="superset_ownership.flags")

    deployed.FLASK_APP_MUTATOR(_MutatorProbe(harness.app))
    lines = [r for r in caplog.records if r.name == "superset_ownership.flags"]
    assert [r.levelno for r in lines] == [logging.INFO]
    assert "OWNERSHIP_ENABLED=True" in lines[0].getMessage()
    assert "FEATURE_FLAGS['OBJECT_OWNERSHIP']=True" in lines[0].getMessage()

    caplog.clear()
    overridden = {**harness.app.config["FEATURE_FLAGS"], "OBJECT_OWNERSHIP": False}
    monkeypatch.setitem(harness.app.config, "FEATURE_FLAGS", overridden)
    deployed.FLASK_APP_MUTATOR(_MutatorProbe(harness.app))
    lines = [r for r in caplog.records if r.name == "superset_ownership.flags"]
    assert [r.levelno for r in lines] == [logging.WARNING]
    message = lines[0].getMessage()
    assert (
        "OWNERSHIP_ENABLED=True but FEATURE_FLAGS['OBJECT_OWNERSHIP']=False" in message
    )
    assert "no control in the UI" in message
    assert "must not be set on its own" in message
    # and the startup check that follows in the same mutator says why it is
    # not ok, pointing at that line rather than repeating it
    lifecycle_lines = [
        r.getMessage()
        for r in caplog.records
        if r.name == "superset_ownership.lifecycle" and "not ok" in r.getMessage()
    ]
    assert lifecycle_lines
    assert "flags warning above" in lifecycle_lines[0]


# --- 17. USER HARD-DELETE ----------------------------------------------------
# Revision 0003 (qa/reviews/decision-owner-fk.md): owner_user_id -> ab_user.id
# ON DELETE SET NULL. The harness database is SQLite without PRAGMA
# foreign_keys, so the SET NULL itself is not exercised here (test_migrations
# does that with the pragma on, and the PostgreSQL probe for real); what is
# exercised is the module's part -- the audit record and the backfill.


def _null_owner(harness, ref):
    """What the constraint does on PostgreSQL when the owner is hard-deleted."""
    import sqlalchemy as sa
    from superset_ownership import service
    from superset_ownership.db import ownership_object

    with harness.ctx():
        from superset import db

        db.session.execute(
            sa.update(ownership_object)
            .where(
                ownership_object.c.asset_type == ref.asset_type,
                ownership_object.c.object_id == ref.id,
            )
            .values(owner_user_id=None)
        )
        db.session.commit()
        service.invalidate_all()


def test_hard_deleting_a_user_emits_one_event_per_object_they_owned(harness):
    """The guard's `before_delete` listener on FAB's User, installed by the
    same `install()` the deployed mutator calls: a real `session.delete`
    of a real user who owns two objects (by transfer, so their deletion is
    not blocked by created_by_fk on a foreign-key-enforcing database) leaves
    two events after the commit, each naming the owner the row is losing."""
    from superset_ownership import audit, guard

    w = harness.world
    leaver = harness.add_person(guid(0x40), "Leaver", "Gamma", "sales_readers")
    chart = harness.create_chart(w.ada, "leaver-chart")
    dash = harness.create_dashboard(w.ada, title="leaver-dash")
    for ref in (chart, dash):
        r = harness.put(
            w.ada,
            f"/api/v1/ownership/{ref.asset_type}/{ref.id}/owner",
            {"subject": leaver.ref},
        )
        assert r.status_code == 200, r.get_json()
    kept = harness.create_chart(w.ada, "ada-keeps-this")
    harness.audit.clear()

    with harness.ctx():
        from sqlalchemy import event
        from superset import db, security_manager

        assert event.contains(
            security_manager.user_model, "before_delete", guard._before_user_delete
        ), "install() attached the listener to FAB's User"
        user = security_manager.get_user_by_id(leaver.id)
        db.session.delete(user)
        db.session.flush()
        assert harness.events(audit.OWNER_REMOVED_BY_USER_DELETE) == [], (
            "nothing is emitted before the delete commits"
        )
        db.session.commit()
        assert security_manager.get_user_by_id(leaver.id) is None

    events = harness.events(audit.OWNER_REMOVED_BY_USER_DELETE)
    assert sorted((e["object"]["type"], e["object"]["id"]) for e in events) == sorted(
        [("chart", chart.id), ("dashboard", dash.id)]
    )
    for e in events:
        assert e["before"] == {"owner_user_id": leaver.id}
        assert e["after"] == {"owner_user_id": None}
        assert e["object"]["uuid"] in (chart.uuid, dash.uuid)
    charts_named = {e["object"]["id"] for e in events if e["object"]["type"] == "chart"}
    assert kept.id not in charts_named


def test_hard_deleting_a_user_while_the_ownership_tables_are_absent_succeeds(
    harness, tmp_path, caplog
):
    """The state OWNERSHIP_AUTO_MIGRATE=false puts every worker in until
    the operator's first `superset ownership db upgrade` (and every running
    worker after `superset ownership teardown --confirm`): the listener is
    on FAB's User, the tables are not there. The delete must go through --
    on PostgreSQL a SELECT against the missing table aborts the flush's
    transaction and the DELETE that follows fails, so the listener asks the
    inspector first and never issues it. Asserted here on the real
    application: the user is deleted, no event, one INFO line, and no
    statement reads `ownership_object` (the shape that keeps PostgreSQL
    safe; the pure suite runs it against PostgreSQL when a throwaway
    database is named). The session database is snapshotted before and
    restored after, so the rest of the suite sees its tables and rows."""
    import logging
    import shutil

    import sqlalchemy as sa
    from sqlalchemy import event
    from superset_ownership import audit, guard, service

    snapshot_dir = harness.snapshot_database(str(tmp_path / "before"))
    snapshot = os.path.join(snapshot_dir, "superset.db")
    uri = harness.app.config["SQLALCHEMY_DATABASE_URI"]
    live = uri[len("sqlite:///") :].split("?", 1)[0]
    leaver = harness.add_person(guid(0x43), "Leaver-nt", "Gamma", "sales_readers")
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.lower().split()))

    try:
        with harness.ctx():
            from superset import db, security_manager

            assert event.contains(
                security_manager.user_model, "before_delete", guard._before_user_delete
            )
            with db.engine.begin() as conn:
                conn.execute(sa.text("DROP TABLE ownership_share"))
                conn.execute(sa.text("DROP TABLE ownership_object"))
            assert not sa.inspect(db.engine).has_table("ownership_object")
            harness.audit.clear()
            caplog.set_level(logging.DEBUG, logger="superset_ownership.guard")
            event.listen(db.engine, "before_cursor_execute", record)
            try:
                db.session.delete(security_manager.get_user_by_id(leaver.id))
                db.session.commit()
            finally:
                event.remove(db.engine, "before_cursor_execute", record)
            assert security_manager.get_user_by_id(leaver.id) is None, "deleted"
    finally:
        with harness.ctx():
            from superset import db

            db.session.remove()
            db.engine.dispose()
        shutil.copyfile(snapshot, live)
        service.invalidate_all()

    assert harness.events(audit.OWNER_REMOVED_BY_USER_DELETE) == []
    assert not [st for st in statements if "from ownership_object" in st], (
        "no statement was issued against the absent table"
    )
    guard_lines = [
        (r.levelno, r.getMessage(), r.exc_info)
        for r in caplog.records
        if r.name == "superset_ownership.guard"
    ]
    assert guard_lines == [
        (
            logging.INFO,
            f"superset_ownership: user {leaver.id} is being deleted while "
            "ownership_object does not exist (the ownership chain has not run, or "
            "was torn down); nothing to record",
            None,
        )
    ]
    with harness.ctx():
        from superset import db

        assert sa.inspect(db.engine).has_table("ownership_object"), "restored"


def test_the_next_backfill_re_homes_an_object_whose_owner_was_hard_deleted(harness):
    """Before 0003 a dangling owner id counted as a real owner and the
    backfill skipped the row for good. After 0003 the row reads NULL, so
    the next sweep attributes it -- here to its creator -- keeping its
    visibility. A DEACTIVATED owner is still a real owner: that row is not
    touched (`is_unowned` handles it at read time)."""
    from superset_ownership import backfill

    w = harness.world
    gone = harness.add_person(guid(0x41), "Gone", "Gamma", "sales_readers")
    dormant = harness.add_person(guid(0x42), "Dormant", "Gamma", "sales_readers")
    re_homed = harness.create_chart(w.ada, "re-homed")
    left_alone = harness.create_chart(w.ada, "left-alone")
    for ref, person in ((re_homed, gone), (left_alone, dormant)):
        r = harness.put(
            w.ada, f"/api/v1/ownership/chart/{ref.id}/owner", {"subject": person.ref}
        )
        assert r.status_code == 200, r.get_json()
    r = harness.set_visibility(gone, re_homed, "private")
    assert r.status_code == 200, r.get_json()

    # The hard delete, and the constraint's effect on the row.
    with harness.ctx():
        from superset import db, security_manager

        db.session.delete(security_manager.get_user_by_id(gone.id))
        db.session.commit()
    _null_owner(harness, re_homed)
    harness.set_active(dormant, False)
    assert harness.row(re_homed).owner_user_id is None
    assert harness.row(left_alone).owner_user_id == dormant.id

    with harness.ctx():
        backfill.run()

    after = harness.row(re_homed)
    assert after.owner_user_id == w.ada.id, "re-attributed to the creator"
    assert after.visibility == "private", "the repair keeps the visibility"
    assert harness.row(left_alone).owner_user_id == dormant.id, (
        "a deactivated owner is not a missing one"
    )


def test_backfill_ignores_a_default_owner_that_names_no_user(harness, caplog):
    """OWNERSHIP_DEFAULT_OWNER is an integer from the environment; with the
    owner foreign key in place a typo would be a raw IntegrityError that
    aborts the sweep and rolls every object back. It is validated once, up
    front, logged, and ignored for the run."""
    import logging

    from superset_ownership import backfill

    w = harness.world
    harness.create_chart(w.ada, "default-owner-probe")
    harness.app.config["OWNERSHIP_DEFAULT_OWNER"] = 987654
    caplog.set_level(logging.INFO, logger="superset_ownership.backfill")
    try:
        with harness.ctx():
            backfill.run()
    finally:
        harness.app.config.pop("OWNERSHIP_DEFAULT_OWNER", None)

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("OWNERSHIP_DEFAULT_OWNER=987654 names no user" in m for m in warnings)


# --- 18. SHARE INSERT DEFECT -------------------------------------------------


def test_an_unclassifiable_integrity_error_on_the_share_insert_answers_a_fixed_500(
    harness, caplog
):
    """Probe P6 of the round-3 review. A `BEFORE INSERT` trigger on
    `ownership_share` raises a message that names neither UNIQUE nor
    FOREIGN KEY -- the shape of a CHECK or NOT NULL on a dialect that names
    no constraint. The service classifies it as neither race, logs the
    driver text at ERROR and re-raises; the route must catch it, roll
    back, and answer 500 with a fixed sentence. Left to Superset's generic
    handler the body for a non-guest caller carried the driver message,
    the INSERT and its parameters (the leak #74 closed for the transfer
    route with the same rule). Nothing is written and nothing is queued."""
    import logging

    import sqlalchemy as sa
    from superset_ownership.db import ownership_share

    w = harness.world
    chart = harness.create_chart(w.ada, "share-insert-defect")
    # H1 (review round 1, issue #93): a share write is refused outright on a
    # still-private object, before the insert this probe means to exercise.
    harness.set_visibility(w.ada, chart, "shared")
    outbox_before = len(harness.outbox())
    trigger = (
        "CREATE TRIGGER ownership_share_probe BEFORE INSERT ON ownership_share "
        "BEGIN SELECT RAISE(ABORT, "
        "'probe: column constraint failed on host metadb'); END"
    )
    caplog.set_level(logging.INFO, logger="superset_ownership")
    with harness.ctx():
        from superset import db

        with db.engine.begin() as conn:
            conn.execute(sa.text(trigger))
    try:
        r = harness.post(
            w.ada,
            f"/api/v1/ownership/chart/{chart.id}/shares",
            {"subject": w.ben.ref, "role": "viewer"},
        )
    finally:
        with harness.ctx():
            from superset import db

            db.session.rollback()
            with db.engine.begin() as conn:
                conn.execute(sa.text("DROP TRIGGER ownership_share_probe"))

    assert r.status_code == 500, r.get_json()
    body = r.get_json()
    assert body == {"message": "the share could not be recorded; nothing was changed"}
    raw = r.get_data(as_text=True)
    for fragment in (
        "INSERT",
        "parameters",
        "metadb",
        "sqlite3",
        "IntegrityError",
        "probe",
        w.ben.ref,
        "sqlalche.me",
    ):
        assert fragment not in raw, fragment

    with harness.ctx():
        from superset import db

        shares = db.session.execute(
            sa.select(sa.func.count())
            .select_from(ownership_share)
            .where(ownership_share.c.object_id == chart.id)
        ).scalar()
    assert shares == 0, "nothing written"
    assert len(harness.outbox()) == outbox_before, "nothing queued"
    assert harness.authorizer.count("write_tuple") == 0

    errors = [
        (r.name, r.getMessage())
        for r in caplog.records
        if r.levelno == logging.ERROR and r.name.startswith("superset_ownership")
    ]
    assert [name for name, _ in errors] == [
        "superset_ownership.service",
        "superset_ownership.api",
    ]
    assert "neither the unique race nor the object foreign key" in errors[0][1]
    assert "metadb" in errors[0][1], "the driver text is in the log, not the body"
    assert "answered 500 with a fixed body" in errors[1][1]
    assert harness.events("ownership.share_added") == []


# --- a tenant administrator reads every object of their tenant (PR #116) --------------


def _tenant_admin_world(harness, base: int):
    """Ana (owner) and Tam (tenant administrator) in tenant A, Cleo the
    administrator of tenant B, plus one private chart of Ana's on a
    published dashboard."""
    from superset_ownership.identity import tenant_role_name

    tenant_a, tenant_b = guid(0xA), guid(0xB)
    role_a, role_b = tenant_role_name(tenant_a), tenant_role_name(tenant_b)
    ana = harness.add_person(guid(base), f"Ana{base}", "Gamma", "sales_readers", role_a)
    tam = harness.add_person(
        guid(base + 1), f"Tam{base}", "Gamma", "sales_readers", role_a
    )
    cleo = harness.add_person(
        guid(base + 2), f"Cleo{base}", "Gamma", "sales_readers", role_b
    )
    harness.authorizer.make_tenant_administrator(tam.ref, tenant_a)
    harness.authorizer.make_tenant_administrator(cleo.ref, tenant_b)
    chart = harness.create_chart(ana, f"admin-reads-{base}")
    dash = harness.create_dashboard(ana, chart, title=f"admin-reads-dash-{base}")
    harness.drain()
    assert harness.row(chart).visibility == "private"
    assert harness.row(dash).visibility == "private"
    assert harness.row(chart).tenant_guid == tenant_a
    return tenant_a, ana, tam, cleo, chart, dash


def test_tenant_administrator_reads_every_object_of_their_tenant(harness):
    """Option B (the user's call after the manual round): an object hidden
    from the tenant administrator is one they cannot re-home -- the row to
    click "Sharing" on is not there. So the administrator lists, opens and
    loads the data of every object of their own tenant, private or shared,
    from the row's mirrored tenant and one cached administrator check, with
    no store `check` on the object; the other tenant's administrator still
    gets nothing; and the sharing itself stays the owner's (403)."""
    from superset.dashboards.api import DashboardRestApi

    tenant_a, ana, tam, cleo, chart, dash = _tenant_admin_world(harness, 0x150)

    for ref in (chart, dash):
        d = harness.decide(tam, ref)
        assert d.verdict == ALLOW, (ref, d)
        assert "check" not in d.names, d.names
        assert harness.decide(cleo, ref).verdict == DENY
        assert harness.detail(tam, ref).status_code == 200
        assert harness.detail(cleo, ref).status_code == 404
    assert chart.id in harness.listed_ids(tam, "chart")
    assert dash.id in harness.listed_ids(tam, "dashboard")
    assert chart.id not in harness.listed_ids(cleo, "chart")
    assert dash.id not in harness.listed_ids(cleo, "dashboard")

    # The chart's data and its tile, the same answer as opening it.
    r = harness.post(tam, "/api/v1/chart/data", harness.query_context(chart.id))
    assert r.status_code == 200, r.get_json()
    r = harness.post(cleo, "/api/v1/chart/data", harness.query_context(chart.id))
    assert r.status_code == 403
    with harness.acting_as(tam):
        tile = DashboardRestApi._serialize_dashboard_chart(
            DashboardRestApi(), harness._load(chart)
        )
    assert "has_access" not in tile
    assert "form_data" in tile

    # Reading is not sharing: the ownership detail says so, and the writes
    # answer as before (PR #112).
    r = harness.get(tam, f"/api/v1/ownership/chart/{chart.id}")
    assert r.status_code == 200
    body = r.get_json()
    assert (body["can_manage"], body["can_share"], body["manage_reason"]) == (
        True,
        False,
        "tenant_admin",
    )
    r = harness.post(
        tam, f"/api/v1/ownership/chart/{chart.id}/shares", {"subject": cleo.ref}
    )
    assert r.status_code == 403
    r = harness.put(
        tam, f"/api/v1/ownership/chart/{chart.id}/visibility", {"visibility": "public"}
    )
    assert r.status_code == 403
    # And the administrator can do the thing the read was for.
    r = harness.put(
        tam, f"/api/v1/ownership/chart/{chart.id}/owner", {"subject": tam.ref}
    )
    assert r.status_code == 200, r.get_json()
    assert harness.row(chart).owner_user_id == tam.id


def test_tenant_administrator_reads_an_unowned_object_of_their_tenant(harness):
    """Issue #102, closed by the same rule: an object whose owner has been
    deactivated is unreachable for everyone -- and was for the tenant
    administrator too, who is the one person meant to rescue it. Now the
    administrator lists and opens it, and claims it from the drawer."""
    tenant_a, ana, tam, cleo, chart, dash = _tenant_admin_world(harness, 0x160)
    harness.set_active(ana, False)
    try:
        assert harness.decide(tam, chart).verdict == ALLOW
        assert harness.decide(cleo, chart).verdict == DENY
        assert chart.id in harness.listed_ids(tam, "chart")
        r = harness.get(tam, f"/api/v1/ownership/chart/{chart.id}")
        assert r.status_code == 200
        assert r.get_json()["unowned"] is True
        assert r.get_json()["can_manage"] is True
        r = harness.post(tam, f"/api/v1/ownership/chart/{chart.id}/claim", {})
        assert r.status_code == 200, r.get_json()
        assert harness.row(chart).owner_user_id == tam.id
    finally:
        harness.set_active(ana, True)


def test_tenant_administrator_does_not_read_an_untenanted_or_foreign_object(harness):
    """The ground is the ROW's tenant: a row with no tenant mirrored belongs
    to no tenant, so no administrator reads it on this ground (its owner
    and the outside-every-tenant callers do, as before); and a row of
    another tenant is that tenant's administrator's, not this one's."""
    tenant_a, ana, tam, cleo, chart, dash = _tenant_admin_world(harness, 0x170)
    _blank_row_tenant(harness, chart)
    assert harness.row(chart).tenant_guid is None
    assert harness.decide(tam, chart).verdict == DENY
    assert chart.id not in harness.listed_ids(tam, "chart")
    assert harness.decide(ana, chart).verdict == ALLOW, "the owner, as ever"

    other = harness.create_chart(cleo, "tenant-b-private")
    harness.drain()
    assert harness.decide(cleo, other).verdict == ALLOW
    assert harness.decide(tam, other).verdict == DENY
    assert other.id not in harness.listed_ids(tam, "chart")
    assert other.id in harness.listed_ids(cleo, "chart")


def test_tenant_administrator_sees_a_members_draft_dashboard(harness):
    """Stock hides an unpublished dashboard from everyone but its owners;
    the administrator's list admits their tenant's drafts too (an
    administrator re-homes a draft as much as a published one), while the
    public-within-tenant set keeps stock's `published` condition."""
    tenant_a, ana, tam, cleo, chart, dash = _tenant_admin_world(harness, 0x180)
    with harness.ctx():
        from superset import db

        obj = harness._load(dash)
        obj.published = False
        db.session.commit()
    assert dash.id in harness.listed_ids(tam, "dashboard")
    assert dash.id not in harness.listed_ids(cleo, "dashboard")
    # A public draft of Ana's is not in a plain member's list (stock's
    # rule), but is in the administrator's.
    amy = harness.add_person(
        guid(0x183), "Amy180", "Gamma", "sales_readers", f"Tenant_{tenant_a}_Role"
    )
    harness.set_visibility(ana, dash, "public")
    assert dash.id not in harness.listed_ids(amy, "dashboard")
    assert dash.id in harness.listed_ids(tam, "dashboard")


def test_the_administrator_check_is_asked_once_per_request(harness):
    """`service.administered_tenant` is answered once per user per request
    (the tile marker asks per tile) and cached for the lookup TTL across
    requests: many decisions in one request cost one administrator check,
    and a caller who administers nothing costs one check too, not one per
    object."""
    from superset_ownership import service

    tenant_a, ana, tam, cleo, chart, dash = _tenant_admin_world(harness, 0x190)
    charts = [chart] + [harness.create_chart(ana, f"many-{i}") for i in range(4)]
    harness.drain()
    # The rows first: `harness.row` opens its own app context, which would
    # detach a user loaded before it.
    rows = [harness.row(ref) for ref in charts]
    with harness.acting_as(tam):
        harness.authorizer.calls.clear()
        user = harness._user(tam)
        for row in rows:
            assert service.tenant_admin_reads(row, user) is True
        assert [c[0] for c in harness.authorizer.calls].count("user_in_group") == 1
    with harness.acting_as(ana):
        harness.authorizer.calls.clear()
        user = harness._user(ana)
        for row in rows:
            assert service.tenant_admin_reads(row, user) is False
        assert [c[0] for c in harness.authorizer.calls].count("user_in_group") == 1


def test_owner_reads_with_no_administrator_check_even_during_an_outage(harness):
    """Review round 1 of PR #116: the administrator ground sits AFTER the
    owner fast path. An owner's own read costs no administrator check --
    and during a store outage, when a failing check is (rightly) never
    cached, an owner's dashboard is still one zero-call decision per tile."""
    tenant_a, ana, tam, cleo, chart, dash = _tenant_admin_world(harness, 0x1B0)
    d = harness.decide(ana, chart)
    assert d.verdict == ALLOW
    assert d.names == ()
    harness.authorizer.up = False
    try:
        for _ in range(2):
            d = harness.decide(ana, chart)
            assert d.verdict == ALLOW
            assert d.names == (), "an owner never asks the store, outage or not"
    finally:
        harness.authorizer.up = True


def test_tenant_administrator_lists_a_chartless_dashboard(harness):
    """A dashboard with no chart has no dataset to grant on; stock's inner
    join drops it from the public set (as stock itself does), but the
    administrator's set admits it -- an empty dashboard is re-homed too."""
    tenant_a, ana, tam, cleo, chart, dash = _tenant_admin_world(harness, 0x1C0)
    empty = harness.create_dashboard(ana, title="empty-1c0")
    harness.drain()
    assert empty.id in harness.listed_ids(tam, "dashboard")
    assert empty.id not in harness.listed_ids(cleo, "dashboard")
    assert harness.decide(tam, empty).verdict == ALLOW


def test_a_revoked_administrator_can_be_forgotten_without_waiting_for_the_ttl(
    harness,
):
    """Issue #130: a "yes" is shared for OWNERSHIP_LOOKUP_CACHE_TTL seconds
    and nothing invalidates it, so an administrator the platform has just
    demoted keeps reading that tenant's objects until the entry ages out.
    The TTL is the documented bound; this is how an operator stops waiting
    for it. (A GRANT needs nothing: a "no" is never shared.)"""
    from superset_ownership import plugin_hooks, service

    tenant_a, ana, tam, cleo, chart, dash = _tenant_admin_world(harness, 0x1F0)
    row = harness.row(chart)

    with harness.acting_as(tam):
        user = harness._user(tam)
        assert service.tenant_admin_reads(row, user) is True

    with harness.ctx():
        assert (
            plugin_hooks.cache_get(service._ADMINISTERED_TENANT_SETTING, "user", tam.id)
            is not plugin_hooks.MISS
        ), "the answer another request would be served"

    # The platform revokes the relation; nothing tells this module.
    admin_tuple = (tam.ref, "admin", f"tenant:{service.normalize_tenant(tenant_a)}")
    harness.authorizer.tuples.discard(admin_tuple)
    try:
        with harness.ctx():
            assert service.forget_administered_tenant(tam.id) is True
            assert (
                plugin_hooks.cache_get(
                    service._ADMINISTERED_TENANT_SETTING, "user", tam.id
                )
                is plugin_hooks.MISS
            )

        with harness.acting_as(tam):
            assert service.tenant_admin_reads(row, harness._user(tam)) is False, (
                "the next request asks the store, and the store says no"
            )
    finally:
        # The store outlives this test; every tenant in it keeps its
        # administrator, which other tests enumerate.
        harness.authorizer.tuples.add(admin_tuple)
        with harness.ctx():
            service.forget_administered_tenant(tam.id)


def test_forgetting_an_administrator_also_drops_the_hooks_own_answer(harness):
    """Review round 1. Where a deployment configures
    `OWNERSHIP_IS_TENANT_ADMINISTRATOR`, that hook's answer is cached in the
    same shared layer under its OWN key. Dropping only the derived one meant
    the recompute read the stale hook answer and re-cached the same "yes"
    with a fresh full TTL -- the command made the problem last longer."""
    from flask import current_app

    from superset_ownership import plugin_hooks, plugins, service

    tenant_a, ana, tam, cleo, chart, dash = _tenant_admin_world(harness, 0x300)
    row = harness.row(chart)
    answers = {"value": True}

    with harness.ctx():
        reg = current_app.extensions[plugins.EXT_KEY]
        reg.hooks.callables["OWNERSHIP_IS_TENANT_ADMINISTRATOR"] = lambda user: answers[
            "value"
        ]
    try:
        with harness.acting_as(tam):
            assert service.tenant_admin_reads(row, harness._user(tam)) is True

        with harness.ctx():
            assert (
                plugin_hooks.cache_get(
                    "OWNERSHIP_IS_TENANT_ADMINISTRATOR", "user", tam.id
                )
                is not plugin_hooks.MISS
            ), "the hook's own answer is cached, separately"

        # The platform demotes them; the hook would now say no.
        answers["value"] = False

        with harness.ctx():
            service.forget_administered_tenant(tam.id)
            assert (
                plugin_hooks.cache_get(
                    "OWNERSHIP_IS_TENANT_ADMINISTRATOR", "user", tam.id
                )
                is plugin_hooks.MISS
            ), "both keys go, or the recompute re-caches the stale yes"

        with harness.acting_as(tam):
            assert service.tenant_admin_reads(row, harness._user(tam)) is False
    finally:
        with harness.ctx():
            current_app.extensions[plugins.EXT_KEY].hooks.callables.pop(
                "OWNERSHIP_IS_TENANT_ADMINISTRATOR", None
            )
            service.forget_administered_tenant(tam.id)


def test_forgetting_an_administrator_nobody_cached_is_not_an_error(harness):
    from superset_ownership import service

    with harness.ctx():
        service.forget_administered_tenant(999999)


def test_a_negative_administrator_answer_is_not_shared_across_requests(harness):
    """A "no" is request-local: a member made administrator between two
    requests is one on the second, not after the TTL. (A "yes" is shared
    and outlives a revocation by at most the TTL -- documented.)"""
    from superset_ownership.identity import tenant_role_name

    tenant_a = guid(0xA)
    ana = harness.add_person(
        guid(0x1D0), "Ana1d0", "Gamma", "sales_readers", tenant_role_name(tenant_a)
    )
    tia = harness.add_person(
        guid(0x1D1), "Tia1d0", "Gamma", "sales_readers", tenant_role_name(tenant_a)
    )
    chart = harness.create_chart(ana, "grant-later")
    harness.drain()
    assert harness.decide(tia, chart).verdict == DENY
    assert chart.id not in harness.listed_ids(tia, "chart")
    harness.authorizer.make_tenant_administrator(tia.ref, tenant_a)
    assert harness.decide(tia, chart).verdict == ALLOW, "seen on the next request"
    assert chart.id in harness.listed_ids(tia, "chart")


def test_ownership_list_route_scopes_the_administrator_from_the_rows(harness):
    """`_visibility_scope` used to make an uncached administrator check and
    a `tenant_objects` store read per request, and scoped by the store's
    tenant tuples while the read gate used the row's mirrored tenant. Now
    both answer from the row: one cached administrator check, no
    `tenant_objects` read, and the listed set is the readable set."""
    tenant_a, ana, tam, cleo, chart, dash = _tenant_admin_world(harness, 0x1E0)
    harness.authorizer.calls.clear()
    r = harness.get(tam, "/api/v1/ownership/charts?limit=100")
    assert r.status_code == 200
    ids = {item["object_id"] for item in r.get_json()["result"]}
    assert chart.id in ids
    names = [c[0] for c in harness.authorizer.calls]
    assert "tenant_objects" not in names
    assert names.count("user_in_group") <= 1
    row = next(i for i in r.get_json()["result"] if i["object_id"] == chart.id)
    assert (row["can_manage"], row["can_share"]) == (True, False)
    r = harness.get(cleo, "/api/v1/ownership/charts?limit=100")
    assert chart.id not in {item["object_id"] for item in r.get_json()["result"]}


# --- QA blockers: subject validation and the shapes the store will refuse ---------


def test_a_group_subject_without_member_is_refused_not_queued(harness):
    """Issue #118, the blocker two QA testers hit by accident. The store only
    accepts a group as a subject in its userset form, so `group:<id>` with
    no `#member` could never be delivered -- it used to answer 200, sit in
    the mirror, die in the outbox, and then block every later intent for
    that object. It is a malformed subject and it is refused before
    anything is written."""
    tenant = guid(0x1F)
    role = f"Tenant_{tenant}_Role"
    una = harness.add_person(guid(0x200), "Una", "Gamma", "sales_readers", role)
    chart = harness.create_chart(una, "no-member-suffix")
    harness.set_visibility(una, chart, "shared")
    harness.drain()

    for bad in (
        f"group:{tenant}.dashboard_designer",
        f"group:{tenant}.dashboard_designer#",
        f"group:{tenant}.dashboard_designer#admin",
        "group:#member",
    ):
        r = harness.post(
            una,
            f"/api/v1/ownership/chart/{chart.id}/shares",
            {"subject": bad, "role": "viewer"},
        )
        assert r.status_code == 400, (bad, r.status_code, r.get_json())
        assert "member set" in r.get_json()["message"], bad

    with harness.ctx():
        from superset_ownership import outbox, service

        assert service.list_share_rows("chart", chart.id) == []
        assert outbox.status()["dead"] == 0
    # And the well-formed spelling still works.
    harness.authorizer.groups[f"{tenant}.dashboard_designer"] = set()
    r = harness.post(
        una,
        f"/api/v1/ownership/chart/{chart.id}/shares",
        {"subject": f"group:{tenant}.dashboard_designer#member", "role": "viewer"},
    )
    assert r.status_code == 200, r.get_json()


def test_a_group_name_with_a_space_or_colon_is_a_400_not_a_500(harness):
    """Issue #124: the picker's display text is the spaced name, so a paste
    lands here. `identity.group_id` raised out of the validators and the
    route answered 500."""
    tenant = guid(0x21)
    role = f"Tenant_{tenant}_Role"
    uri = harness.add_person(guid(0x210), "Uri", "Gamma", "sales_readers", role)
    chart = harness.create_chart(uri, "spaced-group-name")
    harness.set_visibility(uri, chart, "shared")

    for bad in (
        f"group:{tenant}.dashboard designer#member",
        f"group:{tenant}.dashboard designer#member",
        f"group:{tenant}.dash:designer#member",
        f"group:{tenant}.{'x' * 250}#member",
    ):
        r = harness.post(
            uri,
            f"/api/v1/ownership/chart/{chart.id}/shares",
            {"subject": bad, "role": "viewer"},
        )
        assert r.status_code == 400, (bad, r.status_code, r.get_json())
        from urllib.parse import quote

        r = harness.delete(
            uri, f"/api/v1/ownership/chart/{chart.id}/shares/{quote(bad, safe='')}"
        )
        assert r.status_code == 400, (bad, r.status_code, r.get_json())


def test_wrong_typed_bodies_are_400_not_500(harness):
    """Issue #128: `{"subject": 123}` and `{"visibility": ["private"]}` raised
    a TypeError inside the validators while every sibling wrong type
    answered a clean 400."""
    tenant = guid(0x22)
    role = f"Tenant_{tenant}_Role"
    uma = harness.add_person(guid(0x220), "Uma", "Gamma", "sales_readers", role)
    chart = harness.create_chart(uma, "wrong-types")
    harness.set_visibility(uma, chart, "shared")

    for body in ({"subject": 123}, {"subject": ["user:x"]}, {"subject": {"a": 1}}):
        r = harness.post(uma, f"/api/v1/ownership/chart/{chart.id}/shares", body)
        assert r.status_code == 400, (body, r.status_code, r.get_json())
    for body in ({"visibility": ["private"]}, {"visibility": 1}, {"visibility": {}}):
        r = harness.put(uma, f"/api/v1/ownership/chart/{chart.id}/visibility", body)
        assert r.status_code == 400, (body, r.status_code, r.get_json())
    # The owner route takes the same subject validator.
    r = harness.put(uma, f"/api/v1/ownership/chart/{chart.id}/owner", {"subject": 7})
    assert r.status_code == 400, r.get_json()

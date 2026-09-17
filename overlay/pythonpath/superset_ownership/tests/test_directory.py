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
"""`superset_ownership.directory`: the `Directory` protocol split out of
`Authorizer` (`qa/design/directory-hook/02-technical-spec.md` section 7).

`OpenFGADirectory` is driven through a fake `requests.post` that answers
the same shapes the real OpenFGA HTTP API does (`test_fga_client.py`'s
pattern); `LocalDirectory` is driven through a fake FAB security manager
(`test_group_id.py`'s pattern), reused here rather than re-invented.

Pure: no real Superset, no real OpenFGA. `superset` is stubbed in
`sys.modules` for the few functions these classes call through it
(`security_manager`, `db`).
"""

from __future__ import annotations

# The standard library, not superset.utils.json: this module is pure (see
# the module docstring above) and must stay importable where Superset is
# absent.
import json as _json  # noqa: TID251
import sys
import types
from dataclasses import dataclass, field
from unittest import mock

import pytest
import requests
from superset_ownership import directory, fga, plugins
from superset_ownership.directory import (
    Directory,
    DirectoryHealth,
    DirectoryUnavailable,
    LocalDirectory,
    OpenFGADirectory,
)

TENANT_A = "a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40"
TENANT_B = "b7f28e5a-9c14-4d6b-a2f0-5e3d8c1a6b92"
ADA = "3f0a91c7-2d84-4e63-9b15-7c4e8a2f6d31"
BEN = "6c2b48e9-5a71-4f92-8d03-2e9b7c1a4d53"


def _resp(status=200, body=None, text=None):
    r = requests.Response()
    r.status_code = status
    r._content = text.encode() if text is not None else _json.dumps(body or {}).encode()
    r.headers["Content-Type"] = "application/json"
    return r


# ---------------------------------------------------------------------------
# A fake OpenFGA store, driven through fga.requests.post exactly as
# test_fga_client.py drives fga.py itself. Enough of the API to exercise
# OpenFGADirectory's read shapes: /read (object-scoped and the fast path's
# user-scoped, bare-type-object variant), /check.
# ---------------------------------------------------------------------------


class FakeStore:
    def __init__(self):
        # tenant -> [group name, ...], present only when the tuple is
        # "backfilled" (the additive group.tenant relation).
        self.tenant_groups: dict[str, list[str]] = {}
        # group name -> [user ref, ...] ("user:<guid>" or "group:<name>#member")
        self.group_members: dict[str, list[str]] = {}
        # tenant -> [user guid, ...]
        self.tenant_members: dict[str, list[str]] = {}
        # tenant -> [user ref or group#member ref, ...]: the `admin`
        # relation on the tenant object (Neurons' shape).
        self.tenant_admins: dict[str, list[str]] = {}
        # (object, relation-or-"") -> [raw tuple dict, ...], for the
        # generic object-scoped reads M5b's test seeds directly
        # (list_grants/list_relations/object_tenant/purge_object/
        # revoke_subject -- fga.read_all's various shapes).
        self.object_tuples: dict[tuple[str, str], list[dict]] = {}
        self.model_has_relation = True
        # M2 (review round 1, PR #101): when set, drives BOTH GET shapes
        # (`/authorization-models` -- list, newest first -- and
        # `/authorization-models/<id>` -- one pinned model) from this list
        # instead of the single synthetic model `model_has_relation` builds,
        # so a test can hold two DIFFERENT models on the store at once (a
        # `health()` pinned to the older one must read that one, not
        # `list_models()[0]`). `None` (the default) preserves the original
        # single-model behaviour driven by `model_has_relation`.
        self.models: list[dict] | None = None
        # When set, every `/list-objects` call answers with this HTTP status
        # instead of a real answer -- PCS-10243 #91 residue 2's own probe:
        # `_walk_groups`'s per-member `list_objects` call is strict now, so
        # a mid-walk store failure there must raise rather than under-report.
        self.list_objects_status: int | None = None
        self.calls: list[tuple[str, dict]] = []
        # (url, headers) for every POST, in order -- H-1's probe: assert
        # every path this class exercises carries the connection's
        # Authorization header, not just the ones fga.py's own tests cover.
        self.calls_with_headers: list[tuple[str, dict]] = []

    def post(self, url, json=None, timeout=None, **kw):
        path = url.rsplit("/stores/", 1)[-1].split("/", 1)[-1]
        path = "/" + path.split("/", 1)[-1] if "/" in path else "/" + path
        self.calls.append((url, json or {}))
        self.calls_with_headers.append((url, dict(kw.get("headers") or {})))
        tk = (json or {}).get("tuple_key", {})
        if url.endswith("/check"):
            return self._check(tk)
        if url.endswith("/read"):
            return self._read(json or {})
        if url.endswith("/list-objects"):
            return self._list_objects(json or {})
        if url.endswith("/write"):
            # M-5's wire-level probe only needs the request BODY this class
            # already records in `self.calls`; the write itself always
            # "succeeds" here.
            return _resp(200, {})
        raise AssertionError(f"unexpected URL {url}")

    def _list_objects(self, body):
        # The walk's `list_objects(f"user:{guid}", "member", "group")`:
        # every group this user is a member of.
        if self.list_objects_status is not None:
            return _resp(
                self.list_objects_status,
                {"code": "internal_error", "message": "boom"},
            )
        user = body.get("user", "")
        guid = user.split(":", 1)[-1] if user.startswith("user:") else None
        objs = [
            f"group:{name}"
            for name, members in self.group_members.items()
            if guid and f"user:{guid}" in members
        ]
        return _resp(200, {"objects": objs})

    def _check(self, tk):
        user, relation, obj = tk.get("user"), tk.get("relation"), tk.get("object")
        allowed = False
        if relation == "member" and obj and obj.startswith("tenant:"):
            tenant = obj.split(":", 1)[1].split("#", 1)[0]
            allowed = user in {f"user:{g}" for g in self.tenant_members.get(tenant, [])}
        elif relation == "admin" and obj and obj.startswith("tenant:"):
            tenant = obj.split(":", 1)[1].split("#", 1)[0]
            admins = self.tenant_admins.get(tenant, [])
            allowed = user in admins or any(
                a.startswith("group:")
                and user
                in self.group_members.get(a.split(":", 1)[1].split("#", 1)[0], [])
                for a in admins
            )
        elif relation == "member" and obj and obj.startswith("group:"):
            name = obj.split(":", 1)[1].split("#", 1)[0]
            allowed = user in self.group_members.get(name, [])
        return _resp(200, {"allowed": allowed})

    def _read(self, body):
        # I-1: real OpenFGA rejects any `page_size` outside `[1, 100]` with
        # `400 page_size_invalid` -- enforced here so a directory method
        # that (re-)introduces an unclamped page request fails a test
        # instead of silently degrading only in production, the way the
        # reviewer's own `/subjects` repro found `search_users` doing.
        page_size = body.get("page_size", 100)
        if not (1 <= page_size <= 100):
            return _resp(
                400,
                {
                    "code": "page_size_invalid",
                    "message": (
                        "invalid ReadRequest.PageSize: value must be inside "
                        "range [1, 100]"
                    ),
                },
            )
        tk = body.get("tuple_key", {})
        user, relation, obj = tk.get("user"), tk.get("relation"), tk.get("object")
        if (
            relation == "tenant"
            and obj == "group:"
            and user
            and user.startswith("tenant:")
        ):
            # The fast path's user-scoped, bare-type Read.
            if not self.model_has_relation:
                return _resp(
                    400,
                    {
                        "code": "validation_error",
                        "message": "relation 'group#tenant' not found",
                    },
                )
            tenant = user.split(":", 1)[1].split("#", 1)[0]
            names = self.tenant_groups.get(tenant, [])
            tuples = [{"key": {"object": f"group:{n}"}} for n in names]
            return _resp(200, {"tuples": tuples, "continuation_token": ""})
        if relation == "tenant" and obj and obj.startswith("group:"):
            # group_exists' fast probe: does this group carry a tenant tuple?
            name = obj.split(":", 1)[1]
            has = any(name in names for names in self.tenant_groups.values())
            if not self.model_has_relation:
                return _resp(
                    400,
                    {
                        "code": "validation_error",
                        "message": "relation 'group#tenant' not found",
                    },
                )
            tuples = [{"key": {"object": obj}}] if has else []
            return _resp(200, {"tuples": tuples, "continuation_token": ""})
        if relation == "member" and obj and obj.startswith("group:"):
            name = obj.split(":", 1)[1]
            tuples = [
                {"key": {"user": u, "relation": "member", "object": obj}}
                for u in self.group_members.get(name, [])
            ]
            return _resp(200, {"tuples": tuples, "continuation_token": ""})
        if relation == "admin" and obj and obj.startswith("tenant:"):
            tenant = obj.split(":", 1)[1]
            tuples = [
                {"key": {"user": u, "relation": "admin", "object": obj}}
                for u in self.tenant_admins.get(tenant, [])
            ]
            return _resp(200, {"tuples": tuples, "continuation_token": ""})
        if relation == "member" and obj and obj.startswith("tenant:"):
            # Chunked ONE tuple per page (regardless of the requested
            # page_size) so a test can drive real multi-page pagination
            # (M-3: `search_users`'s `limit`/`cursor` are honoured against
            # the store's OWN continuation token) without needing 100+
            # fixture members; every existing caller loops until the token
            # is exhausted, so the final, aggregated answer is unchanged.
            tenant = obj.split(":", 1)[1]
            members = self.tenant_members.get(tenant, [])
            offset = int(body.get("continuation_token") or 0)
            chunk = members[offset : offset + 1]
            next_offset = offset + len(chunk)
            next_token = str(next_offset) if next_offset < len(members) else ""
            tuples = [
                {"key": {"user": f"user:{g}", "relation": "member", "object": obj}}
                for g in chunk
            ]
            return _resp(200, {"tuples": tuples, "continuation_token": next_token})
        seeded = self.object_tuples.get((obj or "", relation or ""))
        if seeded is not None:
            return _resp(
                200, {"tuples": [{"key": t} for t in seeded], "continuation_token": ""}
            )
        return _resp(200, {"tuples": [], "continuation_token": ""})

    def get(self, url, timeout=None, **kw):
        # `fga.get_model()` (no `model_id`) -> `fga.list_models()` -> one
        # GET of `/authorization-models`; `fga.get_model(<id>)` (a PINNED
        # id, M2) -> one GET of `/authorization-models/<id>`. When
        # `self.models` is unset, both shapes are served from a single
        # synthetic model whose `group` type carries `tenant` iff
        # `model_has_relation` -- the same flag the POST-side `/read`
        # handlers above already use for the fast-path's own 400, so a test
        # can flip ONE attribute and get a consistent store on both sides
        # (`health()`'s probe, residue 3). When `self.models` IS set (a
        # list of model dicts, newest first), it drives both shapes
        # instead, so a test can hold two DIFFERENT models on the store at
        # once.
        self.calls.append((url, {}))
        self.calls_with_headers.append((url, dict(kw.get("headers") or {})))
        models = self.models
        if models is None:
            relations = {"member": {}}
            if self.model_has_relation:
                relations["tenant"] = {}
            models = [
                {
                    "id": "m1",
                    "schema_version": "1.1",
                    "type_definitions": [
                        {"type": "group", "relations": relations},
                    ],
                }
            ]
        if url.endswith("/authorization-models"):
            return _resp(200, {"authorization_models": models})
        marker = "/authorization-models/"
        if marker in url:
            model_id = url.rsplit(marker, 1)[-1]
            for m in models:
                if m.get("id") == model_id:
                    return _resp(200, {"authorization_model": m})
            return _resp(404, {"code": "not_found", "message": "no such model"})
        raise AssertionError(f"unexpected URL {url}")


@pytest.fixture(autouse=True)
def _pre_configuration_group_ids(monkeypatch):
    """This file's fixtures spell group ids in the pre-configuration shape
    (`<name>_<tenant>`); the format-independent logic under test is the
    same under Neurons' default (`<tenant>.<name>`), which
    `test_group_id.py` pins on its own."""
    from superset_ownership.identity import (
        GROUP_ID_FORMAT_SETTING,
        GROUP_ID_FORMAT_SUFFIX,
    )

    monkeypatch.setenv(GROUP_ID_FORMAT_SETTING, GROUP_ID_FORMAT_SUFFIX)
    # And the walk mode the tests assume (`auto`): a dev container pointed
    # at a Neurons store runs with OWNERSHIP_DIRECTORY_GROUP_WALK=always.
    monkeypatch.delenv("OWNERSHIP_DIRECTORY_GROUP_WALK", raising=False)


@pytest.fixture
def store(monkeypatch):
    s = FakeStore()
    monkeypatch.setattr(fga.requests, "post", s.post)
    monkeypatch.setattr(fga.requests, "get", s.get)
    return s


@pytest.fixture
def superset_stub(monkeypatch):
    stub = types.ModuleType("superset")
    stub.security_manager = mock.Mock()
    stub.db = mock.Mock()
    monkeypatch.setitem(sys.modules, "superset", stub)
    return stub


def _labels_query(accounts):
    """`db.session.query(...).all()` returning fake ab_user rows, so
    `_user_labels`'s one query per page finds names."""
    return list(accounts)


@dataclass
class FakeAbUser:
    id: int
    username: str
    first_name: str = ""
    last_name: str = ""
    email: str | None = None


# --------------------------------------------------------------------- protocol


def test_directory_protocol_has_no_write_method():
    """Read-only by construction (contract section 5): a plug-in cannot
    become a second writer to the authorization store."""
    methods = {n for n in dir(Directory) if not n.startswith("_")}
    assert methods == {
        "search_users",
        "user_in_tenant",
        "list_groups",
        "group_members",
        "user_in_group",
        "group_exists",
        "tenant_administrators",
        "health",
    }
    assert "protocol_version" in Directory.__annotations__
    for write_ish in ("write", "delete", "set", "create", "remove", "revoke", "purge"):
        assert not any(write_ish in m for m in methods), methods


def test_both_shipped_directories_satisfy_the_protocol():
    assert isinstance(OpenFGADirectory(), Directory)
    assert isinstance(LocalDirectory(), Directory)


# ------------------------------------------------------------------- list_groups


def test_fast_list_groups_never_surfaces_a_foreign_or_untenanted_group(store):
    """M-2 pinned at the DIRECTORY level (N-4): the reviewer's own probe --
    a store answering tenant A's Read with a group actually named for
    tenant B and one carrying no tenant at all -- must not reach
    `list_groups(A)`'s own answer, not only the `/subjects` route's
    separate re-check (api.py). `_fast_groups`'s `group_belongs_to_tenant`
    filter is what `verify` and any other direct consumer of the
    `Directory` protocol relies on; this fails if that filter is removed."""
    store.tenant_groups[TENANT_A] = [
        "eng_" + TENANT_A,  # legitimately tenant A's
        "eng_" + TENANT_B,  # a store bug: named for the OTHER tenant
        "untenanted",  # a store bug: does not parse to any tenant
    ]
    d = OpenFGADirectory()

    page = d.list_groups(TENANT_A)

    assert [g["id"] for g in page["items"]] == [f"group:eng_{TENANT_A}"]


def test_fast_list_groups_returns_members_none(store):
    store.tenant_groups[TENANT_A] = ["dashboard_designer_" + TENANT_A]
    d = OpenFGADirectory()

    page = d.list_groups(TENANT_A)

    assert [g["id"] for g in page["items"]] == [f"group:dashboard_designer_{TENANT_A}"]
    assert page["items"][0]["members"] is None
    assert page["items"][0]["tenant"] == TENANT_A


def test_a_400_on_the_fast_path_sets_the_store_wide_flag_and_falls_back(store, caplog):
    import logging

    store.model_has_relation = False
    store.tenant_members[TENANT_A] = [ADA]
    store.group_members["eng_" + TENANT_A] = [f"user:{ADA}"]
    caplog.set_level(logging.WARNING, logger="superset_ownership.directory")
    d = OpenFGADirectory()

    page = d.list_groups(TENANT_A)

    assert d._model_lacks_relation is True
    assert [g["id"] for g in page["items"]] == [f"group:eng_{TENANT_A}"]
    assert any(
        "falling back to the member walk" in r.getMessage() for r in caplog.records
    )
    # A second tenant does not repeat the model-wide WARNING.
    caplog.clear()
    d.list_groups(TENANT_B)
    assert not any(
        "falling back to the member walk" in r.getMessage() for r in caplog.records
    )


def test_per_tenant_verdict_does_not_leak_across_tenants(store):
    """B3: tenant A's tuples are backfilled, tenant B's are not (an empty
    first page). A single process-wide flag would give B tenant A's
    answer; the per-tenant dict must not."""
    store.tenant_groups[TENANT_A] = ["eng_" + TENANT_A]
    store.tenant_members[TENANT_B] = [BEN]
    store.group_members["ops_" + TENANT_B] = [f"user:{BEN}"]
    d = OpenFGADirectory()

    a = d.list_groups(TENANT_A)
    b = d.list_groups(TENANT_B)

    assert [g["id"] for g in a["items"]] == [f"group:eng_{TENANT_A}"]
    assert [g["id"] for g in b["items"]] == [f"group:ops_{TENANT_B}"]
    assert d._verdict[TENANT_A][0] is True  # fast path proven
    assert d._verdict[TENANT_B][0] is False  # walked; the tuple is not there yet


def test_a_tenant_flips_to_fast_once_its_tuples_appear(store):
    d = OpenFGADirectory()
    store.tenant_members[TENANT_A] = [ADA]
    store.group_members["eng_" + TENANT_A] = [f"user:{ADA}"]

    walked = d.list_groups(TENANT_A)
    assert walked["items"][0]["members"] == 1  # the walk counts members

    # The tuple lands; re-probing happens once the TTL has passed.
    store.tenant_groups[TENANT_A] = ["eng_" + TENANT_A]
    d._verdict[TENANT_A] = (False, 0.0)  # force expiry rather than sleep

    fast = d.list_groups(TENANT_A)
    assert fast["items"][0]["members"] is None  # the fast path now answers


def test_a_zero_group_tenant_walks_once_per_ttl(store):
    d = OpenFGADirectory()
    page = d.list_groups(TENANT_A)
    assert page["items"] == []
    assert (
        d._verdict[TENANT_A][0] is True
    )  # empty is a real answer, cached as "fast is fine"


def test_the_walk_under_the_default_neurons_format(store, monkeypatch):
    """PR #113 review: the autouse fixture pins the pre-configuration
    format for this file, so nothing here walked a `{tenant}.{name}` store.
    Under the default -- a Neurons store, which has no group->tenant tuple
    -- the fast path is empty, the walk finds the member's groups by the
    dotted id, keeps only the tenant's own, and drops the foreign one."""
    from superset_ownership.identity import GROUP_ID_FORMAT_SETTING

    monkeypatch.delenv(GROUP_ID_FORMAT_SETTING, raising=False)
    subject = f"{TENANT_A}.{ADA}"
    store.tenant_members[TENANT_A] = [subject]
    store.group_members[f"{TENANT_A}.eng"] = [f"user:{subject}"]
    store.group_members[f"{TENANT_A}.eng.eu"] = [f"user:{subject}"]
    store.group_members[f"{TENANT_B}.eng"] = [f"user:{subject}"]  # a store bug
    store.group_members["untenanted"] = [f"user:{subject}"]
    d = OpenFGADirectory()

    page = d.list_groups(TENANT_A)

    assert [g["id"] for g in page["items"]] == [
        f"group:{TENANT_A}.eng",
        f"group:{TENANT_A}.eng.eu",
    ]
    assert all(g["tenant"] == TENANT_A for g in page["items"])
    assert d._verdict[TENANT_A][0] is False, "walked; the fast path stays empty"
    assert d.group_exists(f"group:{TENANT_A}.eng")
    assert d.user_in_group(subject, f"group:{TENANT_A}.eng")


def test_always_mode_never_probes_the_fast_path(store, monkeypatch):
    monkeypatch.setenv("OWNERSHIP_DIRECTORY_GROUP_WALK", "always")
    store.tenant_members[TENANT_A] = [ADA]
    store.group_members["eng_" + TENANT_A] = [f"user:{ADA}"]
    store.tenant_groups[TENANT_A] = ["eng_" + TENANT_A]  # even though it's there
    d = OpenFGADirectory()

    page = d.list_groups(TENANT_A)
    assert page["items"][0]["members"] == 1  # walked, not fast


def test_walk_groups_raises_on_a_store_failure_reading_a_members_groups(
    store, monkeypatch
):
    """PCS-10243 #91, residue 2: `_walk_groups`'s per-member `list_objects`
    call is `strict=True` now, so a store failure MID-WALK raises like every
    other read in this class instead of silently under-reporting the walk
    (M-3's contract). `tenant_members` (the walk's first read) succeeds --
    only the per-member `list_objects` call fails, so this pins down which
    read grew the strict behaviour."""
    monkeypatch.setenv("OWNERSHIP_DIRECTORY_GROUP_WALK", "always")
    store.tenant_members[TENANT_A] = [ADA]
    store.list_objects_status = 503
    d = OpenFGADirectory()

    with pytest.raises(DirectoryUnavailable):
        d.list_groups(TENANT_A)


def test_never_mode_never_walks(store, monkeypatch):
    monkeypatch.setenv("OWNERSHIP_DIRECTORY_GROUP_WALK", "never")
    store.tenant_members[TENANT_A] = [ADA]
    store.group_members["eng_" + TENANT_A] = [f"user:{ADA}"]  # would be found by a walk
    d = OpenFGADirectory()

    page = d.list_groups(TENANT_A)
    assert page["items"] == []  # empty means empty; no fallback


def test_verdict_table_is_capped_and_evicts_oldest_first():
    d = OpenFGADirectory()
    for i in range(1000):
        d._remember(f"tenant-{i}", True)
    assert len(d._verdict) == 1000
    d._remember("tenant-1000", True)
    assert len(d._verdict) == 1000
    assert "tenant-0" not in d._verdict


# ------------------------------------------------------------------- group_exists


def test_group_exists_fast_path(store):
    store.tenant_groups[TENANT_A] = ["eng_" + TENANT_A]
    d = OpenFGADirectory()
    assert d.group_exists(f"group:eng_{TENANT_A}") is True
    assert d.group_exists(f"group:nope_{TENANT_A}") is False


def test_group_exists_falls_back_when_the_model_lacks_the_relation(store):
    store.model_has_relation = False
    store.group_members["eng_" + TENANT_A] = [f"user:{ADA}"]
    d = OpenFGADirectory()
    assert d.group_exists(f"group:eng_{TENANT_A}") is True
    assert d.group_exists(f"group:nope_{TENANT_A}") is False


# ------------------------------------------------------------------- search_users


def test_search_users_pages_filters_labels_and_keeps_directory_only_members(
    store, superset_stub
):
    store.tenant_members[TENANT_A] = ["local-1", BEN, "ghost-guid"]
    superset_stub.security_manager.user_model = object()
    superset_stub.db.session.query.return_value.all.return_value = [
        FakeAbUser(1, "local-1", "Jane", "Doe", "jane@example.com"),
        FakeAbUser(2, BEN, "Ben", "Smith", "ben@example.com"),
    ]
    d = OpenFGADirectory()

    page = d.search_users(TENANT_A, "")
    by_guid = {u["guid"]: u for u in page["items"]}
    assert set(by_guid) == {"local-1", BEN, "ghost-guid"}
    assert by_guid["local-1"] == {
        "guid": "local-1",
        "display_name": "Jane Doe",
        "email": "jane@example.com",
        "superset_id": 1,
    }
    # Unknown to Superset: labelled by the GUID, no email, no id.
    assert by_guid["ghost-guid"] == {
        "guid": "ghost-guid",
        "display_name": "ghost-guid",
        "email": None,
        "superset_id": None,
    }

    filtered = d.search_users(TENANT_A, "smith")
    assert [u["guid"] for u in filtered["items"]] == [BEN]


def test_user_in_tenant_raises_on_a_store_failure(monkeypatch):
    monkeypatch.setattr(
        fga.requests,
        "post",
        mock.Mock(side_effect=requests.exceptions.ConnectionError("down")),
    )
    d = OpenFGADirectory()
    with pytest.raises(DirectoryUnavailable):
        d.user_in_tenant(ADA, TENANT_A)


def test_user_in_tenant_true_and_false(store):
    store.tenant_members[TENANT_A] = [ADA]
    d = OpenFGADirectory()
    assert d.user_in_tenant(ADA, TENANT_A) is True
    assert d.user_in_tenant(BEN, TENANT_A) is False


# ------------------------------------------------------------- tenant_administrators


def test_tenant_administrators_parity_with_the_old_backfill_algorithm(
    store, superset_stub
):
    """The tenant's administrators are the `admin` tuples on the tenant
    object (Neurons' shape), users directly or through a group named as
    admin; active accounts only."""
    store.tenant_members[TENANT_A] = [ADA, BEN]
    store.tenant_admins[TENANT_A] = [f"user:{ADA}"]

    ada_user = FakeAbUser(11, ADA, "Ada", "Lovelace")
    ada_user.is_active = True
    ben_user = FakeAbUser(12, BEN, "Ben", "Smith")
    ben_user.is_active = False  # deactivated: not reported
    superset_stub.security_manager.find_user.side_effect = (
        lambda username=None, email=None: {ADA: ada_user, BEN: ben_user}.get(username)
    )
    superset_stub.security_manager.user_model = object()
    superset_stub.db.session.query.return_value.all.return_value = [ada_user, ben_user]

    admins = OpenFGADirectory().tenant_administrators(TENANT_A)
    assert [a["guid"] for a in admins] == [ADA]
    assert admins[0]["superset_id"] == 11


# ------------------------------------------------------------------------ health


def test_health_reports_unreachable(monkeypatch):
    monkeypatch.setattr(fga, "reachable", lambda: False)
    health = OpenFGADirectory().health()
    assert health == DirectoryHealth(ok=False, detail="store unreachable")


def test_health_reports_ok_without_a_model_reader(monkeypatch):
    monkeypatch.setattr(fga, "reachable", lambda: True)
    monkeypatch.delattr(fga, "get_model", raising=False)
    health = OpenFGADirectory().health()
    assert health.ok is True


def test_health_probes_the_model_for_group_tenant_present(store, monkeypatch):
    """PCS-10243 #91, residue 3: `health()` must not trust the lazily-set
    `_model_lacks_relation` flag -- a freshly built directory that has never
    run `list_groups`/`group_exists` for any tenant starts with that flag at
    `None`, and the pre-fix `not self._model_lacks_relation` read that as
    "present" regardless of what the model actually says. Probing the model
    directly (`store.model_has_relation = True` here) must report "present"
    on a directory that has done nothing else yet."""
    monkeypatch.setattr(fga, "reachable", lambda: True)
    store.model_has_relation = True
    d = OpenFGADirectory()
    assert d._model_lacks_relation is None  # nothing has probed it yet

    health = d.health()

    assert health.ok is True
    assert "group.tenant present" in health.detail
    assert d._model_lacks_relation is False  # health() updates the flag too


def test_health_probes_the_model_for_group_tenant_absent(store, monkeypatch):
    """The other side of the same probe: a model that genuinely lacks
    `group.tenant` (the events-store repro from the issue) must report
    "absent" on a directory that has never run `list_groups`/`group_exists`
    either -- not "present" by the same stale default."""
    monkeypatch.setattr(fga, "reachable", lambda: True)
    store.model_has_relation = False
    d = OpenFGADirectory()
    assert d._model_lacks_relation is None  # nothing has probed it yet

    health = d.health()

    assert health.ok is True
    assert "group.tenant absent" in health.detail
    assert d._model_lacks_relation is True


def test_health_reads_the_pinned_model_not_the_latest(store, monkeypatch):
    """M2 (review round 1, PR #101, `review-sweep-pr101.md`): `health()`
    must read the connection's PINNED model when `OWNERSHIP_FGA_MODEL` is
    set -- the exact model `plugin_verify.RequiredRelations` reads via
    `fga.get_model(connection.model_id if connection else None)` -- not
    always the store's latest (`list_models()[0]`), or the two checks can
    disagree the moment a deployment's pinned id is not the newest version
    (the runbook's own a4/a5 sequence: `fga install-model` writes a new
    model while the deployment still pins the previous id until the next
    boot).

    Two DIFFERENT models on the store: the latest lacks `group.tenant`, an
    older, pinned one has it. Before the fix, `health()` called
    `fga.get_model()` with no id and always read the latest -- this would
    report "absent" here, disagreeing with `RequiredRelations`, which reads
    the pin and would see "present"."""
    store.models = [
        {  # store's latest -- NOT what the connection pins
            "id": "m2-latest",
            "schema_version": "1.1",
            "type_definitions": [{"type": "group", "relations": {"member": {}}}],
        },
        {  # older, pinned by the connection below
            "id": "m1-pinned",
            "schema_version": "1.1",
            "type_definitions": [
                {"type": "group", "relations": {"member": {}, "tenant": {}}}
            ],
        },
    ]
    conn = fga.FgaConnection(
        api_url="http://openfga:8080", store_id="store1", model_id="m1-pinned"
    )
    monkeypatch.setattr(fga, "reachable", lambda: True)
    monkeypatch.setattr(plugins, "get_fga_connection", lambda: conn)

    health = OpenFGADirectory().health()

    assert health.ok is True
    assert "m1-pinned" in health.detail, health.detail
    assert "group.tenant present" in health.detail, health.detail


# ------------------------------------------------ H-1: credentials on every path


def test_strict_check_and_fast_group_read_send_the_connection_headers(
    store, monkeypatch
):
    """H-1: `directory._strict_check` (`user_in_tenant`) and
    `directory._fast_group_read` (`list_groups`'s fast path) go through
    `fga._request` -- which injects `conn.headers` and would refresh on a
    401 -- rather than a raw `requests.post` that sent neither. On a
    credentialed store the earlier version answered 401 on every one of
    these two calls; this pins that both now carry the same
    `Authorization` header every OTHER fga.py-routed call does."""
    from superset_ownership.fga_connection import FgaConnection

    conn = FgaConnection(
        api_url="http://openfga:8080",
        store_id="store1",
        model_id=None,
        headers={"Authorization": "Bearer s3cret"},
    )
    monkeypatch.setattr(fga, "_conn", lambda: conn)
    store.tenant_members[TENANT_A] = [ADA]
    store.tenant_groups[TENANT_A] = ["eng_" + TENANT_A]

    d = OpenFGADirectory()
    assert d.user_in_tenant(ADA, TENANT_A) is True
    page = d.list_groups(TENANT_A)
    assert page["items"], "the fast path made no call to assert headers on"

    assert store.calls_with_headers, "no HTTP calls were recorded"
    for url, headers in store.calls_with_headers:
        assert headers.get("Authorization") == "Bearer s3cret", (url, headers)


# ------------------------------------------------------- M-3: raise, don't swallow


def test_search_users_raises_on_a_store_failure(monkeypatch):
    monkeypatch.setattr(
        fga.requests,
        "post",
        mock.Mock(side_effect=requests.exceptions.ConnectionError("down")),
    )
    with pytest.raises(DirectoryUnavailable):
        OpenFGADirectory().search_users(TENANT_A, "")


def test_group_members_raises_on_a_store_failure(monkeypatch):
    monkeypatch.setattr(
        fga.requests,
        "post",
        mock.Mock(side_effect=requests.exceptions.ConnectionError("down")),
    )
    with pytest.raises(DirectoryUnavailable):
        OpenFGADirectory().group_members(f"group:eng_{TENANT_A}")


def test_user_in_group_raises_on_a_store_failure(monkeypatch):
    monkeypatch.setattr(
        fga.requests,
        "post",
        mock.Mock(side_effect=requests.exceptions.ConnectionError("down")),
    )
    with pytest.raises(DirectoryUnavailable):
        OpenFGADirectory().user_in_group(ADA, f"group:eng_{TENANT_A}")


def test_group_exists_raises_on_a_store_failure_past_the_fast_path(store, monkeypatch):
    # The fast path's own read is already strict; force it into the
    # member-based fallback (model lacks the relation) and fail THAT read.
    store.model_has_relation = False
    d = OpenFGADirectory()
    monkeypatch.setattr(
        fga.requests,
        "post",
        mock.Mock(side_effect=requests.exceptions.ConnectionError("down")),
    )
    with pytest.raises(DirectoryUnavailable):
        d.group_exists(f"group:eng_{TENANT_A}")


def test_tenant_administrators_raises_on_a_store_failure(monkeypatch):
    monkeypatch.setattr(
        fga.requests,
        "post",
        mock.Mock(side_effect=requests.exceptions.ConnectionError("down")),
    )
    with pytest.raises(DirectoryUnavailable):
        OpenFGADirectory().tenant_administrators(TENANT_A)


def test_search_users_pages_on_the_store_token(store, superset_stub):
    """M-3: `search_users` is paginated for real -- `limit`/`cursor` are
    honoured against the STORE's own continuation token, not just used to
    slice an already-fully-fetched list."""
    store.tenant_members[TENANT_A] = [ADA, BEN]
    superset_stub.security_manager.user_model = object()
    superset_stub.db.session.query.return_value.all.return_value = []
    d = OpenFGADirectory()

    first = d.search_users(TENANT_A, "", limit=1)
    assert len(first["items"]) == 1
    assert first["next_cursor"] is not None

    seen = {u["guid"] for u in first["items"]}
    second = d.search_users(TENANT_A, "", limit=1, cursor=first["next_cursor"])
    seen |= {u["guid"] for u in second["items"]}
    assert seen == {ADA, BEN}


def test_search_users_sizes_the_read_to_the_remaining_budget(store, superset_stub):
    """N-3: each store read is requested at `page_size=limit - len(items)`
    -- the fix that makes a single page unable to contribute more matches
    than there is room for."""
    store.tenant_members[TENANT_A] = [ADA, BEN]
    superset_stub.security_manager.user_model = object()
    superset_stub.db.session.query.return_value.all.return_value = []

    OpenFGADirectory().search_users(TENANT_A, "", limit=1)

    read_bodies = [body for (url, body) in store.calls if url.endswith("/read")]
    assert read_bodies, "no /read call recorded"
    assert read_bodies[0]["page_size"] == 1


def test_search_users_never_drops_matches_when_a_store_page_exceeds_limit(
    monkeypatch, superset_stub
):
    """N-3: sizing the request to the remaining budget assumes the store
    honours `page_size` -- a store that does not (or a fake standing in
    for one, exactly as the review's own probe did) must still lose
    nothing. Over-delivering past `limit` in that case is acceptable;
    silently discarding a row that can never be reached again is not."""
    from superset_ownership import fga

    all_guids = [ADA, BEN, "ghost-guid"]

    def fake_read_page(tuple_key, *, page_size=100, token="", strict=False):
        # Ignores page_size entirely -- the exact misbehaviour this test
        # has to survive.
        if token:
            return [], ""
        tuples = [
            {"user": f"user:{g}", "relation": "member", "object": tuple_key["object"]}
            for g in all_guids
        ]
        return tuples, ""

    monkeypatch.setattr(fga, "read_page", fake_read_page)
    superset_stub.security_manager.user_model = object()
    superset_stub.db.session.query.return_value.all.return_value = []

    page = OpenFGADirectory().search_users(TENANT_A, "", limit=2)

    lost = set(all_guids) - {u["guid"] for u in page["items"]}
    assert lost == set(), f"lost: {sorted(lost)}"


def test_search_users_page_loop_is_capped(monkeypatch, caplog, superset_stub):
    """N-4: the one cursor loop `OpenFGADirectory` owns outright (no
    plug-in involved) stops after a sane number of pages and warns,
    instead of hanging on a store whose continuation token never
    advances to empty."""
    import logging

    superset_stub.security_manager.user_model = object()
    superset_stub.db.session.query.return_value.all.return_value = []
    monkeypatch.setattr(directory, "_MAX_SEARCH_USERS_PAGES", 3)

    calls = {"n": 0}

    def fake_read_page(tuple_key, *, page_size=100, token="", strict=False):
        calls["n"] += 1
        # Always one non-matching tuple and a token that never runs out.
        row = {
            "user": "user:no-match",
            "relation": "member",
            "object": tuple_key["object"],
        }
        return [row], "more"

    monkeypatch.setattr(fga, "read_page", fake_read_page)
    caplog.set_level(logging.WARNING, logger="superset_ownership.directory")

    page = OpenFGADirectory().search_users(TENANT_A, "no-match-ever", limit=100)

    assert calls["n"] == 3
    assert page["next_cursor"] == "more"
    assert any("store pages" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------- M-4: authorizer composition


class _PrefixedIdAuthorizer:
    """A LOCAL stand-in for the shipped `PrefixedIdAuthorizer` example
    (`qa/ivanti_pcs_example/authz.py`, owned by a different slice of this
    series) -- same `_subject_ref`/`_from_subject_ref` shape, so this test
    does not depend on that file. Only `user:` subjects are remapped, as
    the shipped example does; `tenant:`/`group:` pass through unchanged."""

    name = "openfga"
    protocol_version = 1

    def _subject_ref(self, s: str) -> str:
        if s.startswith("user:"):
            return s.replace("user:", "user:neurons|", 1)
        return s

    def _from_subject_ref(self, s: str) -> str:
        return s.replace("user:neurons|", "user:", 1)


@pytest.fixture
def prefixed_authorizer(monkeypatch):
    authorizer = _PrefixedIdAuthorizer()
    monkeypatch.setitem(directory.__dict__, "_loaded_authorizer", lambda: authorizer)
    return authorizer


def test_user_in_tenant_composes_with_a_prefixed_id_authorizer(
    store, prefixed_authorizer
):
    """M-4: without going through the authorizer's `_subject_ref`, a store
    keyed `user:neurons|<guid>` never matches `user:<guid>` and
    `user_in_tenant` answers False forever, even for a real member (the
    review's exact probe)."""
    store.tenant_members[TENANT_A] = [f"neurons|{ADA}"]
    d = OpenFGADirectory()
    assert d.user_in_tenant(ADA, TENANT_A) is True
    # the wire call really carried the mapped form
    assert any(
        call[1].get("tuple_key", {}).get("user") == f"user:neurons|{ADA}"
        for call in store.calls
    )


def test_search_users_unmaps_the_guid_under_a_prefixed_id_authorizer(
    store, superset_stub, prefixed_authorizer
):
    """M-4: without unmapping, `search_users` reports `neurons|<guid>` as
    the member's GUID (the review's exact probe) instead of the plain GUID
    the rest of this module speaks."""
    store.tenant_members[TENANT_A] = [f"neurons|{ADA}"]
    superset_stub.security_manager.user_model = object()
    superset_stub.db.session.query.return_value.all.return_value = []
    page = OpenFGADirectory().search_users(TENANT_A, "")
    assert [u["guid"] for u in page["items"]] == [ADA]


def test_tenant_administrators_composes_with_a_prefixed_id_authorizer(
    store, superset_stub, prefixed_authorizer
):
    store.tenant_members[TENANT_A] = [f"neurons|{ADA}"]
    store.tenant_admins[TENANT_A] = [f"user:neurons|{ADA}"]
    ada_user = FakeAbUser(11, ADA, "Ada", "Lovelace")
    ada_user.is_active = True
    superset_stub.security_manager.find_user.side_effect = (
        lambda username=None, email=None: ada_user if username == ADA else None
    )
    superset_stub.security_manager.user_model = object()
    superset_stub.db.session.query.return_value.all.return_value = [ada_user]

    admins = OpenFGADirectory().tenant_administrators(TENANT_A)
    assert [a["guid"] for a in admins] == [ADA]


def _make_prefixed_id_authorizer():
    """The review's own probe (`PrefixedIdAuthorizer`, issue #73 shape) as
    a LOCAL subclass of the real `OpenFGAAuthorizer` -- not
    `qa/ivanti_pcs_example/authz.py`, owned by a different slice of this
    series. Shared by every M-5/M5b wire-level test below."""
    from superset_ownership.authz import OpenFGAAuthorizer

    class PrefixedIdAuthorizer(OpenFGAAuthorizer):
        def _object_ref(self, obj: str) -> str:
            t, _, i = obj.partition(":")
            return f"{t}:pcs-{i}"

        def _from_object_ref(self, ref: str) -> str:
            t, _, i = ref.partition(":")
            return f"{t}:{i.removeprefix('pcs-')}"

        def _subject_ref(self, s: str) -> str:
            if s.startswith("user:"):
                return s.replace("user:", "user:neurons|", 1)
            return s

        def _from_subject_ref(self, s: str) -> str:
            return s.replace("user:neurons|", "user:", 1)

    return PrefixedIdAuthorizer()


def test_openfga_authorizer_check_and_write_tuple_use_the_ref_hooks(store):
    """M-5: the mutation that survived the full suite was
    `OpenFGAAuthorizer.check`/`write_tuple` calling `fga.*` with the raw
    strings they were called with, bypassing `_subject_ref`/`_object_ref`
    (spec section 8) -- no test drove either method through an authorizer
    whose hooks actually reshape the wire form. Asserts the tuple that
    reaches the fake store's `/check` and `/write` carries the MAPPED
    keys, not the ones `check`/`write_tuple` were called with."""
    authorizer = _make_prefixed_id_authorizer()
    mapped_user = f"user:neurons|{ADA}"

    authorizer.check(f"user:{ADA}", "viewer", "dashboard:u1")
    check_url, check_body = next(c for c in store.calls if c[0].endswith("/check"))
    assert check_body["tuple_key"] == {
        "user": mapped_user,
        "relation": "viewer",
        "object": "dashboard:pcs-u1",
    }

    authorizer.write_tuple(f"user:{ADA}", "viewer", "dashboard:u1")
    write_url, write_body = next(c for c in store.calls if c[0].endswith("/write"))
    assert write_body["writes"]["tuple_keys"] == [
        {"user": mapped_user, "relation": "viewer", "object": "dashboard:pcs-u1"},
    ]


# The mapped spelling of "dashboard:u1" under `_make_prefixed_id_authorizer`
# -- shared by every M5b test below to keep the wire-shape literals short.
_PCS_U1 = "dashboard:pcs-u1"


def test_openfga_authorizer_object_tenant_uses_the_object_hook(store):
    """M5b (survived round 2): `object_tenant` is the one of the nine
    untested hook sites that sits on the ACCESS path (`hooks.py:503`) --
    under a prefixed store it used to read the un-prefixed object, find no
    tenant tuple, and the tenant scope denied a legitimate caller."""
    authorizer = _make_prefixed_id_authorizer()
    store.object_tuples[(_PCS_U1, "tenant")] = [
        {"user": f"tenant:{TENANT_A}#member", "relation": "tenant", "object": _PCS_U1}
    ]

    tenant = authorizer.object_tenant("dashboard", "u1")

    read_url, read_body = next(c for c in store.calls if c[0].endswith("/read"))
    assert read_body["tuple_key"] == {"object": _PCS_U1, "relation": "tenant"}
    assert tenant == TENANT_A


def test_openfga_authorizer_list_grants_maps_both_directions(store):
    """M5b (survived round 2): `list_grants` reads through `_object_ref`
    and must unmap each row's subject through `_from_subject_ref` -- the
    picker/detail would otherwise show the store's prefixed spelling
    (`neurons|<guid>`) instead of the GUID the rest of the module uses."""
    authorizer = _make_prefixed_id_authorizer()
    store.object_tuples[(_PCS_U1, "")] = [
        {"user": f"user:neurons|{ADA}", "relation": "viewer", "object": _PCS_U1},
        {"user": f"tenant:{TENANT_A}#member", "relation": "tenant", "object": _PCS_U1},
    ]

    grants = authorizer.list_grants("dashboard:u1")

    read_url, read_body = next(c for c in store.calls if c[0].endswith("/read"))
    assert read_body["tuple_key"] == {"object": _PCS_U1}
    # the tenant bookkeeping row is excluded, same as the unprefixed class
    assert grants == [{"user": f"user:{ADA}", "relation": "viewer"}]


def test_openfga_authorizer_list_objects_maps_the_subject_and_unmaps_results(store):
    """M5b: `list_objects`'s subject is TOP-level in the `/list-objects`
    body (not nested under `tuple_key`, unlike `/check` and `/write`);
    results come back through `_from_object_ref`."""
    authorizer = _make_prefixed_id_authorizer()
    # FakeStore._list_objects answers every group whose members carry the
    # SUBJECT's (mapped) guid -- seeded under that same mapped spelling,
    # so a hit here proves the outgoing request carried it.
    store.group_members["pcs-eng"] = [f"user:neurons|{ADA}"]

    objs = authorizer.list_objects(f"user:{ADA}", "member", "dashboard")

    call_url, body = next(c for c in store.calls if c[0].endswith("/list-objects"))
    assert body["user"] == f"user:neurons|{ADA}"
    # the raw hit ("group:pcs-eng") comes back unmapped to our own spelling
    assert objs == ["group:eng"]


def test_openfga_authorizer_delete_tuple_maps_both_subject_and_object(store):
    authorizer = _make_prefixed_id_authorizer()

    authorizer.delete_tuple(f"user:{ADA}", "viewer", "dashboard:u1")

    _, body = next(c for c in store.calls if c[0].endswith("/write"))
    assert body["deletes"]["tuple_keys"] == [
        {"user": f"user:neurons|{ADA}", "relation": "viewer", "object": _PCS_U1},
    ]


def test_openfga_authorizer_user_in_group_maps_the_subject(store):
    """`user_in_group`'s `group` argument is passed straight through
    (groups are not in `_object_ref`'s domain, by design -- only the
    `user:` subject is mapped)."""
    authorizer = _make_prefixed_id_authorizer()

    authorizer.user_in_group(ADA, "group:eng")

    _, body = next(c for c in store.calls if c[0].endswith("/check"))
    assert body["tuple_key"] == {
        "user": f"user:neurons|{ADA}",
        "relation": "member",
        "object": "group:eng",
    }


def test_openfga_authorizer_set_object_tenant_maps_the_object(store):
    """`set_object_tenant` maps the asset object, not the tenant subject
    (a `tenant:` reference, outside this example's `_subject_ref`
    domain -- it only reshapes `user:`)."""
    authorizer = _make_prefixed_id_authorizer()

    authorizer.set_object_tenant("dashboard", "u1", TENANT_A)

    _, body = next(c for c in store.calls if c[0].endswith("/write"))
    assert body["writes"]["tuple_keys"] == [
        {"user": f"tenant:{TENANT_A}#member", "relation": "tenant", "object": _PCS_U1},
    ]


def test_openfga_authorizer_list_relations_and_purge_object_map_the_object(store):
    """M5b: `list_relations` and `purge_object` both build their Read
    through `_object_ref`; the wire request must carry the mapped object,
    not the caller's own spelling."""
    authorizer = _make_prefixed_id_authorizer()

    authorizer.list_relations(f"user:{ADA}", "dashboard:u1")
    read_url, read_body = next(c for c in store.calls if c[0].endswith("/read"))
    assert read_body["tuple_key"]["object"] == _PCS_U1

    store.calls.clear()
    authorizer.purge_object("dashboard:u1")
    read_url, read_body = next(c for c in store.calls if c[0].endswith("/read"))
    assert read_body["tuple_key"]["object"] == _PCS_U1


def test_openfga_authorizer_revoke_subject_maps_both_subject_and_object(store):
    """M5b: `revoke_subject` reads through `_object_ref` and filters on
    the MAPPED subject -- a mismatch here would mean a revoke silently
    finds nothing to delete under a prefixed store."""
    authorizer = _make_prefixed_id_authorizer()
    store.object_tuples[(_PCS_U1, "")] = [
        {"user": f"user:neurons|{ADA}", "relation": "viewer", "object": _PCS_U1},
    ]

    authorizer.revoke_subject(f"user:{ADA}", "dashboard:u1")

    read_url, read_body = next(c for c in store.calls if c[0].endswith("/read"))
    assert read_body["tuple_key"] == {"object": _PCS_U1}
    write_url, write_body = next(c for c in store.calls if c[0].endswith("/write"))
    assert write_body["deletes"]["tuple_keys"] == [
        {"user": f"user:neurons|{ADA}", "relation": "viewer", "object": _PCS_U1},
    ]


# ------------------------------------------------ L-4: OWNERSHIP_DIRECTORY_GROUP_WALK


@pytest.mark.parametrize("bad", ["Always", "ALWAYS", "off", "1", "sometimes"])
def test_an_unrecognised_group_walk_setting_warns_and_falls_back_to_auto(
    bad, monkeypatch, caplog
):
    # An empty/blank value is "unset" (settings.get's own default rule, like
    # OWNERSHIP_GROUP_ID_FORMAT's blank-means-default) -- not exercised here.
    import logging

    monkeypatch.setenv("OWNERSHIP_DIRECTORY_GROUP_WALK", bad)
    caplog.set_level(logging.WARNING, logger="superset_ownership.directory")
    d = OpenFGADirectory()
    assert d._mode == "auto"
    assert any(
        "OWNERSHIP_DIRECTORY_GROUP_WALK" in r.getMessage() for r in caplog.records
    )


@pytest.mark.parametrize("good", ["auto", "always", "never", " never "])
def test_a_recognised_group_walk_setting_is_used_as_given(good, monkeypatch):
    monkeypatch.setenv("OWNERSHIP_DIRECTORY_GROUP_WALK", good)
    d = OpenFGADirectory()
    assert d._mode == good.strip()


# --------------------------------------------------------------- LocalDirectory


@dataclass
class Role:
    name: str


@dataclass
class LocalUser:
    id: int
    username: str
    roles: list = field(default_factory=list)
    first_name: str = ""
    last_name: str = ""
    email: str | None = None
    is_active: bool = True


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
def local_world(superset_stub):
    tenant_role_a = Role(f"tenant_{TENANT_A}")
    from superset_ownership.identity import group_id

    users = [
        LocalUser(
            1,
            ADA,
            [tenant_role_a, Role(group_id("dashboard_designer", TENANT_A))],
            "Ada",
            "Lovelace",
            "ada@example.com",
        ),
        LocalUser(
            2,
            BEN,
            [tenant_role_a, Role(group_id("chart_designer", TENANT_A))],
            "Ben",
            "Smith",
            "ben@example.com",
        ),
    ]
    superset_stub.security_manager = FakeSecurityManager(users)
    return users


def test_local_directory_search_users_and_user_in_tenant(local_world):
    d = LocalDirectory()
    page = d.search_users(TENANT_A, "")
    # The guid a UserRef carries is the STORE id, Neurons' `<tenant>.<member>`.
    assert {u["guid"] for u in page["items"]} == {
        f"{TENANT_A}.{ADA}",
        f"{TENANT_A}.{BEN}",
    }
    assert d.user_in_tenant(ADA, TENANT_A) is True
    assert d.user_in_tenant(f"{TENANT_A}.{ADA}", TENANT_A) is True
    assert d.user_in_tenant(ADA, TENANT_B) is False
    assert d.user_in_tenant("nobody", TENANT_A) is False


def test_local_directory_list_groups_matches_the_old_authorizer_method(local_world):
    from superset_ownership.identity import group_object

    groups = LocalDirectory().list_groups(TENANT_A)["items"]
    assert {g["id"] for g in groups} == {
        group_object("dashboard_designer", TENANT_A),
        group_object("chart_designer", TENANT_A),
    }
    assert all(g["members"] == 1 for g in groups)
    assert all(g["tenant"] == TENANT_A for g in groups)


def test_local_directory_group_exists_and_group_members(local_world):
    from superset_ownership.identity import group_id

    name = group_id("dashboard_designer", TENANT_A)
    d = LocalDirectory()
    assert d.group_exists(f"group:{name}") is True
    assert d.group_exists("group:nonexistent") is False
    members = d.group_members(f"group:{name}")["items"]
    # The guid a UserRef carries is the STORE id, Neurons' `<tenant>.<member>`.
    assert [m["guid"] for m in members] == [f"{TENANT_A}.{ADA}"]


def test_local_directory_user_in_group_delegates_to_the_authorizer(local_world):
    from superset_ownership.identity import group_id

    name = group_id("dashboard_designer", TENANT_A)
    d = LocalDirectory()
    assert d.user_in_group(ADA, f"group:{name}") is True
    assert d.user_in_group(BEN, f"group:{name}") is False


def test_local_directory_health():
    assert LocalDirectory().health() == DirectoryHealth(ok=True, detail="FAB roles")


# ------------------------------------- H-2: user_in_tenant spelling parity


def test_local_directory_user_in_tenant_accepts_every_spelling_the_old_method_did(
    superset_stub,
):
    """H-2: the pre-split `LocalAuthorizer.tenant_members` answered `guid in
    tenant_members(t)`, and `tenant_members` was a scan comparing
    `resolve_member_guid(user)`, never a username lookup -- true for a
    `local-<id>` spelling (this repo's own demo users, whose usernames
    carry no GUID) and for a mixed-case GUID username alike. The probe in
    the review names these two exact spellings."""
    tenant_role_a = Role(f"tenant_{TENANT_A}")
    # username carries no GUID at all -> resolve_member_guid falls back to
    # "local-<id>".
    alice = LocalUser(7, "alice", [tenant_role_a])
    # username IS a GUID, but upper-cased -> resolve_member_guid lower-cases it.
    ben_mixed_case = LocalUser(2, BEN.upper(), [tenant_role_a])
    superset_stub.security_manager = FakeSecurityManager([alice, ben_mixed_case])

    d = LocalDirectory()
    assert d.user_in_tenant("local-7", TENANT_A) is True
    assert d.user_in_tenant(BEN, TENANT_A) is True
    assert d.user_in_tenant("nobody", TENANT_A) is False
    assert d.user_in_tenant("local-7", TENANT_B) is False


# ------------------------------------------------------------------- structural


def test_hooks_module_never_imports_the_directory():
    """Zero directory calls on the access path (contract section 5): the
    invariant enforced structurally rather than only by convention."""
    import inspect

    from superset_ownership import hooks

    source = inspect.getsource(hooks)
    assert "get_directory" not in source
    assert "superset_ownership.directory" not in source
    assert "from superset_ownership import directory" not in source

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
"""The OpenFGA client's failure contract, against a mocked HTTP layer.

The outbox's whole retry-versus-park decision rests on the client raising
the right exception for the right response. These tests pin that contract
at the source, response by response, so the outbox tests' fake store is
known to be modelling something real:

  transport error, 5xx      -> StoreUnavailable   (retry)
  4xx, non-JSON body        -> StoreRejected      (park)
  4xx "already exists" /
  "does not exist"          -> success            (idempotent)

and that the OpenFGA-backed authorizer's purge and revoke are strict end to
end -- read AND delete -- and that revoke never removes ownership.
"""

from __future__ import annotations

import json  # noqa: TID251 - pure unit test file, stays importable without Superset
import os
from unittest import mock

import pytest
import requests
from superset_ownership import fga, fga_connection, plugins
from superset_ownership.authz import OpenFGAAuthorizer
from superset_ownership.fga import StoreRejected, StoreUnavailable

OBJ = "chart:326fc7e5-b7f1-448e-8a6f-80d0e7ce0b64"
BEN = "user:6c2b48e9-5a71-4f92-8d03-2e9b7c1a4d53"


def _resp(status=200, json=None, text=None):
    r = requests.Response()
    r.status_code = status
    if text is not None:
        r._content = text.encode()
    else:
        import json as _json  # noqa: TID251 - see the module-level import above

        r._content = _json.dumps(json if json is not None else {}).encode()
    r.headers["Content-Type"] = "application/json"
    return r


ALREADY_EXISTS = _resp(
    400,
    {
        "code": "write_failed_due_to_invalid_input",
        "message": "cannot write a tuple which already exists: ...",
    },
)
DOES_NOT_EXIST = _resp(
    400,
    {
        "code": "write_failed_due_to_invalid_input",
        "message": "cannot delete a tuple which does not exist: ...",
    },
)
VALIDATION = _resp(
    400, {"code": "validation_error", "message": "relation 'chart#veiwer' not found"}
)


# ---------------------------------------------------------------- read_all(strict=True)


@pytest.mark.parametrize(
    "failure, expected",
    [
        (requests.exceptions.ConnectionError("refused"), StoreUnavailable),
        (requests.exceptions.ReadTimeout("slow"), StoreUnavailable),
        (_resp(503, text="upstream unavailable"), StoreUnavailable),
        (_resp(500, text="boom"), StoreUnavailable),
        (_resp(400, {"code": "validation_error"}), StoreRejected),
        (_resp(401, {"code": "unauthenticated"}), StoreRejected),
        (_resp(200, text="<html>proxy login</html>"), StoreRejected),
    ],
    ids=["connection", "timeout", "503", "500", "400", "401", "html-200"],
)
def test_strict_read_raises_on_every_failure(failure, expected):
    with mock.patch.object(
        fga.requests,
        "post",
        side_effect=[failure] if isinstance(failure, Exception) else None,
        return_value=None if isinstance(failure, Exception) else failure,
    ):
        with pytest.raises(expected):
            fga.read_all(OBJ, strict=True)


def test_non_strict_read_keeps_the_fail_closed_list_contract():
    with mock.patch.object(fga.requests, "post", return_value=_resp(503, text="x")):
        assert fga.read_all(OBJ) == []


def test_strict_read_follows_the_continuation_token():
    pages = [
        _resp(
            200,
            {
                "tuples": [{"key": {"user": BEN, "relation": "viewer", "object": OBJ}}],
                "continuation_token": "t1",
            },
        ),
        _resp(
            200,
            {
                "tuples": [
                    {"key": {"user": "user:a", "relation": "owner", "object": OBJ}}
                ],
                "continuation_token": "",
            },
        ),
    ]
    with mock.patch.object(fga.requests, "post", side_effect=pages):
        assert len(fga.read_all(OBJ, strict=True)) == 2


def test_object_tenant_is_non_strict_by_default_and_strict_on_request():
    # The read gate keeps the fail-closed "no tenant" answer; a write that
    # would stamp a tenant on "no tenant" asks strictly and gets the failure.
    with mock.patch.object(
        fga.requests, "post", return_value=_resp(503, text="upstream unavailable")
    ):
        assert fga.object_tenant("chart", "u1") is None
        with pytest.raises(StoreUnavailable):
            fga.object_tenant("chart", "u1", strict=True)
    with mock.patch.object(
        fga.requests, "post", return_value=_resp(400, {"code": "validation_error"})
    ):
        with pytest.raises(StoreRejected):
            fga.object_tenant("chart", "u1", strict=True)
    tenant_tuple = {
        "key": {"user": "tenant:t1#member", "relation": "tenant", "object": "chart:u1"}
    }
    with mock.patch.object(
        fga.requests, "post", return_value=_resp(200, {"tuples": [tenant_tuple]})
    ):
        assert fga.object_tenant("chart", "u1", strict=True) == "t1"


def test_authorizer_object_tenant_passes_strict_through():
    with mock.patch.object(fga, "object_tenant", return_value="t1") as read:
        assert OpenFGAAuthorizer().object_tenant("chart", "u1", strict=True) == "t1"
        read.assert_called_once_with("chart", "u1", strict=True)
        OpenFGAAuthorizer().object_tenant("chart", "u1")
        read.assert_called_with("chart", "u1", strict=False)


# ------------------------------------------------------------------- write(strict=True)


@pytest.mark.parametrize(
    "failure, expected",
    [
        (requests.exceptions.ReadTimeout("slow"), StoreUnavailable),
        (requests.exceptions.ConnectionError("refused"), StoreUnavailable),
        (_resp(502, text="bad gateway"), StoreUnavailable),
        (VALIDATION, StoreRejected),
        (_resp(401, {"code": "unauthenticated"}), StoreRejected),
    ],
    ids=["timeout", "connection", "502", "validation-400", "401"],
)
def test_strict_write_raises_on_every_failure(failure, expected):
    kw = (
        {"side_effect": failure}
        if isinstance(failure, Exception)
        else {"return_value": failure}
    )
    with mock.patch.object(fga.requests, "post", **kw):
        with pytest.raises(expected):
            fga.write_tuple(BEN, "viewer", OBJ, strict=True)


@pytest.mark.parametrize(
    "resp", [ALREADY_EXISTS, DOES_NOT_EXIST], ids=["already-exists", "does-not-exist"]
)
def test_strict_write_treats_idempotent_rejections_as_success(resp):
    with mock.patch.object(fga.requests, "post", return_value=resp):
        assert fga.write_tuple(BEN, "viewer", OBJ, strict=True) is True
        assert fga.delete_tuple(BEN, "viewer", OBJ, strict=True) is True


def test_non_strict_write_keeps_the_bool_contract():
    with mock.patch.object(
        fga.requests, "post", side_effect=requests.exceptions.ReadTimeout("slow")
    ):
        assert fga.write_tuple(BEN, "viewer", OBJ) is False
    with mock.patch.object(fga.requests, "post", return_value=VALIDATION):
        assert fga.write_tuple(BEN, "viewer", OBJ) is False


def test_strict_set_object_tenant_goes_through_write():
    with mock.patch.object(fga.requests, "post", return_value=_resp(503, text="x")):
        with pytest.raises(StoreUnavailable):
            fga.set_object_tenant("chart", "u1", "t1", strict=True)
    with pytest.raises(StoreRejected):
        fga.set_object_tenant("chart", "", "t1", strict=True)


# ------------------------------------------------------------- delete_each(strict=True)


def test_strict_delete_each_stops_at_the_first_failure_and_keeps_what_landed():
    calls = [_resp(200), _resp(503, text="x"), _resp(200)]
    with mock.patch.object(fga.requests, "post", side_effect=calls) as post:
        with pytest.raises(StoreUnavailable):
            fga.delete_each([{"user": "a"}, {"user": "b"}, {"user": "c"}], strict=True)
        assert post.call_count == 2, "the third delete is left for the retry"


def test_strict_delete_each_rejection_is_rejected():
    with mock.patch.object(fga.requests, "post", return_value=VALIDATION):
        with pytest.raises(StoreRejected):
            fga.delete_each([{"user": "a"}], strict=True)
    with mock.patch.object(fga.requests, "post", return_value=DOES_NOT_EXIST):
        assert fga.delete_each([{"user": "a"}], strict=True) == {
            "deleted": 0,
            "already_absent": 1,
            "failed": 0,
        }


# -------------------------------------------------------------------------- reachable()


def test_reachable_probes_the_root_healthz_and_needs_a_200():
    with mock.patch.object(
        fga.requests, "get", return_value=_resp(200, {"status": "SERVING"})
    ) as get:
        assert fga.reachable() is True
        assert get.call_args.args[0] == f"{fga.FGA_API_URL}/healthz", (
            "OpenFGA's healthz is not per-store"
        )
    with mock.patch.object(
        fga.requests, "get", return_value=_resp(404, text="not found")
    ):
        assert fga.reachable() is False, (
            "any HTTP server answering 404 is not the store"
        )
    with mock.patch.object(
        fga.requests, "get", side_effect=requests.exceptions.ConnectionError("x")
    ):
        assert fga.reachable() is False


# --------------------------------------------------------------- the OpenFGA authorizer


def _store(tuples_on_obj, write_resp=None):
    """Route /read to a page of `tuples_on_obj`; /write to `write_resp` (200)."""

    def post(url, json=None, headers=None, timeout=None):
        if url.endswith("/read"):
            return _resp(
                200,
                {
                    "tuples": [{"key": t} for t in tuples_on_obj],
                    "continuation_token": "",
                },
            )
        # `bool(Response)` is `.ok`, so `write_resp or ...` would swap a 4xx for a 200.
        return write_resp if write_resp is not None else _resp(200)

    return post


def test_authorizer_purge_raises_when_the_read_fails_with_a_5xx():
    """The exact hole the second review reproduced: a 503 on the read must not
    read as an empty object and return True."""
    with mock.patch.object(
        fga.requests, "post", return_value=_resp(503, text="restarting")
    ) as post:
        with pytest.raises(StoreUnavailable):
            OpenFGAAuthorizer().purge_object(OBJ)
        assert all(c.args[0].endswith("/read") for c in post.call_args_list), (
            "no delete was issued"
        )


def test_authorizer_revoke_raises_when_the_read_is_refused():
    with mock.patch.object(fga.requests, "post", return_value=VALIDATION):
        with pytest.raises(StoreRejected):
            OpenFGAAuthorizer().revoke_subject(BEN, OBJ)


def test_authorizer_revoke_removes_shares_only_never_owner_or_tenant():
    tuples = [
        {"user": BEN, "relation": "owner", "object": OBJ},
        {"user": BEN, "relation": "editor", "object": OBJ},
        {"user": BEN, "relation": "viewer", "object": OBJ},
        {"user": "tenant:t1#member", "relation": "tenant", "object": OBJ},
        {"user": "user:other", "relation": "viewer", "object": OBJ},
    ]
    with mock.patch.object(fga.requests, "post", side_effect=_store(tuples)) as post:
        assert OpenFGAAuthorizer().revoke_subject(BEN, OBJ) is True
    deleted = [
        c.kwargs["json"]["deletes"]["tuple_keys"][0]["relation"]
        for c in post.call_args_list
        if c.args[0].endswith("/write")
    ]
    assert sorted(deleted) == ["editor", "viewer"]


def test_authorizer_purge_raises_when_a_delete_is_refused():
    tuples = [{"user": BEN, "relation": "viewer", "object": OBJ}]
    with mock.patch.object(
        fga.requests, "post", side_effect=_store(tuples, write_resp=VALIDATION)
    ):
        with pytest.raises(StoreRejected):
            OpenFGAAuthorizer().purge_object(OBJ)


def test_authorizer_purge_succeeds_and_deletes_every_tuple():
    tuples = [
        {"user": BEN, "relation": "viewer", "object": OBJ},
        {"user": "user:a", "relation": "owner", "object": OBJ},
        {"user": "tenant:t1#member", "relation": "tenant", "object": OBJ},
    ]
    with mock.patch.object(fga.requests, "post", side_effect=_store(tuples)) as post:
        assert OpenFGAAuthorizer().purge_object(OBJ) is True
    assert len([c for c in post.call_args_list if c.args[0].endswith("/write")]) == 3


# ------------------------------------------------------- connection threading (spec §5)
#
# Every test below builds its connection purely from process environment
# variables -- no Flask app, no app context -- which is the production
# no-app path: `fga._conn()` / `fga.refresh_connection()` / `fga._may_refresh()`
# all delegate straight to `superset_ownership.plugins`, whose own no-app
# fallback (`plugins.get_fga_connection()` -> `plugins._build_connection({})`)
# reads the same environment variables through `settings.get`.


@pytest.fixture(autouse=True)
def _reset_refresh_cooldown(monkeypatch):
    """A 401 test elsewhere in this file must not leave the process-local
    refresh cooldown armed for a later, unrelated test -- the whole suite
    runs in one process and `plugins._fallback_refreshed_at` (the no-app
    path's cooldown timestamp, consulted by `plugins.may_refresh`) is
    module state."""
    from superset_ownership import plugins

    monkeypatch.setattr(plugins, "_fallback_refreshed_at", 0.0)


def _token_env_connection(
    monkeypatch, token: str, *, api_url="http://store.example", store="S1"
):
    """Point `fga._conn()` at a store requiring `Authorization: Bearer
    <token>`, re-read from `OWNERSHIP_TOKEN_FOR_TEST` on every build -- so a
    test can change the token between an initial call and a refresh to
    simulate rotation."""
    monkeypatch.setenv("OWNERSHIP_FGA_API_URL", api_url)
    monkeypatch.setenv("OWNERSHIP_FGA_STORE", store)
    monkeypatch.delenv("OWNERSHIP_FGA_MODEL", raising=False)
    monkeypatch.delenv("OWNERSHIP_FGA_CONFIG_PROVIDER", raising=False)
    monkeypatch.setenv("OWNERSHIP_TOKEN_FOR_TEST", token)
    monkeypatch.setenv(
        "OWNERSHIP_FGA_CREDENTIALS",
        json.dumps({"type": "api_token", "token_env": "OWNERSHIP_TOKEN_FOR_TEST"}),
    )


def test_conn_reaches_the_env_default_through_plugins(monkeypatch):
    """`fga._conn()` reaches `plugins.get_fga_connection()`, whose own
    no-app fallback (`_build_connection`) builds the same env default --
    pins the number, not the path (N4: no ImportError fallback survives in
    `fga._conn()` to fall back to)."""
    monkeypatch.delenv("OWNERSHIP_FGA_API_URL", raising=False)
    monkeypatch.delenv("OWNERSHIP_FGA_CONFIG_PROVIDER", raising=False)
    conn = fga._conn()
    assert conn.api_url == "http://openfga:8080"


def test_headers_are_injected_on_every_request_site(monkeypatch):
    """The table in spec §5.4: every function that talks to the store must
    carry the connection's headers, not just the read/write paths the
    outbox exercises elsewhere in this file."""
    _token_env_connection(monkeypatch, "tok-1")
    expected = {"Authorization": "Bearer tok-1"}

    with mock.patch.object(
        fga.requests, "get", return_value=_resp(200, {"status": "SERVING"})
    ) as get:
        fga.reachable()
        assert get.call_args.kwargs["headers"] == expected

    with mock.patch.object(
        fga.requests, "post", return_value=_resp(200, {"allowed": True})
    ) as post:
        fga.check(BEN, "viewer", OBJ)
        assert post.call_args.kwargs["headers"] == expected

    with mock.patch.object(
        fga.requests, "post", return_value=_resp(200, {"objects": []})
    ) as post:
        fga.list_objects(BEN, "viewer", "dashboard")
        assert post.call_args.kwargs["headers"] == expected

    with mock.patch.object(fga.requests, "post", return_value=_resp(200)) as post:
        fga.write_tuple(BEN, "viewer", OBJ)
        assert post.call_args.kwargs["headers"] == expected

    with mock.patch.object(
        fga.requests,
        "post",
        return_value=_resp(200, {"tuples": [], "continuation_token": ""}),
    ) as post:
        fga.read_all(OBJ)
        assert post.call_args.kwargs["headers"] == expected

    with mock.patch.object(
        fga.requests,
        "post",
        return_value=_resp(200, {"tuples": [], "continuation_token": ""}),
    ) as post:
        fga.tenant_objects("t1", "dashboard")
        assert post.call_args.kwargs["headers"] == expected

    with mock.patch.object(
        fga.requests, "post", return_value=_resp(200, {"tuples": []})
    ) as post:
        fga.group_exists("eng")
        assert post.call_args.kwargs["headers"] == expected

    with mock.patch.object(fga.requests, "post", return_value=_resp(200)) as post:
        fga.delete_each([{"user": BEN, "relation": "viewer", "object": OBJ}])
        assert post.call_args.kwargs["headers"] == expected

    with mock.patch.object(
        fga.requests,
        "get",
        return_value=_resp(
            200,
            {
                "authorization_models": [
                    {"id": "m1", "schema_version": "1.1", "type_definitions": []}
                ]
            },
        ),
    ) as get:
        fga.get_model()
        assert get.call_args.kwargs["headers"] == expected

    with mock.patch.object(
        fga.requests, "post", return_value=_resp(200, {"authorization_model_id": "m2"})
    ) as post:
        fga.write_model({"schema_version": "1.1", "type_definitions": []})
        assert post.call_args.kwargs["headers"] == expected


def test_401_refreshes_once_and_retries_once(monkeypatch):
    _token_env_connection(monkeypatch, "tok-1")
    calls: list[dict] = []

    def post(url, json=None, headers=None, timeout=None):
        calls.append(dict(headers))
        return _resp(401, {"code": "unauthenticated"})

    with mock.patch.object(fga.requests, "post", side_effect=post):
        resp = fga._request("POST", "/check", json={}, what="check")

    assert resp.status_code == 401
    assert len(calls) == 2, "one original call plus exactly one retry"
    assert calls[0] == calls[1] == {"Authorization": "Bearer tok-1"}, (
        "credentials had not rotated"
    )


def test_401_refresh_picks_up_a_rotated_token_and_warns_it_changed(monkeypatch, caplog):
    _token_env_connection(monkeypatch, "tok-1")
    calls: list[dict] = []

    def post(url, json=None, headers=None, timeout=None):
        calls.append(dict(headers))
        if len(calls) == 1:
            monkeypatch.setenv("OWNERSHIP_TOKEN_FOR_TEST", "tok-2")
        return _resp(401, {"code": "unauthenticated"})

    with caplog.at_level("WARNING"):
        with mock.patch.object(fga.requests, "post", side_effect=post):
            fga._request("POST", "/check", json={}, what="check")

    assert calls[0] == {"Authorization": "Bearer tok-1"}
    assert calls[1] == {"Authorization": "Bearer tok-2"}
    assert any("credentials changed" in r.message for r in caplog.records)


def test_401_refresh_to_the_same_credentials_warns_unchanged(monkeypatch, caplog):
    _token_env_connection(monkeypatch, "tok-1")

    with caplog.at_level("WARNING"):
        with mock.patch.object(
            fga.requests, "post", return_value=_resp(401, {"code": "unauthenticated"})
        ):
            fga._request("POST", "/check", json={}, what="check")

    assert any(
        "unchanged" in r.message and "OWNERSHIP_FGA_CREDENTIALS" in r.message
        for r in caplog.records
    )


def test_second_401_within_the_cooldown_does_not_refresh_or_retry(monkeypatch):
    _token_env_connection(monkeypatch, "tok-1")

    with mock.patch.object(
        fga.requests, "post", return_value=_resp(401, {"code": "unauthenticated"})
    ) as post:
        fga._request("POST", "/check", json={}, what="check")
        assert post.call_count == 2, "the first 401 gets one refresh-and-retry"
        fga._request("POST", "/check", json={}, what="check")
        assert post.call_count == 3, (
            "the second 401, inside the cooldown, must fail fast: exactly one more call"
        )


def test_a_connection_pinned_via_the_connection_kwarg_skips_401_retry(monkeypatch):
    """`fga install-model --api-url/--store/--credentials-env` pins a
    one-off connection; retrying with the AMBIENT connection on a 401 would
    silently abandon the operator's override."""
    pinned = fga_connection.FgaConnection(
        api_url="http://pinned.example",
        store_id="PIN",
        model_id=None,
        headers={},
        timeout_s=1.0,
    )
    with mock.patch.object(
        fga.requests, "post", return_value=_resp(401, {"code": "unauthenticated"})
    ) as post:
        resp = fga._request(
            "POST",
            "/authorization-models",
            json={},
            what="write_model",
            connection=pinned,
        )
    assert resp.status_code == 401
    assert post.call_count == 1, "no refresh-and-retry when a connection is pinned"


def test_reconnect_always_refreshes_even_though_it_stamps_the_cooldown(monkeypatch):
    """`fga reconnect` is the operator path -- `refresh_connection()` never
    consults the cooldown, it always rebuilds; it stamps
    `plugins._fallback_refreshed_at` (the no-app path's cooldown timestamp,
    the one `plugins.may_refresh` actually consults) so the NEXT 401
    respects the cooldown it just started. N4: `fga.refresh_connection()`
    is a thin delegation straight to `plugins.refresh_fga_connection()` now
    -- no second, redundant timestamp of its own."""
    values = iter([100.0, 100.5])
    monkeypatch.setattr(plugins.time, "monotonic", lambda: next(values))
    fga.refresh_connection()
    assert plugins._fallback_refreshed_at == 100.0
    fga.refresh_connection()
    assert plugins._fallback_refreshed_at == 100.5


def test_provider_is_called_to_build_and_to_refresh_the_connection(monkeypatch):
    calls = {"n": 0}

    def provider():
        calls["n"] += 1
        return fga_connection.FgaConnection(
            api_url="http://p.example",
            store_id="PS",
            model_id=None,
            headers={},
            timeout_s=1.0,
        )

    monkeypatch.setattr(
        fga_connection, "resolve_provider", lambda v: provider if v else None
    )
    monkeypatch.setenv("OWNERSHIP_FGA_CONFIG_PROVIDER", "irrelevant:patched-above")

    conn = _build_env_connection()
    assert conn.api_url == "http://p.example"
    assert calls["n"] == 1

    fga.refresh_connection()
    assert calls["n"] == 2


# ----------------------------------------------------- OWNERSHIP_FGA_CREDENTIALS shapes
#
# B3/N4: these build a connection through the LIVE production path --
# `plugins._build_connection` with an empty config layer, which is exactly
# what `fga._conn()` -> `plugins.get_fga_connection()` reaches with no
# Flask app context -- rather than a standalone fallback builder production
# no longer calls. A malformed value surfaces as `plugins.PluginError`
# (`_build_connection` wraps both `fga_connection.CredentialError` and a
# `settings.SettingError` from bad JSON the same way).


def _build_env_connection():
    _, connection = plugins._build_connection({})
    return connection


def test_credentials_default_is_none_and_carries_no_headers(monkeypatch):
    monkeypatch.delenv("OWNERSHIP_FGA_CREDENTIALS", raising=False)
    assert _build_env_connection().headers == {}


def test_credentials_api_token_needs_token_env_set_and_non_blank(monkeypatch):
    monkeypatch.delenv("OWNERSHIP_MISSING_TOKEN_FOR_TEST", raising=False)
    monkeypatch.setenv(
        "OWNERSHIP_FGA_CREDENTIALS",
        json.dumps(
            {"type": "api_token", "token_env": "OWNERSHIP_MISSING_TOKEN_FOR_TEST"}
        ),
    )
    with pytest.raises(plugins.PluginError, match="OWNERSHIP_MISSING_TOKEN_FOR_TEST"):
        _build_env_connection()

    monkeypatch.setenv("OWNERSHIP_MISSING_TOKEN_FOR_TEST", "")
    with pytest.raises(plugins.PluginError, match="OWNERSHIP_MISSING_TOKEN_FOR_TEST"):
        _build_env_connection()


def test_credentials_api_token_inline_secret_works_and_warns(monkeypatch, caplog):
    monkeypatch.setenv(
        "OWNERSHIP_FGA_CREDENTIALS",
        json.dumps({"type": "api_token", "token": "inline-secret"}),
    )
    with caplog.at_level("WARNING"):
        conn = _build_env_connection()
    assert conn.headers == {"Authorization": "Bearer inline-secret"}
    assert any(
        "secret in config" in r.message and "token_env" in r.message
        for r in caplog.records
    )


def test_credentials_oidc_is_rejected_pointing_at_v2(monkeypatch):
    monkeypatch.setenv(
        "OWNERSHIP_FGA_CREDENTIALS", json.dumps({"type": "oidc_client_credentials"})
    )
    with pytest.raises(plugins.PluginError, match="OWNERSHIP_FGA_CONFIG_PROVIDER"):
        _build_env_connection()


def test_credentials_unknown_type_is_rejected_naming_accepted_types(monkeypatch):
    monkeypatch.setenv("OWNERSHIP_FGA_CREDENTIALS", json.dumps({"type": "kerberos"}))
    with pytest.raises(plugins.PluginError, match="kerberos"):
        _build_env_connection()


def test_credentials_not_json_is_rejected(monkeypatch):
    """B3: the malformed-JSON case, moved onto the production path -- this
    used to exercise only `fga_connection.build_connection_from_env`, a
    builder `plugins._build_connection` never called, which is exactly how
    the silent `{"type": "none"}` fallback survived review."""
    monkeypatch.setenv("OWNERSHIP_FGA_CREDENTIALS", "{not json")
    with pytest.raises(plugins.PluginError, match="OWNERSHIP_FGA_CREDENTIALS"):
        _build_env_connection()


def test_credentials_not_an_object_is_rejected(monkeypatch):
    """B3: same move -- a JSON array is valid JSON but not a mapping."""
    monkeypatch.setenv("OWNERSHIP_FGA_CREDENTIALS", json.dumps(["type", "none"]))
    with pytest.raises(plugins.PluginError, match="OWNERSHIP_FGA_CREDENTIALS"):
        _build_env_connection()


# --------------------------------------------- OWNERSHIP_FGA_CONFIG_PROVIDER resolution


def test_resolve_provider_none_means_no_provider():
    assert fga_connection.resolve_provider(None) is None


def test_resolve_provider_accepts_a_callable_as_is():
    def fn():
        raise AssertionError("never called")

    assert fga_connection.resolve_provider(fn) is fn


def test_resolve_provider_imports_a_colon_separated_dotted_reference():
    resolved = fga_connection.resolve_provider(
        "superset_ownership.fga_connection:StaticProvider"
    )
    assert resolved is fga_connection.StaticProvider


def test_resolve_provider_imports_a_dot_separated_reference():
    resolved = fga_connection.resolve_provider(
        "superset_ownership.fga_connection.StaticProvider"
    )
    assert resolved is fga_connection.StaticProvider


def test_resolve_provider_rejects_an_unimportable_module():
    with pytest.raises(
        fga_connection.CredentialError, match="OWNERSHIP_FGA_CONFIG_PROVIDER"
    ):
        fga_connection.resolve_provider("this.module.does.not:exist")


def test_resolve_provider_rejects_a_missing_attribute():
    with pytest.raises(fga_connection.CredentialError):
        fga_connection.resolve_provider(
            "superset_ownership.fga_connection:not_a_real_name"
        )


def test_resolve_provider_rejects_a_non_callable_non_string():
    with pytest.raises(fga_connection.CredentialError):
        fga_connection.resolve_provider(123)


def test_provider_returning_the_wrong_type_is_a_credential_error(monkeypatch):
    monkeypatch.setattr(
        fga_connection,
        "resolve_provider",
        lambda v: (lambda: "not a connection") if v else None,
    )
    monkeypatch.setenv("OWNERSHIP_FGA_CONFIG_PROVIDER", "irrelevant:patched-above")
    with pytest.raises(plugins.PluginError, match="must return FgaConnection"):
        _build_env_connection()


# ------------------------------------------------------- the fga.FGA_* __getattr__ shim


def test_shim_reads_todays_defaults_with_no_env_set(monkeypatch):
    for key in (
        "OWNERSHIP_FGA_API_URL",
        "OWNERSHIP_FGA_STORE",
        "OWNERSHIP_FGA_MODEL",
        "OWNERSHIP_FGA_TIMEOUT",
        "OWNERSHIP_FGA_CREDENTIALS",
        "OWNERSHIP_FGA_CONFIG_PROVIDER",
    ):
        monkeypatch.delenv(key, raising=False)
    assert fga.FGA_API_URL == "http://openfga:8080"
    assert fga.FGA_STORE_ID == "01M1TT1CJ9PWQVF6KWBEJNSKB8"
    assert fga.FGA_MODEL_ID is None
    assert fga.TIMEOUT == 2.0


def test_shim_reflects_env_overrides_at_read_time(monkeypatch):
    monkeypatch.setenv("OWNERSHIP_FGA_API_URL", "http://other:9999")
    monkeypatch.setenv("OWNERSHIP_FGA_STORE", "OTHERSTORE")
    monkeypatch.setenv("OWNERSHIP_FGA_MODEL", "M123")
    monkeypatch.setenv("OWNERSHIP_FGA_TIMEOUT", "5.5")
    assert fga.FGA_API_URL == "http://other:9999"
    assert fga.FGA_STORE_ID == "OTHERSTORE"
    assert fga.FGA_MODEL_ID == "M123"
    assert fga.TIMEOUT == 5.5


def test_shim_unknown_attribute_raises_attribute_error():
    with pytest.raises(AttributeError):
        fga.NOT_A_REAL_MODULE_ATTRIBUTE  # noqa: B018 - the access is the test


def test_shim_assignment_does_not_change_what_a_request_actually_uses(monkeypatch):
    """The shim replaces the old assignable globals with derived reads; a
    caller that still assigns `fga.FGA_API_URL = ...` sets a plain module
    attribute that shadows the shim on READ, but nothing in `_conn()`/
    `_request()` consults it -- so it has no effect on where a request goes."""
    monkeypatch.delenv("OWNERSHIP_FGA_API_URL", raising=False)
    fga.FGA_API_URL = "http://nope-this-does-nothing"
    try:
        with mock.patch.object(
            fga.requests, "get", return_value=_resp(200, {"status": "SERVING"})
        ) as get:
            fga.reachable()
            assert get.call_args.args[0] == "http://openfga:8080/healthz"
    finally:
        del fga.__dict__["FGA_API_URL"]


# ----------------------------------------------------- S5: 401 retry rebuilds the model


def test_401_retry_rebuilds_the_body_from_the_refreshed_connections_model(monkeypatch):
    """A provider that rotates the pin along with the token -- the kind
    OWNERSHIP_FGA_CONFIG_PROVIDER exists for -- must have the retry carry
    the REFRESHED connection's model id, not the pre-refresh one baked into
    the caller's `json` before `_request` ever saw it."""
    connections = iter(
        [
            fga_connection.FgaConnection(
                api_url="http://store.example",
                store_id="S1",
                model_id="MODEL1",
                headers={"Authorization": "Bearer t1"},
            ),
            fga_connection.FgaConnection(
                api_url="http://store.example",
                store_id="S1",
                model_id="MODEL2",
                headers={"Authorization": "Bearer t2"},
            ),
        ]
    )

    def provider():
        return next(connections)

    monkeypatch.setattr(
        fga_connection, "resolve_provider", lambda v: provider if v else None
    )
    monkeypatch.setenv("OWNERSHIP_FGA_CONFIG_PROVIDER", "irrelevant:patched-above")

    seen_model_ids: list[str | None] = []

    def post(url, json=None, headers=None, timeout=None):
        seen_model_ids.append(json.get("authorization_model_id"))
        return _resp(401, {"code": "unauthenticated"})

    with mock.patch.object(fga.requests, "post", side_effect=post):
        fga._request("POST", "/check", json={"tuple_key": {}}, what="check", model=True)

    assert seen_model_ids == ["MODEL1", "MODEL2"]


def test_check_and_write_thread_the_model_through_request_rather_than_pre_merging(
    monkeypatch,
):
    """`check`/`list_objects`/`write`/`delete_each` no longer build
    `conn.model()` into their own `json` before calling `_request` -- they
    pass `model=True` and let `_request` own it (S5's fix, applied at every
    call site named in the finding)."""
    _token_env_connection(monkeypatch, "tok-1")
    monkeypatch.setenv("OWNERSHIP_FGA_MODEL", "PINNED1")

    with mock.patch.object(
        fga.requests, "post", return_value=_resp(200, {"allowed": True})
    ) as post:
        fga.check(BEN, "viewer", OBJ)
    assert post.call_args.kwargs["json"]["authorization_model_id"] == "PINNED1"

    with mock.patch.object(fga.requests, "post", return_value=_resp(200)) as post:
        fga.write_tuple(BEN, "viewer", OBJ)
    assert post.call_args.kwargs["json"]["authorization_model_id"] == "PINNED1"

    with mock.patch.object(fga.requests, "post", return_value=_resp(200)) as post:
        fga.delete_each([{"user": BEN, "relation": "viewer", "object": OBJ}])
    assert post.call_args.kwargs["json"]["authorization_model_id"] == "PINNED1"


# ----------------------------------------------------------------------------- R2-1


def test_401_retry_through_the_lazy_registry_path_reads_a_rotated_token(monkeypatch):
    """R2-1(b), end to end: a local-only registry (S2/R2-1(a) still skip
    building a connection at load() time for the built-in Local*
    authorizer+directory) whose FIRST `get_fga_connection()` call cached
    both the provider and the connection on the registry -- the 401 retry
    must rebuild through that SAME cached provider, so
    OWNERSHIP_FGA_CREDENTIALS' token_env rotation is picked up exactly as
    it already was on the eager (registry-built) path (`['Bearer t1',
    'Bearer t2']`, not `['Bearer t1', 'Bearer t1']`)."""
    pytest.importorskip("flask")
    from flask import Flask

    for key in list(os.environ):
        if key.startswith("OWNERSHIP_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OWNERSHIP_TOKEN_FOR_TEST", "tok-1")

    app = Flask("r2-1-lazy-rotation-test")
    app.config["OWNERSHIP_FGA_API_URL"] = "http://store.example"
    app.config["OWNERSHIP_FGA_STORE"] = "S1"
    app.config["OWNERSHIP_FGA_CREDENTIALS"] = {
        "type": "api_token",
        "token_env": "OWNERSHIP_TOKEN_FOR_TEST",
    }
    reg = plugins.load(app, strict=True)
    assert reg.connection is None  # local-only: still skipped at load() time

    calls: list[dict] = []

    def post(url, json=None, headers=None, timeout=None):
        calls.append(dict(headers))
        if len(calls) == 1:
            monkeypatch.setenv("OWNERSHIP_TOKEN_FOR_TEST", "tok-2")
        return _resp(401, {"code": "unauthenticated"})

    with app.app_context():
        with mock.patch.object(fga.requests, "post", side_effect=post):
            fga._request("POST", "/check", json={}, what="check")

    assert [c["Authorization"] for c in calls] == ["Bearer tok-1", "Bearer tok-2"]
    assert reg.provider is not None, "the lazy build must have cached a provider"


# ---------------------------- cross-lane: fga.write/write_tuple/delete_each connection=


def test_write_write_tuple_delete_each_accept_a_pinned_connection(monkeypatch):
    """Cross-lane (part 3 worked around the absence of this by calling
    `_request` directly): `write`/`write_tuple`/`delete_each` need a
    `connection=` override the same way `get_model`/`list_models`/
    `write_model` already have, for a caller repairing a specific store
    rather than the ambient one."""
    pinned = fga_connection.FgaConnection(
        api_url="http://pinned.example",
        store_id="PIN",
        model_id="M1",
        headers={"Authorization": "Bearer pinned"},
        timeout_s=1.0,
    )

    with mock.patch.object(fga.requests, "post", return_value=_resp(200)) as post:
        assert fga.write_tuple(BEN, "viewer", OBJ, connection=pinned) is True
    assert post.call_args.args[0] == "http://pinned.example/stores/PIN/write"
    assert post.call_args.kwargs["headers"] == {"Authorization": "Bearer pinned"}

    with mock.patch.object(fga.requests, "post", return_value=_resp(200)) as post:
        assert fga.delete_tuple(BEN, "viewer", OBJ, connection=pinned) is True
    assert post.call_args.args[0] == "http://pinned.example/stores/PIN/write"

    with mock.patch.object(fga.requests, "post", return_value=_resp(200)) as post:
        result = fga.delete_each(
            [{"user": BEN, "relation": "viewer", "object": OBJ}], connection=pinned
        )
    assert result == {"deleted": 1, "already_absent": 0, "failed": 0}
    assert post.call_args.args[0] == "http://pinned.example/stores/PIN/write"


def test_write_connection_kwarg_skips_the_401_retry_like_get_model_does(monkeypatch):
    """A pinned connection is not the ambient one that refreshing would
    replace -- `write(connection=...)` must skip the 401 refresh-and-retry
    exactly as `_request`'s own `connection` override already does."""
    pinned = fga_connection.FgaConnection(
        api_url="http://pinned.example",
        store_id="PIN",
        model_id=None,
        headers={},
        timeout_s=1.0,
    )
    with mock.patch.object(
        fga.requests, "post", return_value=_resp(401, {"code": "unauthenticated"})
    ) as post:
        assert fga.write_tuple(BEN, "viewer", OBJ, connection=pinned) is False
    assert post.call_count == 1


# ---------------------------------------------------- B2: the `fga` CLI group, degraded


@pytest.fixture
def cli_flask_app():
    pytest.importorskip("flask")
    from flask import Flask

    return Flask("cli-fga-degraded-test")


def _degrade_the_connection_seam(app, monkeypatch):
    """A registry whose connection seam specifically failed: OpenFGA
    authorizer (so a connection is even attempted, S2), malformed
    credentials, environment clean so nothing silently answers instead."""
    for key in list(os.environ):
        if key.startswith("OWNERSHIP_"):
            monkeypatch.delenv(key, raising=False)
    app.config["OWNERSHIP_AUTHORIZER"] = "openfga"
    app.config["OWNERSHIP_FGA_CREDENTIALS"] = '{"type": "bogus"}'
    from superset_ownership import plugins

    return plugins.load(app, strict=False)


def test_fga_status_reports_degraded_and_exits_2(cli_flask_app, monkeypatch):
    from click.testing import CliRunner
    from superset_ownership import cli_fga

    _degrade_the_connection_seam(cli_flask_app, monkeypatch)
    with cli_flask_app.app_context():
        result = CliRunner().invoke(cli_fga.fga_status, [])
    assert result.exit_code == 2
    assert "cannot continue" in result.stderr
    assert "connection seam degraded" in result.stderr


def test_fga_reconnect_reports_degraded_and_exits_2(cli_flask_app, monkeypatch):
    from click.testing import CliRunner
    from superset_ownership import cli_fga

    _degrade_the_connection_seam(cli_flask_app, monkeypatch)
    with cli_flask_app.app_context():
        result = CliRunner().invoke(cli_fga.fga_reconnect, [])
    assert result.exit_code == 2
    assert "cannot continue" in result.stderr


def test_fga_show_model_reports_degraded_and_exits_2(cli_flask_app, monkeypatch):
    from click.testing import CliRunner
    from superset_ownership import cli_fga

    _degrade_the_connection_seam(cli_flask_app, monkeypatch)
    with cli_flask_app.app_context():
        result = CliRunner().invoke(cli_fga.fga_show_model, [])
    assert result.exit_code == 2
    assert "cannot continue" in result.stderr


def test_fga_install_model_reports_degraded_and_exits_2(cli_flask_app, monkeypatch):
    from click.testing import CliRunner
    from superset_ownership import cli_fga

    _degrade_the_connection_seam(cli_flask_app, monkeypatch)
    with cli_flask_app.app_context():
        result = CliRunner().invoke(cli_fga.fga_install_model, [])
    assert result.exit_code == 2
    assert "cannot continue" in result.stderr


def test_fga_status_succeeds_with_a_clean_environment_and_no_config(monkeypatch):
    """No app context at all: the no-app fallback builds from the
    environment, same as every command did before plugins.py existed --
    still exit 0, JSON on stdout, no degraded text anywhere."""
    from click.testing import CliRunner
    from superset_ownership import cli_fga

    for key in list(os.environ):
        if key.startswith("OWNERSHIP_"):
            monkeypatch.delenv(key, raising=False)
    with mock.patch.object(
        fga.requests, "get", return_value=_resp(200, {"status": "SERVING"})
    ):
        result = CliRunner().invoke(cli_fga.fga_status, [])
    assert result.exit_code == 0
    assert "DEGRADED" not in result.output
    assert '"store": "01M1TT1CJ9PWQVF6KWBEJNSKB8"' in result.stdout


# -------------------------------------------------------- S6: install-model idempotency


def _fake_store_post_get(
    existing_model: dict | None, written_id="01NEWMODEL0000000000000000"
):
    """Minimal fake answering only what install-model/show-model touch."""
    write_calls: list[dict] = []

    def post(url, json=None, headers=None, timeout=None):
        if url.endswith("/authorization-models"):
            write_calls.append(json)
            r = requests.Response()
            r.status_code = 200
            r._content = f'{{"authorization_model_id": "{written_id}"}}'.encode()
            return r
        raise AssertionError(f"unexpected POST {url}")

    def get(url, json=None, headers=None, timeout=None):
        import json as _json  # noqa: TID251 - see the module-level import above

        r = requests.Response()
        if url.endswith("/authorization-models") and existing_model is not None:
            r.status_code = 200
            r._content = _json.dumps(
                {"authorization_models": [{**existing_model, "id": "EXISTING1"}]}
            ).encode()
        else:
            r.status_code = 200
            r._content = b'{"authorization_models": []}'
        return r

    return post, get, write_calls


def test_install_model_short_circuits_when_the_stores_latest_model_matches(monkeypatch):
    from click.testing import CliRunner
    from superset_ownership import cli_fga
    from superset_ownership.model import MODEL

    for key in list(os.environ):
        if key.startswith("OWNERSHIP_"):
            monkeypatch.delenv(key, raising=False)
    post, get, write_calls = _fake_store_post_get(existing_model=MODEL)
    monkeypatch.setattr(fga.requests, "post", post)
    monkeypatch.setattr(fga.requests, "get", get)

    result = CliRunner().invoke(cli_fga.fga_install_model, [])
    assert result.exit_code == 0
    assert write_calls == [], "an unchanged model must not be written again"
    assert '"installed": false' in result.stdout
    assert '"model_id": "EXISTING1"' in result.stdout
    assert "already current" in result.stderr


def test_install_model_writes_when_the_store_is_empty(monkeypatch):
    from click.testing import CliRunner
    from superset_ownership import cli_fga

    for key in list(os.environ):
        if key.startswith("OWNERSHIP_"):
            monkeypatch.delenv(key, raising=False)
    post, get, write_calls = _fake_store_post_get(existing_model=None)
    monkeypatch.setattr(fga.requests, "post", post)
    monkeypatch.setattr(fga.requests, "get", get)

    result = CliRunner().invoke(cli_fga.fga_install_model, [])
    assert result.exit_code == 0
    assert len(write_calls) == 1
    assert '"installed": true' in result.stdout


def test_install_model_writes_when_the_store_has_a_different_model(monkeypatch):
    from click.testing import CliRunner
    from superset_ownership import cli_fga

    for key in list(os.environ):
        if key.startswith("OWNERSHIP_"):
            monkeypatch.delenv(key, raising=False)
    stale = {"schema_version": "1.1", "type_definitions": [{"type": "user"}]}
    post, get, write_calls = _fake_store_post_get(existing_model=stale)
    monkeypatch.setattr(fga.requests, "post", post)
    monkeypatch.setattr(fga.requests, "get", get)

    result = CliRunner().invoke(cli_fga.fga_install_model, [])
    assert result.exit_code == 0
    assert len(write_calls) == 1
    assert '"installed": true' in result.stdout


# -------------------------------------------------- N8: show-model --check output split


def test_show_model_check_puts_the_dsl_on_stdout_and_the_verdict_on_stderr(monkeypatch):
    from click.testing import CliRunner
    from superset_ownership import cli_fga
    from superset_ownership.model import MODEL

    for key in list(os.environ):
        if key.startswith("OWNERSHIP_"):
            monkeypatch.delenv(key, raising=False)
    post, get, _ = _fake_store_post_get(existing_model=MODEL)
    monkeypatch.setattr(fga.requests, "post", post)
    monkeypatch.setattr(fga.requests, "get", get)

    result = CliRunner().invoke(cli_fga.fga_show_model, ["--check"])
    assert result.exit_code == 0
    assert "type dashboard" in result.stdout
    assert '"missing"' not in result.stdout, (
        "the verdict must not land on stdout by default"
    )
    assert '"missing": []' in result.stderr


def test_show_model_check_json_puts_the_verdict_on_stdout_with_no_dsl(monkeypatch):
    from click.testing import CliRunner
    from superset_ownership import cli_fga
    from superset_ownership.model import MODEL

    for key in list(os.environ):
        if key.startswith("OWNERSHIP_"):
            monkeypatch.delenv(key, raising=False)
    post, get, _ = _fake_store_post_get(existing_model=MODEL)
    monkeypatch.setattr(fga.requests, "post", post)
    monkeypatch.setattr(fga.requests, "get", get)

    result = CliRunner().invoke(cli_fga.fga_show_model, ["--check", "--json"])
    assert result.exit_code == 0
    assert "type dashboard" not in result.stdout, "--json means JSON only, no DSL"
    assert '"missing": []' in result.stdout


def test_show_model_without_check_is_unaffected_by_the_json_flag_split(monkeypatch):
    from click.testing import CliRunner
    from superset_ownership import cli_fga
    from superset_ownership.model import MODEL

    for key in list(os.environ):
        if key.startswith("OWNERSHIP_"):
            monkeypatch.delenv(key, raising=False)
    post, get, _ = _fake_store_post_get(existing_model=MODEL)
    monkeypatch.setattr(fga.requests, "post", post)
    monkeypatch.setattr(fga.requests, "get", get)

    result = CliRunner().invoke(cli_fga.fga_show_model, [])
    assert result.exit_code == 0
    assert "type dashboard" in result.stdout

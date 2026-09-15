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
"""Minimal OpenFGA client for the ownership prototype.

Plain `requests` calls against the OpenFGA HTTP API. Short timeout, fails
CLOSED: any network/HTTP error is logged and treated as "no access" / "no
objects" rather than raised, so a flaky OpenFGA never turns into a 500 for
the end user -- it turns into a (safe) deny. Callers that must not mistake
"could not read" for "nothing there" pass ``strict=True`` and get one of
:class:`StoreUnavailable` / :class:`StoreRejected` instead.

Every HTTP call threads a :class:`~superset_ownership.fga_connection.FgaConnection`
through the single :func:`_request` (spec §5.3) instead of reading module
globals: `api_url`, `store_id`, `model_id` and auth headers are resolved at
CALL time, not import time. That is what closes PCS-10243 #88 -- a
`superset ownership db upgrade` or a bare migration no longer needs config
this module used to read on import. ``FGA_API_URL`` / ``FGA_STORE_ID`` /
``FGA_MODEL_ID`` / ``TIMEOUT`` stay readable as module attributes for any
existing caller through a PEP 562 ``__getattr__`` shim; they are resolved
from the current connection on every read and are no longer assignable.

Ownership note (PCS-10243 §3-§4): connection resolution and the 401 cooldown
proper belong to ``superset_ownership.plugins`` (``get_fga_connection()``,
``refresh_fga_connection()``, ``may_refresh()``); this file imports it
lazily -- inside the functions below, never at module scope -- solely to
avoid a module-level import cycle (``plugins`` reads this module's sibling
``authz``/``directory`` at load time), not because ``plugins`` might be
absent.
"""

from __future__ import annotations

import logging
import time
import warnings
from typing import Any

import requests

from superset_ownership.fga_connection import FgaConnection

logger = logging.getLogger(__name__)

# How long a 401-triggered refresh is trusted before another one is allowed,
# per process. Bounds a credential outage to one extra request pair (check ->
# 401 -> refresh -> retry -> 401) every REFRESH_COOLDOWN_S, rather than one
# extra pair per call (spec §5.3, the "401-refresh amplification" review
# finding).
REFRESH_COOLDOWN_S = 30.0

_GLOBAL_SHIM = {
    "FGA_API_URL": "api_url",
    "FGA_STORE_ID": "store_id",
    "FGA_MODEL_ID": "model_id",
    "TIMEOUT": "timeout_s",
}


def __getattr__(name: str) -> Any:
    """PEP 562: ``fga.FGA_API_URL`` and friends resolve from the current
    connection at READ time (spec §5.4). Not assignable -- there is no
    module-level variable behind these names to assign to."""
    attr = _GLOBAL_SHIM.get(name)
    if attr is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(_conn(), attr)


class StoreError(RuntimeError):
    """Base for the two ways a strict call can fail. Never raised itself."""


class StoreUnavailable(StoreError):  # noqa: N818 - widely imported by this name; an
    # -Error rename would ripple into authz.py/directory.py/service.py/hooks.py/api.py,
    # outside this slice's scope (see the PR-part boundaries in the review).
    """The store could not give an answer: connection refused, timeout, DNS,
    or a 5xx (a load balancer answering for a restarting pod, a proxy's
    error page). Worth retrying; trips the outbox's circuit breaker.
    """


class StoreRejected(StoreError):  # noqa: N818 - see StoreUnavailable, same reason
    """The store answered and refused: a 4xx from model validation, a stale
    model pin, a bad token, or a body that was not the JSON it promised.
    Retrying will not change the answer; the outbox parks the row for an
    operator.
    """


_TRANSPORT_ERRORS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)


def _classify(what: str, exc: BaseException) -> StoreError:
    """Transport failure or 5xx -> StoreUnavailable; anything else the store
    said (4xx, malformed body) -> StoreRejected."""
    if isinstance(exc, _TRANSPORT_ERRORS):
        return StoreUnavailable(f"{what}: {exc.__class__.__name__}")
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        code = exc.response.status_code
        text = exc.response.text[:200]
        if code >= 500:
            return StoreUnavailable(f"{what}: HTTP {code} {text}")
        return StoreRejected(f"{what}: HTTP {code} {text}")
    return StoreRejected(f"{what}: {exc.__class__.__name__}: {exc}")


def _classify_response(what: str, resp: requests.Response) -> StoreError:
    """Same split for a response that was received but is not a success."""
    if resp.status_code >= 500:
        return StoreUnavailable(f"{what}: HTTP {resp.status_code} {resp.text[:200]}")
    return StoreRejected(f"{what}: HTTP {resp.status_code} {resp.text[:200]}")


def _conn() -> FgaConnection:
    """The connection every request threads through: the shared registry's
    (``superset_ownership.plugins.get_fga_connection()``). Propagates
    :class:`~superset_ownership.plugins.PluginError` when the connection
    seam could not be built -- callers that must not surface that as a raw
    traceback (the ``fga`` CLI commands) catch it explicitly."""
    from superset_ownership import plugins

    return plugins.get_fga_connection()


def refresh_connection() -> FgaConnection:
    """Rebuild the connection now, ignoring the cooldown -- the operator
    path (``fga reconnect``) and what a 401's own retry calls once
    ``_may_refresh`` allows it. ``plugins.refresh_fga_connection()`` stamps
    the cooldown timestamp this function used to keep a second, process-
    local copy of; one timestamp, one place."""
    from superset_ownership import plugins

    return plugins.refresh_fga_connection()


def _may_refresh(now: float) -> bool:
    from superset_ownership import plugins

    return bool(plugins.may_refresh(now=now, cooldown=REFRESH_COOLDOWN_S))


def _request(
    method: str,
    path: str,
    *,
    json: Any = None,
    timeout: float | None = None,
    what: str,
    absolute: bool = False,
    connection: FgaConnection | None = None,
    model: bool = False,
) -> requests.Response:
    """The ONE place that talks HTTP.

    Injects ``conn.headers`` and uses ``conn.timeout_s`` unless ``timeout``
    is given. On a 401: if the last refresh is older than
    ``REFRESH_COOLDOWN_S``, refresh once (a WARNING naming whether the
    refreshed connection differs from the old one) and retry once;
    otherwise fail fast, no retry. A 401 surviving the retry -- or hit
    inside the cooldown -- is left for the caller to classify exactly as it
    classifies any other rejection (:class:`StoreRejected` for a strict
    caller, the usual fail-closed default for the rest).

    ``requests`` stays a module attribute and this function looks up
    ``requests.post``/``requests.get`` at call time, so
    ``mock.patch.object(fga.requests, "post", ...)`` in the tests reaches
    every call site unchanged.

    Pass ``connection`` to pin a specific connection for this one call (the
    ``fga install-model``/``show-model`` override flags) -- doing so also
    skips the 401 refresh-and-retry, since a caller-supplied connection is
    not the ambient one that refreshing would replace.

    ``model=True`` (S5) has this function -- not the caller -- merge
    ``conn.model()`` (the ``authorization_model_id`` field, when pinned)
    into ``json`` on EVERY attempt, including the retry. A caller that
    pre-merges ``**conn.model()`` into its own ``json`` before calling this
    function bakes in the PRE-refresh connection's pin; with a provider
    that rotates the pin along with the token (the kind
    ``OWNERSHIP_FGA_CONFIG_PROVIDER`` exists for), the retry would then go
    to the refreshed connection's store/URL carrying the stale model id.
    """
    conn = connection if connection is not None else _conn()
    body = {**json, **conn.model()} if model and json is not None else json
    url = path if absolute else conn.url(path)
    send = requests.post if method == "POST" else requests.get
    resp = send(
        url, json=body, headers=dict(conn.headers), timeout=timeout or conn.timeout_s
    )
    if connection is not None or resp.status_code != 401:
        return resp
    if not _may_refresh(time.monotonic()):
        logger.info(
            "openfga %s: HTTP 401 within the %.0fs cooldown, failing fast",
            what,
            REFRESH_COOLDOWN_S,
        )
        return resp
    old_headers = dict(conn.headers)
    conn = refresh_connection()
    if dict(conn.headers) != old_headers:
        logger.warning("openfga %s: HTTP 401, refreshed: credentials changed", what)
    else:
        logger.warning(
            "openfga %s: HTTP 401, refreshed: unchanged -- check "
            "OWNERSHIP_FGA_CREDENTIALS",
            what,
        )
    body = {**json, **conn.model()} if model and json is not None else json
    url = path if absolute else conn.url(path)
    return send(
        url, json=body, headers=dict(conn.headers), timeout=timeout or conn.timeout_s
    )


def reachable(timeout: float = 1.0) -> bool:
    """Is the store serving right now? Probes OpenFGA's root ``/healthz``
    (it is not per-store) and requires a 200. Used for operator-facing
    messages -- "cannot verify" versus "no" -- never to classify a failed
    write after the fact; the write's own response does that."""
    try:
        conn = _conn()
        resp = _request(
            "GET",
            f"{conn.api_url}/healthz",
            absolute=True,
            what="reachable",
            timeout=timeout,
        )
        return resp.status_code == 200
    except Exception:  # noqa: BLE001 - not answering is the answer
        return False


def check(user: str, relation: str, obj: str) -> bool:
    """user, obj like 'user:3', 'dashboard:<uuid>'."""
    try:
        resp = _request(
            "POST",
            "/check",
            json={"tuple_key": {"user": user, "relation": relation, "object": obj}},
            what="check",
            model=True,
        )
        resp.raise_for_status()
        return bool(resp.json().get("allowed", False))
    except Exception:
        logger.exception(
            "openfga check failed user=%s relation=%s obj=%s", user, relation, obj
        )
        return False


def list_objects(
    user: str, relation: str, object_type: str, *, strict: bool = False
) -> list[str]:
    """Which objects of `object_type` does `user` hold `relation` on.

    Default: `[]` on any failure -- fail-closed, like every other access
    read. With ``strict=True`` a failure raises :class:`StoreUnavailable`
    (transport, 5xx) or :class:`StoreRejected` (a 4xx or a body that is not
    the JSON it promised) instead, the same additive contract
    :func:`write` and :func:`read_page` already carry -- so a caller
    deciding whether an empty answer is real (M-1, `service.
    cached_list_objects`) does not mistake "could not read" for "nothing
    shared" and publish the difference to a cache.
    """
    try:
        resp = _request(
            "POST",
            "/list-objects",
            json={"user": user, "relation": relation, "type": object_type},
            what="list_objects",
            model=True,
        )
        resp.raise_for_status()
        return list(resp.json().get("objects", []))
    except Exception as exc:  # noqa: BLE001 - classified below
        if strict:
            raise _classify("list_objects", exc) from exc
        logger.exception(
            "openfga list_objects failed user=%s relation=%s type=%s",
            user,
            relation,
            object_type,
        )
        return []


# OpenFGA reports both "the tuple is already there" and "the tuple is not
# there" under one code, distinguished by the message. Everything else under
# that code -- an unknown relation, an unknown type, a malformed user -- is a
# real rejection.
_IDEMPOTENT_PHRASES = (
    "cannot write a tuple which already exists",
    "cannot delete a tuple which does not exist",
)


def _is_already_satisfied(resp: requests.Response) -> bool:
    """Did this rejection mean the store already holds the state we asked for?"""
    try:
        payload = resp.json()
    except Exception:
        return False
    if payload.get("code") != "write_failed_due_to_invalid_input":
        return False
    message = str(payload.get("message", "")).lower()
    return any(phrase in message for phrase in _IDEMPOTENT_PHRASES)


def write(
    writes: list[dict[str, str]] | None = None,
    deletes: list[dict[str, str]] | None = None,
    *,
    strict: bool = False,
    connection: FgaConnection | None = None,
) -> bool:
    """writes/deletes are lists of {"user":..,"relation":..,"object":..}.

    Default: False on any failure (the inline callers' contract). With
    ``strict=True`` a failure raises :class:`StoreUnavailable` (transport,
    5xx) or :class:`StoreRejected` (4xx that is not an idempotent no-op), so
    the outbox drain can tell "retry" from "park" without guessing.

    Pass ``connection`` to pin a specific connection for this one write --
    the same override the model commands use (``get_model``/``list_models``/
    ``write_model``), so a caller resolving its own connection (a sibling
    slice's own store-repair path, for one) is not forced to bypass this
    function and call ``_request`` directly to get that.
    """
    body: dict[str, Any] = {}
    if writes:
        body["writes"] = {"tuple_keys": writes}
    if deletes:
        body["deletes"] = {"tuple_keys": deletes}
    if not writes and not deletes:
        return True
    try:
        resp = _request(
            "POST", "/write", json=body, what="write", model=True, connection=connection
        )
    except Exception as exc:  # noqa: BLE001 - classified below
        if strict:
            raise _classify("write", exc) from exc
        logger.exception("openfga write failed writes=%s deletes=%s", writes, deletes)
        return False
    if resp.status_code == 200:
        return True
    # Two rejections mean the store already holds the state asked for, and
    # both have to read as success or every retry of an idempotent
    # operation reports a failure that is not one: writing a tuple that
    # exists, and deleting one that does not.
    #
    # Judged on OpenFGA's own error code, never on the message text. A
    # substring test for "not found" also matched `relation
    # 'dashboard#veiwer' not found` and `type 'widget' not found` -- real
    # model-validation rejections, reported to the caller as success. The
    # allowlist on the route was the only thing standing between that and
    # the exact silent-success bug this handler exists to prevent.
    if _is_already_satisfied(resp):
        logger.info(
            "openfga write already satisfied writes=%s deletes=%s", writes, deletes
        )
        return True
    if strict:
        raise _classify_response("write", resp)
    logger.warning(
        "openfga write rejected writes=%s deletes=%s: %s",
        writes,
        deletes,
        resp.text[:300],
    )
    return False


def write_tuple(
    user: str,
    relation: str,
    obj: str,
    *,
    strict: bool = False,
    connection: FgaConnection | None = None,
) -> bool:
    return write(
        writes=[{"user": user, "relation": relation, "object": obj}],
        strict=strict,
        connection=connection,
    )


def delete_tuple(
    user: str,
    relation: str,
    obj: str,
    *,
    strict: bool = False,
    connection: FgaConnection | None = None,
) -> bool:
    return write(
        deletes=[{"user": user, "relation": relation, "object": obj}],
        strict=strict,
        connection=connection,
    )


def read_object(obj: str, relation: str | None = None) -> list[dict]:
    """Tuples on one object. Paginated -- see read_all."""
    return read_all(obj, relation)


def read_page(
    tuple_key: dict[str, Any],
    *,
    page_size: int = 100,
    token: str = "",
    strict: bool = False,
) -> tuple[list[dict], str]:
    """One page of a paginated Read: an arbitrary ``tuple_key`` (any mix of
    ``user``/``relation``/``object``), the continuation token to resume
    from, and whether to raise on failure.

    Returns ``(tuples, next_token)``; ``next_token == ""`` means the last
    page was read. The generic building block behind :func:`read_all`,
    :func:`tenant_objects`, and (once it lands) ``OpenFGADirectory``.
    """
    body: dict[str, Any] = {"page_size": page_size, "tuple_key": tuple_key}
    if token:
        body["continuation_token"] = token
    try:
        resp = _request("POST", "/read", json=body, what="read_page")
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 - classified below
        if strict:
            raise _classify("read_page", exc) from exc
        logger.exception("openfga read_page failed tuple_key=%s", tuple_key)
        return [], ""
    return [t["key"] for t in data.get("tuples", [])], data.get(
        "continuation_token"
    ) or ""


def tenant_members(tenant_guid: str, *, strict: bool = False) -> list[str]:
    """Deprecated: moved to :func:`superset_ownership.directory.tenant_members`
    (PCS-10243 #91, residue 1). This is a thin re-export kept for one
    release for any caller still importing it from here -- there is no
    group/tenant-membership logic left in this module. Import from
    ``directory`` directly instead.

    M3 (review round 1, PR #101): warns on every call now -- the earlier
    version of this docstring said "kept for one release" with no mechanism
    behind that claim; this is the mechanism."""
    warnings.warn(
        "superset_ownership.fga.tenant_members is deprecated; import "
        "superset_ownership.directory.tenant_members instead",
        DeprecationWarning,
        stacklevel=2,
    )
    from superset_ownership import directory

    return directory.tenant_members(tenant_guid, strict=strict)


def object_tenant(
    asset_type: str, object_uuid: str, *, strict: bool = False
) -> str | None:
    """The tenant an object belongs to, from its `tenant` relation.

    Non-strict by default, like every access read: a store that cannot be
    read answers "no tenant", which the read gate treats as it treats any
    other failure. The write routes that decide whether to STAMP a tenant
    pass ``strict=True``, because for them "no tenant" selects the stamp
    branch and "could not read" must not.
    """
    for t in read_all(f"{asset_type}:{object_uuid}", "tenant", strict=strict):
        user = t.get("user", "")
        if user.startswith("tenant:"):
            return user.split(":", 1)[1].split("#", 1)[0]
    return None


def relations_of(subject: str, obj: str) -> list[str]:
    """Which relations this subject holds on this object, per the store.

    Needed to revoke a grant the local mirror has lost. The store is
    authoritative for shares, so "the mirror has no row" is not the same as
    "there is nothing to revoke" -- and treating them as the same made a live
    grant un-revokable: the API answered 404 while the tuple kept working.
    """
    return sorted(
        {
            t["relation"]
            for t in read_all(obj)
            if t.get("user") == subject and t.get("relation") not in ("owner", "tenant")
        }
    )


def group_exists(group_name: str) -> bool:
    """Deprecated: moved to :func:`superset_ownership.directory.group_exists`
    (PCS-10243 #91, residue 1). Thin re-export, see :func:`tenant_members`."""
    warnings.warn(
        "superset_ownership.fga.group_exists is deprecated; import "
        "superset_ownership.directory.group_exists instead",
        DeprecationWarning,
        stacklevel=2,
    )
    from superset_ownership import directory

    return directory.group_exists(group_name)


def tenant_objects(tenant_guid: str, asset_type: str) -> list[str]:
    """Every object uuid in a tenant, in ONE call.

    Read rejects a bare object type on its own, but accepts one when the user
    is given -- so filtering on the tenant's own subject returns the whole
    set without a call per object. That is the difference between a list page
    costing one request and costing one per row.

    Paginated: follows the continuation token, so a tenant larger than one
    page is not silently truncated.
    """
    out: list[str] = []
    token = ""
    while True:
        key = {
            "user": f"tenant:{tenant_guid}#member",
            "relation": "tenant",
            "object": f"{asset_type}:",
        }
        items, token = read_page(key, token=token, strict=False)
        for t in items:
            obj = t.get("object", "")
            if ":" in obj:
                out.append(obj.split(":", 1)[1])
        if not token:
            return out


def set_object_tenant(
    asset_type: str, object_uuid: str, tenant_guid: str, *, strict: bool = False
) -> bool:
    """Record which tenant an object belongs to.

    Without this tuple an object has no tenant, and a tenant administrator
    -- whose authority is scoped to their own tenant -- can act on nothing.
    Written when the object is created, so the association is a fact in the
    authorization store rather than something inferred from the owner's
    roles at read time.

    Returns False if the write did not land, so the caller can decide
    whether to surface it. Idempotent: re-writing an existing tuple is
    treated as success.
    """
    if not (object_uuid and tenant_guid):
        if strict:
            raise StoreRejected(
                f"set_object_tenant: missing object ({object_uuid!r}) or "
                f"tenant ({tenant_guid!r})"
            )
        return False
    key = {
        "user": f"tenant:{tenant_guid}#member",
        "relation": "tenant",
        "object": f"{asset_type}:{object_uuid}",
    }
    return write(writes=[key], strict=strict)


def delete_each(
    tuples: list[dict], *, strict: bool = False, connection: FgaConnection | None = None
) -> dict:
    """Delete tuples one at a time, tolerating ones that are not there.

    OpenFGA's Write is atomic: a batch containing a single non-existent
    tuple fails entirely. Deleting a set assembled by inference -- as purge
    does -- therefore has to go one at a time, or one wrong guess discards
    the whole operation while reporting the count it asked for.

    With ``strict=True`` the first failure raises (StoreUnavailable or
    StoreRejected) and the rest are left for the retry; what was already
    deleted stays deleted and reads as "already absent" next time. Pass
    ``connection`` to pin a specific connection, as ``write`` does.
    """
    deleted, missing, failed = 0, 0, 0
    for t in tuples:
        try:
            resp = _request(
                "POST",
                "/write",
                json={"deletes": {"tuple_keys": [t]}},
                what="delete",
                model=True,
                connection=connection,
            )
        except Exception as exc:  # noqa: BLE001 - classified below
            if strict:
                raise _classify("delete", exc) from exc
            failed += 1
            logger.exception("openfga delete failed for %s", t)
            continue
        if resp.status_code == 200:
            deleted += 1
        elif _is_already_satisfied(resp):
            missing += 1
        elif strict:
            raise _classify_response(f"delete {t}", resp)
        else:
            failed += 1
            logger.warning("openfga delete rejected %s: %s", t, resp.text[:200])
    return {"deleted": deleted, "already_absent": missing, "failed": failed}


def read_all(
    obj: str, relation: str | None = None, *, strict: bool = False
) -> list[dict]:
    """Every tuple on an object, following the continuation token.

    Fail-closed by default: any error returns what was read so far (``[]`` on
    the first page), which callers deciding *access* want. Callers deciding
    whether a DELETE can be considered complete must not mistake "could not
    read" for "nothing there" -- they pass ``strict=True`` and EVERY failure
    raises: :class:`StoreUnavailable` for transport errors and 5xx,
    :class:`StoreRejected` for a 4xx or a body that is not JSON. A 503 from
    a load balancer in front of a restarting store, or a 400 from a stale
    model pin, must never read as an empty object.
    """
    out: list[dict] = []
    token = ""
    while True:
        key: dict[str, Any] = {"object": obj}
        if relation:
            key["relation"] = relation
        items, token = read_page(key, token=token, strict=strict)
        out.extend(items)
        if not token:
            return out


def get_model(
    model_id: str | None = None, *, connection: FgaConnection | None = None
) -> dict:
    """One authorization model, by id -- or the store's latest, unpinned."""
    if model_id:
        try:
            resp = _request(
                "GET",
                f"/authorization-models/{model_id}",
                what="get_model",
                connection=connection,
            )
            resp.raise_for_status()
            return dict(resp.json()["authorization_model"])
        except Exception as exc:  # noqa: BLE001 - classified below
            raise _classify("get_model", exc) from exc
    models = list_models(connection=connection)
    if not models:
        raise StoreRejected("get_model: store has no authorization models")
    return models[0]


def list_models(*, connection: FgaConnection | None = None) -> list[dict]:
    """Every authorization-model version on the store, newest first."""
    try:
        resp = _request(
            "GET", "/authorization-models", what="list_models", connection=connection
        )
        resp.raise_for_status()
        return list(resp.json().get("authorization_models", []))
    except Exception as exc:  # noqa: BLE001 - classified below
        raise _classify("list_models", exc) from exc


def write_model(model_json: dict, *, connection: FgaConnection | None = None) -> str:
    """Write ``model_json`` (``model.MODEL``, typically) as a new
    authorization-model version. Returns the new model's id."""
    try:
        resp = _request(
            "POST",
            "/authorization-models",
            json=model_json,
            what="write_model",
            connection=connection,
        )
        resp.raise_for_status()
        return str(resp.json()["authorization_model_id"])
    except Exception as exc:  # noqa: BLE001 - classified below
        raise _classify("write_model", exc) from exc

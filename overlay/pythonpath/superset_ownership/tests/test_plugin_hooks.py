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
"""Function hooks (spec `qa/design/directory-hook/01-plugin-contract.md` §4.5):
`plugin_hooks.py`'s registry/resolution/caching/fail-closed machinery, the
`HookedIdentity`/`HookedDirectory` wrappers and `plugins.get_hook`/
`describe()` in `plugins.py`, and the two call sites in `api.py`
(`is_tenant_administrator`, `OWNERSHIP_CAN_MANAGE`).

Pure (no Flask app, or a bare `Flask()` with no Superset) except the last
section, which needs the real Superset harness (`is_tenant_administrator`'s
Directory-seam routing under `plugins.counting()`, and `OWNERSHIP_CAN_MANAGE`
on the real share route) -- those tests take the `harness` fixture, which
`conftest.py` marks `superset` and skips where Superset is not importable.
"""

from __future__ import annotations

import logging
import sys
import types
from typing import Any

import pytest
from superset_ownership import plugin_hooks, plugins


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    import os

    for key in list(os.environ):
        if key.startswith("OWNERSHIP_"):
            monkeypatch.delenv(key, raising=False)
    plugin_hooks._LOGGED.clear()
    plugin_hooks._SUPPRESSED_SINCE_LOG.clear()
    plugin_hooks._FAILURE_COUNTS.clear()
    plugin_hooks._WIDENING_WARNED.clear()
    yield
    plugin_hooks._LOGGED.clear()
    plugin_hooks._SUPPRESSED_SINCE_LOG.clear()
    plugin_hooks._FAILURE_COUNTS.clear()
    plugin_hooks._WIDENING_WARNED.clear()


@pytest.fixture
def flask_app():
    pytest.importorskip("flask")
    from flask import Flask

    return Flask("plugin-hooks-test")


def _module_with(name: str, **attrs: Any) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


# --------------------------------------------------------------------------- resolution


def test_unset_hook_resolves_to_default_source():
    reg = plugin_hooks.resolve({}, strict=True)
    assert reg.callables == {}
    assert reg.sources["OWNERSHIP_MEMBER_GUID"] == "default"


def test_dotted_path_resolves_the_function():
    def tenant_guid(user):
        return "fixed"

    _module_with("hooktest_dotted", tenant_guid=tenant_guid)
    reg = plugin_hooks.resolve(
        {"OWNERSHIP_TENANT_GUID": "hooktest_dotted:tenant_guid"}, strict=True
    )
    assert reg.get("OWNERSHIP_TENANT_GUID") is tenant_guid
    assert reg.sources["OWNERSHIP_TENANT_GUID"] == "hooktest_dotted:tenant_guid"


def test_callable_set_directly_resolves_as_is():
    def is_admin(user):
        return True

    reg = plugin_hooks.resolve(
        {"OWNERSHIP_IS_TENANT_ADMINISTRATOR": is_admin}, strict=True
    )
    assert reg.get("OWNERSHIP_IS_TENANT_ADMINISTRATOR") is is_admin
    assert reg.sources["OWNERSHIP_IS_TENANT_ADMINISTRATOR"] == "<callable>"


def test_none_leaves_the_hook_unset():
    reg = plugin_hooks.resolve({"OWNERSHIP_TENANT_GUID": None}, strict=True)
    assert reg.get("OWNERSHIP_TENANT_GUID") is None
    assert reg.sources["OWNERSHIP_TENANT_GUID"] == "default"


def test_non_callable_strict_boot_fails():
    _module_with("hooktest_noncallable", not_a_function=42)
    with pytest.raises(plugins.PluginError, match="OWNERSHIP_DISPLAY_NAME"):
        plugin_hooks.resolve(
            {"OWNERSHIP_DISPLAY_NAME": "hooktest_noncallable:not_a_function"},
            strict=True,
        )


def test_non_callable_degraded_records_failure_and_leaves_default():
    _module_with("hooktest_noncallable2", not_a_function=42)
    reg = plugin_hooks.resolve(
        {"OWNERSHIP_DISPLAY_NAME": "hooktest_noncallable2:not_a_function"}, strict=False
    )
    assert "OWNERSHIP_DISPLAY_NAME" in reg.failures
    assert reg.get("OWNERSHIP_DISPLAY_NAME") is None
    assert reg.sources["OWNERSHIP_DISPLAY_NAME"] == "default (degraded)"


def test_resolve_rejects_a_bare_class_hook_without_constructing_it():
    """N-8: a hook naming a plain class (no `__call__`) must be rejected
    BEFORE it is constructed -- not silently turned into `SomeClass()` --
    a hook names a callable answer, not a plug-in to build."""
    built: list[Any] = []

    class NotAHook:
        def __init__(self):
            built.append(self)

    _module_with("hooktest_bare_class", NotAHook=NotAHook)
    with pytest.raises(plugins.PluginError, match="OWNERSHIP_DISPLAY_NAME"):
        plugin_hooks.resolve(
            {"OWNERSHIP_DISPLAY_NAME": "hooktest_bare_class:NotAHook"}, strict=True
        )
    assert built == [], "must not have been constructed"


def test_resolve_accepts_a_callable_class_hook():
    """A class whose INSTANCES are callable (defines `__call__`) is exactly
    the shape N-8 still allows: construct it, then call the instance."""

    class CallableHook:
        def __call__(self, user):
            return "from-callable-class"

    _module_with("hooktest_callable_class", CallableHook=CallableHook)
    reg = plugin_hooks.resolve(
        {"OWNERSHIP_DISPLAY_NAME": "hooktest_callable_class:CallableHook"},
        strict=True,
    )
    hook = reg.get("OWNERSHIP_DISPLAY_NAME")
    assert hook is not None
    assert hook(object()) == "from-callable-class"


def test_unimportable_dotted_path_strict_raises():
    with pytest.raises(plugins.PluginError, match="OWNERSHIP_MEMBER_GUID"):
        plugin_hooks.resolve(
            {"OWNERSHIP_MEMBER_GUID": "no.such.module:fn"}, strict=True
        )


# ------------------------------------------------------------------------ shape-pair


def test_shape_pair_one_without_the_other_strict_fails():
    def group_id(name, tenant):
        return f"{name}-{tenant}"

    with pytest.raises(plugins.PluginError, match="OWNERSHIP_SPLIT_GROUP_ID"):
        plugin_hooks.resolve({"OWNERSHIP_GROUP_ID": group_id}, strict=True)


def test_shape_pair_one_without_the_other_degraded_drops_both():
    def group_id(name, tenant):
        return f"{name}-{tenant}"

    reg = plugin_hooks.resolve({"OWNERSHIP_GROUP_ID": group_id}, strict=False)
    assert reg.get("OWNERSHIP_GROUP_ID") is None
    assert "OWNERSHIP_GROUP_ID" in reg.failures
    assert "OWNERSHIP_SPLIT_GROUP_ID" in reg.failures


def test_shape_pair_degraded_keeps_the_missing_ones_own_import_error():
    """N-7: when the missing half of the pair failed to resolve on its OWN
    (a bad dotted path), that reason must survive alongside the pairing
    message -- not be overwritten by it."""
    reg = plugin_hooks.resolve(
        {
            "OWNERSHIP_GROUP_ID": lambda name, tenant: f"{name}-{tenant}",
            "OWNERSHIP_SPLIT_GROUP_ID": "no.such.module:split",
        },
        strict=False,
    )
    assert reg.get("OWNERSHIP_GROUP_ID") is None
    assert reg.get("OWNERSHIP_SPLIT_GROUP_ID") is None
    split_failure = reg.failures["OWNERSHIP_SPLIT_GROUP_ID"]
    assert "no.such.module" in split_failure  # its own import error, kept
    assert "must both be set or neither" in split_failure  # the pairing note too


def test_shape_pair_both_set_is_fine():
    def group_id(name, tenant):
        return f"{name}-{tenant}"

    def split_group_id(group_id_or_ref):
        return None

    reg = plugin_hooks.resolve(
        {"OWNERSHIP_GROUP_ID": group_id, "OWNERSHIP_SPLIT_GROUP_ID": split_group_id},
        strict=True,
    )
    assert reg.get("OWNERSHIP_GROUP_ID") is group_id
    assert reg.get("OWNERSHIP_SPLIT_GROUP_ID") is split_group_id
    assert reg.failures == {}


# ------------------------------------------------------------------- plugins.load()


def test_load_resolves_hooks_onto_the_registry(flask_app):
    def tenant_guid(user):
        return "t"

    _module_with("hooktest_load", tenant_guid=tenant_guid)
    flask_app.config["OWNERSHIP_TENANT_GUID"] = "hooktest_load:tenant_guid"
    reg = plugins.load(flask_app, strict=True)
    assert reg.hooks.get("OWNERSHIP_TENANT_GUID") is tenant_guid


def test_load_degraded_hook_is_recorded_under_a_hook_prefixed_key(flask_app, capsys):
    _module_with("hooktest_load2", not_a_function=1)
    flask_app.config["OWNERSHIP_DISPLAY_NAME"] = "hooktest_load2:not_a_function"
    reg = plugins.load(flask_app, strict=False)
    assert "hook:OWNERSHIP_DISPLAY_NAME" in reg.failures
    err = capsys.readouterr().err
    assert "ownership: DEGRADED — hook:OWNERSHIP_DISPLAY_NAME:" in err


# --------------------------------------------------------------------------- describe()


def test_describe_lists_every_hook_unset_by_default(flask_app):
    plugins.load(flask_app, strict=True)
    with flask_app.app_context():
        d = plugins.describe()
    assert set(d["hooks"]) == {spec.setting for spec in plugin_hooks.HOOK_SPECS}
    assert all(v == {"source": "default"} for v in d["hooks"].values())


def test_describe_reports_the_dotted_path_source(flask_app):
    def tenant_guid(user):
        return "t"

    _module_with("hooktest_describe", tenant_guid=tenant_guid)
    flask_app.config["OWNERSHIP_TENANT_GUID"] = "hooktest_describe:tenant_guid"
    plugins.load(flask_app, strict=True)
    with flask_app.app_context():
        d = plugins.describe()
    assert d["hooks"]["OWNERSHIP_TENANT_GUID"] == {
        "source": "config",
        "path": "hooktest_describe:tenant_guid",
    }


def test_describe_without_a_loaded_registry_still_lists_hooks(monkeypatch):
    plugins._FALLBACK_CACHE.clear()
    monkeypatch.setenv("OWNERSHIP_AUTHORIZER", "local")
    d = plugins.describe()
    assert set(d["hooks"]) == {spec.setting for spec in plugin_hooks.HOOK_SPECS}


# --------------------------------------------------------------------------- precedence


class _StubIdentity:
    protocol_version = 1

    def member_guid(self, user):
        return "seam-guid"

    def tenant_guid(self, user):
        return "seam-tenant"

    def display_name(self, user):
        return "Seam Name"

    def user_for_member_guid(self, guid):
        return "seam-user"

    def normalize_subject(self, raw):
        return f"normalized:{raw}"


class _StubDirectory:
    protocol_version = 1
    name = "stub"

    def tenant_administrators(self, tenant):
        return ["seam-admin"]

    def user_in_group(self, member_guid, group_id):
        return False


def test_hooked_identity_falls_through_to_the_seam_when_unset():
    identity = plugins.HookedIdentity(_StubIdentity(), plugin_hooks.HookRegistry())
    assert identity.member_guid(object()) == "seam-guid"
    assert identity.tenant_guid(object()) == "seam-tenant"
    assert identity.display_name(object()) == "Seam Name"
    assert identity.user_for_member_guid("g") == "seam-user"


def test_hooked_identity_hook_wins_over_the_seam():
    hooks = plugin_hooks.HookRegistry(
        callables={"OWNERSHIP_MEMBER_GUID": lambda user: "hook-guid"}
    )
    identity = plugins.HookedIdentity(_StubIdentity(), hooks)
    assert identity.member_guid(object()) == "hook-guid"
    # Not hooked here: still the seam's own answer.
    assert identity.tenant_guid(object()) == "seam-tenant"


def test_hooked_directory_falls_through_to_the_seam_when_unset():
    directory = plugins.HookedDirectory(_StubDirectory(), plugin_hooks.HookRegistry())
    assert directory.tenant_administrators("t") == ["seam-admin"]


def test_hooked_directory_hook_wins_over_the_seam():
    admin_ref = {
        "guid": "hook-admin",
        "display_name": "Hook Admin",
        "email": None,
        "superset_id": None,
    }
    hooks = plugin_hooks.HookRegistry(
        callables={"OWNERSHIP_ADMINISTRATORS_OF_TENANT": lambda tenant: [admin_ref]}
    )
    directory = plugins.HookedDirectory(_StubDirectory(), hooks)
    assert directory.tenant_administrators("t") == [admin_ref]


def test_administrators_of_tenant_malformed_item_fails_closed_to_empty():
    """M-7: `_coerce` validates every item of a "list" hook's answer against
    `USER_REF_KEYS`, the same shape a directory-side "page" hook's items
    are held to -- a bare string (or any dict missing a required key) is
    not a valid user ref, and must fail closed to `[]` through the same
    `call()` exception path, never reach a caller (`backfill.py`) that
    indexes `u["superset_id"]` unguarded."""
    hooks = plugin_hooks.HookRegistry(
        callables={"OWNERSHIP_ADMINISTRATORS_OF_TENANT": lambda tenant: ["hook-admin"]}
    )
    directory = plugins.HookedDirectory(_StubDirectory(), hooks)
    assert directory.tenant_administrators("t") == []


def test_hooked_directory_unwrap_reports_the_raw_seam():
    directory = plugins.HookedDirectory(_StubDirectory(), plugin_hooks.HookRegistry())
    assert directory._unwrap().__class__ is _StubDirectory  # noqa: SLF001


def test_hooked_wrappers_forward_attribute_sets_to_the_wrapped_object():
    """plugin_verify.py's force_walk support reads AND assigns `directory._mode`
    -- a getattr-only proxy would silently absorb the assignment onto the
    wrapper itself instead of the real seam (see plugins.py's hook-wrapper
    section docstring)."""
    seam = _StubDirectory()
    directory = plugins.HookedDirectory(seam, plugin_hooks.HookRegistry())
    directory._mode = "always"
    assert seam._mode == "always"
    assert directory._mode == "always"  # read back through __getattr__


# ----------------------------------------------------------------------- fail-closed


def test_call_fail_closed_returns_the_spec_unknown_value(caplog):
    def raising(user):
        raise RuntimeError("connection string: postgres://secret")

    with caplog.at_level(logging.ERROR, logger="superset_ownership.plugin_hooks"):
        result = plugin_hooks.call(raising, "OWNERSHIP_TENANT_GUID", object())
    assert result is None  # the lookup kind's unknown value


def test_call_rejects_a_truthy_non_bool_answer_from_a_bool_hook():
    """N-5: `bool("no")` is `True` -- a hook that answers a non-empty string
    instead of a real `bool` must fail closed, not silently pass through as
    a truthy grant."""

    def truthy_string(user):
        return "no"

    assert (
        plugin_hooks.call(truthy_string, "OWNERSHIP_IS_TENANT_ADMINISTRATOR", object())
        is False
    )


def test_call_fail_closed_bool_hook_denies():
    def raising(user):
        raise RuntimeError("boom")

    assert (
        plugin_hooks.call(raising, "OWNERSHIP_IS_TENANT_ADMINISTRATOR", object())
        is False
    )


def test_call_logs_the_exception_class_name_never_the_message(caplog):
    def raising(user):
        raise RuntimeError("ldap bind failed password=hunter2")

    with caplog.at_level(logging.ERROR, logger="superset_ownership.plugin_hooks"):
        plugin_hooks.call(raising, "OWNERSHIP_TENANT_GUID", object())
    messages = [r.getMessage() for r in caplog.records]
    assert any("RuntimeError" in m for m in messages)
    assert not any("hunter2" in m for m in messages)
    assert not any("ldap bind failed" in m for m in messages)


def test_call_logs_once_per_hook_and_exception_class(caplog):
    def raising(user):
        raise RuntimeError("boom")

    with caplog.at_level(logging.ERROR, logger="superset_ownership.plugin_hooks"):
        plugin_hooks.call(raising, "OWNERSHIP_TENANT_GUID", object())
        plugin_hooks.call(raising, "OWNERSHIP_TENANT_GUID", object())
        plugin_hooks.call(raising, "OWNERSHIP_TENANT_GUID", object())
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1


def test_call_logs_again_once_the_ttl_window_elapses(caplog, monkeypatch):
    """M-5: `_log_once` used to mean "once per (hook, exception class),
    ever" -- an operator tailing logs after minute one of a permanently
    broken hook saw nothing further. It must re-announce once the shared
    per-user hook-cache TTL has elapsed, and say how many were suppressed
    in between."""
    import time

    def raising(user):
        raise RuntimeError("boom")

    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(plugin_hooks, "_user_cache_ttl", lambda: 10)

    with caplog.at_level(logging.ERROR, logger="superset_ownership.plugin_hooks"):
        plugin_hooks.call(raising, "OWNERSHIP_TENANT_GUID", object())
        clock[0] += 1  # still inside the TTL window
        plugin_hooks.call(raising, "OWNERSHIP_TENANT_GUID", object())
        clock[0] += 20  # past it
        plugin_hooks.call(raising, "OWNERSHIP_TENANT_GUID", object())
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 2
    assert "1 more suppressed" in error_records[1].getMessage()


def test_describe_surfaces_the_failure_count(monkeypatch):
    """M-5: `describe()`'s per-hook table shows a running failure count so
    an operator (or `plugin verify`) can tell a hook has been failing at
    all, even between rate-limited log lines."""

    def raising(user):
        raise RuntimeError("boom")

    plugin_hooks.call(raising, "OWNERSHIP_TENANT_GUID", object())
    plugin_hooks.call(raising, "OWNERSHIP_TENANT_GUID", object())

    reg = plugin_hooks.HookRegistry()
    reg.sources["OWNERSHIP_TENANT_GUID"] = "config"
    reg.callables["OWNERSHIP_TENANT_GUID"] = raising
    described = reg.describe()
    assert described["OWNERSHIP_TENANT_GUID"]["failure_count"] == 2
    # A hook that never failed carries no such key.
    assert "failure_count" not in described["OWNERSHIP_MEMBER_GUID"]


def test_call_or_default_falls_back_to_the_default_on_raise(caplog):
    def raising_group_id(name, tenant):
        raise RuntimeError("boom")

    def default_group_id(name, tenant):
        return f"{name}::{tenant}"

    with caplog.at_level(logging.ERROR, logger="superset_ownership.plugin_hooks"):
        result = plugin_hooks.call_or_default(
            raising_group_id, "OWNERSHIP_GROUP_ID", ("eng", "t1"), default_group_id
        )
    assert result == "eng::t1"


def test_call_or_default_returns_the_hook_answer_when_it_does_not_raise():
    def group_id(name, tenant):
        return f"custom-{name}-{tenant}"

    result = plugin_hooks.call_or_default(
        group_id, "OWNERSHIP_GROUP_ID", ("eng", "t1"), lambda n, t: "unused"
    )
    assert result == "custom-eng-t1"


# --------------------------------------------------------------------------- caching


class _FakeCache:
    def __init__(self):
        self.store: dict[str, Any] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, timeout=None):
        self.store[key] = value

    def delete(self, key):
        self.store.pop(key, None)


def test_call_caches_a_user_scope_hook_within_the_ttl(monkeypatch):
    calls = []

    def tenant_guid(user):
        calls.append(user)
        return "tenant-x"

    fake = _FakeCache()
    monkeypatch.setattr(plugin_hooks, "_request_local", lambda: None)
    monkeypatch.setattr(plugin_hooks, "_shared_backend", lambda: fake)
    monkeypatch.setattr(plugin_hooks, "_user_cache_ttl", lambda: 30)

    r1 = plugin_hooks.call(tenant_guid, "OWNERSHIP_TENANT_GUID", "u1", cache_key=1)
    r2 = plugin_hooks.call(tenant_guid, "OWNERSHIP_TENANT_GUID", "u1", cache_key=1)
    assert r1 == r2 == "tenant-x"
    assert len(calls) == 1


def test_call_recalls_once_the_cache_entry_is_gone(monkeypatch):
    calls = []

    def tenant_guid(user):
        calls.append(user)
        return "tenant-x"

    fake = _FakeCache()
    monkeypatch.setattr(plugin_hooks, "_request_local", lambda: None)
    monkeypatch.setattr(plugin_hooks, "_shared_backend", lambda: fake)
    monkeypatch.setattr(plugin_hooks, "_user_cache_ttl", lambda: 30)

    plugin_hooks.call(tenant_guid, "OWNERSHIP_TENANT_GUID", "u1", cache_key=1)
    fake.store.clear()  # simulates the TTL having elapsed
    plugin_hooks.call(tenant_guid, "OWNERSHIP_TENANT_GUID", "u1", cache_key=1)
    assert len(calls) == 2


def test_call_does_not_cache_when_ttl_is_zero(monkeypatch):
    calls = []

    def tenant_guid(user):
        calls.append(user)
        return "tenant-x"

    fake = _FakeCache()
    monkeypatch.setattr(plugin_hooks, "_request_local", lambda: None)
    monkeypatch.setattr(plugin_hooks, "_shared_backend", lambda: fake)
    monkeypatch.setattr(plugin_hooks, "_user_cache_ttl", lambda: 0)

    plugin_hooks.call(tenant_guid, "OWNERSHIP_TENANT_GUID", "u1", cache_key=1)
    plugin_hooks.call(tenant_guid, "OWNERSHIP_TENANT_GUID", "u1", cache_key=1)
    assert len(calls) == 2


def test_call_does_not_cache_without_a_cache_key():
    calls = []

    def tenant_guid(user):
        calls.append(user)
        return "tenant-x"

    plugin_hooks.call(tenant_guid, "OWNERSHIP_TENANT_GUID", "u1")
    plugin_hooks.call(tenant_guid, "OWNERSHIP_TENANT_GUID", "u1")
    assert len(calls) == 2


def test_directory_scope_uses_the_group_walk_ttl_setting(monkeypatch):
    calls = []

    def groups_of_tenant(tenant, query, limit, cursor):
        calls.append(tenant)
        return {"items": [], "next_cursor": None}

    fake = _FakeCache()
    monkeypatch.setenv("OWNERSHIP_DIRECTORY_GROUP_WALK_TTL", "60")
    monkeypatch.setattr(plugin_hooks, "_request_local", lambda: None)
    monkeypatch.setattr(plugin_hooks, "_shared_backend", lambda: fake)

    plugin_hooks.call(
        groups_of_tenant,
        "OWNERSHIP_GROUPS_OF_TENANT",
        "t1",
        "",
        100,
        None,
        cache_key="t1",
    )
    plugin_hooks.call(
        groups_of_tenant,
        "OWNERSHIP_GROUPS_OF_TENANT",
        "t1",
        "",
        100,
        None,
        cache_key="t1",
    )
    assert len(calls) == 1


def test_call_never_caches_a_fail_closed_answer(monkeypatch):
    """M-2: a one-off raise must not pin "unknown" for the rest of the TTL
    -- the very next call (still well within the TTL) reaches the hook
    again and gets its real answer once it stops raising."""
    calls = []

    def flaky(user):
        calls.append(user)
        if len(calls) == 1:
            raise RuntimeError("one-off store blip")
        return True

    fake = _FakeCache()
    monkeypatch.setattr(plugin_hooks, "_request_local", lambda: None)
    monkeypatch.setattr(plugin_hooks, "_shared_backend", lambda: fake)
    monkeypatch.setattr(plugin_hooks, "_user_cache_ttl", lambda: 30)

    r1 = plugin_hooks.call(
        flaky, "OWNERSHIP_IS_TENANT_ADMINISTRATOR", "u1", cache_key=1
    )
    r2 = plugin_hooks.call(
        flaky, "OWNERSHIP_IS_TENANT_ADMINISTRATOR", "u1", cache_key=1
    )
    assert r1 is False  # fail-closed
    assert r2 is True  # the hook was asked again, not served the cached False
    assert len(calls) == 2
    assert fake.store, "the successful second answer must still be cached"


def test_call_caches_a_successful_answer_after_a_prior_failure(monkeypatch):
    """The flip side of the above: once a hook DOES answer, that answer is
    cached normally -- M-2 only forbids caching the FAILURE, not every
    answer forever after one."""
    calls = []

    def flaky(user):
        calls.append(user)
        if len(calls) == 1:
            raise RuntimeError("one-off store blip")
        return True

    fake = _FakeCache()
    monkeypatch.setattr(plugin_hooks, "_request_local", lambda: None)
    monkeypatch.setattr(plugin_hooks, "_shared_backend", lambda: fake)
    monkeypatch.setattr(plugin_hooks, "_user_cache_ttl", lambda: 30)

    plugin_hooks.call(flaky, "OWNERSHIP_IS_TENANT_ADMINISTRATOR", "u1", cache_key=1)
    r2 = plugin_hooks.call(
        flaky, "OWNERSHIP_IS_TENANT_ADMINISTRATOR", "u1", cache_key=1
    )
    r3 = plugin_hooks.call(
        flaky, "OWNERSHIP_IS_TENANT_ADMINISTRATOR", "u1", cache_key=1
    )
    assert r2 is True
    assert r3 is True
    assert len(calls) == 2, "the third call must be served from the cache"


# --------------------------------------------------------------- narrow_manage_reason


def test_narrow_manage_reason_none_is_honoured():
    assert plugin_hooks.narrow_manage_reason(None, "owner") is None


def test_narrow_manage_reason_same_as_default_is_honoured():
    assert plugin_hooks.narrow_manage_reason("owner", "owner") == "owner"


def test_narrow_manage_reason_a_wider_reason_is_ignored_and_logged(caplog):
    with caplog.at_level(logging.WARNING, logger="superset_ownership.plugin_hooks"):
        result = plugin_hooks.narrow_manage_reason("admin", "owner")
    assert result == "owner"
    assert any("widening" in r.getMessage() for r in caplog.records)


def test_narrow_manage_reason_a_wider_reason_over_no_default_is_ignored():
    assert plugin_hooks.narrow_manage_reason("owner", None) is None


def test_narrow_manage_reason_widening_warns_once_per_pair_then_debug(caplog):
    """N-4: the SAME (hook_result, default_reason) widening attempt --
    exactly what a broken hook produces for every row of a list page --
    must not re-log at WARNING on every call."""
    with caplog.at_level(logging.DEBUG, logger="superset_ownership.plugin_hooks"):
        plugin_hooks.narrow_manage_reason("admin", "owner")
        plugin_hooks.narrow_manage_reason("admin", "owner")
        plugin_hooks.narrow_manage_reason("admin", "owner")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    debugs = [
        r
        for r in caplog.records
        if r.levelno == logging.DEBUG and "widening" in r.getMessage()
    ]
    assert len(warnings) == 1
    assert len(debugs) == 2
    # A DIFFERENT pair still warns -- this is per-pair, not a global switch.
    with caplog.at_level(logging.WARNING, logger="superset_ownership.plugin_hooks"):
        plugin_hooks.narrow_manage_reason("admin", "tenant_admin")
    assert any(
        r.levelno == logging.WARNING and "tenant_admin" in r.getMessage()
        for r in caplog.records
    )


# ============================================================================
# Harness section: needs the real Superset app. `harness` in the parameter
# list is what makes conftest.py mark and (where Superset is absent) skip
# these -- see pytest_collection_modifyitems.
# ============================================================================


def test_subjects_source_names_the_hook_not_the_directory_class(harness):
    """N-3: `/subjects` used to report the directory CLASS's alias even
    when a configured `OWNERSHIP_USERS_OF_TENANT`/`OWNERSHIP_GROUPS_OF_TENANT`
    hook, not that class, actually answered the rows."""
    from flask import current_app
    from superset_ownership import plugins

    tenant = "c0ffee00-0000-4000-8000-0000000000c1"
    caller = harness.add_person(
        "hooksubjectssrc1", "HookSubjectsSrc1", "Gamma", f"tenant_{tenant}"
    )

    def users_of_tenant(tenant_, query, limit, cursor):
        return {"items": [], "next_cursor": None}

    with harness.ctx():
        reg = current_app.extensions[plugins.EXT_KEY]
        reg.hooks.callables["OWNERSHIP_USERS_OF_TENANT"] = users_of_tenant
        reg.hooks.sources["OWNERSHIP_USERS_OF_TENANT"] = (
            "hook_subjects_src_module:users_of_tenant"
        )
    try:
        r = harness.get(caller, "/api/v1/ownership/subjects")
        assert r.status_code == 200, (r.status_code, r.get_json())
        assert r.get_json()["source"] == "hook:hook_subjects_src_module:users_of_tenant"
    finally:
        with harness.ctx():
            reg = current_app.extensions[plugins.EXT_KEY]
            reg.hooks.callables.pop("OWNERSHIP_USERS_OF_TENANT", None)
            reg.hooks.sources.pop("OWNERSHIP_USERS_OF_TENANT", None)


def test_split_group_id_lying_about_tenant_is_caught_on_the_share_route(harness):
    """M-6: `_validate_group_subject` used to trust a configured
    `OWNERSHIP_SPLIT_GROUP_ID` hook's own parse alone -- a hook that LIES
    (claims a foreign tenant's group belongs to the caller's tenant) made a
    cross-tenant group share succeed. The harness's fake store has no
    independent "this group's real tenant" fact to check the hook against
    (`CountingDirectory` defines no `group_tenant`, so `api.py` falls back
    to the id-format round-trip invariant: `group_id(*split_group_id(id))
    == id`), and that invariant alone must still catch this hook, because
    its lie is inconsistent with the id string itself.
    """
    from flask import current_app
    from superset_ownership import plugins

    tenant_a = "c0ffee00-0000-4000-8000-0000000000a1"
    tenant_b = "c0ffee00-0000-4000-8000-0000000000b1"
    owner = harness.add_person(
        "hooksplitowner1",
        "HookSplitOwner1",
        "Gamma",
        "sales_readers",
        f"tenant_{tenant_a}",
    )
    chart = harness.create_chart(owner, "hook-split-group-lie")

    real_b_group_id = f"eng_{tenant_b}"  # the default OWNERSHIP_GROUP_ID format

    def group_id(name, tenant):
        return f"{name}_{tenant}"

    def split_group_id(group_id_or_ref):
        # The lie: EVERY group parses as tenant_a's, regardless of what its
        # id string actually says.
        text = group_id_or_ref
        if text.startswith("group:"):
            text = text[len("group:") :].split("#", 1)[0]
        name = text.rsplit("_", 1)[0]
        return name, tenant_a

    with harness.ctx():
        reg = current_app.extensions[plugins.EXT_KEY]
        reg.hooks.callables["OWNERSHIP_GROUP_ID"] = group_id
        reg.hooks.callables["OWNERSHIP_SPLIT_GROUP_ID"] = split_group_id
        reg.hooks.sources["OWNERSHIP_GROUP_ID"] = "<test>"
        reg.hooks.sources["OWNERSHIP_SPLIT_GROUP_ID"] = "<test>"
    try:
        r = harness.post(
            owner,
            f"/api/v1/ownership/chart/{chart.id}/shares",
            {"subject": f"group:{real_b_group_id}#member", "role": "viewer"},
        )
        assert r.status_code == 400, (r.status_code, r.get_json())
        assert "tenant" in r.get_json()["message"]
    finally:
        with harness.ctx():
            reg = current_app.extensions[plugins.EXT_KEY]
            reg.hooks.callables.pop("OWNERSHIP_GROUP_ID", None)
            reg.hooks.callables.pop("OWNERSHIP_SPLIT_GROUP_ID", None)
            reg.hooks.sources.pop("OWNERSHIP_GROUP_ID", None)
            reg.hooks.sources.pop("OWNERSHIP_SPLIT_GROUP_ID", None)


def test_harness_hooks_are_all_default_regardless_of_the_deployment_config(harness):
    """M-1: the harness must be hermetic to whatever `OWNERSHIP_*` hook
    block the environment's own `superset_config`/`superset_config_docker.py`
    carries (the acceptance container's does, in full, once `install()` has
    run) -- `superset_harness.py`'s generated config now pins every
    `plugin_hooks.HOOK_SPECS` setting (and `OWNERSHIP_IDENTITY`) to its
    default right after the deployment config is star-imported, so
    `describe()["hooks"]` on the harness app must show every hook unset no
    matter what runs this suite."""
    from superset_ownership import plugins

    with harness.ctx():
        d = plugins.describe()
    assert d["hooks"], "the hooks table must not be empty"
    assert all(v == {"source": "default"} for v in d["hooks"].values()), d["hooks"]
    assert d["identity"]["source"] in ("default", None) or d["identity"][
        "class"
    ].endswith("DefaultIdentity")


def test_is_tenant_administrator_via_hook_makes_no_store_calls(harness):
    """§4.5.2: a configured OWNERSHIP_IS_TENANT_ADMINISTRATOR hook answers
    directly -- no Directory, no Authorizer call at all."""
    from superset_ownership import plugins
    from superset_ownership.api import is_tenant_administrator

    tenant = "c0ffee00-0000-4000-8000-000000000001"
    person = harness.add_person(
        "hooktenantadmin1", "HookTenantAdmin", "Gamma", f"tenant_{tenant}"
    )

    with harness.ctx():
        from flask import current_app

        user = harness._user(person)  # noqa: SLF001 - test-only reach into the harness
        reg = current_app.extensions[plugins.EXT_KEY]
        reg.hooks.callables["OWNERSHIP_IS_TENANT_ADMINISTRATOR"] = lambda u: True
        try:
            with plugins.counting() as log:
                assert is_tenant_administrator(user) is True
            assert log == []
        finally:
            reg.hooks.callables.pop("OWNERSHIP_IS_TENANT_ADMINISTRATOR", None)


def test_is_tenant_administrator_default_path_uses_the_directory_not_the_authorizer(
    harness,
):
    """§4.5.2: with no hook configured, membership is read through the
    Directory seam (`get_directory().user_in_group`) -- never the
    Authorizer directly. The harness's `counting` backend answers BOTH seams
    off one `CountingAuthorizer` (`CountingDirectory.user_in_group` delegates
    to it, D-1), so the group tuple is set on `harness.authorizer.groups`,
    keyed by the group's BARE name -- the same shape `conftest.py`'s own
    `authorizer.groups[h.GROUP] = {...}` uses."""
    from superset_ownership import plugins
    from superset_ownership.api import is_tenant_administrator
    from superset_ownership.identity import (
        resolve_member_guid,
        tenant_administrator_group,
    )

    tenant = "c0ffee00-0000-4000-8000-000000000002"
    admin_person = harness.add_person(
        "hooktenantadmin2", "HookTenantAdmin2", "Gamma", f"tenant_{tenant}"
    )
    group_bare_name = tenant_administrator_group(tenant).split(":", 1)[1]

    with harness.ctx():
        from flask import current_app

        admin_user = harness._user(admin_person)  # noqa: SLF001
        admin_guid = resolve_member_guid(admin_user)
        harness.authorizer.groups[group_bare_name] = {f"user:{admin_guid}"}
        reg = current_app.extensions[plugins.EXT_KEY]
        try:
            with plugins.counting() as log:
                result = is_tenant_administrator(admin_user)
            assert result is True
            # The counting proxy wraps BOTH the authorizer and the directory
            # seams; every recorded call here must be a Directory method,
            # never one only Authorizer answers to (check/list_objects/...).
            assert log == ["user_in_group"]
            assert "OWNERSHIP_IS_TENANT_ADMINISTRATOR" not in reg.hooks.callables
        finally:
            harness.authorizer.groups.pop(group_bare_name, None)


def test_can_manage_hook_narrows_the_owner_to_denied_on_the_share_route(harness):
    """§4.5.2/§5: OWNERSHIP_CAN_MANAGE may narrow the default -- an owner the
    default admits can still be refused by the hook."""
    from flask import current_app
    from superset_ownership import plugins

    tenant = "c0ffee00-0000-4000-8000-000000000003"
    role = f"tenant_{tenant}"
    owner = harness.add_person(
        "hookcmowner1", "HookCmOwner1", "Gamma", "sales_readers", role
    )
    target = harness.add_person("hookcmtarget1", "HookCmTarget1", "Gamma", role)
    chart = harness.create_chart(owner, "hook-can-manage-narrow")

    with harness.ctx():
        reg = current_app.extensions[plugins.EXT_KEY]
        reg.hooks.callables["OWNERSHIP_CAN_MANAGE"] = lambda user, object_state: None
    try:
        r = harness.post(
            owner,
            f"/api/v1/ownership/chart/{chart.id}/shares",
            {"subject": target.ref, "role": "viewer"},
        )
        assert r.status_code == 403, (r.status_code, r.get_json())
    finally:
        with harness.ctx():
            current_app.extensions[plugins.EXT_KEY].hooks.callables.pop(
                "OWNERSHIP_CAN_MANAGE", None
            )


def test_can_manage_hook_widening_is_ignored_and_logged_on_the_share_route(
    harness, caplog
):
    """§5's non-overridable invariant: a hook cannot GRANT a reason the
    default did not -- an outsider with no ground still gets 403, and the
    attempt is logged."""
    from flask import current_app
    from superset_ownership import plugins

    tenant = "c0ffee00-0000-4000-8000-000000000004"
    role = f"tenant_{tenant}"
    owner = harness.add_person(
        "hookcmowner2", "HookCmOwner2", "Gamma", "sales_readers", role
    )
    outsider = harness.add_person("hookcmoutsider2", "HookCmOutsider2", "Gamma", role)
    target = harness.add_person("hookcmtarget2", "HookCmTarget2", "Gamma", role)
    chart = harness.create_chart(owner, "hook-can-manage-widen")

    with harness.ctx():
        reg = current_app.extensions[plugins.EXT_KEY]
        reg.hooks.callables["OWNERSHIP_CAN_MANAGE"] = lambda user, object_state: "owner"
    try:
        with caplog.at_level(logging.WARNING, logger="superset_ownership.plugin_hooks"):
            r = harness.post(
                outsider,
                f"/api/v1/ownership/chart/{chart.id}/shares",
                {"subject": target.ref, "role": "viewer"},
            )
        assert r.status_code == 403, (r.status_code, r.get_json())
        assert any("widening" in rec.getMessage() for rec in caplog.records)
    finally:
        with harness.ctx():
            current_app.extensions[plugins.EXT_KEY].hooks.callables.pop(
                "OWNERSHIP_CAN_MANAGE", None
            )


def test_can_manage_hook_raising_denies_everyone_including_an_admin(harness, caplog):
    """M-4: a raising OWNERSHIP_CAN_MANAGE is NOT "no narrowing" in the
    sense of "the default still stands" -- `call()`'s fail-closed unknown
    is `None`, and `narrow_manage_reason(None, default_reason)` returns
    `None` unconditionally, denying EVERY caller for that object, the owner
    and a Superset admin included. Route-level proof, not just the unit
    test on `narrow_manage_reason` itself; the log carries the exception
    class only, never its message."""
    from flask import current_app
    from superset_ownership import plugins

    tenant = "c0ffee00-0000-4000-8000-000000000005"
    role = f"tenant_{tenant}"
    owner = harness.add_person(
        "hookcmraise1", "HookCmRaise1", "Gamma", "sales_readers", role
    )
    admin = harness.add_person("hookcmraise2", "HookCmRaise2", "Gamma", "Admin", role)
    target = harness.add_person("hookcmraise3", "HookCmRaise3", "Gamma", role)
    chart = harness.create_chart(owner, "hook-can-manage-raises")

    def raising_hook(user, object_state):
        raise RuntimeError("super-secret-connection-string")

    with harness.ctx():
        reg = current_app.extensions[plugins.EXT_KEY]
        reg.hooks.callables["OWNERSHIP_CAN_MANAGE"] = raising_hook
    try:
        with caplog.at_level(logging.ERROR, logger="superset_ownership.plugin_hooks"):
            r_owner = harness.post(
                owner,
                f"/api/v1/ownership/chart/{chart.id}/shares",
                {"subject": target.ref, "role": "viewer"},
            )
            r_admin = harness.post(
                admin,
                f"/api/v1/ownership/chart/{chart.id}/shares",
                {"subject": target.ref, "role": "viewer"},
            )
        assert r_owner.status_code == 403, (r_owner.status_code, r_owner.get_json())
        assert r_admin.status_code == 403, (r_admin.status_code, r_admin.get_json())
        messages = [rec.getMessage() for rec in caplog.records]
        assert any("RuntimeError" in m for m in messages)
        assert not any("super-secret-connection-string" in m for m in messages)
    finally:
        with harness.ctx():
            current_app.extensions[plugins.EXT_KEY].hooks.callables.pop(
                "OWNERSHIP_CAN_MANAGE", None
            )

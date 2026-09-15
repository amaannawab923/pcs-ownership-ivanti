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
"""The loader/registry: resolve_class_ref, load() strict vs degraded,
maintenance_invocation()'s argv table, the no-app fallback, describe() and
counting() -- plus the structural invariant that hooks.py/guard.py never
import the directory (section 7.7), which must hold before section 7 lands
too, not just after.
"""

from __future__ import annotations

import os
import sys
from typing import Protocol, runtime_checkable

import pytest
from superset_ownership import plugins


@pytest.fixture(autouse=True)
def _clean_ownership_env(monkeypatch):
    """The container this suite also runs in deploys with
    OWNERSHIP_AUTHORIZER=openfga (and friends) in the real process
    environment; every test here starts from nothing configured unless it
    sets something itself."""
    for key in list(os.environ):
        if key.startswith("OWNERSHIP_"):
            monkeypatch.delenv(key, raising=False)
    return


# ------------------------------------------------------------ fixtures: a tiny protocol


@runtime_checkable
class _Greeter(Protocol):
    protocol_version: int

    def greet(self) -> str: ...

    def wave(self) -> str: ...


class _GoodGreeter:
    protocol_version = 1

    def greet(self) -> str:
        return "hi"

    def wave(self) -> str:
        return "wave"


class _WrongVersionGreeter:
    protocol_version = 2

    def greet(self) -> str:
        return "hi"

    def wave(self) -> str:
        return "wave"


class _IncompleteGreeter:
    protocol_version = 1

    def greet(self) -> str:
        return "hi"

    # no wave()


# -------------------------------------------------------------------- resolve_class_ref


def test_resolve_class_ref_alias():
    obj = plugins.resolve_class_ref(
        "good", aliases={"good": _GoodGreeter}, protocol=_Greeter, key="X"
    )
    assert isinstance(obj, _GoodGreeter)


def test_resolve_class_ref_alias_is_a_zero_arg_factory_not_just_a_class():
    sentinel = _GoodGreeter()
    obj = plugins.resolve_class_ref(
        "good", aliases={"good": lambda: sentinel}, protocol=_Greeter, key="X"
    )
    assert obj is sentinel


def test_resolve_class_ref_dotted_colon_path():
    obj = plugins.resolve_class_ref(
        "test_plugins:_GoodGreeter", aliases={}, protocol=_Greeter, key="X"
    )
    assert isinstance(obj, _GoodGreeter)


def test_resolve_class_ref_dotted_dot_path():
    obj = plugins.resolve_class_ref(
        "test_plugins._GoodGreeter", aliases={}, protocol=_Greeter, key="X"
    )
    assert isinstance(obj, _GoodGreeter)


def test_resolve_class_ref_instance_passthrough():
    instance = _GoodGreeter()
    obj = plugins.resolve_class_ref(instance, aliases={}, protocol=_Greeter, key="X")
    assert obj is instance


def test_resolve_class_ref_unknown_alias_raises_naming_accepted():
    with pytest.raises(plugins.PluginError, match="X=.*unknown alias"):
        plugins.resolve_class_ref(
            "nope", aliases={"good": _GoodGreeter}, protocol=_Greeter, key="X"
        )


def test_resolve_class_ref_import_error_raises():
    with pytest.raises(plugins.PluginError, match="import failed"):
        plugins.resolve_class_ref(
            "no.such.module:Class", aliases={}, protocol=_Greeter, key="X"
        )


def test_resolve_class_ref_attribute_error_raises():
    with pytest.raises(plugins.PluginError, match="X="):
        plugins.resolve_class_ref(
            "test_plugins:NoSuchClass", aliases={}, protocol=_Greeter, key="X"
        )


def test_resolve_class_ref_missing_method_is_named():
    with pytest.raises(plugins.PluginError, match="missing: wave"):
        plugins.resolve_class_ref(
            _IncompleteGreeter(), aliases={}, protocol=_Greeter, key="X"
        )


def test_resolve_class_ref_protocol_version_mismatch():
    with pytest.raises(plugins.PluginError, match="protocol_version mismatch"):
        plugins.resolve_class_ref(
            _WrongVersionGreeter(),
            aliases={},
            protocol=_Greeter,
            key="OWNERSHIP_AUTHORIZER",
        )


def test_resolve_class_ref_protocol_version_check_only_applies_to_known_seams():
    # key="X" is not one of the three seams: no PROTOCOL_VERSIONS entry, so
    # a mismatching protocol_version is not even checked (this is the path
    # OWNERSHIP_AUDIT_SINK's resolution uses, where protocol is None anyway).
    obj = plugins.resolve_class_ref(
        _WrongVersionGreeter(), aliases={}, protocol=_Greeter, key="X"
    )
    assert isinstance(obj, _WrongVersionGreeter)


def test_resolve_class_ref_protocol_none_skips_isinstance_and_version_checks():
    # _WrongVersionGreeter would fail a protocol_version check under the
    # "authorizer"/"directory"/"identity" seams; with protocol=None (the
    # OWNERSHIP_AUDIT_SINK path) neither the isinstance nor the version
    # check runs at all.
    obj = plugins.resolve_class_ref(
        "test_plugins:_WrongVersionGreeter",
        aliases={},
        protocol=None,
        key="OWNERSHIP_AUDIT_SINK",
    )
    assert isinstance(obj, _WrongVersionGreeter)


def test_resolve_class_ref_none_value_raises():
    with pytest.raises(plugins.PluginError):
        plugins.resolve_class_ref(None, aliases={}, protocol=_Greeter, key="X")


# --------------------------------------------------- S1: protocol_version defaults to 1


class _NoVersionGreeter:
    """Declares every Greeter method and never sets protocol_version --
    the contract's own default-1 rule (spec §4.2) says this must satisfy
    the protocol, not be rejected for a "missing" attribute."""

    def greet(self) -> str:
        return "hi"

    def wave(self) -> str:
        return "wave"


def test_resolve_class_ref_accepts_a_class_with_no_protocol_version_attribute():
    obj = plugins.resolve_class_ref(
        _NoVersionGreeter(), aliases={}, protocol=_Greeter, key="OWNERSHIP_DIRECTORY"
    )
    assert isinstance(obj, _NoVersionGreeter)
    assert getattr(obj, "protocol_version", 1) == 1


def test_resolve_class_ref_still_names_a_missing_method_when_version_is_absent_too():
    class _Half:
        def greet(self) -> str:
            return "hi"

        # no wave(), no protocol_version

    with pytest.raises(plugins.PluginError, match="missing: wave"):
        plugins.resolve_class_ref(_Half(), aliases={}, protocol=_Greeter, key="X")


# ------------------------------------------------------- _default_directory_alias (D-4)


@pytest.mark.parametrize("authz_ref", ["local", "openfga"])
def test_default_directory_alias_follows_the_authorizer(authz_ref):
    assert plugins._default_directory_alias(authz_ref) == authz_ref


def test_default_directory_alias_raises_for_any_other_authorizer():
    with pytest.raises(
        plugins.PluginError, match="OWNERSHIP_DIRECTORY must be set explicitly"
    ):
        plugins._default_directory_alias("counting")


# ----------------------------------------------------------- load(): strict vs degraded


@pytest.fixture
def flask_app():
    pytest.importorskip("flask")
    from flask import Flask

    return Flask("ownership-plugins-test")


def test_load_empty_block_resolves_local_local_default(flask_app):
    reg = plugins.load(flask_app, strict=True)
    assert reg.authorizer.name == "local"
    assert reg.failures == {}
    assert flask_app.extensions["ownership"] is reg


def test_load_openfga_authorizer_and_nothing_else_resolves_the_store_defaults(
    flask_app,
):
    """PCS-10243 #91, residue 4: the OTHER real "empty block" -- the
    runbook's (a) means `OWNERSHIP_AUTHORIZER=openfga` and nothing else set,
    not `OWNERSHIP_AUTHORIZER` left unset entirely. The integrated review's
    I-1 fix (page-size clamp) added a harness test covering the `/subjects`
    route for exactly this configuration, but the snapshot test above (N-5)
    only ever drove `plugins.load` off a config with no `OWNERSHIP_*` keys
    at all, which resolves to `local`/`local` -- a real guard for that
    default, but not for this one. Second row, per the integrated review's
    own (non-blocking) §2 recommendation: the resolved classes AND the
    connection's `api_url`/`store_id` defaults, so a regression silently
    changing either would not need a test on this branch to catch it."""
    from superset_ownership.fga_connection import DEFAULT_API_URL, DEFAULT_STORE_ID

    flask_app.config["OWNERSHIP_AUTHORIZER"] = "openfga"
    reg = plugins.load(flask_app, strict=True)
    assert reg.authorizer.name == "openfga"
    assert reg.directory.name == "openfga"
    assert reg.failures == {}
    assert flask_app.extensions["ownership"] is reg
    assert reg.connection is not None
    assert reg.connection.api_url == DEFAULT_API_URL
    assert reg.connection.store_id == DEFAULT_STORE_ID


def test_load_strict_boot_failure_propagates_naming_key_and_class(flask_app):
    flask_app.config["OWNERSHIP_AUTHORIZER"] = "no.such.module:NopeAuthorizer"
    with pytest.raises(plugins.PluginError, match="OWNERSHIP_AUTHORIZER"):
        plugins.load(flask_app, strict=True)


def test_load_degraded_falls_back_and_records_the_failure(flask_app, capsys):
    flask_app.config["OWNERSHIP_AUTHORIZER"] = "no.such.module:NopeAuthorizer"
    reg = plugins.load(flask_app, strict=False)
    assert reg.authorizer.name == "local"  # the seam's default
    assert "authorizer" in reg.failures
    assert "OWNERSHIP_AUTHORIZER" in reg.failures["authorizer"]
    err = capsys.readouterr().err
    assert "ownership: DEGRADED — authorizer:" in err


def test_load_degraded_prints_the_failure_before_returning(flask_app, capsys):
    """The failure text must be on stderr before the caller's own output --
    simulated here by writing a marker immediately after load() returns and
    checking it comes after the DEGRADED line in capsys' buffer."""
    flask_app.config["OWNERSHIP_AUTHORIZER"] = "no.such.module:NopeAuthorizer"
    plugins.load(flask_app, strict=False)
    print("COMMAND OWN OUTPUT", file=sys.stderr)
    err = capsys.readouterr().err
    assert err.index("DEGRADED") < err.index("COMMAND OWN OUTPUT")


def test_load_degraded_only_the_failing_seam_is_recorded(flask_app):
    # OWNERSHIP_DIRECTORY set explicitly so its resolution does not
    # cascade-fail from _default_directory_alias rejecting the bad
    # authorizer string (that cascade is its own, separately tested,
    # correct behaviour -- see test_load_a_bad_authorizer_ref_cascades...).
    flask_app.config["OWNERSHIP_AUTHORIZER"] = "no.such.module:NopeAuthorizer"
    flask_app.config["OWNERSHIP_DIRECTORY"] = "local"
    reg = plugins.load(flask_app, strict=False)
    assert list(reg.failures) == ["authorizer"]


def test_load_a_bad_authorizer_ref_cascades_to_the_default_directory_alias(flask_app):
    # _default_directory_alias(authz_ref) reads the CONFIGURED string, not
    # whether the authorizer resolved -- a spec-specified consequence, not
    # a bug: an authorizer ref outside {local, openfga} demands an explicit
    # OWNERSHIP_DIRECTORY, whether or not it also happens to be broken.
    flask_app.config["OWNERSHIP_AUTHORIZER"] = "no.such.module:NopeAuthorizer"
    reg = plugins.load(flask_app, strict=False)
    assert set(reg.failures) == {"authorizer", "directory"}
    assert "OWNERSHIP_DIRECTORY must be set explicitly" in reg.failures["directory"]


def test_load_local_authorizer_openfga_directory_alias_mismatch_is_a_plugin_error(
    flask_app,
):
    # OWNERSHIP_AUTHORIZER left at "local" but OWNERSHIP_DIRECTORY points at
    # an unresolvable alias -- exercises the directory seam's own failure
    # path independently of the authorizer's.
    flask_app.config["OWNERSHIP_DIRECTORY"] = "nope"
    with pytest.raises(plugins.PluginError, match="OWNERSHIP_DIRECTORY"):
        plugins.load(flask_app, strict=True)


def test_load_records_resolved_and_logs_one_line_per_seam(flask_app, caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="superset_ownership.plugins"):
        reg = plugins.load(flask_app, strict=True)
    assert set(reg.resolved) == {"authorizer", "directory", "identity"}
    seam_lines = [r.getMessage() for r in caplog.records]
    assert any("ownership: authorizer =" in m for m in seam_lines)
    assert any("ownership: directory =" in m for m in seam_lines)
    assert any("ownership: identity =" in m for m in seam_lines)


# ------------------------------------------------------------------------------- B1


def test_load_resolves_an_instance_set_directly_in_app_config(flask_app):
    """A config-layer value that is already a constructed instance (the
    contract's third spelling) must reach resolve_class_ref as-is: the
    B1 bug was settings.get's default as_str parser stringifying it into
    something like '<...Directory object at 0x...>' first."""
    from superset_ownership.directory import LocalDirectory

    instance = LocalDirectory()
    flask_app.config["OWNERSHIP_DIRECTORY"] = instance
    reg = plugins.load(flask_app, strict=True)
    assert reg.directory is instance
    assert reg.failures == {}


def test_load_resolves_an_authorizer_class_set_directly_in_app_config(flask_app):
    from superset_ownership.authz import LocalAuthorizer

    flask_app.config["OWNERSHIP_AUTHORIZER"] = LocalAuthorizer
    # D-4: the default-directory-alias lookup only recognises the literal
    # strings "local"/"openfga" -- a class value must name its directory
    # explicitly, the same as any other authorizer spelling outside those
    # two would. Not a B1 regression: a correct instance/class read still
    # has to satisfy D-4's separate rule.
    flask_app.config["OWNERSHIP_DIRECTORY"] = "local"
    reg = plugins.load(flask_app, strict=True)
    assert isinstance(reg.authorizer, LocalAuthorizer)


def test_load_resolves_an_identity_instance_set_directly_in_app_config(flask_app):
    from superset_ownership.identity import DefaultIdentity

    instance = DefaultIdentity()
    flask_app.config["OWNERSHIP_IDENTITY"] = instance
    reg = plugins.load(flask_app, strict=True)
    assert reg.identity is instance


def test_load_resolves_a_callable_fga_config_provider_set_directly_in_app_config(
    flask_app,
):
    """OWNERSHIP_FGA_CONFIG_PROVIDER = <callable> (contract §5.2) must not
    be stringified either -- the B1 reproduction's second failure mode."""
    from superset_ownership.authz import OpenFGAAuthorizer
    from superset_ownership.fga_connection import FgaConnection

    def rotating_provider() -> FgaConnection:
        return FgaConnection(
            api_url="http://provider.example",
            store_id="PSTORE",
            model_id=None,
            headers={},
        )

    flask_app.config["OWNERSHIP_AUTHORIZER"] = OpenFGAAuthorizer
    flask_app.config["OWNERSHIP_DIRECTORY"] = (
        "openfga"  # D-4: a class value needs this named too
    )
    flask_app.config["OWNERSHIP_FGA_CONFIG_PROVIDER"] = rotating_provider
    reg = plugins.load(flask_app, strict=True)
    assert reg.provider is rotating_provider
    assert reg.connection.api_url == "http://provider.example"


def test_describe_names_an_instance_resolved_seam(flask_app):
    from superset_ownership.directory import LocalDirectory

    flask_app.config["OWNERSHIP_DIRECTORY"] = LocalDirectory()
    plugins.load(flask_app, strict=True)
    with flask_app.app_context():
        d = plugins.describe()
    assert d["directory"]["class"].endswith("LocalDirectory")


# ------------------------------------------------------------------------------- S2


def test_load_builds_no_connection_for_a_local_only_deployment(flask_app):
    """A `local` authorizer with the default directory (also `local`) never
    talks to a store -- a credentials/provider mistake it never exercises
    must not block its boot, and the boot log must not name a store it
    does not use."""
    reg = plugins.load(flask_app, strict=True)
    assert reg.connection is None
    assert reg.provider is None


def test_load_builds_no_connection_even_with_bad_credentials_when_authorizer_is_local(
    flask_app,
):
    flask_app.config["OWNERSHIP_FGA_CREDENTIALS"] = '{"type": "bogus"}'
    reg = plugins.load(flask_app, strict=True)  # must not raise
    assert reg.connection is None
    assert reg.failures == {}


def test_load_builds_a_connection_for_an_openfga_authorizer(flask_app):
    flask_app.config["OWNERSHIP_AUTHORIZER"] = "openfga"
    reg = plugins.load(flask_app, strict=True)
    assert reg.connection is not None


def test_load_builds_a_connection_when_only_the_directory_is_openfga(flask_app):
    """local authorizer, OWNERSHIP_DIRECTORY explicitly set to the OpenFGA
    one: the directory still needs the store even though the authorizer
    does not (S2's parenthetical: "and OWNERSHIP_DIRECTORY is not the
    OpenFGA one")."""
    flask_app.config["OWNERSHIP_DIRECTORY"] = "openfga"
    reg = plugins.load(flask_app, strict=True)
    assert reg.connection is not None


# ----------------------------------------------------------------------------- R2-1


def _build_store_backed_authorizer_class():
    """A class satisfying `Authorizer` structurally (every protocol member
    present, as a no-op stub) without subclassing `OpenFGAAuthorizer` --
    the contract's dotted-path spelling (`OWNERSHIP_AUTHORIZER =
    "pkg.mod:Class"`), not the documented subclass pattern (§8). R2-1: the
    pre-fix `isinstance(authorizer, OpenFGAAuthorizer)` allowlist missed
    exactly this shape. Built from the live protocol's own member list
    (`plugins._protocol_member_names`) so this stays correct if
    `Authorizer` grows or loses a method.
    """
    from superset_ownership.authz import Authorizer as _AuthorizerProtocol

    def _noop(self, *args, **kwargs):
        return None

    attrs = {"name": "store-backed", "protocol_version": 1}
    for member in plugins._protocol_member_names(_AuthorizerProtocol):
        if member not in attrs:
            attrs[member] = _noop
    return type("_StoreBackedAuthorizer", (), attrs)


_StoreBackedAuthorizer = _build_store_backed_authorizer_class()


def test_needs_fga_connection_true_for_a_non_subclassing_store_backed_authorizer(
    flask_app,
):
    """R2-1(a): the heuristic must build a connection unless BOTH resolved
    seams are the built-in Local* classes -- not just when the authorizer
    happens to be (or subclass) OpenFGAAuthorizer."""
    flask_app.config["OWNERSHIP_AUTHORIZER"] = _StoreBackedAuthorizer
    flask_app.config["OWNERSHIP_DIRECTORY"] = (
        "local"  # D-4: non-alias authorizer needs this named
    )
    reg = plugins.load(flask_app, strict=True)
    assert isinstance(reg.authorizer, _StoreBackedAuthorizer)
    assert reg.connection is not None


def test_needs_fga_connection_false_only_when_both_seams_are_the_local_builtins(
    flask_app,
):
    from superset_ownership.authz import LocalAuthorizer
    from superset_ownership.directory import LocalDirectory

    assert plugins._needs_fga_connection(LocalAuthorizer(), LocalDirectory()) is False
    assert (
        plugins._needs_fga_connection(_StoreBackedAuthorizer(), LocalDirectory())
        is True
    )
    assert plugins._needs_fga_connection(LocalAuthorizer(), object()) is True


def test_lazy_connection_is_built_once_and_cached_on_the_registry(
    flask_app, monkeypatch
):
    """R2-1(b): the lazy path (a local-only load(), a command asking for
    the connection anyway) must build once and cache both the provider
    and the connection on the registry -- not rebuild (and, per N5,
    re-warn on an inline token) on every call."""
    monkeypatch.setenv("OWNERSHIP_TOKEN_FOR_TEST", "tok-1")
    flask_app.config["OWNERSHIP_FGA_CREDENTIALS"] = {
        "type": "api_token",
        "token_env": "OWNERSHIP_TOKEN_FOR_TEST",
    }
    reg = plugins.load(flask_app, strict=True)
    assert reg.connection is None  # local-only: still skipped at load() time
    assert reg.provider is None
    with flask_app.app_context():
        first = plugins.get_fga_connection()
        second = plugins.get_fga_connection()
    assert first is second
    assert reg.connection is first
    assert reg.provider is not None


def test_lazy_connection_reconnect_rereads_a_rotated_token(flask_app, monkeypatch):
    """R2-1(b): once the lazy path has cached a provider, `fga reconnect`
    (`refresh_fga_connection()`) must rebuild THROUGH that provider -- so
    `OWNERSHIP_FGA_CREDENTIALS`' `token_env` rotation, which a
    `StaticProvider` re-reads from the environment on every call, keeps
    working instead of silently freezing at the first-built token."""
    monkeypatch.setenv("OWNERSHIP_TOKEN_FOR_TEST", "tok-1")
    flask_app.config["OWNERSHIP_FGA_CREDENTIALS"] = {
        "type": "api_token",
        "token_env": "OWNERSHIP_TOKEN_FOR_TEST",
    }
    plugins.load(flask_app, strict=True)
    with flask_app.app_context():
        first = plugins.get_fga_connection()
        assert first.headers == {"Authorization": "Bearer tok-1"}
        monkeypatch.setenv("OWNERSHIP_TOKEN_FOR_TEST", "tok-2")
        refreshed = plugins.refresh_fga_connection()
    assert refreshed.headers == {"Authorization": "Bearer tok-2"}


# ------------------------------------------------------------------------------- N1


def test_describe_carries_source_per_seam(flask_app):
    flask_app.config["OWNERSHIP_AUTHORIZER"] = "local"
    plugins.load(flask_app, strict=True)
    with flask_app.app_context():
        d = plugins.describe()
    assert d["authorizer"]["source"] == "config"
    assert d["identity"]["source"] == "default"


def test_describe_source_for_a_degraded_seam_says_so_not_the_bad_values_origin(
    flask_app, monkeypatch
):
    """The failing key's source() answer (e.g. "environment") describes the
    REJECTED value, not the default object now standing in for it -- N1's
    fix is to say so plainly rather than misreport where the default (in
    fact hard-coded, not read from anywhere) supposedly came from."""
    monkeypatch.setenv("OWNERSHIP_AUTHORIZER", "no.such.module:Nope")
    reg = plugins.load(flask_app, strict=False)
    assert reg.failures  # sanity: the seam did fail
    with flask_app.app_context():
        d = plugins.describe()
    assert d["authorizer"]["source"] == "default (degraded)"


# ------------------------------------------------------------------------------- N2


def test_load_is_idempotent_on_the_same_app(flask_app):
    first = plugins.load(flask_app, strict=True)
    second = plugins.load(flask_app, strict=True)
    assert second is first


def test_load_idempotent_does_not_reprint_the_degraded_banner(
    flask_app, capsys, monkeypatch
):
    monkeypatch.setenv("OWNERSHIP_AUTHORIZER", "no.such.module:Nope")
    plugins.load(flask_app, strict=False)
    capsys.readouterr()  # discard the first call's banner
    plugins.load(flask_app, strict=False)
    err = capsys.readouterr().err
    assert err == "", "a second load() on the same app must not run (or print) again"


# ------------------------------------------------------------------------------- B2


def test_get_fga_connection_raises_when_the_connection_seam_is_recorded_as_failed(
    flask_app,
):
    flask_app.config["OWNERSHIP_AUTHORIZER"] = "openfga"
    flask_app.config["OWNERSHIP_FGA_CREDENTIALS"] = '{"type": "bogus"}'
    reg = plugins.load(flask_app, strict=False)
    assert "connection" in reg.failures
    with flask_app.app_context():
        with pytest.raises(plugins.PluginError):
            plugins.get_fga_connection()


def test_get_fga_connection_never_silently_rebuilds_from_an_empty_layer_on_failure(
    flask_app, monkeypatch
):
    """The B2 regression itself: with the bad credentials only in
    app.config and the environment clean, the OLD code's
    `_build_connection({})` fallback would happily answer from the
    environment's default store with no Authorization header. The fixed
    accessor must raise instead of answering anything."""
    for key in list(os.environ):
        if key.startswith("OWNERSHIP_"):
            monkeypatch.delenv(key, raising=False)
    flask_app.config["OWNERSHIP_AUTHORIZER"] = "openfga"
    flask_app.config["OWNERSHIP_FGA_API_URL"] = "http://configured-store.example:8080"
    flask_app.config["OWNERSHIP_FGA_STORE"] = "CONFIGSTORE"
    flask_app.config["OWNERSHIP_FGA_CREDENTIALS"] = (
        '{"type": "api_token", "token_env": "TOKEN_NOT_SET_FOR_TEST"}'
    )
    plugins.load(flask_app, strict=False)
    with flask_app.app_context():
        with pytest.raises(plugins.PluginError, match="TOKEN_NOT_SET_FOR_TEST"):
            plugins.get_fga_connection()


def test_refresh_fga_connection_also_raises_on_a_degraded_connection_seam(flask_app):
    flask_app.config["OWNERSHIP_AUTHORIZER"] = "openfga"
    flask_app.config["OWNERSHIP_FGA_CREDENTIALS"] = '{"type": "bogus"}'
    plugins.load(flask_app, strict=False)
    with flask_app.app_context():
        with pytest.raises(plugins.PluginError):
            plugins.refresh_fga_connection()


def test_get_fga_connection_builds_lazily_from_app_config_when_nothing_failed(
    flask_app,
):
    """S2's lazy path: `local` authorizer, no connection built at load()
    time, no failure either -- a command that asks anyway (the `fga` CLI
    group) still gets a connection built from the LIVE app.config, not an
    empty layer."""
    flask_app.config["OWNERSHIP_FGA_STORE"] = "LATECONFIGSTORE"
    reg = plugins.load(flask_app, strict=True)
    assert reg.connection is None  # local authorizer: nothing built at load()
    with flask_app.app_context():
        conn = plugins.get_fga_connection()
    assert conn is not None
    assert conn.store_id == "LATECONFIGSTORE"


# --------------------------------------------------------------- maintenance_invocation


@pytest.mark.parametrize(
    "argv,expected",
    [
        (["superset", "ownership", "db", "upgrade"], True),
        (["superset", "ownership", "db", "downgrade"], True),
        (["superset", "ownership", "db", "current"], True),
        (["superset", "ownership", "db", "stamp"], True),
        (["superset", "ownership", "status"], True),
        (["superset", "ownership", "fga", "reconnect"], True),
        (["superset", "ownership", "fga", "status"], True),
        (["superset", "ownership", "fga", "install-model"], True),
        (["superset", "ownership", "fga", "show-model"], True),
        (["superset", "ownership", "plugin", "verify"], True),
        (["superset", "ownership", "plugin", "seed-scratch"], True),
        (["superset", "ownership", "plugin", "describe"], True),  # cross-lane M-2
        (["superset", "ownership", "check"], False),
        (["superset", "ownership", "enable"], False),
        (["superset", "ownership", "reconcile"], False),
        (["superset", "ownership", "purge-tenant"], False),
        (["superset", "ownership", "backfill-tenants"], False),
        (["superset", "ownership", "outbox", "drain"], False),
        (["superset", "db", "upgrade"], False),
        (["superset", "run"], False),
        ([], False),
        (["gunicorn", "superset.app:create_app()"], False),
    ],
)
def test_maintenance_invocation_argv_table(argv, expected):
    assert plugins.maintenance_invocation(argv) is expected


# ---------------------------------------------------------------------- no-app fallback


def test_no_app_fallback_resolves_from_the_environment(monkeypatch):
    plugins._FALLBACK_CACHE.clear()
    monkeypatch.setenv("OWNERSHIP_AUTHORIZER", "local")
    assert plugins.get_authorizer().name == "local"


def test_no_app_fallback_rebuilds_when_the_ref_changes(monkeypatch):
    plugins._FALLBACK_CACHE.clear()
    monkeypatch.delenv("OWNERSHIP_AUTHORIZER", raising=False)
    first = plugins.get_authorizer()
    assert first.name == "local"
    monkeypatch.setenv("OWNERSHIP_AUTHORIZER", "openfga")
    second = plugins.get_authorizer()
    assert second.name == "openfga"
    assert second is not first


def test_no_app_fallback_caches_when_the_ref_is_unchanged(monkeypatch):
    plugins._FALLBACK_CACHE.clear()
    monkeypatch.setenv("OWNERSHIP_AUTHORIZER", "local")
    first = plugins.get_authorizer()
    second = plugins.get_authorizer()
    assert first is second


def test_no_app_fallback_bad_ref_raises_at_the_accessor_not_at_import(monkeypatch):
    plugins._FALLBACK_CACHE.clear()
    monkeypatch.setenv("OWNERSHIP_AUTHORIZER", "no.such.module:Nope")
    with pytest.raises(plugins.PluginError):
        plugins.get_authorizer()


def test_get_identity_no_app_fallback_default():
    plugins._FALLBACK_CACHE.clear()
    identity = plugins.get_identity()
    assert identity.protocol_version == 1
    assert hasattr(identity, "member_guid")
    assert hasattr(identity, "normalize_subject")


def test_get_directory_no_app_fallback_resolves_local(monkeypatch):
    # Methods on the real LocalDirectory reach into Superset's security
    # manager, which needs a real app -- structural checks only here. Once
    # directory.py is on this branch, plugins._directory_aliases() finds
    # its real _DIRECTORIES table first (getattr against the module), so
    # this resolves the real LocalDirectory, not plugins.py's own
    # provisional fallback adapter.
    plugins._FALLBACK_CACHE.clear()
    monkeypatch.setenv("OWNERSHIP_AUTHORIZER", "local")
    monkeypatch.delenv("OWNERSHIP_DIRECTORY", raising=False)
    directory = plugins.get_directory()
    assert directory.protocol_version == 1
    assert hasattr(directory, "group_exists")
    assert hasattr(directory, "search_users")


# -------------------------------------------------------- app registry takes precedence


def test_app_registry_wins_over_the_fallback_once_loaded(flask_app):
    reg = plugins.load(flask_app, strict=True)
    with flask_app.app_context():
        assert plugins.get_authorizer() is reg.authorizer
        # get_directory()/get_identity() hand back a hook-consulting wrapper
        # (spec §4.5.1 item 3), not the raw registry seam -- but the seam it
        # wraps IS the registry's, which is the "same instance across calls,
        # from the loaded app" guarantee this test exists to pin.
        assert plugins.get_directory()._unwrap() is reg.directory  # noqa: SLF001
        assert plugins.get_identity()._unwrap() is reg.identity  # noqa: SLF001


# --------------------------------------------------------------------------- describe()


def test_describe_shape_with_a_loaded_registry(flask_app):
    plugins.load(flask_app, strict=True)
    with flask_app.app_context():
        d = plugins.describe()
    assert set(d) == {"authorizer", "directory", "identity", "degraded", "hooks"}
    for seam in ("authorizer", "directory", "identity"):
        assert "class" in d[seam]
        assert d[seam]["protocol_version"] == 1
    assert d["degraded"] == []
    # Every hook the contract defines is listed, unset by default.
    from superset_ownership import plugin_hooks

    assert set(d["hooks"]) == {spec.setting for spec in plugin_hooks.HOOK_SPECS}
    assert all(v == {"source": "default"} for v in d["hooks"].values())


def test_describe_lists_degraded_seams(flask_app):
    flask_app.config["OWNERSHIP_AUTHORIZER"] = "no.such.module:Nope"
    flask_app.config["OWNERSHIP_DIRECTORY"] = "local"
    plugins.load(flask_app, strict=False)
    with flask_app.app_context():
        d = plugins.describe()
    assert d["degraded"] == ["authorizer"]


def test_describe_without_a_loaded_registry_still_answers(monkeypatch):
    plugins._FALLBACK_CACHE.clear()
    monkeypatch.setenv("OWNERSHIP_AUTHORIZER", "local")
    d = plugins.describe()
    assert d["authorizer"]["class"].endswith("LocalAuthorizer")
    assert d["degraded"] == []


class _NamelessDirectory:
    """I-4: satisfies the `Directory` protocol but defines no `name` of its
    own -- the shape a real override (the shipped `FixedGroupsDirectory`
    example, or any third-party class) is under no obligation to carry.
    `describe()`'s `alias` must fall back to the dotted class path for
    exactly this shape, which is also what `api.py`'s `_directory_source()`
    reports as `/subjects`' `source` field."""

    protocol_version = 1

    def search_users(self, tenant, query, *, limit=100, cursor=None):
        return {"items": [], "next_cursor": None}

    def user_in_tenant(self, member_guid, tenant):
        return False

    def list_groups(self, tenant, *, cursor=None):
        return {"items": [], "next_cursor": None}

    def group_members(self, group_id, *, cursor=None):
        return {"items": [], "next_cursor": None}

    def user_in_group(self, member_guid, group_id):
        return False

    def group_exists(self, group_id):
        return False

    def tenant_administrators(self, tenant):
        return []

    def health(self):
        from superset_ownership.directory import DirectoryHealth

        return DirectoryHealth(ok=True, detail="nameless")


def test_describe_alias_falls_back_to_the_dotted_class_path_with_no_name(flask_app):
    """I-4: `describe()["directory"]["class"]` is always the dotted path;
    `alias` -- what `/subjects`' `source` field actually reports -- is the
    shipped classes' short `name` (`local`/`openfga`) when they have one,
    and the SAME dotted path when the plugged class does not. Before this
    fix, `alias` did not exist on the returned dict at all, so
    `api.py::_directory_source()`'s lookup always raised `KeyError` and
    always fell back to `getattr(get_directory(), "name", "openfga")` --
    silently mislabelling a nameless directory as `"openfga"`."""
    flask_app.config["OWNERSHIP_DIRECTORY"] = _NamelessDirectory()
    plugins.load(flask_app, strict=True)
    with flask_app.app_context():
        d = plugins.describe()
    assert d["directory"]["class"].endswith(":_NamelessDirectory")
    assert d["directory"]["alias"] == d["directory"]["class"]
    assert d["directory"]["alias"] != "openfga"


def test_describe_alias_is_the_short_name_for_the_shipped_classes(flask_app):
    """The other half: the two shipped classes DO carry a `name`, and
    `alias` reports it rather than their (also perfectly valid) dotted
    path -- the picker's `source` field should read `"local"`/`"openfga"`
    for the default deployment, not a dotted module path."""
    flask_app.config["OWNERSHIP_AUTHORIZER"] = "local"
    plugins.load(flask_app, strict=True)
    with flask_app.app_context():
        d = plugins.describe()
    assert d["directory"]["alias"] == "local"
    assert d["authorizer"]["alias"] == "local"


# --------------------------------------------------------------------------- counting()


class _FakeAuthorizer:
    name = "fake"

    def check(self, user, relation, obj):
        return True


class _FakeDirectory:
    def group_exists(self, group_name):
        return True


def test_counting_records_calls_on_the_app_registry(flask_app):
    reg = plugins.load(flask_app, strict=True)
    reg.authorizer, reg.directory = _FakeAuthorizer(), _FakeDirectory()
    with flask_app.app_context():
        with plugins.counting() as log:
            plugins.get_authorizer().check("user:a", "viewer", "chart:1")
        assert log == ["check"]
        # restored afterwards
        assert isinstance(plugins.get_authorizer(), _FakeAuthorizer)


def test_counting_shares_one_log_across_authorizer_and_directory(flask_app):
    reg = plugins.load(flask_app, strict=True)
    reg.authorizer, reg.directory = _FakeAuthorizer(), _FakeDirectory()
    with flask_app.app_context():
        with plugins.counting() as log:
            plugins.get_authorizer().check("user:a", "viewer", "chart:1")
            plugins.get_directory().group_exists("g1")
        assert log == ["check", "group_exists"]


def test_counting_restores_on_exception(flask_app):
    reg = plugins.load(flask_app, strict=True)
    reg.authorizer, reg.directory = _FakeAuthorizer(), _FakeDirectory()
    with flask_app.app_context():
        with pytest.raises(ValueError, match="boom"):
            with plugins.counting():
                raise ValueError("boom")
        assert isinstance(plugins.get_authorizer(), _FakeAuthorizer)


# --------------------------------------------- structural: section 7.7 invariant, early


@pytest.mark.parametrize("module_name", ["hooks.py", "guard.py"])
def test_hooks_and_guard_never_import_the_directory(module_name):
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    source = open(os.path.join(here, module_name), encoding="utf-8").read()
    assert "get_directory" not in source
    assert "superset_ownership.directory" not in source


def test_plugins_module_itself_does_not_import_api_or_hooks():
    """The loader must not create a cycle with the request-path modules it
    configures; a lazy, function-local import is fine, a module-level one
    is not."""
    here = os.path.dirname(os.path.abspath(__file__))
    plugins_source = open(
        os.path.join(os.path.dirname(here), "plugins.py"), encoding="utf-8"
    ).read()
    header = plugins_source.split("\ndef ", 1)[0]
    assert "import superset_ownership.api" not in header
    assert "import superset_ownership.hooks" not in header

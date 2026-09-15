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
"""configure.py: the config layer as a function.

Asserts the precedence rules the config block documents (config layer beats
environment; only the literal "true"/True switches the feature on; the UI
flag is DERIVED from the switch, never set on its own) against a plain dict
standing in for a config module's namespace, in both switch states. The
ON-state wiring is checked structurally (which keys, which callables), not
by booting Superset -- test_endpoints.py covers that.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import subprocess
import sys
import types
from types import SimpleNamespace

import pytest
from superset_ownership.configure import configure

# configure() reads superset.config's defaults on every call, so everything
# but the import-purity test needs an importable Superset (the container);
# the pure CI job skips them, like every other Superset-backed test here.
needs_superset = pytest.mark.skipif(
    importlib.util.find_spec("superset") is None,
    reason="configure() needs superset.config; runs in the container",
)

@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """The process running the suite may itself be a configured deployment
    (the dev container exports OWNERSHIP_*); start every test from nothing."""
    for name in list(os.environ):
        if name.startswith(("OWNERSHIP_", "SUPERSET_FEATURE_")):
            monkeypatch.delenv(name, raising=False)


def _ns(**extra):
    ns = {"FEATURE_FLAGS": {"SOMETHING_ELSE": True}, "BLUEPRINTS": []}
    ns.update(extra)
    return ns


# --- the switch -------------------------------------------------------------


@needs_superset
def test_off_by_default_with_nothing_set():
    ns = _ns()
    configure(ns)
    assert ns["OWNERSHIP_ENABLED"] is False
    assert ns["OWNERSHIP_ENABLED_SOURCE"] == "environment"
    assert ns["FEATURE_FLAGS"]["OBJECT_OWNERSHIP"] is False
    assert ns["FEATURE_FLAGS"]["SOMETHING_ELSE"] is True, "earlier flags survive"
    assert ns["BLUEPRINTS"] == [], "OFF registers no blueprint"
    assert callable(ns["FLASK_APP_MUTATOR"]), "OFF still installs the flag report"
    assert "EXTRA_RAISE_FOR_ACCESS_BYPASS" not in ns, "OFF registers no hooks"


@pytest.mark.parametrize("value", ["true", "TRUE", " True "])
@needs_superset
def test_environment_is_the_default_source(monkeypatch, value):
    monkeypatch.setenv("OWNERSHIP_ENABLED", value)
    ns = _ns()
    configure(ns)
    assert ns["OWNERSHIP_ENABLED"] is True
    assert ns["OWNERSHIP_ENABLED_SOURCE"] == "environment"
    assert ns["FEATURE_FLAGS"]["OBJECT_OWNERSHIP"] is True


@pytest.mark.parametrize("value", ["1", "yes", "on", "false", "", "True1"])
@needs_superset
def test_only_the_literal_true_switches_on(monkeypatch, value):
    monkeypatch.setenv("OWNERSHIP_ENABLED", value)
    ns = _ns()
    configure(ns)
    assert ns["OWNERSHIP_ENABLED"] is False


@pytest.mark.parametrize("layer_value", [True, "true"])
@needs_superset
def test_config_layer_beats_environment(monkeypatch, layer_value):
    monkeypatch.setenv("OWNERSHIP_ENABLED", "false")
    ns = _ns(OWNERSHIP_ENABLED=layer_value)
    configure(ns)
    assert ns["OWNERSHIP_ENABLED"] is True
    assert ns["OWNERSHIP_ENABLED_SOURCE"] == "config"


@needs_superset
def test_config_layer_false_beats_environment_true(monkeypatch):
    monkeypatch.setenv("OWNERSHIP_ENABLED", "true")
    ns = _ns(OWNERSHIP_ENABLED=False)
    configure(ns)
    assert ns["OWNERSHIP_ENABLED"] is False
    assert ns["OWNERSHIP_ENABLED_SOURCE"] == "config"


# --- the UI flag is derived, never set on its own ---------------------------


@needs_superset
def test_ui_flag_alone_does_not_turn_the_feature_on(monkeypatch):
    monkeypatch.setenv("SUPERSET_FEATURE_OBJECT_OWNERSHIP", "true")
    ns = _ns()
    ns["FEATURE_FLAGS"]["OBJECT_OWNERSHIP"] = True
    configure(ns)
    assert ns["OWNERSHIP_ENABLED"] is False
    assert ns["FEATURE_FLAGS"]["OBJECT_OWNERSHIP"] is False, "derived from the switch"
    assert ns["OWNERSHIP_INHERITED_UI_FLAG"] is True, (
        "what the earlier layer said is kept for the boot report"
    )


@needs_superset
def test_inherited_ui_flag_is_none_when_no_layer_set_it():
    ns = _ns()
    configure(ns)
    assert ns["OWNERSHIP_INHERITED_UI_FLAG"] is None


@needs_superset
def test_other_superset_feature_env_vars_still_merge(monkeypatch):
    monkeypatch.setenv("SUPERSET_FEATURE_DASHBOARD_RBAC", "true")
    monkeypatch.setenv("SUPERSET_FEATURE_ALERT_REPORTS", "yes")
    ns = _ns()
    configure(ns)
    assert ns["FEATURE_FLAGS"]["DASHBOARD_RBAC"] is True
    assert ns["FEATURE_FLAGS"]["ALERT_REPORTS"] is False, (
        "strict 'true' only, as upstream"
    )


# --- namespace shape --------------------------------------------------------


@needs_superset
def test_works_as_the_first_line_of_an_empty_config():
    """superset/config.py execs SUPERSET_CONFIG_PATH into an EMPTY namespace;
    a config whose only line is configure(globals()) must not KeyError."""
    ns = {}
    configure(ns)
    assert isinstance(ns["FEATURE_FLAGS"], dict)
    assert ns["FEATURE_FLAGS"]["OBJECT_OWNERSHIP"] is False
    assert callable(ns["FLASK_APP_MUTATOR"])


@needs_superset
def test_on_state_wires_hooks_blueprint_and_settings(monkeypatch):
    monkeypatch.setenv("OWNERSHIP_ENABLED", "true")
    sentinel_bp = object()
    ns = _ns(BLUEPRINTS=[sentinel_bp])
    configure(ns)
    from superset_ownership.api import ownership_bp

    assert ns["BLUEPRINTS"] == [sentinel_bp, ownership_bp], (
        "appended after the prior layer's"
    )
    for key in (
        "EXTRA_ACCESS_QUERY_FILTERS",
        "EXTRA_RAISE_FOR_ACCESS_BYPASS",
        "EXTRA_EDITORS_RESOLVER",
        "EXTRA_OWNERS_RESOLVER",
        "AFTER_ASSET_CREATE",
    ):
        assert key in ns, key
    assert ns["OWNERSHIP_AUTHORIZER"] == "local", "the default backend"
    assert "CELERY_CONFIG" not in ns, "outbox Celery is opt-in"
    assert callable(ns["FLASK_APP_MUTATOR"])


@needs_superset
def test_bad_group_id_format_fails_at_config_time(monkeypatch):
    monkeypatch.setenv("OWNERSHIP_ENABLED", "true")
    monkeypatch.setenv("OWNERSHIP_GROUP_ID_FORMAT", "{name}")  # no {tenant}
    from superset_ownership.identity import GroupIdFormatError

    with pytest.raises(GroupIdFormatError):
        configure(_ns())


@needs_superset
def test_outbox_celery_opt_in_sets_celery_config(monkeypatch):
    monkeypatch.setenv("OWNERSHIP_ENABLED", "true")
    monkeypatch.setenv("OWNERSHIP_OUTBOX_CELERY", "true")
    ns = _ns()
    configure(ns)
    cfg = ns["CELERY_CONFIG"]
    assert cfg.imports == ("superset_ownership.outbox",)
    assert "superset_ownership.outbox.drain" in cfg.beat_schedule


# --- import-time purity -----------------------------------------------------


def test_importing_the_module_does_not_import_superset():
    """A config file that superset/config.py is still exec'ing may import
    this module; every `superset.*` import must therefore be deferred into
    the function body or the import forms a cycle."""
    code = (
        "import sys; import superset_ownership.configure; "
        "print(sorted(m for m in sys.modules "
        "if m == 'superset' or m.startswith('superset.')))"
    )
    out = subprocess.run(  # noqa: S603 -- our own interpreter, our own literal
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == "[]", out


# --- the OFF mutator chains and reports -------------------------------------


def _stub_app(**config):
    return SimpleNamespace(
        config={
            "OWNERSHIP_ENABLED": False,
            "FEATURE_FLAGS": {"OBJECT_OWNERSHIP": False},
            **config,
        }
    )


def _run_off_mutator(mutator, monkeypatch, caplog):
    """Run an OFF-state mutator on a stub app with the package's flags module
    made unimportable, so the guarded-import branch runs: the prior mutator,
    then the two inline report lines. Returns the (level, logger, message)
    records it produced."""
    monkeypatch.setitem(sys.modules, "superset_ownership.flags", None)
    app = _stub_app()
    with caplog.at_level(logging.INFO, logger="superset_ownership.config"):
        mutator(app)
    return [(r.levelname, r.name, r.getMessage()) for r in caplog.records]


@needs_superset
def test_off_mutator_chains_the_prior_mutator_then_reports(monkeypatch, caplog):
    seen = []
    ns = _ns(FLASK_APP_MUTATOR=lambda app: seen.append(app))
    configure(ns)
    records = _run_off_mutator(ns["FLASK_APP_MUTATOR"], monkeypatch, caplog)
    assert len(seen) == 1, "the earlier layer's mutator ran, once, first"
    assert [(lvl, name) for lvl, name, _ in records] == [
        ("WARNING", "superset_ownership.config"),
        ("INFO", "superset_ownership.config"),
    ]
    assert "package not importable" in records[0][2]
    assert "package absent" in records[1][2]


# --- drift guard against the dev config -------------------------------------
#
# superset_config_docker_light.py delegates to configure() when the package
# is importable and carries an inline fallback for when it is not. The
# fallback is the only copy of the switch logic outside configure(); these
# tests exec the dev config the way superset/config.py does, on a CONTROLLED
# base (a stub `superset_config` module, so the process's own deployment
# config cannot leak in), with the package hidden, and hold the fallback to
# configure()'s output.

LIGHT_CONFIG = os.path.join(
    os.path.dirname(__file__), "..", "..", "superset_config_docker_light.py"
)


def _exec_light_config(monkeypatch, base, *, package_present):
    stub = types.ModuleType("superset_config")
    for k, v in base.items():
        setattr(stub, k, v)
    monkeypatch.setitem(sys.modules, "superset_config", stub)
    if not package_present:
        real = importlib.util.find_spec

        def hidden(name, *a, **k):
            return None if name == "superset_ownership" else real(name, *a, **k)

        monkeypatch.setattr(importlib.util, "find_spec", hidden)
    ns: dict = {"__file__": LIGHT_CONFIG}
    with open(LIGHT_CONFIG, encoding="utf-8") as handle:
        exec(compile(handle.read(), LIGHT_CONFIG, "exec"), ns)  # noqa: S102
    return ns


def _base(**extra):
    base = {"FEATURE_FLAGS": {"SOMETHING_ELSE": True}, "BLUEPRINTS": []}
    base.update(extra)
    return base


def _switch_keys(ns):
    return {
        "OWNERSHIP_ENABLED": ns["OWNERSHIP_ENABLED"],
        "OWNERSHIP_ENABLED_SOURCE": ns["OWNERSHIP_ENABLED_SOURCE"],
        "OWNERSHIP_INHERITED_UI_FLAG": ns["OWNERSHIP_INHERITED_UI_FLAG"],
        "OBJECT_OWNERSHIP": ns["FEATURE_FLAGS"]["OBJECT_OWNERSHIP"],
        "SOMETHING_ELSE": ns["FEATURE_FLAGS"].get("SOMETHING_ELSE"),
    }


@needs_superset
@pytest.mark.skipif(not os.path.exists(LIGHT_CONFIG), reason="dev config not present")
@pytest.mark.parametrize(
    "env, layer",
    [
        ({}, {}),
        ({"OWNERSHIP_ENABLED": "true"}, {}),
        ({"OWNERSHIP_ENABLED": "yes"}, {}),
        ({"OWNERSHIP_ENABLED": "true"}, {"OWNERSHIP_ENABLED": False}),
        ({}, {"OWNERSHIP_ENABLED": "true"}),
        ({"SUPERSET_FEATURE_OBJECT_OWNERSHIP": "true"}, {}),
    ],
)
def test_dev_config_fallback_resolves_the_switch_like_configure(
    monkeypatch, env, layer
):
    """With the package hidden, the dev config's inline fallback must land on
    the same switch, source, inherited flag and derived flag as configure()
    on the same base -- or raise, when the switch is on without the package."""
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    expected = _base(**layer)
    configure(expected)

    if expected["OWNERSHIP_ENABLED"]:
        with pytest.raises(RuntimeError, match="not importable"):
            _exec_light_config(monkeypatch, _base(**layer), package_present=False)
        return
    fallback = _exec_light_config(monkeypatch, _base(**layer), package_present=False)
    assert _switch_keys(fallback) == _switch_keys(expected)
    assert fallback["BLUEPRINTS"] == [], "OFF registers no blueprint"
    assert "EXTRA_RAISE_FOR_ACCESS_BYPASS" not in fallback


@needs_superset
@pytest.mark.skipif(not os.path.exists(LIGHT_CONFIG), reason="dev config not present")
def test_dev_config_fallback_mutator_reports_like_configure(monkeypatch, caplog):
    """The fallback's OFF mutator and configure()'s guarded-import branch
    emit the same two records, after chaining the same prior mutator."""
    seen = []
    prior = lambda app: seen.append("prior")  # noqa: E731

    expected = _base(FLASK_APP_MUTATOR=prior)
    configure(expected)
    want = _run_off_mutator(expected["FLASK_APP_MUTATOR"], monkeypatch, caplog)
    caplog.clear()

    fallback = _exec_light_config(
        monkeypatch, _base(FLASK_APP_MUTATOR=prior), package_present=False
    )
    got = _run_off_mutator(fallback["FLASK_APP_MUTATOR"], monkeypatch, caplog)

    assert seen == ["prior", "prior"]
    assert [(lvl, name) for lvl, name, _ in got] == [
        (lvl, name) for lvl, name, _ in want
    ]
    assert got[1][2] == want[1][2], "the INFO report line is identical"
    assert got[0][2].split("(")[0] == want[0][2].split("(")[0], (
        "same WARNING up to the import error text"
    )


@needs_superset
@pytest.mark.skipif(not os.path.exists(LIGHT_CONFIG), reason="dev config not present")
@pytest.mark.parametrize("enabled", ["false", "true"])
def test_dev_config_delegates_to_configure_when_the_package_is_present(
    monkeypatch, enabled
):
    """With the package importable the dev config's ownership layer IS
    configure(): every key it produces matches, in both switch states."""
    monkeypatch.setenv("OWNERSHIP_ENABLED", enabled)
    expected = _base()
    configure(expected)
    delegated = _exec_light_config(monkeypatch, _base(), package_present=True)
    keys = {
        k
        for k in expected
        if k.startswith(("OWNERSHIP_", "EXTRA_"))
        or k in ("AFTER_ASSET_CREATE", "BLUEPRINTS")
    }
    for key in sorted(keys):
        assert key in delegated, key
        a, b = expected[key], delegated[key]
        if callable(a) and not isinstance(a, type):
            assert callable(b), key
            assert a.__name__ == b.__name__, key
        else:
            assert a == b, key
    assert delegated["FEATURE_FLAGS"]["OBJECT_OWNERSHIP"] == (enabled == "true")

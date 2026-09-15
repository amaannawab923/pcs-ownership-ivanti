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
"""dashboard_patch.py: the tile access marker as a runtime wrap.

Two layers. The pure half (`_assert_wrappable`, `_degrade_on_failure`,
`get_access_contact_names` without an app) needs no Superset. The
integration half pins the wrap against the REAL stock method in this
checkout: the pinned params/hash must be derivable from it (never
hand-typed), the wrap must compose with it (`form_data` gone, `has_access`
and `owners` added, title kept), and the two failure modes must split by
invocation kind -- web refuses, `ownership check` reports.
"""

from __future__ import annotations

import inspect
import logging
from types import SimpleNamespace

import pytest
from superset_ownership import dashboard_patch as dp

# --- pure: the assertion ---------------------------------------------------


def _stock_like(self, chart):  # a method with the stock parameter shape
    return {"id": 1}


def test_assert_wrappable_rejects_a_changed_parameter_shape():
    def reshaped(self, chart, extra=None):
        return {}

    with pytest.raises(dp.DashboardPatchError, match="parameter shape changed"):
        dp._assert_wrappable(reshaped)


def test_assert_wrappable_rejects_a_changed_source(monkeypatch):
    monkeypatch.setattr(dp, "_EXPECTED_SOURCE_SHA256", "0" * 64)
    with pytest.raises(dp.DashboardPatchError, match="source changed upstream"):
        dp._assert_wrappable(_stock_like)


def test_assert_wrappable_names_the_bytecode_only_requirement(monkeypatch):
    """A base image shipping no .py source: still the same error type, and
    it says what the image must ship, not a bare OSError."""

    def unreadable(method):
        raise OSError("could not get source code")

    monkeypatch.setattr(inspect, "getsource", unreadable)
    with pytest.raises(dp.DashboardPatchError, match="must ship .py sources"):
        dp._assert_wrappable(_stock_like)


def test_assert_wrappable_checks_parameters_before_source(monkeypatch):
    """The parameter check needs no source, so a reshaped method is still
    caught on a bytecode-only install."""

    def unreadable(method):
        raise OSError("no source")

    monkeypatch.setattr(inspect, "getsource", unreadable)

    def reshaped(self, chart, extra=None):
        return {}

    with pytest.raises(dp.DashboardPatchError, match="parameter shape changed"):
        dp._assert_wrappable(reshaped)


def test_pins_for_matches_assert_wrappable(monkeypatch):
    """`_pins_for` is what the version-bump procedure regenerates the
    constants from; pinning a method to its own output must pass."""
    params, digest = dp._pins_for(_stock_like)
    monkeypatch.setattr(dp, "_EXPECTED_PARAMS", params)
    monkeypatch.setattr(dp, "_EXPECTED_SOURCE_SHA256", digest)
    dp._assert_wrappable(_stock_like)


# --- pure: which processes may boot without the wrap ------------------------


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["superset", "ownership", "check"], True),
        (["superset", "ownership", "status"], True),
        (["superset", "ownership", "db", "upgrade"], True),
        (["superset", "ownership", "plugin", "describe"], True),
        (["superset", "ownership", "backfill-tenants"], False),
        (["superset", "ownership", "enable"], False),
        (["superset", "ownership", "reconcile"], False),
        (["superset", "ownership", "outbox", "drain"], False),
        (["superset", "db", "upgrade"], False),
        (["superset", "run", "-p", "8088"], False),
        (["gunicorn", "superset.app:create_app()"], False),
        (["celery", "--app=superset.tasks.celery_app:app", "worker"], False),
        (["python", "-c", "from superset.app import create_app; ..."], False),
        (["superset", "ownership"], False),
    ],
)
def test_degrade_on_failure_by_invocation(argv, expected):
    assert dp._degrade_on_failure(argv) is expected


def test_degrade_on_failure_agrees_with_maintenance_invocation():
    """Everything plugins.maintenance_invocation() allows to run degraded,
    this does too -- one policy, widened by exactly `check`."""
    from superset_ownership import plugins

    for argv in (
        ["superset", "ownership", "db", "current"],
        ["superset", "ownership", "fga", "status"],
        ["superset", "ownership", "plugin", "verify"],
    ):
        assert plugins.maintenance_invocation(argv) is True
        assert dp._degrade_on_failure(argv) is True
    assert plugins.maintenance_invocation(["superset", "ownership", "check"]) is False
    assert dp._degrade_on_failure(["superset", "ownership", "check"]) is True


# --- pure: who to ask ---------------------------------------------------------


def test_get_access_contact_names_is_empty_without_an_app_context():
    pytest.importorskip("flask")
    assert dp.get_access_contact_names(SimpleNamespace(owners=["a"])) == []


# --- integration: against the real stock method ----------------------------


@pytest.fixture
def stock_method(harness):
    """The pristine stock method, with the class restored afterwards. The
    harness's own mutator installs the wrap at app creation, so the
    pristine original is what the wrapper remembers."""
    from superset.dashboards.api import DashboardRestApi

    installed = DashboardRestApi._serialize_dashboard_chart
    original = getattr(installed, "_superset_ownership_original", installed)
    DashboardRestApi._serialize_dashboard_chart = original
    try:
        yield original
    finally:
        DashboardRestApi._serialize_dashboard_chart = installed


def test_pins_are_derived_from_the_real_stock_method(stock_method):
    """The constants must equal what `_pins_for` computes from the method
    this checkout ships -- the version-bump procedure's invariant."""
    params, digest = dp._pins_for(stock_method)
    assert params == dp._EXPECTED_PARAMS
    assert digest == dp._EXPECTED_SOURCE_SHA256
    dp._assert_wrappable(stock_method)  # and so it is wrappable


def test_pins_for_sees_through_the_installed_wrapper(harness, stock_method):
    """The version-bump procedure runs in an ON-state process where the
    class attribute is already the wrapper; the pins must still be the
    stock method's."""
    from superset.dashboards.api import DashboardRestApi

    dp.install()
    assert dp._pins_for(DashboardRestApi._serialize_dashboard_chart) == (
        dp._EXPECTED_PARAMS,
        dp._EXPECTED_SOURCE_SHA256,
    )


def test_file_pins_equal_live_pins(stock_method):
    """The build-time (file) and boot-time (live method) checks must compute
    the identical digest, or a build could pass while boot refuses."""
    path = inspect.getsourcefile(stock_method)
    assert dp._pins_from_file(path) == dp._pins_for(stock_method)
    dp.assert_stock_file(path)
    dp.assert_stock_file()  # default path: the installed superset


@pytest.mark.parametrize(
    "prefix, suffix",
    [
        ("", ""),
        ("", "-no-trailing-newline"),
        ("\x0c\n", ""),  # form feed: str.splitlines splits it, the tokenizer does not
        ("# \u2028 in a comment\n", ""),
        ("\ufeff", ""),  # BOM
        ("# -*- coding: latin-1 -*-\n# caf\xe9\n", ""),
    ],
)
def test_file_pins_match_getsource_on_odd_files(tmp_path, prefix, suffix):
    """The file reader must agree with inspect.getsource on every file shape
    the tokenizer and str.splitlines disagree about."""
    import importlib.util

    body = (
        "class DashboardRestApi:\n"
        "    def _serialize_dashboard_chart(self, chart):\n"
        "        return {}\n"
    )
    text = prefix + body
    if suffix:
        text = text.rstrip("\n")
    path = tmp_path / "api.py"
    encoding = "latin-1" if "latin-1" in prefix else "utf-8"
    path.write_bytes(text.encode(encoding))
    spec = importlib.util.spec_from_file_location("odd_api", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    live = module.DashboardRestApi._serialize_dashboard_chart
    assert dp._pins_from_file(str(path)) == dp._pins_for(live)


def test_file_check_last_definition_wins(tmp_path):
    """A redefinition later in the class body is what runtime sees."""
    src = tmp_path / "api.py"
    src.write_text(
        "class DashboardRestApi:\n"
        "    def _serialize_dashboard_chart(self, chart, extra=None):\n"
        "        return {}\n"
        "    def _serialize_dashboard_chart(self, chart):\n"
        "        return {}\n",
        encoding="utf-8",
    )
    params, _ = dp._pins_from_file(str(src))
    assert params == dp._EXPECTED_PARAMS


def test_file_check_reports_unreadable_or_unparsable(tmp_path):
    with pytest.raises(dp.DashboardPatchError, match="must ship .py sources"):
        dp.assert_stock_file(str(tmp_path / "missing.py"))
    bad = tmp_path / "api.py"
    bad.write_text("class DashboardRestApi(:\n", encoding="utf-8")
    with pytest.raises(dp.DashboardPatchError, match="could not parse"):
        dp.assert_stock_file(str(bad))


def test_file_check_rejects_a_reshaped_or_changed_method(tmp_path):
    src = tmp_path / "api.py"
    src.write_text(
        "class DashboardRestApi:\n"
        "    def _serialize_dashboard_chart(self, chart, extra=None):\n"
        "        return {}\n",
        encoding="utf-8",
    )
    with pytest.raises(dp.DashboardPatchError, match="parameter shape changed"):
        dp.assert_stock_file(str(src))
    src.write_text(
        "class DashboardRestApi:\n"
        "    def _serialize_dashboard_chart(self, chart):\n"
        "        return {}\n",
        encoding="utf-8",
    )
    with pytest.raises(dp.DashboardPatchError, match="source changed upstream"):
        dp.assert_stock_file(str(src))
    src.write_text("class Other:\n    pass\n", encoding="utf-8")
    with pytest.raises(dp.DashboardPatchError, match="no DashboardRestApi"):
        dp.assert_stock_file(str(src))


def test_install_is_idempotent(harness, stock_method):
    from superset.dashboards.api import DashboardRestApi

    assert dp.installed() is False
    assert dp.install() is True
    first = DashboardRestApi._serialize_dashboard_chart
    assert dp.installed() is True
    assert dp.install() is True
    assert DashboardRestApi._serialize_dashboard_chart is first, "no double wrap"
    assert first._superset_ownership_original is stock_method
    assert first.__name__ == stock_method.__name__


def _manager_class(harness):
    """Patch the security manager's CLASS, not the session-scoped instance:
    an instance-level monkeypatch leaves a bound method behind in the
    instance __dict__ after undo."""
    return type(harness.app.appbuilder.sm)


def _fake_view(dump):
    return SimpleNamespace(
        chart_entity_response_schema=SimpleNamespace(dump=lambda c: dict(dump))
    )


def test_denied_tile_keeps_its_title_and_names_who_to_ask(
    harness, stock_method, monkeypatch
):
    from superset.dashboards.api import DashboardRestApi

    dp.install()
    with harness.app.app_context():
        monkeypatch.setattr(
            _manager_class(harness), "can_access_chart", lambda self, chart: False
        )
        monkeypatch.setitem(
            harness.app.config,
            "EXTRA_OWNERS_RESOLVER",
            lambda chart: ["Ada Lovelace", "Ben"],
        )
        view = _fake_view({"id": 7, "slice_name": "Revenue", "form_data": {"x": 1}})
        out = DashboardRestApi._serialize_dashboard_chart(view, object())
    assert out == {
        "id": 7,
        "slice_name": "Revenue",
        "has_access": False,
        "owners": ["Ada Lovelace", "Ben"],
    }, "form_data withheld; marker and contacts added"


def test_allowed_tile_is_untouched(harness, stock_method, monkeypatch):
    from superset.dashboards.api import DashboardRestApi

    dp.install()
    with harness.app.app_context():
        monkeypatch.setattr(
            _manager_class(harness), "can_access_chart", lambda self, chart: True
        )
        view = _fake_view({"id": 7, "slice_name": "Revenue", "form_data": {"x": 1}})
        out = DashboardRestApi._serialize_dashboard_chart(view, object())
    assert out == {"id": 7, "slice_name": "Revenue", "form_data": {"x": 1}}


@pytest.mark.parametrize("allowed", [True, False])
def test_wrapper_restates_the_stock_method_exactly(
    harness, stock_method, monkeypatch, allowed
):
    """Differential: on the same input the wrapper's output is the stock
    method's output plus, on denial, exactly the two marker keys -- so a
    future pin bump that forgets to re-derive the restated lines fails
    here rather than in production."""
    from superset.dashboards.api import DashboardRestApi

    dp.install()
    with harness.app.app_context():
        monkeypatch.setattr(
            _manager_class(harness), "can_access_chart", lambda self, chart: allowed
        )
        monkeypatch.setitem(harness.app.config, "EXTRA_OWNERS_RESOLVER", None)
        chart = SimpleNamespace(owners=["Owner"], created_by=None)
        dump = {"id": 7, "slice_name": "Revenue", "form_data": {"x": 1}, "k": "v"}
        stock = stock_method(_fake_view(dump), chart)
        wrapped = DashboardRestApi._serialize_dashboard_chart(_fake_view(dump), chart)
    if allowed:
        assert wrapped == stock
    else:
        assert wrapped == {**stock, "has_access": False, "owners": ["Owner"]}


def test_contact_names_fall_back_from_resolver_to_owners_to_creator(
    harness, monkeypatch
):
    with harness.app.app_context():
        monkeypatch.setitem(harness.app.config, "EXTRA_OWNERS_RESOLVER", None)
        res = SimpleNamespace(owners=["Zed", "Amy", " "], created_by="Creator")
        assert dp.get_access_contact_names(res) == ["Amy", "Zed"]
        res = SimpleNamespace(owners=[], created_by="Creator")
        assert dp.get_access_contact_names(res) == ["Creator"]

        def boom(chart):
            raise RuntimeError("directory down")

        monkeypatch.setitem(harness.app.config, "EXTRA_OWNERS_RESOLVER", boom)
        res = SimpleNamespace(owners=["Owner"], created_by=None)
        assert dp.get_access_contact_names(res) == ["Owner"], (
            "a raising resolver falls back"
        )


# --- integration: the failure split ----------------------------------------


def test_web_process_refuses_to_start_on_drift(harness, stock_method, monkeypatch):
    monkeypatch.setattr(dp, "_EXPECTED_SOURCE_SHA256", "0" * 64)
    monkeypatch.setattr(dp.sys, "argv", ["gunicorn", "superset.app:create_app()"])
    with pytest.raises(dp.DashboardPatchError):
        dp.install()
    assert dp.installed() is False


def test_check_boots_without_the_wrap_and_reports_it(
    harness, stock_method, monkeypatch, caplog
):
    monkeypatch.setattr(dp, "_EXPECTED_SOURCE_SHA256", "0" * 64)
    monkeypatch.setattr(dp.sys, "argv", ["superset", "ownership", "check"])
    with caplog.at_level(logging.ERROR, logger="superset_ownership.dashboard_patch"):
        assert dp.install() is False
    assert dp.installed() is False
    assert "NOT installed" in caplog.text
    with harness.ctx():
        from superset_ownership.lifecycle import check_consistency

        report = check_consistency()
    assert report["dashboard_chart_patch_installed"] is False
    assert report["ok"] is False, "a missing wrap fails the check"


def test_check_reports_the_wrap_when_installed(harness, stock_method):
    dp.install()
    with harness.ctx():
        from superset_ownership.lifecycle import check_consistency

        report = check_consistency()
    assert report["dashboard_chart_patch_installed"] is True

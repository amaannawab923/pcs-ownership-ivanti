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
"""flags.py: one switch, and the report that it stayed one (issue #69).

Pure: a mapping stands in for the Flask config, a namespace for the app and
a stub for Superset's feature-flag manager (so the runtime half is tested
without Superset and without the process-global manager). The deployed
config's derivation, the real manager with a real hook and the CLI's use of
this are asserted against a real Superset in test_endpoints.py (sections 14
to 16).
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from superset_ownership import flags

LOGGER = "superset_ownership.flags"

AGREE_ON = {
    "enabled_backend": True,
    "enabled_ui": True,
    "enabled_ui_runtime": None,
    "ui_flag_hooked": False,
    "ui_flag_hooks": [],
    "flags_agree": True,
}


def _app(enabled, flag, extra_flags=None, **config):
    cfg = {"OWNERSHIP_ENABLED": enabled, "FEATURE_FLAGS": {**(extra_flags or {})}}
    if flag is not None:
        cfg["FEATURE_FLAGS"]["OBJECT_OWNERSHIP"] = flag
    cfg.update(config)
    return SimpleNamespace(config=cfg)


class _Manager:
    """Superset's FeatureFlagManager as this module uses it: one method."""

    def __init__(self, answer):
        self.answer = answer
        self.asked = []

    def is_feature_enabled(self, name):
        self.asked.append(name)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture
def no_manager(monkeypatch):
    """No Superset to ask: the runtime value is not evaluated."""
    monkeypatch.setattr(flags, "_feature_flag_manager", lambda: None)


@pytest.fixture
def manager(monkeypatch):
    """A stub manager the test sets the answer on."""
    stub = _Manager(None)
    monkeypatch.setattr(flags, "_feature_flag_manager", lambda: stub)
    return stub


@pytest.fixture
def flask_app():
    pytest.importorskip("flask")
    from flask import Flask

    app = Flask("ownership-flags-test")
    app.config["OWNERSHIP_ENABLED"] = False
    app.config["FEATURE_FLAGS"] = {"OBJECT_OWNERSHIP": False}
    return app


def _records(caplog):
    return [r for r in caplog.records if r.name == LOGGER]


# --- the switch's own rule -----------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        ("true", True),
        (" TRUE ", True),
        (False, False),
        (None, False),
        ("false", False),  # a string is not truthy here
        ("1", False),
        ("yes", False),
        ("", False),
        (1, False),
    ],
)
def test_as_bool_is_the_config_rule_only_the_literal_true_is_on(value, expected):
    assert flags.as_bool(value) is expected


def test_effective_parses_the_switch_by_that_rule_not_by_truthiness():
    assert flags.effective(_app("false", False).config)["enabled_backend"] is False
    assert flags.effective(_app("true", True).config)["enabled_backend"] is True
    assert flags.effective(_app(True, True).config)["enabled_backend"] is True


# --- effective -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("enabled", "flag", "expected"),
    [
        (True, True, (True, True, True)),
        (False, False, (False, False, True)),
        (True, False, (True, False, False)),
        (False, True, (False, True, False)),
        # unset is off, on either side
        (False, None, (False, False, True)),
        (True, None, (True, False, False)),
    ],
    ids=[
        "both-on",
        "both-off",
        "ui-off",
        "backend-off",
        "flag-unset",
        "flag-unset-backend-on",
    ],
)
def test_effective_reports_both_and_whether_they_agree(enabled, flag, expected):
    backend, ui, agree = expected
    assert flags.effective(_app(enabled, flag).config) == {
        **AGREE_ON,
        "enabled_backend": backend,
        "enabled_ui": ui,
        "flags_agree": agree,
    }


def test_effective_tolerates_a_config_with_no_feature_flags_at_all():
    assert flags.effective({}) == {
        **AGREE_ON,
        "enabled_backend": False,
        "enabled_ui": False,
    }
    assert flags.effective({"FEATURE_FLAGS": None, "OWNERSHIP_ENABLED": "true"}) == {
        **AGREE_ON,
        "enabled_backend": True,
        "enabled_ui": False,
        "flags_agree": False,
    }


def test_effective_without_an_app_reports_both_off_rather_than_raising():
    pytest.importorskip("flask")
    expected = {**AGREE_ON, "enabled_backend": False, "enabled_ui": False}
    assert flags.effective() == expected
    assert flags.report() == expected, "check_consistency's spelling of the same"


@pytest.mark.parametrize(
    ("enabled", "flag", "runtime", "agree"),
    [
        (False, False, True, False),  # probe D: hook turns the UI on, backend off
        (True, True, False, False),  # probe D2: hook turns the UI off, backend on
        (True, True, True, True),  # a hook that leaves it alone
        (False, False, None, True),  # not evaluated: not counted against
        (True, False, True, False),  # static disagreement stays one
    ],
    ids=["D", "D2", "hook-agrees", "not-evaluated", "static-split"],
)
def test_effective_counts_the_runtime_value_against_agreement(
    enabled, flag, runtime, agree
):
    state = flags.effective(_app(enabled, flag).config, runtime_ui=runtime)
    assert state["enabled_ui_runtime"] is runtime
    assert state["flags_agree"] is agree


@pytest.mark.parametrize(
    ("hooks", "expected"),
    [
        ({}, []),
        ({"GET_FEATURE_FLAGS_FUNC": lambda f: f}, ["GET_FEATURE_FLAGS_FUNC"]),
        ({"IS_FEATURE_ENABLED_FUNC": lambda n, d: d}, ["IS_FEATURE_ENABLED_FUNC"]),
        (
            {
                "GET_FEATURE_FLAGS_FUNC": lambda f: f,
                "IS_FEATURE_ENABLED_FUNC": lambda n, d: d,
            },
            ["GET_FEATURE_FLAGS_FUNC", "IS_FEATURE_ENABLED_FUNC"],
        ),
        ({"GET_FEATURE_FLAGS_FUNC": None}, []),  # Superset's default: unset
    ],
)
def test_effective_names_the_configured_runtime_hooks(hooks, expected):
    state = flags.effective(_app(True, True, **hooks).config)
    assert state["ui_flag_hooks"] == expected
    assert state["ui_flag_hooked"] is bool(expected)


# --- runtime_ui_flag / report --------------------------------------------------


def test_runtime_flag_is_not_evaluated_without_a_manager_or_an_app(no_manager):
    assert flags.runtime_ui_flag(_app(True, True)) is None


def test_runtime_flag_is_not_evaluated_on_an_app_with_no_context(manager):
    manager.answer = True
    assert flags.runtime_ui_flag(_app(True, True)) is None
    assert manager.asked == []


def test_runtime_flag_asks_the_manager_under_an_app_context(manager, flask_app):
    manager.answer = True
    assert flags.runtime_ui_flag(flask_app) is True
    assert manager.asked == ["OBJECT_OWNERSHIP"]
    assert flags.report(flask_app) == {
        **AGREE_ON,
        "enabled_backend": False,
        "enabled_ui": False,
        "enabled_ui_runtime": True,
        "flags_agree": False,
    }


def test_a_hook_that_raises_headless_is_reported_not_fatal(manager, flask_app, caplog):
    """A per-user hook that assumes a request (g.user) raises under a bare
    app context; the report says the runtime value is unknown and the boot
    goes on."""
    manager.answer = AttributeError("'_AppCtxGlobals' object has no attribute 'user'")
    caplog.set_level(logging.INFO, logger=LOGGER)
    assert flags.runtime_ui_flag(flask_app) is None
    (line,) = _records(caplog)
    assert line.levelno == logging.WARNING
    assert "could not be evaluated" in line.getMessage()
    assert "AttributeError" in line.getMessage()


# --- warn_if_flags_disagree ----------------------------------------------------


def test_agreeing_switches_log_one_info_line_naming_both(
    caplog, monkeypatch, no_manager
):
    monkeypatch.delenv(flags.FLAG_ENV_OVERRIDE, raising=False)
    monkeypatch.delenv(flags.SWITCH, raising=False)
    caplog.set_level(logging.INFO, logger=LOGGER)

    state = flags.warn_if_flags_disagree(_app(True, True))

    assert state["flags_agree"] is True
    records = _records(caplog)
    assert [r.levelno for r in records] == [logging.INFO]
    message = records[0].getMessage()
    assert "OWNERSHIP_ENABLED=True" in message
    assert "FEATURE_FLAGS['OBJECT_OWNERSHIP']=True" in message
    assert "derived" in message


def test_both_off_is_agreement_too(caplog, monkeypatch, no_manager):
    monkeypatch.delenv(flags.FLAG_ENV_OVERRIDE, raising=False)
    monkeypatch.delenv(flags.SWITCH, raising=False)
    caplog.set_level(logging.INFO, logger=LOGGER)
    flags.warn_if_flags_disagree(_app(False, None))
    records = _records(caplog)
    assert [r.levelno for r in records] == [logging.INFO]
    assert "OWNERSHIP_ENABLED=False" in records[0].getMessage()
    assert "FEATURE_FLAGS['OBJECT_OWNERSHIP']=False" in records[0].getMessage()


def test_ui_on_backend_off_warns_about_404s(caplog, monkeypatch, no_manager):
    """The state a hand-set flag produces on an instance that never loaded
    the module: the list pages add columns and a control with nothing
    behind them."""
    monkeypatch.delenv(flags.FLAG_ENV_OVERRIDE, raising=False)
    monkeypatch.delenv(flags.SWITCH, raising=False)
    caplog.set_level(logging.INFO, logger=LOGGER)

    state = flags.warn_if_flags_disagree(_app(False, True))

    assert state == {
        **AGREE_ON,
        "enabled_backend": False,
        "enabled_ui": True,
        "flags_agree": False,
    }
    records = _records(caplog)
    assert [r.levelno for r in records] == [logging.WARNING]
    message = records[0].getMessage()
    assert (
        "OWNERSHIP_ENABLED=False but FEATURE_FLAGS['OBJECT_OWNERSHIP']=True" in message
    )
    assert "404s" in message
    assert "overridden after the ownership config derived it" in message
    assert "must not be set on its own" in message


def test_backend_on_ui_off_warns_about_enforcement_with_no_ui(
    caplog, monkeypatch, no_manager
):
    monkeypatch.delenv(flags.FLAG_ENV_OVERRIDE, raising=False)
    monkeypatch.delenv(flags.SWITCH, raising=False)
    caplog.set_level(logging.INFO, logger=LOGGER)

    state = flags.warn_if_flags_disagree(_app(True, False))

    assert state["flags_agree"] is False
    records = _records(caplog)
    assert [r.levelno for r in records] == [logging.WARNING]
    message = records[0].getMessage()
    assert (
        "OWNERSHIP_ENABLED=True but FEATURE_FLAGS['OBJECT_OWNERSHIP']=False" in message
    )
    assert "born private" in message
    assert "no control in the UI" in message


@pytest.mark.parametrize(
    ("enabled", "hook", "runtime", "consequence"),
    [
        (False, "GET_FEATURE_FLAGS_FUNC", True, "404s"),  # probe D
        (True, "IS_FEATURE_ENABLED_FUNC", False, "born private"),  # probe D2
    ],
    ids=["D-backend-off-hook-turns-ui-on", "D2-backend-on-hook-turns-ui-off"],
)
def test_a_hook_that_moves_the_flag_is_a_warning_naming_the_hook(
    enabled, hook, runtime, consequence, caplog, monkeypatch, manager, flask_app
):
    """The static dict agrees; the hook the frontend reads through does not.
    Two WARNINGs: the split, naming the hook, the runtime value and the
    failure state; and the standing one that a hook is configured."""
    monkeypatch.delenv(flags.FLAG_ENV_OVERRIDE, raising=False)
    monkeypatch.delenv(flags.SWITCH, raising=False)
    flask_app.config["OWNERSHIP_ENABLED"] = enabled
    flask_app.config["FEATURE_FLAGS"] = {"OBJECT_OWNERSHIP": enabled}
    flask_app.config[hook] = object()
    manager.answer = runtime
    caplog.set_level(logging.INFO, logger=LOGGER)

    state = flags.warn_if_flags_disagree(flask_app)

    assert state["enabled_ui_runtime"] is runtime
    assert state["ui_flag_hooks"] == [hook]
    assert state["flags_agree"] is False
    records = _records(caplog)
    assert [r.levelno for r in records] == [logging.WARNING, logging.WARNING]
    split, standing = (r.getMessage() for r in records)
    assert (
        f"OWNERSHIP_ENABLED={enabled} and FEATURE_FLAGS['OBJECT_OWNERSHIP']={enabled} "
        "agree"
    ) in split
    assert f"{hook} evaluates the flag to {runtime} at runtime" in split
    assert "that is the value the UI reads" in split
    assert consequence in split
    assert f"{hook} is set" in standing
    assert "evaluated per request" in standing
    assert f"anonymous caller ({runtime})" in standing


def test_a_hook_that_agrees_still_gets_the_standing_warning(
    caplog, monkeypatch, manager, flask_app
):
    monkeypatch.delenv(flags.FLAG_ENV_OVERRIDE, raising=False)
    monkeypatch.delenv(flags.SWITCH, raising=False)
    flask_app.config["GET_FEATURE_FLAGS_FUNC"] = object()
    manager.answer = False
    caplog.set_level(logging.INFO, logger=LOGGER)

    state = flags.warn_if_flags_disagree(flask_app)

    assert state["flags_agree"] is True
    assert state["ui_flag_hooked"] is True
    records = _records(caplog)
    assert [r.levelno for r in records] == [logging.INFO, logging.WARNING]
    assert "one switch" in records[0].getMessage()
    assert "GET_FEATURE_FLAGS_FUNC is set" in records[1].getMessage()
    assert "can disagree per user without a line here" in records[1].getMessage()


@pytest.mark.parametrize(
    ("raw", "enabled", "warns"),
    [
        ("true", False, True),  # asked for the UI, backend off: ignored, say so
        ("false", True, True),  # asked for no UI, backend on: ignored, say so
        ("TRUE", True, False),  # agrees with the derivation: nothing to say
        ("", False, False),  # blank is false, agrees
        ("1", False, False),  # "1" is not true (the strict-string rule), agrees
    ],
)
def test_the_generic_env_override_is_reported_as_ignored_when_it_disagrees(
    raw, enabled, warns, caplog, monkeypatch, no_manager
):
    """The light config honours SUPERSET_FEATURE_<NAME> for every flag but
    this one, which the derivation overwrites. An operator who set it and
    sees nothing change gets a line saying which setting to use instead."""
    monkeypatch.setenv(flags.FLAG_ENV_OVERRIDE, raw)
    monkeypatch.delenv(flags.SWITCH, raising=False)
    caplog.set_level(logging.INFO, logger=LOGGER)

    flags.warn_if_flags_disagree(_app(enabled, enabled))

    records = _records(caplog)
    assert records[0].levelno == logging.INFO, "the switches themselves agree"
    ignored = [r for r in records[1:] if "ignored" in r.getMessage()]
    assert len(ignored) == (1 if warns else 0)
    if warns:
        assert ignored[0].levelno == logging.WARNING
        assert f"SUPERSET_FEATURE_OBJECT_OWNERSHIP={raw!r}" in ignored[0].getMessage()
        assert "Set OWNERSHIP_ENABLED instead" in ignored[0].getMessage()


@pytest.mark.parametrize(
    ("inherited", "enabled", "warns"),
    [
        (True, False, True),  # an earlier layer turned the UI on; backend off
        (False, True, True),  # an earlier layer turned it off; backend on
        (True, True, False),  # agrees with the derivation
        (None, False, False),  # no earlier layer said anything
    ],
)
def test_an_earlier_layers_flag_is_reported_as_ignored_when_it_disagrees(
    inherited, enabled, warns, caplog, monkeypatch, no_manager
):
    """The Python spelling of the same mistake: FEATURE_FLAGS["OBJECT_OWNERSHIP"]
    in superset_config.py or superset_config_docker.py. The config records
    what it found before the derivation overwrote it; the same "ignored"
    line fires."""
    monkeypatch.delenv(flags.FLAG_ENV_OVERRIDE, raising=False)
    monkeypatch.delenv(flags.SWITCH, raising=False)
    caplog.set_level(logging.INFO, logger=LOGGER)

    flags.warn_if_flags_disagree(
        _app(enabled, enabled, **{flags.INHERITED_FLAG: inherited})
    )

    ignored = [r for r in _records(caplog) if "ignored" in r.getMessage()]
    assert len(ignored) == (1 if warns else 0)
    if warns:
        assert ignored[0].levelno == logging.WARNING
        message = ignored[0].getMessage()
        assert (
            f"FEATURE_FLAGS['OBJECT_OWNERSHIP']={inherited!r} set by an earlier "
            "config layer"
        ) in message
        assert "Set OWNERSHIP_ENABLED instead" in message


@pytest.mark.parametrize(
    ("source", "raw", "enabled", "level"),
    [
        # layer said off, env asked for on: an instruction refused -- WARNING
        ("config", "true", False, logging.WARNING),
        ("config", " True ", False, logging.WARNING),
        # layer said on, env carries compose's default "false": not a
        # conflict, nothing to remove -- an INFO account (probe F)
        ("config", "false", True, logging.INFO),
        ("config", "", True, logging.INFO),
        ("config", "true", True, None),  # both say on
        ("config", None, True, None),  # env unset (probe F2)
        ("environment", "false", False, None),  # env decided: nothing ignored
        (None, "true", False, None),  # a config that records no source
    ],
)
def test_the_env_switch_is_reported_when_a_config_layer_won(
    source, raw, enabled, level, caplog, monkeypatch, no_manager
):
    """OWNERSHIP_ENABLED set both as a Python setting in an earlier layer and
    in the environment: the layer wins (the config records the source). An
    environment value that ASKED for the non-default and was refused is a
    WARNING with the remedy; the default the compose file injects into every
    process, against a layer that turned the feature on, is an INFO line
    with no remedy -- there is nothing the operator can remove."""
    monkeypatch.delenv(flags.FLAG_ENV_OVERRIDE, raising=False)
    if raw is None:
        monkeypatch.delenv(flags.SWITCH, raising=False)
    else:
        monkeypatch.setenv(flags.SWITCH, raw)
    caplog.set_level(logging.INFO, logger=LOGGER)

    flags.warn_if_flags_disagree(
        _app(enabled, enabled, **{flags.SWITCH_SOURCE: source})
    )

    lines = [r for r in _records(caplog) if "config layer wins" in r.getMessage()]
    assert not [r for r in _records(caplog) if "not a recognised" in r.getMessage()]
    if level is None:
        assert lines == []
        return
    (line,) = lines
    assert line.levelno == level
    message = line.getMessage()
    assert f"set OWNERSHIP_ENABLED={enabled} as a Python setting" in message
    if level == logging.WARNING:
        assert f"OWNERSHIP_ENABLED={raw!r} in the environment is ignored" in message
        assert "Remove one of the two" in message
    else:
        assert f"OWNERSHIP_ENABLED={raw!r} in the environment is the default" in message
        assert "Remove" not in message, "no remedy: nothing to remove"


@pytest.mark.parametrize(
    "raw", ["1", "yes", "on", "enabled", "0", "no", "off", "True!", "t"]
)
@pytest.mark.parametrize("source", ["environment", "config"])
def test_an_unrecognised_switch_spelling_is_off_and_says_so(
    raw, source, caplog, monkeypatch, no_manager
):
    """Probe G: OWNERSHIP_ENABLED=1 used to be off with no boot line. Every
    spelling that is neither "true" nor "false" is still off (the literal
    rule stands) and gets a WARNING naming the accepted spellings; when a
    layer set the switch the line says that value won instead."""
    assert flags.as_bool(raw) is False
    assert flags.is_recognised_spelling(raw) is False
    monkeypatch.delenv(flags.FLAG_ENV_OVERRIDE, raising=False)
    monkeypatch.setenv(flags.SWITCH, raw)
    caplog.set_level(logging.INFO, logger=LOGGER)
    enabled = source == "config"  # a layer that turned it on; else env decided: off

    state = flags.warn_if_flags_disagree(
        _app(enabled, enabled, **{flags.SWITCH_SOURCE: source})
    )

    assert state["enabled_backend"] is enabled
    records = _records(caplog)
    assert [r.levelno for r in records] == [logging.INFO, logging.WARNING]
    message = records[1].getMessage()
    assert f"OWNERSHIP_ENABLED={raw!r} in the environment is not a recognised" in (
        message
    )
    assert "'true' and 'false'" in message
    if source == "config":
        assert "set OWNERSHIP_ENABLED=True as a Python setting, which wins" in message
        assert "read as off" not in message
    else:
        assert "read as off" in message
        assert "Set OWNERSHIP_ENABLED=true" in message
    assert not [r for r in records if "config layer wins" in r.getMessage()], (
        "one line for a value that was not understood, not two"
    )


@pytest.mark.parametrize(
    ("raw", "enabled"),
    [("true", True), ("TRUE", True), (" true ", True), ("false", False),
     ("False", False), (" FALSE ", False), ("", False)],
)
def test_a_recognised_spelling_gets_no_spelling_warning(
    raw, enabled, caplog, monkeypatch, no_manager
):
    """The accepted spellings, whitespace and case included: off or on by
    the literal rule, and no line about the spelling."""
    assert flags.is_recognised_spelling(raw) is True
    assert flags.as_bool(raw) is enabled
    monkeypatch.delenv(flags.FLAG_ENV_OVERRIDE, raising=False)
    monkeypatch.setenv(flags.SWITCH, raw)
    caplog.set_level(logging.INFO, logger=LOGGER)
    flags.warn_if_flags_disagree(
        _app(enabled, enabled, **{flags.SWITCH_SOURCE: "environment"})
    )
    assert [r.levelno for r in _records(caplog)] == [logging.INFO]


# --- the flags-only CLI --------------------------------------------------------


def _invoke(app, *args):
    result = app.test_cli_runner().invoke(app.cli, list(args))
    import json  # noqa: TID251 - stdlib on purpose

    payload = json.loads(result.output) if result.output.strip() else None
    return result.exit_code, payload


def test_install_cli_registers_status_and_check_only(flask_app, manager):
    flags.install_cli(flask_app)
    group = flask_app.cli.commands["ownership"]
    assert sorted(group.commands) == ["check", "status"]
    assert "feature OFF" in (group.help or "")


def test_flags_only_status_is_a_report_and_check_is_a_gate(
    flask_app, manager, monkeypatch
):
    monkeypatch.delenv(flags.FLAG_ENV_OVERRIDE, raising=False)
    monkeypatch.delenv(flags.SWITCH, raising=False)
    flags.install_cli(flask_app)

    manager.answer = False
    code, status = _invoke(flask_app, "ownership", "status")
    assert code == 0, status
    assert status["backend_loaded"] is False
    assert (status["enabled_backend"], status["enabled_ui"]) == (False, False)
    assert status["enabled_ui_runtime"] is False
    code, check = _invoke(flask_app, "ownership", "check")
    assert code == 0, check
    assert check["ok"] is True

    # probe D from the CLI's side: the backend is off, a hook turned the UI on
    flask_app.config["GET_FEATURE_FLAGS_FUNC"] = object()
    manager.answer = True
    code, check = _invoke(flask_app, "ownership", "check")
    assert code == 1, check
    assert check["ok"] is False
    assert check["enabled_ui_runtime"] is True
    assert check["ui_flag_hooked"] is True
    assert check["flags_agree"] is False
    code, status = _invoke(flask_app, "ownership", "status")
    assert code == 0, "status stays a report"
    assert status["flags_agree"] is False

    # and the static spelling of the same state
    flask_app.config.pop("GET_FEATURE_FLAGS_FUNC")
    manager.answer = True
    flask_app.config["FEATURE_FLAGS"] = {"OBJECT_OWNERSHIP": True}
    code, check = _invoke(flask_app, "ownership", "check")
    assert code == 1
    assert (check["enabled_backend"], check["enabled_ui"]) == (False, True)


# --- the check gate (N-10) ------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "ok", "unknown"),
    [
        ({**AGREE_ON}, True, False),  # no hook, not evaluated: a pass
        ({**AGREE_ON, "flags_agree": False}, False, False),
        (
            {**AGREE_ON, "ui_flag_hooked": True,
             "ui_flag_hooks": ["IS_FEATURE_ENABLED_FUNC"], "enabled_ui_runtime": True},
            True, False,
        ),  # a hook that answered and agrees
        (
            {**AGREE_ON, "ui_flag_hooked": True,
             "ui_flag_hooks": ["IS_FEATURE_ENABLED_FUNC"]},
            False, True,
        ),  # probe I4: a hook that could not be evaluated -- flags_agree, not ok
        (
            {**AGREE_ON, "ui_flag_hooked": True,
             "ui_flag_hooks": ["GET_FEATURE_FLAGS_FUNC"], "ok": True},
            False, True,
        ),  # the ON-state payload: its own ok is a further condition
        ({**AGREE_ON, "ok": False}, False, False),  # ... and a false one stays false
    ],
    ids=["no-hook", "disagree", "hook-answered", "I4-hook-raised", "on-state-I4",
         "on-state-not-ok"],
)
def test_gate_fails_when_a_hooks_runtime_answer_is_unknown(state, ok, unknown):
    """`flags_agree` does not count an unevaluated runtime value against
    agreement (the switches did not disagree); `check` does: a hook that
    could not be evaluated leaves the half the gate exists to check unknown,
    and unknown is not a pass."""
    assert flags.runtime_unknown(state) is unknown
    verdict = flags.gate(state)
    assert verdict["ok"] is ok
    assert verdict["flags_agree"] is state["flags_agree"], "the report is unchanged"
    if unknown:
        message = verdict["runtime_unknown"]
        assert state["ui_flag_hooks"][0] in message
        assert "the hook raised" in message
        assert "unknown is not a pass" in message
        assert "Verify the flag per user" in message
    else:
        assert "runtime_unknown" not in verdict


def test_flags_only_check_fails_when_the_hook_raises_headless(
    flask_app, manager, monkeypatch, caplog
):
    """Probe I4 from the CLI's side: a per-user hook that reads g.user
    raises under the bare app context `check` evaluates it in. `status`
    reports `enabled_ui_runtime` null and stays a report; `check` exits 1
    with `runtime_unknown` saying the hook raised and what to verify."""
    monkeypatch.delenv(flags.FLAG_ENV_OVERRIDE, raising=False)
    monkeypatch.delenv(flags.SWITCH, raising=False)
    flags.install_cli(flask_app)
    flask_app.config["IS_FEATURE_ENABLED_FUNC"] = object()
    manager.answer = AttributeError("'_AppCtxGlobals' object has no attribute 'user'")
    caplog.set_level(logging.INFO, logger=LOGGER)

    code, status = _invoke(flask_app, "ownership", "status")
    assert code == 0, status
    assert status["enabled_ui_runtime"] is None
    assert status["ui_flag_hooked"] is True
    assert status["flags_agree"] is True
    assert "ok" not in status
    assert "runtime_unknown" not in status

    code, check = _invoke(flask_app, "ownership", "check")
    assert code == 1, check
    assert check["ok"] is False
    assert check["flags_agree"] is True, "they did not disagree; the value is unknown"
    assert check["enabled_ui_runtime"] is None
    assert check["backend_loaded"] is False
    assert "IS_FEATURE_ENABLED_FUNC is configured" in check["runtime_unknown"]
    assert "the hook raised" in check["runtime_unknown"]
    assert "Verify the flag per user" in check["runtime_unknown"]
    evaluated = [
        r.levelno
        for r in _records(caplog)
        if "could not be evaluated" in r.getMessage()
    ]
    assert evaluated == [logging.WARNING] * 2, "one per evaluation: status and check"

    # the same hook answering is a pass again
    manager.answer = False
    code, check = _invoke(flask_app, "ownership", "check")
    assert code == 0, check
    assert check["ok"] is True
    assert "runtime_unknown" not in check


def test_install_cli_does_not_shadow_the_full_group(flask_app, caplog):
    """click's add_command replaces by name, so the flags-only group
    registered AFTER cli.install would silently turn ten commands into two.
    Nothing calls them in that order; if something does, the flags-only
    install is a no-op with a debug line. The other order (flags-only first,
    then the full group) still ends with the full group, and the flags-only
    install stays idempotent over itself."""
    from superset_ownership import cli

    caplog.set_level(logging.DEBUG, logger=LOGGER)
    flags.install_cli(flask_app)
    flags.install_cli(flask_app)
    assert sorted(flask_app.cli.commands["ownership"].commands) == ["check", "status"]

    cli.install(flask_app)
    assert flask_app.cli.commands["ownership"] is cli.ownership
    caplog.clear()

    flags.install_cli(flask_app)
    assert flask_app.cli.commands["ownership"] is cli.ownership, "not shadowed"
    assert "enable" in flask_app.cli.commands["ownership"].commands
    (line,) = _records(caplog)
    assert line.levelno == logging.DEBUG
    assert "already registered" in line.getMessage()
    assert not [
        r for r in caplog.records
        if r.name == LOGGER and "registered (the switch report only)" in r.getMessage()
    ]


def test_flags_imports_nothing_else_from_the_package():
    """It is the one module a feature-off process loads; loading it must
    not drag the hooks, the guard or the blueprint in with it. Core
    Superset's feature-flag manager is the one thing it reaches for, lazily
    and guarded."""
    with open(flags.__file__, encoding="utf-8") as fh:
        offenders = [
            ln.strip()
            for ln in fh
            if ln.lstrip().startswith(
                ("from superset_ownership", "import superset_ownership")
            )
        ]
    assert offenders == []

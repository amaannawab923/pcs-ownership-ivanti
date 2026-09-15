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
"""One precedence rule, tested from every angle: layer/app.config x
environment x default, for every parser, plus the legacy-boolean matrix
(section 3.1) and the no-app-context path."""

from __future__ import annotations

import logging

import pytest
from superset_ownership import settings

# --------------------------------------------------------------------------- precedence


def test_layer_wins_over_environment(monkeypatch):
    monkeypatch.setenv("OWNERSHIP_X", "from-env")
    assert (
        settings.get("OWNERSHIP_X", layer={"OWNERSHIP_X": "from-layer"}) == "from-layer"
    )


def test_environment_is_the_default_when_layer_is_unset(monkeypatch):
    monkeypatch.setenv("OWNERSHIP_X", "from-env")
    assert settings.get("OWNERSHIP_X", layer={}) == "from-env"


def test_default_when_neither_layer_nor_environment_set(monkeypatch):
    monkeypatch.delenv("OWNERSHIP_X", raising=False)
    assert settings.get("OWNERSHIP_X", "fallback", layer={}) == "fallback"


def test_blank_string_at_a_layer_means_not_set_here(monkeypatch):
    monkeypatch.setenv("OWNERSHIP_X", "from-env")
    assert settings.get("OWNERSHIP_X", layer={"OWNERSHIP_X": ""}) == "from-env"


def test_none_at_the_layer_falls_through_to_environment(monkeypatch):
    monkeypatch.setenv("OWNERSHIP_X", "from-env")
    assert settings.get("OWNERSHIP_X", layer={"OWNERSHIP_X": None}) == "from-env"


def test_default_is_returned_as_is_never_parsed(monkeypatch):
    monkeypatch.delenv("OWNERSHIP_X", raising=False)
    sentinel = object()
    assert settings.get("OWNERSHIP_X", sentinel, settings.as_int, layer={}) is sentinel


def test_app_config_is_the_layer_in_an_app_context(monkeypatch):
    flask = pytest.importorskip("flask")
    monkeypatch.setenv("OWNERSHIP_X", "from-env")
    app = flask.Flask(__name__)
    app.config["OWNERSHIP_X"] = "from-app-config"
    with app.app_context():
        assert settings.get("OWNERSHIP_X") == "from-app-config"


def test_no_app_context_skips_straight_to_environment(monkeypatch):
    monkeypatch.setenv("OWNERSHIP_X", "from-env")
    assert settings.get("OWNERSHIP_X") == "from-env"


def test_source_reports_which_level_answered(monkeypatch):
    monkeypatch.delenv("OWNERSHIP_X", raising=False)
    assert settings.source("OWNERSHIP_X", layer={}) == "default"
    monkeypatch.setenv("OWNERSHIP_X", "v")
    assert settings.source("OWNERSHIP_X", layer={}) == "environment"
    assert settings.source("OWNERSHIP_X", layer={"OWNERSHIP_X": "v"}) == "config"


# --------------------------------------------------------------------------- as_str


def test_as_str_strips_and_blanks_to_none():
    assert settings.as_str("  hi  ") == "hi"
    assert settings.as_str("") is None
    assert settings.as_str("   ") is None
    assert settings.as_str(None) is None


# -------------------------------------------------------------------- as_int / as_float


@pytest.mark.parametrize("raw,expected", [("5", 5), (5, 5), (" -3 ", -3)])
def test_as_int_parses(raw, expected):
    assert settings.as_int(raw) == expected


def test_as_int_raises_setting_error_on_garbage():
    with pytest.raises(settings.SettingError):
        settings.as_int("not-a-number")


def test_as_float_parses_and_raises():
    assert settings.as_float("1.5") == 1.5
    with pytest.raises(settings.SettingError):
        settings.as_float("nope")


def test_get_catches_setting_error_and_warns(monkeypatch, caplog):
    monkeypatch.setenv("OWNERSHIP_N", "not-a-number")
    with caplog.at_level(logging.WARNING, logger="superset_ownership.settings"):
        assert settings.get("OWNERSHIP_N", 7, settings.as_int, layer={}) == 7
    assert any("OWNERSHIP_N" in r.message for r in caplog.records)


# --------------------------------------------- as_bool (strict, OWNERSHIP_ENABLED only)


@pytest.mark.parametrize("raw", ["true", "True", True])
def test_as_bool_strict_on(raw):
    assert settings.as_bool(raw) is True


@pytest.mark.parametrize("raw", ["false", "False", "", False])
def test_as_bool_strict_off(raw):
    assert settings.as_bool(raw) is False


@pytest.mark.parametrize("raw", ["1", "yes", "on", "0", "no", "maybe"])
def test_as_bool_strict_raises_on_anything_else(raw):
    with pytest.raises(settings.SettingError):
        settings.as_bool(raw)


def test_get_with_as_bool_warns_and_uses_default_on_a_bad_spelling(monkeypatch, caplog):
    monkeypatch.setenv("OWNERSHIP_ENABLED", "1")
    with caplog.at_level(logging.WARNING, logger="superset_ownership.settings"):
        assert (
            settings.get("OWNERSHIP_ENABLED", False, settings.as_bool, layer={})
            is False
        )
    assert len(caplog.records) == 1


# --------------------------------------------------------- as_legacy_bool (section 3.1)

_LEGACY_ON = ["1", "true", "yes", "on"]
_LEGACY_OFF = ["0", "false", "no", "off", ""]


@pytest.mark.parametrize("raw", _LEGACY_ON)
def test_as_legacy_bool_on_spellings(raw):
    assert settings.as_legacy_bool(raw) is True


@pytest.mark.parametrize("raw", _LEGACY_OFF)
def test_as_legacy_bool_off_spellings(raw):
    assert settings.as_legacy_bool(raw) is False


def test_as_legacy_bool_accepts_real_bools():
    assert settings.as_legacy_bool(True) is True
    assert settings.as_legacy_bool(False) is False


def test_as_legacy_bool_raises_on_an_unrecognised_spelling():
    with pytest.raises(settings.SettingError):
        settings.as_legacy_bool("maybe")


@pytest.mark.parametrize(
    "key,default",
    [
        ("OWNERSHIP_OUTBOX_ENABLED", True),
        ("OWNERSHIP_AUTO_MIGRATE", True),
        ("OWNERSHIP_OUTBOX_CELERY", False),
        ("OWNERSHIP_REPAIR_ON_START", False),
    ],
)
@pytest.mark.parametrize("raw", _LEGACY_ON)
def test_legacy_keys_on_spelling_is_on_regardless_of_default(
    monkeypatch, key, default, raw
):
    monkeypatch.setenv(key, raw)
    assert settings.get(key, default, settings.as_legacy_bool, layer={}) is True


@pytest.mark.parametrize(
    "key,default",
    [
        ("OWNERSHIP_OUTBOX_ENABLED", True),
        ("OWNERSHIP_AUTO_MIGRATE", True),
        ("OWNERSHIP_OUTBOX_CELERY", False),
        ("OWNERSHIP_REPAIR_ON_START", False),
    ],
)
@pytest.mark.parametrize("raw", _LEGACY_OFF)
def test_legacy_keys_off_spelling_is_off_regardless_of_default(
    monkeypatch, key, default, raw
):
    if raw:
        monkeypatch.setenv(key, raw)
    else:
        monkeypatch.delenv(key, raising=False)
    result = settings.get(key, default, settings.as_legacy_bool, layer={})
    # A blank env value is "not set" and falls through to the key's own
    # default; every other off-spelling ("0"/"false"/"no"/"off") is a
    # working value and must read as False even when the default is True.
    if raw == "":
        assert result is default
    else:
        assert result is False


@pytest.mark.parametrize(
    "key,default",
    [
        ("OWNERSHIP_OUTBOX_ENABLED", True),
        ("OWNERSHIP_AUTO_MIGRATE", True),
        ("OWNERSHIP_OUTBOX_CELERY", False),
        ("OWNERSHIP_REPAIR_ON_START", False),
    ],
)
def test_legacy_keys_unrecognised_spelling_is_off_never_the_default(
    monkeypatch, key, default, caplog
):
    monkeypatch.setenv(key, "maybe")
    with caplog.at_level(logging.WARNING, logger="superset_ownership.settings"):
        result = settings.get(key, default, settings.as_legacy_bool, layer={})
    assert result is False, (
        f"{key} default={default} must never be silently re-enabled by a typo"
    )
    assert len(caplog.records) == 1
    assert key in caplog.records[0].message


def test_legacy_key_missing_falls_through_to_its_own_default(monkeypatch):
    monkeypatch.delenv("OWNERSHIP_OUTBOX_ENABLED", raising=False)
    assert (
        settings.get(
            "OWNERSHIP_OUTBOX_ENABLED", True, settings.as_legacy_bool, layer={}
        )
        is True
    )
    assert (
        settings.get(
            "OWNERSHIP_OUTBOX_ENABLED", False, settings.as_legacy_bool, layer={}
        )
        is False
    )


# -------------------------------------------------------- flags.as_bool stays untouched


def test_flags_as_bool_is_a_separate_lenient_function():
    from superset_ownership import flags

    assert flags.as_bool is not settings.as_bool
    # "1" is off under flags.as_bool (lenient: only True/"true" is on), and
    # raises nothing -- no SettingError exists in that function's world.
    assert flags.as_bool("1") is False
    assert flags.as_bool("true") is True


def test_warn_if_flags_disagree_never_raises_on_an_odd_switch_value():
    flask = pytest.importorskip("flask")
    from superset_ownership import flags

    app = flask.Flask("ownership-settings-test")
    app.config["OWNERSHIP_ENABLED"] = "1"
    app.config["FEATURE_FLAGS"] = {"OBJECT_OWNERSHIP": False}
    flags.warn_if_flags_disagree(app)  # must not raise


# ------------------------------------------------------------ as_class_ref / as_mapping


def test_as_class_ref_is_a_passthrough():
    sentinel = object()
    assert settings.as_class_ref(sentinel) is sentinel
    assert settings.as_class_ref("pkg.mod:Class") == "pkg.mod:Class"


def test_as_mapping_passes_through_a_dict():
    d = {"type": "none"}
    assert settings.as_mapping(d) is d


def test_as_mapping_parses_a_json_string():
    assert settings.as_mapping('{"type": "api_token", "token_env": "X"}') == {
        "type": "api_token",
        "token_env": "X",
    }


def test_as_mapping_raises_on_bad_json():
    with pytest.raises(settings.SettingError):
        settings.as_mapping("{not json")


def test_as_mapping_raises_on_a_json_array():
    with pytest.raises(settings.SettingError):
        settings.as_mapping("[1, 2]")


def test_as_mapping_raises_on_an_unrelated_type():
    with pytest.raises(settings.SettingError):
        settings.as_mapping(42)


# --------------------------------------------------------------- get(on_error="raise")


def test_get_on_error_raise_propagates_the_setting_error_uncaught(monkeypatch):
    """B3: a policy key (OWNERSHIP_FGA_CREDENTIALS) must not fall back to a
    silent default on a malformed value -- on_error="raise" is the escape
    hatch from get()'s normal "log a WARNING and use the default" rule."""
    monkeypatch.setenv("OWNERSHIP_X", "{not json")
    with pytest.raises(settings.SettingError, match="not valid JSON"):
        settings.get(
            "OWNERSHIP_X",
            {"type": "none"},
            settings.as_mapping,
            layer={},
            on_error="raise",
        )


def test_get_on_error_raise_does_not_affect_a_value_that_parses_cleanly(monkeypatch):
    monkeypatch.setenv("OWNERSHIP_X", '{"type": "api_token", "token_env": "T"}')
    result = settings.get(
        "OWNERSHIP_X", {"type": "none"}, settings.as_mapping, layer={}, on_error="raise"
    )
    assert result == {"type": "api_token", "token_env": "T"}


def test_get_on_error_default_is_the_untouched_prior_behaviour(monkeypatch, caplog):
    monkeypatch.setenv("OWNERSHIP_X", "{not json")
    with caplog.at_level(logging.WARNING, logger="superset_ownership.settings"):
        result = settings.get(
            "OWNERSHIP_X", {"type": "none"}, settings.as_mapping, layer={}
        )
    assert result == {"type": "none"}
    assert any("OWNERSHIP_X" in r.message for r in caplog.records)


# ---------------------------------------------- N6: legacy-bool by marker, not identity


def test_legacy_bool_marker_attribute_is_set_on_the_real_parser():
    assert settings.as_legacy_bool.is_legacy_bool is True  # type: ignore[attr-defined]


def test_a_wrapped_legacy_bool_parser_keeps_the_unrecognised_is_false_rule(
    monkeypatch, caplog
):
    """A caller that wraps as_legacy_bool (functools.partial, a lambda
    delegating to it) is no longer `is` the original function -- the old
    `parser is as_legacy_bool` identity check would silently fall through
    to get()'s general rule (unrecognised -> the key's own default) for a
    wrapped parser, exactly the regression section 3.1 exists to prevent
    for a True-default legacy key."""

    def wrapped(value):
        return settings.as_legacy_bool(value)

    wrapped.is_legacy_bool = True

    monkeypatch.setenv("OWNERSHIP_X", "maybe")
    with caplog.at_level(logging.WARNING, logger="superset_ownership.settings"):
        result = settings.get("OWNERSHIP_X", True, wrapped, layer={})
    assert result is False, (
        "a wrapped legacy-bool parser must still read 'maybe' as off"
    )


def test_functools_partial_of_as_legacy_bool_keeps_the_unrecognised_is_false_rule(
    monkeypatch, caplog
):
    """R2-3: the module docstring's own named example --
    ``functools.partial(as_legacy_bool)`` -- must hold with NO manual
    marker set by the caller. Round 2 found that a bare `functools.partial`
    does not forward function attributes on its own, so `parser.func` (not
    `parser` itself) is where `is_legacy_bool` actually lives; `get()`
    unwraps one level of `.func` to find it."""
    import functools

    wrapped = functools.partial(settings.as_legacy_bool)
    assert not hasattr(wrapped, "is_legacy_bool"), (
        "functools.partial does not forward attributes -- if this starts "
        "failing, the marker is reached some other way and this test's "
        "premise no longer holds"
    )

    monkeypatch.setenv("OWNERSHIP_X", "maybe")
    with caplog.at_level(logging.WARNING, logger="superset_ownership.settings"):
        result = settings.get("OWNERSHIP_X", True, wrapped, layer={})
    assert result is False, (
        "functools.partial(as_legacy_bool) must still read 'maybe' as off, "
        "per the docstring's own example"
    )

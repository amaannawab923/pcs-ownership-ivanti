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
"""One switch for the feature, and the check that it stayed one.

The spec (section 11) has a single switch, `FEATURE_FLAGS["OBJECT_OWNERSHIP"]`.
This module had two: `OWNERSHIP_ENABLED` decides whether the backend loads at
all (hooks, blueprint, tables, guard) and the feature flag decides whether the
UI shows the ownership surfaces (list columns, the sharing action and drawer).
Set independently they can disagree, and both disagreements are worse than
either half alone:

  backend off, UI on   every ownership call the list pages make 404s; the
                       Owner and Sharing columns render "Unknown" on every
                       row and the sharing control does nothing
  backend on, UI off   objects are born private and access is enforced, with
                       no control anywhere to see who owns what or to share

So the config wiring DERIVES the flag from the switch (`FEATURE_FLAGS
["OBJECT_OWNERSHIP"] = OWNERSHIP_ENABLED`) and nothing sets it independently.
What this module adds is the check that the derivation held once every config
layer has run, and that it holds at RUNTIME: `warn_if_flags_disagree` is
called from FLASK_APP_MUTATOR, in both states of the switch, and logs one line
with the effective value of each. INFO when they agree; WARNING, naming the
failure mode, when a later layer overrode the flag by hand. `superset
ownership status` and `check` report the same values, and `check` fails when
they differ.

THE FLAG THE UI READS IS NOT THE STATIC DICT. The frontend's
`window.featureFlags` is `get_feature_flags()`, which applies
`GET_FEATURE_FLAGS_FUNC` / `IS_FEATURE_ENABLED_FUNC` on every call (Superset's
per-role / per-tenant rollout hooks). A hook can therefore re-split the two
switches after the derivation. So the report carries three values, not two:

  enabled_backend     OWNERSHIP_ENABLED, parsed by the config's own rule
                      (only the literal "true" / True is on)
  enabled_ui          FEATURE_FLAGS["OBJECT_OWNERSHIP"], the static dict every
                      config layer produced
  enabled_ui_runtime  the flag as Superset's feature-flag manager answers it,
                      hooks applied -- what the bootstrap payload will carry.
                      Evaluated under an app context with NO request, so a
                      per-user hook sees an anonymous caller; None where there
                      is no manager to ask or the hook raised
  ui_flag_hooked      whether either hook is configured. True is reported
                      with a WARNING even when the values agree: a hook is
                      evaluated per request, and what it returns for a given
                      user is not something this report or `check` can see

`flags_agree` is true only when all three agree (a runtime value of None is
not counted against it).

`flags_agree` is what `status` reports; `check` (both states) goes one step
further and FAILS when a hook is configured and its runtime answer could not
be obtained (`gate`): an unknown UI value is not a pass for a deployment
gate, and the payload says why and what to verify by hand.

Two more lines say when an instruction was dropped rather than disagreed with:
`SUPERSET_FEATURE_OBJECT_OWNERSHIP` in the environment, an earlier config
layer's `FEATURE_FLAGS["OBJECT_OWNERSHIP"]`, and `OWNERSHIP_ENABLED` in the
environment when an earlier layer set it as a Python setting are each
reported as ignored when they disagree with the effective value. For the
last one, only an environment value that ASKED for the non-default (on)
earns a WARNING: the compose file injects the default "false" into every
process, so a layer that turns the feature on against it is not a conflict
and is reported at INFO. A switch spelling that is neither "true" nor
"false" ("1", "yes", "on") is off and gets a WARNING naming the accepted
spellings, so a value that was not understood is never silently off.

In the feature-OFF state this is the only module of the package a process
loads: the config's OFF mutator imports it (guarded -- a config shipped
without the package still boots, with a reduced inline report) to make the
report and to register `superset ownership status` / `check`, and nothing
else. It imports nothing else from `superset_ownership`, so loading it wires
nothing; tests/test_endpoints.py's flag-off parity test asserts both halves
of that. It reaches into core Superset for exactly one thing, the
feature-flag manager, lazily and guarded so the pure tests run without it.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Mapping, Optional

logger = logging.getLogger(__name__)

SWITCH = "OWNERSHIP_ENABLED"
FLAG = "OBJECT_OWNERSHIP"
# The generic env override the light config honours for every other flag;
# for this one the derivation wins, and the boot log says so if it was set.
FLAG_ENV_OVERRIDE = f"SUPERSET_FEATURE_{FLAG}"
# Set by the config: the value of FEATURE_FLAGS[FLAG] an EARLIER config layer
# (superset_config.py, superset_config_docker.py) left in the namespace before
# the derivation overwrote it. None when no layer set it.
INHERITED_FLAG = "OWNERSHIP_INHERITED_UI_FLAG"
# Set by the config: where the effective OWNERSHIP_ENABLED came from,
# "config" (an earlier layer set it as a Python setting, which wins) or
# "environment" (the default).
SWITCH_SOURCE = "OWNERSHIP_ENABLED_SOURCE"
# Superset's runtime feature-flag hooks (superset/config.py).
HOOKS = ("GET_FEATURE_FLAGS_FUNC", "IS_FEATURE_ENABLED_FUNC")
# The spellings of the switch the environment is understood in, after
# strip().lower(): on, off, and unset-by-blank. Anything else is off and is
# reported as not understood (see `warn_if_flags_disagree`).
SWITCH_SPELLINGS = ("true", "false", "")


def is_recognised_spelling(raw: Any) -> bool:
    """Whether an environment value of the switch is one of the accepted
    spellings ("true" / "false", case-insensitive, whitespace ignored, or
    blank). `as_bool` reads every other value as off; this says whether the
    operator's value was understood at all."""
    return str(raw).strip().lower() in SWITCH_SPELLINGS


def as_bool(value: Any) -> bool:
    """The config's own rule for the switch: only the literal "true"
    (case-insensitive, whitespace-tolerant) or the bool True is on. "1",
    "yes", "on" and a non-empty string such as "false" are all off, so a
    layer that spells the switch as a string is read the way the environment
    spelling is."""
    return value is True or str(value).strip().lower() == "true"


def effective(
    config: Optional[Mapping[str, Any]] = None,
    *,
    runtime_ui: Optional[bool] = None,
) -> dict[str, Any]:
    """The switches as the process sees them, and whether they agree.

    `config` is a Flask config (or any mapping); None reads the current
    app's, and reports both off where there is no app -- a report, never a
    refusal, so a caller with nothing to compare against still gets a shape
    it can print. `runtime_ui` is the hooked value (see `runtime_ui_flag`);
    None means it was not evaluated and is reported as such.
    """
    if config is None:
        try:
            from flask import current_app

            config = current_app.config
        except Exception:  # noqa: BLE001 - no app context
            config = {}
    enabled_backend = as_bool(config.get(SWITCH))
    enabled_ui = bool((config.get("FEATURE_FLAGS") or {}).get(FLAG))
    hooked = [name for name in HOOKS if config.get(name)]
    agree = enabled_backend == enabled_ui and (
        runtime_ui is None or runtime_ui == enabled_backend
    )
    return {
        "enabled_backend": enabled_backend,
        "enabled_ui": enabled_ui,
        "enabled_ui_runtime": runtime_ui,
        "ui_flag_hooked": bool(hooked),
        "ui_flag_hooks": hooked,
        "flags_agree": agree,
    }


def _feature_flag_manager() -> Any:
    # Core Superset, not this package; guarded so the pure tests (no Superset)
    # and a bare Flask app get "not evaluated" rather than an ImportError.
    try:
        from superset.extensions import feature_flag_manager
    except ImportError:
        return None
    return feature_flag_manager


def runtime_ui_flag(app: Any) -> Optional[bool]:
    """FEATURE_FLAGS[FLAG] as the frontend will read it: through Superset's
    feature-flag manager, with GET_FEATURE_FLAGS_FUNC / IS_FEATURE_ENABLED_FUNC
    applied. Evaluated under an app context and no request, so a per-user hook
    sees an anonymous caller. None (with a WARNING when a hook raised) where it
    cannot be evaluated."""
    manager = _feature_flag_manager()
    if manager is None or not hasattr(app, "app_context"):
        return None
    try:
        with app.app_context():
            return bool(manager.is_feature_enabled(FLAG))
    except Exception as exc:  # noqa: BLE001 - reported, never fatal at boot
        logger.warning(
            "superset_ownership: FEATURE_FLAGS[%r] could not be evaluated through "
            "the feature-flag manager (%s: %s); the runtime value of the UI flag "
            "is unknown to this report",
            FLAG, exc.__class__.__name__, exc,
        )
        return None


def report(app: Any = None) -> dict[str, Any]:
    """`effective` for a Flask app, runtime value included. None reads the
    current app, and where there is none reports both off and the runtime
    value unevaluated -- a report, never a refusal, as `effective` does."""
    if app is None:
        try:
            from flask import current_app

            app = current_app._get_current_object()
        except Exception:  # noqa: BLE001 - no app context
            return effective({})
    return effective(app.config, runtime_ui=runtime_ui_flag(app))


def runtime_unknown(state: Mapping[str, Any]) -> bool:
    """A hook is configured and its answer for the flag was not obtained:
    the value the UI reads is unknown to the report."""
    return bool(state.get("ui_flag_hooked")) and state.get("enabled_ui_runtime") is None


def gate(state: Mapping[str, Any]) -> dict[str, Any]:
    """The `check` verdict from a report: `ok`, and why not when it is not.

    `flags_agree` is a report and a runtime value of None is not counted
    against it (the switches did not DISAGREE). A deployment gate is held to
    more: when a hook is configured and its runtime answer could not be
    obtained -- a per-user hook that reads `g.user` raises when evaluated
    without a request, which is how the boot report and `check` evaluate it
    -- the half the gate exists to check is unknown, and unknown is not a
    pass. So `ok` is false then, with `runtime_unknown` naming the hook and
    what to verify by hand. A payload that already carries an `ok` (the
    ON-state `check_consistency`) keeps it as a further condition.
    """
    payload = dict(state)
    ok = bool(payload["ok"]) if "ok" in payload else bool(payload["flags_agree"])
    if runtime_unknown(state):
        ok = False
        hooks = " / ".join(state.get("ui_flag_hooks") or [])
        payload["runtime_unknown"] = (
            f"{hooks} is configured but FEATURE_FLAGS[{FLAG!r}] could not be "
            "evaluated through it without a request (the hook raised; the "
            "superset_ownership.flags WARNING names the error), so the value the "
            "UI reads is unknown to this check, and unknown is not a pass. Verify "
            "the flag per user -- what the hook answers for a signed-in user -- "
            f"or have the hook leave {FLAG!r} alone: the flag is set from {SWITCH}."
        )
    payload["ok"] = ok
    return payload


def warn_if_flags_disagree(app: Any) -> dict[str, Any]:
    """Log the effective state of the switches, at WARNING if they differ.

    Called from FLASK_APP_MUTATOR, which runs after every config layer and
    after the feature-flag manager is initialised, so what it reads is what
    the process will run with. Returns the report so a caller can act on it.
    """
    state = report(app)
    backend, ui, runtime = (
        state["enabled_backend"], state["enabled_ui"], state["enabled_ui_runtime"]
    )
    if state["flags_agree"]:
        logger.info(
            "superset_ownership: %s=%s FEATURE_FLAGS[%r]=%s (one switch; the flag "
            "is derived from the setting)",
            SWITCH, backend, FLAG, ui,
        )
    elif ui != backend:
        logger.warning(
            "superset_ownership: %s=%s but FEATURE_FLAGS[%r]=%s -- the flag was "
            "overridden after the ownership config derived it. %s. Remove the "
            "override: the flag is set from %s and must not be set on its own.",
            SWITCH, backend, FLAG, ui, _consequence(ui), SWITCH,
        )
    else:
        # Static dict agrees; the hook moved it. That is the UI half exactly.
        logger.warning(
            "superset_ownership: %s=%s and FEATURE_FLAGS[%r]=%s agree, but %s "
            "evaluates the flag to %s at runtime -- that is the value the UI "
            "reads. %s. Have the hook leave %r alone: the flag is set from %s.",
            SWITCH, backend, FLAG, ui, " / ".join(state["ui_flag_hooks"]),
            runtime, _consequence(bool(runtime)), FLAG, SWITCH,
        )

    if state["ui_flag_hooked"]:
        logger.warning(
            "superset_ownership: %s is set: the UI flag FEATURE_FLAGS[%r] is "
            "evaluated per request by it, and this report and `superset ownership "
            "check` see only what it returns for an anonymous caller (%s). The "
            "two switches can disagree per user without a line here.",
            " / ".join(state["ui_flag_hooks"]), FLAG,
            "not evaluated" if runtime is None else runtime,
        )

    config = app.config
    raw = os.environ.get(FLAG_ENV_OVERRIDE)
    if raw is not None and as_bool(raw) != backend:
        logger.warning(
            "superset_ownership: %s=%r in the environment is ignored: "
            "FEATURE_FLAGS[%r] is derived from %s (currently %s). Set %s instead.",
            FLAG_ENV_OVERRIDE, raw, FLAG, SWITCH, backend, SWITCH,
        )
    inherited = config.get(INHERITED_FLAG)
    if inherited is not None and bool(inherited) != backend:
        logger.warning(
            "superset_ownership: FEATURE_FLAGS[%r]=%r set by an earlier config "
            "layer is ignored: the flag is derived from %s (currently %s). Set %s "
            "instead.",
            FLAG, inherited, SWITCH, backend, SWITCH,
        )
    raw_switch = os.environ.get(SWITCH)
    from_layer = config.get(SWITCH_SOURCE) == "config"
    if raw_switch is not None and not is_recognised_spelling(raw_switch):
        # Not understood: off, and say so -- "1" / "yes" / "on" must not be
        # a silent off. When a layer set the switch the environment was not
        # consulted, and the line says which value the process runs with.
        logger.warning(
            "superset_ownership: %s=%r in the environment is not a recognised "
            "spelling; the accepted spellings are 'true' and 'false' "
            "(case-insensitive, surrounding whitespace ignored). %s",
            SWITCH, raw_switch,
            (
                f"An earlier config layer set {SWITCH}={backend} as a Python "
                "setting, which wins over the environment."
                if from_layer
                else f"It is read as off. Set {SWITCH}=true to turn the feature on."
            ),
        )
    elif from_layer and raw_switch is not None and as_bool(raw_switch) != backend:
        if as_bool(raw_switch):
            # The environment asked for the non-default and a layer refused
            # it: an instruction was dropped, which is a WARNING.
            logger.warning(
                "superset_ownership: %s=%r in the environment is ignored: an "
                "earlier config layer set %s=%s as a Python setting, and a config "
                "layer wins over the environment. Remove one of the two.",
                SWITCH, raw_switch, SWITCH, backend,
            )
        else:
            # The environment carries the default (the compose file injects
            # OWNERSHIP_ENABLED="false" into every process) and a layer turned
            # the feature on: nothing was refused, so this is an account, not
            # a warning, and there is nothing for the operator to remove.
            logger.info(
                "superset_ownership: %s=%r in the environment is the default; an "
                "earlier config layer set %s=%s as a Python setting, and a config "
                "layer wins over the environment.",
                SWITCH, raw_switch, SWITCH, backend,
            )
    return state


def _consequence(ui_on: bool) -> str:
    return (
        "the UI will show ownership columns and a sharing control against a "
        "backend that is not loaded: every ownership call 404s and every row "
        "reads Unknown"
        if ui_on
        else "objects are born private and access is enforced, with no control "
        "in the UI to see who owns what or to share"
    )


# ------------------------------------------------------------------ the CLI


def install_cli(app: Any, emit: Optional[Callable[[Any], None]] = None) -> None:
    """`superset ownership status` and `check` for the feature-OFF state.

    The ownership CLI (cli.py) is registered by the ON mutator and needs the
    backend: tables, the sentinel role, the authorization store. With the
    switch off the one thing worth asking is whether the switches agree --
    the "backend off, UI on" state is precisely the one an operator cannot
    see from the backend's silence -- so the OFF mutator registers a group of
    the same name carrying exactly these two commands, with the same keys the
    full commands print. `status` is a report (exit 0); `check` exits 1 when
    the switches disagree or a configured hook's runtime answer could not be
    obtained (`gate`), so it gates a deployment in either state.

    A no-op (one DEBUG line) when the full group is already registered:
    click replaces commands by name, and the flags-only group must never
    shadow the ON-state one.
    """
    import click
    from flask.cli import with_appcontext

    if emit is None:
        import json  # noqa: TID251 - stdlib on purpose: no superset import here

        def emit(payload: Any) -> None:
            click.echo(json.dumps(payload, indent=2, sort_keys=True, default=str))

    @click.group("ownership")
    def ownership() -> None:
        """Object ownership and sharing (PCS-10243) -- feature OFF.

        OWNERSHIP_ENABLED is off, so the backend is not loaded and only the
        switch report is available. Set OWNERSHIP_ENABLED=true for the rest.
        """

    @ownership.command()
    @with_appcontext
    def status() -> None:
        """The switches: enabled_backend, enabled_ui, enabled_ui_runtime."""
        from flask import current_app

        emit({**report(current_app), "backend_loaded": False})

    @ownership.command()
    @with_appcontext
    def check() -> None:
        """Fail (exit 1) when the switches disagree, or a hook's runtime
        answer could not be obtained; the backend is off."""
        from flask import current_app

        payload = {**gate(report(current_app)), "backend_loaded": False}
        emit(payload)
        if not payload["ok"]:
            raise SystemExit(1)

    existing = app.cli.commands.get("ownership")
    flags_only = {"check", "status"}
    if isinstance(existing, click.Group) and set(existing.commands) - flags_only:
        # The full group (cli.install, the ON mutator) is already registered
        # -- click's add_command replaces by name, so registering the
        # flags-only group after it would silently shadow ten commands with
        # two. Nothing calls the two in that order; this makes the order
        # harmless if something ever does.
        logger.debug(
            "superset_ownership: the full `superset ownership` group is already "
            "registered; the flags-only group is not installed over it"
        )
        return
    app.cli.add_command(ownership)
    logger.info(
        "superset_ownership: feature off; `superset ownership status|check` "
        "registered (the switch report only)"
    )

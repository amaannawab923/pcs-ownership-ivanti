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
"""ONE precedence rule for every ``OWNERSHIP_*`` setting.

Before this module every reader in the package invented its own version of
"config, then environment, then a default": some checked ``current_app``
inside a bare ``try/except``, some read the environment only, some warned on
a bad value, one raised. :func:`get` is the single place that answers the
precedence question -- a config layer (an explicit ``layer`` mapping, or
``current_app.config`` when a Flask application context is active) first,
the process environment second, a typed default last -- so every call site
in the package can read a name off the same rule and get the same answer.

What this module deliberately does NOT do: decide whether an unrecognised
spelling is a warning or a boot-stopping error. That is the parser's job.
:func:`as_bool` is strict (anything but ``true``/``false``/blank is an
error); :func:`as_legacy_bool` is the opt-out grammar four keys have always
accepted, widened rather than narrowed so no working deployment regresses
(see the four-key table in the technical spec, section 3.1). ``flags.py``'s
own ``as_bool`` -- the switch's parser -- is intentionally NOT this module's
``as_bool``: it stays lenient and lives where it always has, because
``flags.py`` already owns its own accepted-spelling warning.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Literal, Mapping, Optional

logger = logging.getLogger(__name__)

Parser = Callable[[Any], Any]


class SettingError(ValueError):
    """A parser could not make sense of a value.

    Caught by :func:`get`, which logs one WARNING naming the key, the value
    and (where the parser supplies one) the accepted spellings, then
    returns the caller's default -- except for :func:`as_legacy_bool`,
    where an unrecognised spelling always resolves to ``False`` regardless
    of the key's own default (see section 3.1 of the technical spec: the
    two legacy keys that default True are exactly the ones an operator
    opts OUT of, and a typo must never silently turn them back on).
    """


# --------------------------------------------------------------------------- parsers


def as_str(value: Any) -> Optional[str]:
    """Strip; blank (after stripping) or ``None`` becomes ``None``."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def as_int(value: Any) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise SettingError(f"{value!r} is not an integer") from exc


def as_float(value: Any) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise SettingError(f"{value!r} is not a number") from exc


_STRICT_ON = {"true"}
_STRICT_OFF = {"false", ""}


def as_bool(value: Any) -> bool:
    """STRICT boolean grammar -- the #86 rule for a key that must never be
    silently "on" from an unrecognised spelling.

    ``True``/``"true"`` (any case) is on; ``False``/``"false"``/``""`` is
    off. Anything else raises :class:`SettingError`; :func:`get` catches
    that, logs, and returns the caller's default -- a direct caller (one
    that is not going through :func:`get`) must catch it itself.

    No production call site uses this today: ``OWNERSHIP_ENABLED`` -- the
    key this was written for -- reads inline in
    ``superset_config_docker_light.py``, deliberately NOT through this
    module, because that read must work with the ``superset_ownership``
    package absent (the OFF state, section 3.2). Kept as public API,
    exercised directly by ``test_settings.py``, for a caller that already
    has the package imported and wants the same strict grammar -- one
    parser, one set of accepted spellings, rather than reinventing it.
    """
    if value is True:
        return True
    if value is False:
        return False
    text = str(value).strip().lower()
    if text in _STRICT_ON:
        return True
    if text in _STRICT_OFF:
        return False
    raise SettingError(
        f'{value!r} is not true/True or false/False/"" (accepted: '
        f"true, false, an empty string)"
    )


_LEGACY_ON = {"1", "true", "yes", "on"}
_LEGACY_OFF = {"0", "false", "no", "off", ""}


def as_legacy_bool(value: Any) -> bool:
    """The opt-out grammar for the four legacy boolean keys (section 3.1).

    ``on`` = ``1``/``true``/``yes``/``on``; ``off`` = ``0``/``false``/
    ``no``/``off``/``""``. Anything else raises :class:`SettingError`;
    :func:`get` reads that as **False**, never the key's own default, since
    the two keys that default True are exactly the ones an operator turns
    off on purpose and a typo must not silently re-enable them.
    """
    if value is True:
        return True
    if value is False:
        return False
    text = str(value).strip().lower()
    if text in _LEGACY_ON:
        return True
    if text in _LEGACY_OFF:
        return False
    raise SettingError(
        f"{value!r} not recognised (accepted: "
        f"{', '.join(sorted(_LEGACY_ON | _LEGACY_OFF - {''}))}, or blank for off)"
    )


# Marker attribute, not identity (`parser is as_legacy_bool`): a caller that
# wraps this parser (`functools.partial(as_legacy_bool)`, a lambda that
# delegates to it) keeps the "unrecognised -> False" rule instead of
# silently falling into get()'s general "unrecognised -> the key's own
# default" branch, which for the two True-default legacy keys would be the
# exact regression section 3.1 exists to prevent.
as_legacy_bool.is_legacy_bool = True  # type: ignore[attr-defined]


def _is_legacy_bool_parser(parser: Parser) -> bool:
    """Does ``parser`` carry (or wrap) the ``is_legacy_bool`` marker?

    R2-3: a plain ``functools.partial(as_legacy_bool)`` -- the wrapping
    style the comment above names as the reason the marker exists at all --
    does NOT forward function attributes on its own; ``partial.func`` is
    where the wrapped callable actually lives. One level of unwrapping
    (`.func`) covers `functools.partial` and anything else that exposes
    the callable it wraps under that same name; a caller wrapping it some
    other way (a plain closure, a class with `__call__`) still needs to
    set the marker itself, as the comment above already asks for.
    """
    if parser is as_legacy_bool or getattr(parser, "is_legacy_bool", False):
        return True
    inner = getattr(parser, "func", None)
    return inner is not None and (
        inner is as_legacy_bool or getattr(inner, "is_legacy_bool", False)
    )


def as_class_ref(value: Any) -> Any:
    """Passthrough. Resolving a dotted path or alias into an instance is
    :func:`superset_ownership.plugins.resolve_class_ref`'s job -- it needs
    the protocol to check against, which this module does not know."""
    return value


def as_mapping(value: Any) -> Mapping[str, Any]:
    """A dict as-is; a string (the environment's only spelling for a
    mapping, e.g. ``OWNERSHIP_FGA_CREDENTIALS``) is ``json.loads``'d."""
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        import json  # noqa: TID251 - stdlib on purpose: no superset import here

        try:
            parsed = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise SettingError(f"{value!r} is not valid JSON") from exc
        if not isinstance(parsed, Mapping):
            raise SettingError(f"{value!r} did not decode to a JSON object")
        return parsed
    raise SettingError(f"{value!r} is not a mapping or a JSON object string")


# --------------------------------------------------------------------------- get/source


def _has_app_context() -> bool:
    try:
        from flask import has_app_context
    except ImportError:  # pragma: no cover - Flask is always present here
        return False
    return has_app_context()


def _layer_raw(name: str, layer: Optional[Mapping]) -> Any:
    """The value at the config layer: the explicit ``layer`` mapping when
    given, else ``current_app.config`` when an app context is active, else
    nothing -- straight to the environment."""
    if layer is not None:
        return layer.get(name)
    if not _has_app_context():
        return None
    from flask import current_app

    return current_app.config.get(name)


def _is_unset(value: Any) -> bool:
    """``None`` or a blank string means 'not set here' at either level."""
    if value is None:
        return True
    return isinstance(value, str) and value.strip() == ""


def source(
    name: str, *, layer: Optional[Mapping] = None
) -> Literal["config", "environment", "default"]:
    """Where a call to :func:`get` for ``name`` would answer from -- for
    boot logs and ``status``/``describe`` payloads."""
    if not _is_unset(_layer_raw(name, layer)):
        return "config"
    if not _is_unset(os.environ.get(name)):
        return "environment"
    return "default"


def get(
    name: str,
    default: Any = None,
    parser: Parser = as_str,
    *,
    layer: Optional[Mapping] = None,
    on_error: Literal["default", "raise"] = "default",
) -> Any:
    """ONE precedence for every ``OWNERSHIP_*`` key: ``layer`` (or
    ``current_app.config`` in an app context) -> ``os.environ`` -> ``default``.

    ``None`` or a blank string at a level means "not set here" -- the next
    level is consulted. The parser runs once, on whichever level answered;
    ``default`` is returned exactly as given (it is assumed already typed)
    and never passed through the parser. A :class:`SettingError` raised by
    the parser is logged as one WARNING naming the key, the value and (via
    the exception message) the accepted spellings, and ``get`` returns
    ``default`` -- except for :func:`as_legacy_bool` (or a parser carrying
    its ``is_legacy_bool`` marker attribute, not just an identity check, so
    a wrapped legacy-bool parser keeps the same rule), which resolves an
    unrecognised spelling to ``False`` regardless of ``default`` (section
    3.1). Any other exception the parser raises (``GroupIdFormatError`` is
    the one in this tree) is NOT caught: some keys are policy -- a wrong
    value must stop the process, not warn and carry on with a default that
    silently disagrees with what was written to the store.

    ``on_error="raise"`` is for exactly that kind of policy key when the
    parser is a general-purpose one (:func:`as_mapping`, for
    ``OWNERSHIP_FGA_CREDENTIALS``) that cannot itself be trusted to signal
    "this must stop the boot" the way ``GroupIdFormatError`` does: the
    :class:`SettingError` propagates uncaught, exactly as any other
    exception the parser might raise, instead of being logged and
    swallowed into ``default``.
    """
    raw = _layer_raw(name, layer)
    if _is_unset(raw):
        raw = os.environ.get(name)
    if _is_unset(raw):
        return default
    try:
        return parser(raw)
    except SettingError as exc:
        if on_error == "raise":
            raise
        if _is_legacy_bool_parser(parser):
            logger.warning("ownership: %s=%r: %s", name, raw, exc)
            return False
        logger.warning(
            "ownership: %s=%r: %s; using the default %r", name, raw, exc, default
        )
        return default

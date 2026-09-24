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
"""Function hooks: one question, one function (spec §4.5).

The four class seams (`plugins.py`) are bundles of several methods; most
deployments only need to change ONE answer -- where a user's tenant id
lives, who administers a tenant, how a group id is laid out -- without
subclassing anything. This module is the registry and call machinery for
those single-function settings: `HOOK_SPECS` names every hook the contract
defines (§4.5.2's three tables), `resolve()` reads each `OWNERSHIP_*` hook
setting through the same one-precedence/dotted-path machinery the class
seams use (`plugins.resolve_class_ref` / `plugins._import_dotted`), and
`call()` / `call_or_default()` apply the two rules that make a hook safe to
call from anywhere in this package: fail closed on a raise (§4.5.1 item 4,
logged once per hook/exception-class pair, message never logged), and cache
per-user / per-directory answers for a TTL (item 5) using the SAME shared
cache primitive `service.py`'s ownership-row cache already established
(Superset's Flask-Caching handle, gated by the same per-process-backend
safety check) rather than a second cache mechanism.

Deliberately named `plugin_hooks.py`, not `hooks.py`: `hooks.py` is the
pre-existing Superset extension-hook module (`raise_for_access_bypass`
etc.) and is unrelated to this one.

Import discipline mirrors `plugins.py`: this module never imports
`plugins`, `service` or `directory` at module scope (avoiding any import
cycle with `plugins.py`, which resolves hooks as part of `load()`) --
those are reached lazily, inside the functions that need them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping, Optional

from superset_ownership import settings

logger = logging.getLogger(__name__)

ReturnKind = Literal["lookup", "bool", "list", "page", "str", "shape", "decision"]
CacheScope = Literal["user", "directory", "none"]


class HookError(RuntimeError):
    """A hook setting failed to resolve: an unimportable dotted path, a
    missing attribute, or a resolved value that is not callable. Raised by
    `_resolve_callable`; `resolve()` turns this into a `plugins.PluginError`
    on a strict boot, or records it in `HookRegistry.failures` (degraded)."""


@dataclass(frozen=True)
class HookSpec:
    """One row of §4.5.2's three tables.

    `arity` is documentation, not enforced (Python cannot check a plain
    callable's signature reliably -- a bound method, a functools.partial and
    a lambda all answer `inspect.signature` differently); it exists so this
    module's own tests and `describe()`'s consumers can print an accurate
    picture of what a hook is expected to take.
    """

    setting: str
    arity: int
    kind: ReturnKind
    unknown: Callable[[], Any]
    cache: CacheScope = "none"


def _none() -> None:
    return None


def _false() -> bool:
    return False


def _empty_list() -> list:
    return []


def _empty_page() -> dict:
    return {"items": [], "next_cursor": None}


def _empty_degraded_page() -> dict:
    return {"items": [], "next_cursor": None, "degraded": True}


HOOK_SPECS: tuple[HookSpec, ...] = (
    # -- caller-side: take the Flask-AppBuilder User row (§4.5.1 item 2) --
    HookSpec("OWNERSHIP_MEMBER_GUID", 1, "lookup", _none, cache="user"),
    HookSpec("OWNERSHIP_TENANT_GUID", 1, "lookup", _none, cache="user"),
    HookSpec("OWNERSHIP_DISPLAY_NAME", 1, "str", _none, cache="none"),
    HookSpec("OWNERSHIP_IS_TENANT_ADMINISTRATOR", 1, "bool", _false, cache="user"),
    HookSpec("OWNERSHIP_USER_FOR_MEMBER_GUID", 1, "lookup", _none, cache="none"),
    # -- directory-side: take ids; may read the store, a directory, anything
    # read-only --
    HookSpec(
        "OWNERSHIP_USERS_OF_TENANT", 4, "page", _empty_degraded_page, cache="none"
    ),
    HookSpec(
        "OWNERSHIP_GROUPS_OF_TENANT", 4, "page", _empty_degraded_page, cache="directory"
    ),
    HookSpec("OWNERSHIP_MEMBERS_OF_GROUP", 3, "page", _empty_page, cache="directory"),
    HookSpec("OWNERSHIP_USER_IN_GROUP", 2, "bool", _false, cache="none"),
    HookSpec("OWNERSHIP_GROUP_EXISTS", 1, "bool", _false, cache="none"),
    HookSpec(
        "OWNERSHIP_ADMINISTRATORS_OF_TENANT", 1, "list", _empty_list, cache="directory"
    ),
    # -- shape-side: pure, no I/O; a raise falls back to the built-in
    # format-string logic rather than a fixed "unknown" (call_or_default) --
    HookSpec("OWNERSHIP_GROUP_ID", 2, "shape", _none, cache="none"),
    HookSpec("OWNERSHIP_SPLIT_GROUP_ID", 1, "shape", _none, cache="none"),
    HookSpec("OWNERSHIP_GROUP_DISPLAY_NAME", 1, "shape", _none, cache="none"),
    HookSpec("OWNERSHIP_TENANT_ADMINISTRATOR_GROUP", 1, "shape", _none, cache="none"),
    # -- decision-side: may only narrow the default (api.py enforces that
    # with narrow_manage_reason). M-4: a raise here is NOT "no narrowing" in
    # the sense of "the default still stands" -- `call()`'s fail-closed
    # unknown is `None`, and `narrow_manage_reason(None, default_reason)`
    # returns `None` (its first branch short-circuits before comparing to
    # `default_reason`), so a raising OWNERSHIP_CAN_MANAGE denies every
    # OWNER, tenant administrator and permission holder on that object, not
    # merely "no additional grant beyond the default". That is the intended,
    # safe direction (a bug in one hook must not silently keep granting what
    # the default alone would; §4.5.1 item 4's "unknown always denies"
    # reading of §5's non-overridable invariant), documented in §4.5.2 and
    # UPDATING.md. The ONE exception, added for issue #126, is the Superset
    # admin: see `narrow_manage_reason` for why the instance's own operator
    # is not something a customer hook may take away.
    HookSpec("OWNERSHIP_CAN_MANAGE", 2, "decision", _none, cache="none"),
)

# `api.MANAGE_REASON_ADMIN`, spelled here rather than imported: api imports
# this module, not the other way round. `test_plugin_hooks` pins the two
# spellings together.
MANAGE_REASON_ADMIN = "admin"

_SPEC_BY_SETTING: dict[str, HookSpec] = {s.setting: s for s in HOOK_SPECS}

# §4.5.2: "OWNERSHIP_GROUP_ID and OWNERSHIP_SPLIT_GROUP_ID must be
# inverses... Setting one without the other fails the boot."
SHAPE_PAIR: tuple[str, str] = ("OWNERSHIP_GROUP_ID", "OWNERSHIP_SPLIT_GROUP_ID")


def spec_for(setting: str) -> HookSpec:
    return _SPEC_BY_SETTING[setting]


# --------------------------------------------------------------------------- resolution


@dataclass
class HookRegistry:
    """The result of `resolve()`: every configured hook's callable, where it
    came from (for `describe()`), and any resolution failure recorded on a
    degraded boot."""

    callables: dict[str, Callable[..., Any]] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)

    def get(self, setting: str) -> Optional[Callable[..., Any]]:
        return self.callables.get(setting)

    def describe(self) -> dict[str, dict[str, Any]]:
        """`{setting: {"source": "default" | "default (degraded)" | "config",
        ["path": ...], ["failure_count": N]}}` for every hook this package
        knows about, in table order -- what `plugins.describe()`/`plugin
        describe` report. `failure_count` (M-5) appears only once a
        configured hook has raised at least once in this process -- a
        running total, so an operator can tell a hook is failing at all
        even between the rate-limited log lines `_log_once` prints."""
        out: dict[str, dict[str, Any]] = {}
        for spec in HOOK_SPECS:
            source = self.sources.get(spec.setting, "default")
            if source in ("default", "default (degraded)"):
                entry: dict[str, Any] = {"source": source}
            else:
                entry = {"source": "config", "path": source}
            failures = _FAILURE_COUNTS.get(spec.setting)
            if failures:
                entry["failure_count"] = failures
            out[spec.setting] = entry
        return out


def _resolve_callable(value: Any, *, key: str) -> Callable[..., Any]:
    """`"pkg.mod:function"` | a callable set directly in the config module.

    Imports a dotted path through `plugins._import_dotted` -- the same
    machinery the class seams use (§4.5.1 item 1) -- so a hook and a class
    seam fail the same way on a bad module or a missing attribute. Rejects
    anything that is not, in the end, callable.
    """
    if callable(value) and not isinstance(value, str):
        return value
    if isinstance(value, str):
        from superset_ownership import plugins as _plugins

        try:
            # N-8: reject a bare class (one whose instances are not
            # themselves callable) rather than silently constructing it --
            # a hook names a callable answer, not a plug-in to build.
            obj = _plugins._import_dotted(  # noqa: SLF001
                value, key=key, reject_plain_classes=True
            )
        except _plugins.PluginError as exc:
            raise HookError(str(exc)) from exc
        if not callable(obj):
            raise HookError(f"{key}={value!r}: resolved object is not callable")
        return obj
    raise HookError(f"{key}={value!r}: must be a dotted path or a callable")


def _check_shape_pair(reg: HookRegistry, *, strict: bool) -> None:
    a, b = SHAPE_PAIR
    have_a, have_b = a in reg.callables, b in reg.callables
    if have_a == have_b:
        return
    set_one, missing_one = (a, b) if have_a else (b, a)
    msg = (
        f"{a} and {b} must both be set or neither (§4.5.2): {set_one} is "
        f"configured but {missing_one} is not"
    )
    from superset_ownership.plugins import PluginError

    if strict:
        raise PluginError(msg)
    for setting in (a, b):
        reg.callables.pop(setting, None)
        # N-7: `missing_one` may already carry its OWN failure (e.g. its
        # dotted path failed to import) -- overwriting it with only the
        # pairing message threw away the real reason an operator would
        # need to fix it. Keep both when one is already recorded.
        existing = reg.failures.get(setting)
        reg.failures[setting] = f"{existing}; {msg}" if existing else msg
        reg.sources[setting] = "default (degraded)"


def resolve(cfg: Mapping, *, strict: bool) -> HookRegistry:
    """Resolve every `OWNERSHIP_*` hook setting once, the same way
    `plugins.load()` resolves the three class seams: one precedence
    (`settings.get(..., settings.as_class_ref, layer=cfg)`), strict boot
    raises `plugins.PluginError` naming the key on a bad value, degraded
    boot records the failure and leaves the hook unset (the default answer
    applies -- §4.5.1 item 3's fall-through).
    """
    reg = HookRegistry()
    for spec in HOOK_SPECS:
        raw = settings.get(spec.setting, None, settings.as_class_ref, layer=cfg)
        if raw is None:
            reg.sources[spec.setting] = "default"
            continue
        try:
            fn = _resolve_callable(raw, key=spec.setting)
        except HookError as exc:
            from superset_ownership.plugins import PluginError

            if strict:
                raise PluginError(str(exc)) from exc
            reg.failures[spec.setting] = str(exc)
            reg.sources[spec.setting] = "default (degraded)"
            continue
        reg.callables[spec.setting] = fn
        reg.sources[spec.setting] = raw if isinstance(raw, str) else "<callable>"
    _check_shape_pair(reg, strict=strict)
    return reg


# --------------------------------------------------------------------------- caching
#
# Reuses service.py's shared-cache primitive (Superset's own Flask-Caching
# handle) and its per-process-backend safety check
# (`service._shared_layer_usable`) rather than a second cache mechanism --
# an invalidation-free TTL cache is all a read-only hook needs, so none of
# that module's generation/invalidation machinery (built for the
# ownership-row cache, which DOES need invalidation on every write) is
# reused, only the backend lookup and the "is this backend safe to share
# across workers" verdict.

_MISS = object()


def _request_local() -> Optional[dict]:
    try:
        from flask import g, has_app_context

        if not has_app_context():
            return None
        cache = getattr(g, "_ownership_hook_cache", None)
        if cache is None:
            cache = {}
            g._ownership_hook_cache = cache
        return cache
    except Exception:  # noqa: BLE001 - no Flask, or a torn-down context
        return None


def _shared_backend() -> Any:
    try:
        from flask import has_app_context

        if not has_app_context():
            return None
        from superset.extensions import cache_manager

        from superset_ownership import service as _service

        cache = cache_manager.cache
        if cache is None or not _service._shared_layer_usable(cache):  # noqa: SLF001
            return None
        return cache
    except Exception:  # noqa: BLE001 - Superset not importable, cache not initialised
        return None


def _user_cache_ttl() -> int:
    from superset_ownership.service import lookup_cache_ttl

    return lookup_cache_ttl()


def _directory_cache_ttl() -> int:
    """The "directory TTL (300s default)" §4.5.1 item 5 names generically:
    this package's one existing directory-cache TTL setting,
    `OWNERSHIP_DIRECTORY_GROUP_WALK_TTL` (`directory.py`'s fast/walk verdict
    cache), reused rather than adding a second directory TTL knob."""
    return settings.get("OWNERSHIP_DIRECTORY_GROUP_WALK_TTL", 300, settings.as_int)


def _cache_ttl(scope: CacheScope) -> int:
    return _user_cache_ttl() if scope == "user" else _directory_cache_ttl()


def _cache_key(setting: str, key: Any) -> str:
    return f"superset_ownership:hook:{setting}:{key}"


# The sentinel `cache_get` returns for "nothing cached" -- compare with
# `is`, never with `==`; a cached value of None/""/0 is a real answer.
MISS = _MISS


def cache_get(setting: str, scope: CacheScope, key: Any) -> Any:
    """The hook cache (request-local, then the shared TTL layer) for a
    caller outside this module: `MISS` when nothing is cached."""
    return _cache_get(setting, scope, key)


def cache_set(
    setting: str, scope: CacheScope, key: Any, value: Any, *, shared: bool = True
) -> None:
    """Record `value`: request-locally always, and in the shared TTL layer
    unless `shared=False` (a caller keeping a negative answer to this
    request only, so a grant made after it is seen on the next request)."""
    if shared:
        _cache_set(setting, scope, key, value)
        return
    local = _request_local()
    if local is not None:
        local[(setting, key)] = value


def cache_forget(setting: str, key: Any) -> bool:
    """Drop one cached answer, in this request and in the shared layer.

    The shared layer has no invalidation of its own: an entry lives out its
    TTL. That is right for an answer the platform cannot change behind us,
    and wrong for one it can -- `administered_tenant`'s "yes", which the
    platform revokes without telling this module, and which then keeps
    admitting a former tenant administrator for the rest of the window
    (issue #130). This is what a deployment calls when it knows better:
    `superset ownership forget-administrator <user-id>` is the operator's
    spelling of it, and a config hook can call it directly.

    Returns whether the key was reached (False when there is no shared
    layer, or the backend refused).
    """
    local = _request_local()
    if local is not None:
        for cached_key in [k for k in local if k[0] == setting and k[1] == key]:
            local.pop(cached_key, None)
    backend = _shared_backend()
    if backend is None:
        return False
    try:
        backend.delete(_cache_key(setting, key))
    except Exception:  # noqa: BLE001 - a broken backend is not an error here
        logger.debug("ownership: could not drop %s:%s", setting, key, exc_info=True)
        return False
    return True


def _cache_get(setting: str, scope: CacheScope, key: Any) -> Any:
    local = _request_local()
    local_key = (setting, key)
    if local is not None and local_key in local:
        return local[local_key]
    if _cache_ttl(scope) <= 0:
        return _MISS
    backend = _shared_backend()
    if backend is None:
        return _MISS
    try:
        raw = backend.get(_cache_key(setting, key))
    except Exception:  # noqa: BLE001 - a broken backend is a miss, never an error
        raw = None
    if not isinstance(raw, dict) or "v" not in raw:
        return _MISS
    value = raw["v"]
    if local is not None:
        local[local_key] = value
    return value


def _cache_set(setting: str, scope: CacheScope, key: Any, value: Any) -> None:
    local = _request_local()
    if local is not None:
        local[(setting, key)] = value
    ttl = _cache_ttl(scope)
    if ttl <= 0:
        return
    backend = _shared_backend()
    if backend is None:
        return
    try:
        backend.set(_cache_key(setting, key), {"v": value}, timeout=ttl)
    except Exception:  # noqa: BLE001 - a broken backend must not fail the caller
        logger.debug("superset_ownership: hook cache set failed", exc_info=True)


# --------------------------------------------------------------------------- calling

# M-5: `_log_once` used to mean "once per (hook, exception class), ever, for
# the life of the worker" -- an operator tailing logs after minute one of a
# permanently broken hook sees nothing while every answer is silently
# "unknown". `_LOGGED` now holds the last time each pair was logged, and a
# pair re-announces itself once the shared per-user hook-cache TTL
# (`_user_cache_ttl()`) has elapsed since -- reusing that setting rather than
# adding a second one, so the failure re-announces roughly as often as a
# stale cached answer would refresh anyway. `_FAILURE_COUNTS` is a running
# total per SETTING (regardless of exception class or suppression), read by
# `HookRegistry.describe()` so `plugin describe`/`plugin verify` can show
# that a configured hook has been failing at all, not just its class name.
_LOGGED: dict[tuple[str, str], float] = {}
_SUPPRESSED_SINCE_LOG: dict[tuple[str, str], int] = {}
_FAILURE_COUNTS: dict[str, int] = {}


def _log_once(setting: str, exc: BaseException) -> None:
    """§4.5.1 item 4: logged at most once per (hook, exception class) per
    TTL window (M-5), class name only -- never `str(exc)`, which may carry
    a credential or a connection string."""
    import time

    _FAILURE_COUNTS[setting] = _FAILURE_COUNTS.get(setting, 0) + 1
    key = (setting, type(exc).__name__)
    now = time.monotonic()
    ttl = max(_user_cache_ttl(), 1)
    last = _LOGGED.get(key)
    if last is not None and now - last < ttl:
        _SUPPRESSED_SINCE_LOG[key] = _SUPPRESSED_SINCE_LOG.get(key, 0) + 1
        return
    suppressed = _SUPPRESSED_SINCE_LOG.pop(key, 0)
    _LOGGED[key] = now
    if suppressed:
        logger.error(
            "ownership: hook %s raised %s (%d more suppressed since the last message)",
            setting,
            type(exc).__name__,
            suppressed,
        )
    else:
        logger.error("ownership: hook %s raised %s", setting, type(exc).__name__)


def _coerce(spec: HookSpec, result: Any) -> Any:
    """Light type coercion at the boundary -- a hook that answers a truthy
    non-bool ("yes") or a tuple instead of a list must not surprise a caller
    that pattern-matches on the documented return type."""
    if spec.kind == "bool":
        # N-5: `bool(result)` made a truthy non-bool ("no", a non-empty
        # dict, any object) pass as True -- exactly what `plugin_verify`'s
        # `_validate_caller_result`/`caller_side` already reject as a
        # malformed answer for OWNERSHIP_IS_TENANT_ADMINISTRATOR. Accept a
        # real `bool` only; anything else fails closed like any other
        # malformed answer (`call()`'s except -> `spec.unknown()`).
        if not isinstance(result, bool):
            raise TypeError(
                f"{spec.setting}: expected bool, got {type(result).__name__}"
            )
        return result
    if spec.kind == "list":
        # M-7: every current "list" hook (OWNERSHIP_ADMINISTRATORS_OF_TENANT)
        # answers a list of user refs -- validated against the SAME
        # `USER_REF_KEYS` a directory-side "page" hook's items are, via the
        # SAME `DirectoryRejected` path, so a malformed item fails closed to
        # `spec.unknown()` (`[]`) through `call()`'s existing exception
        # handling rather than reaching `backfill.py`'s `u["superset_id"]`
        # indexing (or any other caller) unguarded.
        from superset_ownership.directory import USER_REF_KEYS, DirectoryRejected

        items = list(result) if result is not None else []
        for item in items:
            if not isinstance(item, dict) or not USER_REF_KEYS.issubset(item):
                raise DirectoryRejected(
                    f"{spec.setting} item missing required keys "
                    f"{sorted(USER_REF_KEYS)}: {item!r}"
                )
        return items
    if spec.kind == "str":
        return "" if result is None else str(result)
    if spec.kind == "page":
        from superset_ownership.directory import (
            GROUP_REF_KEYS,
            USER_REF_KEYS,
            validate_page,
        )

        item_keys = (
            GROUP_REF_KEYS
            if spec.setting == "OWNERSHIP_GROUPS_OF_TENANT"
            else USER_REF_KEYS
        )
        return validate_page(result, item_keys)
    return result  # lookup / shape / decision: passed through as-is


def call(
    fn: Callable[..., Any], setting: str, *args: Any, cache_key: Any = None
) -> Any:
    """Call a resolved hook, applying the fail-closed rule (item 4) and, when
    `cache_key` is given and the hook's spec caches, the TTL cache (item 5).

    `cache_key` is `user.id` for a caller-side hook, the directory-side
    hook's own first argument (a tenant or a group id) for a directory-side
    one, and `None` (never cached) for everything else -- the caller
    decides what identifies "the same question", this function only decides
    whether to remember the answer.
    """
    spec = _SPEC_BY_SETTING[setting]
    if cache_key is not None and spec.cache != "none":
        cached = _cache_get(setting, spec.cache, cache_key)
        if cached is not _MISS:
            return cached
    try:
        result = _coerce(spec, fn(*args))
    except Exception as exc:  # noqa: BLE001 - fail-closed by design (§4.5.1 item 4)
        _log_once(setting, exc)
        # M-2: never cache a fail-closed answer. A one-off store blip must
        # not pin "unknown" for the rest of the TTL window -- the next call
        # (even the very next request) gets to ask the hook again. Only a
        # returned (non-raising) answer is remembered.
        return spec.unknown()
    if cache_key is not None and spec.cache != "none":
        _cache_set(setting, spec.cache, cache_key, result)
    return result


def call_or_default(
    fn: Callable[..., Any], setting: str, args: tuple, default_fn: Callable[..., Any]
) -> Any:
    """The shape/`display_name` variant of `call()`: these hooks are pure
    and every caller needs SOME answer (a group id, a display name), so a
    raise falls back to the built-in computation instead of the fixed
    "unknown" value `call()` would return -- there is no sensible `None`
    group id for `identity.group_id()`'s callers to receive."""
    spec = _SPEC_BY_SETTING[setting]
    try:
        return _coerce(spec, fn(*args))
    except Exception as exc:  # noqa: BLE001
        _log_once(setting, exc)
        return default_fn(*args)


_WIDENING_WARNED: set[tuple[Optional[str], Optional[str]]] = set()


def narrow_manage_reason(
    hook_result: Optional[str], default_reason: Optional[str]
) -> Optional[str]:
    """§4.5.2's `OWNERSHIP_CAN_MANAGE` rule: the hook may only narrow the
    default -- `None` (deny) or exactly `default_reason` (a no-op) are
    honoured; any other reason is a widening attempt (a reason the default
    did not grant) and is logged and ignored, per §5's non-overridable
    invariant that no hook can grant management the store does not already
    entitle.

    N-4: a broken hook that always widens the same way used to log a
    WARNING on EVERY call -- one line per row per request on the list page.
    Logged at WARNING once per (hook_result, default_reason) pair per
    process, DEBUG afterward -- the pair is still fully diagnosable, just
    not at WARNING volume forever.

    The Superset admin's ground is the one the hook cannot take away (issue
    #126). A hook that raises answers `None` for every object, and that used
    to deny the admin too -- so one broken line in a customer's config
    locked the whole instance out of every ownership action with no way back
    except editing that config and restarting. The admin is who installs and
    removes the hook; leaving them a path in is what makes a broken hook
    recoverable instead of fatal. Every other ground still fails closed, so
    a broken hook stops granting what only the default granted."""
    if default_reason == MANAGE_REASON_ADMIN and hook_result != default_reason:
        log = logger.warning if hook_result is not None else logger.error
        log(
            "ownership: OWNERSHIP_CAN_MANAGE answered %r for a Superset admin; "
            "the admin ground is not narrowable (issue #126) and stands. Fix or "
            "remove the hook -- every other caller is being denied.",
            hook_result,
        )
        return default_reason
    if hook_result is None or hook_result == default_reason:
        return hook_result
    key = (hook_result, default_reason)
    first_time = key not in _WIDENING_WARNED
    if first_time:
        _WIDENING_WARNED.add(key)
    log = logger.warning if first_time else logger.debug
    log(
        "ownership: OWNERSHIP_CAN_MANAGE returned %r, which the default "
        "reason %r does not grant; ignoring the widening attempt",
        hook_result,
        default_reason,
    )
    return default_reason

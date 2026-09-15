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
"""The plug-in loader and registry.

Three seams -- ``authorizer``, ``directory``, ``identity`` -- each a name
(alias, dotted class path, or an instance already built) resolved once at
boot into an object that satisfies a Protocol, checked for a
``protocol_version`` match, and stashed on ``app.extensions["ownership"]``.
:func:`get_authorizer`, :func:`get_directory` and :func:`get_identity` are
the only way the rest of the package reaches a seam; nothing else should
import ``authz``, ``directory`` or ``identity`` module objects directly to
select a backend.

``directory.py`` defines ``Directory``/``_DIRECTORIES`` and ``identity.py``
defines ``Identity``/``_IDENTITIES``; this module reads both by name off
the two modules (``directory._DIRECTORIES``, ``identity._IDENTITIES``) --
never a hard import of the two at module scope here, to keep this module
from becoming the thing that decides load order between the seams it wires
together.

R2-4: :func:`maintenance_invocation` matches ``sys.argv`` against a fixed
table (below); an invocation that does not match -- including a mistyped
or shell-quoted subcommand, e.g. a single argv element ``"fga status"``
instead of two separate ones -- boots STRICT, same as web/Celery. That is
correct per §4.2 (silently downgrading an unrecognised argv to degraded
would make a typo indistinguishable from a maintenance command), but it
means a broken store configuration then surfaces as a raw ``Failed to
create app`` traceback rather than the DEGRADED banner an operator
debugging that same store would see from a recognised ``fga`` subcommand.
"""

from __future__ import annotations

import importlib
import logging
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Mapping, Optional

from superset_ownership import settings

logger = logging.getLogger(__name__)

EXT_KEY = "ownership"

# The three seams this module resolves. A fourth value ("connection") exists
# on the Registry but is not a resolve_class_ref seam -- it is built by
# _build_connection, a provisional stand-in for section 5's FgaConnection.
PROTOCOL_VERSIONS: dict[str, int] = {"authorizer": 1, "directory": 1, "identity": 1}

_SEAM_FOR_KEY = {
    "OWNERSHIP_AUTHORIZER": "authorizer",
    "OWNERSHIP_DIRECTORY": "directory",
    "OWNERSHIP_IDENTITY": "identity",
}

REFRESH_COOLDOWN_S = 30.0


class PluginError(RuntimeError):
    """A seam failed to resolve. The message names the key, the value and
    what was wrong with it -- this text is what a strict boot prints before
    the process exits, and what a degraded boot prints to stderr and stores
    in :attr:`Registry.failures`."""


# --------------------------------------------------------------------------- Registry


@dataclass
class Registry:
    authorizer: Any
    directory: Any
    identity: Any
    connection: Optional[Any] = None
    provider: Optional[Callable[[], Any]] = None
    hooks: Optional[Any] = None  # plugin_hooks.HookRegistry, resolved after the seams
    resolved: dict[str, str] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    refreshed_at: float = 0.0

    def refresh_connection(self) -> Optional[Any]:
        """Rebuild the connection from ``provider`` (ignoring the 401
        cooldown -- this is the operator-driven path, ``fga reconnect``) and
        stamp ``refreshed_at``. A registry with no provider (a static
        connection, or none loaded) returns the connection unchanged."""
        self.refreshed_at = time.monotonic()
        if self.provider is not None:
            self.connection = self.provider()
        return self.connection


# -------------------------------------------------------------------- resolve_class_ref


def resolve_class_ref(
    value: Any,
    *,
    aliases: Mapping[str, Callable[[], Any]],
    protocol: Optional[type] = None,
    key: str,
) -> Any:
    """``alias`` | ``"pkg.mod:Class"`` | ``"pkg.mod.Class"`` | an instance
    already built -> a constructed object.

    Raises :class:`PluginError` (naming ``key`` and ``value``) on: an
    unknown alias, an import failure, a missing attribute, the object not
    satisfying ``protocol`` (the missing methods are named), or a
    ``protocol_version`` that does not match this module's
    :data:`PROTOCOL_VERSIONS` entry for the seam ``key`` names. ``protocol``
    may be ``None`` (the audit-sink resolution path, which has no Protocol
    and no seam) -- the isinstance and protocol_version checks are skipped.
    """
    if value is None:
        raise PluginError(f"{key}: no value given")
    obj = _construct(value, aliases=aliases, key=key)
    if protocol is not None:
        _check_protocol(obj, protocol=protocol, key=key, value=value)
    return obj


def _construct(
    value: Any, *, aliases: Mapping[str, Callable[[], Any]], key: str
) -> Any:
    """The alias | dotted-path | instance half of resolve_class_ref."""
    if isinstance(value, str):
        if value in aliases:
            try:
                return aliases[value]()
            except PluginError:
                raise
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                raise PluginError(
                    f"{key}={value!r}: alias {value!r} failed to construct: {exc}"
                ) from exc
        if ":" in value or "." in value:
            return _import_dotted(value, key=key)
        raise PluginError(
            f"{key}={value!r}: unknown alias (accepted: "
            f"{', '.join(sorted(aliases)) or '(none registered)'}) and not a "
            f"dotted class path ('pkg.mod:Class' or 'pkg.mod.Class')"
        )
    if isinstance(value, type):
        try:
            return value()
        except Exception as exc:  # noqa: BLE001
            raise PluginError(f"{key}={value!r}: could not construct: {exc}") from exc
    return value  # an instance, accepted as-is (tests, an operator's own object)


def _check_protocol(obj: Any, *, protocol: type, key: str, value: Any) -> None:
    """The protocol/protocol_version half of resolve_class_ref."""
    if not _satisfies(obj, protocol):
        missing = _missing_members(obj, protocol)
        detail = f" (missing: {', '.join(missing)})" if missing else ""
        proto_name = getattr(protocol, "__name__", str(protocol))
        raise PluginError(
            f"{key}={value!r}: class does not satisfy {proto_name}{detail}"
        )
    seam = _SEAM_FOR_KEY.get(key)
    if seam is None:
        return
    expected = PROTOCOL_VERSIONS[seam]
    got = getattr(obj, "protocol_version", 1)
    if got != expected:
        raise PluginError(
            f"{key}={value!r}: protocol_version mismatch "
            f"(expected v{expected}, class declares v{got})"
        )


def _import_dotted(path: str, *, key: str, reject_plain_classes: bool = False) -> Any:
    if ":" in path:
        module_name, _, attr_path = path.partition(":")
    else:
        module_name, _, attr_path = path.rpartition(".")
    if not module_name or not attr_path:
        raise PluginError(
            f"{key}={path!r}: not a dotted class path "
            f"('pkg.mod:Class' or 'pkg.mod.Class')"
        )
    try:
        obj: Any = importlib.import_module(module_name)
    except ImportError as exc:
        raise PluginError(f"{key}={path!r}: import failed: {exc}") from exc
    for part in attr_path.split("."):
        try:
            obj = getattr(obj, part)
        except AttributeError as exc:
            raise PluginError(f"{key}={path!r}: {exc}") from exc
    if isinstance(obj, type):
        # N-8: a class seam (Authorizer/Directory/Identity) NAMES a class
        # deliberately and expects it constructed -- the behaviour below is
        # correct and unchanged for that caller (`reject_plain_classes`
        # defaults to False). A hook setting (`plugin_hooks._resolve_callable`,
        # `reject_plain_classes=True`) names a callable ANSWER, not a
        # plug-in to build: silently instantiating `SomeClass()` as a side
        # effect of resolving `OWNERSHIP_X = "pkg:SomeClass"` is surprising
        # (a stray class whose `__init__` opens a connection, say) when the
        # class was never meant to be used this way. Rejected unless the
        # class itself makes its instances callable (`__call__` defined),
        # which is the one shape where "construct it, then call the
        # instance" is exactly the intended use.
        if reject_plain_classes and "__call__" not in dir(obj):
            raise PluginError(
                f"{key}={path!r}: resolved to a class -- point this hook at "
                f"a function, or a class whose instances define __call__, "
                f"not a bare class"
            )
        try:
            obj = obj()
        except Exception as exc:  # noqa: BLE001
            raise PluginError(f"{key}={path!r}: could not construct: {exc}") from exc
    return obj


def _protocol_member_names(protocol: type) -> list[str]:
    names: set[str] = set()
    for klass in getattr(protocol, "__mro__", (protocol,)):
        if klass is object or klass.__name__ in ("Protocol", "Generic"):
            continue
        names.update(n for n in vars(klass) if not n.startswith("_"))
        names.update(
            n for n in getattr(klass, "__annotations__", {}) if not n.startswith("_")
        )
    return sorted(names)


def _satisfies(obj: Any, protocol: type) -> bool:
    # Always a structural (hasattr) check, never plain isinstance(): S1.
    # A bare `protocol_version: int` annotation on a `@runtime_checkable`
    # Protocol makes isinstance() require the attribute, so a class that
    # never declares it -- meaning "version 1", per the contract's own
    # default -- would fail isinstance() even though it satisfies every
    # method. _missing_members already excludes "protocol_version" from
    # what it demands, so this check and the missing-methods message stay
    # exactly in sync.
    return not _missing_members(obj, protocol)


def _missing_members(obj: Any, protocol: type) -> list[str]:
    return [
        n
        for n in _protocol_member_names(protocol)
        if n != "protocol_version" and not hasattr(obj, n)
    ]


def _qualname(obj: Any) -> str:
    cls = type(obj)
    module = getattr(cls, "__module__", "")
    name = getattr(cls, "__qualname__", cls.__name__)
    return f"{module}:{name}" if module else name


# -------------------------------------------------------------------------- seam wiring


def _authorizer_aliases() -> Mapping[str, Callable[[], Any]]:
    from superset_ownership import authz

    return authz._BACKENDS  # noqa: SLF001 - the one alias table, by design (conftest registers "counting" here)


def _authorizer_protocol() -> Optional[type]:
    from superset_ownership import authz

    return authz.Authorizer


def _directory_aliases(authorizer: Any) -> Mapping[str, Callable[[], Any]]:
    from superset_ownership.directory import _DIRECTORIES

    return _DIRECTORIES


def _default_directory(authorizer: Any) -> Any:
    """A degraded boot's directory seam default: the real ``LocalDirectory``
    -- it needs no store and matches the ``Directory`` protocol exactly."""
    from superset_ownership.directory import LocalDirectory

    return LocalDirectory()


def _directory_protocol() -> Optional[type]:
    directory_mod = _try_import("superset_ownership.directory")
    return getattr(directory_mod, "Directory", None) if directory_mod else None


def _identity_aliases() -> Mapping[str, Callable[[], Any]]:
    from superset_ownership.identity import _IDENTITIES

    return _IDENTITIES


def _default_identity() -> Any:
    """A degraded boot's identity seam default: the real ``DefaultIdentity``."""
    from superset_ownership.identity import DefaultIdentity

    return DefaultIdentity()


def _identity_protocol() -> Optional[type]:
    from superset_ownership import identity as identity_mod

    return getattr(identity_mod, "Identity", None)


def _try_import(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def _default_directory_alias(authz_ref: str) -> str:
    if authz_ref in ("local", "openfga"):
        return authz_ref
    raise PluginError(
        "OWNERSHIP_DIRECTORY must be set explicitly when OWNERSHIP_AUTHORIZER "
        f"is not 'local' or 'openfga' (got {authz_ref!r})"
    )


# --------------------------------------------------------------------------- connection


def _fga_connection_mod() -> Any:
    return _try_import("superset_ownership.fga_connection")


def _build_connection(cfg: Mapping) -> tuple[Optional[Callable[[], Any]], Any]:
    """The ``FgaConnection`` provider and connection for this app, per
    section 5: ``OWNERSHIP_FGA_CONFIG_PROVIDER`` when set, else a
    :class:`fga_connection.StaticProvider` built from the four keys plus
    ``OWNERSHIP_FGA_CREDENTIALS``. Raises :class:`PluginError` (wrapping
    ``fga_connection.CredentialError`` or a ``settings.SettingError`` from a
    malformed ``OWNERSHIP_FGA_CREDENTIALS`` -- B3: that key's parse failure
    must never fall back to the silent ``{"type": "none"}`` default, in
    either strict or degraded mode) on a malformed value -- callers treat
    the ``"connection"`` seam through the same strict/degraded path as the
    other three.
    """
    conn_mod = _fga_connection_mod()
    if (
        conn_mod is None
    ):  # pragma: no cover - fga_connection.py always ships with this package
        return None, None
    try:
        provider_ref = settings.get(
            "OWNERSHIP_FGA_CONFIG_PROVIDER", None, settings.as_class_ref, layer=cfg
        )
        provider = conn_mod.resolve_provider(provider_ref)
        if provider is None:
            provider = conn_mod.StaticProvider(
                api_url=settings.get(
                    "OWNERSHIP_FGA_API_URL", conn_mod.DEFAULT_API_URL, layer=cfg
                ),
                store_id=settings.get(
                    "OWNERSHIP_FGA_STORE", conn_mod.DEFAULT_STORE_ID, layer=cfg
                ),
                model_id=settings.get("OWNERSHIP_FGA_MODEL", None, layer=cfg),
                credentials=settings.get(
                    "OWNERSHIP_FGA_CREDENTIALS",
                    {"type": "none"},
                    settings.as_mapping,
                    layer=cfg,
                    on_error="raise",
                ),
                timeout_s=settings.get(
                    "OWNERSHIP_FGA_TIMEOUT",
                    conn_mod.DEFAULT_TIMEOUT_S,
                    settings.as_float,
                    layer=cfg,
                ),
            )
        connection = provider()
        if not isinstance(connection, conn_mod.FgaConnection):
            raise conn_mod.CredentialError(
                f"OWNERSHIP_FGA_CONFIG_PROVIDER={provider_ref!r}: must return "
                f"FgaConnection, got {type(connection).__name__}"
            )
        return provider, connection
    except (conn_mod.CredentialError, settings.SettingError) as exc:
        raise PluginError(
            str(exc)
            if isinstance(exc, conn_mod.CredentialError)
            else f"OWNERSHIP_FGA_CREDENTIALS: {exc}"
        ) from exc


def _needs_fga_connection(authorizer: Any, directory: Any) -> bool:
    """S2: the connection seam is skipped only when NEITHER resolved seam
    can possibly need a store -- both are the built-in ``LocalAuthorizer``/
    ``LocalDirectory``. Everything else builds one, including a plug-in
    that satisfies ``Authorizer``/``Directory`` and talks to a store
    itself without subclassing ``OpenFGAAuthorizer``/``OpenFGADirectory``
    (R2-1): the contract's dotted-path spelling, not just the documented
    subclass pattern (§8), is a store-backed seam too. Originally an
    ``isinstance(authorizer, OpenFGAAuthorizer)``-style allowlist, which
    missed exactly that plug-in shape -- a broken credentials shape then
    passed a strict web boot, every request rebuilt the connection instead
    of using the registry's, and a later ``fga reconnect`` had no cached
    provider to rebuild through, silently dropping 401-triggered token
    rotation.

    A ``local``-only deployment (both built-ins) never talks to a store,
    so a credentials or provider typo it never uses must not block its
    boot, and its log must not name a store it does not use -- the one
    case this still, correctly, skips.
    """
    from superset_ownership.authz import LocalAuthorizer
    from superset_ownership.directory import LocalDirectory

    return not (
        isinstance(authorizer, LocalAuthorizer)
        and isinstance(directory, LocalDirectory)
    )


# --------------------------------------------------------------------------- load


def _load_connection(
    cfg: Mapping,
    authorizer: Any,
    directory: Any,
    *,
    strict: bool,
    failures: dict[str, str],
) -> tuple[Optional[Callable[[], Any]], Any]:
    """The connection half of ``load()``, split out to keep that function's
    own branching within one screenful: S2 (build nothing when the
    resolved seams do not need a store) plus the strict/degraded split
    every other seam already follows, recorded into the SAME ``failures``
    dict ``load()`` passes to ``_seam``."""
    if not _needs_fga_connection(authorizer, directory):
        return None, None
    try:
        return _build_connection(cfg)
    except PluginError as exc:
        if strict:
            raise
        failures["connection"] = str(exc)
        print(f"ownership: DEGRADED — connection: {exc}", file=sys.stderr)
        return None, None


def load(app: Any, *, strict: bool = True) -> Registry:
    """Resolve the three seams once and stash the registry on
    ``app.extensions["ownership"]``. Called from ``FLASK_APP_MUTATOR`` after
    ``warn_if_flags_disagree(app)``.

    ``strict`` (the caller passes ``not maintenance_invocation()``): a
    failing seam raises :class:`PluginError` out of this function, which
    stops the process, unless ``strict`` is False, in which case the seam
    falls back to its default (``LocalAuthorizer`` / ``LocalDirectory`` /
    ``DefaultIdentity``), the failure text is printed to stderr immediately
    (before the caller's own output) and recorded in ``Registry.failures``.

    Idempotent (N2): a second call on the SAME app returns the registry the
    first call built rather than resolving everything again -- rebuilding
    would construct fresh seam instances (surprising for anything holding a
    reference to the first set) and, in degraded mode, print the DEGRADED
    banner a second time. Only ``FLASK_APP_MUTATOR`` calls this in
    production, so it runs once per app in practice; this guard is what
    makes calling it twice in a test harmless instead of misleading.
    """
    existing = app.extensions.get(EXT_KEY)
    if existing is not None:
        return existing

    cfg = app.config
    failures: dict[str, str] = {}
    resolved: dict[str, str] = {}
    sources: dict[str, str] = {}

    def _seam(
        name: str, key: str, build: Callable[[], Any], default: Callable[[], Any]
    ) -> Any:
        try:
            obj = build()
            resolved[name] = _qualname(obj)
            sources[name] = settings.source(key, layer=cfg)
            return obj
        except PluginError as exc:
            if strict:
                raise
            failures[name] = str(exc)
            print(f"ownership: DEGRADED — {name}: {exc}", file=sys.stderr)
            obj = default()
            resolved[name] = _qualname(obj)
            # N1: the failing key's source() answer (e.g. "environment") is
            # true of the bad value that was rejected, not of the default
            # now in use -- logging it as though it explains the default
            # object is what misled the round-1 transcript.
            sources[name] = "default (degraded)"
            return obj

    authz_ref = settings.get(
        "OWNERSHIP_AUTHORIZER", "local", settings.as_class_ref, layer=cfg
    )
    authorizer = _seam(
        "authorizer",
        "OWNERSHIP_AUTHORIZER",
        lambda: resolve_class_ref(
            authz_ref,
            aliases=_authorizer_aliases(),
            protocol=_authorizer_protocol(),
            key="OWNERSHIP_AUTHORIZER",
        ),
        default=lambda: _authorizer_aliases()["local"](),
    )

    def _build_directory() -> Any:
        dir_ref = settings.get(
            "OWNERSHIP_DIRECTORY", None, settings.as_class_ref, layer=cfg
        ) or _default_directory_alias(authz_ref)
        return resolve_class_ref(
            dir_ref,
            aliases=_directory_aliases(authorizer),
            protocol=_directory_protocol(),
            key="OWNERSHIP_DIRECTORY",
        )

    directory = _seam(
        "directory",
        "OWNERSHIP_DIRECTORY",
        _build_directory,
        default=lambda: _default_directory(authorizer),
    )

    identity_ref = settings.get(
        "OWNERSHIP_IDENTITY", "default", settings.as_class_ref, layer=cfg
    )
    identity = _seam(
        "identity",
        "OWNERSHIP_IDENTITY",
        lambda: resolve_class_ref(
            identity_ref,
            aliases=_identity_aliases(),
            protocol=_identity_protocol(),
            key="OWNERSHIP_IDENTITY",
        ),
        default=_default_identity,
    )

    provider, connection = _load_connection(
        cfg, authorizer, directory, strict=strict, failures=failures
    )

    from superset_ownership import plugin_hooks

    hooks = plugin_hooks.resolve(cfg, strict=strict)
    for hook_name, text in hooks.failures.items():
        failures[f"hook:{hook_name}"] = text
        print(f"ownership: DEGRADED — hook:{hook_name}: {text}", file=sys.stderr)

    reg = Registry(
        authorizer=authorizer,
        directory=directory,
        identity=identity,
        connection=connection,
        provider=provider,
        hooks=hooks,
        resolved=resolved,
        failures=failures,
        sources=sources,
    )
    for seam_name, obj in (
        ("authorizer", authorizer),
        ("directory", directory),
        ("identity", identity),
    ):
        logger.info(
            "ownership: %s = %s (protocol v%d, from %s)",
            seam_name,
            _qualname(obj),
            getattr(obj, "protocol_version", 1),
            sources[seam_name],
        )
    if connection is not None:
        logger.info(
            "ownership: fga = %s store=%s model=%s",
            getattr(connection, "api_url", "?"),
            getattr(connection, "store_id", "?"),
            getattr(connection, "model_id", None) or "(unpinned)",
        )
    # N-2: the three class seams log an INFO line each; a configured hook
    # never did, so `plugin describe` used to be the only place a hook
    # showed at all. One line at boot, naming every hook that resolved to a
    # config source (never the callable itself, which may be a lambda with
    # no useful repr) -- silent when none are configured, matching the
    # seams' own always-log behaviour would just repeat "default" 16 times.
    configured_hooks = sorted(hooks.callables)
    if configured_hooks:
        logger.info("ownership: hooks configured: %s", ", ".join(configured_hooks))
    app.extensions[EXT_KEY] = reg
    return reg


# ---------------------------------------------------------------------- hook wrappers
#
# §4.5.1 item 3's precedence -- a configured hook wins over the
# corresponding class-seam method, which wins over the built-in default --
# is implemented here, at the accessor boundary: get_identity()/
# get_directory() hand back one of these instead of the raw resolved seam.
# The raw seam stays on Registry.identity/.directory unwrapped (describe(),
# counting() and anything else that inspects "which class resolved" keep
# seeing the real thing); only the accessor functions wrap.
#
# Both wrappers forward attribute GETS and SETS for anything they do not
# explicitly hook to the wrapped seam (__getattr__/__setattr__ below) rather
# than only __getattr__: plugin_verify.py's force_walk support reads
# `directory._mode` and, on hasattr, ASSIGNS `directory._mode = "always"` --
# a plain getattr-only proxy would silently absorb that assignment onto the
# wrapper itself, leaving the real OpenFGADirectory's own methods (which
# read `self._mode` on themselves) never seeing the override.


class HookedIdentity:
    """Wraps the resolved `Identity` seam. Each hookable method checks
    `plugins.get_hook(...)` first; when unset, falls through to the wrapped
    seam's own method. `normalize_subject` has no hook of its own (§4.5.2
    names none for it), but it must still resolve the reverse/forward pair
    (`OWNERSHIP_USER_FOR_MEMBER_GUID` / `OWNERSHIP_MEMBER_GUID`) through this
    wrapper's OWN `member_guid`/`user_for_member_guid` -- not the raw
    wrapped seam's -- so a hook that relocates the member GUID off the
    username/email is actually consulted (H-1: the class-seam override path
    already worked, because there `self` in `_default_normalize_subject` is
    the subclass; the hook path did not, because the wrapped seam's own
    methods never see the hooks)."""

    def __init__(self, identity: Any, hooks: Any) -> None:
        object.__setattr__(self, "_identity", identity)
        object.__setattr__(self, "_hooks", hooks)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._identity, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._identity, name, value)

    def _unwrap(self) -> Any:
        """The raw seam underneath -- for `describe()`, which must report
        the resolved `Identity` class, never this wrapper's own."""
        return self._identity

    def member_guid(self, user: Any) -> Optional[str]:
        from superset_ownership import plugin_hooks

        hook = self._hooks.get("OWNERSHIP_MEMBER_GUID") if self._hooks else None
        if hook is None:
            return self._identity.member_guid(user)
        return plugin_hooks.call(
            hook, "OWNERSHIP_MEMBER_GUID", user, cache_key=getattr(user, "id", None)
        )

    def tenant_guid(self, user: Any) -> Optional[str]:
        from superset_ownership import plugin_hooks

        hook = self._hooks.get("OWNERSHIP_TENANT_GUID") if self._hooks else None
        if hook is None:
            return self._identity.tenant_guid(user)
        return plugin_hooks.call(
            hook, "OWNERSHIP_TENANT_GUID", user, cache_key=getattr(user, "id", None)
        )

    def display_name(self, user: Any) -> str:
        from superset_ownership import plugin_hooks

        hook = self._hooks.get("OWNERSHIP_DISPLAY_NAME") if self._hooks else None
        if hook is None:
            return self._identity.display_name(user)
        return plugin_hooks.call_or_default(
            hook, "OWNERSHIP_DISPLAY_NAME", (user,), self._identity.display_name
        )

    def user_for_member_guid(self, guid: str) -> Any:
        from superset_ownership import plugin_hooks

        hook = (
            self._hooks.get("OWNERSHIP_USER_FOR_MEMBER_GUID") if self._hooks else None
        )
        if hook is None:
            return self._identity.user_for_member_guid(guid)
        return plugin_hooks.call(hook, "OWNERSHIP_USER_FOR_MEMBER_GUID", guid)

    def normalize_subject(self, raw: str) -> Optional[str]:
        if self._hooks is None or not (
            self._hooks.get("OWNERSHIP_USER_FOR_MEMBER_GUID")
            or self._hooks.get("OWNERSHIP_MEMBER_GUID")
        ):
            # No relevant hook configured: identical to the pre-H-1 behavior
            # and to the class-seam-only path.
            return self._identity.normalize_subject(raw)
        from superset_ownership.identity import _default_normalize_subject

        # Re-run the canonical resolution with `self` (this wrapper) as the
        # identity being consulted: `_default_normalize_subject` calls back
        # through `self.user_for_member_guid` / `self.member_guid`, which
        # here are THIS class's hook-aware methods, so a configured reverse
        # hook is asked, and the forward-mapping check (`_guid_matches`)
        # runs against the configured forward hook too -- a lying reverse
        # hook (an account whose forward `member_guid` does not agree with
        # what was asked for) still resolves to no account, never a 500
        # (a raising hook fails closed inside `plugin_hooks.call`, which
        # this method's `member_guid`/`user_for_member_guid` already go
        # through).
        return _default_normalize_subject(self, raw)


class HookedDirectory:
    """Wraps the resolved `Directory` seam, the same way `HookedIdentity`
    wraps `Identity`."""

    def __init__(self, directory: Any, hooks: Any) -> None:
        object.__setattr__(self, "_directory", directory)
        object.__setattr__(self, "_hooks", hooks)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._directory, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._directory, name, value)

    def _unwrap(self) -> Any:
        """The raw seam underneath -- for `describe()`, which must report
        the resolved `Directory` class, never this wrapper's own."""
        return self._directory

    def search_users(
        self, tenant: str, query: str, *, limit: int = 100, cursor: Optional[str] = None
    ) -> Any:
        from superset_ownership import plugin_hooks

        hook = self._hooks.get("OWNERSHIP_USERS_OF_TENANT") if self._hooks else None
        if hook is None:
            return self._directory.search_users(
                tenant, query, limit=limit, cursor=cursor
            )
        return plugin_hooks.call(
            hook, "OWNERSHIP_USERS_OF_TENANT", tenant, query, limit, cursor
        )

    def list_groups(self, tenant: str, *, cursor: Optional[str] = None) -> Any:
        from superset_ownership import plugin_hooks

        hook = self._hooks.get("OWNERSHIP_GROUPS_OF_TENANT") if self._hooks else None
        if hook is None:
            return self._directory.list_groups(tenant, cursor=cursor)
        # Directory.list_groups takes no query/limit -- the hook's own
        # signature (§4.5.2) is richer than the protocol method it stands
        # in for, so this call site supplies the neutral values ("no
        # filter", the shipped default page size).
        return plugin_hooks.call(
            hook,
            "OWNERSHIP_GROUPS_OF_TENANT",
            tenant,
            "",
            100,
            cursor,
            cache_key=(tenant, cursor),
        )

    def group_members(self, group_id: str, *, cursor: Optional[str] = None) -> Any:
        from superset_ownership import plugin_hooks

        hook = self._hooks.get("OWNERSHIP_MEMBERS_OF_GROUP") if self._hooks else None
        if hook is None:
            return self._directory.group_members(group_id, cursor=cursor)
        return plugin_hooks.call(
            hook,
            "OWNERSHIP_MEMBERS_OF_GROUP",
            group_id,
            100,
            cursor,
            cache_key=(group_id, cursor),
        )

    def user_in_group(self, member_guid: str, group_id: str) -> bool:
        from superset_ownership import plugin_hooks

        hook = self._hooks.get("OWNERSHIP_USER_IN_GROUP") if self._hooks else None
        if hook is None:
            return self._directory.user_in_group(member_guid, group_id)
        return plugin_hooks.call(hook, "OWNERSHIP_USER_IN_GROUP", member_guid, group_id)

    def group_exists(self, group_id: str) -> bool:
        from superset_ownership import plugin_hooks

        hook = self._hooks.get("OWNERSHIP_GROUP_EXISTS") if self._hooks else None
        if hook is None:
            return self._directory.group_exists(group_id)
        return plugin_hooks.call(hook, "OWNERSHIP_GROUP_EXISTS", group_id)

    def tenant_administrators(self, tenant: str) -> Any:
        from superset_ownership import plugin_hooks

        hook = (
            self._hooks.get("OWNERSHIP_ADMINISTRATORS_OF_TENANT")
            if self._hooks
            else None
        )
        if hook is None:
            return self._directory.tenant_administrators(tenant)
        return plugin_hooks.call(
            hook, "OWNERSHIP_ADMINISTRATORS_OF_TENANT", tenant, cache_key=tenant
        )


def get_hook(name: str) -> Optional[Callable[..., Any]]:
    """The resolved `OWNERSHIP_*` hook callable, or `None` when unset (the
    default/class-seam behaviour applies -- §4.5.1 item 3). Reads the loaded
    registry's hooks when an app registry exists; otherwise resolves against
    settings/`os.environ` directly (mirrors the other accessors' no-app
    fallback), degraded rather than raising -- a read of "is this hook
    configured" must not itself crash a script that never called `load()`.
    """
    reg = _app_registry()
    if reg is not None and reg.hooks is not None:
        return reg.hooks.get(name)
    from superset_ownership import plugin_hooks

    return plugin_hooks.resolve({}, strict=False).get(name)


# --------------------------------------------------------------------------- accessors


def maintenance_invocation(argv: Optional[list[str]] = None) -> bool:
    """True for the ``superset ownership`` subcommands that must run even
    when a seam fails to load: ``db upgrade|downgrade|current|stamp``,
    ``status``, ``fga reconnect|status|install-model|show-model``,
    ``plugin verify|seed-scratch|describe``. False for web/Celery and every
    other command (``check``, ``enable``, ``reconcile``, ``purge-tenant``,
    ``backfill-tenants``, ``outbox *``, a plain ``superset db upgrade``).

    ``plugin describe`` (cross-lane M-2): its entire job is reporting which
    class each seam resolved to -- ``describe()`` already answers that for
    a degraded registry (the failed seam's class is its default, and
    ``degraded`` names it), so refusing to even try under a broken plug-in
    config would make it useless for exactly the situation an operator
    reaches for it in.
    """
    args = list(sys.argv if argv is None else argv)
    try:
        i = args.index("ownership")
    except ValueError:
        return False
    rest = args[i + 1 :]
    if not rest:
        return False
    if rest[0] == "status":
        return True
    if (
        rest[0] == "db"
        and len(rest) > 1
        and rest[1] in ("upgrade", "downgrade", "current", "stamp")
    ):
        return True
    if (
        rest[0] == "fga"
        and len(rest) > 1
        and rest[1]
        in (
            "reconnect",
            "status",
            "install-model",
            "show-model",
        )
    ):
        return True
    if (
        rest[0] == "plugin"
        and len(rest) > 1
        and rest[1]
        in (
            "verify",
            "seed-scratch",
            "describe",
        )
    ):
        return True
    return False


_FALLBACK_CACHE: dict[str, tuple[Any, Any]] = {}
# N3: guards read-modify-write access to _FALLBACK_CACHE. Without it,
# concurrent first-touch calls (e.g. eight threads calling get_authorizer()
# with no app constructed) each build their own instance and race to write
# the cache -- benign today only because every stateless backend in this
# tree is safe to have more than one live instance of (the last write wins,
# the rest are simply discarded); anything that would matter to construct
# twice deserves this lock, not luck.
_fallback_cache_lock = threading.Lock()
# Mirrors Registry.refreshed_at for the no-app-context path (a bare CLI
# bootstrap, or any of fga.py's own unit tests, none of which build a Flask
# app): without this, may_refresh() always answered True with no registry
# to consult, so a 401 refreshed and retried on every call, never
# respecting REFRESH_COOLDOWN_S.
_fallback_refreshed_at: float = 0.0


def _app_registry() -> Optional[Registry]:
    try:
        from flask import current_app, has_app_context
    except ImportError:  # pragma: no cover
        return None
    if not has_app_context():
        return None
    return current_app.extensions.get(EXT_KEY)


def _fallback_seam(seam: str, ref: Any, build: Callable[[], Any]) -> Any:
    """The no-app (or app-without-``load()``) path: a per-process value
    rebuilt only when the resolved ref changes -- this is what preserves
    ``authz.get_authorizer()``'s pre-existing 'switch the backend on a
    running instance' behaviour. Never wraps the failure: a bad ref raises
    here, at first use, exactly as an unresolved import would.

    ``build()`` runs OUTSIDE the lock (N3): it can itself import modules and
    construct arbitrary plug-in objects, and holding a lock across that
    would risk a deadlock against any other lock the constructor takes. Two
    threads racing a first touch can therefore each build once; only the
    cache write is exclusive, so the loser's object is simply discarded --
    correct for every backend in this tree, all of them stateless/safe to
    have more than one live instance of.
    """
    with _fallback_cache_lock:
        cached = _FALLBACK_CACHE.get(seam)
        if cached is not None and cached[0] == ref:
            return cached[1]
    obj = build()
    with _fallback_cache_lock:
        _FALLBACK_CACHE[seam] = (ref, obj)
    return obj


def get_authorizer() -> Any:
    reg = _app_registry()
    if reg is not None:
        return reg.authorizer
    ref = settings.get("OWNERSHIP_AUTHORIZER", "local", settings.as_class_ref)
    return _fallback_seam(
        "authorizer",
        ref,
        lambda: resolve_class_ref(
            ref,
            aliases=_authorizer_aliases(),
            protocol=_authorizer_protocol(),
            key="OWNERSHIP_AUTHORIZER",
        ),
    )


def _fallback_hooks() -> Any:
    """The no-app-registry counterpart of `reg.hooks`, degraded, never
    raising.

    N-6: this used to re-resolve fresh (16 settings reads, dotted imports)
    on EVERY call -- `get_hook()`, `get_directory()`, `get_identity()` each
    call it independently, so a script calling e.g. `identity.group_id()`
    in a loop with no app paid the full resolution cost every iteration.
    Rebuilt only when the resolved value of some `OWNERSHIP_<hook>` setting
    actually changes, the SAME `_fallback_seam` cache the authorizer/
    directory/identity single-value seams already use: computing `ref`
    (the tuple of raw values) is still a settings read per hook every call
    -- cheap -- but the expensive part, resolving each dotted path through
    an import, only runs when something changed.
    """
    from superset_ownership import plugin_hooks

    ref = tuple(
        settings.get(spec.setting, None, settings.as_class_ref)
        for spec in plugin_hooks.HOOK_SPECS
    )
    return _fallback_seam("hooks", ref, lambda: plugin_hooks.resolve({}, strict=False))


def get_directory() -> Any:
    reg = _app_registry()
    if reg is not None:
        return HookedDirectory(reg.directory, reg.hooks)
    authz_ref = settings.get("OWNERSHIP_AUTHORIZER", "local", settings.as_class_ref)
    ref = settings.get(
        "OWNERSHIP_DIRECTORY", None, settings.as_class_ref
    ) or _default_directory_alias(authz_ref)
    authorizer = get_authorizer()
    directory = _fallback_seam(
        "directory",
        ref,
        lambda: resolve_class_ref(
            ref,
            aliases=_directory_aliases(authorizer),
            protocol=_directory_protocol(),
            key="OWNERSHIP_DIRECTORY",
        ),
    )
    return HookedDirectory(directory, _fallback_hooks())


def get_identity() -> Any:
    reg = _app_registry()
    if reg is not None:
        return HookedIdentity(reg.identity, reg.hooks)
    ref = settings.get("OWNERSHIP_IDENTITY", "default", settings.as_class_ref)
    identity = _fallback_seam(
        "identity",
        ref,
        lambda: resolve_class_ref(
            ref,
            aliases=_identity_aliases(),
            protocol=_identity_protocol(),
            key="OWNERSHIP_IDENTITY",
        ),
    )
    return HookedIdentity(identity, _fallback_hooks())


def _connection_from_live_app_config() -> tuple[Optional[Callable[[], Any]], Any]:
    """S2's lazy path: an app registry loaded with no connection (nothing
    FGA-backed was resolved) but no failure either -- a store-dependent CLI
    command asking anyway builds one from ``current_app.config`` (never an
    empty layer, that is the B2 bug: it would silently ignore config and
    answer from the environment's default store instead).

    Returns ``(provider, connection)`` -- R2-1: both must land on the
    registry, not just the connection. A caller that only cached
    ``connection`` left ``reg.provider`` ``None`` forever, so a LATER
    ``fga reconnect``/401 refresh found ``provider is None`` and returned
    the same stale connection unchanged: token rotation via
    ``OWNERSHIP_FGA_CREDENTIALS``' ``token_env`` silently stopped working
    the moment a deployment took this lazy path even once.
    """
    from flask import current_app

    return _build_connection(current_app.config)


def get_fga_connection() -> Optional[Any]:
    """The registry's connection; when the app registry loaded but the
    connection seam specifically failed, :class:`PluginError` (B2) -- never
    a silent rebuild from an empty layer, which would answer from
    ``os.environ``/the built-in defaults while ignoring ``app.config``
    entirely and land on the environment's default store. When the registry
    loaded with no connection AND no failure (S2: nothing FGA-backed was
    resolved, e.g. a ``local`` authorizer), one is still built lazily from
    the live ``current_app.config`` for a command that asks anyway -- and
    (R2-1) BOTH the provider and the connection are cached onto the
    registry right there, so a second call in the same process reuses
    them (one build, one inline-token WARNING, not one per call) and a
    later ``refresh_fga_connection()`` rebuilds through the SAME provider
    instead of finding nothing to refresh through. With no app registry at
    all, built fresh from the environment every call (via
    ``_build_connection``, section 5; there is no registry to cache onto)
    -- and raises :class:`PluginError` at this first use, the same as any
    other no-app accessor, when the environment cannot build one (a
    malformed ``OWNERSHIP_FGA_CREDENTIALS``, for example)."""
    reg = _app_registry()
    if reg is not None:
        if reg.connection is not None:
            return reg.connection
        if "connection" in reg.failures:
            raise PluginError(reg.failures["connection"])
        reg.provider, reg.connection = _connection_from_live_app_config()
        return reg.connection
    _, connection = _build_connection({})
    return connection


def refresh_fga_connection() -> Optional[Any]:
    """Rebuild the connection now, ignoring the cooldown (the operator path,
    ``fga reconnect``, and what a 401's own retry calls once
    :func:`may_refresh` allows it), and stamp the refresh time so the next
    :func:`may_refresh` call honours the cooldown. Raises
    :class:`PluginError` (B2) when the app registry's connection seam
    specifically failed to load, exactly as :func:`get_fga_connection`
    does -- an operator-driven reconnect against a degraded connection must
    report the same failure, not retry the same broken settings silently."""
    global _fallback_refreshed_at
    reg = _app_registry()
    if reg is None:
        _fallback_refreshed_at = time.monotonic()
        return get_fga_connection()
    if "connection" in reg.failures:
        raise PluginError(reg.failures["connection"])
    if reg.provider is None and reg.connection is None:
        # S2: nothing FGA-backed was resolved at load() time (no provider,
        # no static connection), but an operator explicitly asked to
        # reconnect -- build lazily rather than leaving it None, caching
        # both (R2-1) so the NEXT refresh has a provider to rebuild
        # through instead of landing back in this same branch.
        reg.refreshed_at = time.monotonic()
        reg.provider, reg.connection = _connection_from_live_app_config()
        return reg.connection
    return reg.refresh_connection()


def may_refresh(*, now: float, cooldown: float) -> bool:
    reg = _app_registry()
    if reg is None:
        return (now - _fallback_refreshed_at) >= cooldown
    return (now - reg.refreshed_at) >= cooldown


def _alias(obj: Any) -> str:
    """I-4: the short, human name a seam answers to -- the shipped
    `local`/`openfga` classes' own `name` attribute, or the plugged class's
    dotted path when it defines none. Distinct from ``describe()``'s
    ``source`` key (N1: which config layer resolved the setting), which
    `api.py`'s `/subjects` route must not be reading in its place -- that
    was the bug this closes (`_directory_source()` read a key spelled
    ``alias`` that this function did not yet exist to fill, so the lookup
    always raised and always fell back to the same-shaped default)."""
    name = getattr(obj, "name", None)
    return name if isinstance(name, str) and name else _qualname(obj)


def describe() -> dict:
    """``{seam: {"class": ..., "alias": ..., "protocol_version": ...,
    "source": ...}}`` plus ``"degraded": [...]`` -- for ``cli.py status``
    and ``plugin verify``. ``class`` (I-4) is always the resolved class's
    dotted path; ``alias`` is the short name (`local`/`openfga`) the two
    shipped classes answer to, or the same dotted path for anything else --
    what `/subjects`' `source` field reports. ``source`` (N1) is
    ``"config"``/``"environment"``/``"default"`` for a seam that resolved,
    or ``"default (degraded)"`` for one that fell back after a boot
    failure."""
    reg = _app_registry()
    if reg is not None:
        out: dict[str, Any] = {
            seam: {
                "class": reg.resolved.get(seam, _qualname(getattr(reg, seam))),
                "alias": _alias(getattr(reg, seam)),
                "protocol_version": getattr(getattr(reg, seam), "protocol_version", 1),
                "source": reg.sources.get(seam, "default"),
            }
            for seam in ("authorizer", "directory", "identity")
        }
        out["degraded"] = sorted(reg.failures)
        out["hooks"] = (
            reg.hooks.describe()
            if reg.hooks is not None
            else _fallback_hooks().describe()
        )
        return out
    raw_directory = get_directory()._unwrap()  # noqa: SLF001 - describe() reports the seam, not the wrapper
    raw_identity = get_identity()._unwrap()  # noqa: SLF001
    return {
        "authorizer": {
            "class": _qualname(get_authorizer()),
            "alias": _alias(get_authorizer()),
            "protocol_version": getattr(get_authorizer(), "protocol_version", 1),
            "source": settings.source("OWNERSHIP_AUTHORIZER"),
        },
        "directory": {
            "class": _qualname(raw_directory),
            "alias": _alias(raw_directory),
            "protocol_version": getattr(raw_directory, "protocol_version", 1),
            "source": settings.source("OWNERSHIP_DIRECTORY"),
        },
        "identity": {
            "class": _qualname(raw_identity),
            "alias": _alias(raw_identity),
            "protocol_version": getattr(raw_identity, "protocol_version", 1),
            "source": settings.source("OWNERSHIP_IDENTITY"),
        },
        "degraded": [],
        "hooks": _fallback_hooks().describe(),
    }


# --------------------------------------------------------------------------- counting()


class _CountingProxy:
    """Wraps any object; every method call is appended (by name only, no
    arguments) to a shared list before running for real. Structural -- it
    does not need to know the wrapped object's exact shape, so it works the
    same way against an Authorizer and a Directory alike."""

    def __init__(self, target: Any, log: list[str]) -> None:
        self._ownership_target = target
        self._ownership_log = log

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._ownership_target, name)
        if not callable(attr):
            return attr

        def _wrapped(*args: Any, **kwargs: Any) -> Any:
            self._ownership_log.append(name)
            return attr(*args, **kwargs)

        return _wrapped


@contextmanager
def counting() -> Iterator[list]:
    """Wrap the authorizer and directory of the CURRENT registry in
    recording proxies for the duration of the ``with`` block; yields the
    shared call log (method names, in order, both seams interleaved --
    matching the pre-existing ``CountingAuthorizer.calls`` shape). Restores
    the originals on exit even if the block raises."""
    reg = _app_registry()
    log: list[str] = []
    if reg is not None:
        original_authorizer, original_directory = reg.authorizer, reg.directory
        reg.authorizer = _CountingProxy(original_authorizer, log)
        reg.directory = _CountingProxy(original_directory, log)
        try:
            yield log
        finally:
            reg.authorizer = original_authorizer
            reg.directory = original_directory
        return

    # No app registry: patch the fallback cache for the duration instead.
    authorizer, directory = get_authorizer(), get_directory()
    authz_ref = settings.get("OWNERSHIP_AUTHORIZER", "local", settings.as_class_ref)
    dir_ref = settings.get(
        "OWNERSHIP_DIRECTORY", None, settings.as_class_ref
    ) or _default_directory_alias(authz_ref)
    _FALLBACK_CACHE["authorizer"] = (authz_ref, _CountingProxy(authorizer, log))
    _FALLBACK_CACHE["directory"] = (dir_ref, _CountingProxy(directory, log))
    try:
        yield log
    finally:
        _FALLBACK_CACHE["authorizer"] = (authz_ref, authorizer)
        _FALLBACK_CACHE["directory"] = (dir_ref, directory)

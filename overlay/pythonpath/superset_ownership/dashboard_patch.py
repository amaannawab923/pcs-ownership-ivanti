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
"""The dashboard-tile access marker, installed at runtime instead of by
editing `superset/dashboards/api.py`.

When a dashboard the caller can open embeds a chart they cannot access,
stock Superset serialises the tile through
`DashboardRestApi._serialize_dashboard_chart`, which drops `form_data` and
nothing else. The client is then left to infer "restricted" from an absence:
it cannot tell a private chart from a malformed one, and cannot say who to
ask. This module wraps that one method so a denied tile is also marked
`has_access: false` and carries `owners` -- the display names a denied
viewer can contact -- while keeping the chart's identity (`id`,
`slice_name`) so the tile stays in the layout with its title. The wrapper
restates the stock body (dump, drop `form_data` on denial) and adds the
marker; the source pin below is what makes restating it safe.

Why a wrap and not a core edit: the package installs as a wheel on top of a
stock PCS image, and nothing in stock Superset offers a hook at this point.
The wrap is deliberately narrow -- the stock four lines plus two keys, using
the same public predicate (`security_manager.can_access_chart`) the original
uses, evaluated once -- and is guarded by `_assert_wrappable`: the stock
method's parameter shape and source are pinned per PCS version, so a base
image whose method changed refuses to run the wrap rather than silently
composing with code it was never tested against.

Failure mode is deliberately split by process kind (`_degrade_on_failure`):
the web app, Celery and any command that reads authoritative state or writes
data refuse to start on a mismatch -- this wrap is what stands between a
dashboard tile and a private chart's data, and a warn-and-continue mode
would reopen exactly that leak on every render with no visible symptom. The
diagnostic and recovery commands that must be able to report or repair a
broken install (`superset ownership check`, and the maintenance set
`plugins.maintenance_invocation()` already carves out for the plug-in
seams) log at ERROR and continue without the wrap, so `check` can report
`dashboard_chart_patch_installed: false` and exit 1 instead of crashing
before it can answer.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import logging
import sys
import tokenize
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Pinned against superset/dashboards/api.py `_serialize_dashboard_chart` at
# PCS 6.1.0.7 (upstream 76151beade). Update both on every PCS version bump
# (delivery spec §8.5): derive them from the real, imported stock method --
# `_pins_for(DashboardRestApi._serialize_dashboard_chart)` below prints
# exactly what to paste -- never hand-type them, never widen to "close
# enough".
#
# Parameter NAMES and KINDS, not a stringified `inspect.signature()`: the
# string form bakes in annotation formatting, which is cosmetic and changes
# between a module with and without `from __future__ import annotations`.
# What must hold for `_original(self, chart)` to remain a valid call is the
# parameter names, order and kinds.
_EXPECTED_PARAMS: tuple[tuple[str, inspect._ParameterKind], ...] = (
    ("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    ("chart", inspect.Parameter.POSITIONAL_OR_KEYWORD),
)
_EXPECTED_SOURCE_SHA256 = (
    "2a225b2cec9fcc80218c99df8e4fc1da0510d3fdc08c886f955acd35d0109a66"
)

# Set on the installed wrapper so a second FLASK_APP_MUTATOR run (or a second
# `configure()` in one process) is a no-op and `superset ownership check`
# can tell whether the wrap is in place.
_MARKER = "_superset_ownership_patched"


class DashboardPatchError(RuntimeError):
    """Stock `_serialize_dashboard_chart` no longer matches this package's
    pins, or its source could not be read at all (see `_assert_wrappable`)."""


def _pins_for(method: Callable[..., Any]) -> tuple[tuple[tuple[str, Any], ...], str]:
    """The (params, sha256) pair `_assert_wrappable` compares against, derived
    from a live method. Used by the tests and by the version-bump procedure
    to regenerate `_EXPECTED_PARAMS` / `_EXPECTED_SOURCE_SHA256`."""
    # In an ON-state process the class attribute is already the wrapper;
    # pin the stock method it remembers, never the wrapper itself.
    method = getattr(method, "_superset_ownership_original", method)
    params = tuple(
        (name, p.kind) for name, p in inspect.signature(method).parameters.items()
    )
    digest = hashlib.sha256(inspect.getsource(method).encode()).hexdigest()
    return params, digest


_AST_KINDS = {
    "posonlyargs": inspect.Parameter.POSITIONAL_ONLY,
    "args": inspect.Parameter.POSITIONAL_OR_KEYWORD,
    "kwonlyargs": inspect.Parameter.KEYWORD_ONLY,
}


def _read_like_inspect(path: str) -> list[str]:
    """The file's lines exactly as `linecache` hands them to `inspect`."""
    try:
        with tokenize.open(path) as handle:
            lines = handle.readlines()
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        raise DashboardPatchError(
            f"could not read {path} ({exc.__class__.__name__}: {exc}); the PCS "
            "base image must ship .py sources for superset/dashboards/api.py, "
            "not bytecode only"
        ) from exc
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    return lines


def _parse(path: str, lines: list[str]) -> ast.Module:
    try:
        return ast.parse("".join(lines), filename=path)
    except SyntaxError as exc:
        raise DashboardPatchError(f"could not parse {path}: {exc}") from exc


def _locate_method(
    *, ast_tree: ast.Module, path: str
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """The LAST `def _serialize_dashboard_chart` directly inside `class
    DashboardRestApi` -- a redefinition wins at runtime too."""
    node = None
    for cls in ast_tree.body:
        if not (isinstance(cls, ast.ClassDef) and cls.name == "DashboardRestApi"):
            continue
        for fn in cls.body:
            if (
                isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
                and fn.name == "_serialize_dashboard_chart"
            ):
                node = fn
    if node is None:
        raise DashboardPatchError(
            f"{path}: no DashboardRestApi._serialize_dashboard_chart found"
        )
    return node


def _pins_from_file(path: str) -> tuple[tuple[tuple[str, Any], ...], str]:
    """The same (params, sha256) pair as `_pins_for`, read from the SOURCE
    FILE instead of a live method -- for a build step, where importing
    `superset.dashboards.api` is impossible (module import needs an
    initialised app for the encrypted-field factory).

    Reproduces `inspect.getsource` exactly: the file is read the way
    `linecache` reads it for `inspect` (`tokenize.open`, so the encoding
    cookie/BOM apply; `readlines`, so only the tokenizer's line breaks
    count, not `str.splitlines`'s; a `\n` padded onto an unterminated last
    line), and the method's block is cut with `inspect.getblock` from the
    line `ast` locates for the LAST `def _serialize_dashboard_chart` inside
    `class DashboardRestApi` (a redefinition wins at runtime too). The
    digest is byte-for-byte the one the boot-time check computes from the
    live method (test_dashboard_patch.py asserts that equality).
    """
    lines = _read_like_inspect(path)
    node = _locate_method(ast_tree=_parse(path, lines), path=path)
    a = node.args
    params: list[tuple[str, Any]] = []
    for field, kind in _AST_KINDS.items():
        params.extend((arg.arg, kind) for arg in getattr(a, field))
        if field == "args" and a.vararg is not None:
            params.append((a.vararg.arg, inspect.Parameter.VAR_POSITIONAL))
    if a.kwarg is not None:
        params.append((a.kwarg.arg, inspect.Parameter.VAR_KEYWORD))
    first = (node.decorator_list[0].lineno if node.decorator_list else node.lineno) - 1
    block = "".join(inspect.getblock(lines[first:]))
    return tuple(params), hashlib.sha256(block.encode()).hexdigest()


def assert_stock_file(path: str | None = None) -> None:
    """Build-time form of `_assert_wrappable`: check the installed
    `superset/dashboards/api.py` against this package's pins without
    creating an app. `path` defaults to the installed superset's copy."""
    if path is None:
        # find_spec, not `import superset`: the package's __init__ pulls in
        # the app module and most of the tree, which a build step does not
        # need just to locate one file.
        import importlib.util
        import os

        spec = importlib.util.find_spec("superset")
        if spec is None or not spec.submodule_search_locations:
            raise DashboardPatchError("superset is not installed in this environment")
        path = os.path.join(
            list(spec.submodule_search_locations)[0], "dashboards", "api.py"
        )
    params, digest = _pins_from_file(path)
    if params != _EXPECTED_PARAMS:
        raise DashboardPatchError(
            f"{path}: _serialize_dashboard_chart parameter shape changed: "
            f"expected {_EXPECTED_PARAMS!r}, got {params!r}"
        )
    if digest != _EXPECTED_SOURCE_SHA256:
        raise DashboardPatchError(
            f"{path}: _serialize_dashboard_chart source changed upstream "
            f"(sha256 {digest} != pinned {_EXPECTED_SOURCE_SHA256}); re-derive the "
            "pins against this PCS version before installing"
        )


def _assert_wrappable(method: Callable[..., Any]) -> None:
    """Raise `DashboardPatchError` unless `method` is the stock method this
    package was built against.

    Order matters: the parameter check needs no source access and so still
    catches a renamed/reshaped method on a bytecode-only install; the source
    hash runs second and turns an unreadable source into the same error type
    with an explicit statement of the requirement (the PCS base image must
    ship `.py` sources for `superset/dashboards/api.py`).
    """
    actual = tuple(
        (name, p.kind) for name, p in inspect.signature(method).parameters.items()
    )
    if actual != _EXPECTED_PARAMS:
        raise DashboardPatchError(
            "superset/dashboards/api.py: _serialize_dashboard_chart parameter "
            f"shape changed: expected {_EXPECTED_PARAMS!r}, got {actual!r}"
        )
    try:
        source = inspect.getsource(method)
    except OSError as exc:
        raise DashboardPatchError(
            "could not read _serialize_dashboard_chart's source "
            f"({exc.__class__.__name__}: {exc}); the PCS base image must ship "
            ".py sources for superset/dashboards/api.py, not bytecode only"
        ) from exc
    digest = hashlib.sha256(source.encode()).hexdigest()
    if digest != _EXPECTED_SOURCE_SHA256:
        raise DashboardPatchError(
            "superset/dashboards/api.py: _serialize_dashboard_chart source "
            f"changed upstream (sha256 {digest} != pinned {_EXPECTED_SOURCE_SHA256}); "
            "re-derive the pins against this PCS version before installing"
        )


def _degrade_on_failure(argv: list[str] | None = None) -> bool:
    """True for the diagnostic/recovery invocations allowed to boot WITHOUT
    the wrap when `_assert_wrappable` fails: exactly the set
    `plugins.maintenance_invocation()` already carves out for a failed
    plug-in seam, plus `superset ownership check` itself -- the command whose
    job is to report `dashboard_chart_patch_installed: false`. Everything
    else (web, Celery, `backfill-tenants`, `enable`/`disable`, `reconcile`,
    `outbox *`, a plain `superset db upgrade`, the entrypoint's raw backfill
    script) stays strict, for the same reason those commands are strict
    about a broken seam: they read authoritative state or write data."""
    from superset_ownership import plugins

    args = list(sys.argv if argv is None else argv)
    if plugins.maintenance_invocation(args):
        return True
    try:
        i = args.index("ownership")
    except ValueError:
        return False
    rest = args[i + 1 :]
    return bool(rest) and rest[0] == "check"


def _denied_across_tenants(resource: Any) -> bool:
    """Is the caller outside the tenant this resource belongs to? Read from
    the ownership row (revision 0004's mirrored tenant), never the store."""
    try:
        from flask import g

        from superset_ownership import service

        asset_type = "dashboard" if type(resource).__name__ == "Dashboard" else "chart"
        object_id = getattr(resource, "id", None)
        if object_id is None or not service.public_is_tenant_scoped():
            return False
        row = service.lookup(asset_type, object_id)
        if row is None:
            return False
        # The tenancy half of the read gate's rule, whatever the visibility:
        # the owner, and the members of the row's tenant (an untenanted row
        # matches only a caller outside every tenant), are within it.
        return not service.public_row_visible_to(row, getattr(g, "user", None))
    except Exception:  # noqa: BLE001 - a lookup failure must not 500 the tile
        return False


def get_access_contact_names(resource: Any) -> list[str]:
    """Display names a denied viewer can contact to request access to
    ``resource``.

    A denial that cannot name anyone to ask is a dead end, so this tries, in
    order: the deployment's ``EXTRA_OWNERS_RESOLVER`` (for deployments whose
    ownership lives outside the model -- this package sets it to the
    ownership record's owner), the resource's own ``owners``, and finally its
    creator. Returns display names only -- never ids or emails -- sorted for
    a deterministic payload.

    Names are intentionally disclosed to a viewer who was just denied the
    resource: the protected thing is the resource's DATA, and withholding the
    contact makes the denial unactionable without protecting anything.
    """
    from flask import current_app, has_app_context

    if not has_app_context():
        return []

    # Never name another tenant's people: a denial across the tenant line
    # (a tenanted public chart on a dashboard the other tenant reaches)
    # gets no contact -- the tile falls back to "contact your Superset
    # administrator". The owner's name is tenant-internal information.
    if _denied_across_tenants(resource):
        return []

    names: list[str] = []
    if resolver := current_app.config.get("EXTRA_OWNERS_RESOLVER"):
        try:
            names = [str(name) for name in resolver(resource) or []]
        except Exception:  # noqa: BLE001 - a resolver fault must not 500 the dashboard
            logger.warning(
                "EXTRA_OWNERS_RESOLVER raised; falling back to model owners",
                exc_info=True,
            )
            names = []

    if not names:
        names = [str(owner) for owner in (getattr(resource, "owners", None) or [])]
    if not names and (created_by := getattr(resource, "created_by", None)):
        names = [str(created_by)]

    return sorted({name for name in names if name and name.strip()})


def installed() -> bool:
    """Is the wrap currently in place on `DashboardRestApi`? What
    `superset ownership check` reports as `dashboard_chart_patch_installed`."""
    try:
        from superset.dashboards.api import DashboardRestApi
    except ImportError:
        return False
    return bool(getattr(DashboardRestApi._serialize_dashboard_chart, _MARKER, False))


def install() -> bool:
    """Wrap `DashboardRestApi._serialize_dashboard_chart` with the access
    marker. Idempotent. Returns True when the wrap is in place afterwards.

    Raises `DashboardPatchError` when the stock method does not match the
    pins -- unless this process is one `_degrade_on_failure` allows to
    continue, in which case the failure is logged at ERROR and False is
    returned. Called from the ON-state FLASK_APP_MUTATOR, after the config
    has been fully applied.
    """
    from superset.dashboards.api import DashboardRestApi
    from superset.extensions import security_manager

    original = DashboardRestApi._serialize_dashboard_chart
    if getattr(original, _MARKER, False):
        return True

    try:
        _assert_wrappable(original)
    except DashboardPatchError as exc:
        if _degrade_on_failure():
            logger.error(
                "superset_ownership: dashboard-chart access marker NOT installed "
                "(%s). This process is a diagnostic/recovery command and continues "
                "without it; `superset ownership check` reports "
                "dashboard_chart_patch_installed: false. Every other process "
                "(web, Celery, backfill, a plain `superset db upgrade`) refuses to "
                "start on this failure.",
                exc,
            )
            return False
        raise

    def patched(self: Any, chart: Any) -> dict[str, Any]:
        # The stock body, restated (the source pin above guarantees it is
        # exactly this: dump, then drop form_data on denial) so the access
        # predicate runs ONCE per tile rather than once in the original and
        # again here; plus the marker and who to ask.
        serialized: dict[str, Any] = self.chart_entity_response_schema.dump(chart)
        if not security_manager.can_access_chart(chart):
            serialized.pop("form_data", None)
            serialized["has_access"] = False
            # Mirrors what `get_datasource_access_error_object` already puts
            # on a denied datasource.
            serialized["owners"] = get_access_contact_names(chart)
        return serialized

    patched.__name__ = original.__name__
    patched.__qualname__ = getattr(original, "__qualname__", original.__name__)
    patched.__doc__ = original.__doc__
    setattr(patched, _MARKER, True)
    patched._superset_ownership_original = original
    DashboardRestApi._serialize_dashboard_chart = patched  # type: ignore[method-assign]
    logger.info("superset_ownership: dashboard-chart access marker installed")
    return True

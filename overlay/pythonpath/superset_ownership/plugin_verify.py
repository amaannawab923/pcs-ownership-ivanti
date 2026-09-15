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
"""The conformance kit behind ``superset ownership plugin verify`` (spec §10).

This module implements the twenty-two checks of the spec's §10 table, grouped
into six sections (``load``, ``connection``, ``vocabulary``, ``directory``,
``identity``, ``invariants``), plus a seventh section, ``hooks`` (spec §4.5),
with seven more checks covering the single-function hook settings
(`plugin_hooks.py`'s registry): every configured hook resolved and callable
(``hooks/resolved``), the ``OWNERSHIP_GROUP_ID``/``OWNERSHIP_SPLIT_GROUP_ID``
round trip (``hooks/shape_pair_roundtrip``), the caller-side and
directory-side hooks' documented return shapes
(``hooks/caller_side``/``hooks/directory_side``), that no configured hook
writes to the store or the metadata database
(``hooks/read_only``), that ``OWNERSHIP_CAN_MANAGE`` may only narrow the
default reason (``hooks/can_manage_narrow_only``), and that every configured
hook fails closed on an unknown input (``hooks/fail_closed``); plus one more
check beyond the spec's table, added for issue #83 alongside the `{tenant}`
placeholder in ``OWNERSHIP_MANAGE_PERMISSION``:
``vocabulary/manage_permission_template`` echoes what a templated value
renders to for ``--tenant``, and FAILs if that would (still) be a structural
role name. Thirty checks, seven sections, in total. Each check is a small
:class:`Check` object with a ``name``, a ``section``, a declared
``severity`` (the outcome the table names as its usual failure mode -- a
few checks can still land on a milder status depending on what they find)
and a ``run(ctx) -> Result`` method.

Per §4.5.1 item 7 and the task that added the ``hooks`` section: every
``hooks/*`` check SKIPs with reason "no hook configured" when the setting(s)
it exercises are unset, so a default deployment's `plugin verify` output is
unchanged apart from the new SKIP lines -- ``hooks/shape_pair_roundtrip`` is
the one exception (it round-trips whichever of the configured hook or the
built-in format-string default is active, so it never SKIPs).

Import discipline: the seams this kit inspects -- ``plugins``, ``directory``,
``fga``, ``model``, ``identity``, ``authz`` -- are sibling PCS-10243 modules
this module never imports at module scope, so it always imports cleanly on
its own regardless of what else is installed. All of that wiring lives in
:func:`_build_context`, which is called once by :func:`run` and only when a
caller does not already supply a :class:`Context` -- e.g. the CLI, inside an
application context. Unit tests construct a :class:`Context` directly out of
small fakes/stubs and never go through :func:`_build_context`, so they
exercise the checks without needing any of the sibling modules importable
(the pure/no-Superset test job).
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Literal, Mapping, Sequence

logger = logging.getLogger(__name__)

Status = Literal["PASS", "WARN", "FAIL", "SKIP"]
Severity = Literal["FAIL", "WARN"]

SECTIONS: tuple[str, ...] = (
    "load",
    "connection",
    "vocabulary",
    "directory",
    "identity",
    "invariants",
    "hooks",
)

# Exit codes (spec §10): 0 no FAIL; 1 any FAIL; 2 the plug-ins did not load,
# or the store could not be reached before the remaining checks began.
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_CANNOT_CONTINUE = 2


# ---------------------------------------------------------------------------
# Result / Report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Result:
    section: str
    name: str
    status: Status
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)
    exit_code: int = EXIT_OK

    def add(self, result: Result) -> None:
        self.results.append(result)

    @property
    def counts(self) -> dict[Status, int]:
        counts: dict[Status, int] = {"PASS": 0, "WARN": 0, "FAIL": 0, "SKIP": 0}
        for r in self.results:
            counts[r.status] += 1
        return counts

    @property
    def ok(self) -> bool:
        return self.counts["FAIL"] == 0

    def to_json(self) -> dict[str, Any]:
        return {
            "results": [r.to_dict() for r in self.results],
            "summary": self.counts,
            "exit_code": self.exit_code,
        }

    def render_text(self) -> str:
        lines: list[str] = []
        for section in SECTIONS:
            rows = [r for r in self.results if r.section == section]
            if not rows:
                continue
            lines.append(f"== {section} ==")
            for r in rows:
                detail = f"  {r.detail}" if r.detail else ""
                lines.append(f"  {r.status:<4} {r.name}{detail}")
        c = self.counts
        lines.append(
            f"SUMMARY pass={c['PASS']} warn={c['WARN']} fail={c['FAIL']} "
            f"skip={c['SKIP']}"
        )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Context -- the fakeable seam between the checks and the loaded plug-ins
# ---------------------------------------------------------------------------


@dataclass
class Context:
    """Everything a check needs, already resolved.

    Production wiring (:func:`_build_context`) fills this in from the live
    registry inside an application context. Tests build one directly, using
    small fakes/stubs for whichever attributes the check under test reads --
    nothing here requires the sibling modules to exist.
    """

    tenant: str | None = None
    sample_users: int = 5
    object_ref: str | None = None
    as_user: str | None = None
    latency_ms: int = 300
    force_walk: bool = False

    # `load` section: plugins.describe()'s shape -- {"degraded": [...],
    # "failures": {seam: text}, seam: {...}, ...}.
    describe: Mapping[str, Any] = field(default_factory=dict)

    # Resolved protocol instances (Authorizer / Directory / Identity) and the
    # connection, exactly as `plugins.get_*()` would hand them back.
    authorizer: Any = None
    directory: Any = None
    identity: Any = None
    connection: Any = None
    # `connection/reachable`: a zero-arg probe, e.g. `fga.reachable` (headers
    # sent). None only when there is no connection to probe.
    fga_reachable: Callable[[], bool] | None = None

    # `connection/required_relations`: REQUIRED from model/__init__.py, and a
    # callable that fetches what the pinned (or latest) model actually has,
    # as a set of "type.relation" strings. None = could not be determined
    # (store unreachable, unpinned with no latest, etc).
    required_relations: Mapping[str, Sequence[str]] = field(default_factory=dict)
    fetch_model_relations: Callable[[], frozenset[str]] | None = None

    # Tenants discovered from FAB roles (backfill.py's candidate-role scan);
    # `tenant`, if set, is guaranteed to be first. Needed for isolation and
    # cross-tenant-membership checks.
    candidate_tenants: Sequence[str] = ()

    # A sample of JIT-provisioned user objects/ids (tenant_<guid> role
    # holders), opaque to this module -- passed straight to the identity
    # callables below.
    jit_users: Sequence[Any] = ()

    # identity.py module functions (group-id helpers stay module functions,
    # not protocol methods -- spec §6).
    tenant_administrator_group: Callable[[str], str] | None = None
    split_group_id: Callable[[str], Any] | None = None
    member_ref: Callable[[Any], str] | None = None
    normalize_subject: Callable[[str], str | None] | None = None

    # `vocabulary/group_ids_parse`: every group id any of this tenant's
    # members holds a `member` relation to, read raw (no
    # `group_belongs_to_tenant` filtering -- see `GroupIdsParse.run`).
    list_tenant_group_ids: Callable[[str], Sequence[str]] | None = None

    # `vocabulary/manage_permission_template`: `api.manage_permission_role()`
    # -- the already-parsed OWNERSHIP_MANAGE_PERMISSION value (None when
    # unset or refused), literal or carrying `{tenant}` (issue #83).
    manage_permission_role: str | None = None

    # R2-N3 (review round 2, PR #98, `review-manage-template-pr98.md`):
    # `manage_permission_role` alone cannot tell "not set" from "set but
    # refused by the parser" -- both parse to `None`. True when the RAW
    # setting was present at all (whether or not it went on to parse), so
    # `ManagePermissionTemplate` can report the correct one of the two.
    manage_permission_configured: bool = False

    # `invariants` section. Given "owner" | "non_owner" | "public", returns
    # the tuple of store call names `plugins.counting()` recorded while that
    # subject's access decision was made, or None when the probe does not
    # apply (e.g. "public" was asked for but --object is not a public
    # object).
    invariant_probe: Callable[[str], Sequence[str] | None] | None = None
    # Whether `--object` names a public object -- `non_owner_one_check`
    # needs this (a non-owner's decision on a public object costs 0 calls,
    # the same as `public_zero_calls`'s own assertion, not one `check`).
    object_is_public: Callable[[], bool] | None = None

    # `hooks` section (spec §4.5). `hooks_describe` is `plugins.describe()
    # ["hooks"]` -- `{setting: {"source": "default" | "default (degraded)"
    # | "config", ["path": ...]}}` for all 16 §4.5.2 hook settings, in table
    # order; a hook is "configured" when its `source` is `"config"`.
    hooks_describe: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    # Resolution-failure text per hook setting, degraded boot only (mirrors
    # `describe["failures"]` for the three class seams) -- `hooks_describe`
    # alone only says a hook degraded, never why.
    hook_failures: Mapping[str, str] = field(default_factory=dict)
    # The resolved callable for a hook setting, or None when unset --
    # `plugins.get_hook`.
    get_hook: Callable[[str], Callable[..., Any] | None] | None = None
    # `identity.group_id` -- the forward half of the shape pair
    # (`split_group_id` above is the reverse half).
    group_id: Callable[[str, str], str] | None = None
    # `hooks/can_manage_narrow_only`: resolves --object/--as to `(user,
    # object_state)` with the REAL `default_reason` already computed, or
    # None when the object or the user does not resolve. None (the
    # attribute itself, not its call result) when --object/--as were not
    # given -- the check tells the two SKIP reasons apart the same way
    # `invariant_probe`/`OwnerZeroCalls` do.
    can_manage_probe: Callable[[], tuple[Any, Mapping[str, Any]] | None] | None = None
    # A Superset user by primary key, or None -- `hooks/directory_side`'s
    # administrator/is_tenant_administrator consistency check.
    user_by_id: Callable[[int], Any] | None = None
    # The EFFECTIVE (hook-aware) `api.is_tenant_administrator` -- used only
    # to cross-check `OWNERSHIP_ADMINISTRATORS_OF_TENANT`'s own listing,
    # never to re-verify `hooks/caller_side`'s hook in isolation.
    is_tenant_administrator: Callable[[Any], bool] | None = None

    latencies_ms: list[float] = field(default_factory=list)

    def timed(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        start = time.perf_counter()
        result = fn(*args, **kwargs)
        self.latencies_ms.append((time.perf_counter() - start) * 1000)
        return result


# ---------------------------------------------------------------------------
# Check base
# ---------------------------------------------------------------------------


class Check:
    section: str
    name: str
    severity: Severity = "FAIL"

    def run(self, ctx: Context) -> Result:  # pragma: no cover - overridden
        raise NotImplementedError

    def _result(self, status: Status, detail: str = "") -> Result:
        return Result(self.section, self.name, status, detail)


# ---------------------------------------------------------------------------
# load
# ---------------------------------------------------------------------------


class PluginsLoad(Check):
    section = "load"
    name = "plugins_load"
    severity = "FAIL"

    def run(self, ctx: Context) -> Result:
        degraded = list(ctx.describe.get("degraded", []))
        if degraded:
            failures = ctx.describe.get("failures", {})
            named = "; ".join(
                f"{seam}: {failures.get(seam, 'degraded, no failure text')}"
                for seam in degraded
            )
            return self._result("FAIL", f"degraded seam(s): {named}")
        return self._result("PASS", "authorizer, directory and identity all loaded")


# ---------------------------------------------------------------------------
# connection
# ---------------------------------------------------------------------------


class Reachable(Check):
    section = "connection"
    name = "reachable"
    severity = "FAIL"

    def run(self, ctx: Context) -> Result:
        if ctx.connection is None:
            return self._result("SKIP", "no connection loaded (local authorizer)")
        if ctx.fga_reachable is None:
            return self._result(
                "FAIL", "no reachability probe wired for this connection"
            )
        try:
            ok = ctx.fga_reachable()
        except Exception as exc:  # defensive: reachable() must not raise
            return self._result("FAIL", f"reachable() raised: {exc}")
        return (
            self._result("PASS", "store answered")
            if ok
            else self._result("FAIL", "store did not answer 200 with credentials")
        )


class ModelPinned(Check):
    section = "connection"
    name = "model_pinned"
    severity = "WARN"

    def run(self, ctx: Context) -> Result:
        if ctx.connection is None:
            return self._result("SKIP", "no connection loaded")
        model_id = getattr(ctx.connection, "model_id", None)
        if model_id:
            return self._result("PASS", f"pinned to {model_id}")
        return self._result("WARN", "unpinned; the store's latest model is used")


class RequiredRelations(Check):
    section = "connection"
    name = "required_relations"
    severity = "FAIL"

    def run(self, ctx: Context) -> Result:
        if not ctx.required_relations:
            return self._result("SKIP", "no REQUIRED model table supplied")
        if ctx.fetch_model_relations is None:
            return self._result("SKIP", "no way to read the store's model")
        try:
            present = ctx.fetch_model_relations()
        except Exception as exc:
            return self._result("FAIL", f"could not read the model: {exc}")
        required = {
            f"{type_}.{relation}"
            for type_, relations in ctx.required_relations.items()
            for relation in relations
        }
        missing = required - present
        if not missing:
            return self._result("PASS", "every required type.relation is present")
        if missing == {"group.tenant"}:
            return self._result(
                "WARN",
                "group.tenant missing: fallback walk active for list_groups",
            )
        return self._result("FAIL", f"missing: {sorted(missing)}")


class ScratchTupleRoundTrip(Check):
    section = "connection"
    name = "scratch_tuple_roundtrip"
    severity = "FAIL"

    def run(self, ctx: Context) -> Result:
        if ctx.authorizer is None:
            return self._result("SKIP", "no authorizer loaded")
        write_tuple = getattr(ctx.authorizer, "write_tuple", None)
        delete_tuple = getattr(ctx.authorizer, "delete_tuple", None)
        if write_tuple is None or delete_tuple is None:
            return self._result("SKIP", "authorizer has no write_tuple/delete_tuple")
        token = uuid.uuid4()
        user, relation, obj = f"user:verify-{token}", "viewer", f"chart:verify-{token}"
        # `strict=True` and asserting the booleans matters: the non-strict
        # client logs "openfga write rejected" and returns False on a
        # rejected write instead of raising, so a bare try/except around
        # calls whose return value goes unchecked reports PASS when a
        # read-only token, a wrong store or a model missing `chart.viewer`
        # rejects every write (PR90 review H-1).
        try:
            w1 = write_tuple(user, relation, obj, strict=True)
            w2 = write_tuple(user, relation, obj, strict=True)  # already satisfied
        except Exception as exc:
            return self._result("FAIL", f"write round-trip raised: {exc}")
        finally:
            # Always attempt cleanup once a write was tried, even when the
            # round trip fails below -- a broken store must not keep a
            # `verify-*` tuple around because this check found something
            # wrong.
            try:
                d1 = delete_tuple(user, relation, obj, strict=True)
                d2 = delete_tuple(user, relation, obj, strict=True)  # already satisfied
            except Exception:
                d1 = d2 = False
        if not all((w1, w2, d1, d2)):
            return self._result(
                "FAIL",
                f"write/write/delete/delete returned {(w1, w2, d1, d2)}, not all True",
            )
        return self._result("PASS", "write/write/delete/delete all succeeded")


# ---------------------------------------------------------------------------
# vocabulary
# ---------------------------------------------------------------------------


class GroupHasTenantTuple(Check):
    section = "vocabulary"
    name = "group_has_tenant_tuple"
    severity = "FAIL"

    def run(self, ctx: Context) -> Result:
        if ctx.tenant is None:
            return self._result("SKIP", "no --tenant given")
        # A model missing `group.tenant` must SKIP here, the same verdict
        # `required_relations` already gives it -- not FAIL. OpenFGA answers
        # a Read for an undefined relation with `200 {"tuples": []}`, not an
        # error, so `_fast_groups` on such a store silently returns nothing
        # rather than raising something this check's `except` clause could
        # recognise; every walked group then reports as "missing" a tuple
        # it was never possible to write in the first place (PR90 review
        # N-2a). Ask `required_relations`' own source of truth first.
        if ctx.fetch_model_relations is not None:
            try:
                present = ctx.fetch_model_relations()
            except Exception:
                present = None
            if present is not None and "group.tenant" not in present:
                return self._result(
                    "SKIP", "model lacks group.tenant; nothing to compare"
                )
        directory = ctx.directory
        fast = getattr(directory, "_fast_groups", None)
        walked = getattr(directory, "_walk_groups", None)
        if fast is None or walked is None:
            return self._result(
                "SKIP", "directory has no fast-path (e.g. the local backend)"
            )
        try:
            # `_fast_groups(self, tenant, cursor)` -- the real
            # `OpenFGADirectory` signature requires `cursor` positionally;
            # the fixture-driven unit tests' fake took `(self, tenant)` only
            # and never caught the mismatch (PR90 review B-4).
            fast_ids = {g["id"] for g in fast(ctx.tenant, None)["items"]}
            walked_ids = {g["id"] for g in walked(ctx.tenant)["items"]}
        except Exception as exc:
            if "group.tenant" in str(exc) or "group#tenant" in str(exc):
                return self._result(
                    "SKIP", "model lacks group.tenant; nothing to compare"
                )
            return self._result("FAIL", f"comparison raised: {exc}")
        missing = walked_ids - fast_ids
        if not missing:
            return self._result("PASS", "every walked group has a tenant tuple")
        return self._result("FAIL", f"groups without a tenant tuple: {sorted(missing)}")


class MemberGuidsResolve(Check):
    section = "vocabulary"
    name = "member_guids_resolve"
    severity = "WARN"

    def run(self, ctx: Context) -> Result:
        if ctx.tenant is None or ctx.directory is None:
            return self._result("SKIP", "no --tenant given")
        directory_only = 0
        total = 0
        cursor = None
        while True:
            page = ctx.directory.search_users(ctx.tenant, "", cursor=cursor)
            for u in page["items"]:
                total += 1
                if u.get("superset_id") is None:
                    directory_only += 1
            cursor = page.get("next_cursor")
            if not cursor:
                break
        if directory_only:
            return self._result(
                "WARN",
                f"{directory_only}/{total} member(s) are directory-only "
                "(no matching Superset user yet)",
            )
        return self._result("PASS", f"all {total} member(s) resolve to a Superset user")


class AdministratorGroupExists(Check):
    section = "vocabulary"
    name = "administrator_group_exists"
    severity = "WARN"

    def run(self, ctx: Context) -> Result:
        if (
            ctx.tenant is None
            or ctx.directory is None
            or ctx.tenant_administrator_group is None
        ):
            return self._result("SKIP", "no --tenant given")
        group = ctx.tenant_administrator_group(ctx.tenant)
        if ctx.directory.group_exists(group):
            return self._result("PASS", f"{group} exists")
        return self._result("WARN", f"{group} does not exist: a zero-admin tenant")


class NoMemberInTwoTenants(Check):
    section = "vocabulary"
    name = "no_member_in_two_tenants"
    severity = "FAIL"

    def run(self, ctx: Context) -> Result:
        if ctx.tenant is None or ctx.directory is None or ctx.authorizer is None:
            return self._result("SKIP", "no --tenant given")
        others = [t for t in ctx.candidate_tenants if t != ctx.tenant]
        if not others:
            return self._result("SKIP", "only one candidate tenant known")
        offenders: list[str] = []
        cursor = None
        while True:
            page = ctx.directory.search_users(ctx.tenant, "", cursor=cursor)
            for u in page["items"]:
                for other in others:
                    if ctx.authorizer.check(
                        f"user:{u['guid']}", "member", f"tenant:{other}"
                    ):
                        offenders.append(f"{u['guid']} in {ctx.tenant} and {other}")
            cursor = page.get("next_cursor")
            if not cursor:
                break
        if offenders:
            return self._result("FAIL", "; ".join(offenders))
        return self._result("PASS", "no member belongs to two tenants")


class GroupIdsParse(Check):
    section = "vocabulary"
    name = "group_ids_parse"
    severity = "FAIL"

    def run(self, ctx: Context) -> Result:
        # Deliberately NOT `ctx.directory.list_groups()`: both of
        # `OpenFGADirectory`'s paths (`_fast_groups`/`_walk_groups`) filter
        # every candidate through `group_belongs_to_tenant`, which itself
        # calls `split_group_id` -- the exact function this check exists to
        # exercise. A group id that fails to parse is, by that filter's own
        # logic, never "this tenant's", so it never reaches a check reading
        # through either path; a wrong-format id from a misconfigured sync
        # would be silently invisible, not merely mis-parsed (PR90 review
        # B-4/the wrong_format_id fixture never reaching any check). This
        # check reads the raw, unfiltered member-group tuples instead.
        if ctx.tenant is None or ctx.split_group_id is None:
            return self._result("SKIP", "no --tenant given")
        if ctx.list_tenant_group_ids is None:
            return self._result("SKIP", "no way to list this tenant's group ids")
        try:
            group_ids = ctx.list_tenant_group_ids(ctx.tenant)
        except TooManyMembersError as exc:
            return self._result("SKIP", str(exc))
        except Exception as exc:
            # Strict throughout (PR90 review N-4): a store failure while
            # listing must FAIL, never collapse to an empty set and PASS.
            return self._result("FAIL", f"could not list this tenant's groups: {exc}")
        bad = [
            group_id
            for group_id in group_ids
            # `split_group_id` reports a bad id by returning None, not by
            # raising (identity.py's contract) -- a bare try/except here
            # never saw a bad id fail (PR90 review B-4).
            if ctx.split_group_id(group_id) is None
        ]
        if bad:
            return self._result(
                "FAIL", f"ids not parsable under OWNERSHIP_GROUP_ID_FORMAT: {bad}"
            )
        return self._result("PASS", "every listed group id parses")


class ManagePermissionTemplate(Check):
    section = "vocabulary"
    name = "manage_permission_template"
    severity = "FAIL"

    def run(self, ctx: Context) -> Result:
        # `api.manage_permission_role()` has already refused (and warned
        # about) a template that could never render to anything but a
        # structural role, for ANY tenant -- that check does not depend on
        # which GUID fills the placeholder. What is left to confirm here is
        # only that, for THIS run's `--tenant`, the template renders to
        # something at all -- a cheap, tenant-specific echo for an operator
        # confirming their config, not a re-check of what parsing already
        # guarantees.
        from superset_ownership.api import MANAGE_PERMISSION_TENANT_PLACEHOLDER

        role = ctx.manage_permission_role
        if role is None:
            # R2-N3: `manage_permission_role` alone cannot tell "not set"
            # from "set but refused by the parser" -- both parse to `None`.
            # The refusal itself already WARNED above (api.py's
            # `parse_manage_permission`); this line just stops repeating
            # the wrong half of that story.
            if ctx.manage_permission_configured:
                return self._result(
                    "SKIP",
                    "OWNERSHIP_MANAGE_PERMISSION is set but refused (see warning)",
                )
            return self._result("SKIP", "OWNERSHIP_MANAGE_PERMISSION is not set")
        if MANAGE_PERMISSION_TENANT_PLACEHOLDER not in role:
            return self._result("SKIP", "not a {tenant} template")
        if ctx.tenant is None:
            return self._result("SKIP", "no --tenant given")
        rendered = role.replace(MANAGE_PERMISSION_TENANT_PLACEHOLDER, ctx.tenant)
        from superset_ownership.identity import is_structural_tenant_role

        if is_structural_tenant_role(rendered):
            # Reachable whenever an `OWNERSHIP_SPLIT_GROUP_ID` hook's
            # structural answer depends on the GUID itself, not only on
            # `OWNERSHIP_GROUP_ID_FORMAT` -- the same reason this check is
            # worth having at all, since parse time can only probe one GUID.
            # The only non-PASS/SKIP outcome this check has is FAIL, so its
            # declared severity is FAIL, not WARN.
            return self._result(
                "FAIL", f"renders to a structural role for {ctx.tenant}: {rendered}"
            )
        return self._result("PASS", f"renders to {rendered} for {ctx.tenant}")


# ---------------------------------------------------------------------------
# directory
# ---------------------------------------------------------------------------


class TenantIsolation(Check):
    section = "directory"
    name = "tenant_isolation"
    severity = "FAIL"

    def run(self, ctx: Context) -> Result:
        tenants = list(dict.fromkeys(ctx.candidate_tenants))
        if ctx.directory is None or len(tenants) < 2:
            return self._result("SKIP", "fewer than two candidate tenants known")
        a, b = tenants[0], tenants[1]
        ids_a = {g["id"] for g in ctx.timed(ctx.directory.list_groups, a)["items"]}
        ids_b = {g["id"] for g in ctx.timed(ctx.directory.list_groups, b)["items"]}
        overlap = ids_a & ids_b
        if overlap:
            return self._result(
                "FAIL", f"shared between {a} and {b}: {sorted(overlap)}"
            )
        return self._result("PASS", f"{a} and {b} share no groups")


class PaginationRoundTrips(Check):
    section = "directory"
    name = "pagination_roundtrips"
    severity = "FAIL"

    def run(self, ctx: Context) -> Result:
        if ctx.tenant is None or ctx.directory is None:
            return self._result("SKIP", "no --tenant given")

        def walk(limit: int) -> list[str]:
            guids: list[str] = []
            cursor = None
            while True:
                page = ctx.timed(
                    ctx.directory.search_users,
                    ctx.tenant,
                    "",
                    limit=limit,
                    cursor=cursor,
                )
                guids.extend(u["guid"] for u in page["items"])
                cursor = page.get("next_cursor")
                if not cursor:
                    break
            return guids

        small = walk(2)
        large = walk(100)
        if len(small) != len(set(small)):
            return self._result("FAIL", "limit=2 walk returned a duplicate GUID")
        if set(small) != set(large):
            return self._result(
                "FAIL",
                f"limit=2 gave {sorted(set(small))}, "
                f"limit=100 gave {sorted(set(large))}",
            )
        return self._result(
            "PASS", f"{len(large)} member(s), same set at both page sizes"
        )


class NotFoundVsEmpty(Check):
    section = "directory"
    name = "not_found_vs_empty"
    severity = "FAIL"

    def run(self, ctx: Context) -> Result:
        if ctx.tenant is None or ctx.directory is None:
            return self._result("SKIP", "no --tenant given")
        missing = f"nope_{ctx.tenant}"
        if ctx.timed(ctx.directory.group_exists, missing):
            return self._result("FAIL", f"{missing} reported as existing")
        groups = ctx.timed(ctx.directory.list_groups, ctx.tenant)["items"]
        empty = next((g for g in groups if g.get("members") == 0), None)
        if empty is None:
            return self._result("SKIP", "no known-empty group in this tenant to probe")
        try:
            page = ctx.timed(ctx.directory.group_members, empty["id"])
        except Exception as exc:
            return self._result(
                "FAIL", f"group_members raised on an empty group: {exc}"
            )
        if page["items"]:
            return self._result(
                "FAIL",
                f"{empty['id']} reported members == 0 but group_members is non-empty",
            )
        return self._result("PASS", "not-found is False, empty is [] with no raise")


class UserInGroupAgrees(Check):
    section = "directory"
    name = "user_in_group_agrees"
    severity = "FAIL"

    def run(self, ctx: Context) -> Result:
        if ctx.tenant is None or ctx.directory is None or ctx.authorizer is None:
            return self._result("SKIP", "no --tenant given")
        users = ctx.directory.search_users(ctx.tenant, "", limit=ctx.sample_users)[
            "items"
        ]
        # Listed once, not once per sampled user (PR90 review L-1): the
        # tenant's groups don't change between users in the same run.
        groups = ctx.directory.list_groups(ctx.tenant)["items"]
        disagreements: list[str] = []
        checked = 0
        for u in users[: ctx.sample_users]:
            for g in groups:
                checked += 1
                d = ctx.timed(ctx.directory.user_in_group, u["guid"], g["id"])
                a = ctx.timed(ctx.authorizer.user_in_group, u["guid"], g["id"])
                if d != a:
                    disagreements.append(
                        f"{u['guid']} x {g['id']}: directory={d} authorizer={a}"
                    )
        if not checked:
            return self._result("SKIP", "no members/groups to sample")
        if disagreements:
            return self._result("FAIL", "; ".join(disagreements))
        return self._result("PASS", f"{checked} pair(s) agree")


class DirectoryHealthCheck(Check):
    section = "directory"
    name = "health"
    severity = "FAIL"

    def run(self, ctx: Context) -> Result:
        if ctx.directory is None:
            return self._result("SKIP", "no directory loaded")
        try:
            health = ctx.timed(ctx.directory.health)
        except Exception as exc:
            return self._result("FAIL", f"health() raised: {exc}")
        ok = getattr(health, "ok", None)
        if ok is None and isinstance(health, Mapping):
            ok = health.get("ok")
        detail = getattr(health, "detail", None)
        if detail is None and isinstance(health, Mapping):
            detail = health.get("detail", "")
        return (
            self._result("PASS", detail or "ok")
            if ok
            else self._result("FAIL", detail or "health() reported not ok")
        )


class Latency(Check):
    section = "directory"
    name = "latency"
    severity = "WARN"

    def run(self, ctx: Context) -> Result:
        if not ctx.latencies_ms:
            return self._result("SKIP", "no directory calls were timed")
        p95 = _percentile(ctx.latencies_ms, 95)
        if p95 < ctx.latency_ms:
            return self._result(
                "PASS", f"p95={p95:.1f}ms over {len(ctx.latencies_ms)} call(s)"
            )
        return self._result(
            "WARN", f"p95={p95:.1f}ms exceeds the {ctx.latency_ms}ms budget"
        )


def _percentile(samples: Sequence[float], pct: float) -> float:
    if len(samples) == 1:
        return samples[0]
    ordered = sorted(samples)
    k = (len(ordered) - 1) * (pct / 100)
    f, c = int(k), min(int(k) + 1, len(ordered) - 1)
    if f == c:
        return ordered[f]
    return ordered[f] + (ordered[c] - ordered[f]) * (k - f)


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------

_GUID_V4_RE = None


def _guid_v4_re():
    global _GUID_V4_RE
    if _GUID_V4_RE is None:
        import re

        _GUID_V4_RE = re.compile(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
            re.IGNORECASE,
        )
    return _GUID_V4_RE


class GuidV4(Check):
    section = "identity"
    name = "guid_v4"
    severity = "FAIL"

    def run(self, ctx: Context) -> Result:
        if not ctx.jit_users or ctx.identity is None:
            return self._result("SKIP", "no sample JIT users supplied")
        bad: list[str] = []
        for u in ctx.jit_users[: ctx.sample_users]:
            guid = ctx.identity.member_guid(u)
            if not guid or not _guid_v4_re().match(guid):
                bad.append(str(getattr(u, "id", u)))
        if bad:
            return self._result("FAIL", f"not a GUID v4: {bad}")
        return self._result(
            "PASS", f"{len(ctx.jit_users[: ctx.sample_users])} sampled, all GUID v4"
        )


class TenantKnownToStore(Check):
    section = "identity"
    name = "tenant_known"
    severity = "FAIL"

    def run(self, ctx: Context) -> Result:
        if not ctx.jit_users or ctx.identity is None or ctx.authorizer is None:
            return self._result("SKIP", "no sample JIT users supplied")
        bad: list[str] = []
        for u in ctx.jit_users[: ctx.sample_users]:
            guid = ctx.identity.member_guid(u)
            tenant = ctx.identity.tenant_guid(u)
            if not guid or not tenant:
                bad.append(str(getattr(u, "id", u)))
                continue
            if not ctx.authorizer.check(f"user:{guid}", "member", f"tenant:{tenant}"):
                bad.append(str(getattr(u, "id", u)))
        if bad:
            return self._result("FAIL", f"tenant not known to the store for: {bad}")
        return self._result(
            "PASS", f"{len(ctx.jit_users[: ctx.sample_users])} sampled, all known"
        )


class NormalizeSubjectRoundTrip(Check):
    section = "identity"
    name = "normalize_subject_roundtrip"
    severity = "FAIL"

    def run(self, ctx: Context) -> Result:
        if not ctx.jit_users or ctx.normalize_subject is None or ctx.member_ref is None:
            return self._result("SKIP", "no sample JIT users supplied")
        bad: list[str] = []
        for u in ctx.jit_users[: ctx.sample_users]:
            ref = ctx.member_ref(u)
            by_ref = ctx.normalize_subject(ref)
            # `normalize_subject`'s own contract (identity.py's
            # `_default_normalize_subject` docstring): a bare, colon-less
            # string names nobody -- it returns None for ANY input without
            # a "kind:" prefix, by design (a share written as `"user:2"`
            # must never collide with one written as `"2"`). The Superset
            # integer-id form it actually accepts is `f"user:{u.id}"`, not
            # `str(u.id)` -- passing the id bare failed this check for
            # every sampled user, GUID-shaped usernames included, and the
            # spec table's own "str(u.id)" wording is where that came from
            # (PR90 review B-4).
            by_id = ctx.normalize_subject(f"user:{getattr(u, 'id', u)}")
            if by_ref != ref or by_id != ref:
                bad.append(str(getattr(u, "id", u)))
        if bad:
            return self._result(
                "FAIL", f"normalize_subject did not round-trip for: {bad}"
            )
        return self._result(
            "PASS", f"{len(ctx.jit_users[: ctx.sample_users])} sampled, all round-trip"
        )


# ---------------------------------------------------------------------------
# invariants
# ---------------------------------------------------------------------------


class OwnerZeroCalls(Check):
    section = "invariants"
    name = "owner_zero_calls"
    severity = "FAIL"

    def run(self, ctx: Context) -> Result:
        if ctx.invariant_probe is None:
            return self._result("SKIP", "pass --object and --as to run this section")
        calls = ctx.invariant_probe("owner")
        if calls is None:
            return self._result("SKIP", "owner probe not applicable")
        if calls == ():
            return self._result("PASS", "0 store calls")
        return self._result("FAIL", f"owner made call(s): {calls}")


class NonOwnerOneCheck(Check):
    section = "invariants"
    name = "non_owner_one_check"
    severity = "FAIL"

    def run(self, ctx: Context) -> Result:
        if ctx.invariant_probe is None:
            return self._result("SKIP", "pass --object and --as to run this section")
        calls = ctx.invariant_probe("non_owner")
        if calls is None:
            return self._result("SKIP", "non-owner probe not applicable")
        calls = tuple(calls)
        # A public object grants everyone the viewer relation without a
        # store round trip at all -- that is exactly what `public_zero_calls`
        # asserts for the SAME object when the caller is public. A
        # non-owner's decision on it costs 0 calls too, not one `check`
        # (PR90 review N-2b: `--object dashboard:5 --as <ben>` reproduced
        # `FAIL … expected exactly one check (+object_tenant): ()` on a
        # public object, which is backwards).
        if ctx.object_is_public is not None and ctx.object_is_public():
            if calls == ():
                return self._result("PASS", "public object: 0 store calls")
            return self._result("FAIL", f"public object, expected 0 calls: {calls}")
        if calls in (("check",), ("check", "object_tenant")):
            return self._result("PASS", f"calls: {calls}")
        return self._result(
            "FAIL", f"expected exactly one check (+object_tenant): {calls}"
        )


class PublicZeroCalls(Check):
    section = "invariants"
    name = "public_zero_calls"
    severity = "FAIL"

    def run(self, ctx: Context) -> Result:
        if ctx.invariant_probe is None:
            return self._result("SKIP", "pass --object and --as to run this section")
        calls = ctx.invariant_probe("public")
        if calls is None:
            return self._result("SKIP", "the object is not public")
        if calls == ():
            return self._result("PASS", "0 store calls")
        return self._result("FAIL", f"public access made call(s): {calls}")


# ---------------------------------------------------------------------------
# hooks (spec §4.5) -- the single-function hook settings' own conformance
# checks, on top of the twenty-three checks above (the spec's own twenty-two
# plus `vocabulary/manage_permission_template`, issue #83).
# ---------------------------------------------------------------------------

# §4.5.2's caller-side table, restricted to the four hooks whose signature
# is literally `(user) -> ...` -- `OWNERSHIP_USER_FOR_MEMBER_GUID` is listed
# in the same table but takes a guid, not a user (it answers the REVERSE
# question), so it is grouped with the directory-like hooks below for
# `fail_closed` purposes instead (an unknown id, not `user=None`).
_CALLER_SIDE_HOOKS: tuple[str, ...] = (
    "OWNERSHIP_MEMBER_GUID",
    "OWNERSHIP_TENANT_GUID",
    "OWNERSHIP_DISPLAY_NAME",
    "OWNERSHIP_IS_TENANT_ADMINISTRATOR",
)

# §4.5.2's directory-side table, exactly -- what `hooks/directory_side`
# samples.
_DIRECTORY_SIDE_HOOKS: tuple[str, ...] = (
    "OWNERSHIP_USERS_OF_TENANT",
    "OWNERSHIP_GROUPS_OF_TENANT",
    "OWNERSHIP_MEMBERS_OF_GROUP",
    "OWNERSHIP_USER_IN_GROUP",
    "OWNERSHIP_GROUP_EXISTS",
    "OWNERSHIP_ADMINISTRATORS_OF_TENANT",
)

# `hooks/fail_closed`'s own scope: every hook with a documented FIXED
# "unknown" answer (§4.5.2's caller-side and directory-side tables), i.e.
# every hook whose registry entry point is `call()`. The four shape-side
# hooks and `OWNERSHIP_DISPLAY_NAME` all go through `call_or_default`
# instead (a raise falls back to the built-in computation, not a fixed
# "unknown" -- `plugin_hooks.py:427-440`, and the comment at HOOK_SPECS
# already says so for the shape hooks): `OWNERSHIP_DISPLAY_NAME`'s
# `spec.unknown()` is `None` but `_coerce` turns every answer, including a
# raise's fallback, into a `str`, so it can never equal `None` and this
# check would FAIL it on every correct hook (H-2b). `OWNERSHIP_CAN_MANAGE`
# has its own check (`can_manage_narrow_only`, below) and is out of scope
# for this one too.
_FAIL_CLOSED_HOOKS: tuple[str, ...] = tuple(
    name
    for name in (_CALLER_SIDE_HOOKS + _DIRECTORY_SIDE_HOOKS)
    if name != "OWNERSHIP_DISPLAY_NAME"
) + ("OWNERSHIP_USER_FOR_MEMBER_GUID",)

# §4.5.1 item 6: the Authorizer methods that write to the store --
# `hooks/read_only` fails a configured hook that reaches any of these while
# `plugins.counting()` is recording (the proxy already logs every method
# call by name; this is just the write/read split over that vocabulary).
_WRITE_METHOD_NAMES = frozenset(
    {
        "write_tuple",
        "delete_tuple",
        "purge_object",
        "revoke_subject",
        "set_object_tenant",
    }
)


def _bare_group_id(group_id_or_ref: str) -> str:
    """`<id>` from `<id>` | `group:<id>` | `group:<id>#member`.

    Mirrors `identity._bare_group_id` without reaching into a sibling
    module's private helper: a directory-listed group id always carries the
    `group:` object prefix (`OpenFGADirectory.list_groups`'s own `"id"`
    field), while `identity.group_id`/a configured `OWNERSHIP_GROUP_ID` hook
    always answers the bare id (§4.5.2's own signature, `(name, tenant) ->
    str`) -- `shape_pair_roundtrip` needs both forms comparable.
    """
    text = group_id_or_ref
    if text.startswith("group:"):
        text = text[len("group:") :]
        text = text.split("#", 1)[0]
    return text


def _hook_configured(ctx: Context, setting: str) -> bool:
    return ctx.hooks_describe.get(setting, {}).get("source") == "config"


def _probe_args(
    setting: str, ctx: Context, unknown: str, *, force_unknown_tenant: bool = False
) -> tuple[Any, ...]:
    """Best-effort, harmless arguments for calling a configured hook once,
    for `hooks/read_only` (observe side effects) and `hooks/fail_closed`
    (observe the fail-closed answer) -- deliberately never real user data,
    and never real tenant data either when `force_unknown_tenant` is set.

    `hooks/read_only` passes `force_unknown_tenant=False` (its default):
    the operator's own `--tenant`, when given, is real but not sensitive
    beyond what they already handed this CLI, so probing a directory-side
    hook with it is harmless and lets that check exercise a populated
    tenant. `hooks/fail_closed` MUST pass `force_unknown_tenant=True`
    (H-2a): it asserts the hook answers "unknown" for an input it cannot
    possibly recognise, and a correct hook given the operator's REAL,
    populated tenant answers real data -- which is not "unknown" and would
    FAIL a hook that is working exactly as intended. Raises `KeyError` for
    a setting neither function calls.
    """
    tenant = unknown if force_unknown_tenant else (ctx.tenant or unknown)
    builders: dict[str, Callable[[], tuple[Any, ...]]] = {
        "OWNERSHIP_USER_FOR_MEMBER_GUID": lambda: (unknown,),
        "OWNERSHIP_USERS_OF_TENANT": lambda: (tenant, "", 100, None),
        "OWNERSHIP_GROUPS_OF_TENANT": lambda: (tenant, "", 100, None),
        "OWNERSHIP_MEMBERS_OF_GROUP": lambda: (unknown, 100, None),
        "OWNERSHIP_USER_IN_GROUP": lambda: (unknown, unknown),
        "OWNERSHIP_GROUP_EXISTS": lambda: (unknown,),
        "OWNERSHIP_ADMINISTRATORS_OF_TENANT": lambda: (tenant,),
        "OWNERSHIP_GROUP_ID": lambda: (
            "verify_hooks",
            "00000000-0000-4000-8000-000000000000",
        ),
        "OWNERSHIP_SPLIT_GROUP_ID": lambda: (unknown,),
        "OWNERSHIP_GROUP_DISPLAY_NAME": lambda: (unknown,),
        "OWNERSHIP_TENANT_ADMINISTRATOR_GROUP": lambda: (tenant,),
        "OWNERSHIP_CAN_MANAGE": lambda: (None, {"default_reason": None}),
    }
    for caller_side_setting in _CALLER_SIDE_HOOKS:
        builders.setdefault(caller_side_setting, lambda: (None,))
    if setting not in builders:
        raise KeyError(setting)
    return builders[setting]()


class HooksResolved(Check):
    section = "hooks"
    name = "resolved"
    severity = "FAIL"

    def run(self, ctx: Context) -> Result:
        if not ctx.hooks_describe:
            return self._result("SKIP", "no hook configured")
        configured = [
            n
            for n, info in ctx.hooks_describe.items()
            if info.get("source") == "config"
        ]
        if not configured:
            return self._result("SKIP", "no hook configured")
        degraded = [
            n
            for n, info in ctx.hooks_describe.items()
            if info.get("source") == "default (degraded)"
        ]
        names_line = ", ".join(
            f"{n}={'config' if info.get('source') == 'config' else 'default'}"
            for n, info in ctx.hooks_describe.items()
        )
        if degraded:
            named = "; ".join(
                f"{n}: {ctx.hook_failures.get(n, 'failed to resolve')}"
                for n in degraded
            )
            return self._result("FAIL", f"degraded hook(s): {named}")
        return self._result("PASS", names_line)


class ShapePairRoundtrip(Check):
    section = "hooks"
    name = "shape_pair_roundtrip"
    severity = "FAIL"

    def run(self, ctx: Context) -> Result:
        # Never SKIP (spec): this exercises whichever of a configured
        # OWNERSHIP_GROUP_ID/OWNERSHIP_SPLIT_GROUP_ID or the built-in
        # format-string default is currently active, transparently -- both
        # `group_id`/`split_group_id` fall through to the default on their
        # own (`call_or_default`), so this check has no "hook unset" branch.
        if ctx.group_id is None or ctx.split_group_id is None:
            return self._result("FAIL", "no group_id/split_group_id wired")
        ids = self._candidate_ids(ctx)
        for group_ref in ids:
            error = self._roundtrip_error(ctx, group_ref)
            if error is not None:
                return self._result("FAIL", error)
        return self._result("PASS", f"{len(ids)} group id(s) round-trip")

    @staticmethod
    def _candidate_ids(ctx: Context) -> list[str]:
        ids: list[str] = []
        if ctx.tenant is not None and ctx.directory is not None:
            try:
                ids.extend(
                    g["id"]
                    for g in ctx.directory.list_groups(ctx.tenant)["items"][:100]
                )
            except Exception:  # noqa: BLE001 - the directory section already covers this
                logger.debug(
                    "plugin_verify: shape_pair_roundtrip could not list groups",
                    exc_info=True,
                )
            if ctx.tenant_administrator_group is not None:
                ids.append(ctx.tenant_administrator_group(ctx.tenant))
        # A synthetic pair guarantees at least one round trip is exercised
        # even with no --tenant/no directory -- this check is never SKIP.
        ids.append(ctx.group_id("verify_shape_pair", str(uuid.uuid4())))
        return ids

    @staticmethod
    def _roundtrip_error(ctx: Context, group_ref: str) -> str | None:
        bare = _bare_group_id(group_ref)
        parsed = ctx.split_group_id(group_ref)
        if parsed is None:
            return f"{group_ref}: split_group_id returned None"
        name, tenant = parsed
        reforward = ctx.split_group_id(ctx.group_id(name, tenant))
        if reforward != (name, tenant):
            return (
                f"{group_ref}: split_group_id(group_id(name, tenant)) != "
                f"(name, tenant) ({reforward} != {(name, tenant)})"
            )
        rebuilt = ctx.group_id(name, tenant)
        if rebuilt != bare:
            return (
                f"{group_ref}: group_id(*split_group_id(id)) != id "
                f"({rebuilt!r} != {bare!r})"
            )
        return None


_LOCAL_ID_RE = re.compile(r"^local-\d+$")


def _validate_caller_result(name: str, result: Any) -> str | None:
    """The error text for a caller-side hook's answer that does not match
    its §4.5.2 shape, or None when it does.

    `OWNERSHIP_MEMBER_GUID`'s documented shape (§4.5.2 row 1) is a GUID v4,
    the `local-<id>` placeholder `DefaultIdentity`'s own fallback answers
    for a non-GUID account (`identity._default_member_guid`), or `None` --
    NOT "a GUID v4 or None" (H-2c): that stricter rule rejected the
    contract's own default, on its own documented fallback shape, as a
    verify FAIL. `OWNERSHIP_TENANT_GUID` has no such placeholder in the
    contract and keeps the GUID-or-None rule.
    """
    if name == "OWNERSHIP_MEMBER_GUID":
        if (
            result is not None
            and not _guid_v4_re().match(result)
            and not _LOCAL_ID_RE.match(result)
        ):
            return f"{name}: {result!r} is not a GUID, 'local-<id>', or None"
    elif name == "OWNERSHIP_TENANT_GUID":
        if result is not None and not _guid_v4_re().match(result):
            return f"{name}: {result!r} is not a GUID or None"
    elif name == "OWNERSHIP_DISPLAY_NAME":
        if not isinstance(result, str) or not result:
            return f"{name}: {result!r} is not a non-empty str"
    elif name == "OWNERSHIP_IS_TENANT_ADMINISTRATOR":
        if not isinstance(result, bool):
            return f"{name}: {result!r} is not a bool"
    return None


class CallerSide(Check):
    section = "hooks"
    name = "caller_side"
    severity = "FAIL"

    def run(self, ctx: Context) -> Result:
        if not ctx.hooks_describe or ctx.get_hook is None:
            return self._result("SKIP", "no hook configured")
        configured = [n for n in _CALLER_SIDE_HOOKS if _hook_configured(ctx, n)]
        if not configured:
            return self._result("SKIP", "no hook configured")
        if not ctx.jit_users:
            return self._result("SKIP", "no sample JIT users supplied")
        users = list(ctx.jit_users[: ctx.sample_users])
        for name in configured:
            fn = ctx.get_hook(name)
            if fn is None:
                continue
            for u in users:
                try:
                    result = fn(u)
                except Exception as exc:  # noqa: BLE001 - §4.5.1 item 4: class only
                    return self._result("FAIL", f"{name} raised {type(exc).__name__}")
                error = _validate_caller_result(name, result)
                if error is not None:
                    return self._result("FAIL", error)
        return self._result(
            "PASS", f"{len(configured)} hook(s) x {len(users)} user(s), all shaped"
        )


class _DirectoryCheckFailedError(Exception):
    """Raised by one of the `_check_*` helpers below to short-circuit
    `DirectorySide.run` with a FAIL detail -- keeps `run` itself a flat
    sequence of "if configured: check" lines instead of a branch per
    exception site."""


def _check_groups_of_tenant(ctx: Context) -> str | None:
    from superset_ownership.directory import GROUP_REF_KEYS, validate_page

    fn = ctx.get_hook("OWNERSHIP_GROUPS_OF_TENANT")
    try:
        page = validate_page(fn(ctx.tenant, "", 100, None), GROUP_REF_KEYS)
    except Exception as exc:
        raise _DirectoryCheckFailedError(
            f"OWNERSHIP_GROUPS_OF_TENANT raised {type(exc).__name__}"
        ) from exc
    return page["items"][0]["id"] if page["items"] else None


def _check_users_of_tenant(ctx: Context) -> None:
    from superset_ownership.directory import USER_REF_KEYS, validate_page

    fn = ctx.get_hook("OWNERSHIP_USERS_OF_TENANT")
    try:
        validate_page(fn(ctx.tenant, "", 100, None), USER_REF_KEYS)
    except Exception as exc:
        raise _DirectoryCheckFailedError(
            f"OWNERSHIP_USERS_OF_TENANT raised {type(exc).__name__}"
        ) from exc


def _check_members_of_group(ctx: Context, group_id: str) -> None:
    from superset_ownership.directory import USER_REF_KEYS, validate_page

    fn = ctx.get_hook("OWNERSHIP_MEMBERS_OF_GROUP")
    try:
        validate_page(fn(group_id, 100, None), USER_REF_KEYS)
    except Exception as exc:
        raise _DirectoryCheckFailedError(
            f"OWNERSHIP_MEMBERS_OF_GROUP raised {type(exc).__name__}"
        ) from exc


def _check_administrators_of_tenant(ctx: Context) -> list[Any]:
    from superset_ownership.directory import USER_REF_KEYS

    fn = ctx.get_hook("OWNERSHIP_ADMINISTRATORS_OF_TENANT")
    try:
        admins = fn(ctx.tenant)
    except Exception as exc:
        raise _DirectoryCheckFailedError(
            f"OWNERSHIP_ADMINISTRATORS_OF_TENANT raised {type(exc).__name__}"
        ) from exc
    if not isinstance(admins, list) or not all(
        isinstance(a, dict) and USER_REF_KEYS.issubset(a) for a in admins
    ):
        raise _DirectoryCheckFailedError(
            "OWNERSHIP_ADMINISTRATORS_OF_TENANT: malformed list[UserRef]"
        )
    return admins


def _check_user_in_group(ctx: Context, member_guid: str, group_id: str) -> None:
    fn = ctx.get_hook("OWNERSHIP_USER_IN_GROUP")
    try:
        result = fn(member_guid, group_id)
    except Exception as exc:
        raise _DirectoryCheckFailedError(
            f"OWNERSHIP_USER_IN_GROUP raised {type(exc).__name__}"
        ) from exc
    if not isinstance(result, bool):
        raise _DirectoryCheckFailedError(
            f"OWNERSHIP_USER_IN_GROUP: {result!r} is not a bool"
        )


def _check_group_exists(ctx: Context, group_id: str) -> None:
    fn = ctx.get_hook("OWNERSHIP_GROUP_EXISTS")
    try:
        result = fn(group_id)
    except Exception as exc:
        raise _DirectoryCheckFailedError(
            f"OWNERSHIP_GROUP_EXISTS raised {type(exc).__name__}"
        ) from exc
    if not isinstance(result, bool):
        raise _DirectoryCheckFailedError(
            f"OWNERSHIP_GROUP_EXISTS: {result!r} is not a bool"
        )


def _sample_group_and_user(ctx: Context) -> tuple[str | None, str | None]:
    """A group id and a member guid to sample `OWNERSHIP_USER_IN_GROUP`/
    `OWNERSHIP_GROUP_EXISTS` against, and the group id to fall back to when
    `OWNERSHIP_GROUPS_OF_TENANT` itself is not configured -- through the
    wrapped directory, never itself one of this check's assertions."""
    group_id = None
    guid = None
    if ctx.directory is not None:
        try:
            items = ctx.directory.list_groups(ctx.tenant)["items"]
            group_id = items[0]["id"] if items else None
        except Exception:  # noqa: BLE001 - only used to find something to sample
            logger.debug(
                "plugin_verify: directory_side could not list groups", exc_info=True
            )
        try:
            sampled = ctx.directory.search_users(ctx.tenant, "", limit=1)["items"]
            guid = sampled[0]["guid"] if sampled else None
        except Exception:  # noqa: BLE001 - only used to find something to sample
            logger.debug(
                "plugin_verify: directory_side could not search users", exc_info=True
            )
    return group_id, guid


def _admin_consistency_mismatches(ctx: Context, admins: list[Any]) -> list[str]:
    """§4.5.2's tenant-admin consistency rule: every administrator
    `OWNERSHIP_ADMINISTRATORS_OF_TENANT` lists must also be reported as an
    administrator by the EFFECTIVE (hook-aware) `is_tenant_administrator` --
    an administrator with no matching Superset user is skipped (directory-
    only, nothing to cross-check against)."""
    if ctx.user_by_id is None or ctx.is_tenant_administrator is None:
        return []
    mismatches: list[str] = []
    for admin_ref in admins:
        superset_id = admin_ref.get("superset_id")
        if superset_id is None:
            continue
        user = ctx.user_by_id(superset_id)
        if user is None:
            continue
        if not ctx.is_tenant_administrator(user):
            mismatches.append(admin_ref.get("guid") or str(superset_id))
    return mismatches


def _directory_side_body(  # noqa: C901 - a flat, independent checklist of
    # six optional per-hook probes; splitting further would scatter one
    # linear narrative across more indirection than it removes.
    ctx: Context,
    configured: list[str],
) -> tuple[list[str], list[Any] | None]:
    fallback_group_id, sample_guid = _sample_group_and_user(ctx)
    checked: list[str] = []
    admins: list[Any] | None = None

    if "OWNERSHIP_GROUPS_OF_TENANT" in configured:
        first_group_id = _check_groups_of_tenant(ctx)
        checked.append("OWNERSHIP_GROUPS_OF_TENANT")
    else:
        first_group_id = fallback_group_id

    if "OWNERSHIP_USERS_OF_TENANT" in configured:
        _check_users_of_tenant(ctx)
        checked.append("OWNERSHIP_USERS_OF_TENANT")

    if "OWNERSHIP_MEMBERS_OF_GROUP" in configured and first_group_id:
        _check_members_of_group(ctx, first_group_id)
        checked.append("OWNERSHIP_MEMBERS_OF_GROUP")

    if "OWNERSHIP_ADMINISTRATORS_OF_TENANT" in configured:
        admins = _check_administrators_of_tenant(ctx)
        checked.append("OWNERSHIP_ADMINISTRATORS_OF_TENANT")

    if "OWNERSHIP_USER_IN_GROUP" in configured and sample_guid and first_group_id:
        _check_user_in_group(ctx, sample_guid, first_group_id)
        checked.append("OWNERSHIP_USER_IN_GROUP")

    if "OWNERSHIP_GROUP_EXISTS" in configured and first_group_id:
        _check_group_exists(ctx, first_group_id)
        checked.append("OWNERSHIP_GROUP_EXISTS")

    return checked, admins


class DirectorySide(Check):
    section = "hooks"
    name = "directory_side"
    severity = "FAIL"

    def run(self, ctx: Context) -> Result:
        if not ctx.hooks_describe or ctx.get_hook is None:
            return self._result("SKIP", "no hook configured")
        configured = [n for n in _DIRECTORY_SIDE_HOOKS if _hook_configured(ctx, n)]
        if not configured:
            return self._result("SKIP", "no hook configured")
        if ctx.tenant is None:
            return self._result("SKIP", "no --tenant given")

        try:
            checked, admins = _directory_side_body(ctx, configured)
        except _DirectoryCheckFailedError as exc:
            return self._result("FAIL", str(exc))

        if admins:
            mismatches = _admin_consistency_mismatches(ctx, admins)
            if mismatches:
                return self._result(
                    "FAIL",
                    "OWNERSHIP_ADMINISTRATORS_OF_TENANT lists "
                    f"{mismatches} but is_tenant_administrator disagrees",
                )

        if not checked:
            return self._result("SKIP", "no directory-side hook could be sampled")
        return self._result("PASS", f"checked: {', '.join(checked)}")


@contextmanager
def _watch_sql_writes() -> Iterator[list[str]]:
    """Yields a list that accumulates INSERT/UPDATE/DELETE statements
    executed against the metadata DB engine for the duration, via
    SQLAlchemy's `before_cursor_execute` event -- `hooks/read_only`'s other
    half of "no writes", alongside `plugins.counting()` for the
    Authorizer's own write methods (§4.5.1 item 6)."""
    import re

    write_re = re.compile(r"^\s*(insert|update|delete)\b", re.IGNORECASE)
    statements: list[str] = []
    engine = None
    try:
        from superset import db

        engine = db.session.get_bind()
    except Exception:  # noqa: BLE001 - no engine to watch is not a failure here
        logger.debug(
            "plugin_verify: read_only could not get a DB engine", exc_info=True
        )

    listener = None
    if engine is not None:
        from sqlalchemy import event

        def listener(  # noqa: ANN001
            conn, cursor, statement, parameters, context, executemany
        ):
            if write_re.match(statement or ""):
                statements.append(statement)

        event.listen(engine, "before_cursor_execute", listener)
    try:
        yield statements
    finally:
        if engine is not None and listener is not None:
            from sqlalchemy import event

            event.remove(engine, "before_cursor_execute", listener)


@contextmanager
def _watch_fga_writes() -> Iterator[list[str]]:
    """M-8: `plugins.counting()` (§4.5.1 item 6) wraps the CURRENT
    registry's Authorizer/Directory OBJECTS only -- a hook that reaches
    `superset_ownership.fga`'s free functions directly, bypassing the
    Authorizer seam entirely, is invisible to it. The reference hooks in
    `qa/ivanti_pcs_example/hooks.py` do exactly this for READS
    (`fga.read_page`); a hook that did the same for a WRITE
    (`fga.write_tuple`/`fga.delete_tuple`, both of which funnel through
    `fga.write`, or a custom hook calling `fga._request` directly) would be
    caught by neither `counting()` nor `_watch_sql_writes` (the metadata
    engine only). Wraps `fga._request`, the one choke point every fga
    write AND read passes through, and records a call whose method+path
    names a write endpoint -- never blocks it: `counting()` does not block
    either, and `read_only` is a diagnostic, not a sandbox."""
    from superset_ownership import fga

    calls: list[str] = []
    real_request = fga._request  # noqa: SLF001 - the one choke point to watch

    def watching_request(method: str, path: str, *args: Any, **kwargs: Any) -> Any:
        if method.upper() == "POST" and path in ("/write", "/delete"):
            calls.append(f"{method} {path}")
        return real_request(method, path, *args, **kwargs)

    fga._request = watching_request  # noqa: SLF001
    try:
        yield calls
    finally:
        fga._request = real_request  # noqa: SLF001


def _call_ignoring_errors(fn: Callable[..., Any], args: tuple[Any, ...]) -> None:
    """`hooks/read_only` cares only whether calling a hook WROTE anything,
    not whether it answered correctly (that is `caller_side`/
    `directory_side`'s job) or raised (`fail_closed`'s) -- a raise here is
    swallowed, logged at debug, and any write already made before it is
    still on the `counting()`/`_watch_sql_writes` log."""
    try:
        fn(*args)
    except Exception:  # noqa: BLE001 - see docstring
        logger.debug("plugin_verify: read_only probe call raised", exc_info=True)


def _probe_for_writes(
    ctx: Context, name: str, args: tuple[Any, ...], sql_writes: list[str]
) -> str | None:
    """Calls a configured hook once under `plugins.counting()` AND
    `_watch_fga_writes` (M-8), with `sql_writes` (from `_watch_sql_writes`)
    cleared first -- a FAIL detail naming what it wrote, or None when it
    made no writes at all through any of the three surfaces."""
    from superset_ownership import plugins

    sql_writes.clear()
    with plugins.counting() as log, _watch_fga_writes() as fga_writes:
        _call_ignoring_errors(ctx.get_hook(name), args)
    writes = sorted({c for c in log if c in _WRITE_METHOD_NAMES})
    if writes:
        return f"{name} wrote via {writes}"
    if fga_writes:
        return f"{name} wrote directly via fga: {sorted(set(fga_writes))}"
    if sql_writes:
        return f"{name} executed a SQL write statement"
    return None


class ReadOnly(Check):
    section = "hooks"
    name = "read_only"
    severity = "FAIL"

    def run(self, ctx: Context) -> Result:
        if not ctx.hooks_describe or ctx.get_hook is None:
            return self._result("SKIP", "no hook configured")
        configured = [
            n
            for n, info in ctx.hooks_describe.items()
            if info.get("source") == "config"
        ]
        if not configured:
            return self._result("SKIP", "no hook configured")

        unknown = f"verify-nonexistent-{uuid.uuid4()}"
        with _watch_sql_writes() as sql_writes:
            for name in configured:
                if ctx.get_hook(name) is None:
                    continue
                try:
                    args = _probe_args(name, ctx, unknown)
                except KeyError:
                    continue
                error = _probe_for_writes(ctx, name, args, sql_writes)
                if error is not None:
                    return self._result("FAIL", error)
        return self._result(
            "PASS",
            f"{len(configured)} hook(s) made no writes observed via the "
            "Authorizer/Directory, fga, or the metadata engine",
        )


class CanManageNarrowOnly(Check):
    section = "hooks"
    name = "can_manage_narrow_only"
    severity = "FAIL"

    def run(self, ctx: Context) -> Result:
        hook = ctx.get_hook("OWNERSHIP_CAN_MANAGE") if ctx.get_hook else None
        if hook is None:
            return self._result("SKIP", "no hook configured")
        if ctx.can_manage_probe is None:
            return self._result("SKIP", "pass --object and --as to run this section")
        resolved = ctx.can_manage_probe()
        if resolved is None:
            return self._result(
                "SKIP", "--object/--as did not resolve to an object and a user"
            )
        user, object_state = resolved
        default_reason = object_state.get("default_reason")
        narrowed_state = dict(object_state)
        narrowed_state["default_reason"] = None
        try:
            widened = hook(user, narrowed_state)
        except Exception as exc:  # noqa: BLE001
            return self._result(
                "FAIL", f"OWNERSHIP_CAN_MANAGE raised {type(exc).__name__}"
            )
        if widened is not None:
            return self._result(
                "FAIL", f"hook widened: granted {widened!r} with no default reason"
            )
        try:
            result = hook(user, object_state)
        except Exception as exc:  # noqa: BLE001
            return self._result(
                "FAIL", f"OWNERSHIP_CAN_MANAGE raised {type(exc).__name__}"
            )
        if result not in (None, default_reason):
            return self._result(
                "FAIL",
                f"hook widened: returned {result!r}, default is {default_reason!r}",
            )
        return self._result(
            "PASS", f"default_reason={default_reason!r}, hook answered {result!r}"
        )


class FailClosed(Check):
    section = "hooks"
    name = "fail_closed"
    severity = "FAIL"

    def run(self, ctx: Context) -> Result:
        if not ctx.hooks_describe or ctx.get_hook is None:
            return self._result("SKIP", "no hook configured")
        configured = [n for n in _FAIL_CLOSED_HOOKS if _hook_configured(ctx, n)]
        if not configured:
            return self._result("SKIP", "no hook configured")

        from superset_ownership import plugin_hooks

        unknown = f"verify-nonexistent-{uuid.uuid4()}"
        tested = 0
        for name in configured:
            fn = ctx.get_hook(name)
            if fn is None:
                continue
            args = _probe_args(name, ctx, unknown, force_unknown_tenant=True)
            spec = plugin_hooks.spec_for(name)
            try:
                result = plugin_hooks.call(fn, name, *args)
            except Exception as exc:  # noqa: BLE001 - call() itself must not raise
                return self._result(
                    "FAIL", f"{name} raised {type(exc).__name__} through the registry"
                )
            expected = spec.unknown()
            # `OWNERSHIP_USERS_OF_TENANT`/`OWNERSHIP_GROUPS_OF_TENANT`'s
            # `unknown()` is `_empty_degraded_page` (`"degraded": True`) --
            # a signal `plugin_hooks.call` itself attaches ONLY on the
            # exception-fallback path (§4.5.1 item 4), never something a
            # correctly answering hook is expected to set for an ordinary
            # "no such tenant" empty result. Comparing the raw dict for
            # equality would FAIL a correct page hook on every unknown
            # input purely for lacking a key it was never asked to carry;
            # what this check actually needs from a "page" hook is an empty
            # page, degraded or not.
            if spec.kind == "page":
                ok = (
                    isinstance(result, dict)
                    and result.get("items") == []
                    and result.get("next_cursor") is None
                )
            else:
                ok = result == expected
            if not ok:
                return self._result(
                    "FAIL",
                    f"{name}: expected {expected!r} for an unknown input, "
                    f"got {result!r}",
                )
            tested += 1
        if tested == 0:
            return self._result("SKIP", "no hook configured")
        return self._result("PASS", f"{tested} hook(s) answered unknown, no raise")


# ---------------------------------------------------------------------------
# The 30-check registry (table order == spec §10 table order, hooks appended)
# ---------------------------------------------------------------------------

CHECKS: tuple[Check, ...] = (
    PluginsLoad(),
    Reachable(),
    ModelPinned(),
    RequiredRelations(),
    ScratchTupleRoundTrip(),
    GroupHasTenantTuple(),
    MemberGuidsResolve(),
    AdministratorGroupExists(),
    NoMemberInTwoTenants(),
    GroupIdsParse(),
    ManagePermissionTemplate(),
    TenantIsolation(),
    PaginationRoundTrips(),
    NotFoundVsEmpty(),
    UserInGroupAgrees(),
    DirectoryHealthCheck(),
    Latency(),
    GuidV4(),
    TenantKnownToStore(),
    NormalizeSubjectRoundTrip(),
    OwnerZeroCalls(),
    NonOwnerOneCheck(),
    PublicZeroCalls(),
    HooksResolved(),
    ShapePairRoundtrip(),
    CallerSide(),
    DirectorySide(),
    ReadOnly(),
    CanManageNarrowOnly(),
    FailClosed(),
)


def list_checks() -> list[dict[str, str]]:
    """The kit's table (section, name, severity) in registry order -- for
    documentation and tooling that wants the check list without running it
    (e.g. this module's own tests, or a future `plugin describe --checks`).
    `plugin describe` itself (`cli_plugin.py`) answers `plugins.describe()`
    -- which seam resolved to which class -- and does not call this; no
    command wires this in today (PR90 review L-2)."""
    return [
        {"section": c.section, "name": c.name, "severity": c.severity} for c in CHECKS
    ]


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _skip_rest(report: Report, checks: Sequence[Check], reason: str) -> None:
    for c in checks:
        report.add(Result(c.section, c.name, "SKIP", reason))


def _run_one(c: Check, ctx: Context) -> Result:
    """`c.run(ctx)`, guarded: a conformance kit exists to tell a third-party
    plug-in it misbehaves, so the plug-in misbehaving (raising instead of
    returning) must produce a FAIL row and let the rest of the kit continue,
    not abort the whole process with a traceback (PR90 review H-2)."""
    try:
        return c.run(ctx)
    except Exception as exc:  # noqa: BLE001 - reported as this check's result
        return Result(c.section, c.name, "FAIL", f"raised: {type(exc).__name__}: {exc}")


def run_against(ctx: Context) -> Report:
    """Run the full kit against an already-built :class:`Context`.

    Split out from :func:`run` so tests can drive the checks without ever
    calling :func:`_build_context` (and therefore without needing any of the
    sibling PCS-10243 modules to exist on this branch).
    """
    report = Report()
    by_section = {s: [c for c in CHECKS if c.section == s] for s in SECTIONS}

    load_checks = by_section["load"]
    for c in load_checks:
        report.add(_run_one(c, ctx))
    if any(r.status == "FAIL" for r in report.results):
        rest = [c for c in CHECKS if c.section != "load"]
        _skip_rest(report, rest, "plug-ins did not load")
        report.exit_code = EXIT_CANNOT_CONTINUE
        return report

    connection_checks = by_section["connection"]
    reachable_check = next(c for c in connection_checks if c.name == "reachable")
    reachable_result = _run_one(reachable_check, ctx)
    report.add(reachable_result)
    if reachable_result.status == "FAIL":
        rest = [c for c in CHECKS if c.section != "load" and c is not reachable_check]
        _skip_rest(report, rest, "connection unreachable")
        report.exit_code = EXIT_CANNOT_CONTINUE
        return report
    for c in connection_checks:
        if c is reachable_check:
            continue
        report.add(_run_one(c, ctx))

    for section in ("vocabulary", "directory", "identity", "invariants", "hooks"):
        for c in by_section[section]:
            report.add(_run_one(c, ctx))

    report.exit_code = EXIT_FAIL if not report.ok else EXIT_OK
    return report


def run(
    *,
    tenant: str | None = None,
    sample_users: int = 5,
    object_ref: str | None = None,
    as_user: str | None = None,
    latency_ms: int = 300,
    force_walk: bool = False,
    max_members: int = 500,
    ctx: Context | None = None,
) -> Report:
    """`superset ownership plugin verify`'s entry point.

    Requires an application context (the loaded registry lives on
    `app.extensions["ownership"]`). Must be called with `ctx=` already built
    to run without Superset/plug-ins available -- e.g. from a unit test.
    """
    if ctx is None:
        ctx = _build_context(
            tenant=tenant,
            sample_users=sample_users,
            object_ref=object_ref,
            as_user=as_user,
            latency_ms=latency_ms,
            force_walk=force_walk,
            max_members=max_members,
        )
    return run_against(ctx)


def _build_context(
    *,
    tenant: str | None,
    sample_users: int,
    object_ref: str | None,
    as_user: str | None,
    latency_ms: int,
    force_walk: bool,
    max_members: int = 500,
) -> Context:
    """Lazily wire a Context to the live registry (plugins, directory, fga,
    model, identity, authz), inside an application context -- the CLI's own
    path. This branch's unit tests build a :class:`Context` directly instead
    (see :func:`run_against`), so they never call this function."""
    from superset_ownership import fga, model as model_mod, plugins, settings
    from superset_ownership.api import (
        MANAGE_PERMISSION_CONFIG_KEY,
        manage_permission_role,
    )
    from superset_ownership.identity import (
        group_id,
        member_ref,
        normalize_subject,
        split_group_id,
        tenant_administrator_group,
    )

    import contextlib

    describe = dict(plugins.describe())
    hook_failures: dict[str, str] = {}
    if describe.get("degraded") and "failures" not in describe:
        # `plugins.describe()` (part 1's loader) does not (yet) surface why
        # a seam degraded -- only that it did. `Registry.failures` carries
        # the text; read it defensively here rather than block on that
        # module (PR90 review M-3; see PR-BODY-ADDENDUM.md).
        with contextlib.suppress(Exception):  # "no failure text" is the fallback
            from flask import current_app

            reg = current_app.extensions.get(plugins.EXT_KEY)
            if reg is not None:
                describe["failures"] = dict(getattr(reg, "failures", {}))
    with contextlib.suppress(Exception):  # a hook failure to read is "no text"
        from flask import current_app

        reg = current_app.extensions.get(plugins.EXT_KEY)
        if reg is not None and getattr(reg, "hooks", None) is not None:
            hook_failures = dict(getattr(reg.hooks, "failures", {}))
    authorizer = plugins.get_authorizer()
    directory = plugins.get_directory()
    identity = plugins.get_identity()
    try:
        connection = plugins.get_fga_connection()
    except Exception:
        connection = None
    fga_reachable = fga.reachable if connection is not None else None

    if force_walk and hasattr(directory, "_mode"):
        directory._mode = "always"  # noqa: SLF001 - deliberate test-mode override, see spec §10

    def fetch_model_relations() -> frozenset[str]:
        model_json = fga.get_model(connection.model_id if connection else None)
        return _relations_in_model(model_json)

    candidate_tenants = list(_discover_candidate_tenants())
    if tenant and tenant not in candidate_tenants:
        candidate_tenants.insert(0, tenant)
    elif tenant:
        candidate_tenants.remove(tenant)
        candidate_tenants.insert(0, tenant)

    return Context(
        tenant=tenant,
        sample_users=sample_users,
        object_ref=object_ref,
        as_user=as_user,
        latency_ms=latency_ms,
        force_walk=force_walk,
        describe=describe,
        authorizer=authorizer,
        directory=directory,
        identity=identity,
        connection=connection,
        fga_reachable=fga_reachable,
        required_relations=model_mod.REQUIRED,
        fetch_model_relations=fetch_model_relations,
        candidate_tenants=candidate_tenants,
        jit_users=list(_discover_jit_users(tenant, sample_users)),
        tenant_administrator_group=tenant_administrator_group,
        split_group_id=split_group_id,
        member_ref=member_ref,
        normalize_subject=normalize_subject,
        list_tenant_group_ids=lambda t: _list_tenant_group_ids(
            t, max_members=max_members
        ),
        manage_permission_role=manage_permission_role(),
        manage_permission_configured=(
            settings.get(MANAGE_PERMISSION_CONFIG_KEY, None, settings.as_str)
            is not None
        ),
        invariant_probe=_make_invariant_probe(object_ref, as_user),
        object_is_public=_make_object_is_public(object_ref),
        hooks_describe=dict(describe.get("hooks", {})),
        hook_failures=hook_failures,
        get_hook=plugins.get_hook,
        group_id=group_id,
        can_manage_probe=_make_can_manage_probe(object_ref, as_user),
        user_by_id=_user_by_id,
        is_tenant_administrator=_effective_is_tenant_administrator,
    )


class TooManyMembersError(Exception):
    """`_list_tenant_group_ids` refuses to walk a tenant with more than
    `max_members` members -- one Read per member has no other bound, so
    past this point the right verdict is SKIP-with-a-reason, not an
    unbounded store cost or (worse) a silent truncation reported as PASS."""


def _list_tenant_group_ids(tenant: str, *, max_members: int = 500) -> list[str]:
    """Every group id any of `tenant`'s members holds a `member` relation
    to, read straight off the store -- unlike `Directory.list_groups`, NOT
    filtered through `group_belongs_to_tenant` (which itself calls
    `split_group_id`), so a wrong-format id actually reaches
    `GroupIdsParse` instead of being silently excluded as "not this
    tenant's" before the check ever sees it.

    Strict throughout (PR90 review N-4): `directory.tenant_members`/
    `fga.read_page`'s non-strict defaults swallow a store failure and
    return `[]`/no tuples, which would make an unreachable or erroring
    store report `PASS every listed group id parses` -- a store error must
    surface as FAIL here, not a vacuous PASS. `read_page` (a single
    paginated Read, `object="group:"` as a type filter) replaces the
    original per-member `fga.list_objects` call, OpenFGA's most expensive
    read and the one with no strict mode at all.

    M3 (review round 1, PR #101, `review-sweep-pr101.md`): calls
    `directory.tenant_members` directly rather than the deprecated
    `fga.tenant_members` re-export (PCS-10243 #91, residue 1) -- the
    logic moved to `directory.py`; `fga.py` only re-exports it for one
    release.
    """
    from superset_ownership import directory, fga

    members = directory.tenant_members(tenant, strict=True)
    if len(members) > max_members:
        raise TooManyMembersError(
            f"{len(members)} members exceeds --max-members={max_members}"
        )
    ids: set[str] = set()
    for guid in members:
        cursor = ""
        while True:
            tuples, cursor = fga.read_page(
                {"user": f"user:{guid}", "relation": "member", "object": "group:"},
                token=cursor,
                strict=True,
            )
            ids.update(t["object"] for t in tuples)
            if not cursor:
                break
    return sorted(ids)


def _relations_in_model(model_json: Mapping[str, Any]) -> frozenset[str]:
    out: set[str] = set()
    for t in model_json.get("type_definitions", []):
        for relation in t.get("relations", {}):
            out.add(f"{t['type']}.{relation}")
    return frozenset(out)


def _discover_candidate_tenants() -> list[str]:
    """Tenant GUIDs from FAB roles named `tenant_<guid>` (backfill.py's
    candidate-role scan, reused read-only)."""
    from superset import security_manager

    from superset_ownership.identity import is_structural_tenant_role

    tenants = []
    for role in security_manager.get_all_roles():
        # A structural tenant role IS the thing we're collecting candidates
        # from (`tenant_<guid>`, and -- structurally, though not expected as
        # an actual FAB role name -- the administrator group's id); the
        # inverted condition this replaces collected every role that was
        # NOT one, which is how `tenant_isolation` ended up comparing
        # `Admin` against `Public` instead of two real tenants (PR90 review
        # B-4).
        if is_structural_tenant_role(role.name):
            guid = role.name.removeprefix("tenant_")
            if guid:
                tenants.append(guid)
    return tenants


def _discover_jit_users(tenant: str | None, sample_users: int) -> Sequence[Any]:
    # `sample_users <= 0` means "sample none" (the documented way to quiet
    # the identity section against a scratch store, README §6/M-7) -- the
    # loop below breaks on `len(users) >= sample_users`, which for
    # `sample_users == 0` is already true after the FIRST append, not
    # before it, so it always returned exactly one user regardless of what
    # was asked for (PR90 review N-3). Short-circuit instead of scanning at
    # all.
    if sample_users <= 0:
        return []
    from superset import security_manager

    role_name = f"tenant_{tenant}" if tenant else None
    users = []
    for user in security_manager.get_all_users():
        role_names = {r.name for r in user.roles}
        if role_name is not None and role_name not in role_names:
            continue
        if role_name is None and not any(r.startswith("tenant_") for r in role_names):
            continue
        users.append(user)
        if len(users) >= sample_users:
            break
    return users


def _resolve_ownership_row(
    object_ref: str,
) -> tuple[Any, str, Mapping[str, Any] | None] | None:
    """`--object`'s asset (by Superset id or uuid) plus its `ownership_object`
    row -- `(obj, asset_type, row)`, or None if the asset itself does not
    resolve. Shared by the invariant probe and the public-object check
    below, so both agree on what "this object" and "its row" mean.

    `--object` names the asset the operator can see (its Superset id, e.g.
    `chart:56`, as the runbook and README use it) or its uuid;
    `ownership_object` is keyed by the asset's real `object_uuid`, so the
    object is resolved FIRST and its own `.uuid` used for the row lookup --
    looking `ref_id` up directly against `object_uuid` means an id-form
    `--object` never matches and every caller below silently reads as SKIP.
    """
    from superset import db
    from superset.models.dashboard import Dashboard
    from superset.models.slice import Slice

    from superset_ownership.db import ownership_object

    asset_type, _, ref_id = object_ref.partition(":")
    model_cls = {"chart": Slice, "dashboard": Dashboard}.get(asset_type)
    if model_cls is None:
        return None
    obj = (
        db.session.get(model_cls, int(ref_id))
        if ref_id.isdigit()
        else db.session.query(model_cls).filter(model_cls.uuid == ref_id).one_or_none()
    )
    if obj is None:
        return None
    row = (
        db.session.execute(
            ownership_object.select().where(
                ownership_object.c.asset_type == asset_type,
                ownership_object.c.object_uuid == str(obj.uuid),
            )
        )
        .mappings()
        .first()
    )
    return obj, asset_type, row


def _make_invariant_probe(
    object_ref: str | None, as_user: str | None
) -> Callable[[str], Sequence[str] | None] | None:
    if not object_ref or not as_user:
        return None

    def probe(kind: str) -> Sequence[str] | None:
        import contextlib

        from flask_appbuilder.security.sqla.models import User
        from superset import db, security_manager
        from superset.utils.core import override_user

        from superset_ownership import plugins

        resolved = _resolve_ownership_row(object_ref)
        if resolved is None:
            return None
        obj, asset_type, row = resolved
        if kind == "public" and (row is None or row["visibility"] != "public"):
            return None
        if kind == "owner":
            if row is None or row["owner_user_id"] is None:
                return None
            user = db.session.get(User, row["owner_user_id"])
        else:
            user = security_manager.find_user(username=as_user)
        if user is None:
            return None

        with plugins.counting() as log:
            with override_user(user):
                with contextlib.suppress(Exception):
                    security_manager.raise_for_access(**{asset_type: obj})
        return tuple(log)

    return probe


def _make_object_is_public(object_ref: str | None) -> Callable[[], bool] | None:
    if not object_ref:
        return None

    def is_public() -> bool:
        resolved = _resolve_ownership_row(object_ref)
        if resolved is None:
            return False
        _, _, row = resolved
        return row is not None and row["visibility"] == "public"

    return is_public


def _make_can_manage_probe(
    object_ref: str | None, as_user: str | None
) -> Callable[[], tuple[Any, Mapping[str, Any]] | None] | None:
    """`hooks/can_manage_narrow_only`'s probe: `--object`/`--as` resolved to
    `(user, object_state)`, `object_state["default_reason"]` already the
    REAL default (`api._default_manage_reason`) computed as that user, the
    same way `_make_invariant_probe` resolves its own subject. None when
    `--object`/`--as` were not given; the returned callable answers None
    when the object or the user does not resolve (mirrors
    `_resolve_ownership_row`'s own "asset itself does not resolve" case).
    """
    if not object_ref or not as_user:
        return None

    def probe() -> tuple[Any, Mapping[str, Any]] | None:
        from superset import security_manager
        from superset.utils.core import override_user

        from superset_ownership import api as api_mod

        resolved = _resolve_ownership_row(object_ref)
        if resolved is None:
            return None
        _, _, row = resolved
        user = security_manager.find_user(username=as_user)
        if user is None:
            return None
        with override_user(user):
            default_reason = api_mod._default_manage_reason(row, user)  # noqa: SLF001
            object_state = api_mod._can_manage_object_state(  # noqa: SLF001
                row, user, default_reason
            )
        return user, object_state

    return probe


def _user_by_id(user_id: int) -> Any:
    """A Superset user by primary key, or None -- `hooks/directory_side`'s
    administrator/`is_tenant_administrator` consistency check, resolving an
    `OWNERSHIP_ADMINISTRATORS_OF_TENANT` hook's `UserRef.superset_id`."""
    from flask_appbuilder.security.sqla.models import User
    from superset import db

    return db.session.get(User, user_id)


def _effective_is_tenant_administrator(user: Any) -> bool:
    """`api.is_tenant_administrator`, already hook-aware (§4.5.1 item 3's
    precedence) -- used only to cross-check `OWNERSHIP_ADMINISTRATORS_OF_TENANT`'s
    own listing against the SAME answer every other caller in this package
    gets, never to re-verify `OWNERSHIP_IS_TENANT_ADMINISTRATOR` in
    isolation (that is `hooks/caller_side`'s job)."""
    from superset_ownership.api import is_tenant_administrator

    return is_tenant_administrator(user)


# ---------------------------------------------------------------------------
# Scratch-store fixtures
# ---------------------------------------------------------------------------


class _Fixtures:
    """Scratch-store fixture writer for `plugin verify`'s vocabulary/
    directory sections and `plugin seed-scratch`.

    Shapes reused verbatim from `qa/seed_fga.sh` (the same tenants, users and
    role groups every manual test round is already seeded with) plus one
    `group X tenant Y` tuple per group -- the additive relation this PR's
    model artefact adds (spec §9).
    """

    BROKEN_VARIANTS: tuple[str, ...] = (
        "group_without_tenant_tuple",
        "cross_tenant_member",
        "wrong_format_id",
    )

    TENANT_A = "a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40"
    TENANT_B = "b7f28e5a-9c14-4d6b-a2f0-5e3d8c1a6b92"
    ADA = "3f0a91c7-2d84-4e63-9b15-7c4e8a2f6d31"
    BEN = "6c2b48e9-5a71-4f92-8d03-2e9b7c1a4d53"
    CLEO = "9d7e35a1-8c62-4b04-a7f1-3d5e9b2c8a76"

    def group_id(self, name: str, tenant: str) -> str:
        # Mirrors identity.group_object's default "{name}_{tenant}" shape;
        # the real helper is used instead of this one whenever it is
        # available (see seed_scratch), so a configured
        # OWNERSHIP_GROUP_ID_FORMAT of "{tenant}_{name}" is still honoured.
        return f"group:{name}_{tenant}"

    def tuples(
        self,
        tenant_a: str | None = None,
        tenant_b: str | None = None,
        *,
        broken: str | None = None,
    ) -> list[dict[str, str]]:
        if broken is not None and broken not in self.BROKEN_VARIANTS:
            raise ValueError(
                f"unknown --broken variant: {broken!r}, "
                f"want one of {self.BROKEN_VARIANTS}"
            )
        a = tenant_a or self.TENANT_A
        b = tenant_b or self.TENANT_B
        dd_a = self.group_id("dashboard_designer", a)
        cd_a = self.group_id("chart_designer", a)
        ta_a = self.group_id("tenant_administrator", a)
        dd_b = self.group_id("dashboard_designer", b)

        rows = [
            {"user": f"user:{self.ADA}", "relation": "member", "object": f"tenant:{a}"},
            {"user": f"user:{self.BEN}", "relation": "member", "object": f"tenant:{a}"},
            {
                "user": f"user:{self.CLEO}",
                "relation": "member",
                "object": f"tenant:{b}",
            },
            {"user": f"user:{self.ADA}", "relation": "member", "object": dd_a},
            {"user": f"user:{self.BEN}", "relation": "member", "object": cd_a},
            {"user": f"user:{self.ADA}", "relation": "member", "object": ta_a},
            {"user": f"user:{self.CLEO}", "relation": "member", "object": dd_b},
            {"user": f"{ta_a}#member", "relation": "member", "object": dd_a},
            # group.tenant is `define tenant: [tenant#member]` (model/ownership.fga):
            # the SUBJECT is the tenant's member set, the OBJECT is the group --
            # the inverse of how earlier drafts of this fixture (and the README/
            # debate-doc rows they were copied from) read it. See PR90 review B-2.
            {"user": f"tenant:{a}#member", "relation": "tenant", "object": dd_a},
            {"user": f"tenant:{a}#member", "relation": "tenant", "object": cd_a},
            {"user": f"tenant:{a}#member", "relation": "tenant", "object": ta_a},
            {"user": f"tenant:{b}#member", "relation": "tenant", "object": dd_b},
        ]

        if broken == "group_without_tenant_tuple":
            rows = [
                r
                for r in rows
                if not (r["relation"] == "tenant" and r["object"] == dd_a)
            ]
        elif broken == "cross_tenant_member":
            rows.append(
                {
                    "user": f"user:{self.ADA}",
                    "relation": "member",
                    "object": f"tenant:{b}",
                }
            )
        elif broken == "wrong_format_id":
            # A group id in the OTHER format than the default -- fails
            # split_group_id under a "{name}_{tenant}"-configured instance.
            rows.append(
                {
                    "user": f"user:{self.BEN}",
                    "relation": "member",
                    "object": f"group:{a}_stray",
                }
            )
        return rows

    def seed(
        self,
        conn: Any,
        tenant_a: str | None = None,
        tenant_b: str | None = None,
        *,
        broken: str | None = None,
    ) -> list[dict[str, str]]:
        """Write the fixture tuples through `conn` (anything exposing
        `write_tuple(user, relation, object)`, e.g. an Authorizer)."""
        rows = self.tuples(tenant_a, tenant_b, broken=broken)
        for r in rows:
            conn.write_tuple(r["user"], r["relation"], r["object"])
        return rows

    def seed_scratch(
        self, *, api_url: str, store: str, broken: str | None = None
    ) -> dict[str, Any]:
        """`superset ownership plugin seed-scratch`'s implementation.

        Writes straight at `--store` through a one-off :class:`FgaConnection`
        this function builds itself -- never through the registry's
        authorizer (`OpenFGAAuthorizer` has no `connection=` constructor
        argument, and even if it did, its `write_tuple` would still need a
        pinned connection to target `--store` rather than whatever
        `OWNERSHIP_FGA_*` the process is configured with). `_scratch_write_tuple`
        below calls `fga.write_tuple(..., connection=...)` directly (the
        round-2 review's part 1 added the kwarg to `write`/`write_tuple`/
        `delete_tuple`/`delete_each`, the same override `get_model`/
        `list_models`/`write_model` already had).

        Refuses to run when `--store` is the same store the process is
        already configured to talk to (`OWNERSHIP_FGA_STORE` / whatever
        `plugins.get_fga_connection()` resolves from the environment): the
        whole point of a scratch store is that it is disposable, and a typo
        or a copy-pasted production URL must not silently write fixture
        tuples over real data.
        """
        configured = _configured_store_id()
        if configured is not None and configured == store:
            raise ValueError(
                f"--store {store!r} is the store this process is configured "
                "to use (OWNERSHIP_FGA_STORE / OWNERSHIP_FGA_CONFIG_PROVIDER); "
                "seed-scratch refuses to write fixtures over it. Pass a "
                "throwaway scratch store id instead."
            )
        from superset_ownership.fga_connection import FgaConnection

        connection = FgaConnection(api_url=api_url, store_id=store, model_id=None)
        writer = _ScratchConnectionWriter(connection)
        rows = self.seed(writer, broken=broken)
        return {
            "store": store,
            "api_url": api_url,
            "broken": broken,
            "written": len(rows),
        }


fixtures = _Fixtures()


def _configured_store_id() -> str | None:
    """The store id the process is currently configured to talk to, or None
    when it cannot be determined (no connection built, or the fga slice
    could not resolve one) -- in which case `seed_scratch` proceeds, since
    there is nothing to refuse against."""
    try:
        from superset_ownership import plugins

        connection = plugins.get_fga_connection()
    except Exception:  # noqa: BLE001 - "unknown" is the safe answer here too
        return None
    return getattr(connection, "store_id", None) if connection is not None else None


def _scratch_write_tuple(connection: Any, user: str, relation: str, obj: str) -> None:
    """Write one tuple straight at `connection`'s store. Raises on anything
    that is not success or an idempotent no-op -- `seed-scratch` seeds a
    fresh store and a rejected write there is a real defect (a fixture the
    shipped model does not accept), not something to swallow.

    `fga.write_tuple` now accepts `connection=` directly (spec §5, part 1
    of the round-2 review fixes), so this goes through the module's public
    surface instead of reaching for `fga._request` -- the workaround this
    function used before that kwarg existed.
    """
    from superset_ownership import fga

    try:
        fga.write_tuple(user, relation, obj, strict=True, connection=connection)
    except fga.StoreError as exc:
        raise RuntimeError(
            f"seed-scratch: write rejected for {user} {relation} {obj}: {exc} -- "
            "is the shipped model installed on this store? try: superset "
            "ownership fga install-model --store <id>"
        ) from exc


class _ScratchConnectionWriter:
    """A `write_tuple`-shaped object bound to one `FgaConnection`, so
    `_Fixtures.seed()` (written generically against "anything exposing
    write_tuple") can target `--store` without going through the registry's
    authorizer or its ambient connection. See `_Fixtures.seed_scratch`."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def write_tuple(self, user: str, relation: str, obj: str) -> bool:
        _scratch_write_tuple(self._connection, user, relation, obj)
        return True

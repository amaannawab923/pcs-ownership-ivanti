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
"""The recipient check an ownership transfer must pass (spec section 8).

Ownership never substitutes for the underlying dataset grant; it only adds
to it. `raise_for_access_bypass` (hooks.py) asserts `base_permission_holds`
BEFORE the owner fast path, so an owner without the dataset grant is denied
like anyone else. A transfer to such a recipient therefore produced an
object nobody could use: the new owner could not open it, and the previous
owner had lost it. Ownership must not change hands unless the recipient
independently holds the baseline permission.

This module is the DECISION, kept free of Flask and Superset so it can be
unit-tested on its own. The permission predicate is injected: the API passes
a predicate that closes over `hooks.base_permission_holds_for` and the asset
type, the tests pass a lambda. Whether a given user holds a given grant is
Superset's question (can_access_datasource, never a reimplementation);
whether a transfer may proceed given that answer is this module's.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional, Union

logger = logging.getLogger(__name__)

# The predicate: does `user` hold the baseline permission for `obj`? The API
# passes a Superset user, the backfill passes a user id; the predicate is
# whatever its caller closed over, so the parameter is `Any`. The one alias
# both call sites and `first_holder` share.
HoldsFn = Callable[[Any, Any], bool]

# Outcomes. `MISSING_GRANT` is the refusal the spec asks for; `UNVERIFIABLE`
# is the predicate failing to answer at all, which is refused too -- an
# ownership change is the one write that must never proceed on a guess.
# `NO_DATASET` is a property of the object, not of the recipient: a chart
# whose dataset no longer resolves, or a dashboard none of whose charts'
# datasets resolve, cannot be opened by anyone, so it is refused with
# wording that does not blame the recipient.
ALLOWED = "allowed"
MISSING_GRANT = "missing_grant"
UNVERIFIABLE = "unverifiable"
NO_DATASET = "no_dataset"

# Which read raised behind an `unverifiable` outcome (`Decision.raised`), in
# the words the server-side log uses. The traceback identifies the frame; the
# sentence is what an operator greps for, so it must name the right step.
DATASET_READ = "datasource load"
PERMISSION_CHECK = "baseline permission check"


@dataclass(frozen=True)
class Decision:
    """Whether a transfer to a recipient may proceed, and why not if not.

    `message` is safe to return to the API client. `error` is the exception
    behind an `unverifiable` outcome, kept for the server-side log and never
    for the response body: a driver error can carry host names, the failing
    statement and its parameters. `raised` names the read that failed
    (`DATASET_READ` or `PERMISSION_CHECK`) and `name_lookup_raised` records
    that the recipient's display name could not be resolved either, so the
    caller's log line can say exactly what happened.
    """

    outcome: str
    message: Optional[str] = None
    error: Optional[BaseException] = None
    raised: Optional[str] = None
    name_lookup_raised: bool = False

    @property
    def allowed(self) -> bool:
        return self.outcome == ALLOWED


def _grant_description(asset_type: str) -> str:
    # A dashboard passes when the recipient can read ANY dataset on it (the
    # same rule the read path applies); a chart has exactly one.
    if asset_type == "dashboard":
        return "any dataset on this dashboard"
    return "this chart's dataset"


def missing_grant_message(recipient_name: str, asset_type: str) -> str:
    """The 409 body: names the recipient and the grant they lack."""
    return (
        f"cannot assign ownership: {recipient_name} does not have access to "
        f"{_grant_description(asset_type)}"
    )


def unverifiable_message(recipient_name: str, asset_type: str) -> str:
    """The body when the check could not be evaluated. A fixed sentence: the
    exception stays in `Decision.error` and in the log, never here."""
    return (
        f"cannot assign ownership: could not verify whether {recipient_name} has "
        f"access to {_grant_description(asset_type)}; nothing was changed"
    )


NO_DATASET_MESSAGE = "cannot assign ownership: this chart has no dataset"
NO_DATASET_DASHBOARD_MESSAGE = (
    "cannot assign ownership: no chart on this dashboard has a dataset"
)


def no_dataset_message(asset_type: str) -> str:
    """The 409 body when the object's dataset does not resolve: about the
    object, never about the recipient. A dashboard is refused this way only
    when it has charts and none of their datasets resolves (an empty
    dashboard passes the read gate, so it passes here too)."""
    if asset_type == "dashboard":
        return NO_DATASET_DASHBOARD_MESSAGE
    return NO_DATASET_MESSAGE


# The recipient's display name: a string, or a zero-argument callable that is
# resolved only when the transfer is refused (looking the name up costs a
# query, and the allowed path never shows it).
RecipientName = Union[str, Callable[[], str], None]

# Whether the object's dataset resolves: a bool, or a zero-argument callable
# evaluated under the same guard as the predicate (reading a chart's datasource
# is a query, and it must not be the one read that escapes the guard).
HasDataset = Union[bool, Callable[[], bool]]


def _fallback_name(recipient: Any) -> str:
    return str(getattr(recipient, "username", None) or recipient)


def _resolve_name(recipient_name: RecipientName, fallback: str) -> tuple[str, bool]:
    """The name to put in a refusal, and whether looking it up failed.

    A callable is resolved here, and defensively: it is a query against the
    same database whose failure may be the reason for the refusal, so it
    falling over must not turn the documented refusal into an unhandled
    error. `fallback` is the subject reference, read before the check ran
    (see `decide`), so this needs no further read of the recipient."""
    if callable(recipient_name):
        try:
            recipient_name = recipient_name()
        except Exception:  # noqa: BLE001 - the name is cosmetic; the refusal is not
            logger.warning(
                "superset_ownership: could not resolve the display name of %r; "
                "using the subject reference in the refusal",
                fallback, exc_info=True,
            )
            return fallback, True
    return str(recipient_name or fallback), False


def decide(
    recipient: Any,
    obj: Any,
    asset_type: str,
    holds: HoldsFn,
    recipient_name: RecipientName = None,
    has_dataset: HasDataset = True,
) -> Decision:
    """May ownership of `obj` be given to `recipient`?

    `holds(recipient, obj)` answers whether the recipient carries the baseline
    permission for the object. A predicate that raises is treated as "cannot
    tell", and the transfer is refused: proceeding would produce exactly the
    unusable object this check exists to prevent, so the safe failure is a
    refusal that says why. `has_dataset` false (a chart whose dataset does
    not resolve; a dashboard none of whose charts' datasets resolve) is
    refused before the predicate is asked, with its own outcome, so the
    message describes the object rather than the recipient.

    Everything that reads the database -- `has_dataset` when it is a
    callable, the predicate, and the recipient's name on the refusal path --
    is evaluated under one guard, so a database that cannot answer always
    yields the `unverifiable` decision (fixed sentence, exception in
    `Decision.error`, the failing step in `Decision.raised`) and never an
    exception the caller did not plan for. The one attribute this module
    reads from the recipient itself, the subject reference used when the
    display name cannot be looked up, is read BEFORE the check: after a
    failed statement the session may be rolled back, and a mapped attribute
    on an expired instance is a query on a dead session.
    """
    fallback = _fallback_name(recipient)
    raised = DATASET_READ
    try:
        if not (has_dataset() if callable(has_dataset) else has_dataset):
            return Decision(NO_DATASET, no_dataset_message(asset_type))
        raised = PERMISSION_CHECK
        ok = bool(holds(recipient, obj))
    except Exception as exc:  # noqa: BLE001 - any failure to answer is a refusal
        name, name_lookup_raised = _resolve_name(recipient_name, fallback)
        return Decision(
            UNVERIFIABLE,
            unverifiable_message(name, asset_type),
            error=exc,
            raised=raised,
            name_lookup_raised=name_lookup_raised,
        )
    if ok:
        return Decision(ALLOWED)
    name, name_lookup_raised = _resolve_name(recipient_name, fallback)
    return Decision(
        MISSING_GRANT,
        missing_grant_message(name, asset_type),
        name_lookup_raised=name_lookup_raised,
    )


@dataclass(frozen=True)
class Pick:
    """`first_holder`'s answer: who was chosen (None when nobody), and which
    candidates could not be evaluated at all."""

    chosen: Optional[Any] = None
    errored: tuple[Any, ...] = ()

    @property
    def unverifiable(self) -> bool:
        """Nobody was chosen AND at least one candidate's check raised, so
        "none of them holds the grant" was never established."""
        return self.chosen is None and bool(self.errored)


def first_holder(candidates: Iterable[Any], obj: Any, holds: HoldsFn) -> Pick:
    """The first candidate (in the given order) who holds the baseline
    permission for `obj`.

    Used by the backfill's tenant-administrator fallback: administrators are
    tried in a deterministic order and the first who could actually open the
    object is recorded as its owner. A candidate whose check raises is skipped
    (not chosen), for the same reason `decide` refuses on an error; each such
    candidate is logged with its traceback and reported in `Pick.errored`, so
    the caller can tell "none holds it" from "could not tell".
    """
    errored: list[Any] = []
    for candidate in candidates:
        try:
            if holds(candidate, obj):
                return Pick(candidate, tuple(errored))
        except Exception:  # noqa: BLE001 - an unanswerable candidate is not chosen
            logger.warning(
                "superset_ownership: could not evaluate the baseline permission of "
                "candidate %r for %r; skipping it",
                candidate, getattr(obj, "id", obj), exc_info=True,
            )
            errored.append(candidate)
    return Pick(None, tuple(errored))

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
"""The recipient baseline check on an ownership transfer (spec section 8).

Sections 1-5 are pure: the permission predicate is injected, so no Flask, no
Superset. Proven:

  1. HOLDS          a recipient with the dataset grant is allowed
  2. MISSING GRANT  one without it is refused, and the message names the
                    recipient and the grant they lack (chart and dashboard
                    wording both)
  3. UNVERIFIABLE   a predicate that cannot answer refuses too, distinctly,
                    never lets the transfer through, keeps the exception OUT
                    of the message and IN `Decision.error`; the dataset read
                    and the name lookup sit under the same guard, so when
                    they fail too the decision is still that one; the
                    decision records which read raised and whether the name
                    lookup did; the subject reference is read before the
                    check, so a recipient expired by the failure still gets
                    named
  4. RECIPIENT      the predicate is asked about the RECIPIENT, never about
                    anyone else; the display name is resolved only on refusal
  5. BACKFILL       the tenant-administrator fallback picks the first
                    administrator who holds the grant, skips those who do not,
                    leaves the object unowned when none does, does NOT apply
                    the check to the attribution steps before it, evaluates
                    an administrator listed twice once, logs a skipped
                    candidate with its traceback, and reports "could
                    not tell" separately from "none holds it"

Sections 6-9 need Superset importable and are skipped cleanly where it is
not (in the container they run):

  6. PREDICATE      `base_permission_holds` against a fake security manager
                    that answers for the user in `g`: dashboard with no
                    charts, with one accessible dataset of several, with
                    none; chart with and without a dataset; the same chart
                    answered differently for two users; `dataset_resolves`
                    for a dashboard none of whose datasets resolve
  7. IMPERSONATION  `base_permission_holds_for` evaluates as the recipient,
                    restores the caller and the caller's per-request cache,
                    does so even when the check raises, leaves no `g.user`
                    behind when there was none (the backfill's situation),
                    and -- through the real `override_user` and the real
                    `base_permission_holds` -- gets the recipient's answer,
                    not the caller's, from a user-aware security manager
  8. ROUTE HELPER   `_refuse_transfer`: allowed returns None and looks up no
                    name; missing grant is 409 with the named message; a check
                    that raised is 500 with a fixed sentence, the exception
                    only in a WARNING log with its traceback -- also when the
                    name lookup raises too, and when the datasource load is
                    what raised, with no exception text in the body either
                    way, and the WARNING's sentence names the read that
                    raised; the same 500 when the failure expired the asset,
                    the recipient and the caller, because the refusal branch
                    reads no mapped attribute after the check; a chart with
                    no dataset is 409 with wording about
                    the chart, a dashboard with charts and no dataset likewise
                    about the dashboard; the audit event has before == after
                    and the refusal in `attempted`; an audit sink that cannot
                    build the record does not change the status
  9. ROUTES         `PUT .../owner` and `POST .../claim` against a real
                    SQLite mirror and outbox: a refusal writes NOTHING (mirror
                    row unchanged, outbox empty, upsert never reached); an
                    allowed transfer still updates the mirror and queues the
                    old-owner delete and new-owner write
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest
from superset_ownership import transfer
from superset_ownership.backfill import (
    _resolve_owner,
    _resolve_owner_detailed,
    ADMINISTRATOR,
    ATTRIBUTED,
    NO_CANDIDATES,
    NO_HOLDER,
    UNVERIFIABLE,
)

# The ground the direct `_refuse_transfer` calls below are attempted under;
# it is keyword-only and required, and these tests do not assert on it
# (`test_api.py` does).
OWNER = "owner"

ADA = SimpleNamespace(
    id=11, username="3f0a91c7-2d84-4e63-9b15-7c4e8a2f6d31", is_active=True, roles=[]
)
BEN = SimpleNamespace(
    id=12, username="6c2b48e9-5a71-4f92-8d03-2e9b7c1a4d53", is_active=True, roles=[]
)
CHART = SimpleNamespace(
    id=5, uuid="326fc7e5-b7f1-448e-8a6f-80d0e7ce0b64", resolved_datasource=object()
)


def always(_user, _obj):
    return True


def never(_user, _obj):
    return False


def broken(_user, _obj):
    raise RuntimeError("could not connect to server: host=metadb user=superset")


class OperationalError(Exception):
    """Stands in for the driver error: its text carries the host, the failing
    statement and its parameters, none of which may reach a client."""


class PendingRollbackError(Exception):
    """What a session answers to the NEXT query after one has failed."""


DRIVER_TEXT = (
    "could not connect to server: host=metadb user=superset\n"
    "[SQL: SELECT tables.id, tables.table_name FROM tables WHERE tables.id = %(pk)s]\n"
    "[parameters: {'pk': 42}]"
)


class Expiring:
    """A stand-in for a mapped instance. Its attributes are plain reads until
    `expire()` -- what a `rollback()` does to every instance in the session
    -- after which any read is a query, and on a dead session an error.
    `getattr(x, name, default)` does not swallow it either, exactly as it
    would not swallow the driver's."""

    def __init__(self, **attrs):
        self.__dict__["_attrs"] = attrs
        self.__dict__["_expired"] = False

    def expire(self):
        self.__dict__["_expired"] = True

    def __getattr__(self, name):
        if self.__dict__["_expired"]:
            raise PendingRollbackError(
                f"This Session's transaction has been rolled back (reading {name!r})"
            )
        try:
            return self.__dict__["_attrs"][name]
        except KeyError:
            raise AttributeError(name) from None


# --- 1. HOLDS ---------------------------------------------------------------


def test_recipient_with_grant_is_allowed():
    d = transfer.decide(BEN, CHART, "chart", always, recipient_name="Ben")
    assert d.allowed
    assert d.outcome == transfer.ALLOWED
    assert d.message is None
    assert d.error is None


# --- 2. MISSING GRANT --------------------------------------------------------


def test_recipient_without_grant_is_refused_and_message_names_the_grant():
    d = transfer.decide(BEN, CHART, "chart", never, recipient_name="Ben Lovelace")
    assert not d.allowed
    assert d.outcome == transfer.MISSING_GRANT
    assert d.message == (
        "cannot assign ownership: Ben Lovelace does not have access to this chart's dataset"
    )


def test_dashboard_wording_names_any_dataset_on_it():
    d = transfer.decide(BEN, CHART, "dashboard", never, recipient_name="Ben")
    assert d.outcome == transfer.MISSING_GRANT
    assert d.message == (
        "cannot assign ownership: Ben does not have access to any dataset on this dashboard"
    )


def test_recipient_name_defaults_to_username():
    d = transfer.decide(BEN, CHART, "chart", never)
    assert BEN.username in d.message


def test_chart_without_a_dataset_is_refused_with_wording_about_the_chart():
    asked = []

    def holds(user, obj):
        asked.append(user)
        return True

    d = transfer.decide(BEN, CHART, "chart", holds, recipient_name="Ben", has_dataset=False)
    assert not d.allowed
    assert d.outcome == transfer.NO_DATASET
    assert d.message == "cannot assign ownership: this chart has no dataset"
    assert "Ben" not in d.message, "a property of the chart, not of the recipient"
    assert asked == [], "nothing to ask: nobody can open a chart without a dataset"


def test_dashboard_whose_datasets_do_not_resolve_is_refused_with_wording_about_the_dashboard():
    d = transfer.decide(BEN, CHART, "dashboard", always, recipient_name="Ben", has_dataset=False)
    assert d.outcome == transfer.NO_DATASET
    assert d.message == "cannot assign ownership: no chart on this dashboard has a dataset"
    assert "Ben" not in d.message


# --- 3. UNVERIFIABLE ---------------------------------------------------------


def test_predicate_that_raises_refuses_distinctly_and_keeps_the_exception_out_of_the_message():
    d = transfer.decide(BEN, CHART, "chart", broken, recipient_name="Ben")
    assert not d.allowed
    assert d.outcome == transfer.UNVERIFIABLE
    assert d.message == (
        "cannot assign ownership: could not verify whether Ben has access to "
        "this chart's dataset; nothing was changed"
    )
    # The driver message (host names, statements, parameters) is for the log.
    assert "metadb" not in d.message
    assert isinstance(d.error, RuntimeError)
    assert "metadb" in str(d.error)
    # For the caller's log line: which read raised, and that the name did not.
    assert d.raised == transfer.PERMISSION_CHECK
    assert d.name_lookup_raised is False


def test_name_lookup_that_raises_falls_back_to_the_subject_reference(caplog):
    """The name is looked up on the refusal path, against the same database
    whose failure may be the reason for the refusal. It falling over must
    not turn the documented refusal into an unhandled error."""

    def name():
        raise RuntimeError("PendingRollbackError: This Session's transaction has been rolled back")

    with caplog.at_level(logging.WARNING, logger="superset_ownership.transfer"):
        d = transfer.decide(BEN, CHART, "chart", broken, recipient_name=name)
    assert d.outcome == transfer.UNVERIFIABLE
    assert d.message == (
        f"cannot assign ownership: could not verify whether {BEN.username} has "
        "access to this chart's dataset; nothing was changed"
    )
    assert "PendingRollback" not in d.message and "metadb" not in d.message
    assert isinstance(d.error, RuntimeError) and "metadb" in str(d.error), (
        "the ORIGINAL exception is the one reported, not the name lookup's"
    )
    (rec,) = [r for r in caplog.records if r.name == "superset_ownership.transfer"]
    assert "display name" in rec.getMessage() and rec.exc_info[0] is RuntimeError
    assert d.raised == transfer.PERMISSION_CHECK, "the check is what raised, not the name"
    assert d.name_lookup_raised is True, "recorded, so the caller's log line can say so"

    # The same on the missing-grant path.
    d = transfer.decide(BEN, CHART, "chart", never, recipient_name=name)
    assert d.outcome == transfer.MISSING_GRANT
    assert BEN.username in d.message
    assert d.raised is None and d.name_lookup_raised is True


def test_subject_reference_is_read_before_the_check_so_a_rolled_back_session_cannot_break_the_refusal():
    """A failed statement can leave the session rolled back, which expires
    every instance in it: the next attribute read is a query, and on a
    dead session an error. The fallback name is the one attribute this
    module reads from the recipient, and it is read before the check runs,
    so a refusal after such a failure still names the subject."""
    recipient = Expiring(id=BEN.id, username=BEN.username)

    def holds(user, obj):
        user.expire()  # what the rollback behind the failure does
        raise OperationalError(DRIVER_TEXT)

    def name():
        raise PendingRollbackError("This Session's transaction has been rolled back")

    d = transfer.decide(recipient, CHART, "chart", holds, recipient_name=name)
    assert d.outcome == transfer.UNVERIFIABLE
    assert d.message == (
        f"cannot assign ownership: could not verify whether {BEN.username} has "
        "access to this chart's dataset; nothing was changed"
    )
    assert isinstance(d.error, OperationalError)
    assert d.raised == transfer.PERMISSION_CHECK and d.name_lookup_raised is True
    with pytest.raises(PendingRollbackError):
        recipient.username  # the instance really is unreadable now


def test_dataset_read_that_raises_is_unverifiable_not_an_error():
    """`has_dataset` may be a callable: on a chart it is a lazy load of the
    datasource, a query, and it must sit under the same guard as the
    predicate."""
    asked = []

    def holds(user, obj):
        asked.append(user)
        return True

    def datasource_load():
        raise RuntimeError("could not connect to server: host=metadb user=superset")

    d = transfer.decide(BEN, CHART, "chart", holds, recipient_name="Ben", has_dataset=datasource_load)
    assert d.outcome == transfer.UNVERIFIABLE
    assert d.message == (
        "cannot assign ownership: could not verify whether Ben has access to "
        "this chart's dataset; nothing was changed"
    )
    assert "metadb" not in d.message
    assert isinstance(d.error, RuntimeError)
    assert d.raised == transfer.DATASET_READ, "named for the log: the dataset, not the check"
    assert asked == [], "the predicate is not asked about an object that could not be read"
    # A callable that answers is used like the bool.
    assert transfer.decide(BEN, CHART, "chart", holds, has_dataset=lambda: True).allowed
    assert (
        transfer.decide(BEN, CHART, "chart", holds, has_dataset=lambda: False).outcome
        == transfer.NO_DATASET
    )


def test_predicate_result_is_coerced_to_bool():
    # A predicate returning a truthy non-bool (a list of grants, say) allows;
    # an empty one refuses. The decision never depends on the exact type.
    assert transfer.decide(BEN, CHART, "chart", lambda u, o: ["grant"]).allowed
    assert transfer.decide(BEN, CHART, "chart", lambda u, o: []).outcome == transfer.MISSING_GRANT


# --- 4. RECIPIENT ------------------------------------------------------------


def test_predicate_is_asked_about_the_recipient_and_the_object():
    asked = []

    def holds(user, obj):
        asked.append((user, obj))
        return True

    transfer.decide(BEN, CHART, "chart", holds)
    assert asked == [(BEN, CHART)]


def test_display_name_is_resolved_only_when_refusing():
    calls = []

    def name():
        calls.append(1)
        return "Ben Lovelace"

    assert transfer.decide(BEN, CHART, "chart", always, recipient_name=name).allowed
    assert calls == [], "the allowed path never shows the name; do not pay for it"
    d = transfer.decide(BEN, CHART, "chart", never, recipient_name=name)
    assert calls == [1]
    assert "Ben Lovelace" in d.message
    d = transfer.decide(BEN, CHART, "chart", broken, recipient_name=name)
    assert calls == [1, 1]
    assert "Ben Lovelace" in d.message


# --- 5. BACKFILL fallback ----------------------------------------------------


def test_first_holder_picks_first_that_holds_and_reports_errored_candidates(caplog):
    def holds(uid, _obj):
        if uid == 1:
            raise RuntimeError("cannot tell")
        return uid == 3

    with caplog.at_level(logging.WARNING, logger="superset_ownership.transfer"):
        picked = transfer.first_holder([1, 2, 3, 4], CHART, holds)
    assert picked.chosen == 3
    assert picked.errored == (1,)
    assert not picked.unverifiable, "somebody was chosen; the error did not decide"
    # The skipped candidate is logged WITH its traceback, at WARNING.
    (rec,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "candidate 1" in rec.getMessage()
    assert rec.exc_info and rec.exc_info[0] is RuntimeError

    picked = transfer.first_holder([1, 2], CHART, holds)
    assert picked.chosen is None
    assert picked.errored == (1,)
    assert picked.unverifiable, "nobody chosen AND one could not be evaluated"

    picked = transfer.first_holder([2, 4], CHART, holds)
    assert picked.chosen is None and picked.errored == () and not picked.unverifiable

    assert transfer.first_holder([], CHART, always) == transfer.Pick(None, ())


def _obj(created_by=None, changed_by=None):
    return SimpleNamespace(id=5, created_by_fk=created_by, changed_by_fk=changed_by)


def test_backfill_admin_fallback_skips_administrators_without_the_grant():
    holds = lambda uid, _obj: uid in {22, 31}  # noqa: E731
    # derived admins tried first, in order; 21 lacks the grant, 22 holds it.
    assert _resolve_owner(_obj(), None, "chart", [31], [21, 22], holds=holds) == 22
    # No derived admin holds it: fall through to the single-tenant fallback.
    assert _resolve_owner(_obj(), None, "chart", [30, 31], [21], holds=holds) == 31
    r = _resolve_owner_detailed(_obj(), None, "chart", [30, 31], [21], holds=holds)
    assert (r.owner, r.reason, r.errored) == (31, ADMINISTRATOR, ())


def test_backfill_leaves_object_unowned_when_no_administrator_holds_the_grant():
    assert _resolve_owner(_obj(), None, "chart", [30], [21], holds=never) is None
    r = _resolve_owner_detailed(_obj(), None, "chart", [30], [21], holds=never)
    assert (r.owner, r.reason) == (None, NO_HOLDER)
    # No administrator at all is a different fact from "none holds it".
    r = _resolve_owner_detailed(_obj(), None, "chart", [], [], holds=never)
    assert (r.owner, r.reason) == (None, NO_CANDIDATES)


def test_backfill_reports_an_evaluation_error_as_unverifiable_not_as_none_holds(caplog):
    with caplog.at_level(logging.INFO, logger="superset_ownership"):
        r = _resolve_owner_detailed(_obj(), None, "chart", [30], [21], holds=broken)
    assert (r.owner, r.reason, r.errored) == (None, UNVERIFIABLE, (21, 30))
    verdicts = [
        rec for rec in caplog.records if rec.name == "superset_ownership.backfill"
    ]
    assert verdicts, "a per-object verdict line is logged"
    assert all(rec.levelno == logging.WARNING for rec in verdicts)
    assert all("could not evaluate" in rec.getMessage() for rec in verdicts)
    assert not any("administrator(s) [" in rec.getMessage() for rec in verdicts), (
        "the INFO verdict 'none of N administrator(s) [...] holds' was never established"
    )
    # One candidate raised, a later one holds: the object is owned, and the
    # error is still reported to the caller.
    holds = lambda uid, _obj: uid == 30 if uid != 21 else broken(uid, _obj)  # noqa: E731
    r = _resolve_owner_detailed(_obj(), None, "chart", [30], [21], holds=holds)
    assert (r.owner, r.reason, r.errored) == (30, ADMINISTRATOR, (21,))


def test_backfill_evaluates_an_administrator_listed_in_both_groups_once(caplog):
    """On a single-tenant instance the object's tenant's administrators and the
    fallback tenant's administrators are the same group; each must be
    evaluated once, logged once and counted once."""
    calls = []

    def holds(uid, _obj):
        calls.append(uid)
        if uid == 21:
            raise RuntimeError("cannot tell")
        return False

    with caplog.at_level(logging.INFO, logger="superset_ownership"):
        r = _resolve_owner_detailed(_obj(), None, "chart", [21, 22], [21, 22], holds=holds)
    assert calls == [21, 22], "no candidate evaluated twice"
    assert (r.owner, r.reason, r.errored) == (None, UNVERIFIABLE, (21,))
    tracebacks = [r for r in caplog.records if r.name == "superset_ownership.transfer"]
    verdicts = [r for r in caplog.records if r.name == "superset_ownership.backfill"]
    assert len(tracebacks) == 1 and len(verdicts) == 1

    # Order of preference is kept: the object's own tenant's administrators
    # first, then the fallback's, and an id in both is tried at its first place.
    calls.clear()
    _resolve_owner_detailed(_obj(), None, "chart", [30, 22], [22, 23], holds=holds)
    assert calls == [22, 23, 30]


def test_backfill_attribution_steps_are_not_subject_to_the_check():
    # The creator, the last editor and the configured default are recorded as
    # they are: they are attribution, not a change of hands. The predicate is
    # never consulted for them.
    asked = []

    def holds(uid, _obj):
        asked.append(uid)
        return False

    assert _resolve_owner(_obj(created_by=7), None, "chart", [30], [21], holds=holds) == 7
    assert _resolve_owner(_obj(changed_by=8), None, "chart", [30], [21], holds=holds) == 8
    assert _resolve_owner(_obj(), 9, "chart", [30], [21], holds=holds) == 9
    assert asked == []
    r = _resolve_owner_detailed(_obj(created_by=7), None, "chart", [30], [21], holds=holds)
    assert (r.owner, r.reason) == (7, ATTRIBUTED)


def test_backfill_without_a_predicate_keeps_the_previous_choice():
    assert _resolve_owner(_obj(), None, "chart", [30], [21, 22]) == 21
    assert _resolve_owner(_obj(), None, "chart", [30, 31], []) == 30


def test_backfill_skips_an_attribution_source_naming_a_user_that_does_not_exist(
    caplog, monkeypatch
):
    """Since revision 0003 owner_user_id is a foreign key into ab_user, so
    a history-derived creator who was hard-deleted since, or a mistyped
    OWNERSHIP_DEFAULT_OWNER, would be refused by the database as a raw
    IntegrityError that aborts the whole sweep. With `exists` given, such a
    source is logged and the next one tried; the object is never handed to
    a user id that names nobody."""
    from superset_ownership import backfill

    monkeypatch.setattr(backfill, "_creator_from_history", lambda asset_type, oid: 41)
    existing = {7, 8, 9, 21}
    exists = existing.__contains__

    with caplog.at_level(logging.WARNING, logger="superset_ownership.backfill"):
        # History names 41 (gone); the default names 9 (present): the default.
        assert _resolve_owner(_obj(), 9, "chart", [30], [21], exists=exists) == 9
        # History gone, default 99 gone: the administrator fallback.
        r = _resolve_owner_detailed(_obj(), 99, "chart", [30], [21], exists=exists)
        assert (r.owner, r.reason) == (21, ADMINISTRATOR)
        # A creator that exists is taken as before; nothing is asked of history.
        r = _resolve_owner(_obj(created_by=7), 99, "chart", [30], [21], exists=exists)
        assert r == 7
    skipped = [
        rec.getMessage() for rec in caplog.records if "no such user" in rec.getMessage()
    ]
    assert any("history names user 41" in m for m in skipped)
    assert any("OWNERSHIP_DEFAULT_OWNER names user 99" in m for m in skipped)
    # Without the predicate the previous behaviour holds (the pure callers).
    assert _resolve_owner(_obj(), 99, "chart", [30], [21]) == 41


# --- Superset-backed sections ------------------------------------------------


def _needs_superset():
    pytest.importorskip(
        "superset.utils.core", reason="superset not importable; test skipped"
    )


class FakeSecurityManager:
    """Only what the code under test asks of Superset's security manager.

    `can_access_datasource` answers for the user in `flask.g`, as the real one
    does, from a per-user grant table: a user-agnostic fake would let a check
    evaluated for the wrong user pass."""

    def __init__(self, grants=None, users=()):
        # {user id: {datasource objects that user may read}}
        self.grants = {uid: set(ds) for uid, ds in (grants or {}).items()}
        self.users = {u.id: u for u in users}
        self.asked: list[tuple[Any, Any]] = []  # (user id, datasource)

    def can_access_datasource(self, datasource):
        from flask import g

        user = getattr(g, "user", None)
        uid = getattr(user, "id", None)
        self.asked.append((uid, datasource))
        return datasource in self.grants.get(uid, set())

    def get_user_by_id(self, user_id):
        return self.users.get(user_id)

    def find_user(self, username=None, email=None):
        for u in self.users.values():
            if username and u.username == username:
                return u
            if email and getattr(u, "email", None) == email:
                return u
        return None


def _slice(ds, ds_id, ds_type="table"):
    return SimpleNamespace(datasource_type=ds_type, datasource_id=ds_id, resolved_datasource=ds)


# --- 6. PREDICATE ------------------------------------------------------------


def test_base_permission_holds_against_a_fake_security_manager(monkeypatch):
    _needs_superset()
    import superset
    from flask import Flask, g
    from superset_ownership import hooks

    sales, hr = object(), object()
    sm = FakeSecurityManager(grants={BEN.id: {sales}})
    monkeypatch.setattr(superset, "security_manager", sm)

    with Flask("test").app_context():
        g.user = BEN

        # Dashboard with no charts: passes (the read gate's rule).
        assert hooks.base_permission_holds(SimpleNamespace(slices=[]), "dashboard") is True
        assert sm.asked == []

        # One accessible dataset of several: passes; each dataset asked once.
        dash = SimpleNamespace(slices=[_slice(hr, 2), _slice(hr, 2), _slice(sales, 1)])
        assert hooks.base_permission_holds(dash, "dashboard") is True
        assert sm.asked == [(BEN.id, hr), (BEN.id, sales)], (
            "deduplicated by (type, id), stops at the first hit"
        )

        # None accessible: refused.
        sm.asked.clear()
        assert hooks.base_permission_holds(SimpleNamespace(slices=[_slice(hr, 2)]), "dashboard") is False

        # Chart: exactly its own dataset.
        chart = SimpleNamespace(resolved_datasource=sales)
        assert hooks.base_permission_holds(chart, "chart") is True
        assert hooks.base_permission_holds(SimpleNamespace(resolved_datasource=hr), "chart") is False
        # The same chart, a user without the grant: refused. The fake answers
        # for the user in `g`, so this is the answer the check would give if
        # it were evaluated for the wrong person.
        g.user = ADA
        assert hooks.base_permission_holds(chart, "chart") is False
        g.user = BEN

        # Chart whose dataset does not resolve: nobody passes, and the
        # security manager is not even asked.
        sm.asked.clear()
        assert hooks.base_permission_holds(SimpleNamespace(datasource=None), "chart") is False
        assert sm.asked == []
        assert hooks.dataset_resolves(SimpleNamespace(datasource=None), "chart") is False
        assert hooks.dataset_resolves(SimpleNamespace(resolved_datasource=hr), "chart") is True
        # A dashboard: an empty one resolves (it passes the read gate); one
        # with charts resolves when any chart's dataset does; one with charts
        # and no resolving dataset does not -- nobody can open it, so the
        # refusal must describe the dashboard, not the recipient.
        assert hooks.dataset_resolves(SimpleNamespace(slices=[]), "dashboard") is True
        assert hooks.dataset_resolves(
            SimpleNamespace(slices=[_slice(None, 2), _slice(hr, 1)]), "dashboard"
        ) is True
        orphaned = SimpleNamespace(slices=[_slice(None, 2), _slice(None, 3)])
        assert hooks.dataset_resolves(orphaned, "dashboard") is False
        assert hooks.base_permission_holds(orphaned, "dashboard") is False


# --- 7. IMPERSONATION --------------------------------------------------------


def test_base_permission_holds_for_evaluates_as_the_recipient_and_restores_the_caller(
    monkeypatch,
):
    """`base_permission_holds_for` must ask Superset's check AS the recipient
    and leave the caller's request exactly as it found it: `g.user` back to
    the caller, this module's per-request cache neither read by nor
    surviving from the impersonated evaluation."""
    _needs_superset()
    from flask import Flask, g
    from superset_ownership import hooks

    seen = {}

    def fake_base_permission_holds(obj, asset_type):
        seen["user"] = g.user
        seen["cache_during"] = getattr(g, "_ownership_editor_cache", "absent")
        # Something evaluated while impersonating populates the cache.
        g._ownership_editor_cache = {"chart:5": [999]}
        return g.user is BEN

    monkeypatch.setattr(hooks, "base_permission_holds", fake_base_permission_holds)

    app = Flask("test")
    with app.app_context():
        g.user = ADA  # the caller
        g._ownership_editor_cache = {"chart:5": [1]}  # the caller's cache

        assert hooks.base_permission_holds_for(BEN, CHART, "chart") is True
        assert seen["user"] is BEN
        assert seen["cache_during"] == "absent"
        # Caller restored, caller's cache restored, recipient's cache gone.
        assert g.user is ADA
        assert g._ownership_editor_cache == {"chart:5": [1]}

        assert hooks.base_permission_holds_for(ADA, CHART, "chart") is False
        assert g.user is ADA


def test_base_permission_holds_for_asks_the_security_manager_as_the_recipient(monkeypatch):
    """No fake in the middle: the real `override_user`, the real
    `base_permission_holds`, and a security manager that answers for the
    user in `g`. The caller has no grant and the recipient has it; the
    check evaluated for the recipient passes, and the same check evaluated
    for the caller does not."""
    _needs_superset()
    import superset
    from flask import Flask, g
    from superset_ownership import hooks

    sales = object()
    sm = FakeSecurityManager(grants={BEN.id: {sales}})
    monkeypatch.setattr(superset, "security_manager", sm)
    chart = SimpleNamespace(resolved_datasource=sales)

    with Flask("test").app_context():
        g.user = ADA  # the caller, who may manage the chart but not open it
        assert hooks.base_permission_holds_for(BEN, chart, "chart") is True
        assert hooks.base_permission_holds_for(ADA, chart, "chart") is False
        assert sm.asked == [(BEN.id, sales), (ADA.id, sales)]
        assert g.user is ADA


def test_base_permission_holds_for_restores_even_when_the_check_raises(monkeypatch):
    _needs_superset()
    from flask import Flask, g
    from superset_ownership import hooks

    def exploding(obj, asset_type):
        g._ownership_editor_cache = {"leak": True}
        raise RuntimeError("boom")

    monkeypatch.setattr(hooks, "base_permission_holds", exploding)

    app = Flask("test")
    with app.app_context():
        g.user = ADA
        with pytest.raises(RuntimeError):
            hooks.base_permission_holds_for(BEN, CHART, "chart")
        assert g.user is ADA
        assert not hasattr(g, "_ownership_editor_cache")


def test_base_permission_holds_for_leaves_no_user_behind_when_there_was_none(monkeypatch):
    """The backfill runs under an app context with no `g.user` at all. After
    the check, there must still be none -- on the normal path and when the
    check raised."""
    _needs_superset()
    from flask import Flask, g
    from superset_ownership import hooks

    seen = {}

    def fake(obj, asset_type):
        seen["user"] = g.user
        if asset_type == "explode":
            raise RuntimeError("boom")
        return True

    monkeypatch.setattr(hooks, "base_permission_holds", fake)

    app = Flask("test")
    with app.app_context():
        assert not hasattr(g, "user")
        assert hooks.base_permission_holds_for(BEN, CHART, "chart") is True
        assert seen["user"] is BEN
        assert not hasattr(g, "user")
        with pytest.raises(RuntimeError):
            hooks.base_permission_holds_for(BEN, CHART, "explode")
        assert not hasattr(g, "user")


# --- 8. ROUTE HELPER ---------------------------------------------------------


def _api():
    # Skip only when Superset itself is absent. `superset_ownership.api` is
    # imported plainly so that a broken import in the module under test
    # fails the run instead of turning into green skips.
    _needs_superset()
    from superset_ownership import api

    return api


def _app(records: list):
    from flask import Flask

    app = Flask("test")
    app.config["OWNERSHIP_AUDIT_SINK"] = records.append
    return app


def _row(owner_id, uuid=CHART.uuid, visibility="private"):
    return SimpleNamespace(object_uuid=uuid, owner_user_id=owner_id, visibility=visibility)


def _refusal_harness(monkeypatch, holds_for, records, names=None, name_lookup_raises=None):
    """`_refuse_transfer` with its collaborators faked. Returns the list of
    `get_user_info` calls so a test can assert the name was (not) looked up.
    `name_lookup_raises` makes the `get_user_info` fake raise that exception
    -- what a session in a failed transaction does."""
    from superset_ownership import hooks, service

    monkeypatch.setattr(hooks, "base_permission_holds_for", holds_for)
    looked_up = []

    def get_user_info(user_id):
        looked_up.append(user_id)
        if name_lookup_raises is not None:
            raise name_lookup_raises
        return {"id": user_id, "name": (names or {}).get(user_id, "Ben Lovelace")}

    monkeypatch.setattr(service, "get_user_info", get_user_info)
    return looked_up


def _body(resp):
    response, status = resp
    return status, response.get_json()


def test_refuse_transfer_allows_a_holder_without_looking_up_the_name(monkeypatch):
    api = _api()
    records: list = []
    asked = []

    def holds_for(user, obj, asset_type):
        asked.append((user, obj, asset_type))
        return True

    looked_up = _refusal_harness(monkeypatch, holds_for, records)
    with _app(records).test_request_context():
        assert api._refuse_transfer("chart", 5, CHART, _row(ADA.id), BEN, ADA, admitted_by=OWNER) is None
    assert asked == [(BEN, CHART, "chart")], "evaluated for the RECIPIENT, for this object"
    assert looked_up == [], "the display name is only needed for a refusal"
    assert records == [], "an allowed check is not an event"


def test_refuse_transfer_missing_grant_is_409_with_audit_that_describes_no_change(
    monkeypatch, caplog
):
    api = _api()
    records: list = []
    looked_up = _refusal_harness(monkeypatch, lambda u, o, t: False, records)
    with _app(records).test_request_context(headers={"X-Request-Id": "req-1"}):
        with caplog.at_level(logging.INFO, logger="superset_ownership.api"):
            resp = api._refuse_transfer("chart", 5, CHART, _row(ADA.id), BEN, ADA, admitted_by=OWNER)
    status, body = _body(resp)
    assert status == 409
    assert body == {
        "message": "cannot assign ownership: Ben Lovelace does not have access to "
        "this chart's dataset"
    }
    assert looked_up == [BEN.id]

    (event,) = records
    assert event["event"] == "ownership.owner_refused"
    assert event["actor"]["superset_id"] == ADA.id
    assert event["object"] == {"type": "chart", "id": 5, "uuid": CHART.uuid}
    # Nothing changed, and the shape says so: a consumer folding events into
    # current state reads the same owner on both sides.
    assert event["before"] == event["after"] == {"owner_user_id": ADA.id}
    assert event["attempted"] == {"owner_user_id": BEN.id, "reason": "missing_grant"}
    assert event["request_id"] == "req-1"
    assert any(
        r.levelno == logging.INFO and "refused (missing_grant" in r.getMessage()
        for r in caplog.records
    )


def _assert_no_exception_text(body):
    assert set(body) == {"message"}
    for fragment in ("metadb", "SELECT", "parameters", "pk", "Rollback", "Error", "Traceback"):
        assert fragment not in body["message"], fragment


def test_refuse_transfer_unverifiable_is_500_with_a_fixed_body_and_a_traceback_in_the_log(
    monkeypatch, caplog
):
    api = _api()
    records: list = []

    def holds_for(user, obj, asset_type):
        raise OperationalError(DRIVER_TEXT)

    _refusal_harness(monkeypatch, holds_for, records)
    with _app(records).test_request_context():
        with caplog.at_level(logging.INFO, logger="superset_ownership.api"):
            resp = api._refuse_transfer("chart", 5, CHART, _row(ADA.id), BEN, ADA, admitted_by=OWNER)
    status, body = _body(resp)
    # A local evaluation failure, not a gateway: a server error with a fixed
    # sentence. The driver message never reaches the client.
    assert status == 500
    assert body == {
        "message": "cannot assign ownership: could not verify whether Ben Lovelace "
        "has access to this chart's dataset; nothing was changed"
    }
    _assert_no_exception_text(body)

    (event,) = records
    assert event["before"] == event["after"] == {"owner_user_id": ADA.id}
    assert event["attempted"] == {"owner_user_id": BEN.id, "reason": "unverifiable"}

    # Server side: WARNING, with the traceback, naming the read that raised.
    (rec,) = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert rec.levelno == logging.WARNING
    assert "unverifiable" in rec.getMessage()
    assert rec.getMessage().endswith("the baseline permission check raised")
    assert rec.exc_info and rec.exc_info[0] is OperationalError
    assert "metadb" in caplog.text, "the exception text is in the log, not the body"


def test_refuse_transfer_is_still_the_documented_500_when_the_name_lookup_raises_too(
    monkeypatch, caplog
):
    """The database failed under the permission check, so the session is in
    a failed transaction and the display-name lookup on the refusal path
    raises as well. The route must still answer the fixed sentence, log ONE
    WARNING carrying the ORIGINAL exception, and emit the audit event --
    never escape to Superset's generic handler, whose body carries the
    driver text."""
    api = _api()
    records: list = []

    def holds_for(user, obj, asset_type):
        raise OperationalError(DRIVER_TEXT)

    looked_up = _refusal_harness(
        monkeypatch, holds_for, records,
        name_lookup_raises=PendingRollbackError("This Session's transaction has been rolled back"),
    )
    with _app(records).test_request_context():
        with caplog.at_level(logging.INFO, logger="superset_ownership.api"):
            resp = api._refuse_transfer("chart", 5, CHART, _row(ADA.id), BEN, ADA, admitted_by=OWNER)
    status, body = _body(resp)
    assert status == 500
    assert body == {
        "message": f"cannot assign ownership: could not verify whether {BEN.username} "
        "has access to this chart's dataset; nothing was changed"
    }
    _assert_no_exception_text(body)
    assert looked_up == [BEN.id], "the lookup was attempted, and its failure absorbed"

    (event,) = records
    assert event["before"] == event["after"] == {"owner_user_id": ADA.id}
    assert event["attempted"] == {"owner_user_id": BEN.id, "reason": "unverifiable"}

    (rec,) = [
        r for r in caplog.records
        if r.name == "superset_ownership.api" and r.levelno >= logging.WARNING
    ]
    assert rec.exc_info and rec.exc_info[0] is OperationalError, (
        "the original exception is the one in the log, not the name lookup's"
    )
    assert rec.getMessage().endswith(
        "the baseline permission check raised; the display-name lookup raised too"
    ), "the sentence an operator greps for says what happened"
    assert "metadb" in caplog.text


def test_refuse_transfer_is_the_documented_500_when_the_datasource_load_raises(
    monkeypatch, caplog
):
    """A chart's datasource is a lazy relationship: reading it is a query. It
    is the first read the check makes; when it is the one that fails, the
    outcome is the same fixed 500 as a failing permission check."""
    api = _api()
    records: list = []
    asked = []

    def holds_for(user, obj, asset_type):
        asked.append(user)
        return True

    class ChartWhoseDatasourceCannotLoad:
        id = 5
        uuid = CHART.uuid

        @property
        def resolved_datasource(self):
            raise OperationalError(DRIVER_TEXT)

    looked_up = _refusal_harness(monkeypatch, holds_for, records)
    with _app(records).test_request_context():
        with caplog.at_level(logging.INFO, logger="superset_ownership.api"):
            resp = api._refuse_transfer(
                "chart", 5, ChartWhoseDatasourceCannotLoad(), _row(ADA.id), BEN, ADA,
                admitted_by=OWNER,
            )
    status, body = _body(resp)
    assert status == 500
    assert body == {
        "message": "cannot assign ownership: could not verify whether Ben Lovelace "
        "has access to this chart's dataset; nothing was changed"
    }
    _assert_no_exception_text(body)
    assert asked == [], "the predicate is not consulted about an object that could not be read"
    assert looked_up == [BEN.id]

    (event,) = records
    assert event["attempted"] == {"owner_user_id": BEN.id, "reason": "unverifiable"}
    (rec,) = [
        r for r in caplog.records
        if r.name == "superset_ownership.api" and r.levelno >= logging.WARNING
    ]
    assert rec.exc_info and rec.exc_info[0] is OperationalError
    assert rec.getMessage().endswith("the datasource load raised"), (
        "the predicate was never asked; the sentence must not blame it"
    )
    assert "the baseline permission check" not in rec.getMessage()
    assert "metadb" in caplog.text


def test_refuse_transfer_is_still_the_documented_500_when_the_failure_expired_every_instance(
    monkeypatch, caplog
):
    """A statement that fails can leave the session rolled back, and a
    rollback expires every instance in it -- the asset, the recipient and
    the caller alike -- so their next attribute read is a query on a dead
    session. Nothing on the route's path rolls back today; if something
    added around the check ever does (a retry wrapper, a defensive rollback
    in a hook), the refusal branch must not be where that surfaces: it
    reads no mapped attribute after the check, so the answer is still the
    fixed 500 and the WARNING with the original exception."""
    api = _api()
    records: list = []
    asset = Expiring(id=5, uuid=CHART.uuid, resolved_datasource=object())
    recipient = Expiring(id=BEN.id, username=BEN.username, is_active=True, roles=[])
    actor = Expiring(id=ADA.id, username=ADA.username, roles=[])

    def holds_for(user, obj, asset_type):
        for instance in (asset, recipient, actor):
            instance.expire()
        raise OperationalError(DRIVER_TEXT)

    looked_up = _refusal_harness(
        monkeypatch, holds_for, records,
        name_lookup_raises=PendingRollbackError("This Session's transaction has been rolled back"),
    )
    with _app(records).test_request_context():
        with caplog.at_level(logging.INFO):
            resp = api._refuse_transfer("chart", 5, asset, None, recipient, actor, admitted_by=OWNER)
    status, body = _body(resp)
    assert status == 500
    assert body == {
        "message": f"cannot assign ownership: could not verify whether {BEN.username} "
        "has access to this chart's dataset; nothing was changed"
    }
    _assert_no_exception_text(body)
    assert looked_up == [BEN.id], "the id was read before the check, not from the dead instance"

    (rec,) = [
        r for r in caplog.records
        if r.name == "superset_ownership.api" and r.levelno >= logging.WARNING
    ]
    assert rec.exc_info and rec.exc_info[0] is OperationalError
    assert f"chart 5 owner -> {BEN.id} refused (unverifiable; by user {ADA.id})" in rec.getMessage()
    assert rec.getMessage().endswith(
        "the baseline permission check raised; the display-name lookup raised too"
    )
    # The audit record resolves the caller's roles, a read of the expired
    # caller that the emit guard absorbs: no event, an ERROR line, and the
    # status above unchanged by it.
    assert records == []
    assert any(
        r.name == "superset_ownership.audit" and "could not build audit event" in r.getMessage()
        for r in caplog.records
    )
    for instance in (asset, recipient, actor):
        with pytest.raises(PendingRollbackError):
            instance.id  # every one of them really was unreadable after the check


def test_refuse_transfer_dashboard_without_a_resolving_dataset_is_409_about_the_dashboard(
    monkeypatch,
):
    api = _api()
    records: list = []
    asked = []

    def holds_for(user, obj, asset_type):
        asked.append(user)
        return False

    _refusal_harness(monkeypatch, holds_for, records)
    orphaned = SimpleNamespace(
        id=7, uuid="c7bc10f4-0000-4000-8000-000000000007",
        slices=[_slice(None, 2), _slice(None, 3)],
    )
    with _app(records).test_request_context():
        resp = api._refuse_transfer("dashboard", 7, orphaned, None, BEN, ADA, admitted_by=OWNER)
    status, body = _body(resp)
    assert status == 409
    assert body == {"message": "cannot assign ownership: no chart on this dashboard has a dataset"}
    assert "Ben" not in body["message"]
    assert asked == []
    (event,) = records
    assert event["attempted"] == {"owner_user_id": BEN.id, "reason": "no_dataset"}


def test_refuse_transfer_chart_without_a_dataset_is_409_about_the_chart(monkeypatch):
    api = _api()
    records: list = []
    asked = []

    def holds_for(user, obj, asset_type):
        asked.append(user)
        return False

    _refusal_harness(monkeypatch, holds_for, records)
    orphan = SimpleNamespace(id=6, uuid="c7bc10f4-0000-4000-8000-000000000006", datasource=None)
    with _app(records).test_request_context():
        resp = api._refuse_transfer("chart", 6, orphan, None, BEN, ADA, admitted_by=OWNER)
    status, body = _body(resp)
    assert status == 409
    assert body == {"message": "cannot assign ownership: this chart has no dataset"}
    assert asked == []
    (event,) = records
    assert event["before"] == event["after"] == {"owner_user_id": None}
    assert event["attempted"] == {"owner_user_id": BEN.id, "reason": "no_dataset"}


def test_refuse_transfer_answers_as_documented_when_the_audit_record_cannot_be_built(
    monkeypatch, caplog
):
    """If the check failed because the database is unwell, resolving the actor
    for the audit record may fail too. The refusal must still be the
    documented status, not a 500 from the audit line."""
    api = _api()
    from superset_ownership import audit

    records: list = []
    _refusal_harness(monkeypatch, lambda u, o, t: False, records)

    def exploding_actor(user):
        raise RuntimeError("session is closed")

    monkeypatch.setattr(audit, "_actor", exploding_actor)
    with _app(records).test_request_context():
        with caplog.at_level(logging.ERROR, logger="superset_ownership.audit"):
            resp = api._refuse_transfer("chart", 5, CHART, _row(ADA.id), BEN, ADA, admitted_by=OWNER)
    status, _ = _body(resp)
    assert status == 409
    assert records == []
    assert any("could not build audit event" in r.getMessage() for r in caplog.records)


def test_audit_build_carries_attempted_only_when_given():
    from superset_ownership import audit

    plain = audit.build(audit.OWNER_ASSIGNED, before={"owner_user_id": 1}, after={"owner_user_id": 2})
    assert "attempted" not in plain, "every other event keeps its exact shape"
    refused = audit.build(
        audit.OWNER_REFUSED,
        before={"owner_user_id": 1},
        after={"owner_user_id": 1},
        attempted={"owner_user_id": 2, "reason": "missing_grant"},
    )
    assert refused["attempted"] == {"owner_user_id": 2, "reason": "missing_grant"}
    assert refused["before"] == refused["after"]


# --- 9. ROUTES ---------------------------------------------------------------


def _sqlite_db(tmp_path):
    """A real `ownership_object` / `ownership_outbox` behind `superset.db`."""
    import sqlalchemy as sa
    from sqlalchemy.orm import Session
    from superset_ownership import migrate

    uri = f"sqlite:///{tmp_path / 'routes.db'}"
    migrate.upgrade("head", uri)
    engine = sa.create_engine(uri)
    return engine, SimpleNamespace(session=Session(engine))


def _route_harness(monkeypatch, tmp_path, holds_for, caller=ADA, row_owner=ADA.id):
    """`_set_asset_owner` / `_claim_asset` with authentication, the asset
    lookup and the subject validation faked, and everything from
    `service.lookup` down (mirror row, outbox, commit) REAL on SQLite."""
    api = _api()
    import sqlalchemy as sa
    import superset
    from sqlalchemy.orm import Session
    from superset_ownership import hooks, service
    from superset_ownership.db import ownership_object, ownership_outbox

    engine, fake_db = _sqlite_db(tmp_path)
    with fake_db.session.begin():
        fake_db.session.execute(
            sa.insert(ownership_object).values(
                asset_type="chart", object_id=CHART.id, object_uuid=CHART.uuid,
                owner_user_id=row_owner, visibility="private",
            )
        )
    monkeypatch.setattr(superset, "db", fake_db)
    monkeypatch.setattr(
        superset, "security_manager", FakeSecurityManager(users=[ADA, BEN])
    )
    monkeypatch.setattr(api, "_authn", lambda mutating=False: caller)
    monkeypatch.setattr(api, "_get_asset", lambda asset_type, pk: CHART if pk == CHART.id else None)
    # The routes read the admitting rule from the seam itself; it goes into
    # the audit event as `admitted_by`, which these tests do not assert on
    # (`test_api.py` does). Stubbed as the owner: the ground that admits
    # every action, so the transfer paths are reached unconditionally.
    monkeypatch.setattr(api, "_manage_reason", lambda row, user: api.MANAGE_REASON_OWNER)
    monkeypatch.setattr(api, "_validate_subject", lambda subject, caller, **_: (True, ""))
    # The authorization store is outside this harness. The routes read the
    # object's tenant from it before a transfer or claim (an object never
    # changes tenant); an untenanted object keeps every path open.
    monkeypatch.setattr(
        api, "get_authorizer",
        lambda: SimpleNamespace(object_tenant=lambda asset_type, object_uuid, strict=False: None),
    )
    monkeypatch.setattr(hooks, "base_permission_holds_for", holds_for)
    monkeypatch.setattr(service, "get_user_info", lambda uid: {"id": uid, "name": "Ben Lovelace"})

    upserts = []
    real_upsert = service.upsert_ownership

    def spy_upsert(**kw):
        upserts.append(kw)
        return real_upsert(**kw)

    monkeypatch.setattr(service, "upsert_ownership", spy_upsert)

    def state():
        with Session(engine) as s:
            row = s.execute(
                sa.select(ownership_object).where(ownership_object.c.object_id == CHART.id)
            ).mappings().one()
            outbox_rows = [
                (r["op"], r["subject"], r["relation"], r["object"])
                for r in s.execute(sa.select(ownership_outbox).order_by(ownership_outbox.c.id)).mappings()
            ]
        return row["owner_user_id"], outbox_rows

    return api, state, upserts


def test_put_owner_refusal_writes_nothing_to_the_mirror_or_the_outbox(monkeypatch, tmp_path):
    api, state, upserts = _route_harness(monkeypatch, tmp_path, lambda u, o, t: False)
    records: list = []
    with _app(records).test_request_context(
        f"/api/v1/ownership/chart/{CHART.id}/owner", method="PUT",
        json={"subject": f"user:{BEN.username}"},
    ):
        status, body = _body(api._set_asset_owner("chart", CHART.id))
    assert status == 409
    assert "Ben Lovelace does not have access to this chart's dataset" in body["message"]
    assert upserts == [], "refused BEFORE the first write"
    assert state() == (ADA.id, []), "mirror row unchanged, outbox empty"
    assert [r["event"] for r in records] == ["ownership.owner_refused"]


def test_put_owner_unverifiable_writes_nothing_either(monkeypatch, tmp_path):
    def holds_for(u, o, t):
        raise RuntimeError("store unreachable")

    api, state, upserts = _route_harness(monkeypatch, tmp_path, holds_for)
    records: list = []
    with _app(records).test_request_context(
        f"/api/v1/ownership/chart/{CHART.id}/owner", method="PUT",
        json={"subject": f"user:{BEN.username}"},
    ):
        status, body = _body(api._set_asset_owner("chart", CHART.id))
    assert status == 500
    assert "store unreachable" not in body["message"]
    assert upserts == []
    assert state() == (ADA.id, [])


def test_put_owner_to_a_holder_still_transfers(monkeypatch, tmp_path):
    api, state, upserts = _route_harness(monkeypatch, tmp_path, lambda u, o, t: u is BEN)
    records: list = []
    with _app(records).test_request_context(
        f"/api/v1/ownership/chart/{CHART.id}/owner", method="PUT",
        json={"subject": f"user:{BEN.username}"},
    ):
        response = api._set_asset_owner("chart", CHART.id)
    assert response.status_code == 200
    assert response.get_json() == {"object_id": CHART.id, "owner_user_id": BEN.id}
    assert len(upserts) == 1 and upserts[0]["owner_user_id"] == BEN.id
    owner, outbox_rows = state()
    assert owner == BEN.id
    obj = f"chart:{CHART.uuid}"
    assert outbox_rows == [
        ("delete", f"user:{ADA.username}", "owner", obj),
        ("write", f"user:{BEN.username}", "owner", obj),
    ], "old owner tuple deleted, new one written, in that order"
    assert [r["event"] for r in records] == ["ownership.owner_assigned"]


def test_claim_by_a_caller_without_the_grant_is_refused_and_writes_nothing(monkeypatch, tmp_path):
    # BEN (say, a tenant administrator) claims an unowned chart he cannot open.
    api, state, upserts = _route_harness(
        monkeypatch, tmp_path, lambda u, o, t: False, caller=BEN, row_owner=None
    )
    records: list = []
    with _app(records).test_request_context(
        f"/api/v1/ownership/chart/{CHART.id}/claim", method="POST"
    ):
        status, body = _body(api._claim_asset("chart", CHART.id))
    assert status == 409
    assert body["message"] == (
        "cannot assign ownership: Ben Lovelace does not have access to this chart's dataset"
    )
    assert upserts == []
    assert state() == (None, [])
    (event,) = records
    assert event["event"] == "ownership.owner_refused"
    assert event["before"] == event["after"] == {"owner_user_id": None}
    assert event["attempted"] == {"owner_user_id": BEN.id, "reason": "missing_grant"}


def test_claim_by_a_holder_still_works(monkeypatch, tmp_path):
    api, state, upserts = _route_harness(
        monkeypatch, tmp_path, lambda u, o, t: True, caller=BEN, row_owner=None
    )
    records: list = []
    with _app(records).test_request_context(
        f"/api/v1/ownership/chart/{CHART.id}/claim", method="POST"
    ):
        response = api._claim_asset("chart", CHART.id)
    assert response.status_code == 200
    owner, outbox_rows = state()
    assert owner == BEN.id
    assert outbox_rows == [("write", f"user:{BEN.username}", "owner", f"chart:{CHART.uuid}")]
    assert [r["event"] for r in records] == ["ownership.owner_assigned"]

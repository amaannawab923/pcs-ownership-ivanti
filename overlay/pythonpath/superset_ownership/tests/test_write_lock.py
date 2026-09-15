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
"""The per-object write lock (`service.lock_object`): outbox id order equals
commit order because every writer on an object serialises on its row.

Three kinds of test:

  STRUCTURAL   read from the source (`ast`), no database: which functions
               call `lock_object` -- every mutating REQUEST route exactly
               once (so no route ever holds two rows and no two routes can
               wait on each other) and the hard-delete hook -- and none of
               the read paths (the hooks' bypass and resolvers, the data
               guard, the list and detail routes). The bulk paths (the hook
               over a bulk delete, the backfill, purge_tenant, teardown,
               disable, enable) lock N rows in one transaction, in
               (asset_type, object_id) order -- the collector and the
               backfill are shown visiting in that order, the lifecycle
               paths shown locking through one ordered statement before
               their writes; purge_tenant and teardown delete through one
               helper that locks the object rows before touching share
               rows. The advisory-lock key is the (asset code, object id)
               pair, distinct per object.
  SQLITE       the helper itself on a throwaway file: the row comes back as
               stored, None when absent, is recorded request-locally, costs
               one SELECT (the advisory lock is Postgres-only); the
               statement it runs compiles to `FOR UPDATE` on Postgres and to
               a plain SELECT on SQLite; upsert_ownership settles a crossed
               INSERT by re-locking and updating; the bulk delete helper
               deletes what it is scoped to, object rows locked first.
  POSTGRES     two writers on ONE object, each on its own connection to the
               live metadata database (the container's), racing "lock,
               enqueue, commit" the way two routes would. With the lock the
               outbox ids are in commit order every run -- with a row, and
               without one (the advisory lock); with `lock_object` stubbed
               to a no-op the same race is allowed to interleave, and the
               run reports how often it did. Two creators of the same row
               both succeed. A share route racing purge_tenant's row
               deletion on a surviving object completes without a deadlock.
               Two ordered multi-object transactions straddling a pair of
               objects whose string keys share a `hashtext` complete -- the
               deadlock a hashed advisory key gave them is shown absent.
               Skipped where Postgres is not reachable (the pure CI job).
               The outbox rows these tests write are marked `done` in the
               transaction that writes them, so a running drain never
               delivers them.
"""

from __future__ import annotations

import ast
import os
import threading
import time
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session
from superset_ownership import lifecycle, migrate, outbox, service
from superset_ownership.db import ownership_object, ownership_outbox, ownership_share

PACKAGE = Path(service.__file__).parent

# --------------------------------------------------------------------------- structural


def _callers_of(module_file: str, attr: str) -> dict[str, int]:
    """{function name: number of `<x>.<attr>(...)` / `<attr>(...)` calls in
    its body} for every top-level function in the module, nested functions
    included in their enclosing definition."""
    tree = ast.parse((PACKAGE / module_file).read_text())
    out: dict[str, int] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        n = 0
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            f = sub.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if name == attr:
                n += 1
        if n:
            out[node.name] = n
    return out


MUTATING_ROUTES = {
    "_set_asset_visibility",
    "_add_asset_share",
    "_remove_asset_share",
    "_set_asset_owner",
    "_claim_asset",
}
READ_ROUTES = {
    "_list_assets",
    "_get_asset_detail",
    "list_dashboards",
    "get_dashboard",
    "list_charts",
    "get_chart",
    "search_subjects",
}


def _top_level_functions(module_file: str) -> set[str]:
    tree = ast.parse((PACKAGE / module_file).read_text())
    return {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}


def test_every_mutating_route_takes_the_lock_and_no_read_route_does():
    callers = _callers_of("api.py", "lock_object")
    assert set(callers) == MUTATING_ROUTES
    assert READ_ROUTES <= _top_level_functions("api.py")
    # Read paths never lock: the detail and list GETs, and the subject
    # search, are among the module's functions and are not in the set.
    assert not (READ_ROUTES & set(callers))
    # The tenant purge route delegates to lifecycle.purge_tenant, a bulk
    # path that writes the store inline and enqueues nothing per object; its
    # DELETEs take their own row locks. Not a lock_object caller.
    assert "purge_tenant_route" not in callers


def test_each_route_locks_exactly_one_row_so_no_two_requests_can_wait_on_each_other():
    """A cycle needs a transaction holding one row while waiting for
    another. Each REQUEST route locks exactly one row, once, and never a
    second -- so two routes, on any two objects, cannot form a cycle. The
    bulk paths (the hook over a bulk delete, the backfill, purge_tenant,
    teardown, disable, enable) lock N rows in one transaction and are the
    only paths that can wait on each other; they take their rows in
    (asset_type, object_id) order (pinned below), and the advisory key
    names one object and one only (pinned below too), so two of them
    cannot cycle either."""
    callers = _callers_of("api.py", "lock_object")
    assert callers == dict.fromkeys(MUTATING_ROUTES, 1)
    # And the routes each lock the object THEY were called for, not a
    # constant or a second id.
    tree = ast.parse((PACKAGE / "api.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in MUTATING_ROUTES:
            calls = [
                sub
                for sub in ast.walk(node)
                if isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "lock_object"
            ]
            assert len(calls) == 1
            assert [a.id for a in calls[0].args] == ["asset_type", "pk"], node.name


def test_the_hard_delete_hook_locks_and_no_other_hook_or_read_path_does():
    """hooks.py holds both the delete hook (a writer: it enqueues the purge)
    and the read paths (the bypass, the resolvers, the data guard). Only the
    writer locks."""
    callers = _callers_of("hooks.py", "lock_object")
    assert callers == {"after_asset_delete": 1}
    read_paths = {
        "raise_for_access_bypass",
        "_query_filter",
        "extra_editors",
        "editor_subject_ids",
        "owners_resolver",
        "enforce_chart_data_access",
        "govern",
        "after_asset_create",
    }
    assert read_paths <= _top_level_functions("hooks.py")
    assert not (read_paths & set(callers))
    # The create paths (govern, after_asset_create) are in the read set
    # above: they lock only through upsert_ownership, never directly.


def _function(module_file: str, name: str) -> ast.FunctionDef:
    tree = ast.parse((PACKAGE / module_file).read_text())
    return next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name
    )


def _call_lines(fn: ast.FunctionDef, name: str) -> list[int]:
    """Line numbers of every `name(...)` / `<x>.name(...)` call in `fn`."""
    out = []
    for sub in ast.walk(fn):
        if not isinstance(sub, ast.Call):
            continue
        f = sub.func
        called = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
        if called == name:
            out.append(sub.lineno)
    return out


def test_the_bulk_paths_take_their_rows_in_one_order_and_delete_object_rows_first():
    """purge_tenant and teardown delete rows only through
    lifecycle.delete_object_rows, which locks the object rows (FOR UPDATE,
    ordered) before the share delete -- the share routes' order, object
    then share. disable and enable take the same ordered lock over every
    row before the bulk UPDATEs that would otherwise lock in scan order --
    and disable before its sentinel sweep, so ownership rows come before
    asset-side rows there as they do on the routes.
    (The hook's collector and the backfill are shown visiting in that
    order by the two behavioural tests below.)"""
    assert _callers_of("lifecycle.py", "delete_object_rows") == {
        "purge_tenant": 1,
        "teardown": 1,
    }
    assert _callers_of("lifecycle.py", "ownership_lock_statement") == {
        "delete_object_rows": 1,
        "disable": 1,
        "enable": 1,
    }
    # Inside the helper: the FOR UPDATE select comes before both deletes.
    helper = _function("lifecycle.py", "delete_object_rows")
    (lock_line,) = _call_lines(helper, "ownership_lock_statement")
    delete_lines = _call_lines(helper, "delete")
    assert len(delete_lines) == 2
    assert all(lock_line < d for d in delete_lines)
    # In disable and enable: the ordered lock precedes every UPDATE of the
    # object table (and, in disable, the deletes behind remove_rows).
    for name in ("disable", "enable"):
        fn = _function("lifecycle.py", name)
        (lock_line,) = _call_lines(fn, "ownership_lock_statement")
        writes = _call_lines(fn, "update") + _call_lines(fn, "delete")
        assert writes, name
        assert all(lock_line < w for w in writes), (name, lock_line, writes)
    # And in disable, before the sentinel sweep too: a route holds the
    # ownership row and THEN writes the sentinel row, so disable takes the
    # ownership rows before it strips a sentinel (enable already replays
    # its sentinels after the lock). The sweep is the `guard.suppressed`
    # block; the lock line comes before it and before every
    # remove_sentinel call.
    fn = _function("lifecycle.py", "disable")
    (lock_line,) = _call_lines(fn, "ownership_lock_statement")
    (suppressed_line,) = _call_lines(fn, "suppressed")
    strips = _call_lines(fn, "remove_sentinel")
    assert strips
    assert lock_line < suppressed_line, (lock_line, suppressed_line)
    assert all(lock_line < line for line in strips), (lock_line, strips)
    tree = ast.parse((PACKAGE / "lifecycle.py").read_text())
    # And nothing else in lifecycle.py deletes share rows before object
    # rows: disable's share delete follows its UPDATE of every object row,
    # and reconcile's orphan sweep deletes only share rows whose object has
    # NO ownership row -- nothing there for a route to hold.
    share_deleters = {
        n.name
        for n in tree.body
        if isinstance(n, ast.FunctionDef)
        and any(
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr == "delete"
            and isinstance(sub.func.value, ast.Name)
            and sub.func.value.id == "ownership_share"
            for sub in ast.walk(n)
        )
    }
    assert share_deleters == {"delete_object_rows", "disable", "reconcile"}


def test_the_collector_reraises_database_errors_and_logs_the_rest(caplog):
    """A DeadlockDetected (an OperationalError) from the hook's lock has
    already aborted the transaction: it must surface where it happened,
    not be logged as "could not collect" and resurface two statements
    later as InFailedSqlTransaction. Any other failure of the hook is
    logged and Superset's delete proceeds."""
    from unittest import mock

    from sqlalchemy.exc import OperationalError
    from superset_ownership import guard, hooks

    chart = mock.Mock(id=5, uuid="u-5")
    session = mock.Mock(deleted=[chart])
    deadlock = OperationalError("x", {}, Exception("deadlock detected"))
    with mock.patch.object(guard, "_asset_type_of", return_value="chart"):
        with mock.patch.object(hooks, "after_asset_delete", side_effect=deadlock):
            with pytest.raises(OperationalError):
                guard._collect_deleted(session)
        bug = RuntimeError("hook bug")
        with mock.patch.object(hooks, "after_asset_delete", side_effect=bug):
            with caplog.at_level("ERROR"):
                guard._collect_deleted(session)
    assert "could not collect deleted chart 5" in caplog.text


def test_the_collector_visits_a_bulk_delete_in_asset_type_then_id_order():
    from unittest import mock

    from superset_ownership import guard, hooks

    objs = [mock.Mock(id=i, uuid=f"u-{i}") for i in (9, 2, 5)]
    dashboards = [mock.Mock(id=i, uuid=f"d-{i}") for i in (4, 1)]
    session = mock.Mock(deleted=objs + dashboards)
    kinds = {id(o): "chart" for o in objs} | {id(o): "dashboard" for o in dashboards}
    seen: list[tuple[str, int]] = []
    kind_of = lambda o: kinds[id(o)]  # noqa: E731
    record = lambda t, i, u: seen.append((t, i))  # noqa: E731
    with mock.patch.object(guard, "_asset_type_of", side_effect=kind_of):
        with mock.patch.object(hooks, "after_asset_delete", side_effect=record):
            guard._collect_deleted(session)
    assert seen == [
        ("chart", 2),
        ("chart", 5),
        ("chart", 9),
        ("dashboard", 1),
        ("dashboard", 4),
    ]


def test_the_backfill_upserts_a_sweep_in_asset_type_then_id_order(monkeypatch):
    """backfill.run visits charts before dashboards and each in ascending
    id order, whatever order the tables return them in -- so the advisory
    and row locks its upserts take are taken in (asset_type, object_id)
    order, the collector's and delete_object_rows' order. Everything
    around the sweep is stubbed; what is observed is the sequence of
    upsert_ownership calls."""
    import sys
    import types
    from unittest import mock

    from superset_ownership import backfill

    class Slice:  # noqa: D401 - a stand-in model class
        pass

    class Dashboard:
        pass

    def obj(i, prefix):
        o = mock.Mock()
        o.id = i
        o.uuid = f"{prefix}-{i}"
        return o

    charts = [obj(i, "u") for i in (9, 2, 5)]
    dashboards = [obj(i, "d") for i in (4, 1)]
    rows = {Slice: charts, Dashboard: dashboards}

    superset = types.ModuleType("superset")
    superset.db = mock.Mock()
    models = types.ModuleType("superset.models")
    dashboard_mod = types.ModuleType("superset.models.dashboard")
    dashboard_mod.Dashboard = Dashboard
    slice_mod = types.ModuleType("superset.models.slice")
    slice_mod.Slice = Slice
    for name, module in (
        ("superset", superset),
        ("superset.models", models),
        ("superset.models.dashboard", dashboard_mod),
        ("superset.models.slice", slice_mod),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    from superset_ownership import db as db_module

    monkeypatch.setattr(db_module, "create_tables", lambda engine: None)
    monkeypatch.setattr(lifecycle, "all_rows", lambda model_cls: rows[model_cls])
    monkeypatch.setattr(backfill, "_default_owner", lambda: None)
    monkeypatch.setattr(backfill, "_tenant_admins", lambda: [])
    monkeypatch.setattr(backfill, "_tenant_datasources", lambda: {})
    monkeypatch.setattr(backfill, "_holds_baseline", lambda asset_type: None)
    monkeypatch.setattr(
        backfill, "_derive_tenant", lambda asset_type, o, tenant_ds: None
    )
    # The public-arming step (OWNERSHIP_PUBLIC_SCOPE=tenant) resolves the
    # sentinel subject through the stubbed superset; not this test's
    # concern (test_endpoints covers arming), so it answers "no subject".
    from superset_ownership import sentinel

    monkeypatch.setattr(sentinel, "get_sentinel_subject", lambda: None)
    monkeypatch.setattr(
        backfill,
        "_resolve_owner_detailed",
        lambda *a, **k: backfill.Resolution(1, backfill.ATTRIBUTED),
    )
    monkeypatch.setattr(
        service, "lookup", lambda asset_type, object_id, fresh=False: None
    )
    monkeypatch.setattr(service, "invalidate_all", lambda: None)
    upserts: list[tuple[str, int]] = []
    monkeypatch.setattr(
        service,
        "upsert_ownership",
        lambda **kw: upserts.append((kw["asset_type"], kw["object_id"])),
    )

    backfill.run()

    assert upserts == [
        ("chart", 2),
        ("chart", 5),
        ("chart", 9),
        ("dashboard", 1),
        ("dashboard", 4),
    ]
    superset.db.session.commit.assert_called_once()


def test_the_guard_never_locks_and_the_service_locks_only_in_the_upsert():
    """The flush guard runs on EVERY flush in the application and reads the
    row to defend the sentinel; it must not lock. In service.py the only
    caller is upsert_ownership (which updates an existing row) -- the read
    paths lookup / lookup_many / list_rows / the share readers never do."""
    assert _callers_of("guard.py", "lock_object") == {}
    # Twice in upsert_ownership: before deciding INSERT vs UPDATE, and
    # again when the INSERT lost a race to another creator (the re-lock
    # finds and holds that creator's row).
    assert _callers_of("service.py", "lock_object") == {"upsert_ownership": 2}


def test_the_outbox_docstring_names_the_lock_as_the_ordering_guarantee():
    doc = ast.get_docstring(ast.parse((PACKAGE / "outbox.py").read_text()))
    assert "A LIMIT OF THE ORDERING GUARANTEE" not in doc
    assert "ID ORDER IS COMMIT ORDER" in doc
    assert "service.lock_object" in doc
    assert "FOR UPDATE" in doc
    assert "advisory lock" in doc
    assert "object row before share rows" in doc


# ------------------------------------------------------------------ the statement


def test_the_lock_statement_is_for_update_on_postgres_and_plain_on_sqlite():
    stmt = service._lock_statement("chart", 56)
    pg = str(stmt.compile(dialect=postgresql.dialect()))
    lite = str(stmt.compile(dialect=sqlite.dialect()))
    assert pg.rstrip().endswith("FOR UPDATE")
    assert "FOR UPDATE" not in lite
    assert "FROM ownership_object" in pg
    assert "FROM ownership_object" in lite
    # And it is one row's: both key columns are in the WHERE.
    assert "ownership_object.asset_type =" in pg
    assert "ownership_object.object_id =" in pg


# --------------------------------------------------------------------------- SQLite


class _DB:
    def __init__(self, tmp_path):
        uri = f"sqlite:///{tmp_path / 'lock.db'}"
        migrate.upgrade("head", uri)
        self.engine = sa.create_engine(uri)
        self.session = Session(self.engine)
        self.statements: list[str] = []

        @sa.event.listens_for(self.engine, "before_cursor_execute")
        def _record(conn, cursor, statement, parameters, context, executemany):
            self.statements.append(statement)


@pytest.fixture
def db(tmp_path, monkeypatch):
    d = _DB(tmp_path)
    monkeypatch.setattr(service, "_db", lambda: d)
    monkeypatch.setattr(service, "_request_cache_override", None)
    monkeypatch.setattr(service, "_shared_cache_override", None)
    monkeypatch.setattr(service, "_ttl_override", 0)
    monkeypatch.setattr(service, "_shared_layer_verdict", None)
    monkeypatch.setenv("OWNERSHIP_OUTBOX_ENABLED", "false")
    yield d
    d.session.close()


def _insert(db, object_id=56, owner=1, visibility="private"):
    db.session.execute(
        sa.insert(ownership_object).values(
            asset_type="chart",
            object_id=object_id,
            object_uuid=f"u-{object_id}",
            owner_user_id=owner,
            visibility=visibility,
        )
    )
    db.session.commit()


def test_lock_object_returns_the_row_as_stored_or_none(db):
    assert service.lock_object("chart", 56) is None
    _insert(db)
    row = service.lock_object("chart", 56)
    assert row is not None
    assert (row.object_id, row.owner_user_id, row.visibility) == (56, 1, "private")
    # A string id is normalised like lookup's, and a non-id refused.
    assert service.lock_object("chart", "56").id == row.id
    with pytest.raises(TypeError):
        service.lock_object("chart", "fifty-six")


def test_lock_object_costs_one_select_and_is_recorded_request_locally(db, monkeypatch):
    _insert(db)
    rc = service._RequestCache()
    monkeypatch.setattr(service, "_request_cache_override", rc)
    db.statements.clear()

    row = service.lock_object("chart", 56)

    selects = [s for s in db.statements if s.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 1
    assert rc.rows[("chart", 56)] == row
    # The later, cached read in the same request is served from the lock's
    # result: no second SELECT.
    db.statements.clear()
    assert service.lookup("chart", 56) == row
    assert not db.statements


def test_lock_object_reads_past_a_stale_request_local_copy(db, monkeypatch):
    """The lock returns the row AS STORED even when the request already
    holds a copy -- it is the fresh read the routes rely on."""
    _insert(db)
    rc = service._RequestCache()
    monkeypatch.setattr(service, "_request_cache_override", rc)
    stale = service.lookup("chart", 56)
    with Session(db.engine) as other:
        other.execute(
            sa.update(ownership_object)
            .where(ownership_object.c.object_id == 56)
            .values(visibility="shared")
        )
        other.commit()
    assert rc.rows[("chart", 56)] is stale
    assert service.lock_object("chart", 56).visibility == "shared"


def test_upsert_takes_the_lock_before_deciding_insert_or_update(db, monkeypatch):
    calls: list[tuple] = []
    real = service.lock_object

    def spy(asset_type, object_id):
        calls.append((asset_type, object_id))
        return real(asset_type, object_id)

    monkeypatch.setattr(service, "lock_object", spy)
    # No row: nothing to lock, INSERT. Then a row: locked, UPDATE.
    service.upsert_ownership("chart", 56, "u-56", 1, "private")
    service.upsert_ownership("chart", 56, "u-56", 2, "shared")
    assert calls == [("chart", 56), ("chart", 56)]
    rows = db.session.execute(sa.select(ownership_object)).mappings().all()
    assert len(rows) == 1
    assert (rows[0]["owner_user_id"], rows[0]["visibility"]) == (2, "shared")


def test_the_advisory_lock_is_not_taken_on_sqlite(db):
    """No dialect switch reaches SQLite: the helper issues no statement
    there (the one-SELECT cost above already pins that) and says so."""
    db.statements.clear()
    assert service._take_advisory_lock("chart", 56) is False
    assert not db.statements


def test_the_advisory_lock_key_is_the_asset_code_and_the_object_id():
    """The two-int4 key: a fixed code per asset type and the object's own
    id -- no hashing, so no two objects share an advisory lock, which the
    multi-object paths' lock order depends on. An asset type without a
    code is refused, never silently left unlocked."""
    assert service._advisory_lock_key("chart", 56) == (1, 56)
    assert service._advisory_lock_key("dashboard", 56) == (2, 56)
    assert service.ADVISORY_LOCK_CLASS == {"chart": 1, "dashboard": 2}
    codes = list(service.ADVISORY_LOCK_CLASS.values())
    assert len(set(codes)) == len(codes)
    with pytest.raises(ValueError, match="no advisory-lock class"):
        service._advisory_lock_key("dataset", 56)
    # Distinct objects, distinct keys -- including the pair whose string
    # forms share a hashtext (the reviewer's reproduction).
    keys = {
        service._advisory_lock_key(t, i)
        for t, i in (
            ("chart", 21239),
            ("dashboard", 35736),
            ("dashboard", 100),
            ("chart", 100),
        )
    }
    assert len(keys) == 4


def test_upsert_settles_a_crossed_insert_by_relocking_and_updating(db, monkeypatch):
    """Two creators of the same row-less object: where nothing serialised
    them, both read "no row" and both INSERT; the second's INSERT hits the
    unique key. It used to raise out of upsert_ownership (a 500). Now the
    savepoint is rolled back, the lock is re-taken -- finding the first
    creator's row -- and that row is UPDATEd; the outer transaction stays
    usable."""
    real = service.lock_object
    answers: list = []

    def lock(asset_type, object_id):
        if answers:
            return answers.pop(0)
        return real(asset_type, object_id)

    monkeypatch.setattr(service, "lock_object", lock)
    service.upsert_ownership("chart", 56, "u-56", 1, "private")  # the first creator
    db.session.commit()
    answers.append(None)  # the second creator read "no row" before the first committed
    db.statements.clear()
    service.upsert_ownership("chart", 56, None, 2, "shared")
    db.session.commit()
    rows = db.session.execute(sa.select(ownership_object)).mappings().all()
    assert len(rows) == 1
    row = rows[0]
    assert (row["owner_user_id"], row["visibility"]) == (2, "shared")
    assert row["object_uuid"] == "u-56"  # the first creator's uuid, never blanked
    kinds = [st.split()[0].upper() for st in db.statements]
    assert {"SAVEPOINT", "INSERT", "UPDATE"} <= set(kinds)
    assert kinds.index("INSERT") < kinds.index("UPDATE")


def _insert_share(db, object_id, subject="user:probe"):
    db.session.execute(
        sa.insert(ownership_share).values(
            asset_type="chart",
            object_id=object_id,
            subject=subject,
            role="viewer",
        )
    )
    db.session.commit()


def test_delete_object_rows_locks_the_object_rows_in_order_then_deletes_scoped_rows(db):
    for oid in (56, 57, 58):
        _insert(db, object_id=oid)
        _insert_share(db, oid)
    db.statements.clear()

    assert lifecycle.delete_object_rows(db.session, "chart", [58, 56]) == (2, 2)
    db.session.commit()

    kinds = [st.split()[0].upper() for st in db.statements]
    assert kinds[:3] == ["SELECT", "DELETE", "DELETE"], db.statements
    # The lock select is ordered the way every bulk path orders its rows,
    # and it names the object table; the first delete is the share table's.
    ordered = "ORDER BY ownership_object.asset_type, ownership_object.object_id"
    assert ordered in db.statements[0]
    assert db.statements[1].upper().startswith("DELETE FROM OWNERSHIP_SHARE")
    assert db.statements[2].upper().startswith("DELETE FROM OWNERSHIP_OBJECT")
    left = db.session.execute(sa.select(ownership_object.c.object_id)).scalars().all()
    assert left == [57]
    shares = db.session.execute(sa.select(ownership_share.c.object_id)).scalars().all()
    assert shares == [57]
    # An empty scope deletes nothing; no scope at all deletes every row.
    assert lifecycle.delete_object_rows(db.session, "chart", []) == (0, 0)
    assert lifecycle.delete_object_rows(db.session) == (1, 1)
    db.session.commit()
    assert db.session.execute(sa.select(ownership_object)).all() == []


def test_the_bulk_lock_statement_is_for_update_on_postgres():
    from superset_ownership.lifecycle import ownership_lock_statement

    stmt = ownership_lock_statement("chart", [1, 2])
    pg = str(stmt.compile(dialect=postgresql.dialect()))
    assert pg.rstrip().endswith("FOR UPDATE")
    assert "ORDER BY ownership_object.asset_type, ownership_object.object_id" in pg
    lite = str(ownership_lock_statement(None, None).compile(dialect=sqlite.dialect()))
    assert "FOR UPDATE" not in lite
    assert "WHERE" not in lite


# --------------------------------------------------------------------------- Postgres
#
# Throwaway rows only: object ids 999980-999995 (charts), never used by
# anything else, created by the fixtures and removed -- with every outbox row
# written against them -- afterwards. Every test here needs the container's
# Postgres and carries the `superset` marker like the rest of the suite that
# does (`pytest -m superset`).

pytest_pg = pytest.mark.superset

OBJECT_ID = 999995
OBJECT = "chart:00000000-0000-4000-8000-000000999995"
ROWLESS_ID = 999985
ROWLESS = "chart:00000000-0000-4000-8000-000000999985"
CREATED_ID = 999986
PURGE_IDS = (999980, 999981, 999982)
THROWAWAY_IDS = tuple(range(999980, 999990)) + (OBJECT_ID,)
ROUNDS = int(os.environ.get("OWNERSHIP_LOCK_RACE_ROUNDS", "20"))
# A's window between enqueue and commit, in ms; B starts a third of the way in.
HOLD_MS = 60


def _uuid(object_id: int) -> str:
    return f"00000000-0000-4000-8000-{object_id:012d}"


def _postgres_uri() -> str | None:
    uri = os.environ.get("OWNERSHIP_TEST_PG_URI")
    if uri:
        return uri
    try:
        from superset.app import create_app  # noqa: PLC0415

        return str(create_app().config["SQLALCHEMY_DATABASE_URI"])
    except Exception:  # noqa: BLE001 - no Superset here: skip below
        return None


@pytest.fixture(scope="module")
def pg_engine():
    uri = _postgres_uri()
    if not uri or not uri.startswith("postgresql"):
        pytest.skip("no Postgres metadata database; the race runs in the container")
    engine = sa.create_engine(uri, pool_size=4, max_overflow=4)
    try:
        with engine.connect() as c:
            has_tables = c.execute(
                sa.text(
                    "select count(*) from information_schema.tables where table_name "
                    "in ('ownership_object','ownership_outbox','ownership_share')"
                )
            ).scalar()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres not reachable: {exc.__class__.__name__}")
    if has_tables != 3:
        pytest.skip("ownership tables not migrated in this database")
    yield engine
    engine.dispose()


def _throwaway_rows(table):
    return sa.and_(table.c.asset_type == "chart", table.c.object_id.in_(THROWAWAY_IDS))


def _throwaway_outbox():
    return ownership_outbox.c.object.in_([f"chart:{_uuid(i)}" for i in THROWAWAY_IDS])


def _wipe(engine):
    with engine.begin() as c:
        c.execute(sa.delete(ownership_outbox).where(_throwaway_outbox()))
        c.execute(sa.delete(ownership_share).where(_throwaway_rows(ownership_share)))
        c.execute(sa.delete(ownership_object).where(_throwaway_rows(ownership_object)))


@pytest.fixture
def clean_slate(pg_engine):
    """No throwaway row of any kind before the test, and none after."""
    _wipe(pg_engine)
    try:
        yield pg_engine
    finally:
        _wipe(pg_engine)


@pytest.fixture
def throwaway_object(clean_slate):
    """One `ownership_object` row nothing else uses."""
    with clean_slate.begin() as c:
        c.execute(
            sa.insert(ownership_object).values(
                asset_type="chart",
                object_id=OBJECT_ID,
                object_uuid=_uuid(OBJECT_ID),
                owner_user_id=None,
                visibility="private",
            )
        )


class _ThreadDB:
    """`service._db()` stand-in whose `.session` is per thread: each writer
    is its own request with its own connection, as two web workers are."""

    def __init__(self, engine):
        self.engine = engine
        self._local = threading.local()

    @property
    def session(self):
        s = getattr(self._local, "session", None)
        if s is None:
            s = self._local.session = Session(self.engine)
        return s

    def close(self):
        s = getattr(self._local, "session", None)
        if s is not None:
            s.close()
            self._local.session = None


def _enqueue_done(session, obj: str, subject: str) -> int:
    """An outbox row exactly as a route writes it -- same INSERT, same id
    allocation -- marked `done` in the same transaction, so a drain tick
    that lands during the test never claims and delivers it."""
    row_id = outbox.enqueue(outbox.OP_WRITE, obj, subject, "viewer", session=session)
    session.execute(
        sa.update(ownership_outbox)
        .where(ownership_outbox.c.id == row_id)
        .values(status=outbox.STATUS_DONE)
    )
    return row_id


def _race_once(tdb: _ThreadDB, object_id: int, obj: str) -> tuple[list[int], list[int]]:
    """Two writers on ONE object, as two routes: each takes the lock, enqueues
    an outbox row, holds its transaction open for a moment (the window
    between a route's enqueue and its commit, stretched so the race is
    real rather than a coin toss), and commits. Writer B starts a little
    after A and commits promptly, so without the lock B enqueues the higher
    id and commits first; with it, B cannot enqueue until A has committed.

    Returns (ids in commit order, ids in id order)."""
    commit_order: list[int] = []
    order_lock = threading.Lock()
    errors: list[BaseException] = []
    start = threading.Event()

    def writer(delay_ms: int, hold_ms: int, subject: str):
        try:
            start.wait()
            time.sleep(delay_ms / 1000)
            service.lock_object("chart", object_id)
            row_id = _enqueue_done(tdb.session, obj, subject)
            time.sleep(hold_ms / 1000)
            tdb.session.commit()
            with order_lock:
                commit_order.append(row_id)
        except BaseException as exc:  # noqa: BLE001 - surfaced to the test
            errors.append(exc)
            tdb.session.rollback()
        finally:
            tdb.close()

    # A: enqueue at ~0 ms, commit at ~HOLD_MS. B: enqueue at ~HOLD_MS/3,
    # commit a few ms later -- well before A, unless the lock holds it.
    a = threading.Thread(target=writer, args=(0, HOLD_MS, "user:a"))
    b = threading.Thread(target=writer, args=(HOLD_MS // 3, 5, "user:b"))
    a.start()
    b.start()
    start.set()
    a.join(30)
    b.join(30)
    assert not errors, errors
    assert len(commit_order) == 2
    return commit_order, sorted(commit_order)


def _thread_db(pg_engine, monkeypatch) -> _ThreadDB:
    tdb = _ThreadDB(pg_engine)
    monkeypatch.setattr(service, "_db", lambda: tdb)
    monkeypatch.setattr(service, "_request_cache_override", None)
    return tdb


def _run_rounds(
    pg_engine, monkeypatch, rounds: int, object_id: int = OBJECT_ID, obj: str = OBJECT
) -> int:
    """How many of `rounds` races committed out of id order."""
    tdb = _thread_db(pg_engine, monkeypatch)
    interleaved = 0
    for _ in range(rounds):
        commit_order, id_order = _race_once(tdb, object_id, obj)
        if commit_order != id_order:
            interleaved += 1
    return interleaved


@pytest_pg
@pytest.mark.parametrize("rounds", [ROUNDS])
def test_with_the_lock_outbox_ids_are_in_commit_order_every_time(
    pg_engine, throwaway_object, monkeypatch, rounds
):
    interleaved = _run_rounds(pg_engine, monkeypatch, rounds)
    assert interleaved == 0, f"{interleaved}/{rounds} out of id order WITH the lock"


@pytest_pg
@pytest.mark.parametrize("rounds", [ROUNDS])
def test_without_the_lock_the_same_race_is_allowed_to_interleave(
    pg_engine, throwaway_object, monkeypatch, rounds, capsys
):
    """A demonstration, deliberately non-failing on the count: with
    `lock_object` stubbed to a no-op the two writers no longer serialise,
    and the run reports how often the second enqueuer committed first. The
    number is expected to be high (B's commit is scheduled well before A's)
    but a machine may be slow enough for it to be lower; what the test
    shows is that nothing else forces the unlocked race into order -- the
    lock is what provides the guarantee. It fails only if a round errors
    or a writer does not commit."""
    monkeypatch.setattr(service, "lock_object", lambda asset_type, object_id: None)
    interleaved = _run_rounds(pg_engine, monkeypatch, rounds)
    with capsys.disabled():
        print(
            f"\n[write-lock] unlocked race: {interleaved}/{rounds} rounds committed "
            "out of id order"
        )


@pytest_pg
def test_lock_object_holds_an_advisory_lock_for_the_transaction(
    pg_engine, clean_slate, monkeypatch
):
    """With no row (nothing for FOR UPDATE to hold), the session still
    holds one transaction-scoped advisory lock after lock_object, and none
    once it has committed. The lock is keyed the two-int4 way: pg_locks
    shows the asset code as `classid`, the object id as `objid`, and
    `objsubid = 2` (the two-key form; a bigint key would show 1) -- a
    return to a hashed key is caught here."""
    tdb = _thread_db(pg_engine, monkeypatch)
    held = sa.text(
        "select count(*) from pg_locks "
        "where locktype = 'advisory' and pid = pg_backend_pid()"
    )
    shape = sa.text(
        "select classid, objid, objsubid from pg_locks "
        "where locktype = 'advisory' and pid = pg_backend_pid()"
    )
    try:
        assert service._take_advisory_lock("chart", ROWLESS_ID) is True
        tdb.session.rollback()
        assert service.lock_object("chart", ROWLESS_ID) is None
        assert tdb.session.execute(held).scalar() == 1
        assert tdb.session.execute(shape).all() == [
            (service.ADVISORY_LOCK_CLASS["chart"], ROWLESS_ID, 2)
        ]
        tdb.session.commit()
        assert tdb.session.execute(held).scalar() == 0
    finally:
        tdb.session.rollback()
        tdb.close()


@pytest_pg
@pytest.mark.parametrize("rounds", [ROUNDS])
def test_a_row_less_object_is_serialised_too(
    pg_engine, clean_slate, monkeypatch, rounds
):
    """The same race on an object with NO ownership row -- a pre-existing
    object the feature never recorded, or one being governed for the first
    time. FOR UPDATE matches nothing; the advisory lock is what orders the
    two writers, so the ids are in commit order every time."""
    interleaved = _run_rounds(pg_engine, monkeypatch, rounds, ROWLESS_ID, ROWLESS)
    assert interleaved == 0, f"{interleaved}/{rounds} out of id order, row-less object"


@pytest_pg
@pytest.mark.parametrize(
    "advisory", [True, False], ids=["advisory lock", "unique key only"]
)
def test_two_concurrent_creators_both_succeed_and_one_row_results(
    pg_engine, clean_slate, monkeypatch, advisory
):
    """Two writers governing the same row-less object at once (two
    adoptions, two claims, a backfill against a route). With the advisory
    lock the second waits and finds the first's row; with it disabled both
    INSERT and the second hits the unique key. Either way: neither errors,
    one row results, and it holds the last committer's values.

    On Postgres the `advisory lock` variant cannot reach upsert_ownership's
    IntegrityError branch -- the second creator waits on the advisory lock
    and then finds the row -- so it is the `unique key only` variant that
    covers the savepoint-and-relock path here (the SQLite test covers it
    without a database race)."""
    tdb = _thread_db(pg_engine, monkeypatch)
    if not advisory:
        monkeypatch.setattr(
            service, "_take_advisory_lock", lambda asset_type, object_id: False
        )
    errors: list[BaseException] = []
    commits: list[int] = []
    start = threading.Event()

    def creator(delay_ms: int, hold_ms: int, owner: int):
        try:
            start.wait()
            time.sleep(delay_ms / 1000)
            service.upsert_ownership(
                "chart", CREATED_ID, _uuid(CREATED_ID), owner, "private"
            )
            time.sleep(hold_ms / 1000)
            tdb.session.commit()
            commits.append(owner)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
            tdb.session.rollback()
        finally:
            tdb.close()

    a = threading.Thread(target=creator, args=(0, HOLD_MS, 1))
    b = threading.Thread(target=creator, args=(HOLD_MS // 3, 5, 2))
    a.start()
    b.start()
    start.set()
    a.join(30)
    b.join(30)
    assert not errors, errors
    assert sorted(commits) == [1, 2]
    with pg_engine.connect() as c:
        rows = c.execute(
            sa.select(
                ownership_object.c.owner_user_id, ownership_object.c.object_uuid
            ).where(
                ownership_object.c.asset_type == "chart",
                ownership_object.c.object_id == CREATED_ID,
            )
        ).all()
    assert len(rows) == 1
    # B cannot commit before A in either variant (it waits on the advisory
    # lock, or its INSERT waits on A's uncommitted row), so B's values win.
    assert rows[0] == (commits[-1], _uuid(CREATED_ID))
    assert commits[-1] == 2


@pytest.fixture
def surviving_objects(clean_slate):
    """Three `ownership_object` rows of one tenant's objects, each with one
    share row, the way purge_tenant finds a surviving object."""
    with clean_slate.begin() as c:
        for oid in PURGE_IDS:
            c.execute(
                sa.insert(ownership_object).values(
                    asset_type="chart",
                    object_id=oid,
                    object_uuid=_uuid(oid),
                    owner_user_id=1,
                    visibility="shared",
                )
            )
            c.execute(
                sa.insert(ownership_share).values(
                    asset_type="chart",
                    object_id=oid,
                    subject="user:probe",
                    role="viewer",
                )
            )


def _share_writes():
    """What a share route does to the share table after its lock -- the
    real service calls, on the route's own session."""
    oid, uuid = PURGE_IDS[1], _uuid(PURGE_IDS[1])
    return [
        (
            "add_share on an existing subject (UPDATE)",
            lambda: service.add_share("chart", oid, uuid, "user:probe", "editor"),
        ),
        (
            "remove_share (DELETE)",
            lambda: service.remove_share("chart", oid, uuid, "user:probe"),
        ),
        (
            "add_share on a new subject (INSERT)",
            lambda: service.add_share("chart", oid, uuid, "user:new", "viewer"),
        ),
        (
            "add_share re-inserting the subject purge deletes (INSERT, unique key)",
            lambda: service.add_share("chart", oid, uuid, "user:probe", "viewer"),
        ),
    ]


@pytest_pg
@pytest.mark.parametrize(
    "label, write", _share_writes(), ids=[w[0] for w in _share_writes()]
)
@pytest.mark.parametrize("first", ["route", "purge"])
def test_a_share_route_and_purge_tenant_on_a_surviving_object_do_not_deadlock(
    pg_engine, surviving_objects, monkeypatch, label, write, first
):
    """The inversion purge_tenant had: a share route holds the object row
    and then writes a share row; a purge that deleted share rows before
    object rows held what the route wanted while waiting for what the
    route held, and Postgres killed one side with DeadlockDetected. Here
    the purge's row deletion (lifecycle.delete_object_rows, the statements
    purge_tenant runs) and a share route's lock-then-write race on one of
    the surviving objects, in both arrival orders, and both complete.

    The route's write is the real service call; its outbox row is marked
    done in the same transaction."""
    tdb = _thread_db(pg_engine, monkeypatch)
    monkeypatch.setenv("OWNERSHIP_OUTBOX_ENABLED", "true")
    oid = PURGE_IDS[1]
    results: dict[str, str] = {}
    errors: dict[str, BaseException] = {}
    route_locked = threading.Event()
    purge_started = threading.Event()

    def route():
        try:
            if first == "purge":
                purge_started.wait(5)  # the purge holds the rows, mid store calls
            t0 = time.time()
            row = service.lock_object("chart", oid)
            route_locked.set()
            if row is None:
                # The purge got there first and the object's row is gone: the
                # real route answers 409/404 and Superset's teardown rolls the
                # request back, releasing the lock.
                tdb.session.rollback()
                results["route"] = f"object gone; rolled back, {time.time() - t0:.2f}s"
                return
            if first == "route":
                purge_started.wait(5)
                time.sleep(0.2)  # the route's store calls; the purge waits on the row
            assert write() is True
            tdb.session.execute(
                sa.update(ownership_outbox)
                .where(
                    _throwaway_outbox(),
                    ownership_outbox.c.status == outbox.STATUS_PENDING,
                )
                .values(status=outbox.STATUS_DONE)
            )
            tdb.session.commit()
            results["route"] = f"ok in {time.time() - t0:.2f}s"
        except BaseException as exc:  # noqa: BLE001
            errors["route"] = exc
            tdb.session.rollback()
        finally:
            route_locked.set()
            tdb.close()

    def purge():
        session = Session(pg_engine)
        try:
            if first == "route":
                route_locked.wait(5)  # the route holds the row, mid store calls
                purge_started.set()
            t0 = time.time()
            ids = list(PURGE_IDS)
            shares, objects = lifecycle.delete_object_rows(session, "chart", ids)
            if first == "purge":
                purge_started.set()
                time.sleep(0.2)  # purge_tenant's inline store deletes, before commit
            session.commit()
            took = time.time() - t0
            results["purge"] = f"ok in {took:.2f}s: {shares} shares, {objects} objects"
        except BaseException as exc:  # noqa: BLE001
            errors["purge"] = exc
            session.rollback()
        finally:
            purge_started.set()
            session.close()

    a = threading.Thread(target=route)
    b = threading.Thread(target=purge)
    a.start()
    b.start()
    a.join(30)
    b.join(30)
    assert not errors, {k: f"{type(v).__name__}: {v}" for k, v in errors.items()}
    assert set(results) == {"route", "purge"}, results
    # Which branch the route took is part of the claim: arriving first it
    # holds the row and writes, the purge waiting on it; arriving second
    # its FOR UPDATE returns only after the purge's commit, on a row that
    # is gone -- never a stale copy of it.
    if first == "route":
        assert results["route"].startswith("ok in"), results
    else:
        assert results["route"].startswith("object gone"), results
    assert results["purge"].startswith("ok in"), results
    # Whatever the order, the purge removed every row of the three objects
    # -- including the share row the route wrote or changed before it.
    with pg_engine.connect() as c:
        left_objects = c.execute(
            sa.select(sa.func.count())
            .select_from(ownership_object)
            .where(_throwaway_rows(ownership_object))
        ).scalar()
        left_shares = c.execute(
            sa.select(sa.func.count())
            .select_from(ownership_share)
            .where(_throwaway_rows(ownership_share))
        ).scalar()
        pending = c.execute(
            sa.select(sa.func.count())
            .select_from(ownership_outbox)
            .where(
                _throwaway_outbox(),
                ownership_outbox.c.status == outbox.STATUS_PENDING,
            )
        ).scalar()
    left = (left_objects, left_shares, pending)
    assert left == (0, 0, 0), (results, left)


# A pair of objects whose string keys share a `hashtext`, found by the
# round-2 review: `hashtext('chart:21239') = hashtext('dashboard:35736')`.
# Checked at run time; if a Postgres release changes `hashtext`, a pair is
# searched for among the first 100 k ids of each type, and the test skips
# if none is found. Advisory locks only: no table row is written for any of
# these ids.
COLLIDING_PAIR = (("chart", 21239), ("dashboard", 35736))
_ASSET_ORDER = {"chart": 0, "dashboard": 1}


def _canonical(key: tuple[str, int]) -> tuple[int, int]:
    return _ASSET_ORDER[key[0]], key[1]


def _colliding_pair(engine) -> tuple[tuple[str, int], tuple[str, int]] | None:
    """Two distinct objects, in (asset_type, object_id) order, whose
    `'type:id'` strings hash alike."""
    with engine.connect() as c:
        same = c.execute(
            sa.text("select hashtext(:a) = hashtext(:b)"),
            {"a": "%s:%d" % COLLIDING_PAIR[0], "b": "%s:%d" % COLLIDING_PAIR[1]},
        ).scalar()
        if same:
            return COLLIDING_PAIR
        found = c.execute(
            sa.text(
                "with keys as ("
                "  select 'chart' as t, g as i from generate_series(1, 100000) g"
                "  union all"
                "  select 'dashboard', g from generate_series(1, 100000) g)"
                " select a.t, a.i, b.t, b.i from keys a join keys b"
                "   on hashtext(a.t || ':' || a.i) = hashtext(b.t || ':' || b.i)"
                "  and (a.t, a.i) < (b.t, b.i)"
                " limit 1"
            )
        ).first()
    if not found:
        return None
    pair = ((found[0], found[1]), (found[2], found[3]))
    return tuple(sorted(pair, key=_canonical))  # type: ignore[return-value]


def _between(first: tuple[str, int], last: tuple[str, int]) -> tuple[str, int] | None:
    """An object strictly between two others in (asset_type, object_id)
    order, so that {first, middle} and {middle, last} are both canonically
    ordered sets."""
    if first[0] != last[0]:
        return (last[0], last[1] - 1) if last[1] > 1 else (first[0], first[1] + 1)
    return (first[0], first[1] + 1) if first[1] + 1 < last[1] else None


@pytest_pg
def test_two_ordered_multi_object_transactions_over_a_hashtext_colliding_pair_complete(
    pg_engine, monkeypatch
):
    """The multi-object paths (the hard-delete hook over a bulk delete, the
    backfill) take one advisory lock per object in (asset_type, object_id)
    order. With the key hashed to 32 bits, two distinct objects X < Z could
    share a lock; then T1 over {X, Y} holds the shared lock for X and
    waits for Y, while T2 over {Y, Z} holds Y and waits for the shared lock
    for Z -- a cycle the sort cannot see, and DeadlockDetected after
    deadlock_timeout. With the (asset code, object id) key there is no
    shared lock: both transactions complete. Real helper, per-thread
    sessions, advisory locks only."""
    pair = _colliding_pair(pg_engine)
    if pair is None:
        pytest.skip("no hashtext-colliding pair among the first 100k ids of each type")
    first, last = pair
    middle = _between(first, last)
    if middle is None:
        pytest.skip(f"no object between {first} and {last} in canonical order")
    assert _canonical(first) < _canonical(middle) < _canonical(last)
    with pg_engine.connect() as c:
        assert (
            c.execute(
                sa.text("select hashtext(:a) = hashtext(:b)"),
                {
                    "a": "%s:%d" % first,
                    "b": "%s:%d" % last,
                },
            ).scalar()
            is True
        )
        assert (
            c.execute(
                sa.text("select hashtext(:a) = hashtext(:b)"),
                {
                    "a": "%s:%d" % first,
                    "b": "%s:%d" % middle,
                },
            ).scalar()
            is False
        )

    tdb = _thread_db(pg_engine, monkeypatch)
    holding = {"t1": threading.Event(), "t2": threading.Event()}
    errors: dict[str, BaseException] = {}
    results: dict[str, str] = {}

    def transaction(name: str, objects, other: str):
        session = tdb.session
        try:
            # Bounded either way: a deadlock is reported after
            # deadlock_timeout (1 s by default), anything else after this.
            session.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
            t0 = time.time()
            head, tail = objects
            assert service._take_advisory_lock(*head) is True
            holding[name].set()
            # Each side holds its first object before asking for its second.
            assert holding[other].wait(5)
            assert service._take_advisory_lock(*tail) is True
            session.commit()
            results[name] = f"ok in {time.time() - t0:.2f}s"
        except BaseException as exc:  # noqa: BLE001 - surfaced to the test
            errors[name] = exc
            session.rollback()
        finally:
            holding[name].set()
            tdb.close()

    t1 = threading.Thread(target=transaction, args=("t1", (first, middle), "t2"))
    t2 = threading.Thread(target=transaction, args=("t2", (middle, last), "t1"))
    t1.start()
    t2.start()
    t1.join(30)
    t2.join(30)
    assert not errors, {k: f"{type(v).__name__}: {v}" for k, v in errors.items()}
    assert set(results) == {"t1", "t2"}, results
    # Nothing left held on any of the three objects' keys.
    keys = [service._advisory_lock_key(*o) for o in (first, middle, last)]
    with pg_engine.connect() as c:
        left = c.execute(
            sa.text(
                "select classid, objid from pg_locks "
                "where locktype = 'advisory' and objsubid = 2"
            )
        ).all()
    assert not [k for k in left if tuple(k) in keys], left

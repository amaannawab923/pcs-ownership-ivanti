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
"""The ownership-row lookup cache (spec 6.2: one cached lookup per request).

Pure: a throwaway SQLite database built by the real migration chain, a
counting engine, a dict standing in for `flask.g` and a dict-backed fake
standing in for Flask-Caching. No Flask, no Superset.

Proven:
  1. ONE HIT       N lookups of one object in one request cost one SELECT --
                   negative results included
  2. NO CONTEXT    with no request store and no shared cache every call
                   queries, exactly as before (CLI, tests)
  3. WRITE         every writer invalidates: the next lookup re-reads and
                   returns the new value, never the stale one
  4. BATCH         lookup_many is one query for any number of ids, and
                   seeds the request-local cache
  5. TTL LAYER     a second request is served from the shared cache with no
                   query; invalidate() removes the entry; invalidate_all()
                   turns the whole cache over; a dirty key is never
                   re-published within the request (rollback safety);
                   entries are plain dicts, and a malformed one is a miss
  6. FRESH         fresh=True bypasses both layers for the read
  7. COMMIT ORDER  a reader in another request that republishes the old row
                   between a write and its commit cannot pin it past the
                   commit: the invalidation is replayed after the OUTER
                   commit -- a savepoint release does not consume it -- and
                   dropped on rollback; a same-request rollback drops the
                   request-local copy (a savepoint rollback drops the rows
                   read inside it and keeps the pending replay); a request
                   that memoised an older generation still invalidates the
                   current one; a replay reads the generation once and a
                   bulk one bumps it instead of deleting key by key;
                   guard.install() attaches the listeners that do all this
  8. GENERATION    the generation is time-based and stored with a finite
                   timeout, so a backend that evicts the key cannot revive
                   invalidated entries
  9. BOUNDARY      object ids are normalised to int; the TTL parser used by
                   the config tolerates a bad value; the bypass hook falls
                   through for an object with no id
 10. APP CONTEXT   flask.g and a real Flask-Caching SimpleCache are used;
                   the shared layer is refused, with one warning, on a
                   per-process backend under several worker processes --
                   and under gunicorn, uwsgi or a Celery worker when the
                   count is unknown (it is never read as one); a handle
                   whose backend cannot be inspected is a miss, not an
                   error
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session
from superset_ownership import migrate, service
from superset_ownership.db import ownership_object, ownership_share
from superset_ownership.service import OwnershipRow

UUID_A = "326fc7e5-b7f1-448e-8a6f-80d0e7ce0b64"
UUID_B = "4d1c2a7e-9f30-4b8a-a1c2-0e5f6a7b8c9d"


# --------------------------------------------------------------------------- fixtures


class CountingDB:
    """`service._db()` stand-in: one long-lived session over a SQLite file,
    with a counter of SELECTs against ownership_object."""

    def __init__(self, tmp_path):
        uri = f"sqlite:///{tmp_path / 'cache.db'}"
        migrate.upgrade("head", uri)
        self.engine = sa.create_engine(uri)
        self.session = Session(self.engine)
        self.row_selects = 0

        @event.listens_for(self.engine, "before_cursor_execute")
        def _count(conn, cursor, statement, parameters, context, executemany):
            head = statement.lstrip().upper()
            if head.startswith("SELECT") and "FROM OWNERSHIP_OBJECT" in statement.upper():
                self.row_selects += 1


class FakeCache:
    """The tiny interface the shared layer needs: get / set / delete (and
    the optional get_many / set_many). Records what was stored so a test can
    look at the serialised shape."""

    def __init__(self):
        self.store: dict[str, Any] = {}
        self.timeouts: dict[str, Any] = {}
        self.gets = 0
        self.deletes = 0

    def get(self, key):
        self.gets += 1
        return self.store.get(key)

    def set(self, key, value, timeout=None):
        self.store[key] = value
        self.timeouts[key] = timeout

    def delete(self, key):
        self.deletes += 1
        self.store.pop(key, None)
        self.timeouts.pop(key, None)

    def entries(self, asset_type, object_id):
        return {k: v for k, v in self.store.items() if k.endswith(f":{asset_type}:{object_id}")}


class FakeCacheWithMany(FakeCache):
    def __init__(self):
        super().__init__()
        self.delete_many_calls = 0

    def get_many(self, *keys):
        self.gets += 1
        return [self.store.get(k) for k in keys]

    def set_many(self, mapping, timeout=None):
        self.store.update(mapping)

    def delete_many(self, *keys):
        self.delete_many_calls += 1
        self.delete_many_keys = list(keys)
        for k in keys:
            self.store.pop(k, None)
            self.timeouts.pop(k, None)


@pytest.fixture
def db(tmp_path, monkeypatch):
    cdb = CountingDB(tmp_path)
    monkeypatch.setattr(service, "_db", lambda: cdb)
    # No request store and no shared cache unless a test installs one.
    monkeypatch.setattr(service, "_request_cache_override", None)
    monkeypatch.setattr(service, "_shared_cache_override", None)
    monkeypatch.setattr(service, "_ttl_override", 0)
    monkeypatch.setattr(service, "_shared_layer_verdict", None)
    # Share writes touch the outbox; keep that inline and store-free.
    monkeypatch.setenv("OWNERSHIP_OUTBOX_ENABLED", "false")
    yield cdb
    cdb.session.close()


@pytest.fixture
def request_scope(monkeypatch):
    """Installs a fresh request-local store and returns a function that
    starts the next 'request' (a new store)."""

    def begin(rc=None):
        rc = rc or service._RequestCache()
        monkeypatch.setattr(service, "_request_cache_override", rc)
        return rc

    begin()
    return begin


@pytest.fixture
def shared(monkeypatch):
    cache = FakeCache()
    monkeypatch.setattr(service, "_shared_cache_override", cache)
    monkeypatch.setattr(service, "_ttl_override", 30)
    return cache


def _insert(db, object_id, uuid=UUID_A, owner=1, visibility="private", asset_type="chart"):
    db.session.execute(
        sa.insert(ownership_object).values(
            asset_type=asset_type, object_id=object_id, object_uuid=uuid,
            owner_user_id=owner, visibility=visibility,
        )
    )
    db.session.commit()


# --------------------------------------------------------------------------- 1. one hit per request


def test_n_lookups_of_one_object_in_a_request_hit_the_database_once(db, request_scope):
    _insert(db, 56)
    rows = [service.lookup("chart", 56) for _ in range(5)]
    assert db.row_selects == 1
    assert all(r == rows[0] for r in rows)
    assert rows[0].owner_user_id == 1 and rows[0].visibility == "private"


def test_a_negative_result_is_cached_too(db, request_scope):
    assert service.lookup("chart", 999) is None
    assert service.lookup("chart", 999) is None
    assert service.lookup("chart", 999) is None
    assert db.row_selects == 1, "'no row' is asked as often as any row and is cached"


def test_the_key_is_the_object_not_the_user(db, request_scope):
    """Same object id under two asset types are two keys; the row carries
    nothing user- or tenant-specific, so one entry serves every caller."""
    _insert(db, 7, uuid=UUID_A, asset_type="chart")
    _insert(db, 7, uuid=UUID_B, asset_type="dashboard")
    assert service.lookup("chart", 7).object_uuid == UUID_A
    assert service.lookup("dashboard", 7).object_uuid == UUID_B
    assert service.lookup("chart", 7).object_uuid == UUID_A
    assert db.row_selects == 2


def test_a_new_request_starts_empty(db, request_scope):
    _insert(db, 56)
    service.lookup("chart", 56)
    request_scope()  # next request
    service.lookup("chart", 56)
    assert db.row_selects == 2


# --------------------------------------------------------------------------- 2. no context


def test_without_a_request_store_every_call_queries(db):
    """CLI bootstrap and pure tests have no `flask.g`: behaviour is the
    original one, a query per call, and nothing raises."""
    _insert(db, 56)
    assert service._request_cache() is None
    assert service._shared_cache() is None
    for _ in range(3):
        assert service.lookup("chart", 56).object_id == 56
    assert db.row_selects == 3
    service.invalidate("chart", 56)  # a no-op that must not raise
    service.invalidate_all()


def test_ttl_zero_disables_the_shared_layer(db, request_scope, monkeypatch):
    monkeypatch.setattr(service, "_shared_cache_override", None)
    monkeypatch.setattr(service, "_ttl_override", 0)
    assert service._shared_cache() is None
    _insert(db, 56)
    service.lookup("chart", 56)
    service.lookup("chart", 56)
    assert db.row_selects == 1, "the request-local layer is unaffected"


def test_ttl_is_read_from_the_environment_when_there_is_no_app(monkeypatch):
    monkeypatch.setattr(service, "_ttl_override", None)
    monkeypatch.setenv("OWNERSHIP_LOOKUP_CACHE_TTL", "12")
    assert service.lookup_cache_ttl() == 12
    # A literal 0 is still the explicit, documented way to turn the shared
    # layer off.
    monkeypatch.setenv("OWNERSHIP_LOOKUP_CACHE_TTL", "0")
    assert service.lookup_cache_ttl() == 0
    # A blank environment value now means "not set here" like every other
    # OWNERSHIP_* key read through settings.get (section 3 of the plug-in
    # architecture spec: one precedence, no per-key exception for blank) --
    # it falls through to the default rather than being read as an explicit
    # 0. parse_lookup_cache_ttl's own blank-is-zero branch still exists and
    # is exercised directly by the parser tests below; only settings.get's
    # unified precedence can no longer deliver a blank value to it.
    monkeypatch.setenv("OWNERSHIP_LOOKUP_CACHE_TTL", "")
    assert service.lookup_cache_ttl() == service.LOOKUP_CACHE_TTL_DEFAULT
    monkeypatch.setenv("OWNERSHIP_LOOKUP_CACHE_TTL", "nonsense")
    assert service.lookup_cache_ttl() == service.LOOKUP_CACHE_TTL_DEFAULT, "typo -> default"
    monkeypatch.delenv("OWNERSHIP_LOOKUP_CACHE_TTL")
    assert service.lookup_cache_ttl() == service.LOOKUP_CACHE_TTL_DEFAULT


# --------------------------------------------------------------------------- 3. writes invalidate


def test_set_visibility_invalidates_and_the_next_lookup_re_reads(db, request_scope, shared):
    _insert(db, 56, visibility="private")
    assert service.lookup("chart", 56).visibility == "private"
    assert db.row_selects == 1

    service.set_visibility("chart", 56, "shared")
    db.session.commit()
    row = service.lookup("chart", 56)
    assert row.visibility == "shared", "never the stale row"
    assert db.row_selects == 2


def test_upsert_invalidates_and_reads_fresh_to_decide_insert_vs_update(db, request_scope, shared):
    # Negative cached in both layers first.
    assert service.lookup("chart", 56) is None
    assert service.lookup("chart", 56) is None
    assert db.row_selects == 1

    service.upsert_ownership("chart", 56, UUID_A, 1, "private")
    db.session.commit()
    assert service.lookup("chart", 56).visibility == "private"

    # A second upsert on a cached row must UPDATE, not INSERT (unique key).
    service.upsert_ownership("chart", 56, UUID_A, 2, "shared")
    db.session.commit()
    row = service.lookup("chart", 56)
    assert (row.owner_user_id, row.visibility) == (2, "shared")
    n = db.session.execute(
        sa.select(sa.func.count()).select_from(ownership_object)
    ).scalar_one()
    assert n == 1


def test_share_writes_invalidate_the_object(db, request_scope, shared):
    """A share does not change the row, but it changes what the editors
    resolver derives from it; every share writer goes through invalidate."""
    _insert(db, 56)
    service.lookup("chart", 56)
    rc = service._request_cache()
    assert ("chart", 56) in rc.rows

    service.add_share_row("chart", 56, "user:ben", "editor")
    assert ("chart", 56) not in rc.rows and ("chart", 56) in rc.dirty

    service.lookup("chart", 56)
    service.remove_share_row("chart", 56, "user:ben")
    assert ("chart", 56) not in rc.rows


def test_after_asset_delete_invalidates(db, request_scope, shared):
    from superset_ownership import hooks

    _insert(db, 56)
    assert service.lookup("chart", 56) is not None
    hooks.after_asset_delete("chart", 56, None)
    db.session.commit()
    assert service.lookup("chart", 56) is None, "a cached copy must not outlive the row"
    assert db.session.execute(
        sa.select(sa.func.count()).select_from(ownership_share)
    ).scalar_one() == 0


# --------------------------------------------------------------------------- 4. batched lookup


def test_lookup_many_is_one_query_and_seeds_the_request_cache(db, request_scope):
    for i in range(1, 11):
        _insert(db, i, uuid=f"{UUID_A[:-2]}{i:02d}")
    rows = service.lookup_many("chart", list(range(1, 11)) + [99])
    assert db.row_selects == 1
    assert sorted(rows) == list(range(1, 11))
    assert 99 not in rows

    # Every id the batch resolved -- present or absent -- costs nothing after.
    for i in range(1, 11):
        assert service.lookup("chart", i) is rows[i]
    assert service.lookup("chart", 99) is None
    assert db.row_selects == 1


def test_lookup_many_only_queries_the_ids_it_does_not_know(db, request_scope):
    _insert(db, 1); _insert(db, 2, uuid=UUID_B)
    service.lookup("chart", 1)
    assert db.row_selects == 1
    rows = service.lookup_many("chart", [1, 2, 2, 1])
    assert sorted(rows) == [1, 2]
    assert db.row_selects == 2
    assert service.lookup_many("chart", [1, 2]) == rows
    assert db.row_selects == 2, "nothing left to ask"
    assert service.lookup_many("chart", []) == {}


# --------------------------------------------------------------------------- 5. the TTL layer


def test_a_second_request_is_served_from_the_shared_cache(db, request_scope, shared):
    _insert(db, 56)
    assert service.lookup("chart", 56).object_id == 56
    assert db.row_selects == 1

    request_scope()  # a new request, empty request-local cache
    row = service.lookup("chart", 56)
    assert row == OwnershipRow(id=row.id, asset_type="chart", object_id=56,
                               object_uuid=UUID_A, owner_user_id=1, visibility="private")
    assert db.row_selects == 1, "served from the shared layer"

    request_scope()
    assert service.lookup("chart", 999) is None
    request_scope()
    assert service.lookup("chart", 999) is None
    assert db.row_selects == 2, "negative results are shared too"


def test_invalidate_removes_the_shared_entry(db, request_scope, shared):
    _insert(db, 56)
    service.lookup("chart", 56)
    assert len([k for k in shared.store if ":chart:56" in k]) == 1
    service.invalidate("chart", 56)
    assert not [k for k in shared.store if ":chart:56" in k]
    request_scope()
    service.lookup("chart", 56)
    assert db.row_selects == 2


def test_invalidate_all_turns_the_cache_over(db, request_scope, shared):
    _insert(db, 56); _insert(db, 57, uuid=UUID_B)
    service.lookup("chart", 56); service.lookup("chart", 57)
    assert db.row_selects == 2
    service.invalidate_all()
    request_scope()
    service.lookup("chart", 56); service.lookup("chart", 57)
    assert db.row_selects == 4, "the old generation is unreachable"
    request_scope()
    service.lookup("chart", 56)
    assert db.row_selects == 4, "the new generation is populated"


def test_invalidate_all_also_turns_over_the_list_objects_cache(
    db, request_scope, shared, monkeypatch
):
    """Issue #82: `invalidate_all()` (bulk row writes; the test suite's own
    `harness` fixture, resetting a long-lived session between tests) turns
    the list_objects cache over too. Since M-2 the two caches carry
    SEPARATE generations (a group write no longer has to bump the row
    cache's just to turn its own over) -- but `invalidate_all()` itself
    still bumps both, one call each, so this still holds in one call to
    it, nothing enumerated."""
    class FakeListAuthorizer:
        def __init__(self, tuples):
            self.tuples = list(tuples)
            self.calls = 0

        def list_objects(self, subject, relation, asset_type, *, strict=False):
            self.calls += 1
            return list(self.tuples)

        def reachable(self):
            return True

    fake = FakeListAuthorizer(["chart:" + UUID_A])
    monkeypatch.setattr(service, "get_authorizer", lambda: fake)

    assert service.cached_list_objects("chart", "user:7") == ["chart:" + UUID_A]
    assert fake.calls == 1

    service.invalidate_all()
    request_scope()
    assert service.cached_list_objects("chart", "user:7") == ["chart:" + UUID_A]
    assert fake.calls == 2, "the old generation's entry is unreachable"


def test_a_dirty_key_is_not_republished_within_the_request(db, request_scope, shared):
    """Rollback safety: after a write in this request, reads of that object
    go to the database and stay request-local. If the transaction then rolls
    back, the shared layer never saw the uncommitted value."""
    _insert(db, 56, visibility="private")
    service.set_visibility("chart", 56, "shared")  # not committed
    assert service.lookup("chart", 56).visibility == "shared"  # this session sees it
    assert not [k for k in shared.store if ":chart:56" in k], "not published"
    db.session.rollback()

    request_scope()
    assert service.lookup("chart", 56).visibility == "private", "the committed row"


def test_entries_are_plain_dicts_and_a_malformed_entry_is_a_miss(db, request_scope, shared):
    _insert(db, 56)
    row = service.lookup("chart", 56)
    (key,) = [k for k in shared.store if ":chart:56" in k]
    assert shared.store[key] == asdict(row), "no pickled class instance"
    assert isinstance(shared.store[key], dict)

    # An entry from another shape of the class: miss, re-read, no exception.
    shared.store[key] = {"unexpected": 1}
    request_scope()
    assert service.lookup("chart", 56) == row
    assert db.row_selects == 2


def test_a_broken_backend_degrades_to_the_database(db, request_scope, monkeypatch):
    class Broken:
        def get(self, key): raise RuntimeError("redis down")
        def set(self, key, value, timeout=None): raise RuntimeError("redis down")
        def delete(self, key): raise RuntimeError("redis down")

    monkeypatch.setattr(service, "_shared_cache_override", Broken())
    monkeypatch.setattr(service, "_ttl_override", 30)
    _insert(db, 56)
    assert service.lookup("chart", 56).object_id == 56
    service.invalidate("chart", 56)
    service.invalidate_all()
    assert service.lookup_many("chart", [56]) [56].object_id == 56


def test_lookup_many_uses_get_many_and_set_many_when_offered(db, request_scope, monkeypatch):
    cache = FakeCacheWithMany()
    monkeypatch.setattr(service, "_shared_cache_override", cache)
    monkeypatch.setattr(service, "_ttl_override", 30)
    _insert(db, 1); _insert(db, 2, uuid=UUID_B)
    service.lookup_many("chart", [1, 2, 3])
    assert db.row_selects == 1
    assert len([k for k in cache.store if ":chart:" in k]) == 3  # 3 is a negative
    request_scope()
    rows = service.lookup_many("chart", [1, 2, 3])
    assert sorted(rows) == [1, 2]
    assert db.row_selects == 1, "all three answered by one get_many"


# --------------------------------------------------------------------------- 6. fresh


def test_fresh_bypasses_both_layers_for_the_read(db, request_scope, shared):
    """A fresh read never writes to the shared layer -- not even the row it
    just read. It may be seeing the request's own uncommitted state, and a
    write that is then rolled back must leave no trace there; the entry a
    fresh read leaves untouched below is stale, and that is the intended
    trade (a writer invalidates it; a mere fresh read does not)."""
    _insert(db, 56)
    service.lookup("chart", 56)
    service.lookup("chart", 56)
    assert db.row_selects == 1
    service.lookup("chart", 56, fresh=True)
    assert db.row_selects == 2
    service.lookup_many("chart", [56], fresh=True)
    assert db.row_selects == 3
    # A fresh read updates the request-local copy but never the shared one.
    db.session.execute(
        sa.update(ownership_object).where(ownership_object.c.object_id == 56).values(visibility="shared")
    )
    db.session.commit()
    assert service.lookup("chart", 56).visibility == "private", "still cached (no writer ran)"
    assert service.lookup("chart", 56, fresh=True).visibility == "shared"
    assert service.lookup("chart", 56).visibility == "shared", "request-local refreshed"
    (key,) = [k for k in shared.store if ":chart:56" in k]
    assert shared.store[key]["visibility"] == "private", "shared entry untouched by a fresh read"


# --------------------------------------------------------------------------- 7. commit order


class OtherWorker:
    """A second session on the same database: another request, or another
    worker, reading while the first request's transaction is still open."""

    def __init__(self, db):
        self.session = Session(db.engine)


@pytest.fixture
def listeners(db):
    """The guard's commit/rollback listeners, on the test session, exactly
    as guard.install() attaches them to Superset's."""
    from superset_ownership import guard

    event.listen(db.session, "after_commit", guard._after_commit)
    event.listen(db.session, "after_soft_rollback", guard._after_rollback)
    yield
    event.remove(db.session, "after_commit", guard._after_commit)
    event.remove(db.session, "after_soft_rollback", guard._after_rollback)


def test_a_concurrent_reader_cannot_pin_the_old_row_past_the_commit(
    db, request_scope, shared, listeners, monkeypatch
):
    """The race: writer A updates and invalidates but has not committed;
    reader B (another request, another connection) misses the shared cache,
    reads the still-committed OLD row and publishes it; A commits. Without
    the post-commit replay B's entry answers `private` for a full TTL after
    the object became public."""
    _insert(db, 56, visibility="private")
    writer = request_scope()

    # A: write + invalidate, no commit.
    service.set_visibility("chart", 56, "public")
    assert not shared.entries("chart", 56), "invalidated at the time of the write"
    assert ("chart", 56) in writer.pending

    # B: concurrent request on its own connection. READ COMMITTED: the old row.
    other = OtherWorker(db)
    monkeypatch.setattr(service, "_db", lambda: other)
    request_scope()
    assert service.lookup("chart", 56).visibility == "private"
    (entry,) = shared.entries("chart", 56).values()
    assert entry["visibility"] == "private", "B republished the old row"
    other.session.close()

    # A: commit -> guard._after_commit -> service.replay_invalidations.
    monkeypatch.setattr(service, "_db", lambda: db)
    request_scope(writer)
    db.session.commit()
    assert not shared.entries("chart", 56), "the replay removed B's entry"
    assert not writer.pending and ("chart", 56) in writer.dirty, "consumed; still dirty"

    # C: the next request sees the committed row.
    request_scope()
    assert service.lookup("chart", 56).visibility == "public"
    assert db.row_selects == 2, "B's read and C's read; set_visibility reads nothing"

    # A, after its own commit, still never republishes (dirty for the request).
    request_scope(writer)
    service.lookup("chart", 56)
    (entry,) = shared.entries("chart", 56).values()
    assert entry["visibility"] == "public", "C's entry, untouched by A"


def test_the_race_without_the_replay_is_the_stale_read(db, request_scope, shared, monkeypatch):
    """The same sequence with no listener attached: this is the failure the
    replay exists for, kept as the negative control."""
    _insert(db, 56, visibility="private")
    writer = request_scope()
    service.set_visibility("chart", 56, "public")
    other = OtherWorker(db)
    monkeypatch.setattr(service, "_db", lambda: other)
    request_scope()
    service.lookup("chart", 56)
    other.session.close()
    monkeypatch.setattr(service, "_db", lambda: db)
    request_scope(writer)
    db.session.commit()
    request_scope()
    assert service.lookup("chart", 56).visibility == "private", "stale without the replay"
    service.replay_invalidations()  # nothing pending in THIS request: a no-op
    request_scope(writer)
    service.replay_invalidations()  # the writer's pending set, replayed late
    request_scope()
    assert service.lookup("chart", 56).visibility == "public"


def test_a_rollback_drops_the_pending_replay_without_publishing(
    db, request_scope, shared, listeners, monkeypatch
):
    """Nothing was committed, so the entry a concurrent reader published is
    right and must survive; the writer's keys stay dirty."""
    _insert(db, 56, visibility="private")
    writer = request_scope()
    service.set_visibility("chart", 56, "public")

    other = OtherWorker(db)
    monkeypatch.setattr(service, "_db", lambda: other)
    request_scope()
    assert service.lookup("chart", 56).visibility == "private"
    other.session.close()

    monkeypatch.setattr(service, "_db", lambda: db)
    request_scope(writer)
    db.session.rollback()
    assert not writer.pending and ("chart", 56) in writer.dirty
    (entry,) = shared.entries("chart", 56).values()
    assert entry["visibility"] == "private", "the committed row; left in place"


def test_a_rollback_in_the_same_request_drops_the_request_local_copy(
    db, request_scope, shared, listeners
):
    """After a write, the request-local copy holds the uncommitted value; a
    full rollback must forget it, or every later read in the same request
    (the API's 502 paths roll back and answer in the same request) serves
    the rolled-back row."""
    _insert(db, 56, visibility="private")
    rc = request_scope()
    service.set_visibility("chart", 56, "shared")
    assert service.lookup("chart", 56).visibility == "shared"
    assert ("chart", 56) in rc.rows

    db.session.rollback()
    assert ("chart", 56) not in rc.rows
    assert service.lookup("chart", 56).visibility == "private", "re-read after the rollback"
    assert not shared.entries("chart", 56), "still dirty: not published"


def test_a_savepoint_rollback_keeps_the_pending_replay(db, request_scope, shared, listeners):
    """The outer transaction is still live after a savepoint rollback, so
    the write made before the savepoint still commits and still needs its
    replay. The request-local rows are dropped (see the next test) and cost
    one re-read."""
    _insert(db, 56, visibility="private")
    rc = request_scope()
    service.set_visibility("chart", 56, "shared")
    service.lookup("chart", 56)
    savepoint = db.session.begin_nested()
    savepoint.rollback()
    assert ("chart", 56) in rc.pending, "outer transaction still live"
    assert ("chart", 56) not in rc.rows
    assert service.lookup("chart", 56).visibility == "shared", "the outer write, re-read"
    db.session.commit()
    assert not rc.pending
    request_scope()
    assert service.lookup("chart", 56).visibility == "shared"


def test_a_savepoint_rollback_drops_a_row_read_inside_it(db, request_scope, shared, listeners):
    """A write inside a savepoint, read back inside it, then rolled back:
    the request-local copy held the discarded value and must go, or the rest
    of the request is served a row the database never had."""
    _insert(db, 56, visibility="private")
    rc = request_scope()
    assert service.lookup("chart", 56).visibility == "private"
    savepoint = db.session.begin_nested()
    service.set_visibility("chart", 56, "shared")
    assert service.lookup("chart", 56).visibility == "shared"
    savepoint.rollback()
    assert ("chart", 56) not in rc.rows
    assert ("chart", 56) in rc.pending, "kept: one redundant delete at the outer commit"
    assert service.lookup("chart", 56).visibility == "private", "re-read after the savepoint rollback"
    assert not shared.entries("chart", 56), "still dirty: not published"
    db.session.commit()
    assert not rc.pending


def test_a_savepoint_released_after_the_write_replays_at_the_outer_commit_only(
    db, request_scope, shared, listeners, monkeypatch
):
    """SQLAlchemy fires after_commit for a SAVEPOINT release too. A release
    makes nothing visible to other connections, so replaying there consumed
    the pending set while the transaction was still open, the outer commit
    had nothing left to replay, and a reader that republished the old row
    in between kept it for a full TTL."""
    _insert(db, 56, visibility="public")
    writer = request_scope()
    service.set_visibility("chart", 56, "private")
    with db.session.begin_nested():
        pass
    assert ("chart", 56) in writer.pending, "consumed by the savepoint release, not the commit"

    # A concurrent reader republishes the still-committed old row.
    other = OtherWorker(db)
    monkeypatch.setattr(service, "_db", lambda: other)
    request_scope()
    assert service.lookup("chart", 56).visibility == "public"
    other.session.close()

    monkeypatch.setattr(service, "_db", lambda: db)
    request_scope(writer)
    db.session.commit()
    assert not writer.pending and not shared.entries("chart", 56), "replayed at the outer commit"
    request_scope()
    assert service.lookup("chart", 56).visibility == "private"


def test_after_commit_tells_a_savepoint_release_from_the_outer_commit(db):
    """Pins the API the guard relies on: inside after_commit,
    `in_nested_transaction()` is True for a release and False for the outer
    commit (SQLAlchemy 2.x)."""
    seen = []

    def record(session):
        seen.append(session.in_nested_transaction())

    event.listen(db.session, "after_commit", record)
    try:
        db.session.execute(sa.select(1))
        with db.session.begin_nested():
            pass
        db.session.commit()
    finally:
        event.remove(db.session, "after_commit", record)
    assert seen == [True, False]


def test_invalidate_all_is_replayed_after_the_commit(db, request_scope, shared, listeners, monkeypatch):
    _insert(db, 56, visibility="private")
    request_scope()
    service.lookup("chart", 56)
    gen_before = shared.store[service._GEN_KEY]

    writer = request_scope()
    db.session.execute(
        sa.update(ownership_object).where(ownership_object.c.object_id == 56).values(visibility="public")
    )
    service.invalidate_all()
    gen_written = shared.store[service._GEN_KEY]
    assert gen_written > gen_before and writer.pending_all

    # A concurrent reader publishes the old row under the NEW generation.
    other = OtherWorker(db)
    monkeypatch.setattr(service, "_db", lambda: other)
    request_scope()
    assert service.lookup("chart", 56).visibility == "private"
    other.session.close()

    monkeypatch.setattr(service, "_db", lambda: db)
    request_scope(writer)
    db.session.commit()
    assert shared.store[service._GEN_KEY] > gen_written, "bumped again after the commit"
    assert not writer.pending_all
    request_scope()
    assert service.lookup("chart", 56).visibility == "public"


def test_a_request_that_memoised_an_older_generation_invalidates_the_current_one(
    db, request_scope, shared, listeners, monkeypatch
):
    """A reads (memoises generation G0); another worker bumps to G1; C
    publishes the row under G1; A writes and commits. A's invalidation must
    delete the G1 entry, not only the G0 one it remembers."""
    monkeypatch.setattr(service.time, "time", lambda: 1_700_000_000.0)
    _insert(db, 56, visibility="private")
    a = request_scope()
    service.lookup("chart", 56)
    g0 = a.generation

    request_scope()  # another worker
    service.invalidate_all()
    g1 = shared.store[service._GEN_KEY]
    assert g1 == g0 + 1

    request_scope()  # C
    service.lookup("chart", 56)
    assert service._shared_key(g1, "chart", 56) in shared.store

    request_scope(a)
    assert a.generation == g0
    service.set_visibility("chart", 56, "shared")
    assert not shared.entries("chart", 56), "deleted under the backend's generation"
    db.session.commit()

    request_scope()
    assert service.lookup("chart", 56).visibility == "shared"


def test_a_replay_reads_the_generation_once_and_deletes_each_key_once(
    db, request_scope, shared, monkeypatch
):
    """A replay of N keys is one generation read and N deletes -- not a
    generation read per key. With an older generation memoised each key is
    deleted under both; after invalidate_all, or above the bulk threshold,
    the bump replaces the deletes."""
    monkeypatch.setattr(service.time, "time", lambda: 1_700_000_000.0)
    for i in range(1, 11):
        _insert(db, i, uuid=f"{UUID_B[:-2]}{i:02d}")
    rc = request_scope()
    for i in range(1, 11):
        service.set_visibility("chart", i, "public")
    assert len(rc.pending) == 10
    shared.gets = shared.deletes = 0
    service.replay_invalidations()
    assert shared.gets == 1, "the generation, once per replay"
    assert shared.deletes == 10
    assert not rc.pending
    gen = shared.store[service._GEN_KEY]
    assert rc.generation == gen

    # An older generation memoised (another worker bumped in between): both.
    rc = request_scope()
    for i in range(1, 11):
        service.invalidate("chart", i)
    request_scope()
    service.invalidate_all()
    assert shared.store[service._GEN_KEY] == gen + 1
    request_scope(rc)
    shared.gets = shared.deletes = 0
    service.replay_invalidations()
    assert shared.gets == 1 and shared.deletes == 20
    assert rc.generation == gen + 1

    # invalidate_all in the request: the bump covers every key; no deletes.
    rc = request_scope()
    service.invalidate("chart", 1)
    service.invalidate_all()  # bumps at the time of the write: gen + 2
    service.invalidate("chart", 2)
    assert rc.pending_all and rc.pending == {("chart", 2)}
    shared.gets = shared.deletes = 0
    service.replay_invalidations()
    # M-2: `invalidate_all()` also set `list_objects_pending_all` (the
    # list_objects cache's own bulk-turnover flag), even though nothing in
    # this test ever touched that cache -- so the replay bumps BOTH
    # generations, one `_read_generation` each, not the single shared read
    # the two used to share before M-2 gave list_objects its own counter.
    assert shared.deletes == 0 and shared.gets == 2
    assert shared.store[service._GEN_KEY] == gen + 3, "bumped again by the replay"
    assert not rc.pending and not rc.pending_all

    # More keys pending than the threshold: one bump instead of N deletes.
    rc = request_scope()
    rc.pending = {("chart", i) for i in range(service.REPLAY_BUMP_THRESHOLD + 1)}
    shared.gets = shared.deletes = 0
    service.replay_invalidations()
    assert shared.deletes == 0 and shared.store[service._GEN_KEY] == gen + 4
    assert not rc.pending


def test_a_replay_uses_delete_many_when_the_backend_offers_it(db, request_scope, monkeypatch):
    """Below the bump threshold a replay on a backend with `delete_many`
    (Flask-Caching, Redis) is one round trip, not one delete per key -- and
    still under both generations when this request memoised an older one."""
    cache = FakeCacheWithMany()
    monkeypatch.setattr(service, "_shared_cache_override", cache)
    monkeypatch.setattr(service, "_ttl_override", 30)
    for i in range(1, 6):
        _insert(db, i, uuid=f"{UUID_B[:-2]}{i:02d}")
    request_scope()
    service.lookup_many("chart", [1, 2, 3, 4, 5])
    assert len([k for k in cache.store if ":chart:" in k]) == 5
    rc = request_scope()
    for i in range(1, 6):
        service.invalidate("chart", i)
    gen = cache.store[service._GEN_KEY]
    rc.generation = gen - 1  # memoised an older generation
    cache.deletes = cache.delete_many_calls = 0
    service.replay_invalidations()
    assert cache.delete_many_calls == 1 and cache.deletes == 0
    assert len(cache.delete_many_keys) == 10, "five keys under each generation, one round trip"
    assert not rc.pending and rc.generation == gen
    assert not [k for k in cache.store if ":chart:" in k]


def test_install_attaches_the_listeners_to_supersets_session(db, request_scope, shared, monkeypatch):
    """guard.install() is the production wiring: the four listeners on
    Superset's scoped session, once. Installed here against a stub `superset`
    module whose `db.session` is a throwaway scoped_session on the test
    database, then driven by a real commit."""
    import sys
    import types

    from sqlalchemy.orm import scoped_session, sessionmaker
    from superset_ownership import guard

    factory = scoped_session(sessionmaker(bind=db.engine))
    stand_in = types.SimpleNamespace(session=factory, engine=db.engine)
    pkg = types.ModuleType("superset")
    pkg.db = stand_in
    monkeypatch.setitem(sys.modules, "superset", pkg)
    monkeypatch.setattr(guard, "_INSTALLED", False)
    wired = (
        ("before_flush", guard._before_flush),
        ("before_commit", guard._before_commit),
        ("after_commit", guard._after_commit),
        ("after_soft_rollback", guard._after_rollback),
    )
    try:
        guard.install()
        assert guard._INSTALLED
        for name, fn in wired:
            assert event.contains(factory, name, fn), name
        guard.install()  # idempotent

        _insert(db, 56, visibility="private")
        monkeypatch.setattr(service, "_db", lambda: stand_in)
        rc = request_scope()
        service.set_visibility("chart", 56, "public")
        assert ("chart", 56) in rc.pending and ("chart", 56) in rc.dirty
        factory.commit()
        assert not rc.pending, "replayed by the installed after_commit listener"
        assert ("chart", 56) in rc.dirty

        service.set_visibility("chart", 56, "shared")
        service.lookup("chart", 56)
        assert ("chart", 56) in rc.rows and ("chart", 56) in rc.pending
        factory.rollback()
        assert not rc.pending and ("chart", 56) not in rc.rows, "discarded by the installed rollback listener"
        assert service.lookup("chart", 56).visibility == "public"
    finally:
        factory.remove()
        for name, fn in wired:
            if event.contains(factory, name, fn):
                event.remove(factory, name, fn)


# --------------------------------------------------------------------------- 8. generation


def test_the_generation_is_seeded_from_the_clock_with_a_finite_timeout(db, request_scope, shared, monkeypatch):
    monkeypatch.setattr(service.time, "time", lambda: 1_700_000_000.0)
    assert service._GEN_KEY not in shared.store
    _insert(db, 56)
    service.lookup("chart", 56)
    assert shared.store[service._GEN_KEY] == 1_700_000_000, "cold miss: the clock"
    assert service._shared_key(1_700_000_000, "chart", 56) in shared.store
    timeout = shared.timeouts[service._GEN_KEY]
    assert timeout and timeout > service.lookup_cache_ttl(), "finite, and outlives the rows"

    service.invalidate_all()
    assert shared.store[service._GEN_KEY] == 1_700_000_001, "max(stored + 1, clock)"
    monkeypatch.setattr(service.time, "time", lambda: 1_700_000_100.0)
    service.invalidate_all()
    assert shared.store[service._GEN_KEY] == 1_700_000_100, "the clock, once it is ahead"


def test_a_backend_that_evicts_the_generation_key_cannot_revive_old_entries(
    db, request_scope, shared, monkeypatch
):
    """SimpleCache prunes by nearest expiry. A counter-based generation that
    read as 0 when its key was evicted would put every reader back in a key
    space whose entries were still inside their TTL. Time-based, the
    re-seeded generation is at or past every one handed out, so the old
    entries stay unreachable."""
    clock = [1_700_000_000.0]
    monkeypatch.setattr(service.time, "time", lambda: clock[0])
    _insert(db, 56, visibility="private")
    service.lookup("chart", 56)
    old_key = service._shared_key(1_700_000_000, "chart", 56)
    assert old_key in shared.store

    db.session.execute(
        sa.update(ownership_object).where(ownership_object.c.object_id == 56).values(visibility="public")
    )
    db.session.commit()
    clock[0] += 5
    request_scope()
    service.invalidate_all()  # generation 1_700_000_005; the old entry is still in the store
    assert old_key in shared.store

    shared.store.pop(service._GEN_KEY)  # the backend evicts the key
    request_scope()
    assert service.lookup("chart", 56).visibility == "public", "not the revived old entry"
    assert shared.store[service._GEN_KEY] >= 1_700_000_005
    assert db.row_selects == 2


def test_a_generation_key_holding_garbage_is_reseeded(db, request_scope, shared, monkeypatch):
    monkeypatch.setattr(service.time, "time", lambda: 1_700_000_000.0)
    shared.store[service._GEN_KEY] = "garbage"
    _insert(db, 56)
    service.lookup("chart", 56)
    assert shared.store[service._GEN_KEY] == 1_700_000_000


# --------------------------------------------------------------------------- 9. boundaries


def test_a_str_object_id_is_the_same_key_as_the_int(db, request_scope, shared):
    """`"56"` and `56` share one shared key; a str must return the row and
    must not publish a negative under the int caller's key."""
    _insert(db, 56)
    row = service.lookup("chart", "56")
    assert row is not None and row.object_id == 56
    assert ("chart", 56) in service._request_cache().rows
    assert list(shared.entries("chart", 56).values()) == [asdict(row)]

    request_scope()
    assert service.lookup("chart", 56) == row
    assert db.row_selects == 1, "served from the entry the str call published"
    assert service.lookup_many("chart", ["56", 56, " 56 "]) == {56: row}

    service.invalidate("chart", "56")
    assert not shared.entries("chart", 56)

    with pytest.raises(TypeError):
        service.lookup("chart", "fifty-six")
    with pytest.raises(TypeError):
        service.lookup("chart", None)
    with pytest.raises(TypeError):
        service.lookup("chart", True)
    with pytest.raises(TypeError):
        service.lookup_many("chart", [1, 2.5])


def test_the_ttl_parser_tolerates_a_bad_value():
    """The config module and lookup_cache_ttl() share this parser: a typo in
    the environment degrades to the default, never to a failed import."""
    parse = service.parse_lookup_cache_ttl
    default = service.LOOKUP_CACHE_TTL_DEFAULT
    assert parse(None) == default
    assert parse("") == 0 and parse("   ") == 0 and parse("0") == 0
    assert parse("12") == 12 and parse(" 12 ") == 12 and parse(12) == 12
    assert parse("-3") == 0
    assert parse("nonsense") == default
    assert parse("30s") == default
    assert parse(True) == default


def test_purge_object_invalidates(db, request_scope, shared):
    from superset_ownership.authz import LocalAuthorizer

    _insert(db, 56)
    service.add_share_row("chart", 56, "user:ben", "editor")
    db.session.commit()
    service.lookup("chart", 56)
    rc = service._request_cache()
    rc.dirty.clear(); rc.pending.clear()
    assert ("chart", 56) in rc.rows

    assert LocalAuthorizer().purge_object(f"chart:{UUID_A}") is True
    assert ("chart", 56) not in rc.rows and ("chart", 56) in rc.dirty
    assert db.session.execute(
        sa.select(sa.func.count()).select_from(ownership_share)
    ).scalar_one() == 0


def test_the_bypass_hook_falls_through_for_an_object_with_no_id(db, request_scope, shared):
    """An object that is not persisted has no row to govern it by; the hook
    must return False (Superset's own checks decide), never raise from the
    access-decision path."""
    from types import SimpleNamespace

    from superset_ownership import hooks

    assert hooks.raise_for_access_bypass(user_id=1, chart=SimpleNamespace(id=None)) is False
    assert hooks.raise_for_access_bypass(user_id=1, dashboard=SimpleNamespace()) is False
    assert db.row_selects == 0 and not shared.store
    _insert(db, 56, visibility="public")
    assert hooks.raise_for_access_bypass(user_id=1, chart=SimpleNamespace(id=56)) is False
    assert db.row_selects == 1, "a persisted object is looked up as before"


def test_the_list_filter_reads_the_rows_once_and_publishes_nothing(db, request_scope, shared, monkeypatch):
    """owned_or_shared_rows is one scan; it seeds the request-local cache
    with the candidates so lookup_many / lookup in the same request cost no
    further query, and the shared layer sees none of it.

    User 7 owns both candidates and the store lists nothing for them, so
    `shared_uuids` is empty. PR #94 review, M-1: `pending_revocations_for`
    can only ever remove a SHARED candidate, so `owned_or_shared_rows` skips
    calling it whenever there is nothing it could remove -- an owner-only
    request pays zero revocation checks, not one per call. `CountingDB`
    counts any SELECT that mentions `ownership_object`, so the only cost
    left is the row scan itself: two calls (`owned_or_shared_rows`, then
    `owned_or_shared_object_ids`, which calls it again) -> 2, not 4. See
    `test_the_list_filter_subtracts_a_pending_revocation_with_two_bounded_queries`
    below for the shape when there IS a shared, revoked candidate to check.
    """
    _insert(db, 1, owner=7)
    _insert(db, 2, owner=7, uuid=UUID_B)
    _insert(db, 3, owner=8, uuid=f"{UUID_B[:-1]}e", visibility="public")

    class NoShares:
        def list_objects(self, subject, relation, asset_type, *, strict=False): return []
        def reachable(self): return True

    monkeypatch.setattr(service, "get_authorizer", lambda: NoShares())
    monkeypatch.setattr("superset_ownership.identity.member_ref", lambda user_id: f"user:{user_id}")

    rows = service.owned_or_shared_rows("chart", 7)
    assert sorted(rows) == [1, 2]
    assert service.owned_or_shared_object_ids("chart", 7) == {1, 2}
    assert db.row_selects == 2, (
        "M-1: no shared candidates -- pending_revocations_for is never called"
    )
    assert service.lookup_many("chart", [1, 2]) == rows
    assert service.lookup("chart", 1) == rows[1]
    assert db.row_selects == 2, "answered request-locally"
    # "superset_ownership:row:v1:" for an actual cached ROW entry, never
    # the bare "superset_ownership:row:gen" generation counter -- reading
    # that counter is a side effect of this cache's OWN generation-keyed
    # reads (`cached_list_objects` -> `_generation` -> `_read_generation`),
    # not a row published anywhere.
    row_keys = [k for k in shared.store if k.startswith("superset_ownership:row:v1:")]
    assert not row_keys, (
        "nothing published from a scan -- the OwnershipRow cache stays request-local"
    )
    # The list_objects cache (issue #82) is a SEPARATE layer, and it DOES
    # publish: `owned_or_shared_rows` calls `owned_or_shared_object_ids`,
    # which calls it again, but the second call is a request-local hit (the
    # key is already in `rc.list_objects`), so `NoShares.list_objects`
    # itself is asked exactly once, and this is that one answer, published
    # for the next REQUEST to reuse.
    lo_key = _lo_key(shared, "chart", "user:7")
    # "superset_ownership:list_objects:v1:" for an actual cached entry,
    # never the bare "superset_ownership:list_objects:gen" generation
    # counter (M-2) -- same distinction the row-cache prefix above draws.
    prefix = "superset_ownership:list_objects:v1:"
    lo_keys = [k for k in shared.store if k.startswith(prefix)]
    assert lo_keys == [lo_key]
    assert shared.store[lo_key] == []


def test_the_list_filter_subtracts_a_pending_revocation_with_two_bounded_queries(
    db, request_scope, shared, monkeypatch
):
    """PR #94 review, H-1: with a shared candidate, `owned_or_shared_rows`
    makes exactly one further call -- `outbox.pending_revocations_for` --
    whose first (and, absent any queued `delete`, only) query reads
    `ownership_outbox`, never `ownership_object`. Queuing a `delete` adds a
    second query, against `ownership_share` -- still never `ownership_object`
    -- so `CountingDB` (which keys on the substring `FROM OWNERSHIP_OBJECT`)
    does not count either of them: the row scan is the only thing it sees,
    whether or not a revocation is actually found.
    """
    from superset_ownership import outbox

    _insert(db, 2, owner=9, uuid=UUID_B, visibility="shared")  # user 7 does not own it

    class OneShare:
        def list_objects(self, subject, relation, asset_type, *, strict=False):
            return [f"{asset_type}:{UUID_B}"]
        def reachable(self):
            return True

    monkeypatch.setattr(service, "get_authorizer", lambda: OneShare())
    monkeypatch.setattr(
        "superset_ownership.identity.member_ref", lambda user_id: f"user:{user_id}"
    )

    # No outbox row yet: the share is reachable, and pending_revocations_for's
    # first query (against ownership_outbox) is the only extra statement --
    # not counted, since it never mentions ownership_object.
    rows = service.owned_or_shared_rows("chart", 7)
    assert sorted(rows) == [2]
    assert db.row_selects == 1, "the row scan only"

    # Queue a delete for the share: revoked, and the carve-out's second
    # query -- against ownership_share, not ownership_object -- still does
    # not move the counter.
    outbox.enqueue(
        outbox.OP_DELETE, f"chart:{UUID_B}", "user:7", "viewer", session=db.session
    )
    db.session.commit()

    rows = service.owned_or_shared_rows("chart", 7)
    assert rows == {}, "the pending delete revokes the only share"
    assert db.row_selects == 2, (
        "one more row scan; pending_revocations_for's two queries never "
        "select FROM ownership_object"
    )


# --------------------------------------------------------------------------- 10. a real Flask app context


def test_under_a_real_app_context_g_and_flask_caching_are_used(db, monkeypatch):
    """The production branches: `flask.g` holds the request cache, Superset's
    cache_manager.cache (a real Flask-Caching SimpleCache here) is the shared
    layer, the editors resolver's entry is dropped by invalidate(), and the
    generation key survives SimpleCache's pruning where a timeout=0 key
    would have been the first to go."""
    flask = pytest.importorskip("flask")
    app, cache, _stub = _flask_app_with_simple_cache(monkeypatch)
    app.config["OWNERSHIP_LOOKUP_CACHE_TTL"] = 30

    _insert(db, 56)
    with app.app_context():
        assert service.lookup_cache_ttl() == 30
        assert service._shared_cache() is cache
        flask.g._ownership_editor_cache = {"chart:56": [1]}
        for _ in range(4):
            assert service.lookup("chart", 56).object_id == 56
        assert db.row_selects == 1
        rc = flask.g._ownership_row_cache
        assert isinstance(rc, service._RequestCache) and ("chart", 56) in rc.rows
        service.invalidate("chart", 56)
        assert flask.g._ownership_editor_cache == {}, "derived entry dropped"
        service.lookup("chart", 56)
        assert db.row_selects == 2

    with app.app_context():
        service.lookup("chart", 56)
        assert db.row_selects == 3, "the dirty request never republished; re-read"
    with app.app_context():
        service.lookup("chart", 56)
        assert db.row_selects == 3, "served by Flask-Caching"
        service.invalidate_all()
        gen = cache.get(service._GEN_KEY)
        assert gen and gen >= int(time.time()) - 5
        for i in range(40):
            cache.set(f"filler:{i}", i, timeout=300)
        assert cache.get(service._GEN_KEY) == gen, "outlives the pruning of nearer-expiry entries"


def _flask_app_with_simple_cache(monkeypatch, threshold=5):
    """A real Flask app with a real Flask-Caching SimpleCache installed as
    Superset's cache_manager.cache, and the process-shape environment the
    multi-worker guard reads pinned to "one process, no server": a runner
    that exports a worker count, is itself served by gunicorn, or was
    started by a Celery worker must not change what these tests see."""
    flask = pytest.importorskip("flask")
    flask_caching = pytest.importorskip("flask_caching")
    import sys
    import types

    for name in (
        "SERVER_WORKER_AMOUNT", "WEB_CONCURRENCY", "SERVER_SOFTWARE",
        "CELERY_LOG_LEVEL", "CELERY_LOG_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    app = flask.Flask(__name__)
    cache = flask_caching.Cache(app, config={"CACHE_TYPE": "SimpleCache", "CACHE_THRESHOLD": threshold})
    stub = types.ModuleType("superset.extensions")
    stub.cache_manager = types.SimpleNamespace(cache=cache)
    pkg = types.ModuleType("superset")
    pkg.extensions = stub
    monkeypatch.setitem(sys.modules, "superset", pkg)
    monkeypatch.setitem(sys.modules, "superset.extensions", stub)
    monkeypatch.setattr(service, "_request_cache_override", None)
    monkeypatch.setattr(service, "_shared_cache_override", None)
    monkeypatch.setattr(service, "_ttl_override", None)
    monkeypatch.setattr(service, "_shared_layer_verdict", None)
    return app, cache, stub


def test_the_shared_layer_is_refused_on_a_per_process_backend_under_several_workers(
    db, monkeypatch, caplog
):
    """SimpleCache is process memory: with N gunicorn workers an invalidation
    reaches one of them and the other N-1 serve the old row for a full TTL
    after every write. The module turns the shared layer off for that
    deployment, once per process, with one warning; a single worker, or a
    shared backend under several workers, keeps it."""
    app, cache, stub = _flask_app_with_simple_cache(monkeypatch)
    app.config["OWNERSHIP_LOOKUP_CACHE_TTL"] = 10
    monkeypatch.setenv("SERVER_WORKER_AMOUNT", "4")
    # The `db` fixture ran the migration chain, whose alembic env applies
    # `fileConfig(...)`; it must leave the module's logger (created before
    # it) enabled, or this warning and every other never reach the log.
    assert not service.logger.disabled
    _insert(db, 56)

    with app.app_context(), caplog.at_level(logging.WARNING, logger="superset_ownership.service"):
        assert service._shared_cache() is None
        assert service.lookup_cache_ttl() == 0, "reports the layer as off"
        service._shared_cache()
        service.lookup("chart", 56)
    with app.app_context():
        service.lookup("chart", 56)
    assert db.row_selects == 2, "no shared layer: each request selects"
    assert cache.get(service._GEN_KEY) is None, "nothing written to the backend"
    warnings = [r for r in caplog.records if "shared ownership-row cache is OFF" in r.getMessage()]
    assert len(warnings) == 1, "decided and reported once per process"
    assert "SimpleCache" in warnings[0].getMessage() and "4 worker" in warnings[0].getMessage()

    # One worker process: the layer is on.
    monkeypatch.setenv("SERVER_WORKER_AMOUNT", "1")
    monkeypatch.setattr(service, "_shared_layer_verdict", None)
    with app.app_context():
        assert service._shared_cache() is cache
        assert service.lookup_cache_ttl() == 10

    # gunicorn's own variable counts too.
    monkeypatch.delenv("SERVER_WORKER_AMOUNT")
    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    monkeypatch.setattr(service, "_shared_layer_verdict", None)
    with app.app_context():
        assert service._shared_cache() is None

    # Several workers on a backend that is not per-process: the layer is on.
    import types

    shared_backend = types.SimpleNamespace(cache=FakeCache())
    stub.cache_manager = types.SimpleNamespace(cache=shared_backend)
    monkeypatch.setattr(service, "_shared_layer_verdict", None)
    with app.app_context():
        assert service._shared_cache() is shared_backend

    # Unset or unparsable worker counts are UNKNOWN, not one.
    monkeypatch.delenv("WEB_CONCURRENCY")
    assert service._worker_count() is None
    monkeypatch.setenv("SERVER_WORKER_AMOUNT", "many")
    assert service._worker_count() is None
    monkeypatch.setenv("SERVER_WORKER_AMOUNT", "0")
    assert service._worker_count() is None
    monkeypatch.setenv("SERVER_WORKER_AMOUNT", "-1")
    assert service._worker_count() is None

    # ... and an unknown count with nothing marking the process as forked
    # (`flask run`, the CLI) is one process: the layer stays on.
    monkeypatch.delenv("SERVER_WORKER_AMOUNT")
    stub.cache_manager = types.SimpleNamespace(cache=cache)
    monkeypatch.setattr(service, "_shared_layer_verdict", None)
    with app.app_context():
        assert service._forking_server() is None
        assert service._shared_cache() is cache
        assert service.lookup_cache_ttl() == 10


def test_under_gunicorn_with_no_worker_count_the_per_process_layer_is_refused(
    db, monkeypatch, caplog
):
    """gunicorn exports SERVER_SOFTWARE to its workers and never the count:
    `gunicorn -w 8` and a `gunicorn.conf.py` leave SERVER_WORKER_AMOUNT and
    WEB_CONCURRENCY unset in every worker. An unknown count under a forking
    server FAILS CLOSED -- the layer is refused, once, with a warning that
    names the variable to set -- and a count of 1 turns it back on."""
    app, cache, stub = _flask_app_with_simple_cache(monkeypatch)
    app.config["OWNERSHIP_LOOKUP_CACHE_TTL"] = 10
    monkeypatch.setenv("SERVER_SOFTWARE", "gunicorn/26.0.0")
    _insert(db, 56)

    assert service._forking_server() == "gunicorn"
    with app.app_context(), caplog.at_level(logging.WARNING, logger="superset_ownership.service"):
        assert service._shared_cache() is None
        assert service.lookup_cache_ttl() == 0
        service.lookup("chart", 56)
    with app.app_context():
        service.lookup("chart", 56)
    assert db.row_selects == 2, "no shared layer: each request selects"
    assert cache.get(service._GEN_KEY) is None
    warnings = [r for r in caplog.records if "shared ownership-row cache is OFF" in r.getMessage()]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "gunicorn" in message and "unknown" in message
    assert "SERVER_WORKER_AMOUNT" in message, "the warning names the variable to set"

    # The count made known (run-server.sh exports it): one worker keeps it.
    monkeypatch.setenv("SERVER_WORKER_AMOUNT", "1")
    monkeypatch.setattr(service, "_shared_layer_verdict", None)
    with app.app_context():
        assert service._shared_cache() is cache
        assert service.lookup_cache_ttl() == 10

    # gunicorn's own variable serves as well; several workers refuse.
    monkeypatch.delenv("SERVER_WORKER_AMOUNT")
    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    monkeypatch.setattr(service, "_shared_layer_verdict", None)
    with app.app_context():
        assert service._shared_cache() is None

    # A shared backend under gunicorn with the count unknown: the layer is on.
    import types

    monkeypatch.delenv("WEB_CONCURRENCY")
    shared_backend = types.SimpleNamespace(cache=FakeCache())
    stub.cache_manager = types.SimpleNamespace(cache=shared_backend)
    monkeypatch.setattr(service, "_shared_layer_verdict", None)
    with app.app_context():
        assert service._shared_cache() is shared_backend


def test_a_celery_worker_or_uwsgi_refuses_the_per_process_layer(db, monkeypatch, caplog):
    """A Celery worker exports CELERY_LOG_LEVEL / CELERY_LOG_FILE to its pool
    children (celery.app.log.Logging.setup), and its pool is not described
    by the web's worker count -- so even SERVER_WORKER_AMOUNT=1 does not
    turn the per-process layer on there. The `uwsgi` module exists only
    inside uwsgi; with it importable and the count unknown, refused too."""
    import sys
    import types

    app, cache, stub = _flask_app_with_simple_cache(monkeypatch)
    app.config["OWNERSHIP_LOOKUP_CACHE_TTL"] = 10

    monkeypatch.setenv("CELERY_LOG_LEVEL", "")  # present, as Celery leaves it
    monkeypatch.setenv("SERVER_WORKER_AMOUNT", "1")
    assert service._forking_server() == "celery"
    with app.app_context(), caplog.at_level(logging.WARNING, logger="superset_ownership.service"):
        assert service._shared_cache() is None
        assert service.lookup_cache_ttl() == 0
    warnings = [r for r in caplog.records if "shared ownership-row cache is OFF" in r.getMessage()]
    assert len(warnings) == 1 and "Celery" in warnings[0].getMessage()

    monkeypatch.delenv("CELERY_LOG_LEVEL")
    monkeypatch.delenv("SERVER_WORKER_AMOUNT")
    monkeypatch.setitem(sys.modules, "uwsgi", types.ModuleType("uwsgi"))
    monkeypatch.setattr(service, "_shared_layer_verdict", None)
    assert service._forking_server() == "uwsgi"
    with app.app_context():
        assert service._shared_cache() is None
    monkeypatch.setenv("SERVER_WORKER_AMOUNT", "1")
    monkeypatch.setattr(service, "_shared_layer_verdict", None)
    with app.app_context():
        assert service._shared_cache() is cache, "a known count of 1 under uwsgi keeps it"


def test_a_cache_handle_whose_backend_property_raises_degrades_to_the_database(db, monkeypatch):
    """Flask-Caching's `Cache.cache` is a property that raises KeyError for a
    handle not initialised on the current app. The guard's inspection sits
    inside the "a broken backend is a miss" boundary: lookup() answers from
    the database, nothing escapes, and the verdict is left undecided so an
    initialised handle is judged on the next call."""
    import types

    class Handle:
        @property
        def cache(self):
            raise KeyError(self)

        def get(self, key):
            return None

        def set(self, key, value, timeout=None):
            pass

        def delete(self, key):
            pass

    app, cache, stub = _flask_app_with_simple_cache(monkeypatch)
    app.config["OWNERSHIP_LOOKUP_CACHE_TTL"] = 10
    stub.cache_manager = types.SimpleNamespace(cache=Handle())
    _insert(db, 56)
    with app.app_context():
        assert service._shared_cache() is None
        assert service.lookup("chart", 56).object_id == 56
        assert service.lookup("chart", 56).object_id == 56
        assert service._shared_layer_verdict is None, "undecided: the handle could not be inspected"
    assert db.row_selects == 1, "answered request-locally after the one SELECT"

    # An initialised handle afterwards is judged, and used.
    stub.cache_manager = types.SimpleNamespace(cache=cache)
    with app.app_context():
        assert service._shared_cache() is cache
        assert service._shared_layer_verdict is True


# --------------------------------------------- 11. list_objects (reverse-index) cache
#
# Issue #82 / SOW section 6.2. A SEPARATE cache from the OwnershipRow one
# above -- same two layers, same TTL config, same `shared` fixture -- in
# front of `get_authorizer().list_objects(subject, "viewer", asset_type)`,
# the list base filter's own cost (never the access decision's; the owner
# fast path in `hooks.raise_for_access_bypass` never calls `list_objects` at
# all). See `service.cached_list_objects` / `service.invalidate_list_objects`
# for the implementation and the module comment above them for the full
# design note this section proves.


class FakeListAuthorizer:
    """`list_objects` + `reachable`, counting calls.

    `up=False` models the store being unreachable end to end (both
    `/healthz` and `/list-objects`): `strict=False` answers `[]` silently,
    same as the real non-strict backend; `strict=True` -- what
    `service.cached_list_objects` always passes (M-1) -- raises
    `fga.StoreUnavailable`, the additive contract `fga.list_objects` now
    carries.

    `list_objects_error`, set directly, models the narrower M-1 case a
    single `up` flag cannot: a store that answers `/healthz` (so
    `reachable()` would say "up") while `/list-objects` ITSELF fails --
    the split the old `reachable()`-classified design could not see,
    because it asked a different endpoint than the one that failed.
    """

    def __init__(self, tuples=None):
        self.tuples: list[str] = list(tuples or [])
        self.calls = 0
        self.up = True
        self.list_objects_error: Exception | None = None

    def list_objects(self, subject, relation, asset_type, *, strict=False):
        self.calls += 1
        exc = self.list_objects_error
        if exc is None and not self.up:
            from superset_ownership.fga import StoreUnavailable

            exc = StoreUnavailable("store unreachable")
        if exc is not None:
            if strict:
                raise exc
            return []
        return list(self.tuples)

    def reachable(self):
        return self.up

    def write_tuple(self, subject, relation, obj, *, strict=False):
        return True

    def delete_tuple(self, subject, relation, obj, *, strict=False):
        return True

    def revoke_subject(self, subject, obj):
        return True

    def purge_object(self, obj):
        return True


def test_n_calls_in_one_request_cost_one_list_objects_call(
    db, request_scope, monkeypatch
):
    authorizer = FakeListAuthorizer(["chart:" + UUID_A])
    monkeypatch.setattr(service, "get_authorizer", lambda: authorizer)
    for _ in range(5):
        assert service.cached_list_objects("chart", "user:7") == ["chart:" + UUID_A]
    assert authorizer.calls == 1


def test_without_a_request_store_every_call_asks_the_store(monkeypatch):
    monkeypatch.setattr(service, "_request_cache_override", None)
    monkeypatch.setattr(service, "_shared_cache_override", None)
    monkeypatch.setattr(service, "_ttl_override", 0)
    authorizer = FakeListAuthorizer([])
    monkeypatch.setattr(service, "get_authorizer", lambda: authorizer)
    for _ in range(3):
        service.cached_list_objects("chart", "user:7")
    assert authorizer.calls == 3


def test_a_list_objects_new_request_starts_empty_with_no_shared_layer(
    db, request_scope, monkeypatch
):
    authorizer = FakeListAuthorizer(["chart:" + UUID_A])
    monkeypatch.setattr(service, "get_authorizer", lambda: authorizer)
    service.cached_list_objects("chart", "user:7")
    request_scope()  # next request; no `shared` fixture, so no TTL layer either
    service.cached_list_objects("chart", "user:7")
    assert authorizer.calls == 2, "no shared layer: every request asks again"


def test_list_objects_second_request_is_served_from_the_shared_cache(
    db, request_scope, shared, monkeypatch
):
    """This is #82's whole point: the SECOND request costs nothing, not just
    the second call within the first."""
    authorizer = FakeListAuthorizer(["chart:" + UUID_A])
    monkeypatch.setattr(service, "get_authorizer", lambda: authorizer)
    assert service.cached_list_objects("chart", "user:7") == ["chart:" + UUID_A]
    assert authorizer.calls == 1
    request_scope()
    assert service.cached_list_objects("chart", "user:7") == ["chart:" + UUID_A]
    assert authorizer.calls == 1, "served from the TTL layer, no store call"


def _lo_key(shared, asset_type: str, subject: str) -> str:
    """The shared key `cached_list_objects` would use right now for
    (asset_type, subject) -- generation included. M-2: this cache carries
    its OWN generation (`service._LIST_OBJECTS_GEN_KEY`), separate from the
    row cache's (`service._GEN_KEY`) -- read the right one, or this helper
    would (usually harmlessly, since both seed from the same clock second
    when nothing has bumped either yet) compute the wrong key the moment a
    test bumps one generation but not the other."""
    gen = service._read_generation(shared, service._LIST_OBJECTS_GEN_KEY)
    return service._list_objects_key(gen, asset_type, subject)


def test_the_key_is_asset_type_and_subject(db, request_scope, shared, monkeypatch):
    authorizer = FakeListAuthorizer(["chart:" + UUID_A])
    monkeypatch.setattr(service, "get_authorizer", lambda: authorizer)
    service.cached_list_objects("chart", "user:7")
    service.cached_list_objects("dashboard", "user:7")
    service.cached_list_objects("chart", "user:8")
    assert authorizer.calls == 3
    prefix = "superset_ownership:list_objects:v1:"  # never the bare ...:gen key (M-2)
    lo_keys = [k for k in shared.store if k.startswith(prefix)]
    assert sorted(lo_keys) == sorted([
        _lo_key(shared, "chart", "user:7"),
        _lo_key(shared, "chart", "user:8"),
        _lo_key(shared, "dashboard", "user:7"),
    ])


def test_only_the_raw_shared_tuples_are_cached_a_plain_list(
    db, request_scope, shared, monkeypatch
):
    authorizer = FakeListAuthorizer(["chart:" + UUID_A])
    monkeypatch.setattr(service, "get_authorizer", lambda: authorizer)
    service.cached_list_objects("chart", "user:7")
    stored = shared.store[_lo_key(shared, "chart", "user:7")]
    assert stored == ["chart:" + UUID_A]
    assert type(stored) is list, "the raw answer only, never the derived reachable set"


def test_invalidate_removes_both_layers_and_the_next_call_asks_again(
    db, request_scope, shared, monkeypatch
):
    """Own write, no TTL wait: a share this request just made must be
    visible to a list this same request renders next."""
    authorizer = FakeListAuthorizer(["chart:" + UUID_A])
    monkeypatch.setattr(service, "get_authorizer", lambda: authorizer)
    service.cached_list_objects("chart", "user:7")
    assert authorizer.calls == 1
    rc = service._request_cache()
    assert ("chart", "user:7") in rc.list_objects
    key = _lo_key(shared, "chart", "user:7")

    authorizer.tuples.append("chart:" + UUID_B)  # the new share, per the store
    service.invalidate_list_objects("chart", "user:7")
    assert ("chart", "user:7") not in rc.list_objects
    assert key not in shared.store

    fresh = service.cached_list_objects("chart", "user:7")
    assert fresh == ["chart:" + UUID_A, "chart:" + UUID_B]
    assert authorizer.calls == 2, "the new share is visible without waiting out the TTL"


def test_invalidate_with_no_subject_is_a_no_op(db, request_scope, shared):
    service.invalidate_list_objects("chart", None)  # must not raise
    service.invalidate_list_objects("chart", "")


def test_a_group_subject_turns_the_whole_cache_over(
    db, request_scope, shared, monkeypatch
):
    """A group write/revoke cannot target the individual members it
    reaches (see the module comment) -- so an unrelated subject's own
    cached answer must not survive it either, or a member's stale entry
    from before the change would outlive it, exactly the post-delivery
    staleness window the enqueue+delivery invalidation elsewhere in this
    file closes for an individual subject."""
    authorizer = FakeListAuthorizer(["chart:" + UUID_A])
    monkeypatch.setattr(service, "get_authorizer", lambda: authorizer)

    # Dee's own entry, unrelated to the group write below, warmed first.
    service.cached_list_objects("chart", "user:dee")
    assert authorizer.calls == 1

    service.invalidate_list_objects("chart", "group:eng#member")

    rc = service._request_cache()
    assert ("chart", "user:dee") not in rc.list_objects, "the whole cache turned over"
    fresh = service.cached_list_objects("chart", "user:dee")
    assert fresh == ["chart:" + UUID_A]
    assert authorizer.calls == 2, "re-asked, not served from the pre-invalidation entry"


def test_a_dirty_list_objects_key_is_not_republished_within_the_request(
    db, request_scope, shared, monkeypatch
):
    authorizer = FakeListAuthorizer(["chart:" + UUID_A])
    monkeypatch.setattr(service, "get_authorizer", lambda: authorizer)
    service.cached_list_objects("chart", "user:7")
    key = _lo_key(shared, "chart", "user:7")
    service.invalidate_list_objects("chart", "user:7")
    service.cached_list_objects("chart", "user:7")  # re-read, same request: dirty
    assert authorizer.calls == 2, "a dirty key is never served from the shared layer"
    assert key not in shared.store, "never republished for the rest of this request"


def test_degraded_store_is_never_published_and_the_next_request_retries(
    db, request_scope, shared, monkeypatch
):
    """M-1: a store that could not really be read must never be published
    as "nothing shared" -- that would hide every one of a subject's real
    shares for a full TTL after the store recovers, which a fresh
    per-request failure does not do. `cached_list_objects` calls
    `list_objects(..., strict=True)`, which raises instead of answering
    `[]`; the exception is caught and answered empty for THIS call only,
    never published, so the next request (or the same one, past the TTL)
    retries rather than trusting a cached failure."""
    authorizer = FakeListAuthorizer([])
    authorizer.up = False
    monkeypatch.setattr(service, "get_authorizer", lambda: authorizer)
    key = _lo_key(shared, "chart", "user:7")
    assert service.cached_list_objects("chart", "user:7") == []
    assert authorizer.calls == 1
    assert key not in shared.store, (
        "an unreachable store's answer is never published to the shared layer"
    )

    request_scope()
    authorizer.up = True
    authorizer.tuples = ["chart:" + UUID_A]
    recovered = service.cached_list_objects("chart", "user:7")
    assert recovered == ["chart:" + UUID_A]
    assert authorizer.calls == 2, "not served from a cached []: the request retried"


def test_reachable_is_never_consulted_the_strict_read_classifies_itself(
    db, request_scope, shared, monkeypatch
):
    """M-1: `cached_list_objects` used to call `Authorizer.reachable()` on
    every miss to decide whether to publish -- an operator-facing probe
    against a DIFFERENT endpoint (`/healthz`) than the one whose answer is
    actually being cached (`/list-objects`), and a second HTTP round trip
    on every miss besides. `list_objects(strict=True)` now carries its own
    outcome (`fga.StoreUnavailable` / `StoreRejected` on failure), so
    `reachable()` is not consulted at all any more -- on a hit or a miss."""
    authorizer = FakeListAuthorizer(["chart:" + UUID_A])
    reachable_calls = []
    real_reachable = authorizer.reachable

    def spy():
        reachable_calls.append(1)
        return real_reachable()

    authorizer.reachable = spy
    monkeypatch.setattr(service, "get_authorizer", lambda: authorizer)

    service.cached_list_objects("chart", "user:7")  # miss: one store call
    assert authorizer.calls == 1
    assert reachable_calls == [], "M-1: no reachable() call on a miss"

    service.cached_list_objects("chart", "user:7")  # request-local hit
    assert authorizer.calls == 1, "a hit makes no store call"
    assert reachable_calls == [], "a hit asks nothing about reachability"

    request_scope()
    service.cached_list_objects("chart", "user:7")  # shared-layer hit
    assert authorizer.calls == 1, "still a cache hit, no store call"
    assert reachable_calls == [], "still a cache hit, no reachable() call"


def test_a_failed_read_is_never_published_even_though_healthz_would_say_up(
    db, request_scope, shared, monkeypatch
):
    """M-1's actual bug, reproduced: the store answers `/healthz` (so
    `reachable()` would have said "up") while `/list-objects` ITSELF times
    out or 5xxs. The old design classified a miss by `reachable()` alone
    and could not see this split -- the non-strict `fga.list_objects`
    swallowed the failure into `[]`, and that `[]` got published for a
    full TTL, hiding a subject's real shares. `list_objects(strict=True)`
    raises from the SAME call that actually failed, so it is never
    mistaken for a healthy empty answer, and the next read -- this request
    past the TTL, or the next one -- retries instead of trusting a cached
    failure."""
    from superset_ownership.fga import StoreUnavailable

    authorizer = FakeListAuthorizer([])
    authorizer.list_objects_error = StoreUnavailable("list-objects timed out")
    monkeypatch.setattr(service, "get_authorizer", lambda: authorizer)
    key = _lo_key(shared, "chart", "user:7")

    assert service.cached_list_objects("chart", "user:7") == []
    assert authorizer.calls == 1
    assert key not in shared.store, "a failed read is never published"

    request_scope()
    authorizer.list_objects_error = None
    authorizer.tuples = ["chart:" + UUID_A]
    recovered = service.cached_list_objects("chart", "user:7")
    assert recovered == ["chart:" + UUID_A]
    assert authorizer.calls == 2, "not served from a cached []: the request retried"


def test_a_group_write_turns_over_only_the_list_objects_generation(
    db, request_scope, shared, monkeypatch
):
    """M-2: the fallback `invalidate_list_objects` takes for a non-`user:`
    (group) subject used to reuse the ROW cache's single generation
    counter, so a group share or revoke cooled every cached ownership row
    in the deployment -- the #75 cache's whole point -- as a side effect,
    on every group write. The list_objects cache now carries its own
    generation key (M-2): a group write must bump only that one."""
    _insert(db, 56)
    service.lookup("chart", 56)
    assert db.row_selects == 1
    row_gen_before = service._read_generation(shared, service._GEN_KEY)

    authorizer = FakeListAuthorizer(["chart:" + UUID_A])
    monkeypatch.setattr(service, "get_authorizer", lambda: authorizer)
    service.cached_list_objects("chart", "user:dee")
    lo_gen_before = service._read_generation(shared, service._LIST_OBJECTS_GEN_KEY)

    service.invalidate_list_objects("chart", "group:eng#member")

    row_gen_after = service._read_generation(shared, service._GEN_KEY)
    lo_gen_after = service._read_generation(shared, service._LIST_OBJECTS_GEN_KEY)
    assert row_gen_after == row_gen_before, "the row cache's generation is untouched"
    assert lo_gen_after > lo_gen_before, "the list_objects cache's own generation bumped"

    request_scope()
    service.lookup("chart", 56)
    assert db.row_selects == 1, (
        "the row entry, cached under the old (unbumped) generation, is still reachable"
    )


# ---------------------------------------------------- 11b. write invalidation wiring


def test_outbox_write_tuple_invalidates_the_subjects_list_objects_entry(
    db, request_scope, shared, monkeypatch
):
    """`outbox.write_tuple` is the ONE call site every share, transfer,
    claim and initial-govern write goes through (service.py, hooks.py,
    api.py) -- proving it invalidates is proving all of them do."""
    from superset_ownership import outbox

    authorizer = FakeListAuthorizer(["chart:" + UUID_A])
    monkeypatch.setattr(service, "get_authorizer", lambda: authorizer)
    # The facade's inline branch, not the enqueue one -- no outbox DB row needed.
    monkeypatch.setenv("OWNERSHIP_OUTBOX_ENABLED", "false")
    monkeypatch.setattr(outbox, "get_authorizer", lambda: authorizer)

    service.cached_list_objects("chart", "user:7")
    assert authorizer.calls == 1

    outbox.write_tuple("user:7", "viewer", "chart:" + UUID_B)
    rc = service._request_cache()
    assert ("chart", "user:7") not in rc.list_objects, "invalidated before the write"

    authorizer.tuples.append("chart:" + UUID_B)
    fresh = service.cached_list_objects("chart", "user:7")
    assert fresh == ["chart:" + UUID_A, "chart:" + UUID_B]
    assert authorizer.calls == 2


def test_outbox_delete_tuple_invalidates_the_subjects_list_objects_entry(
    db, request_scope, shared, monkeypatch
):
    from superset_ownership import outbox

    authorizer = FakeListAuthorizer(["chart:" + UUID_A])
    monkeypatch.setattr(service, "get_authorizer", lambda: authorizer)
    monkeypatch.setenv("OWNERSHIP_OUTBOX_ENABLED", "false")
    monkeypatch.setattr(outbox, "get_authorizer", lambda: authorizer)

    service.cached_list_objects("chart", "user:7")
    outbox.delete_tuple("user:7", "viewer", "chart:" + UUID_A)
    rc = service._request_cache()
    assert ("chart", "user:7") not in rc.list_objects


def test_outbox_revoke_subject_invalidates_the_subjects_list_objects_entry(
    db, request_scope, shared, monkeypatch
):
    from superset_ownership import outbox

    authorizer = FakeListAuthorizer(["chart:" + UUID_A])
    monkeypatch.setattr(service, "get_authorizer", lambda: authorizer)
    monkeypatch.setenv("OWNERSHIP_OUTBOX_ENABLED", "false")
    monkeypatch.setattr(outbox, "get_authorizer", lambda: authorizer)

    service.cached_list_objects("chart", "user:7")
    outbox.revoke_subject("chart", UUID_A, "user:7")
    rc = service._request_cache()
    assert ("chart", "user:7") not in rc.list_objects


def test_outbox_purge_object_does_not_need_a_subject_to_invalidate(
    db, request_scope, shared, monkeypatch
):
    """purge_object removes every relationship on an object, not one
    subject's, so it has no single subject to target -- and does not need
    one: `owned_or_shared_rows`'s own row scan drops the object once its
    `ownership_object` row is gone, whatever a stale cached entry still
    names. This just proves the facade call does not raise for lack of one."""
    from superset_ownership import outbox

    authorizer = FakeListAuthorizer([])
    monkeypatch.setenv("OWNERSHIP_OUTBOX_ENABLED", "false")
    monkeypatch.setattr(outbox, "get_authorizer", lambda: authorizer)
    assert outbox.purge_object("chart", UUID_A) is True


def test_outbox_apply_invalidates_at_delivery_not_only_at_enqueue(
    db, request_scope, shared, monkeypatch
):
    """The bug this closes: a read BETWEEN the enqueue and the drain is not
    wrong to publish a fresh cache entry (the store genuinely still has the
    OLD answer at that moment) -- but that entry must not outlive the
    DELIVERY, or it answers stale for up to a full TTL past the point the
    store's answer actually changed. `outbox._apply` (what `drain()` calls
    to deliver one row) invalidates again once delivery succeeds, on top of
    the facade's own enqueue-time invalidation."""
    from superset_ownership import outbox

    authorizer = FakeListAuthorizer(["chart:" + UUID_A])
    monkeypatch.setattr(service, "get_authorizer", lambda: authorizer)

    # The enqueue already happened (irrelevant here -- this test drives
    # `_apply` directly, as `drain()` would once it has claimed the row).
    # A read in between: the store still has the OLD answer, so caching it
    # here is correct AT THE TIME.
    assert service.cached_list_objects("chart", "user:7") == ["chart:" + UUID_A]
    assert authorizer.calls == 1

    # Delivery: the store's answer changes (a share for Ben, say -- what
    # matters here is only that SOMETHING for this subject/asset_type was
    # written) and `_apply` must invalidate the entry the read above just
    # published, not leave it to answer stale for the rest of the TTL.
    authorizer.tuples.append("chart:" + UUID_B)
    row = {
        "op": outbox.OP_WRITE,
        "subject": "user:7",
        "relation": "viewer",
        "object": "chart:" + UUID_B,
    }
    outbox._apply(row, authorizer)
    rc = service._request_cache()
    assert ("chart", "user:7") not in rc.list_objects, "invalidated at delivery"

    fresh = service.cached_list_objects("chart", "user:7")
    assert fresh == ["chart:" + UUID_A, "chart:" + UUID_B]
    assert authorizer.calls == 2, "re-asked, not served from the pre-delivery entry"

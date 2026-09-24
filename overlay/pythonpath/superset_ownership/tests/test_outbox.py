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
"""Assurance tests for the transactional outbox.

Pure: a throwaway SQLite database, a fake authorization store, no Superset.
The fake store mirrors the real client's FAILURE CONTRACT exactly, because a
fake that fails the way the code wishes the store failed lets the code's own
bugs pass (that is how the first cut of the purge path shipped):

  up=False        the store cannot answer (connection refused, timeout, 5xx):
                  every strict call raises StoreUnavailable; `reachable()` is
                  False. Non-strict writes return False.
  read_rejected   the store answers but refuses the READ (a 400 from a stale
                  model pin, an HTML page from a proxy): strict reads raise
                  StoreRejected while `reachable()` stays True. This is the
                  state in which "could not read" was once mistaken for "nothing
                  there".
  reject={...}    the store answers and refuses these WRITES: StoreRejected.

Proven:
  1. ATOMICITY   the outbox row commits with the local change, and rolls back
                 with it
  2. DOWN        sharing succeeds while the store is unreachable; the row waits
                 `pending`, attempts and last_error recorded
  3. RECOVERY    one drain after the store returns delivers it
  4. PURGE/REVOKE a store that cannot be read, OR refuses the read, is a FAILURE
                 for a delete event, never an empty result -- and revoke_subject
                 never touches `owner`/`tenant`
  5. CLASSIFY    unavailable -> retry (and trips the breaker); rejected -> dead
                 at once (and does NOT trip the breaker); the split comes from
                 the exception type, not from a probe
  6. ORDERING    a stuck, claimed, or dead row blocks later rows for the SAME
                 object only; the LIMIT is not consumed by blocked rows
  7. CONCURRENCY the claim is ONE statement carrying both "nobody has this row"
                 and "nothing earlier for this object is undelivered"; a claim
                 stolen between the select and the claim is detected; stale
                 claims are reaped; a drain that outlives its lease cannot
                 overwrite the re-claimer's outcome
  8. REVOCATION  has_pending_revocation denies the right subject while a
                 delete / revoke_subject / purge is undelivered, dead included;
                 a revoked GROUP share denies every member; pending_revocations_for
                 (issue #84) answers the same rule for every object a subject
                 might be asked about in one query, which is what the list
                 filter and single-object GET subtract before returning
  9. INTERRUPT   a soft time limit hands the row back untouched, never as a
                 verdict
 10. DEAD/REPLAY parked after max attempts, never deleted, replayable
 11. IDEMPOTENT  re-delivery is a no-op
 12. OPERATOR    startup warns when nothing drains; status reports last delivery
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from superset_ownership import db as ownership_db, migrate, outbox
from superset_ownership.db import ownership_outbox
from superset_ownership.fga import StoreRejected, StoreUnavailable

OBJ = "chart:326fc7e5-b7f1-448e-8a6f-80d0e7ce0b64"
OTHER = "chart:other"
BEN = "user:6c2b48e9-5a71-4f92-8d03-2e9b7c1a4d53"
ADA = "user:3f0a91c7-2d84-4e63-9b15-7c4e8a2f6d31"


# --------------------------------------------------------------------------- fakes


class FakeStore:
    """Stand-in for the OpenFGA authorizer with the real client's failure
    contract (see the module docstring)."""

    def __init__(self, up: bool = True):
        self.up = up
        self.read_rejected = False
        self.reject: set[tuple] = set()
        self.tuples: set[tuple[str, str, str]] = set()
        self.calls: list[tuple] = []

    def reachable(self):
        return self.up

    def _write_failure(self, key, strict):
        if not self.up:
            if strict:
                raise StoreUnavailable("write: ConnectionError")
            return False
        if key in self.reject:
            if strict:
                raise StoreRejected("write: HTTP 400 validation_error")
            return False
        return None

    def write_tuple(self, subject, relation, obj, *, strict=False):
        self.calls.append(("write", subject, relation, obj))
        failed = self._write_failure((subject, relation, obj), strict)
        if failed is not None:
            return failed
        self.tuples.add((subject, relation, obj))
        return True

    def delete_tuple(self, subject, relation, obj, *, strict=False):
        self.calls.append(("delete", subject, relation, obj))
        failed = self._write_failure((subject, relation, obj), strict)
        if failed is not None:
            return failed
        self.tuples.discard((subject, relation, obj))
        return True

    def set_object_tenant(self, asset_type, object_uuid, tenant_guid, *, strict=False):
        self.calls.append(("set_tenant", asset_type, object_uuid, tenant_guid))
        key = (f"tenant:{tenant_guid}#member", "tenant", f"{asset_type}:{object_uuid}")
        failed = self._write_failure(key, strict)
        if failed is not None:
            return failed
        self.tuples.add(key)
        return True

    def _strict_read(self, obj):
        if not self.up:
            raise StoreUnavailable(f"read_all {obj}: ConnectionError")
        if self.read_rejected:
            raise StoreRejected(f"read_all {obj}: HTTP 400 validation_error")
        return {t for t in self.tuples if t[2] == obj}

    def _strict_delete(self, tuples):
        for t in tuples:
            self.delete_tuple(*t, strict=True)

    def purge_object(self, obj):
        self.calls.append(("purge", obj))
        self._strict_delete(self._strict_read(obj))
        return True

    def revoke_subject(self, subject, obj):
        # Same exclusion as the real backend: shares only, never ownership.
        self.calls.append(("revoke", subject, obj))
        self._strict_delete(
            {
                t
                for t in self._strict_read(obj)
                if t[0] == subject and t[1] not in ("owner", "tenant")
            }
        )
        return True


def _db(tmp_path):
    uri = f"sqlite:///{tmp_path / 'outbox.db'}"
    migrate.upgrade("head", uri)  # the real chain builds the real table
    return sa.create_engine(uri)


def _rows(session):
    return [
        dict(r)
        for r in session.execute(
            sa.select(ownership_outbox).order_by(ownership_outbox.c.id)
        ).mappings()
    ]


def _statuses(session):
    return {r["id"]: r["status"] for r in _rows(session)}


def _enqueue_share(session, obj=OBJ, subject=BEN):
    return outbox.enqueue(outbox.OP_WRITE, obj, subject, "viewer", session=session)


# --------------------------------------------------------------------------- 1. atomicity


def test_outbox_row_commits_with_the_local_change(tmp_path):
    engine = _db(tmp_path)
    with Session(engine) as s:
        s.execute(
            sa.insert(ownership_db.ownership_share).values(
                asset_type="chart", object_id=56, subject=BEN, role="viewer"
            )
        )
        _enqueue_share(s)
        s.commit()
    with Session(engine) as s:
        (row,) = _rows(s)
        assert row["status"] == "pending"
        assert row["created_on"] is not None, "created_on set client-side by enqueue"
        assert (
            s.execute(
                sa.select(sa.func.count()).select_from(ownership_db.ownership_share)
            ).scalar_one()
            == 1
        )


def test_outbox_row_rolls_back_with_the_local_change(tmp_path):
    engine = _db(tmp_path)
    with Session(engine) as s:
        s.execute(
            sa.insert(ownership_db.ownership_share).values(
                asset_type="chart", object_id=56, subject=BEN, role="viewer"
            )
        )
        _enqueue_share(s)
        s.rollback()
    with Session(engine) as s:
        assert _rows(s) == [], "no orphan instruction"
        assert (
            s.execute(
                sa.select(sa.func.count()).select_from(ownership_db.ownership_share)
            ).scalar_one()
            == 0
        ), "no orphan change"


# --------------------------------------------------------------------------- 2+3. down, recovery


def test_share_succeeds_while_store_is_down_and_waits_in_outbox(tmp_path):
    engine = _db(tmp_path)
    store = FakeStore(up=False)
    with Session(engine) as s:
        _enqueue_share(s)
        s.commit()
        stats = outbox.drain(session=s, authorizer=store)
        assert stats["failed"] == 1 and stats["delivered"] == 0
        (row,) = _rows(s)
        assert row["status"] == "pending" and row["attempts"] == 1
        assert row["claimed_at"] is None, "claim released on failure"
        assert "ConnectionError" in row["last_error"]
    assert store.tuples == set()


def test_pending_row_is_delivered_once_store_recovers(tmp_path):
    engine = _db(tmp_path)
    store = FakeStore(up=False)
    with Session(engine) as s:
        _enqueue_share(s)
        s.commit()
        outbox.drain(session=s, authorizer=store)
        store.up = True
        stats = outbox.drain(session=s, authorizer=store)
        assert stats["delivered"] == 1
        (row,) = _rows(s)
        assert (
            row["status"] == "done"
            and row["done_on"] is not None
            and row["last_error"] is None
        )
    assert (BEN, "viewer", OBJ) in store.tuples


def test_every_op_type_is_delivered(tmp_path):
    engine = _db(tmp_path)
    store = FakeStore()
    store.tuples |= {
        ("user:old", "owner", OBJ),
        (BEN, "viewer", OBJ),
        (BEN, "editor", OBJ),
    }
    with Session(engine) as s:
        outbox.enqueue(outbox.OP_DELETE, OBJ, "user:old", "owner", session=s)
        outbox.enqueue(outbox.OP_WRITE, OBJ, "user:new", "owner", session=s)
        outbox.enqueue(outbox.OP_SET_TENANT, OBJ, "tenant-guid", "tenant", session=s)
        outbox.enqueue(outbox.OP_REVOKE_SUBJECT, OBJ, BEN, session=s)
        s.commit()
        assert outbox.drain(session=s, authorizer=store)["delivered"] == 4
    assert ("user:old", "owner", OBJ) not in store.tuples
    assert ("user:new", "owner", OBJ) in store.tuples
    assert ("tenant:tenant-guid#member", "tenant", OBJ) in store.tuples
    assert not {t for t in store.tuples if t[0] == BEN}, (
        "revoke_subject removed BOTH of Ben's relations"
    )


# --------------------------------------------------------------------------- 4. purge / revoke vs a down store


def test_purge_is_not_recorded_done_while_store_is_unreachable(tmp_path):
    """The bug the first review caught: an unreadable store looked like an
    empty object and the delete event was marked done with tuples left behind."""
    engine = _db(tmp_path)
    store = FakeStore(up=False)
    store.tuples |= {("user:a", "owner", OBJ), (BEN, "viewer", OBJ)}
    with Session(engine) as s:
        outbox.enqueue(outbox.OP_PURGE_OBJECT, OBJ, session=s)
        s.commit()
        stats = outbox.drain(session=s, authorizer=store)
        assert stats["delivered"] == 0 and stats["failed"] == 1
        (row,) = _rows(s)
        assert row["status"] == "pending", (
            "an unreachable store is a failure, not an empty purge"
        )
        assert (
            "StoreUnavailable" in row["last_error"]
            or "ConnectionError" in row["last_error"]
        )
        assert len(store.tuples) == 2, "nothing was touched"

        store.up = True
        assert outbox.drain(session=s, authorizer=store)["delivered"] == 1
    assert not {t for t in store.tuples if t[2] == OBJ}, (
        "purged once the store was back"
    )


def test_revoke_subject_is_not_recorded_done_while_store_is_unreachable(tmp_path):
    engine = _db(tmp_path)
    store = FakeStore(up=False)
    store.tuples.add((BEN, "viewer", OBJ))
    with Session(engine) as s:
        outbox.enqueue(outbox.OP_REVOKE_SUBJECT, OBJ, BEN, session=s)
        s.commit()
        outbox.drain(session=s, authorizer=store)
        assert _statuses(s) == {1: "pending"}
        assert (BEN, "viewer", OBJ) in store.tuples


def test_purge_removes_only_that_objects_tuples(tmp_path):
    engine = _db(tmp_path)
    store = FakeStore()
    store.tuples |= {
        ("user:a", "owner", OBJ),
        (BEN, "viewer", OBJ),
        ("user:x", "viewer", OTHER),
    }
    with Session(engine) as s:
        outbox.enqueue(outbox.OP_PURGE_OBJECT, OBJ, session=s)
        s.commit()
        outbox.drain(session=s, authorizer=store)
    assert store.tuples == {("user:x", "viewer", OTHER)}


@pytest.mark.parametrize("op", [outbox.OP_PURGE_OBJECT, outbox.OP_REVOKE_SUBJECT])
def test_read_refused_by_a_reachable_store_is_dead_not_done(tmp_path, op):
    """The second-review hole: the store ANSWERS the read with an error (a
    400 from a stale model pin, an HTML page from a proxy). That is not an
    empty object. The row must not be `done` -- it is parked dead, the
    tuples are untouched, and the read gate keeps denying (dead counts)."""
    engine = _db(tmp_path)
    store = FakeStore()
    store.read_rejected = True
    store.tuples |= {("user:a", "owner", OBJ), (BEN, "viewer", OBJ)}
    with Session(engine) as s:
        outbox.enqueue(
            op, OBJ, BEN if op == outbox.OP_REVOKE_SUBJECT else None, session=s
        )
        s.commit()
        stats = outbox.drain(session=s, authorizer=store)
        assert stats["delivered"] == 0 and stats["dead"] == 1
        (row,) = _rows(s)
        assert row["status"] == "dead" and "HTTP 400" in row["last_error"]
        assert len(store.tuples) == 2, "nothing was touched"
        assert outbox.has_pending_revocation(OBJ, BEN, session=s) is True, (
            "still denied"
        )
        assert stats["circuit_open"] is False, "a rejection is not a transport failure"


def test_revoke_subject_never_removes_owner_or_tenant(tmp_path):
    """A stale share row on the current owner (a transfer leaves one) is
    revoked as a SHARE. Ownership is not a share and is never touched."""
    engine = _db(tmp_path)
    store = FakeStore()
    store.tuples |= {
        (BEN, "owner", OBJ),
        (BEN, "editor", OBJ),
        (BEN, "viewer", OBJ),
        ("tenant:t1#member", "tenant", OBJ),
    }
    with Session(engine) as s:
        outbox.enqueue(outbox.OP_REVOKE_SUBJECT, OBJ, BEN, session=s)
        s.commit()
        assert outbox.drain(session=s, authorizer=store)["delivered"] == 1
    assert store.tuples == {(BEN, "owner", OBJ), ("tenant:t1#member", "tenant", OBJ)}
    assert ("delete", BEN, "owner", OBJ) not in store.calls


def test_delete_refused_midway_through_a_purge_is_retried_not_done(tmp_path):
    """A purge issues one delete per tuple. If the store goes away halfway,
    the row stays pending and the next pass finishes the job."""
    engine = _db(tmp_path)

    class DiesAfterOne(FakeStore):
        def delete_tuple(self, subject, relation, obj, *, strict=False):
            if len([c for c in self.calls if c[0] == "delete"]) == 1:
                self.up = False
            return super().delete_tuple(subject, relation, obj, strict=strict)

    store = DiesAfterOne()
    store.tuples |= {
        ("user:a", "owner", OBJ),
        (BEN, "viewer", OBJ),
        (ADA, "viewer", OBJ),
    }
    with Session(engine) as s:
        outbox.enqueue(outbox.OP_PURGE_OBJECT, OBJ, session=s)
        s.commit()
        stats = outbox.drain(session=s, authorizer=store)
        assert stats["failed"] == 1 and _statuses(s) == {1: "pending"}
        assert len(store.tuples) == 2, "one delete landed before the store went away"
        store.up = True
        assert outbox.drain(session=s, authorizer=store)["delivered"] == 1
    assert not {t for t in store.tuples if t[2] == OBJ}


# --------------------------------------------------------------------------- 5. failure classification


def test_rejection_is_parked_dead_immediately_and_does_not_trip_breaker(tmp_path):
    """A store that answered and said no will say no again; retrying is
    pointless and must not starve delivery of everything else."""
    engine = _db(tmp_path)
    store = FakeStore()
    store.reject.add((BEN, "viewer", OBJ))
    with Session(engine) as s:
        _enqueue_share(s)  # 1: rejected
        _enqueue_share(s, obj=OTHER, subject=ADA)  # 2: fine
        _enqueue_share(s, obj="chart:third", subject=ADA)  # 3: fine
        s.commit()
        stats = outbox.drain(session=s, authorizer=store)
    assert stats["dead"] == 1 and stats["delivered"] == 2
    assert stats["circuit_open"] is False, "a rejection is not a transport failure"
    assert _statuses(s) == {1: "dead", 2: "done", 3: "done"}


def test_a_false_from_a_backend_that_does_not_raise_is_a_rejection(tmp_path):
    """A backend may answer False instead of raising (the local one does when
    an object no longer resolves). It answered; that is a rejection."""
    engine = _db(tmp_path)

    class Answers(FakeStore):
        def write_tuple(self, subject, relation, obj, *, strict=False):
            return False

    with Session(engine) as s:
        _enqueue_share(s)
        s.commit()
        stats = outbox.drain(session=s, authorizer=Answers())
    assert stats["dead"] == 1 and stats["circuit_open"] is False
    assert "did not accept" in _rows(s)[0]["last_error"]


def test_classification_does_not_consult_reachable(tmp_path):
    """The split is made from the failure itself. A probe that happens to
    answer must not turn a 5xx into a rejection, and a probe that fails must
    not turn a 4xx into a retry."""
    engine = _db(tmp_path)

    class Liar(FakeStore):
        def reachable(self):
            raise AssertionError("the drain must not probe")

    # 5xx / timeout -> StoreUnavailable -> stays pending, even though a probe would say "up"
    store = Liar()
    store.up = False
    with Session(engine) as s:
        _enqueue_share(s)
        s.commit()
        assert outbox.drain(session=s, authorizer=store)["failed"] == 1
        assert _statuses(s) == {1: "pending"}
        # 4xx -> StoreRejected -> dead, even though a probe would say "down"
        store.up = True
        store.reject.add((BEN, "viewer", OBJ))
        assert outbox.drain(session=s, authorizer=store)["dead"] == 1
        assert _statuses(s) == {1: "dead"}


def test_unreachable_trips_breaker_and_keeps_rows_pending(tmp_path):
    engine = _db(tmp_path)
    store = FakeStore(up=False)
    with Session(engine) as s:
        for i in range(20):
            _enqueue_share(s, obj=f"chart:{i}")
        s.commit()
        stats = outbox.drain(session=s, authorizer=store)
    assert stats["circuit_open"] is True
    assert stats["failed"] == outbox.CONSECUTIVE_FAILURE_LIMIT
    assert len(store.calls) == outbox.CONSECUTIVE_FAILURE_LIMIT, (
        "did not burn a timeout on all 20"
    )
    assert set(_statuses(s).values()) == {"pending"}, (
        "unreachable never parks a row dead early"
    )


# --------------------------------------------------------------------------- 6. ordering


def test_stuck_row_blocks_later_rows_for_the_same_object_only(tmp_path):
    engine = _db(tmp_path)

    class DeleteFails(FakeStore):
        def delete_tuple(self, subject, relation, obj, *, strict=False):
            self.calls.append(("delete", subject, relation, obj))
            raise StoreRejected("delete: HTTP 400")  # store says no -> dead at once

    store = DeleteFails()
    with Session(engine) as s:
        outbox.enqueue(outbox.OP_DELETE, OBJ, "user:old", "owner", session=s)  # 1
        outbox.enqueue(
            outbox.OP_WRITE, OBJ, "user:new", "owner", session=s
        )  # 2, must wait
        _enqueue_share(s, obj=OTHER)  # 3, unrelated
        s.commit()
        stats = outbox.drain(session=s, authorizer=store)
    assert _statuses(s) == {1: "dead", 2: "pending", 3: "done"}
    assert stats["skipped"] == 1
    assert ("user:new", "owner", OBJ) not in store.tuples, "never two owners"


def test_dead_object_does_not_consume_the_limit(tmp_path):
    """Head-of-line starvation: rows behind a dead object used to eat the LIMIT
    so nothing queued after them was ever drained."""
    engine = _db(tmp_path)
    store = FakeStore()
    with Session(engine) as s:
        # a dead row for OBJ, then 5 pending rows for OBJ, then one deliverable row
        outbox.enqueue(outbox.OP_DELETE, OBJ, "user:old", "owner", session=s)
        s.execute(sa.update(ownership_outbox).values(status="dead"))
        for _ in range(5):
            _enqueue_share(s)
        _enqueue_share(s, obj=OTHER, subject=ADA)
        s.commit()
        stats = outbox.drain(session=s, authorizer=store, limit=3)
    assert stats["delivered"] == 1, "the deliverable row was reached despite limit=3"
    assert (ADA, "viewer", OTHER) in store.tuples


# --------------------------------------------------------------------------- 7. concurrency


def test_two_drains_cannot_both_take_a_row(tmp_path):
    """Drain A holds a claim on row 1; drain B must neither deliver it nor
    run ahead of it on the same object, and must still deliver other objects."""
    engine = _db(tmp_path)
    store = FakeStore()
    with Session(engine) as a:
        outbox.enqueue(outbox.OP_DELETE, OBJ, "user:old", "owner", session=a)  # 1
        outbox.enqueue(outbox.OP_WRITE, OBJ, "user:new", "owner", session=a)  # 2
        _enqueue_share(a, obj=OTHER, subject=ADA)  # 3
        a.commit()
        assert outbox._claim(a, 1, outbox._utcnow()) == 1, (
            "A claims row 1 (as the real drain does)"
        )
    with Session(engine) as b:
        stats = outbox.drain(session=b, authorizer=store)
    assert stats["delivered"] == 1 and stats["skipped"] == 0
    assert _statuses(b) == {1: "claimed", 2: "pending", 3: "done"}, (
        "B skipped OBJ entirely (row 2 not even selected) while A holds row 1"
    )
    assert ("user:new", "owner", OBJ) not in store.tuples


def test_claim_refuses_a_row_while_an_earlier_row_for_its_object_is_undelivered(
    tmp_path,
):
    """The ordering guarantee lives IN the claim statement, so it holds even
    when a drain's view of the queue is stale (READ COMMITTED: another drain
    took the earlier row after this one's select)."""
    engine = _db(tmp_path)
    with Session(engine) as s:
        outbox.enqueue(outbox.OP_DELETE, OBJ, "user:old", "owner", session=s)  # 1
        outbox.enqueue(outbox.OP_WRITE, OBJ, "user:new", "owner", session=s)  # 2
        _enqueue_share(s, obj=OTHER)  # 3
        s.commit()
        ts = outbox._utcnow()
        assert outbox._claim(s, 2, ts) == 0, "row 1 is pending: row 2 is not claimable"
        assert outbox._claim(s, 3, ts) == 1, "another object is unaffected"
        assert outbox._claim(s, 1, ts) == 1
        assert outbox._claim(s, 2, ts) == 0, "row 1 is claimed: still not"
        s.execute(
            sa.update(ownership_outbox)
            .where(ownership_outbox.c.id == 1)
            .values(status="dead", claimed_at=None)
        )
        s.commit()
        assert outbox._claim(s, 2, ts) == 0, "row 1 is dead: still not"
        s.execute(
            sa.update(ownership_outbox)
            .where(ownership_outbox.c.id == 1)
            .values(status="done")
        )
        s.commit()
        assert outbox._claim(s, 2, ts) == 1, "row 1 delivered: now yes"


class _StealsAfterSelect(Session):
    """A session that lets a rival drain claim row 1 between this drain's
    pending-rows SELECT and its first claim UPDATE -- the gap the atomic
    claim exists for. (The theft happens just before the UPDATE rather than
    just after the SELECT so this drain's SQLite read transaction can be
    ended first; SQLite's single writer would otherwise block the rival.)"""

    def __init__(self, engine, rival_engine):
        super().__init__(engine)
        self._rival = rival_engine
        self._selected = False
        self._stolen = False

    def execute(self, stmt, *a, **kw):
        if self._selected and not self._stolen:
            self._stolen = True
            self.commit()
            with Session(self._rival) as rival:
                assert outbox._claim(rival, 1, outbox._utcnow()) == 1
        if isinstance(stmt, sa.sql.Select):
            self._selected = True
        return super().execute(stmt, *a, **kw)


def test_claim_lost_between_select_and_claim_is_detected(tmp_path):
    """Both rows for OBJ are in this drain's select. Then a rival claims row 1.
    This drain's claim of row 1 must affect 0 rows, it must apply NOTHING, and
    it must not advance to row 2 either."""
    engine = _db(tmp_path)
    with Session(engine) as s:
        outbox.enqueue(outbox.OP_DELETE, OBJ, "user:old", "owner", session=s)  # 1
        outbox.enqueue(outbox.OP_WRITE, OBJ, "user:new", "owner", session=s)  # 2
        s.commit()
    store = FakeStore()
    with _StealsAfterSelect(engine, engine) as b:
        stats = outbox.drain(session=b, authorizer=store)
    assert b._stolen, "the rival did run"
    assert store.calls == [], "nothing applied by the drain that lost the claim"
    assert stats["delivered"] == 0 and stats["skipped"] == 2, (
        "row 1 lost, row 2 blocked behind it"
    )
    with Session(engine) as s:
        assert _statuses(s) == {1: "claimed", 2: "pending"}


def test_outcome_is_discarded_when_the_claim_was_reaped_and_retaken(tmp_path):
    """Drain A outlives its lease mid-row; the reaper frees the row and drain C
    re-claims and settles it. A's late verdict must not overwrite C's."""
    engine = _db(tmp_path)
    with Session(engine) as s:
        _enqueue_share(s)
        s.commit()
        a_ts = outbox._utcnow() - outbox.CLAIM_LEASE - timedelta(seconds=1)
        assert outbox._claim(s, 1, a_ts) == 1
        # C's drain: reaps A's stale claim, re-claims, delivers.
        assert outbox.drain(session=s, authorizer=FakeStore())["reclaimed"] == 1
        assert _statuses(s) == {1: "done"}
        # A finally finishes and tries to record a failure.
        assert (
            outbox._settle(s, 1, a_ts, status="dead", attempts=1, claimed_at=None)
            is False
        )
        assert _statuses(s) == {1: "done"}, "C's outcome stands"


def test_stale_claim_is_reaped_after_the_lease(tmp_path):
    engine = _db(tmp_path)
    store = FakeStore()
    with Session(engine) as s:
        _enqueue_share(s)
        s.commit()
        stale = outbox._utcnow() - outbox.CLAIM_LEASE - timedelta(seconds=1)
        s.execute(
            sa.update(ownership_outbox).values(status="claimed", claimed_at=stale)
        )
        s.commit()
        stats = outbox.drain(session=s, authorizer=store)
    assert stats["reclaimed"] == 1 and stats["delivered"] == 1
    assert _statuses(s) == {1: "done"}


def test_fresh_claim_is_not_reaped(tmp_path):
    engine = _db(tmp_path)
    with Session(engine) as s:
        _enqueue_share(s)
        s.commit()
        s.execute(
            sa.update(ownership_outbox).values(
                status="claimed", claimed_at=outbox._utcnow()
            )
        )
        s.commit()
        stats = outbox.drain(session=s, authorizer=FakeStore())
    assert stats["reclaimed"] == 0 and stats["delivered"] == 0
    assert _statuses(s) == {1: "claimed"}


# --------------------------------------------------------------------------- 8. revocation window


def test_pending_delete_denies_that_subject_only(tmp_path):
    engine = _db(tmp_path)
    with Session(engine) as s:
        outbox.enqueue(outbox.OP_DELETE, OBJ, BEN, "viewer", session=s)
        s.commit()
        assert outbox.has_pending_revocation(OBJ, BEN, session=s) is True
        assert outbox.has_pending_revocation(OBJ, ADA, session=s) is False, (
            "someone else is unaffected"
        )
        assert outbox.has_pending_revocation(OTHER, BEN, session=s) is False, (
            "another object is unaffected"
        )


def test_pending_revoke_subject_denies_that_subject(tmp_path):
    engine = _db(tmp_path)
    with Session(engine) as s:
        outbox.enqueue(outbox.OP_REVOKE_SUBJECT, OBJ, BEN, session=s)
        s.commit()
        assert outbox.has_pending_revocation(OBJ, BEN, session=s) is True
        assert outbox.has_pending_revocation(OBJ, ADA, session=s) is False


def test_pending_group_revocation_denies_every_member(tmp_path):
    """The read path holds the user's own reference, never their groups; a
    revoked group share therefore denies everyone on the object until it is
    delivered. Fail-closed, by design."""
    engine = _db(tmp_path)
    group = "group:analysts_t1#member"
    with Session(engine) as s:
        outbox.enqueue(outbox.OP_REVOKE_SUBJECT, OBJ, group, session=s)
        s.commit()
        assert outbox.has_pending_revocation(OBJ, BEN, session=s) is True
        assert outbox.has_pending_revocation(OBJ, ADA, session=s) is True
        assert outbox.has_pending_revocation(OTHER, BEN, session=s) is False
        s.execute(
            sa.update(ownership_outbox).values(op=outbox.OP_DELETE, relation="viewer")
        )
        s.commit()
        assert outbox.has_pending_revocation(OBJ, BEN, session=s) is True, (
            "a delete of a group relation too"
        )


def _mirror(session, subject, role, object_id=56, asset_type="chart"):
    """Give the mirror an ownership row for OBJ and a share row for `subject`."""
    uuid = OBJ.split(":", 1)[1]
    if not session.execute(
        sa.select(ownership_db.ownership_object.c.id).where(
            ownership_db.ownership_object.c.object_uuid == uuid
        )
    ).first():
        session.execute(
            sa.insert(ownership_db.ownership_object).values(
                asset_type=asset_type,
                object_id=object_id,
                object_uuid=uuid,
                owner_user_id=1,
                visibility="shared",
            )
        )
    session.execute(
        sa.insert(ownership_db.ownership_share).values(
            asset_type=asset_type, object_id=object_id, subject=subject, role=role
        )
    )


@pytest.mark.parametrize(
    "subject", [BEN, "group:analysts_t1#member"], ids=["user", "group"]
)
def test_a_queued_role_change_is_not_a_revocation(tmp_path, subject):
    """delete(editor) + write(viewer) with the mirror row re-roled to viewer:
    the subject (every member, for a group) keeps access while it is queued.
    Judged on the ROW's subject, so it holds for groups too."""
    engine = _db(tmp_path)
    with Session(engine) as s:
        _mirror(s, subject, "viewer")
        outbox.enqueue(outbox.OP_DELETE, OBJ, subject, "editor", session=s)
        outbox.enqueue(outbox.OP_WRITE, OBJ, subject, "viewer", session=s)
        s.commit()
        assert outbox.has_pending_revocation(OBJ, BEN, session=s) is False
        # Same rows, mirror row gone: now it IS a revocation.
        s.execute(sa.delete(ownership_db.ownership_share))
        s.commit()
        assert outbox.has_pending_revocation(OBJ, BEN, session=s) is True


def test_a_queued_role_change_is_not_a_revocation_with_object_id_passed(tmp_path):
    """Same fixture as above, but through the indexed path both production
    call sites actually use (PR #94 review, H-1/Observations): passing the
    caller's own `object_id` must answer identically to the `object_uuid`
    fallback, without ever joining back into `ownership_object`."""
    engine = _db(tmp_path)
    with Session(engine) as s:
        _mirror(s, BEN, "viewer")  # object_id=56 by default
        outbox.enqueue(outbox.OP_DELETE, OBJ, BEN, "editor", session=s)
        outbox.enqueue(outbox.OP_WRITE, OBJ, BEN, "viewer", session=s)
        s.commit()
        assert outbox.has_pending_revocation(OBJ, BEN, object_id=56, session=s) is False
        # Same rows, mirror row gone: now it IS a revocation.
        s.execute(sa.delete(ownership_db.ownership_share))
        s.commit()
        assert outbox.has_pending_revocation(OBJ, BEN, object_id=56, session=s) is True


def test_a_mirror_row_does_not_exempt_revoke_subject_or_purge(tmp_path):
    """Only a `delete` can be a role change. A revoke_subject or purge with a
    mirror row still present (a stray, or a re-share racing the drain) is
    still a revocation until delivered."""
    engine = _db(tmp_path)
    with Session(engine) as s:
        _mirror(s, BEN, "viewer")
        outbox.enqueue(outbox.OP_REVOKE_SUBJECT, OBJ, BEN, session=s)
        s.commit()
        assert outbox.has_pending_revocation(OBJ, BEN, session=s) is True
        s.execute(
            sa.update(ownership_outbox).values(op=outbox.OP_PURGE_OBJECT, subject=None)
        )
        s.commit()
        assert outbox.has_pending_revocation(OBJ, ADA, session=s) is True


def test_a_mirror_row_on_another_object_does_not_exempt(tmp_path):
    engine = _db(tmp_path)
    with Session(engine) as s:
        _mirror(s, BEN, "viewer", object_id=99)  # share row on a different object id
        s.execute(
            sa.update(ownership_db.ownership_object).values(object_uuid="other-uuid")
        )
        outbox.enqueue(outbox.OP_DELETE, OBJ, BEN, "editor", session=s)
        s.commit()
        assert outbox.has_pending_revocation(OBJ, BEN, session=s) is True


def test_pending_purge_denies_everyone_on_the_object(tmp_path):
    engine = _db(tmp_path)
    with Session(engine) as s:
        outbox.enqueue(outbox.OP_PURGE_OBJECT, OBJ, session=s)
        s.commit()
        assert outbox.has_pending_revocation(OBJ, BEN, session=s) is True
        assert outbox.has_pending_revocation(OBJ, ADA, session=s) is True
        assert outbox.has_pending_revocation(OTHER, BEN, session=s) is False


def test_delivered_revocation_no_longer_denies_and_pending_grant_never_does(tmp_path):
    engine = _db(tmp_path)
    store = FakeStore()
    with Session(engine) as s:
        outbox.enqueue(outbox.OP_DELETE, OBJ, BEN, "viewer", session=s)
        _enqueue_share(s, subject=ADA)  # a pending GRANT is not a revocation
        s.commit()
        assert outbox.has_pending_revocation(OBJ, ADA, session=s) is False
        outbox.drain(session=s, authorizer=store)
        assert outbox.has_pending_revocation(OBJ, BEN, session=s) is False, (
            "delivered -> store is authoritative again"
        )


@pytest.mark.parametrize("status", ["pending", "claimed", "dead"])
def test_pending_revocation_denies_for_every_undelivered_status(tmp_path, status):
    """pending, claimed AND dead all count: a revocation in flight, or one
    an operator has yet to resolve, is still a revocation."""
    engine = _db(tmp_path)
    with Session(engine) as s:
        outbox.enqueue(outbox.OP_DELETE, OBJ, BEN, "viewer", session=s)
        s.execute(sa.update(ownership_outbox).values(status=status))
        s.commit()
        assert outbox.has_pending_revocation(OBJ, BEN, session=s) is True


# ------------------------------------------- 8b. pending_revocations_for (#84)
#
# The same rule as has_pending_revocation, answered for every object a
# subject might be asked about in ONE bounded, indexed pass instead of one
# query per candidate -- what owned_or_shared_rows subtracts from the
# store's list_objects answer before the list filter (and, through FAB's
# list base filter, single-object GET) returns it. Same fixtures, same
# shape of assertion, so a change to one rule's behaviour cannot drift from
# the other's.
#
# `asset_type="chart"` matches OBJ/OTHER, both `chart:...` refs. The empty
# `{}` for `object_id_by_uuid` is correct whenever no `_mirror()` row is in
# play: an object this call cannot resolve to an id is left as a plain
# revocation (the fail-closed default -- see the function's docstring), so
# every one of these fixtures behaves exactly as before the rewrite except
# the one test that exercises the carve-out itself, which builds the map
# `_mirror()` implies.

CHART = "chart"


def test_pending_revocations_for_denies_that_subject_only(tmp_path):
    engine = _db(tmp_path)
    with Session(engine) as s:
        outbox.enqueue(outbox.OP_DELETE, OBJ, BEN, "viewer", session=s)
        s.commit()
        assert outbox.pending_revocations_for(BEN, CHART, {}, session=s) == {OBJ}
        assert outbox.pending_revocations_for(ADA, CHART, {}, session=s) == set(), (
            "someone else is unaffected"
        )


def test_pending_revocations_for_revoke_subject(tmp_path):
    engine = _db(tmp_path)
    with Session(engine) as s:
        outbox.enqueue(outbox.OP_REVOKE_SUBJECT, OBJ, BEN, session=s)
        s.commit()
        assert outbox.pending_revocations_for(BEN, CHART, {}, session=s) == {OBJ}
        assert outbox.pending_revocations_for(ADA, CHART, {}, session=s) == set()


def test_pending_revocations_for_group_denies_every_member(tmp_path):
    """Same fail-closed rule as has_pending_revocation: the caller holds
    only its own reference, never the groups it belongs to, so a revoked
    group share is treated as revoking every subject until delivered."""
    engine = _db(tmp_path)
    group = "group:analysts_t1#member"
    with Session(engine) as s:
        outbox.enqueue(outbox.OP_REVOKE_SUBJECT, OBJ, group, session=s)
        s.commit()
        assert outbox.pending_revocations_for(BEN, CHART, {}, session=s) == {OBJ}
        assert outbox.pending_revocations_for(ADA, CHART, {}, session=s) == {OBJ}


def test_pending_revocations_for_purge_denies_everyone(tmp_path):
    engine = _db(tmp_path)
    with Session(engine) as s:
        outbox.enqueue(outbox.OP_PURGE_OBJECT, OBJ, session=s)
        s.commit()
        assert outbox.pending_revocations_for(BEN, CHART, {}, session=s) == {OBJ}
        assert outbox.pending_revocations_for(ADA, CHART, {}, session=s) == {OBJ}


@pytest.mark.parametrize(
    "subject", [BEN, "group:analysts_t1#member"], ids=["user", "group"]
)
def test_pending_revocations_for_excludes_a_queued_role_change(tmp_path, subject):
    """delete(editor) + write(viewer), the mirror re-roled to viewer: not a
    revocation, same as has_pending_revocation, judged on the row's subject.
    The carve-out only fires when the caller's own scan resolved the
    object's id -- here, the id `_mirror()` gave the ownership row -- which
    is the shape `owned_or_shared_rows` actually builds it in."""
    engine = _db(tmp_path)
    object_id_by_uuid = {OBJ.split(":", 1)[1]: 56}  # _mirror()'s default object_id
    with Session(engine) as s:
        _mirror(s, subject, "viewer")
        outbox.enqueue(outbox.OP_DELETE, OBJ, subject, "editor", session=s)
        outbox.enqueue(outbox.OP_WRITE, OBJ, subject, "viewer", session=s)
        s.commit()
        got = outbox.pending_revocations_for(BEN, CHART, object_id_by_uuid, session=s)
        assert got == set()
        # Same rows, mirror row gone: now it IS a revocation.
        s.execute(sa.delete(ownership_db.ownership_share))
        s.commit()
        got = outbox.pending_revocations_for(BEN, CHART, object_id_by_uuid, session=s)
        assert got == {OBJ}


def test_pending_revocations_for_ignores_a_role_change_it_cannot_resolve(tmp_path):
    """The carve-out is skipped, not guessed, for an object the caller's own
    scan did not cover: the same fail-closed default as a fresh `check`
    (never fewer denials), and it is what makes it safe for
    `owned_or_shared_rows` to pass only its own asset type's rows."""
    engine = _db(tmp_path)
    with Session(engine) as s:
        _mirror(s, BEN, "viewer")
        outbox.enqueue(outbox.OP_DELETE, OBJ, BEN, "editor", session=s)
        s.commit()
        assert outbox.pending_revocations_for(BEN, CHART, {}, session=s) == {OBJ}, (
            "no id for this uuid in the map -- treated as a plain revocation"
        )


def test_pending_revocations_for_ignores_delivered_rows_and_pending_grants(tmp_path):
    engine = _db(tmp_path)
    store = FakeStore()
    with Session(engine) as s:
        outbox.enqueue(outbox.OP_DELETE, OBJ, BEN, "viewer", session=s)
        _enqueue_share(s, subject=ADA)  # a pending GRANT is not a revocation
        s.commit()
        assert outbox.pending_revocations_for(ADA, CHART, {}, session=s) == set()
        outbox.drain(session=s, authorizer=store)
        assert outbox.pending_revocations_for(BEN, CHART, {}, session=s) == set(), (
            "delivered -> store is authoritative again"
        )


@pytest.mark.parametrize("status", ["pending", "claimed", "dead"])
def test_pending_revocations_for_includes_every_undelivered_status(tmp_path, status):
    """pending, claimed AND dead all count: a revocation in flight, or one
    an operator has yet to resolve, is still a revocation."""
    engine = _db(tmp_path)
    with Session(engine) as s:
        outbox.enqueue(outbox.OP_DELETE, OBJ, BEN, "viewer", session=s)
        s.execute(sa.update(ownership_outbox).values(status=status))
        s.commit()
        assert outbox.pending_revocations_for(BEN, CHART, {}, session=s) == {OBJ}


def test_pending_revocations_for_spans_every_object_in_one_query(tmp_path):
    """The whole point: one call answers for every object the subject might
    be asked about, not one call per candidate."""
    engine = _db(tmp_path)
    with Session(engine) as s:
        outbox.enqueue(outbox.OP_DELETE, OBJ, BEN, "viewer", session=s)
        outbox.enqueue(outbox.OP_REVOKE_SUBJECT, OTHER, BEN, session=s)
        s.commit()
        assert outbox.pending_revocations_for(BEN, CHART, {}, session=s) == {OBJ, OTHER}


# --------------------------------------------------------------------------- 9. interrupt


def test_soft_time_limit_hands_the_row_back_untouched(tmp_path):
    """The worker's soft time limit is not the store's verdict: the row goes
    back to pending with attempts unchanged, the claim released, and the
    signal propagates so Celery ends the task."""
    engine = _db(tmp_path)

    class Interrupted(FakeStore):
        def write_tuple(self, subject, relation, obj, *, strict=False):
            raise outbox.SoftTimeLimitExceeded()

    with Session(engine) as s:
        _enqueue_share(s)
        s.commit()
        with pytest.raises(outbox.SoftTimeLimitExceeded):
            outbox.drain(session=s, authorizer=Interrupted())
        (row,) = _rows(s)
        assert (
            row["status"] == "pending"
            and row["attempts"] == 0
            and row["claimed_at"] is None
        )
        assert row["last_error"] is None
        assert outbox.drain(session=s, authorizer=FakeStore())["delivered"] == 1, (
            "next pass takes it"
        )


# --------------------------------------------------------------------------- 10. dead / replay


def test_row_is_parked_dead_after_max_attempts_and_can_be_replayed(tmp_path):
    engine = _db(tmp_path)
    store = FakeStore(up=False)
    with Session(engine) as s:
        _enqueue_share(s)
        s.commit()
        for _ in range(3):
            outbox.drain(session=s, authorizer=store, max_attempts=3)
        (row,) = _rows(s)
        assert row["status"] == "dead" and row["attempts"] == 3
        st = outbox.status(session=s)
        assert st["dead"] == 1 and st["oldest_dead"] is not None
        assert outbox.dead_rows(session=s)[0]["id"] == 1
        assert outbox.replay_dead(session=s) == 1
        (row,) = _rows(s)
        assert row["status"] == "pending" and row["attempts"] == 0
        store.up = True
        assert outbox.drain(session=s, authorizer=store)["delivered"] == 1


def test_replay_can_target_specific_rows(tmp_path):
    engine = _db(tmp_path)
    with Session(engine) as s:
        _enqueue_share(s)
        _enqueue_share(s, obj=OTHER)
        s.execute(
            sa.update(ownership_outbox).values(
                status="dead", attempts=10, last_error="x"
            )
        )
        s.commit()
        assert outbox.replay_dead(row_ids=[2], session=s) == 1
        assert _statuses(s) == {1: "dead", 2: "pending"}
        assert _rows(s)[1]["attempts"] == 0 and _rows(s)[1]["last_error"] is None


def test_unknown_op_is_parked_dead_not_retried(tmp_path):
    """A row this code cannot interpret (a newer writer, a downgrade) is an
    operator problem, not something to retry ten times."""
    engine = _db(tmp_path)
    with Session(engine) as s:
        outbox.enqueue("frobnicate", OBJ, BEN, session=s)
        s.commit()
        stats = outbox.drain(session=s, authorizer=FakeStore())
    assert stats["dead"] == 1 and stats["circuit_open"] is False
    assert "unknown outbox op" in _rows(s)[0]["last_error"]


def test_status_reports_blocked_rows(tmp_path):
    engine = _db(tmp_path)
    with Session(engine) as s:
        outbox.enqueue(outbox.OP_DELETE, OBJ, "user:old", "owner", session=s)
        s.execute(sa.update(ownership_outbox).values(status="dead"))
        _enqueue_share(s)
        _enqueue_share(s, obj=OTHER)
        s.commit()
        st = outbox.status(session=s)
    assert (st["pending"], st["dead"], st["blocked"]) == (2, 1, 1), (
        "one of the two pending rows is frozen behind the dead object"
    )


# --------------------------------------------------------------------------- 11. idempotent


def test_redelivery_is_idempotent(tmp_path):
    """A row replayed after a crash is written to the store AGAIN and that
    second write reads as success (the real client maps "already exists" to
    True); the row ends done, not dead."""
    engine = _db(tmp_path)
    store = FakeStore()
    with Session(engine) as s:
        _enqueue_share(s)
        s.commit()
        outbox.drain(session=s, authorizer=store)
        s.execute(
            sa.update(ownership_outbox).values(status="pending")
        )  # crash-mid-pass replay
        s.commit()
        stats = outbox.drain(session=s, authorizer=store)
        assert stats["delivered"] == 1 and _statuses(s) == {1: "done"}
    assert len([c for c in store.calls if c[0] == "write"]) == 2, "delivered twice"
    assert store.tuples == {(BEN, "viewer", OBJ)}


# --------------------------------------------------------------------------- 12. operator


class _App:
    def __init__(self, config):
        self.config = config


def test_status_reports_last_delivery_and_stall(tmp_path):
    engine = _db(tmp_path)
    with Session(engine) as s:
        st = outbox.status(session=s)
        assert st["last_delivered"] is None and st["stalled"] is False
        _enqueue_share(s)
        s.commit()
        assert outbox.status(session=s)["stalled"] is False, (
            "fresh pending work is normal"
        )
        s.execute(
            sa.update(ownership_outbox).values(
                created_on=outbox._utcnow() - outbox.STALL_AFTER - timedelta(seconds=1)
            )
        )
        s.commit()
        assert outbox.status(session=s)["stalled"] is True, (
            "old pending work means nothing drains"
        )
        outbox.drain(session=s, authorizer=FakeStore())
        st = outbox.status(session=s)
        assert st["last_delivered"] is not None and st["stalled"] is False


def test_prune_removes_only_old_done_rows(tmp_path):
    engine = _db(tmp_path)
    with Session(engine) as s:
        for i in range(4):
            _enqueue_share(s, obj=f"chart:{i}")
        s.commit()
        old = outbox._utcnow() - timedelta(days=40)
        s.execute(
            sa.update(ownership_outbox)
            .where(ownership_outbox.c.id == 1)
            .values(status="done", done_on=old)
        )
        s.execute(
            sa.update(ownership_outbox)
            .where(ownership_outbox.c.id == 2)
            .values(status="done", done_on=outbox._utcnow())
        )
        s.execute(
            sa.update(ownership_outbox)
            .where(ownership_outbox.c.id == 3)
            .values(status="dead", done_on=old)
        )
        s.commit()
        assert outbox.prune_done(timedelta(days=30), session=s) == 1
        assert _statuses(s) == {2: "done", 3: "dead", 4: "pending"}


def test_discarded_failure_outcome_is_not_counted(tmp_path):
    """Drain A's row was reaped and re-delivered by C while A was stuck. A's
    late failure must neither be written nor reported as dead/failed."""
    engine = _db(tmp_path)

    class ReapedMeanwhile(FakeStore):
        def __init__(self, session):
            super().__init__(up=False)
            self._s = session

        def write_tuple(self, subject, relation, obj, *, strict=False):
            # Simulate the lease expiring and drain C settling the row.
            self._s.execute(
                sa.update(ownership_outbox).values(status="done", claimed_at=None)
            )
            self._s.commit()
            return super().write_tuple(subject, relation, obj, strict=strict)

    with Session(engine) as s:
        _enqueue_share(s)
        s.commit()
        stats = outbox.drain(session=s, authorizer=ReapedMeanwhile(s))
    assert stats["failed"] == 0 and stats["dead"] == 0 and stats["delivered"] == 0
    assert _statuses(s) == {1: "done"}


def test_startup_warns_when_outbox_is_on_and_nothing_drains_it(monkeypatch):
    monkeypatch.setenv("OWNERSHIP_OUTBOX_ENABLED", "true")
    warnings: list[str] = []
    monkeypatch.setattr(
        outbox.logger, "warning", lambda msg, *a, **k: warnings.append(msg % a)
    )
    outbox.warn_if_no_drainer(_App({"CELERY_CONFIG": None}))
    outbox.warn_if_no_drainer(
        _App({"CELERY_CONFIG": {"beat_schedule": {"x": {"task": "other"}}}})
    )
    assert len(warnings) == 2 and all("no Celery beat entry" in w for w in warnings)

    warnings.clear()

    class Cfg:
        beat_schedule = {"drain": {"task": outbox.TASK_NAME, "schedule": 10}}

    outbox.warn_if_no_drainer(_App({"CELERY_CONFIG": Cfg}))
    monkeypatch.setenv("OWNERSHIP_OUTBOX_ENABLED", "false")
    outbox.warn_if_no_drainer(_App({"CELERY_CONFIG": None}))
    assert warnings == [], "a scheduled drain, or the outbox off, is fine"


# --------------------------------------------------------------------------- migration guard


def test_downgrade_refuses_to_discard_undelivered_rows(tmp_path):
    import os

    engine = _db(tmp_path)
    uri = str(engine.url)
    with Session(engine) as s:
        _enqueue_share(s)
        s.commit()
    os.environ.pop("OWNERSHIP_OUTBOX_FORCE_DOWNGRADE", None)
    with pytest.raises(Exception, match="undelivered"):
        migrate.downgrade("0001_object_ownership", uri)
    assert migrate.current(uri) == "0002_ownership_outbox", "nothing was dropped"
    os.environ["OWNERSHIP_OUTBOX_FORCE_DOWNGRADE"] = "1"
    try:
        migrate.downgrade("0001_object_ownership", uri)
        assert migrate.current(uri) == "0001_object_ownership"
    finally:
        os.environ.pop("OWNERSHIP_OUTBOX_FORCE_DOWNGRADE", None)


def test_discard_dead_clears_the_row_and_unblocks_the_object(tmp_path):
    """Issue #118's missing escape hatch. A row the store will never accept
    blocks every later intent for the SAME object, and `replay` only kills
    it again. `discard_dead` deletes the intent so the queue moves; the
    store is left exactly as it was, which is why it warns and why the
    operator's next step is `check`."""
    engine = _db(tmp_path)
    store = FakeStore(up=True)
    with Session(engine) as s:
        _enqueue_share(s)  # row 1, will be made dead
        _enqueue_share(s, subject="user:later")  # row 2, same object, queued behind it
        s.execute(
            sa.update(ownership_outbox)
            .where(ownership_outbox.c.id == 1)
            .values(status="dead", attempts=3, last_error="validation_error")
        )
        s.commit()

        # Blocked: the later row cannot be delivered while row 1 sits dead.
        assert outbox.drain(session=s, authorizer=store)["delivered"] == 0
        assert _statuses(s) == {1: "dead", 2: "pending"}

        assert outbox.discard_dead(session=s) == 1
        assert _statuses(s) == {2: "pending"}
        assert outbox.drain(session=s, authorizer=store)["delivered"] == 1
        assert outbox.status(session=s)["dead"] == 0


def test_discard_dead_can_target_specific_rows_and_leaves_the_rest(tmp_path):
    engine = _db(tmp_path)
    with Session(engine) as s:
        _enqueue_share(s)
        _enqueue_share(s, obj=OTHER)
        _enqueue_share(s, obj="dashboard:third")
        s.execute(
            sa.update(ownership_outbox)
            .where(ownership_outbox.c.id.in_([1, 2]))
            .values(status="dead", attempts=3)
        )
        s.commit()
        assert outbox.discard_dead(row_ids=[2], session=s) == 1
        assert _statuses(s) == {1: "dead", 3: "pending"}, "only the named row goes"
        assert outbox.discard_dead(row_ids=[3], session=s) == 0, (
            "pending is never discarded"
        )
        assert _statuses(s) == {1: "dead", 3: "pending"}


# --- review round 1: discarding a REVOKING row restores access -------------


def _enqueue(session, op, obj=OBJ, subject=BEN):
    return outbox.enqueue(op, obj, subject, "viewer", session=session)


def _kill_all(session):
    session.execute(
        sa.update(ownership_outbox)
        .where(ownership_outbox.c.status == "pending")
        .values(status="dead", attempts=3, last_error="validation_error")
    )
    session.commit()


def test_a_dead_revoking_row_is_kept_because_discarding_it_would_grant(tmp_path):
    """`has_pending_revocation` counts dead rows ON PURPOSE: while one sits
    there the read gate denies that subject locally, even though the store's
    tuple was never removed. Discarding it therefore does not leave access
    as it was -- it GRANTS, silently, and `reconcile` will not take the
    tuple away again (it never deletes a share tuple the mirror has lost).
    Found in review round 1."""
    engine = _db(tmp_path)
    with Session(engine) as s:
        _enqueue(s, outbox.OP_WRITE, obj="chart:granting")
        _enqueue(s, outbox.OP_REVOKE_SUBJECT, obj="chart:revoking")
        _enqueue(s, outbox.OP_DELETE, obj="chart:deleting")
        _enqueue(s, outbox.OP_PURGE_OBJECT, obj="chart:purging")
        _kill_all(s)

        assert outbox.discard_dead(session=s) == 1, "only the granting row went"
        left = {r["op"] for r in _rows(s)}
        assert left == {
            outbox.OP_REVOKE_SUBJECT,
            outbox.OP_DELETE,
            outbox.OP_PURGE_OBJECT,
        }


def test_a_named_revoking_row_is_kept_too(tmp_path):
    """Naming the row is not the same as knowing what it is."""
    engine = _db(tmp_path)
    with Session(engine) as s:
        _enqueue(s, outbox.OP_REVOKE_SUBJECT)
        _kill_all(s)
        assert outbox.discard_dead(row_ids=[1], session=s) == 0
        assert _statuses(s) == {1: "dead"}


def test_a_revoking_row_can_still_be_discarded_knowingly(tmp_path):
    """The operator who will remove the tuples by hand has a way through."""
    engine = _db(tmp_path)
    with Session(engine) as s:
        _enqueue(s, outbox.OP_REVOKE_SUBJECT)
        _kill_all(s)
        assert outbox.discard_dead(session=s) == 0
        assert outbox.discard_dead(session=s, including_revocations=True) == 1
        assert _rows(s) == []


def test_keeping_a_revoking_row_still_unblocks_the_objects_that_can_move(tmp_path):
    """The point of `discard` is that the queue moves again. Holding a
    revoking row back must not hold back another OBJECT's queue -- delivery
    is ordered per object, so it does not."""
    engine = _db(tmp_path)
    store = FakeStore(up=True)
    with Session(engine) as s:
        _enqueue(s, outbox.OP_REVOKE_SUBJECT, obj=OBJ)
        _enqueue(s, outbox.OP_WRITE, obj=OTHER)
        _kill_all(s)
        _enqueue(s, outbox.OP_WRITE, obj=OTHER, subject="user:later")

        assert outbox.discard_dead(session=s) == 1  # OTHER's dead write
        assert outbox.drain(session=s, authorizer=store)["delivered"] == 1
        statuses = _statuses(s)
        assert statuses[1] == "dead", "the revoking row stays, and keeps denying"
        assert statuses[3] == "done", "the other object's queue moved"

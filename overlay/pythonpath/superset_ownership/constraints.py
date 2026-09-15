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
"""The two foreign keys the ownership chain keeps (decision record:
qa/reviews/decision-owner-fk.md, spec §4.1/§4.3).

OWNER_FK   ownership_object.owner_user_id -> ab_user.id   ON DELETE SET NULL
           Cross-MetaData (ab_user is Superset's), so it is hand-written DDL
           here and never a declarative ForeignKey (migrations/env.py).
           A hard-deleted user leaves the object with no owner, which is
           exactly what the module already means by "unowned": the read
           gate grants nobody, shares go dormant, administrators reach it
           and a tenant administrator may claim it, and the next backfill
           re-attributes it (creator, last editor, history, the configured
           default, a tenant administrator). The un-owning itself is the
           database's; the record of it is guard.py's user-delete listener
           (ORM deletes) and `check`/`reconcile` (anything else).

SHARE_FK   ownership_share(asset_type, object_id)
             -> ownership_object(asset_type, object_id)  ON DELETE CASCADE
           Intra-module (same MetaData), so db.py declares it too; this
           module adds it to installs that predate revision 0003.

There is deliberately NO foreign key from ownership_object.object_id to
dashboards.id / slices.id; the decision record says why (one column cannot
reference two tables, a cascade would duplicate the delete hook's row
cleanup while doing nothing for the store tuples, and a restrict would
block every retention purge).

Every function here runs inside an Alembic operations context: revision
0003 provides one, and ``ensure_owner_fk_on`` / ``ensure_share_fk_on`` open
one for the re-check ``migrate.upgrade`` performs after every run.

WHEN THE OWNER CONSTRAINT IS CREATED. On the deployed stack it is created
by revision 0003 itself, directly: Flask-AppBuilder's SecurityManager
creates every ``ab_*`` table (``create_db()``, ``FAB_CREATE_DB`` default
true) inside ``appbuilder.init_app``, which Superset runs BEFORE
``FLASK_APP_MUTATOR`` -- so when the mutator runs this chain on a fresh
database, ``ab_user`` already exists, and a fresh ``superset db upgrade``
ends with both constraints in place and no deferral. The §12 test harness
has the same order, and so does every ``superset ownership db ...``
command: they are Flask CLI commands, app creation (and with it FAB's
``create_db()``) runs before the command body, so ``superset ownership db
upgrade`` run by hand on a fresh database, in either order relative to
``superset db upgrade``, also finds ``ab_user`` and creates the constraint
directly. There is no order of the two CLI commands that reaches the
deferral under the default configuration.

The re-check is the FALLBACK for a database where ``ab_user`` was absent
when 0003 ran, which is reachable only outside the CLI: a direct
``migrate.upgrade(uri)`` from an embedding or a custom entrypoint that
creates no application first (the tests and the probes do this), or a
database whose ``ab_user`` was dropped by hand. ``FAB_CREATE_DB=False``
defers as designed too, but is not a working fresh-database deployment of
Superset: its own ``superset db upgrade`` then fails on the first core
migration that references ``ab_user``. Alembic never re-runs a recorded
revision, so when the deferral does engage 0003 records itself with the
constraint skipped (INFO) and ``migrate.upgrade`` adds it on its next run
-- the next boot, or the deploy contract's unconditional ``superset
ownership db upgrade`` -- warning once while ``ab_user`` is still absent.
``check_consistency`` reports the constraint as missing until then. The
mechanism is kept because it costs two inspections and the tests exercise
it.

CONCURRENCY. Several processes boot at once on a deploy (web workers, the
Celery worker, ``superset init``) and every one of them runs the re-check.
On PostgreSQL the check-then-add is serialised with a transaction-scoped
advisory lock (``pg_advisory_xact_lock``), so the second process inspects
after the first has committed and finds the constraint present. On any
dialect, and as a belt for the plain-Alembic path, a ``DuplicateObject``-
shaped failure of the ADD is followed by a re-inspection: if the name is
present, another process won and the result is success. Within ONE process
the operations context is a process-global proxy (``alembic.op``), so the
``ensure_*_on`` entry points and ``migrate.upgrade`` hold a process-local
lock; two threads never run DDL through the proxy at once.

LOCKING ON ab_user. ``ADD CONSTRAINT`` validates existing rows under a
``SHARE ROW EXCLUSIVE`` lock on BOTH tables, so an ``UPDATE ab_user`` (FAB
writes ``last_login`` on every login) waits for the scan. At the current
row counts that is milliseconds. The PostgreSQL idiom for a large table --
``ADD CONSTRAINT ... NOT VALID``, commit, then ``VALIDATE CONSTRAINT``
under ``SHARE UPDATE EXCLUSIVE`` -- needs two transactions, which a
single-transaction revision cannot give it, so 0003 does not do it. If
``ownership_object`` ever grows to where the scan matters, an operator can
start it by hand before the deploy::

    ALTER TABLE ownership_object
        ADD CONSTRAINT fk_ownership_object_owner_user_id_ab_user
        FOREIGN KEY (owner_user_id) REFERENCES ab_user (id)
        ON DELETE SET NULL NOT VALID;

and stop there: ``ensure_owner_fk`` (0003, and the re-check after every
``migrate.upgrade``) finds the name present, reads
``pg_constraint.convalidated``, and when it is false runs the same cleanup
0003 would have run (the ``NOT VALID`` add does not check existing rows,
so a dangling owner would make ``VALIDATE`` fail) and then ``VALIDATE
CONSTRAINT``, logging both. ``VALIDATE`` is idempotent and takes only
``SHARE UPDATE EXCLUSIVE`` on ``ownership_object`` (``ROW SHARE`` on
``ab_user``), so logins are not blocked. Until it has run,
``check_consistency`` reports the constraint as present but not
validated and fails. The same applies to the share constraint.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

import sqlalchemy as sa
from alembic import op
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError

logger = logging.getLogger(__name__)

OWNER_FK = "fk_ownership_object_owner_user_id_ab_user"
SHARE_FK = "fk_ownership_share_object_ownership_object"
CORE_USER_TABLE = "ab_user"

# The revision that introduced the constraints. migrate.upgrade re-ensures
# them only on a database at or beyond it, so a deliberate downgrade to
# 0002 stays a database without foreign keys.
FK_REVISION = "0003_ownership_foreign_keys"

# One process, one DDL run at a time: `alembic.op` is a module-global proxy
# that Operations.context() rewires, so two threads sharing it cross their
# connections (one thread's ADD CONSTRAINT runs on the other's transaction).
# Re-entrant, because migrate.upgrade holds it around the whole run and the
# re-check inside that run takes it again.
_DDL_LOCK = threading.RLock()

# PostgreSQL advisory lock key: serialises the inspect-and-add across
# PROCESSES for the life of the transaction that takes it.
ADVISORY_LOCK_KEY = "superset_ownership.constraints"


# --------------------------------------------------------------------- inspection


def _bind():
    return op.get_bind()


def has_table(name: str) -> bool:
    return sa.inspect(_bind()).has_table(name)


def fk_names(table: str) -> set[str]:
    return {fk["name"] for fk in sa.inspect(_bind()).get_foreign_keys(table)}


def _is_sqlite() -> bool:
    return _bind().dialect.name == "sqlite"


def _serialise_across_processes() -> None:
    """Take the module's advisory lock for the rest of this transaction
    (PostgreSQL). Every process that may add a constraint takes it BEFORE
    inspecting, so the check-then-add is one critical section across the
    boot storm; released at commit or rollback. A no-op elsewhere."""
    conn = _bind()
    if conn.dialect.name == "postgresql":
        conn.execute(
            sa.text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": ADVISORY_LOCK_KEY},
        )


def _fk_present(bind: Any, table: str, name: str) -> bool:
    insp = sa.inspect(bind)
    if not insp.has_table(table):
        return False
    return name in {fk["name"] for fk in insp.get_foreign_keys(table)}


def _fk_validated(bind: Any, table: str, name: str) -> bool:
    """Has PostgreSQL checked the existing rows against this constraint?
    False for a constraint added ``NOT VALID`` (by hand, see the module
    docstring) whose ``VALIDATE CONSTRAINT`` has not run. Every other
    dialect validates on creation, so True there. The caller has
    established the constraint is present.

    Read in the schema the connection resolves ``table`` to
    (``pg_table_is_visible``, the same rule the inspector's ``has_table``
    and ``get_foreign_keys`` apply), so a same-named table and constraint
    in another schema is never the one answered for; exactly one row is
    expected, because the caller has just found the constraint."""
    if bind.dialect.name != "postgresql":
        return True
    query = sa.text(
        "SELECT c.convalidated FROM pg_constraint c "
        "JOIN pg_class t ON t.oid = c.conrelid "
        "WHERE c.conname = :name AND t.relname = :table "
        "AND pg_catalog.pg_table_is_visible(t.oid)"
    )
    params = {"name": name, "table": table}
    if isinstance(bind, sa.engine.Engine):
        with bind.connect() as conn:
            rows = conn.execute(query, params).scalars().all()
    else:
        rows = bind.execute(query, params).scalars().all()
    if len(rows) != 1:
        raise RuntimeError(
            f"superset_ownership: expected one pg_constraint row for {name} on "
            f"{table} in the visible schema, found {len(rows)}"
        )
    return bool(rows[0])


def missing(bind: Any) -> list[str]:
    """The constraint names not in the database, in creation order."""
    return [
        name
        for table, name in (
            ("ownership_object", OWNER_FK),
            ("ownership_share", SHARE_FK),
        )
        if not _fk_present(bind, table, name)
    ]


def status(bind: Any) -> dict[str, Any]:
    """What `check_consistency` reports: is each constraint in the database,
    is it validated (PostgreSQL; a hand-applied ``NOT VALID`` constraint is
    present but enforced for new writes only until the next upgrade
    validates it), and does the chain's recorded revision say it should be.
    `bind` is an Engine or Connection on the application's database."""
    from superset_ownership import migrate

    at = migrate.current_on(bind)
    owner_present = _fk_present(bind, "ownership_object", OWNER_FK)
    share_present = _fk_present(bind, "ownership_share", SHARE_FK)
    return {
        "owner_fk_present": owner_present,
        "share_fk_present": share_present,
        "owner_fk_validated": owner_present
        and _fk_validated(bind, "ownership_object", OWNER_FK),
        "share_fk_validated": share_present
        and _fk_validated(bind, "ownership_share", SHARE_FK),
        "required": at is not None and migrate.lineage_includes(FK_REVISION, at),
        "revision": at,
    }


# ------------------------------------------------------------------------ DDL


def create_fk(
    name: str,
    table: str,
    referent: str,
    local_cols: list[str],
    remote_cols: list[str],
    ondelete: str,
) -> None:
    """Add a named foreign key, idempotently, on any supported dialect.

    Superset's own helper is used when Superset is importable, so the
    dialect handling (SQLite batch mode) and the naming are exactly those of
    core's migrations. The pure test job has no Superset; the fallback does
    the same two things with plain Alembic.
    """
    try:
        from superset.migrations.shared.utils import create_fks_for_table
    except ImportError:
        create_fks_for_table = None

    if create_fks_for_table is not None:
        create_fks_for_table(
            name, table, referent, local_cols, remote_cols, ondelete=ondelete
        )
        return

    if not has_table(table) or name in fk_names(table):
        return
    if _is_sqlite():
        # SQLite cannot ALTER TABLE ... ADD CONSTRAINT; batch mode rebuilds
        # the table. Both callers check the referent exists first, and
        # SQLite only enforces the constraint on connections that turn
        # PRAGMA foreign_keys on.
        with op.batch_alter_table(table) as batch:
            batch.create_foreign_key(
                name, referent, local_cols, remote_cols, ondelete=ondelete
            )
    else:
        op.create_foreign_key(
            name, table, referent, local_cols, remote_cols, ondelete=ondelete
        )


def validate_fk(name: str, table: str) -> None:
    """``VALIDATE CONSTRAINT`` a constraint that was added ``NOT VALID``
    (PostgreSQL). Idempotent; ``SHARE UPDATE EXCLUSIVE`` on ``table`` only.
    The caller has repaired the rows the constraint would reject."""
    _bind().execute(sa.text(f'ALTER TABLE {table} VALIDATE CONSTRAINT "{name}"'))


def drop_fk(name: str, table: str) -> None:
    """Drop a named foreign key if present, on any supported dialect."""
    if not has_table(table) or name not in fk_names(table):
        return
    if _is_sqlite():
        # resolve_fks=False: reflect only this table. Reflecting the referent
        # too would fail on a database where ab_user is absent.
        with op.batch_alter_table(
            table, reflect_kwargs={"resolve_fks": False}
        ) as batch:
            batch.drop_constraint(name, type_="foreignkey")
    else:
        op.drop_constraint(name, table, type_="foreignkey")


# --------------------------------------------------------------- pre-cleanup


def null_dangling_owners() -> int:
    """SET NULL every owner_user_id naming a user that no longer exists.

    What the constraint would have done had it been there; what
    `is_unowned` already treats such a row as. Returns the count."""
    conn = _bind()
    dangling = (
        "FROM ownership_object o WHERE o.owner_user_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM ab_user u WHERE u.id = o.owner_user_id)"
    )
    count = conn.execute(sa.text(f"SELECT count(*) {dangling}")).scalar() or 0
    if count:
        conn.execute(
            sa.text(
                "UPDATE ownership_object SET owner_user_id = NULL "
                "WHERE owner_user_id IS NOT NULL AND NOT EXISTS "
                "(SELECT 1 FROM ab_user u WHERE u.id = ownership_object.owner_user_id)"
            )
        )
    return int(count)


def delete_orphan_shares() -> int:
    """Delete share rows whose object row is gone (what `reconcile --write`
    does). Returns the count."""
    conn = _bind()
    orphan = (
        "FROM ownership_share s WHERE NOT EXISTS (SELECT 1 FROM ownership_object o "
        "WHERE o.asset_type = s.asset_type AND o.object_id = s.object_id)"
    )
    count = conn.execute(sa.text(f"SELECT count(*) {orphan}")).scalar() or 0
    if count:
        conn.execute(
            sa.text(
                "DELETE FROM ownership_share WHERE NOT EXISTS "
                "(SELECT 1 FROM ownership_object o "
                "WHERE o.asset_type = ownership_share.asset_type "
                "AND o.object_id = ownership_share.object_id)"
            )
        )
    return int(count)


# --------------------------------------------------------------------- ensure


def ensure_owner_fk(*, deferred: bool = False) -> bool:
    """Owner FK present afterwards? False only when ab_user does not exist
    yet (the chain ran before Superset's, see the module docstring), in
    which case nothing is changed and the next ``migrate.upgrade`` tries
    again. ``deferred`` is what revision 0003 passes: the re-check that
    follows it in the same run is the one that warns, so a first run on a
    database without ab_user warns once, not twice.

    Not safe to call from two threads of one process (the operations
    context is process-global); ``ensure_owner_fk_on`` and
    ``migrate.upgrade`` hold ``_DDL_LOCK`` for their callers."""
    if not has_table("ownership_object"):
        return False
    if not has_table(CORE_USER_TABLE):
        logger.log(
            logging.INFO if deferred else logging.WARNING,
            "superset_ownership: %s does not exist yet (this chain ran before "
            "Superset created its tables: a direct migrate.upgrade() outside the "
            "application, or FAB_CREATE_DB=False); %s is not created now and will "
            "be on the next `superset ownership db upgrade`",
            CORE_USER_TABLE,
            OWNER_FK,
        )
        return False
    _serialise_across_processes()
    if OWNER_FK in fk_names("ownership_object"):
        _validate_if_needed(
            OWNER_FK,
            "ownership_object",
            null_dangling_owners,
            "ownership row(s) named a user that no longer exists; "
            "owner set to NULL (unowned)",
        )
        return True
    nulled = null_dangling_owners()
    create_fk(
        OWNER_FK,
        "ownership_object",
        CORE_USER_TABLE,
        ["owner_user_id"],
        ["id"],
        "SET NULL",
    )
    # Logged AFTER the ADD CONSTRAINT succeeded: had it failed, the UPDATE
    # above is rolled back with it and a line saying rows were changed
    # would be false.
    logger.log(
        logging.WARNING if nulled else logging.INFO,
        "superset_ownership: %d ownership row(s) named a user that no longer exists; "
        "owner set to NULL (unowned) and %s added",
        nulled,
        OWNER_FK,
    )
    logger.info("superset_ownership: %s created", OWNER_FK)
    return True


def ensure_share_fk() -> bool:
    """Share FK present afterwards? False only when the tables are absent.
    Same threading caveat as ``ensure_owner_fk``."""
    if not has_table("ownership_share") or not has_table("ownership_object"):
        return False
    _serialise_across_processes()
    if SHARE_FK in fk_names("ownership_share"):
        _validate_if_needed(
            SHARE_FK,
            "ownership_share",
            delete_orphan_shares,
            "share row(s) had no ownership row behind them; removed",
        )
        return True
    removed = delete_orphan_shares()
    create_fk(
        SHARE_FK,
        "ownership_share",
        "ownership_object",
        ["asset_type", "object_id"],
        ["asset_type", "object_id"],
        "CASCADE",
    )
    logger.log(
        logging.WARNING if removed else logging.INFO,
        "superset_ownership: %d share row(s) had no ownership row behind them; "
        "removed and %s added",
        removed,
        SHARE_FK,
    )
    logger.info("superset_ownership: %s created", SHARE_FK)
    return True


def _validate_if_needed(
    name: str, table: str, repair: Callable[[], int], repaired: str
) -> None:
    """A constraint that is present but was added ``NOT VALID`` by hand
    (PostgreSQL; module docstring) is finished here: the rows it would
    reject are repaired exactly as they would have been before a normal
    add, then ``VALIDATE CONSTRAINT``. Both logged. A no-op for a validated
    constraint and on every other dialect."""
    if _fk_validated(_bind(), table, name):
        return
    count = repair()
    validate_fk(name, table)
    logger.log(
        logging.WARNING if count else logging.INFO,
        "superset_ownership: %s was present but NOT VALID (added by hand); "
        "%d %s and the constraint validated",
        name,
        count,
        repaired,
    )


def ensure_owner_fk_on(database_uri: str) -> bool:
    """``ensure_owner_fk`` outside a migration run. Called by migrate.upgrade;
    see the module docstring for why a recorded revision is not enough."""
    return _ensure_on(database_uri, ensure_owner_fk, "ownership_object", OWNER_FK)


def ensure_share_fk_on(database_uri: str) -> bool:
    """``ensure_share_fk`` outside a migration run: for a database that was
    stamped past 0003, or downgraded and re-upgraded by hand."""
    return _ensure_on(database_uri, ensure_share_fk, "ownership_share", SHARE_FK)


def _ensure_on(
    database_uri: str, ensure: Callable[[], bool], table: str, name: str
) -> bool:
    """Run one ``ensure_*`` in its own transaction on a fresh connection,
    holding the process lock, and survive losing a cross-process race.

    The advisory lock closes the race on PostgreSQL; the except branch is
    for the other dialects and for anything that slipped past it: a
    failed ADD whose name is present AND validated on re-inspection means
    another process added it first, which is the state wanted. Presence
    alone is not enough: ``ensure`` also runs ``VALIDATE CONSTRAINT`` over
    a constraint added ``NOT VALID`` by hand, and that constraint is
    present by name whether or not the VALIDATE succeeded (a failed repair,
    a role that does not own the table) -- such a failure is re-raised, not
    read as the lost race. A follower under the storm never gets here for
    that reason: it waits on the advisory lock and reads the leader's
    ``convalidated = t``."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with _DDL_LOCK:
        engine = sa.create_engine(database_uri)
        try:
            try:
                with engine.begin() as conn:
                    with Operations.context(MigrationContext.configure(conn)):
                        return ensure()
            except (ProgrammingError, IntegrityError, OperationalError) as exc:
                # engine.begin() has rolled the failed transaction back.
                with engine.connect() as conn:
                    wanted = _fk_present(conn, table, name) and _fk_validated(
                        conn, table, name
                    )
                if not wanted:
                    raise
                logger.info(
                    "superset_ownership: %s was added by another process while this "
                    "one was adding it (%s); nothing to do",
                    name,
                    exc.__class__.__name__,
                )
                return True
        finally:
            engine.dispose()

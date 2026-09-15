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
"""Alembic environment for the ownership module's PARALLEL, ISOLATED chain.

Three things make this chain coexist with Superset's in the same database
without ever touching it:

1. ``version_table="alembic_version_ownership"`` -- our bookkeeping lives in
   its own table. Superset's ``alembic_version`` is never read or written, so
   the two chains cannot collide and ``superset db upgrade`` never sees ours.
2. ``target_metadata = db.metadata`` -- the module's own MetaData (db.py), not
   Superset's ``Model.metadata``. Autogenerate only ever sees our tables.
3. ``include_object`` -- belt and braces: a reflected Superset table can never
   leak into a comparison, so no autogenerate can emit a DROP against core.

Cross-MetaData foreign keys are deliberately NOT declared on the SQLAlchemy
Table: a declarative ForeignKey to a table in another MetaData raises
NoReferencedTableError. The one this module keeps -- owner_user_id ->
ab_user.id, ON DELETE SET NULL -- is hand-written DDL in revision 0003
(constraints.py), and ``include_object`` skips FK comparison so autogenerate
does not fight that choice. object_id -> dashboards/slices is deliberately
absent (qa/reviews/decision-owner-fk.md). The intra-module share -> object
constraint IS declared in db.py; same MetaData, no gotcha.

BOOT-STORM SAFETY (issue #89). ``constraints.py``'s advisory lock (and the
re-check belt built on it) only ever serialises the revision-0003
constraint re-check -- it says nothing about the chain's own revision
TRANSITIONS. Several application processes booting at once against a
database that is behind head (0002 -> 0003, or from empty) used to race the
``alembic_version_ownership`` UPDATE: the loser's ``command.upgrade`` had
already computed its list of steps against a current-revision read taken
before any of them started, so by the time it tried to record the new
revision the winner had already moved the row and Alembic raised
``CommandError: Online migration expected to match one row...`` (0002 ->
0003) or, worse, two processes' ``CREATE TABLE`` collided outright (0001 ->
0002, ``UniqueViolation`` on ``pg_class``).

The fix is ``_serialise_chain_transitions`` below: on PostgreSQL, a fixed
``pg_advisory_xact_lock(CHAIN_LOCK_KEY)`` taken as the FIRST statement
inside the migration's own transaction, before ``context.run_migrations()``
ever reads the current revision. A process that has to wait blocks on the
lock itself -- not on a doomed attempt to run steps computed from a stale
read -- and when it acquires the lock the winner's transaction has already
committed, so the SELECT `run_migrations()` issues next sees the winner's
new revision under READ COMMITTED. Alembic then computes ZERO steps from
"already at head" to "head": this is Alembic's own no-op path, not a
re-apply of the winner's DDL and not a second UPDATE of the version row --
exactly the outcome a second-comer needs, with no special-casing here
beyond taking the lock early enough. This ordering guarantee needs the
lock to be transaction-scoped AND taken on the very connection the
follower's revision read runs on: a session-level lock (``pg_advisory_lock``)
acquired on a second connection would not put the follower's read after
the leader's commit in any particular order, only the shared ``xact``
variant on the one connection ``run_migrations()`` itself uses does that.

A follower waiting on ``CHAIN_LOCK_KEY`` waits for as long as the leader's
migration transaction is open, with no ``lock_timeout`` set on this
connection -- the same property the pre-existing ``constraints.py`` lock
already has, and not changed here: a modest, guessed timeout would abort a
follower behind a leader that is merely slow (0003 scanning `ab_user`, or
a large backlog of pending revisions) exactly as readily as it would abort
one behind a genuinely stuck leader, and this module has no way to tell
the two apart from the follower's side. An operator who needs a bound on
that wait can set ``lock_timeout`` on the role or the connection string;
should the leader die mid-transaction with the lock held, PostgreSQL
releases it at that connection's rollback the same way it would release
any other lock the aborted transaction took.

``CHAIN_LOCK_KEY`` uses the two-int4 form of the advisory lock
(``pg_advisory_xact_lock(int, int)``), a distinct Postgres key space from
both ``constraints.py``'s single-bigint ``hashtext(...)`` lock and
``service.py``'s per-object two-int4 locks (``ADVISORY_LOCK_CLASS``, first
key 1 for chart / 2 for dashboard, second key the object's id) -- so this
lock can never alias either. The first key here is 0, a value
``ADVISORY_LOCK_CLASS`` never assigns (it starts at 1 and is append-only),
which rules out a collision by construction regardless of the second key.

SQLite and every other non-PostgreSQL dialect take no lock: there is no
cross-process advisory-lock primitive to take, and the deployment
assumption for those dialects is a single process per database file (the
module docstring's ``migrate.py`` deploy contract). Within one process,
``constraints._DDL_LOCK`` already serialises every call into this chain
(``migrate.upgrade``/``downgrade``/``stamp`` all hold it), so threads
sharing one interpreter were -- and remain -- safe; only separate OS
processes are the boot-storm this fixes.

The whole guarantee above rests on ``CHAIN_LOCK_KEY`` staying held for the
ENTIRE run, not just its first statement: ``run_migrations_online`` passes
``transaction_per_migration=False`` to ``context.configure`` explicitly
(Alembic's own default, but pinned here rather than relied on) so every
revision in one ``upgrade`` call shares the one transaction the lock was
taken in, and no revision in ``versions/`` calls Alembic's
``op.get_context().autocommit_block()``: that context manager commits the
run's transaction early to run DDL outside it (the ``NOT VALID`` /
``VALIDATE CONSTRAINT`` idiom ``constraints.py`` discusses), which would
release ``CHAIN_LOCK_KEY`` before the run finishes and reopen the exact
interleaving this lock exists to close. A future revision that needs that
idiom must not reach for ``autocommit_block()`` without first moving the
lock to a session-level ``pg_advisory_lock``/``pg_advisory_unlock`` pair
held across the whole run.

``migrate.upgrade``'s retry (migrate.py, same issue) is the backstop for
what this lock does not reach: a dialect with no chain lock (SQLite). It
only retries an exception that matches the specific race shape it
recognises -- Alembic's own version-row update matching zero rows, or DDL
colliding on one of the chain's own tables, or an ``OperationalError``
carrying SQLite's own "database is locked"/"already exists" text
(``migrate._looks_like_chain_transition_race``) -- and logs, then
re-raises unretried, anything else, a real PostgreSQL lock-wait timeout
included: an operator who sets ``lock_timeout`` per the paragraph above
gets an unretried ``OperationalError`` on the follower once that timeout
elapses, not a second wait behind the same leader.
"""
from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool, text
from sqlalchemy.engine import Connection

from superset_ownership import db as ownership_db

config = context.config

# Two-int4 pg_advisory_xact_lock key for this chain's own revision
# transitions. Fixed and never reused for anything else -- see the "BOOT-
# STORM SAFETY" section above for why 0 as the first key can never alias
# service.py's per-object locks.
CHAIN_LOCK_KEY: tuple[int, int] = (0, 89)

# Logging is the caller's, never this file's. Every run of this chain is
# in-process -- migrate.py, and through it the startup hook, the backfill and
# the `superset ownership db` commands -- inside a process whose logging is
# already configured (Superset's, or pytest's). The usual
# `logging.config.fileConfig(config.config_file_name)` line is deliberately
# absent: it resets the root logger's level and replaces its handlers on
# every call, whatever `disable_existing_loggers` says, which would drop
# every INFO line the application logs after the chain has run and discard
# the rotating file handler with it. There is no CLI path to configure
# logging for either: alembic.ini carries no `sqlalchemy.url` (migrate.py
# injects it from Superset's config so no DSN is ever committed), so the raw
# `alembic upgrade`/`downgrade` from this directory cannot run online --
# `engine_from_config` fails on the missing url; `alembic heads`/`history`
# work but do not execute this file.

target_metadata = ownership_db.metadata

VERSION_TABLE = "alembic_version_ownership"


def include_object(obj, name, type_, reflected, compare_to):  # type: ignore[no-untyped-def]
    """Restrict autogenerate to tables this module owns."""
    if type_ == "table":
        if reflected and name not in target_metadata.tables:
            return False  # Superset's table. Never touch it.
        return name in target_metadata.tables
    if type_ == "foreign_key_constraint":
        # No foreign key is compared: the cross-MetaData one (owner ->
        # ab_user) is hand-written DDL, and the declared intra-module one
        # (share -> object) is created by revision 0003, not autogenerate.
        return False
    return True


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        version_table=VERSION_TABLE,
        include_object=include_object,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _serialise_chain_transitions(connection: Connection) -> None:
    """Boot-storm safety for the chain's OWN revision transitions (issue
    #89): PostgreSQL only, a fixed ``pg_advisory_xact_lock(CHAIN_LOCK_KEY)``
    taken before Alembic reads the current revision. Must be called as the
    first statement inside the migration's transaction -- see the module
    docstring's "BOOT-STORM SAFETY" section for why that ordering is what
    makes a second-comer's read see the first process's commit and take
    Alembic's own already-at-head path instead of racing the version row.
    A no-op on every other dialect: no cross-process primitive exists there,
    and the deploy contract is one process per database file."""
    if connection.dialect.name != "postgresql":
        return
    connection.execute(
        text("SELECT pg_advisory_xact_lock(:a, :b)"),
        {"a": CHAIN_LOCK_KEY[0], "b": CHAIN_LOCK_KEY[1]},
    )


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table=VERSION_TABLE,
            include_object=include_object,
            # Alembic's own default; pinned explicitly because the boot-
            # storm lock's whole-run guarantee (module docstring, "BOOT-
            # STORM SAFETY") depends on every revision sharing the one
            # transaction CHAIN_LOCK_KEY was taken in.
            transaction_per_migration=False,
        )
        with context.begin_transaction():
            _serialise_chain_transitions(connection)
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

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
"""Run the ownership module's parallel Alembic chain.

Drives Alembic's Python API directly rather than registering a second
Flask-Migrate instance. Superset hardcodes ``migrate.init_app(app, db=db,
directory=APP_DIR + "/migrations")`` and its ``alembic.ini`` has no
``version_locations``, so there is no supported way to graft a branch onto
Superset's chain from a pip package -- and no need to. This module owns its
own ``alembic.ini``/``env.py``/``versions/`` and its own version table
(``alembic_version_ownership``, see migrations/env.py), so it is a wholly
separate chain against the same database. Zero edits to Superset.

Deploy contract (SOW §4 / spec §5): run ``superset ownership db upgrade``
immediately AFTER ``superset db upgrade``. Idempotent; safe on every deploy.

``upgrade`` ends with a re-check of revision 0003's two foreign keys
(``_ensure_constraints``). On the deployed stack that is a no-op -- 0003
creates both itself -- and it exists for the databases where 0003 was
recorded with one of them skipped (constraints.py has the cases).

BOOT STORM (issue #89). Several processes call ``upgrade`` at once on every
deploy (web workers, the Celery worker, ``superset init``, each running
this chain's ``FLASK_APP_MUTATOR`` hook). The chain's own revision
transitions -- as opposed to revision 0003's constraint re-check, which
constraints.py already serialised -- were not safe under that: two
processes racing the ``alembic_version_ownership`` UPDATE, or racing DDL
outright on an empty database. migrations/env.py's ``run_migrations_online``
now takes a fixed ``pg_advisory_xact_lock`` for the whole run on PostgreSQL,
so concurrent boots serialise there and a second-comer finds the chain
already at the target revision (Alembic's own no-op, not a re-apply). The
``except`` below is the backstop for what that lock does not reach: a
dialect with no chain lock (SQLite). It retries once, but only when the
exception it caught actually matches the race shape this backstop exists
for -- Alembic's own version-row update matching zero rows, or DDL
colliding on one of the chain's own tables (see
``_looks_like_chain_transition_race``). Anything else -- a bad revision
name, an unreachable database, a genuine unrelated failure, OR a real
lock-wait timeout on PostgreSQL (reachable only if an operator sets
``lock_timeout``; see migrations/env.py's own docstring) -- is logged at
WARNING with its exception class and text and re-raised without a retry:
this module never claims a boot raced a concurrent upgrader without having
checked the shape of the failure first, and it does not retry a PostgreSQL
lock-wait timeout even though it, too, surfaces as ``OperationalError`` --
that text does not match SQLite's own "database is locked"/"already
exists" wording ``_looks_like_chain_transition_race`` looks for.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")
VERSION_TABLE = "alembic_version_ownership"


def _superset_uri() -> str:
    """Exactly the DSN Superset uses, so both chains provably target one DB."""
    from flask import current_app

    return str(current_app.config["SQLALCHEMY_DATABASE_URI"])


def _config(database_uri: Optional[str] = None):
    """An Alembic Config pointed at this module's chain and the target DB."""
    from alembic.config import Config

    cfg = Config(os.path.join(MIGRATIONS_DIR, "alembic.ini"))
    cfg.set_main_option("script_location", MIGRATIONS_DIR)
    uri = database_uri if database_uri is not None else _superset_uri()
    # ConfigParser treats '%' as interpolation; escape it (passwords may contain it).
    cfg.set_main_option("sqlalchemy.url", uri.replace("%", "%%"))
    # Logging is left exactly as the caller configured it: every caller of
    # this module runs inside a process whose logging is already set up --
    # Superset's (the startup hook, the backfill, the `superset ownership db`
    # commands) or pytest's -- and env.py never calls
    # `logging.config.fileConfig` (see the note there), so no attribute is
    # needed to switch it off.
    return cfg


# Tables this chain's own DDL collision can name -- the version table
# Alembic bookkeeps in, plus every table a from-empty upgrade creates.
# Used by `_looks_like_chain_transition_race` to recognise the P15 shape
# (two processes' CREATE TABLE racing outright) without guessing at driver-
# specific message text beyond the table name itself.
_CHAIN_TABLE_NAMES = (
    VERSION_TABLE,
    "ownership_object",
    "ownership_share",
    "ownership_outbox",
)


def _looks_like_chain_transition_race(exc: Exception) -> bool:
    """Does `exc` match the boot-storm race ``upgrade``'s retry exists to
    recover from (issue #89) -- two processes computing their list of steps
    against a stale current-revision read and then both trying to apply
    it? By the time this process gets a second try, migrations/env.py's
    chain-level advisory lock means the other side has already committed
    or rolled back, so a retry is safe. Two shapes are recognised, both
    confirmed against the actual exception rather than assumed:

    - Alembic's own version-row UPDATE matching zero rows because a
      second-comer already moved the chain past this revision (review
      P14) -- a ``CommandError`` carrying this exact substring.
    - DDL for one of this chain's own tables colliding outright on an
      empty database (review P15) -- an ``IntegrityError``/
      ``ProgrammingError`` naming one of ``_CHAIN_TABLE_NAMES``, or an
      ``OperationalError`` carrying SQLite's own "database is locked" or
      "already exists" text. Reachable only on a dialect with no
      chain-level lock (SQLite; this module's documented single-process
      deploy contract).

    Deliberately NOT recognised: a genuine PostgreSQL lock-wait timeout
    (``OperationalError: canceling statement due to lock timeout``,
    reachable only if an operator sets ``lock_timeout`` -- see
    migrations/env.py). It is also an ``OperationalError``, but its text
    does not match SQLite's wording above, so it falls through to "anything
    else" below and is never retried; a retry would only wait out another
    ``lock_timeout`` behind the same leader.

    Anything else -- a bad revision name, an unreachable database, a
    deadlock unrelated to this chain, a real lock-wait timeout -- is not
    this race, and the caller re-raises it instead of retrying."""
    from alembic.util.exc import CommandError
    from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError

    text = str(exc)
    if isinstance(exc, CommandError):
        return "expected to match one row" in text
    if isinstance(exc, (IntegrityError, ProgrammingError)):
        return any(name in text for name in _CHAIN_TABLE_NAMES)
    if isinstance(exc, OperationalError):
        return "database is locked" in text or "already exists" in text
    return False


def upgrade(revision: str = "head", database_uri: Optional[str] = None) -> None:
    from alembic import command
    from alembic.util.exc import CommandError
    from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError

    from superset_ownership import constraints

    cfg = _config(database_uri)
    # One DDL run per process at a time: the operations context both the
    # chain and the re-check use is a process-global proxy (constraints.py).
    with constraints._DDL_LOCK:
        try:
            command.upgrade(cfg, revision)
        except (
            CommandError,
            OperationalError,
            IntegrityError,
            ProgrammingError,
        ) as exc:
            # Boot-storm belt (issue #89): migrations/env.py's chain-level
            # advisory lock (PostgreSQL) is what actually prevents this --
            # it serialises concurrent boots so a second-comer's read of
            # the current revision happens after the first's commit, and
            # Alembic's own upgrade then computes zero steps. This catch is
            # the backstop for what that lock does not reach: a dialect
            # with no chain lock (SQLite; the module docstring's
            # single-process assumption). It only retries when the
            # exception actually matches that race shape
            # (`_looks_like_chain_transition_race`) -- never on the
            # unchecked assumption that any failure here must be one, and
            # NOT for a genuine PostgreSQL lock-wait timeout: that also
            # surfaces as `OperationalError`, but does not match the race
            # shape and is re-raised unretried (see that function's
            # docstring and migrations/env.py's own).
            if _looks_like_chain_transition_race(exc):
                logger.warning(
                    "superset_ownership: upgrade to %s raced a concurrent "
                    "upgrader (%s: %s); retrying once now that it has "
                    "finished",
                    revision,
                    exc.__class__.__name__,
                    exc,
                )
                command.upgrade(cfg, revision)
            else:
                # A real defect, not a resolved race: a bad revision name,
                # an unreachable database, a deadlock unrelated to this
                # chain. Logged at WARNING with the exception's own class
                # and text -- not claimed as a race -- and re-raised
                # unretried.
                logger.warning(
                    "superset_ownership: upgrade to %s failed (%s: %s); "
                    "not retrying -- does not match the chain-transition "
                    "race shape",
                    revision,
                    exc.__class__.__name__,
                    exc,
                )
                raise
        logger.info("superset_ownership: chain upgraded to %s", revision)
        _ensure_constraints(cfg)


def _ensure_constraints(cfg) -> None:  # type: ignore[no-untyped-def]
    """Re-check revision 0003's two constraints after every run on a database
    at or beyond it.

    On the deployed stack 0003 creates both itself (FAB creates ab_user
    before FLASK_APP_MUTATOR runs this chain -- and before the body of any
    `superset ownership db` command, so no order of the two CLI commands
    reaches the deferral), and this is a no-op that costs two inspections.
    It is the fallback for the cases where 0003 was recorded without one
    of them: ab_user absent when it ran (reachable only outside the CLI: a
    direct `migrate.upgrade(uri)` from an embedding or a custom entrypoint
    that creates no application first, or FAB_CREATE_DB=False, under which
    Superset's own upgrade does not complete either; constraints.py), or a
    `stamp` past 0003 -- Alembic never re-runs a recorded revision, so
    without this the constraint would be skipped once and missing for
    good. It also finishes a constraint an operator added `NOT VALID` by
    hand (repair, then VALIDATE CONSTRAINT). Idempotent, serialised across
    processes (constraints.py), and only on a database at or beyond 0003,
    so a deliberate downgrade to 0002 stays unconstrained."""
    from superset_ownership import constraints

    uri = cfg.get_main_option("sqlalchemy.url").replace("%%", "%")
    at = current(uri)
    if at is None or not lineage_includes(constraints.FK_REVISION, at):
        return
    constraints.ensure_owner_fk_on(uri)
    constraints.ensure_share_fk_on(uri)


def lineage_includes(revision: str, at: str) -> bool:
    """Is `revision` `at` itself or one of its ancestors on this chain?"""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(_config("sqlite://"))
    return revision in {r.revision for r in script.iterate_revisions(at, "base")}


def downgrade(revision: str, database_uri: Optional[str] = None) -> None:
    from alembic import command

    from superset_ownership import constraints

    with constraints._DDL_LOCK:
        command.downgrade(_config(database_uri), revision)
    logger.info("superset_ownership: chain downgraded to %s", revision)


def forget(bind=None, database_uri: Optional[str] = None) -> bool:  # type: ignore[no-untyped-def]
    """Drop the chain's own version table, so the next `upgrade` runs the
    chain from the start. `lifecycle.teardown` calls this after dropping
    the module's tables: with the tables gone and the version row still at
    head, `upgrade` was a no-op and the uninstalled feature could not be
    installed again. Returns whether there was a table to drop. `bind` (an
    Engine, or a Connection whose transaction the caller owns) wins over
    `database_uri`."""
    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.engine import Connection

    engine = bind
    if engine is None:
        engine = create_engine(database_uri or _superset_uri())
    if not inspect(engine).has_table(VERSION_TABLE):
        return False
    statement = text(f"DROP TABLE IF EXISTS {VERSION_TABLE}")
    if isinstance(engine, Connection):
        # Inside the caller's transaction (SQLAlchemy 2 autobegins; a
        # nested `begin()` would raise): they commit.
        engine.execute(statement)
    else:
        with engine.begin() as conn:
            conn.execute(statement)
    logger.info("superset_ownership: chain forgotten (%s dropped)", VERSION_TABLE)
    return True


class StampRefusedError(RuntimeError):
    """`stamp` would record a revision whose constraints are not in the
    database; `upgrade` is the command that adds them."""


def stamp(revision: str = "head", database_uri: Optional[str] = None) -> None:
    """Record ``revision`` without running it. ``upgrade`` is idempotent so this
    is rarely needed; kept for operators wanting an explicit adoption step.

    Refused when it would record 0003 (or later) on a database that has the
    ownership tables but not both of 0003's constraints: a stamp runs no
    DDL, the recorded revision is never re-run, and the constraints would
    be missing until the next ``upgrade`` re-checked them (the owner one)
    or for good (the share one, before the re-check covered it). On a
    database without the tables -- the adoption case the command exists
    for -- nothing is checked."""
    from alembic import command
    from sqlalchemy import create_engine, inspect

    from superset_ownership import constraints

    cfg = _config(database_uri)
    uri = cfg.get_main_option("sqlalchemy.url").replace("%%", "%")
    target = heads()[0] if revision == "head" else revision
    if lineage_includes(constraints.FK_REVISION, target):
        engine = create_engine(uri)
        try:
            with engine.connect() as conn:
                insp = inspect(conn)
                has_tables = insp.has_table("ownership_object") and insp.has_table(
                    "ownership_share"
                )
                missing = constraints.missing(conn) if has_tables else []
        finally:
            engine.dispose()
        if missing:
            raise StampRefusedError(
                f"refusing to stamp {revision}: {', '.join(missing)} missing from the "
                "database; a stamp runs no DDL and a recorded revision is never "
                "re-run. Run `superset ownership db upgrade` instead."
            )
    with constraints._DDL_LOCK:
        command.stamp(cfg, revision)


def current(database_uri: Optional[str] = None) -> Optional[str]:
    """The revision this database is at, or None if the chain has never run."""
    from sqlalchemy import create_engine

    uri = database_uri if database_uri is not None else _superset_uri()
    engine = create_engine(uri)
    try:
        return current_on(engine)
    finally:
        engine.dispose()


def current_on(bind) -> Optional[str]:  # type: ignore[no-untyped-def]
    """`current` on an Engine or Connection the caller already holds."""
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy.engine import Connection

    def read(conn: Connection) -> Optional[str]:
        ctx = MigrationContext.configure(conn, opts={"version_table": VERSION_TABLE})
        return ctx.get_current_revision()

    if isinstance(bind, Connection):
        return read(bind)
    with bind.connect() as conn:
        return read(conn)


def heads() -> list[str]:
    from alembic.script import ScriptDirectory

    return list(ScriptDirectory.from_config(_config("sqlite://")).get_heads())

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
"""Assurance tests for the ownership module's parallel Alembic chain.

The SOW promises a migration chain that "runs alongside -- and never modifies --
Superset's own schema". These tests are the evidence behind that sentence. Each
one builds a throwaway SQLite database, runs the real chain against it, and
checks a property that would be expensive to discover in production.

The properties, in the order a deploy would hit them:

  1. A fresh database gets the complete schema.
  2. Running it again changes nothing (safe to call on every deploy).
  3. A prototype-era database -- tables made by create_all(), no indexes -- is
     adopted in place: data kept, missing indexes added, nothing recreated.
  4. Superset's own tables, INCLUDING its alembic_version, are byte-for-byte
     untouched by upgrade and by downgrade. This is the isolation guarantee.
  5. Downgrade removes only what we own, and re-upgrade works afterwards.
  6. The chain has exactly one head (no accidental branch).
  7. A '%' in the database password does not break the Alembic config.
  8. Running the chain in-process leaves the process's logging exactly as it
     found it: the module's own loggers enabled, the root logger's level and
     handlers untouched, a logger deliberately disabled before still
     disabled, and an INFO line logged afterwards still reaching a handler
     attached before. The startup hook and the backfill both migrate first
     and log after.
  9. Revision 0003's two foreign keys (qa/reviews/decision-owner-fk.md):
     owner -> ab_user ON DELETE SET NULL and share -> object ON DELETE
     CASCADE. Present after upgrade, absent after downgrade, rows that
     would violate them repaired first (and the counts logged once the
     constraint is in), the owner constraint created DIRECTLY by 0003 when
     ab_user exists (the deployed order: FAB creates ab_user before the
     mutator runs this chain) and deferred -- not lost -- when it does not,
     the pure-Alembic fallback producing the same constraint as Superset's
     helper, and, with SQLite told to enforce (PRAGMA foreign_keys=ON),
     deleting a user un-owning their objects and deleting an object row
     taking its shares with it. The object side has no foreign key, and a
     test pins that as the decision it is.
 10. The re-check that adds a skipped constraint later is safe to run from
     every booting process at once (a lost race is success, not a crash;
     one process runs one DDL at a time), covers both constraints, `stamp`
     refuses to record 0003 over a database missing them, a constraint
     added NOT VALID by hand is repaired and validated by the next upgrade,
     and `check_consistency` reports a missing or unvalidated one until
     then. The PostgreSQL tests run only against a database whose name
     says it is a throwaway.
 10b. The chain's own revision TRANSITIONS -- not just 0003's constraint
     re-check -- are boot-storm safe (issue #89): three real OS processes
     upgrading at once, from 0002 and from empty, all succeed, the DDL
     runs exactly once, and the chain lands at head; migrations/env.py's
     advisory lock is what does it, `migrate.upgrade`'s retry-once is the
     backstop, and the SQLite path (no chain lock; single process assumed)
     is unaffected by either.
 11. A hard-deleted user's objects are un-owned by the database with no
     trace; the guard's user-delete listener leaves one audit event per
     object, after the delete commits, and nothing for a rolled-back one.
     With the ownership tables absent it issues no statement against them
     and the delete goes through (on PostgreSQL a failed SELECT would have
     aborted it).
 12. The share insert under the new foreign key: losing the unique race
     still updates the role; the object row having vanished is refused
     (ShareTargetGoneError), not reported as written with a tuple queued;
     an IntegrityError that is neither is re-raised, not called "gone".
"""

from __future__ import annotations

import logging

import sqlalchemy as sa
from superset_ownership import db as ownership_db, migrate

OUR_TABLES = {"ownership_object", "ownership_share", "ownership_outbox"}
OUR_VERSION_TABLE = "alembic_version_ownership"
ROOT_REVISION = "0001_object_ownership"
HEAD_REVISION = "0004_ownership_object_tenant"
# What an install from BEFORE the chain existed actually had.
PROTOTYPE_TABLES = {"ownership_object", "ownership_share"}

EXPECTED_INDEXES = {
    "ownership_object": {"ix_ownership_object_visibility", "ix_ownership_object_owner"},
    "ownership_share": {"ix_ownership_share_object"},
    "ownership_outbox": {"ix_ownership_outbox_status_id", "ix_ownership_outbox_object"},
}
OWNER_FK = "fk_ownership_object_owner_user_id_ab_user"
SHARE_FK = "fk_ownership_share_object_ownership_object"


# --------------------------------------------------------------------------- helpers


def _uri(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'chain.db'}"


def _engine(uri: str):
    return sa.create_engine(uri)


def _tables(uri: str) -> set[str]:
    return set(sa.inspect(_engine(uri)).get_table_names())


def _indexes(uri: str, table: str) -> set[str]:
    return {ix["name"] for ix in sa.inspect(_engine(uri)).get_indexes(table)}


def _count(uri: str, table: str) -> int:
    with _engine(uri).connect() as conn:
        return conn.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one()


def _rows(uri: str, table: str) -> list[tuple]:
    with _engine(uri).connect() as conn:
        return [
            tuple(r) for r in conn.execute(sa.text(f"SELECT * FROM {table} ORDER BY 1"))
        ]


def _same_data(before: dict, after: dict) -> bool:
    """Row-for-row equality across a revision that adds or drops a NULL-able
    trailing column (0004's tenant_guid): the columns both sides have must
    match exactly, and any column only one side has must be NULL on every
    row -- so data is neither lost nor invented."""
    if before.keys() != after.keys():
        return False
    for table in before:
        b, a = before[table], after[table]
        if len(b) != len(a):
            return False
        for rb, ra in zip(b, a, strict=True):
            n = min(len(rb), len(ra))
            if rb[:n] != ra[:n] or any(v is not None for v in rb[n:] + ra[n:]):
                return False
    return True


def _columns(uri: str, table: str) -> list[tuple[str, str]]:
    return [
        (c["name"], str(c["type"])) for c in sa.inspect(_engine(uri)).get_columns(table)
    ]


def _fks(uri: str, table: str) -> dict[str, dict]:
    return {fk["name"]: fk for fk in sa.inspect(_engine(uri)).get_foreign_keys(table)}


def _enforcing(uri: str):
    """A connection on which SQLite enforces foreign keys. Off by default and
    per connection, which is why the assertions that need it ask for it."""
    conn = _engine(uri).connect()
    conn.execute(sa.text("PRAGMA foreign_keys=ON"))
    return conn


# Exactly the DDL ``metadata.create_all()`` emitted before the chain existed:
# the two original tables, their unique constraints, no indexes, no foreign
# keys (db.py declares the share -> object one, so create_all() would no
# longer reproduce the prototype), and no outbox.
PROTOTYPE_DDL = (
    "CREATE TABLE ownership_object ("
    "id INTEGER NOT NULL, asset_type VARCHAR(32) NOT NULL, object_id INTEGER NOT NULL, "
    "object_uuid VARCHAR(64), owner_user_id INTEGER, "
    "visibility VARCHAR(16) DEFAULT 'private' NOT NULL, PRIMARY KEY (id), "
    "CONSTRAINT uq_ownership_object_type_id UNIQUE (asset_type, object_id))",
    "CREATE TABLE ownership_share ("
    "id INTEGER NOT NULL, asset_type VARCHAR(32) NOT NULL, object_id INTEGER NOT NULL, "
    "subject VARCHAR(128) NOT NULL, role VARCHAR(32) NOT NULL, PRIMARY KEY (id), "
    "CONSTRAINT uq_ownership_share_subject UNIQUE (asset_type, object_id, subject))",
)


def _make_prototype_era_db(uri: str) -> None:
    """Reproduce a database the OLD code created.

    Before the chain existed, schema came from ``metadata.create_all()``:
    the two original tables present, none of the ix_* indexes, no foreign
    keys, and no outbox (it did not exist yet).
    """
    with _engine(uri).begin() as conn:
        for ddl in PROTOTYPE_DDL:
            conn.execute(sa.text(ddl))


def _make_fake_superset_world(uri: str) -> None:
    """Stand in for Superset's side of a shared database.

    A populated ``alembic_version`` (Superset's chain bookkeeping) and a
    populated core table. If our chain ever touches either, the isolation
    guarantee is broken. These are what test 4 snapshots.
    """
    with _engine(uri).begin() as conn:
        conn.execute(
            sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        conn.execute(sa.text("INSERT INTO alembic_version VALUES ('39097d124752')"))
        conn.execute(
            sa.text(
                "CREATE TABLE dashboards (id INTEGER PRIMARY KEY, dashboard_title VARCHAR(500))"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO dashboards VALUES (1, 'Video Game Sales'), (2, 'Slack')"
            )
        )
        # The one core table revision 0003 references (owner_user_id -> ab_user.id).
        conn.execute(
            sa.text(
                "CREATE TABLE ab_user (id INTEGER PRIMARY KEY, username VARCHAR(64))"
            )
        )
        conn.execute(
            sa.text("INSERT INTO ab_user VALUES (1, 'admin'), (11, 'ben'), (12, 'ada')")
        )


def _snapshot_superset_world(uri: str) -> dict:
    return {
        "alembic_version_rows": _rows(uri, "alembic_version"),
        "alembic_version_cols": _columns(uri, "alembic_version"),
        "dashboards_rows": _rows(uri, "dashboards"),
        "dashboards_cols": _columns(uri, "dashboards"),
        "ab_user_rows": _rows(uri, "ab_user"),
        "ab_user_cols": _columns(uri, "ab_user"),
    }


# --------------------------------------------------------------------------- 1. fresh


def test_fresh_database_gets_complete_schema(tmp_path):
    uri = _uri(tmp_path)
    assert migrate.current(uri) is None, "a fresh db has never seen the chain"

    migrate.upgrade("head", uri)

    assert migrate.current(uri) == HEAD_REVISION
    assert OUR_TABLES <= _tables(uri)
    assert OUR_VERSION_TABLE in _tables(uri)
    for table, expected in EXPECTED_INDEXES.items():
        assert expected <= _indexes(uri, table), f"{table} missing indexes"


def test_fresh_schema_matches_module_metadata(tmp_path):
    """The chain must produce the same columns the module's MetaData declares,
    or code reading via db.metadata and DDL written by the chain drift apart."""
    uri = _uri(tmp_path)
    migrate.upgrade("head", uri)
    for table in OUR_TABLES:
        declared = [c.name for c in ownership_db.metadata.tables[table].columns]
        actual = [name for name, _ in _columns(uri, table)]
        assert actual == declared, (
            f"{table}: chain columns {actual} != metadata {declared}"
        )


def test_forget_lets_the_chain_run_again_after_a_teardown(tmp_path):
    """`lifecycle.teardown` drops the module's tables and then calls
    `migrate.forget`: without it the version row stayed at head, `upgrade`
    was a no-op and an uninstalled feature could not be installed again."""
    from sqlalchemy import create_engine

    uri = _uri(tmp_path)
    migrate.upgrade("head", uri)
    ownership_db.metadata.drop_all(bind=create_engine(uri), checkfirst=True)
    assert migrate.current(uri) == HEAD_REVISION, "the stale state teardown left"

    assert migrate.forget(database_uri=uri) is True
    assert migrate.current(uri) is None
    assert migrate.forget(database_uri=uri) is False

    # A Connection bind, inside the caller's own transaction (review nit).
    migrate.upgrade("head", uri)
    engine = create_engine(uri)
    with engine.begin() as conn:
        assert migrate.forget(bind=conn) is True
    assert migrate.current(uri) is None

    migrate.upgrade("head", uri)
    assert migrate.current(uri) == HEAD_REVISION
    assert OUR_TABLES <= _tables(uri)


# --------------------------------------------------------------------------- 2. idempotent


def test_upgrade_is_idempotent(tmp_path):
    uri = _uri(tmp_path)
    _make_fake_superset_world(uri)
    migrate.upgrade("head", uri)
    before = {t: (_columns(uri, t), _indexes(uri, t), _fks(uri, t)) for t in OUR_TABLES}
    assert OWNER_FK in before["ownership_object"][2]
    assert SHARE_FK in before["ownership_share"][2]

    migrate.upgrade("head", uri)  # a deploy pipeline calls this unconditionally
    migrate.upgrade("head", uri)

    assert migrate.current(uri) == HEAD_REVISION
    after = {t: (_columns(uri, t), _indexes(uri, t), _fks(uri, t)) for t in OUR_TABLES}
    assert after == before


# --------------------------------------------------------------------------- 3. adoption


def test_adopts_prototype_era_database_without_losing_data(tmp_path):
    """The case every existing install hits: tables exist from create_all(),
    rows exist, indexes do not. Upgrade must adopt, not recreate."""
    uri = _uri(tmp_path)
    _make_prototype_era_db(uri)
    with _engine(uri).begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO ownership_object (asset_type, object_id, object_uuid, owner_user_id, visibility) "
                "VALUES ('chart', 56, 'u-56', 11, 'private'), ('dashboard', 5, 'u-5', 1, 'public')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO ownership_share (asset_type, object_id, subject, role) "
                "VALUES ('chart', 56, 'user:ben', 'viewer')"
            )
        )
    # Precondition: this really is the prototype state.
    assert migrate.current(uri) is None
    assert "ownership_outbox" not in _tables(uri), (
        "precondition: prototype db predates the outbox"
    )
    for table in PROTOTYPE_TABLES:
        assert not (EXPECTED_INDEXES[table] & _indexes(uri, table)), (
            "precondition: prototype db has no ix_ indexes"
        )
        assert not _fks(uri, table), "precondition: prototype db has no foreign keys"
    rows_before = {t: _rows(uri, t) for t in PROTOTYPE_TABLES}

    migrate.upgrade("head", uri)

    assert migrate.current(uri) == HEAD_REVISION, "adopted install is now tracked"
    assert _same_data(rows_before, {t: _rows(uri, t) for t in PROTOTYPE_TABLES}), (
        "adoption must not touch data"
    )
    assert "ownership_outbox" in _tables(uri), (
        "adoption also brings the new outbox table"
    )
    for table, expected in EXPECTED_INDEXES.items():
        assert expected <= _indexes(uri, table), (
            f"adoption must add the missing {table} indexes"
        )
    assert SHARE_FK in _fks(uri, "ownership_share"), (
        "adoption also brings the share -> object constraint"
    )


def test_adoption_is_idempotent_too(tmp_path):
    uri = _uri(tmp_path)
    _make_prototype_era_db(uri)
    migrate.upgrade("head", uri)
    snap = {t: (_rows(uri, t), _indexes(uri, t)) for t in OUR_TABLES}
    migrate.upgrade("head", uri)
    assert {t: (_rows(uri, t), _indexes(uri, t)) for t in OUR_TABLES} == snap


# --------------------------------------------------------------------------- 4. isolation


def test_never_touches_superset_tables_on_upgrade(tmp_path):
    """THE guarantee. Superset's alembic_version and a core table sit in the
    same database; our upgrade must leave both byte-for-byte identical."""
    uri = _uri(tmp_path)
    _make_fake_superset_world(uri)
    before = _snapshot_superset_world(uri)

    migrate.upgrade("head", uri)

    assert _snapshot_superset_world(uri) == before
    assert _rows(uri, "alembic_version") == [("39097d124752",)], (
        "our chain must never write Superset's alembic_version"
    )
    assert migrate.current(uri) == HEAD_REVISION, "ours is tracked separately"


def test_never_touches_superset_tables_on_downgrade(tmp_path):
    uri = _uri(tmp_path)
    _make_fake_superset_world(uri)
    migrate.upgrade("head", uri)
    before = _snapshot_superset_world(uri)

    migrate.downgrade("base", uri)

    assert _snapshot_superset_world(uri) == before
    assert "dashboards" in _tables(uri), "downgrade must not drop core tables"
    assert "alembic_version" in _tables(uri), (
        "downgrade must not drop Superset's version table"
    )


def test_uses_its_own_version_table_not_supersets(tmp_path):
    """Two chains, two bookkeeping tables. If they shared one, `superset db
    upgrade` would see our revision and fail, and vice versa."""
    uri = _uri(tmp_path)
    migrate.upgrade("head", uri)
    tables = _tables(uri)
    assert OUR_VERSION_TABLE in tables
    assert "alembic_version" not in tables, (
        "a fresh upgrade must not create Superset's alembic_version table"
    )


# --------------------------------------------------------------------------- 5. downgrade


def test_downgrade_base_removes_only_our_tables(tmp_path):
    uri = _uri(tmp_path)
    _make_fake_superset_world(uri)
    migrate.upgrade("head", uri)
    assert OUR_TABLES <= _tables(uri)

    migrate.downgrade("base", uri)

    assert not (OUR_TABLES & _tables(uri)), "our tables gone"
    assert migrate.current(uri) is None, "chain rewound to base"
    assert {"alembic_version", "dashboards"} <= _tables(uri), "core untouched"


def test_reupgrade_after_downgrade(tmp_path):
    uri = _uri(tmp_path)
    migrate.upgrade("head", uri)
    migrate.downgrade("base", uri)
    migrate.upgrade("head", uri)
    assert migrate.current(uri) == HEAD_REVISION
    assert OUR_TABLES <= _tables(uri)


def test_stamp_records_without_running(tmp_path):
    """Operators adopting an install by hand: stamp marks the revision but
    creates nothing."""
    uri = _uri(tmp_path)
    migrate.stamp("head", uri)
    assert migrate.current(uri) == HEAD_REVISION
    assert not (OUR_TABLES & _tables(uri)), "stamp must not create tables"


# --------------------------------------------------------------------------- 6. shape


def test_chain_has_exactly_one_head():
    """Multiple heads mean someone added a revision without chaining it; an
    upgrade to 'head' would then be ambiguous and Alembic refuses to run."""
    heads = migrate.heads()
    assert heads == [HEAD_REVISION], f"expected a single head, got {heads}"


def test_root_revision_is_an_independent_root():
    """down_revision must be None: a fresh root, not a graft onto Superset's
    chain. Grafting is exactly what would make `superset db upgrade` run ours."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(migrate._config("sqlite://"))
    rev = script.get_revision(ROOT_REVISION)
    assert rev.down_revision is None
    assert rev.branch_labels in (None, set(), frozenset())


# --------------------------------------------------------------------------- 7. config


def test_percent_in_password_survives_config_roundtrip():
    """ConfigParser treats '%' as interpolation. A password like 'p%40ss' must
    reach Alembic intact, or the very first deploy against such a DB dies with
    an InterpolationSyntaxError."""
    uri = "postgresql://user:p%40ss%25word@db.example/superset"
    cfg = migrate._config(uri)
    assert cfg.get_main_option("sqlalchemy.url") == uri


def test_config_points_at_this_modules_chain():
    cfg = migrate._config("sqlite://")
    assert cfg.get_main_option("script_location") == migrate.MIGRATIONS_DIR


# --------------------------------------------------------------------------- 8. loggers


def test_upgrade_leaves_the_modules_loggers_enabled(tmp_path):
    """Alembic's logging setup disables every pre-existing logger unless told
    otherwise. `create_tables()` runs the chain at startup and at the top of
    the backfill; if it silenced `superset_ownership.*`, every refusal,
    skipped-candidate and verdict line logged afterwards would be dropped."""
    loggers = [
        logging.getLogger(name)
        for name in (
            "superset_ownership.api",
            "superset_ownership.backfill",
            "superset_ownership.transfer",
            "superset_ownership.hooks",
        )
    ]
    for lg in loggers:
        lg.disabled = False
    migrate.upgrade("head", f"sqlite:///{tmp_path / 'loggers.db'}")
    assert [lg.disabled for lg in loggers] == [False] * len(loggers)


def test_upgrade_leaves_the_root_logger_and_its_handlers_untouched(tmp_path):
    """The customary `logging.config.fileConfig(config.config_file_name)`
    in an Alembic env.py resets the root logger's level and replaces its
    handlers on every call, whatever `disable_existing_loggers` says. The
    application configures logging before the chain runs (Superset sets the
    root level and installs its handlers, the rotating file handler
    included); an in-process run must not configure logging over that, or
    every INFO line logged afterwards -- this module's refusal and verdict
    lines, and Superset's own -- is dropped and the file handler is gone.
    This pins env.py never calling it (and alembic.ini carrying nothing for
    it to apply)."""
    root = logging.getLogger()
    api = logging.getLogger("superset_ownership.api")
    api.setLevel(logging.NOTSET)  # inherits from root, as in the deployed app

    class Capture(logging.Handler):
        def __init__(self):
            super().__init__()
            self.records = []

        def emit(self, record):
            self.records.append(record)

    capture = Capture()
    # A logger a library switched off on purpose before the chain ran.
    silenced = logging.getLogger("third_party.deliberately_silent")
    saved_level, saved_handlers = root.level, list(root.handlers)
    root.setLevel(logging.INFO)  # Superset's LOG_LEVEL in the light config
    root.addHandler(capture)
    silenced.disabled = True
    try:
        handlers_before = list(root.handlers)
        assert api.getEffectiveLevel() == logging.INFO

        migrate.upgrade("head", f"sqlite:///{tmp_path / 'root.db'}")

        assert root.level == logging.INFO, "the root logger's level was reset"
        assert root.handlers == handlers_before, "root handlers were replaced"
        assert all(h is before for h, before in zip(root.handlers, handlers_before))
        assert api.getEffectiveLevel() == logging.INFO
        assert api.disabled is False
        assert silenced.disabled is True, (
            "fileConfig(disable_existing_loggers=False) would have re-enabled it"
        )

        api.info("superset_ownership: INFO line after the chain has run")
        assert [r.getMessage() for r in capture.records if r.name == api.name] == [
            "superset_ownership: INFO line after the chain has run"
        ], "an INFO line no longer reaches a handler attached before the upgrade"
    finally:
        silenced.disabled = False
        root.removeHandler(capture)
        root.setLevel(saved_level)
        for h in list(root.handlers):
            if h not in saved_handlers:
                root.removeHandler(h)
        for h in saved_handlers:
            if h not in root.handlers:
                root.addHandler(h)


# ------------------------------------------------------------------ 9. foreign keys
def _seed_governed(uri: str) -> None:
    """Two governed objects with owners that exist, one share each."""
    with _engine(uri).begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO ownership_object "
                "(asset_type, object_id, object_uuid, owner_user_id, visibility) "
                "VALUES ('chart', 56, 'u-56', 11, 'private'), "
                "('dashboard', 5, 'u-5', 12, 'shared')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO ownership_share (asset_type, object_id, subject, role) "
                "VALUES ('chart', 56, 'user:1', 'viewer'), "
                "('dashboard', 5, 'user:11', 'editor')"
            )
        )


def test_owner_fk_is_created_directly_by_0003_when_ab_user_exists(tmp_path, caplog):
    """The deployed order: Flask-AppBuilder creates ab_user inside
    appbuilder.init_app, BEFORE FLASK_APP_MUTATOR runs this chain, so a fresh
    `superset db upgrade` gets the constraint from revision 0003 itself --
    no deferral line, no WARNING, and the post-upgrade re-check finds it
    already there. The deferral (next tests) is the fallback."""
    uri = _uri(tmp_path)
    _make_fake_superset_world(uri)
    caplog.set_level(logging.INFO, logger="superset_ownership.constraints")

    migrate.upgrade("head", uri)

    messages = [
        r.getMessage()
        for r in caplog.records
        if r.name == "superset_ownership.constraints"
    ]
    assert [m for m in messages if m.endswith(f"{OWNER_FK} created")] == [
        f"superset_ownership: {OWNER_FK} created"
    ], "created exactly once, by 0003"
    assert not any("does not exist yet" in m for m in messages), "no deferral"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert OWNER_FK in _fks(uri, "ownership_object")


def test_owner_fk_is_set_null_into_ab_user(tmp_path):
    uri = _uri(tmp_path)
    _make_fake_superset_world(uri)
    migrate.upgrade("head", uri)

    fk = _fks(uri, "ownership_object")[OWNER_FK]
    assert fk["referred_table"] == "ab_user"
    assert fk["constrained_columns"] == ["owner_user_id"]
    assert fk["referred_columns"] == ["id"]
    assert fk["options"].get("ondelete", "").upper() == "SET NULL"


def test_share_fk_cascades_from_the_object_row(tmp_path):
    uri = _uri(tmp_path)
    migrate.upgrade("head", uri)  # intra-module: needs no core table at all

    fk = _fks(uri, "ownership_share")[SHARE_FK]
    assert fk["referred_table"] == "ownership_object"
    assert fk["constrained_columns"] == ["asset_type", "object_id"]
    assert fk["referred_columns"] == ["asset_type", "object_id"]
    assert fk["options"].get("ondelete", "").upper() == "CASCADE"


def test_object_side_has_no_foreign_key_by_decision(tmp_path):
    """object_id -> dashboards/slices is deliberately absent: a cascade would
    remove the row the delete hook reads the uuid from to purge the store,
    and a restrict would block every retention purge (decision record).
    The only constraint on ownership_object references ab_user; the outbox
    references nothing."""
    uri = _uri(tmp_path)
    _make_fake_superset_world(uri)
    migrate.upgrade("head", uri)

    assert {fk["referred_table"] for fk in _fks(uri, "ownership_object").values()} == {
        "ab_user"
    }
    assert _fks(uri, "ownership_outbox") == {}


def test_share_fk_matches_the_declared_metadata(tmp_path):
    """db.py declares the intra-module constraint; the chain must create the
    same one, or create_all() (the harness) and the chain drift apart."""
    declared = [
        c
        for c in ownership_db.ownership_share.constraints
        if isinstance(c, sa.ForeignKeyConstraint)
    ]
    assert [c.name for c in declared] == [SHARE_FK]
    assert declared[0].ondelete == "CASCADE"
    uri = _uri(tmp_path)
    migrate.upgrade("head", uri)
    fk = _fks(uri, "ownership_share")[SHARE_FK]
    assert fk["constrained_columns"] == [c.parent.name for c in declared[0].elements]


def test_upgrade_repairs_violations_before_constraining(tmp_path, caplog):
    """Rows the constraints would reject are what `reconcile` would repair:
    an owner naming a user that no longer exists becomes NULL (unowned), a
    share with no object row behind it is removed. Both counts logged."""
    uri = _uri(tmp_path)
    _make_fake_superset_world(uri)
    migrate.upgrade("0002_ownership_outbox", uri)
    _seed_governed(uri)
    with _engine(uri).begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO ownership_object "
                "(asset_type, object_id, object_uuid, owner_user_id, visibility) "
                "VALUES ('chart', 57, 'u-57', 999, 'private'), "
                "('chart', 58, 'u-58', 998, 'public')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO ownership_share (asset_type, object_id, subject, role) "
                "VALUES ('chart', 4040, 'user:1', 'viewer'), "
                "('dashboard', 4041, 'user:1', 'viewer')"
            )
        )

    caplog.set_level(logging.INFO, logger="superset_ownership.constraints")
    migrate.upgrade("head", uri)

    owners = {
        (r[1], r[2]): r[4] for r in _rows(uri, "ownership_object")
    }  # (asset_type, object_id) -> owner_user_id
    assert owners == {
        ("chart", 56): 11,
        ("dashboard", 5): 12,
        ("chart", 57): None,
        ("chart", 58): None,
    }
    shares = {(r[1], r[2], r[3]) for r in _rows(uri, "ownership_share")}
    assert shares == {("chart", 56, "user:1"), ("dashboard", 5, "user:11")}
    messages = [
        r.getMessage()
        for r in caplog.records
        if r.name == "superset_ownership.constraints"
    ]
    assert any(
        m.startswith("superset_ownership: 2 ownership row(s) named a user")
        for m in messages
    )
    assert any(
        m.startswith("superset_ownership: 2 share row(s) had no ownership row")
        for m in messages
    )
    assert [r.levelno for r in caplog.records if "named a user" in r.getMessage()] == [
        logging.WARNING
    ]


def test_upgrade_logs_zero_repairs_on_a_clean_database(tmp_path, caplog):
    uri = _uri(tmp_path)
    _make_fake_superset_world(uri)
    migrate.upgrade("0002_ownership_outbox", uri)
    _seed_governed(uri)
    caplog.set_level(logging.INFO, logger="superset_ownership.constraints")

    migrate.upgrade("head", uri)

    counted = [
        r
        for r in caplog.records
        if "named a user" in r.getMessage() or "had no ownership row" in r.getMessage()
    ]
    assert [r.levelno for r in counted] == [logging.INFO, logging.INFO]
    assert all(r.getMessage().startswith("superset_ownership: 0 ") for r in counted)


def test_owner_fk_is_deferred_not_lost_when_ab_user_does_not_exist_yet(
    tmp_path, caplog
):
    """The fallback. ab_user is absent when 0003 runs only outside the CLI
    -- a direct migrate.upgrade() that creates no application first, as
    this test does, or FAB_CREATE_DB=False. Alembic never re-runs a
    recorded revision, so migrate.upgrade re-checks the constraint that
    references the core table: skipped with one warning, added on the
    next run."""
    uri = _uri(tmp_path)
    caplog.set_level(logging.INFO, logger="superset_ownership.constraints")
    migrate.upgrade("head", uri)

    assert migrate.current(uri) == HEAD_REVISION, "the revision is recorded regardless"
    assert OWNER_FK not in _fks(uri, "ownership_object")
    assert SHARE_FK in _fks(uri, "ownership_share"), (
        "the intra-module constraint never waits"
    )
    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "ab_user does not exist" in r.getMessage()
    ]
    assert len(warnings) == 1
    assert "superset ownership db upgrade" in warnings[0].getMessage()

    # Superset's chain runs; ab_user appears. The next deploy's unconditional
    # upgrade adds the constraint without any operator step.
    with _engine(uri).begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE ab_user (id INTEGER PRIMARY KEY, username VARCHAR(64))"
            )
        )
    migrate.upgrade("head", uri)

    assert migrate.current(uri) == HEAD_REVISION
    assert OWNER_FK in _fks(uri, "ownership_object")
    assert (
        _fks(uri, "ownership_object")[OWNER_FK]["options"].get("ondelete", "").upper()
        == "SET NULL"
    )


def test_downgrade_to_0002_drops_the_constraints_and_keeps_every_row(tmp_path):
    uri = _uri(tmp_path)
    _make_fake_superset_world(uri)
    migrate.upgrade("head", uri)
    _seed_governed(uri)
    rows_before = {t: _rows(uri, t) for t in OUR_TABLES}
    indexes_before = {t: _indexes(uri, t) for t in OUR_TABLES}

    migrate.downgrade("0002_ownership_outbox", uri)

    assert migrate.current(uri) == "0002_ownership_outbox"
    assert OWNER_FK not in _fks(uri, "ownership_object")
    assert SHARE_FK not in _fks(uri, "ownership_share")
    assert _same_data(rows_before, {t: _rows(uri, t) for t in OUR_TABLES}), (
        "a constraint drop loses no data"
    )
    assert {
        t: _indexes(uri, t) - {"ix_ownership_object_tenant_guid"} for t in OUR_TABLES
    } == {
        t: ix - {"ix_ownership_object_tenant_guid"} for t, ix in indexes_before.items()
    }, "nor indexes (SQLite rebuilds the table)"

    migrate.upgrade("head", uri)
    assert OWNER_FK in _fks(uri, "ownership_object")
    assert SHARE_FK in _fks(uri, "ownership_share")
    assert _same_data(rows_before, {t: _rows(uri, t) for t in OUR_TABLES})


def test_downgrade_below_0003_stays_unconstrained_on_later_upgrades_to_0002(tmp_path):
    """The post-upgrade re-check is for a database AT or beyond 0003. An
    operator who deliberately downgraded to 0002 and upgrades to 0002 again
    must not be handed the constraint back through the side door."""
    uri = _uri(tmp_path)
    _make_fake_superset_world(uri)
    migrate.upgrade("head", uri)
    migrate.downgrade("0002_ownership_outbox", uri)

    migrate.upgrade("0002_ownership_outbox", uri)

    assert migrate.current(uri) == "0002_ownership_outbox"
    assert OWNER_FK not in _fks(uri, "ownership_object")


def test_downgrade_is_idempotent_when_the_owner_fk_was_never_created(tmp_path):
    """No ab_user, so 0003 skipped the owner constraint; its downgrade must
    skip the drop just as quietly (both directions inspect first)."""
    uri = _uri(tmp_path)
    migrate.upgrade("head", uri)
    assert OWNER_FK not in _fks(uri, "ownership_object")

    migrate.downgrade("0002_ownership_outbox", uri)

    assert migrate.current(uri) == "0002_ownership_outbox"
    assert not _fks(uri, "ownership_share")


def test_downgrade_base_from_head_removes_only_our_tables_with_constraints_present(
    tmp_path,
):
    uri = _uri(tmp_path)
    _make_fake_superset_world(uri)
    migrate.upgrade("head", uri)
    _seed_governed(uri)
    before = _snapshot_superset_world(uri)

    migrate.downgrade("base", uri)

    assert not (OUR_TABLES & _tables(uri))
    assert _snapshot_superset_world(uri) == before, (
        "ab_user is referenced, never touched"
    )


def test_pure_alembic_fallback_creates_the_same_constraints(tmp_path, monkeypatch):
    """Without Superset (the pure CI job) the module's own Alembic path runs;
    it must produce constraints identical to Superset's helper. Forced here
    by making the helper's module unimportable."""
    import sys

    monkeypatch.setitem(sys.modules, "superset.migrations.shared.utils", None)
    uri = _uri(tmp_path)
    _make_fake_superset_world(uri)

    migrate.upgrade("head", uri)

    owner = _fks(uri, "ownership_object")[OWNER_FK]
    share = _fks(uri, "ownership_share")[SHARE_FK]
    assert (owner["referred_table"], owner["options"].get("ondelete", "").upper()) == (
        "ab_user",
        "SET NULL",
    )
    assert (share["referred_table"], share["options"].get("ondelete", "").upper()) == (
        "ownership_object",
        "CASCADE",
    )
    for table, expected in EXPECTED_INDEXES.items():
        assert expected <= _indexes(uri, table), (
            f"{table} indexes survive the SQLite table rebuild"
        )

    migrate.downgrade("0002_ownership_outbox", uri)
    assert OWNER_FK not in _fks(uri, "ownership_object")
    assert SHARE_FK not in _fks(uri, "ownership_share")


# With SQLite told to enforce (PRAGMA foreign_keys=ON, what PostgreSQL does
# unconditionally), the constraints do what the decision record promises.


def test_enforced_deleting_a_user_unowns_their_objects(tmp_path):
    uri = _uri(tmp_path)
    _make_fake_superset_world(uri)
    migrate.upgrade("head", uri)
    _seed_governed(uri)

    with _enforcing(uri) as conn:
        conn.execute(sa.text("DELETE FROM ab_user WHERE id = 11"))
        conn.commit()

    owners = {(r[1], r[2]): r[4] for r in _rows(uri, "ownership_object")}
    assert owners == {("chart", 56): None, ("dashboard", 5): 12}, (
        "ben's chart is unowned; ada's dashboard untouched"
    )
    assert len(_rows(uri, "ownership_share")) == 2, (
        "shares are not the user's; they stay (dormant while unowned)"
    )


def test_enforced_deleting_an_object_row_takes_its_shares(tmp_path):
    uri = _uri(tmp_path)
    _make_fake_superset_world(uri)
    migrate.upgrade("head", uri)
    _seed_governed(uri)

    with _enforcing(uri) as conn:
        conn.execute(
            sa.text(
                "DELETE FROM ownership_object "
                "WHERE asset_type = 'chart' AND object_id = 56"
            )
        )
        conn.commit()

    assert {(r[1], r[2]) for r in _rows(uri, "ownership_share")} == {("dashboard", 5)}


def test_enforced_a_share_without_an_object_row_is_refused(tmp_path):
    uri = _uri(tmp_path)
    _make_fake_superset_world(uri)
    migrate.upgrade("head", uri)
    _seed_governed(uri)

    import pytest

    with _enforcing(uri) as conn, pytest.raises(sa.exc.IntegrityError):
        conn.execute(
            sa.text(
                "INSERT INTO ownership_share (asset_type, object_id, subject, role) "
                "VALUES ('chart', 4040, 'user:1', 'viewer')"
            )
        )


def test_enforced_an_owner_that_does_not_exist_is_refused(tmp_path):
    uri = _uri(tmp_path)
    _make_fake_superset_world(uri)
    migrate.upgrade("head", uri)

    import pytest

    with _enforcing(uri) as conn, pytest.raises(sa.exc.IntegrityError):
        conn.execute(
            sa.text(
                "INSERT INTO ownership_object "
                "(asset_type, object_id, object_uuid, owner_user_id, visibility) "
                "VALUES ('chart', 1, 'u-1', 999, 'private')"
            )
        )


# ------------------------------------------------- 10. the re-check under load
# The scenario these cover is the FALLBACK one: 0003 recorded with the owner
# constraint skipped (ab_user absent at the time), then ab_user appears and
# every booting process runs migrate.upgrade at once.


def _create_ab_user(uri: str) -> None:
    # N5 (review round 2, PR #97, `review-boot-storm-pr97.md`): dispose the
    # engine THIS function built, the way `_reset_throwaway_postgresql`
    # does -- `with engine.begin() as conn:` only returns the connection to
    # the pool on exit, it does not dispose the pool itself, so a pooled
    # psycopg2 connection (and its socket) stayed open past this function's
    # return; the fork-based boot-storm tests below are the first place in
    # this file that could inherit it. The two `_engine(uri).dispose()`
    # calls the earlier fix added at the call sites were themselves inert
    # (`_engine()` returns a NEW engine every call, so they disposed an
    # empty pool of their own, never this one) and have been removed.
    engine = _engine(uri)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE ab_user (id INTEGER PRIMARY KEY, username VARCHAR(64))"
            )
        )
    engine.dispose()


def _deferred_then_ab_user(uri: str) -> None:
    """A database at 0003 without the owner constraint, ab_user now present."""
    migrate.upgrade("head", uri)
    assert OWNER_FK not in _fks(uri, "ownership_object")
    _create_ab_user(uri)


def test_losing_the_cross_process_race_is_success_not_a_crash(
    tmp_path, monkeypatch, caplog
):
    """N processes boot on a database where the deferral engaged; each
    inspects, finds the constraint missing, and issues ADD CONSTRAINT.
    PostgreSQL serialises them on the table lock and every process but the
    first fails with DuplicateObject. The advisory lock closes that on
    PostgreSQL; this pins the dialect-neutral belt: a failed ADD whose name
    is present on re-inspection is another process having won, and the
    re-check returns True, logs it at INFO, and raises nothing -- for both
    DDL paths (Superset's helper and plain Alembic run through the same
    `create_fk`)."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy.exc import ProgrammingError
    from superset_ownership import constraints

    uri = _uri(tmp_path)
    _deferred_then_ab_user(uri)
    real_create_fk = constraints.create_fk

    def create_then_lose(name, table, *args, **kwargs):
        # The other process's ADD landed and committed first (its own
        # connection); ours then fails the way psycopg2 reports it.
        other = _engine(uri)
        with other.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                real_create_fk(name, table, *args, **kwargs)
        other.dispose()
        raise ProgrammingError(
            "ALTER TABLE ...",
            {},
            Exception(f'constraint "{name}" for relation "{table}" already exists'),
        )

    monkeypatch.setattr(constraints, "create_fk", create_then_lose)
    caplog.clear()  # the deferral's own WARNING, from the setup above
    caplog.set_level(logging.INFO, logger="superset_ownership.constraints")

    assert constraints.ensure_owner_fk_on(uri) is True

    assert OWNER_FK in _fks(uri, "ownership_object")
    assert [
        r.getMessage() for r in caplog.records if "another process" in r.getMessage()
    ] == [
        f"superset_ownership: {OWNER_FK} was added by another process while this one "
        "was adding it (ProgrammingError); nothing to do"
    ]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_a_failed_add_whose_constraint_is_still_missing_is_raised(
    tmp_path, monkeypatch
):
    """The belt must not hide a real failure (ab_user without a unique key,
    for one): a failed ADD with the name still absent propagates."""
    import pytest
    from sqlalchemy.exc import ProgrammingError
    from superset_ownership import constraints

    uri = _uri(tmp_path)
    _deferred_then_ab_user(uri)

    def fail(*args, **kwargs):
        raise ProgrammingError("ALTER TABLE ...", {}, Exception("no unique constraint"))

    monkeypatch.setattr(constraints, "create_fk", fail)
    with pytest.raises(ProgrammingError):
        constraints.ensure_owner_fk_on(uri)
    assert OWNER_FK not in _fks(uri, "ownership_object")


def test_concurrent_boots_in_one_process_all_succeed_with_one_constraint(tmp_path):
    """Three threads run migrate.upgrade at once against the deferred
    database. The operations context is a process-global proxy, so without
    the process lock two threads cross-wire their connections; with it every
    thread boots and exactly one constraint exists. (Cross-process, the
    PostgreSQL advisory lock and the test above do the same job.)"""
    import threading

    uri = _uri(tmp_path)
    _deferred_then_ab_user(uri)
    outcomes: dict[str, object] = {}

    def boot(name: str) -> None:
        try:
            migrate.upgrade("head", uri)
            outcomes[name] = "booted"
        except Exception as exc:  # noqa: BLE001 - the assertion below reports it
            outcomes[name] = f"{exc.__class__.__name__}: {str(exc)[:120]}"

    threads = [threading.Thread(target=boot, args=(f"w{i}",)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert outcomes == {"w0": "booted", "w1": "booted", "w2": "booted"}
    fks = _fks(uri, "ownership_object")
    assert [n for n in fks if n == OWNER_FK] == [OWNER_FK]
    assert migrate.current(uri) == HEAD_REVISION


THROWAWAY_DATABASE_NAMES = ("_test", "ownership_")


def _throwaway_postgresql_uri() -> str:
    """OWNERSHIP_TEST_DATABASE_URI, or skip. The PostgreSQL tests DROP the
    ownership tables and `ab_user` in whatever database it names, so the
    name must look like a throwaway (`*_test*` or `ownership_*`); anything
    else -- a real Superset database -- is refused before a statement runs,
    not documented as something not to do."""
    import os
    import re

    import pytest

    uri = os.environ.get("OWNERSHIP_TEST_DATABASE_URI")
    if not uri or not uri.startswith("postgresql"):
        pytest.skip("OWNERSHIP_TEST_DATABASE_URI (postgresql) not set")
    database = sa.engine.make_url(uri).database or ""
    if not (re.search(r"_test", database) or database.startswith("ownership_")):
        pytest.fail(
            f"OWNERSHIP_TEST_DATABASE_URI names {database!r}; these tests drop "
            "tables and run only against a database named *_test* or ownership_*"
        )
    return uri


def _reset_throwaway_postgresql(uri: str) -> None:
    engine = _engine(uri)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "DROP TABLE IF EXISTS ownership_share, ownership_object, "
                "ownership_outbox, alembic_version_ownership, ab_user CASCADE"
            )
        )
    engine.dispose()


def test_the_postgresql_tests_refuse_a_database_that_is_not_a_throwaway(monkeypatch):
    import pytest

    monkeypatch.setenv(
        "OWNERSHIP_TEST_DATABASE_URI", "postgresql://u:p@db-light:5432/superset_light"
    )
    with pytest.raises(pytest.fail.Exception, match="superset_light"):
        _throwaway_postgresql_uri()
    for name in ("ownership_fk_test", "superset_test", "ci_test_db"):
        monkeypatch.setenv(
            "OWNERSHIP_TEST_DATABASE_URI", f"postgresql://u:p@db-light:5432/{name}"
        )
        assert _throwaway_postgresql_uri().endswith(name)
    monkeypatch.delenv("OWNERSHIP_TEST_DATABASE_URI")
    with pytest.raises(pytest.skip.Exception):
        _throwaway_postgresql_uri()


def test_concurrent_boots_on_postgresql_all_succeed_with_one_constraint():
    """The same storm against a real PostgreSQL database, where the
    DuplicateObject race is real: set OWNERSHIP_TEST_DATABASE_URI to a
    throwaway database to run it (the container job does)."""
    import threading

    uri = _throwaway_postgresql_uri()
    _reset_throwaway_postgresql(uri)
    _deferred_then_ab_user(uri)
    outcomes: dict[str, object] = {}

    def boot(name: str) -> None:
        try:
            migrate.upgrade("head", uri)
            outcomes[name] = "booted"
        except Exception as exc:  # noqa: BLE001
            outcomes[name] = f"{exc.__class__.__name__}: {str(exc)[:120]}"

    threads = [threading.Thread(target=boot, args=(f"w{i}",)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert outcomes == {"w0": "booted", "w1": "booted", "w2": "booted"}
    with _engine(uri).connect() as conn:
        count = conn.execute(
            sa.text("SELECT count(*) FROM pg_constraint WHERE conname = :n"),
            {"n": OWNER_FK},
        ).scalar_one()
    assert count == 1


def test_the_re_check_covers_the_share_constraint_too(tmp_path):
    """A database recorded at 0003 whose share constraint is missing (a
    stamp before the refusal existed, a hand edit): the next upgrade adds
    it, as it does the owner one -- it is intra-module and idempotent, so
    there is no reason to leave it out."""
    from superset_ownership import constraints

    uri = _uri(tmp_path)
    _make_fake_superset_world(uri)
    migrate.upgrade("0002_ownership_outbox", uri)
    _seed_governed(uri)
    from alembic import command

    command.stamp(migrate._config(uri), "head")  # the raw stamp, no refusal
    assert migrate.current(uri) == HEAD_REVISION
    assert constraints.missing(_engine(uri)) == [OWNER_FK, SHARE_FK]

    migrate.upgrade("head", uri)

    assert constraints.missing(_engine(uri)) == []
    share = _fks(uri, "ownership_share")[SHARE_FK]
    assert share["options"]["ondelete"].upper() == "CASCADE"
    assert len(_rows(uri, "ownership_object")) == 2
    assert len(_rows(uri, "ownership_share")) == 2


def test_a_constraint_added_not_valid_by_hand_is_repaired_and_validated(
    tmp_path, monkeypatch, caplog
):
    """The documented large-table procedure: the operator adds the owner
    constraint `NOT VALID` before the deploy and stops. 0003 (or the
    re-check) finds the name present; it must not stop there -- the rows
    the `NOT VALID` add never checked are repaired and the constraint is
    VALIDATEd, both logged. SQLite has no NOT VALID, so the PostgreSQL
    answer is stubbed here (the PostgreSQL test below does it for real)
    and the shape is pinned: repair, then VALIDATE, in that order."""
    from superset_ownership import constraints

    uri = _uri(tmp_path)
    _make_fake_superset_world(uri)
    migrate.upgrade("head", uri)
    with _engine(uri).begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO ownership_object "
                "(asset_type, object_id, object_uuid, owner_user_id, visibility) "
                "VALUES ('chart', 57, 'u-57', 999, 'private')"
            )
        )
    calls: list[tuple] = []
    monkeypatch.setattr(constraints, "_fk_validated", lambda bind, t, n: False)

    def validate_stub(name: str, table: str) -> None:
        # Read on the run's own connection: the repair is not committed yet.
        owners = (
            constraints._bind()
            .execute(sa.text("SELECT object_id, owner_user_id FROM ownership_object"))
            .all()
        )
        calls.append(("validate", name, table, dict(owners)))

    monkeypatch.setattr(constraints, "validate_fk", validate_stub)
    caplog.set_level(logging.INFO, logger="superset_ownership.constraints")

    assert constraints.ensure_owner_fk_on(uri) is True
    assert constraints.ensure_share_fk_on(uri) is True

    assert [c[:3] for c in calls] == [
        ("validate", OWNER_FK, "ownership_object"),
        ("validate", SHARE_FK, "ownership_share"),
    ]
    assert calls[0][3][57] is None, "repaired BEFORE the VALIDATE"
    messages = [
        (r.levelno, r.getMessage())
        for r in caplog.records
        if "NOT VALID" in r.getMessage()
    ]
    assert messages == [
        (
            logging.WARNING,
            f"superset_ownership: {OWNER_FK} was present but NOT VALID (added by "
            "hand); 1 ownership row(s) named a user that no longer exists; owner "
            "set to NULL (unowned) and the constraint validated",
        ),
        (
            logging.INFO,
            f"superset_ownership: {SHARE_FK} was present but NOT VALID (added by "
            "hand); 0 share row(s) had no ownership row behind them; removed and "
            "the constraint validated",
        ),
    ]


def test_a_validated_constraint_is_left_alone(tmp_path, monkeypatch, caplog):
    """The common case: present and validated (every dialect but PostgreSQL
    always; PostgreSQL after a normal add). No repair, no VALIDATE, no line."""
    from superset_ownership import constraints

    uri = _uri(tmp_path)
    _make_fake_superset_world(uri)
    migrate.upgrade("head", uri)
    monkeypatch.setattr(
        constraints, "validate_fk", lambda *a: (_ for _ in ()).throw(AssertionError(a))
    )
    caplog.set_level(logging.INFO, logger="superset_ownership.constraints")
    caplog.clear()

    assert constraints.ensure_owner_fk_on(uri) is True
    assert constraints.ensure_share_fk_on(uri) is True

    assert not [r for r in caplog.records if "NOT VALID" in r.getMessage()]
    status = constraints.status(_engine(uri))
    assert (status["owner_fk_validated"], status["share_fk_validated"]) == (True, True)


def test_a_failed_validate_over_a_present_constraint_is_raised_not_called_a_lost_race(
    tmp_path, monkeypatch, caplog
):
    """The re-check's belt (a failed statement whose constraint is present
    on re-inspection is another process having won) must not cover the
    VALIDATE step: a constraint added `NOT VALID` by hand is present by
    name whether or not its VALIDATE succeeded. Here the repair is
    neutralised over a dangling owner, so the VALIDATE fails the way
    PostgreSQL reports it (ForeignKeyViolation), and the constraint stays
    unvalidated: `ensure_owner_fk_on` must re-raise, not log "added by
    another process" and return True -- the boot would proceed, `check`
    would stay red and its instruction (run the upgrade) would loop into
    the same swallow. The PostgreSQL answer is stubbed (SQLite has neither
    NOT VALID nor VALIDATE); the shape is what is pinned."""
    import pytest
    from sqlalchemy.exc import IntegrityError
    from superset_ownership import constraints

    uri = _uri(tmp_path)
    _make_fake_superset_world(uri)
    migrate.upgrade("head", uri)
    with _engine(uri).begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO ownership_object "
                "(asset_type, object_id, object_uuid, owner_user_id, visibility) "
                "VALUES ('chart', 57, 'u-57', 999, 'private')"
            )
        )
    monkeypatch.setattr(constraints, "_fk_validated", lambda bind, t, n: False)
    monkeypatch.setattr(constraints, "null_dangling_owners", lambda: 0)

    def validate_like_postgresql(name: str, table: str) -> None:
        dangling = (
            constraints._bind()
            .execute(
                sa.text(
                    "SELECT count(*) FROM ownership_object WHERE owner_user_id = 999"
                )
            )
            .scalar()
        )
        if dangling:
            raise IntegrityError(
                f'ALTER TABLE {table} VALIDATE CONSTRAINT "{name}"',
                {},
                Exception(
                    'insert or update on table "ownership_object" violates '
                    f'foreign key constraint "{name}"'
                ),
            )

    monkeypatch.setattr(constraints, "validate_fk", validate_like_postgresql)
    caplog.set_level(logging.INFO, logger="superset_ownership.constraints")
    caplog.clear()

    with pytest.raises(IntegrityError) as info:
        constraints.ensure_owner_fk_on(uri)

    assert "VALIDATE CONSTRAINT" in str(info.value)
    assert not [r for r in caplog.records if "another process" in r.getMessage()], (
        "a failed VALIDATE is not the lost race"
    )
    assert not [r for r in caplog.records if "constraint validated" in r.getMessage()]
    # The same failure through the deployed entry point stops the boot.
    with pytest.raises(IntegrityError):
        migrate.upgrade("head", uri)


def test_the_lost_race_belt_still_covers_a_failed_add_of_a_validated_constraint(
    tmp_path, monkeypatch
):
    """The other half of the previous test: a failed ADD whose constraint
    the other process added AND validated is still the lost race. The
    re-inspection reads `_fk_validated` for the winner's constraint; when
    it says validated the belt returns True as before."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy.exc import ProgrammingError
    from superset_ownership import constraints

    uri = _uri(tmp_path)
    _deferred_then_ab_user(uri)
    real_create_fk = constraints.create_fk
    asked: list[tuple[str, str]] = []
    real_validated = constraints._fk_validated

    def validated(bind, table, name):
        asked.append((table, name))
        return real_validated(bind, table, name)

    def create_then_lose(name, table, *args, **kwargs):
        other = _engine(uri)
        with other.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                real_create_fk(name, table, *args, **kwargs)
        other.dispose()
        raise ProgrammingError("ALTER TABLE ...", {}, Exception("already exists"))

    monkeypatch.setattr(constraints, "create_fk", create_then_lose)
    monkeypatch.setattr(constraints, "_fk_validated", validated)

    assert constraints.ensure_owner_fk_on(uri) is True
    assert ("ownership_object", OWNER_FK) in asked, "the belt asked for validated"


def test_a_not_valid_owner_constraint_on_postgresql_is_validated_by_the_upgrade():
    """The procedure for real: at 0002, `ADD CONSTRAINT ... NOT VALID` by
    hand over a dangling owner (which the NOT VALID add accepts), then
    `migrate.upgrade("head")`. Afterwards `pg_constraint.convalidated` is
    true, the dangling owner is NULL, and `status()` says validated."""
    from superset_ownership import constraints

    uri = _throwaway_postgresql_uri()
    _reset_throwaway_postgresql(uri)
    _create_ab_user(uri)
    migrate.upgrade("0002_ownership_outbox", uri)
    with _engine(uri).begin() as conn:
        conn.execute(sa.text("INSERT INTO ab_user VALUES (11, 'ben')"))
        conn.execute(
            sa.text(
                "INSERT INTO ownership_object "
                "(asset_type, object_id, object_uuid, owner_user_id, visibility) "
                "VALUES ('chart', 56, 'u-56', 11, 'private'), "
                "('chart', 57, 'u-57', 999, 'private')"
            )
        )
        conn.execute(
            sa.text(
                f"ALTER TABLE ownership_object ADD CONSTRAINT {OWNER_FK} "
                "FOREIGN KEY (owner_user_id) REFERENCES ab_user (id) "
                "ON DELETE SET NULL NOT VALID"
            )
        )

    def convalidated() -> bool:
        with _engine(uri).connect() as conn:
            return conn.execute(
                sa.text("SELECT convalidated FROM pg_constraint WHERE conname = :n"),
                {"n": OWNER_FK},
            ).scalar_one()

    assert convalidated() is False
    assert constraints.status(_engine(uri))["owner_fk_validated"] is False

    migrate.upgrade("head", uri)

    assert convalidated() is True
    status = constraints.status(_engine(uri))
    assert (status["owner_fk_present"], status["owner_fk_validated"]) == (True, True)
    with _engine(uri).connect() as conn:
        owners = dict(
            conn.execute(
                sa.text("SELECT object_id, owner_user_id FROM ownership_object")
            ).all()
        )
    assert owners == {56: 11, 57: None}, "the dangling owner was repaired first"
    migrate.upgrade("head", uri)  # idempotent: validated stays validated
    assert convalidated() is True


def test_stamp_refuses_to_record_0003_over_missing_constraints(tmp_path):
    """`stamp head` at 0002 would leave both constraints missing with no
    DDL ever running for them; it is refused, and says to upgrade. A stamp
    to 0002 (below the constraints) and a stamp over a database that has
    them are still accepted."""
    import pytest

    uri = _uri(tmp_path)
    _make_fake_superset_world(uri)
    migrate.upgrade("0002_ownership_outbox", uri)

    with pytest.raises(migrate.StampRefusedError) as info:
        migrate.stamp("head", uri)
    assert OWNER_FK in str(info.value)
    assert SHARE_FK in str(info.value)
    assert "superset ownership db upgrade" in str(info.value)
    assert migrate.current(uri) == "0002_ownership_outbox", "nothing recorded"

    migrate.stamp("0002_ownership_outbox", uri)  # re-stamping below 0003: fine
    migrate.upgrade("head", uri)
    migrate.stamp("head", uri)  # constraints present: fine
    assert migrate.current(uri) == HEAD_REVISION


def test_ensure_entry_points_hold_the_process_lock(tmp_path, monkeypatch):
    """`alembic.op` is process-global; the entry points serialise on
    `_DDL_LOCK`. Pinned by holding the lock from another thread and
    checking the re-check waits for it."""
    import threading
    import time

    from superset_ownership import constraints

    uri = _uri(tmp_path)
    _deferred_then_ab_user(uri)
    released = threading.Event()
    acquired = threading.Event()

    def hold() -> None:
        with constraints._DDL_LOCK:
            acquired.set()
            released.wait(timeout=30)

    holder = threading.Thread(target=hold)
    holder.start()
    acquired.wait(timeout=30)
    result: list[bool] = []
    worker = threading.Thread(
        target=lambda: result.append(constraints.ensure_owner_fk_on(uri))
    )
    worker.start()
    time.sleep(0.3)
    assert not result, "the re-check ran while another thread held the DDL lock"
    assert OWNER_FK not in _fks(uri, "ownership_object")
    released.set()
    worker.join(timeout=30)
    holder.join(timeout=30)
    assert result == [True]
    assert OWNER_FK in _fks(uri, "ownership_object")


# --------------------------------- 10b. the chain's own revision transitions
# Issue #89: unlike the re-check above (which only ever protects revision
# 0003's constraint ADD), these boot-storm processes race the chain's own
# UPGRADE -- the alembic_version_ownership UPDATE, or the DDL of an early
# revision on an empty database. `constraints._DDL_LOCK` (a threading.RLock)
# already fully serialises every call into this chain WITHIN one process, so
# a thread-based test here would pass whether or not migrations/env.py's
# PostgreSQL advisory lock does anything -- it would not be testing issue
# #89 at all. These tests use real, separate OS processes (multiprocessing,
# "fork") so each has its own unshared `_DDL_LOCK`, and only the chain-level
# `pg_advisory_xact_lock` in env.py can serialise them.


def _boot_storm_worker(uri: str, revision: str, queue) -> None:
    """Run in a forked child process: a fresh interpreter, so a fresh,
    process-local `_DDL_LOCK` unrelated to the other children's. Catches
    everything so the process itself always exits 0 and the failure (if
    any) travels back on the queue instead."""
    from superset_ownership import migrate as migrate_module

    try:
        migrate_module.upgrade(revision, uri)
        queue.put("booted")
    except Exception as exc:  # noqa: BLE001 - the assertion below reports it
        queue.put(f"{exc.__class__.__name__}: {str(exc)[:200]}")


def _run_boot_storm(uri: str, revision: str, n: int = 3):
    """Start `n` forked children, drain their results, then join them.
    `finally` covers the failure path too (review N2): if a child hangs
    (a lock that never releases) `queue.get` raises `Empty` instead of
    returning, and without this the other children would be left running
    rather than terminated."""
    import multiprocessing

    ctx = multiprocessing.get_context("fork")
    queue: multiprocessing.Queue = ctx.Queue()
    procs = [
        ctx.Process(target=_boot_storm_worker, args=(uri, revision, queue))
        for _ in range(n)
    ]
    try:
        for p in procs:
            p.start()
        results = [queue.get(timeout=90) for _ in procs]
        for p in procs:
            p.join(timeout=90)
        return results, [p.exitcode for p in procs]
    finally:
        for p in procs:
            if p.is_alive():
                p.terminate()
                p.join(5)


def test_boot_storm_from_0002_to_head_on_postgresql():
    """Review P14, reproduced and then fixed: three processes upgrading an
    existing database at 0002 to head at once. Before migrations/env.py's
    advisory lock, the loser(s) raced the alembic_version_ownership UPDATE
    and failed with `CommandError: Online migration expected to match one
    row...`. Now every process boots, the DDL (both revision 0003
    constraints) runs exactly once, and the chain lands at head."""
    uri = _throwaway_postgresql_uri()
    _reset_throwaway_postgresql(uri)
    _create_ab_user(uri)
    migrate.upgrade("0002_ownership_outbox", uri)
    assert migrate.current(uri) == "0002_ownership_outbox"

    # N5: the parent's pooled connection from `_create_ab_user` above is
    # disposed by that function itself now, before forking -- each child
    # would otherwise inherit the parent's socket, harmless today because
    # children exit via os._exit and the parent always opens a fresh engine
    # for its own checks, but a fork-based test is the first place in this
    # file it could bite.
    results, exit_codes = _run_boot_storm(uri, "head")

    assert results == ["booted", "booted", "booted"], results
    assert exit_codes == [0, 0, 0]
    assert migrate.current(uri) == HEAD_REVISION
    with _engine(uri).connect() as conn:
        owner_count = conn.execute(
            sa.text("SELECT count(*) FROM pg_constraint WHERE conname = :n"),
            {"n": OWNER_FK},
        ).scalar_one()
        share_count = conn.execute(
            sa.text("SELECT count(*) FROM pg_constraint WHERE conname = :n"),
            {"n": SHARE_FK},
        ).scalar_one()
    assert (owner_count, share_count) == (1, 1), "the DDL ran exactly once"


def test_boot_storm_from_empty_to_head_on_postgresql():
    """Review P15, reproduced and then fixed: the worse pre-fix failure,
    from an empty database -- 0001's own CREATE TABLE colliding across
    processes (`UniqueViolation` on `pg_class`). Same fix, same proof, from
    base instead of 0002."""
    uri = _throwaway_postgresql_uri()
    _reset_throwaway_postgresql(uri)
    _create_ab_user(uri)
    assert migrate.current(uri) is None

    # N5: see the sibling test above -- `_create_ab_user` disposes its own
    # engine before this returns.
    results, exit_codes = _run_boot_storm(uri, "head")

    assert results == ["booted", "booted", "booted"], results
    assert exit_codes == [0, 0, 0]
    assert migrate.current(uri) == HEAD_REVISION
    assert OUR_TABLES <= _tables(uri)


def test_upgrade_retries_once_after_a_racing_commanderror(
    tmp_path, caplog, monkeypatch
):
    """migrate.upgrade's belt (issue #89): a CommandError shaped like the
    boot-storm's version-row race (what a dialect with no chain lock, e.g.
    SQLite, can still hit) is retried once, not raised straight into a
    failed boot. The stub fails the FIRST call only, the way a real race
    would have resolved itself by the time this process gets a second
    try."""
    from alembic import command
    from alembic.util.exc import CommandError

    uri = _uri(tmp_path)
    real_upgrade = command.upgrade
    calls = {"n": 0}

    def flaky(cfg, revision):
        calls["n"] += 1
        if calls["n"] == 1:
            raise CommandError(
                "Online migration expected to match one row when updating "
                "'0002_ownership_outbox' to '0003_ownership_foreign_keys'"
            )
        return real_upgrade(cfg, revision)

    monkeypatch.setattr(command, "upgrade", flaky)
    caplog.set_level(logging.INFO, logger="superset_ownership.migrate")

    migrate.upgrade("head", uri)

    assert calls["n"] == 2, "one retry, not a loop"
    assert migrate.current(uri) == HEAD_REVISION
    messages = [
        r.getMessage() for r in caplog.records if r.name == "superset_ownership.migrate"
    ]
    assert any("raced a concurrent" in m and "retrying once" in m for m in messages)


def test_upgrade_does_not_retry_a_second_failure(tmp_path, monkeypatch):
    """A CommandError shaped like the boot-storm race that is STILL there
    on the retry is a real defect (a genuinely broken chain, not a
    resolved race) and must propagate -- not loop, not be swallowed."""
    import pytest
    from alembic import command
    from alembic.util.exc import CommandError

    uri = _uri(tmp_path)
    calls = {"n": 0}

    def always_fails(cfg, revision):
        calls["n"] += 1
        raise CommandError(
            "Online migration expected to match one row when updating "
            "'0002_ownership_outbox' to '0003_ownership_foreign_keys'"
        )

    monkeypatch.setattr(command, "upgrade", always_fails)

    with pytest.raises(CommandError):
        migrate.upgrade("head", uri)
    assert calls["n"] == 2, "tried once, retried once, then gave up"


def test_upgrade_does_not_retry_an_unrelated_failure(tmp_path, monkeypatch, caplog):
    """migrate.py's retry is narrowed to the chain-transition race shape
    (review M1): a `CommandError` that does not look like that race -- a
    bad revision name, say -- is logged at WARNING with its own class and
    text and re-raised on the first attempt, never retried and never
    described as a race that was not checked for."""
    import pytest
    from alembic import command
    from alembic.util.exc import CommandError

    uri = _uri(tmp_path)
    calls = {"n": 0}

    def always_fails(cfg, revision):
        calls["n"] += 1
        raise CommandError("Can't locate revision identified by 'no_such_revision'")

    monkeypatch.setattr(command, "upgrade", always_fails)
    caplog.set_level(logging.WARNING, logger="superset_ownership.migrate")

    with pytest.raises(CommandError):
        migrate.upgrade("no_such_revision", uri)
    assert calls["n"] == 1, "not retried -- does not match the race shape"
    messages = [
        r.getMessage() for r in caplog.records if r.name == "superset_ownership.migrate"
    ]
    assert any("CommandError" in m and "not retrying" in m for m in messages)
    assert not any("raced a concurrent" in m for m in messages)


def test_revision_transition_boot_storm_on_sqlite_still_boots(tmp_path):
    """SQLite takes no chain-level lock (migrations/env.py; no cross-process
    primitive exists, and the deploy contract is one process per database
    file). Three THREADS in one process racing the same transition remain
    safe regardless -- `constraints._DDL_LOCK` already serialises every
    call into this chain within a process, before and after issue #89 --
    which is exactly the boundary the module docstring draws: SQLite's
    safety is a single-process assumption, not a claim about concurrent
    processes."""
    import threading

    uri = _uri(tmp_path)
    migrate.upgrade("0002_ownership_outbox", uri)
    outcomes: dict[str, object] = {}

    def boot(name: str) -> None:
        try:
            migrate.upgrade("head", uri)
            outcomes[name] = "booted"
        except Exception as exc:  # noqa: BLE001 - the assertion reports it
            outcomes[name] = f"{exc.__class__.__name__}: {str(exc)[:120]}"

    threads = [threading.Thread(target=boot, args=(f"w{i}",)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert outcomes == {"w0": "booted", "w1": "booted", "w2": "booted"}
    assert migrate.current(uri) == HEAD_REVISION


# ---------------------------------------------- 10c. check_consistency's signal
# check_consistency needs Superset for the sentinel scan; here everything but
# the database is stubbed, the way test_group_id.py drives the share-mirror
# buckets, so the constraint report is exercised against a real chain.


def _consistency_on(monkeypatch, uri: str):
    import sys
    import types

    from sqlalchemy.orm import Session
    from superset_ownership import lifecycle, outbox, sentinel

    stub = types.ModuleType("superset")
    stub.security_manager = None
    stub.db = types.SimpleNamespace(session=Session(_engine(uri)))
    models = types.ModuleType("superset.models")
    dashboard = types.ModuleType("superset.models.dashboard")
    dashboard.Dashboard = type("Dashboard", (), {})
    slice_ = types.ModuleType("superset.models.slice")
    slice_.Slice = type("Slice", (), {})
    monkeypatch.setitem(sys.modules, "superset", stub)
    monkeypatch.setitem(sys.modules, "superset.models", models)
    monkeypatch.setitem(sys.modules, "superset.models.dashboard", dashboard)
    monkeypatch.setitem(sys.modules, "superset.models.slice", slice_)
    monkeypatch.setattr(lifecycle, "all_rows", lambda model: [])
    monkeypatch.setattr(sentinel, "get_sentinel_subject", lambda: None)
    monkeypatch.setattr(outbox, "enabled", lambda: False)
    # The stub `superset` has no DashboardRestApi to wrap; the field this
    # feeds is the dashboard patch's, proven in test_dashboard_patch.py.
    # Without this the answer depended on whether an earlier test had
    # already imported the real superset.dashboards.api in this process.
    from superset_ownership import dashboard_patch

    monkeypatch.setattr(dashboard_patch, "installed", lambda: True)
    return lifecycle


def test_check_reports_both_constraints_present_on_the_deployed_path(
    tmp_path, monkeypatch
):
    uri = _uri(tmp_path)
    _make_fake_superset_world(uri)
    migrate.upgrade("head", uri)
    lifecycle = _consistency_on(monkeypatch, uri)

    report = lifecycle.check_consistency()

    assert (report["owner_fk_present"], report["share_fk_present"]) == (True, True)
    assert (report["owner_fk_validated"], report["share_fk_validated"]) == (True, True)
    assert report["constraints_missing"] == []
    assert report["constraints_unvalidated"] == []
    assert report["ok"] is True, {
        k: v for k, v in report.items() if v is False or (isinstance(v, list) and v)
    }


def test_check_fails_while_a_constraint_is_present_but_not_validated(
    tmp_path, monkeypatch, caplog
):
    """A hand-applied NOT VALID constraint is present and `check` used to
    say so and stop; it is enforced for new writes only until validated,
    so the report lists it, fails, and the boot log says what to run."""
    from superset_ownership import constraints

    uri = _uri(tmp_path)
    _make_fake_superset_world(uri)
    migrate.upgrade("head", uri)
    monkeypatch.setattr(
        constraints, "_fk_validated", lambda bind, table, name: name != OWNER_FK
    )
    lifecycle = _consistency_on(monkeypatch, uri)

    report = lifecycle.check_consistency()

    assert (report["owner_fk_present"], report["share_fk_present"]) == (True, True)
    assert (report["owner_fk_validated"], report["share_fk_validated"]) == (False, True)
    assert report["constraints_missing"] == []
    assert report["constraints_unvalidated"] == [OWNER_FK]
    assert report["ok"] is False

    caplog.set_level(logging.INFO, logger="superset_ownership.lifecycle")
    lifecycle.startup_check()
    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and r.name == "superset_ownership.lifecycle"
    ]
    assert len(warnings) == 1
    assert OWNER_FK in warnings[0]
    assert "NOT VALID" in warnings[0]
    assert "superset ownership db upgrade" in warnings[0]


def test_check_fails_while_the_deferred_owner_constraint_is_missing(
    tmp_path, monkeypatch, caplog
):
    """The state the deferral is built for was invisible after its single
    WARNING: 0003 recorded, owner constraint absent, `check` green. Now it
    is red, the boot log says so at every boot, and the next upgrade
    (the re-check) turns it green again."""
    uri = _uri(tmp_path)
    migrate.upgrade("head", uri)  # no ab_user: deferred
    lifecycle = _consistency_on(monkeypatch, uri)

    report = lifecycle.check_consistency()
    assert (report["owner_fk_present"], report["share_fk_present"]) == (False, True)
    assert report["constraints_missing"] == [OWNER_FK]
    assert report["ok"] is False

    caplog.set_level(logging.INFO, logger="superset_ownership.lifecycle")
    report = lifecycle.startup_check()
    assert report["ok"] is False
    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and r.name == "superset_ownership.lifecycle"
    ]
    assert len(warnings) == 1
    assert OWNER_FK in warnings[0]
    assert "superset ownership db upgrade" in warnings[0]
    assert not any("startup check ok" in r.getMessage() for r in caplog.records)

    # ab_user appears; the next upgrade's re-check adds the constraint.
    _create_ab_user(uri)
    migrate.upgrade("head", uri)
    report = lifecycle.check_consistency()
    assert report["constraints_missing"] == []
    assert report["ok"] is True


def test_check_does_not_require_the_constraints_below_0003(tmp_path, monkeypatch):
    """A deliberate downgrade to 0002 is a database without foreign keys by
    choice; presence is reported, nothing is required. The tenant column
    (0004) is different: under OWNERSHIP_PUBLIC_SCOPE=tenant its absence
    means the tenant rule cannot be decided at all (the module cannot read
    its rows), so `check` reports `schema_behind` and fails -- and passes
    again under instance
    scope, where the column decides nothing."""
    uri = _uri(tmp_path)
    _make_fake_superset_world(uri)
    migrate.upgrade("head", uri)
    migrate.downgrade("0002_ownership_outbox", uri)
    lifecycle = _consistency_on(monkeypatch, uri)

    monkeypatch.delenv("OWNERSHIP_PUBLIC_SCOPE", raising=False)
    report = lifecycle.check_consistency()
    assert (report["owner_fk_present"], report["share_fk_present"]) == (False, False)
    assert report["constraints_missing"] == []
    assert report["schema_behind"] == "0004_ownership_object_tenant"
    assert report["ok"] is False

    monkeypatch.setenv("OWNERSHIP_PUBLIC_SCOPE", "instance")
    report = lifecycle.check_consistency()
    assert "schema_behind" not in report
    assert report["ok"] is True


# ------------------------------------------- 11. the user-delete audit record


def _user_delete_world(tmp_path, monkeypatch):
    """The chain at head over a stand-in ab_user mapped as a declarative
    class, a session with the guard's after-commit emission wired, and the
    audit sink captured. What `guard.install()` does against FAB's User,
    done against the stand-in."""
    from sqlalchemy import event
    from sqlalchemy.orm import declarative_base, Session
    from superset_ownership import audit, guard

    uri = _uri(tmp_path)
    _make_fake_superset_world(uri)
    migrate.upgrade("head", uri)
    _seed_governed(uri)  # ben (11) owns chart 56, ada (12) owns dashboard 5

    class User(declarative_base()):
        __tablename__ = "ab_user"
        id = sa.Column(sa.Integer, primary_key=True)
        username = sa.Column(sa.String(64))

    assert guard.install_user_delete_listener(User) is True
    assert guard.install_user_delete_listener(User) is False, "idempotent"
    engine = _engine(uri)
    session = Session(engine)
    event.listen(session, "after_commit", guard._after_commit)
    event.listen(session, "after_soft_rollback", guard._after_rollback)
    emitted: list[dict] = []
    monkeypatch.setattr(audit, "emit_record", emitted.append)
    return uri, User, session, emitted


def test_hard_deleting_a_user_through_the_orm_leaves_one_event_per_owned_object(
    tmp_path, monkeypatch, caplog
):
    from superset_ownership import audit

    uri, user_model, session, emitted = _user_delete_world(tmp_path, monkeypatch)
    with session.begin():
        session.execute(
            sa.text(
                "INSERT INTO ownership_object "
                "(asset_type, object_id, object_uuid, owner_user_id, visibility) "
                "VALUES ('chart', 57, 'u-57', 11, 'public')"
            )
        )
    caplog.set_level(logging.INFO, logger="superset_ownership.guard")

    ben = session.get(user_model, 11)
    session.delete(ben)
    session.flush()
    assert emitted == [], "nothing is reported before the delete commits"
    session.commit()

    assert [e["event"] for e in emitted] == [audit.OWNER_REMOVED_BY_USER_DELETE] * 2
    objects = [
        (e["object"]["type"], e["object"]["id"], e["object"]["uuid"]) for e in emitted
    ]
    assert objects == [("chart", 56, "u-56"), ("chart", 57, "u-57")]
    assert all(e["before"] == {"owner_user_id": 11} for e in emitted)
    assert all(e["after"] == {"owner_user_id": None} for e in emitted)
    assert all(e["actor"]["superset_id"] is None for e in emitted), "no request"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "user 11 un-owns 2 object(s)" in warnings[0]
    # ada's dashboard is not ben's and gets no event; the user is gone.
    assert session.get(user_model, 11) is None


def test_a_rolled_back_user_delete_reports_nothing(tmp_path, monkeypatch):
    uri, user_model, session, emitted = _user_delete_world(tmp_path, monkeypatch)

    session.delete(session.get(user_model, 11))
    session.flush()
    session.rollback()

    assert emitted == []
    assert session.get(user_model, 11) is not None
    session.commit()
    assert emitted == [], "and nothing leaks into the next commit"


def test_deleting_a_user_who_owns_nothing_is_silent(tmp_path, monkeypatch, caplog):
    uri, user_model, session, emitted = _user_delete_world(tmp_path, monkeypatch)
    caplog.set_level(logging.INFO, logger="superset_ownership.guard")

    session.delete(session.get(user_model, 1))  # admin owns nothing in the seed
    session.commit()

    assert emitted == []
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def _statements_on(engine) -> list[str]:
    """Every statement the engine executes from now on, lowercased."""
    from sqlalchemy import event

    seen: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _record(conn, cursor, statement, parameters, context, executemany):
        seen.append(" ".join(statement.lower().split()))

    return seen


def test_deleting_a_user_while_the_ownership_table_is_absent_still_deletes(
    tmp_path, monkeypatch, caplog
):
    """The listener is installed whenever the module is wired, and the
    tables can be absent then: OWNERSHIP_AUTO_MIGRATE=false before the
    operator's first `superset ownership db upgrade`, and every process
    still running after `superset ownership teardown`. On PostgreSQL a
    SELECT against the missing table would abort the flush's transaction
    and the DELETE that follows would fail (catching the exception does
    not undo that), so the table's presence is checked with the inspector
    and the SELECT is never issued. This SQLite test pins that shape --
    no statement reads `ownership_object` -- plus the outcome: the user is
    deleted, nothing is emitted, one INFO line says why. The PostgreSQL
    test below runs the real thing."""
    uri, user_model, session, emitted = _user_delete_world(tmp_path, monkeypatch)
    with _engine(uri).begin() as conn:
        conn.execute(sa.text("DROP TABLE ownership_share"))
        conn.execute(sa.text("DROP TABLE ownership_object"))
    caplog.set_level(logging.DEBUG, logger="superset_ownership.guard")
    statements = _statements_on(session.get_bind())

    session.delete(session.get(user_model, 11))
    session.commit()

    assert session.get(user_model, 11) is None, "the user is deleted"
    assert emitted == []
    assert not [st for st in statements if "from ownership_object" in st], (
        "no statement was issued against the absent table"
    )
    guard_lines = [
        (r.levelno, r.getMessage(), r.exc_info)
        for r in caplog.records
        if r.name == "superset_ownership.guard"
    ]
    assert guard_lines == [
        (
            logging.INFO,
            "superset_ownership: user 11 is being deleted while ownership_object "
            "does not exist (the ownership chain has not run, or was torn down); "
            "nothing to record",
            None,
        )
    ]


def test_deleting_a_user_while_the_ownership_table_is_absent_on_postgresql():
    """The real failure mode: PostgreSQL aborts the transaction on the first
    failed statement, so the user delete used to fail with
    InFailedSqlTransaction whenever `ownership_object` was absent. A
    stand-in mapped `ab_user` in the throwaway database, the ownership
    tables dropped, `session.delete(user); commit()`: the user is gone."""
    from sqlalchemy.orm import declarative_base, Session
    from superset_ownership import guard

    uri = _throwaway_postgresql_uri()
    _reset_throwaway_postgresql(uri)
    _create_ab_user(uri)
    migrate.upgrade("head", uri)
    with _engine(uri).begin() as conn:
        conn.execute(sa.text("INSERT INTO ab_user VALUES (11, 'ben')"))
        conn.execute(sa.text("DROP TABLE ownership_share, ownership_object"))

    class User(declarative_base()):
        __tablename__ = "ab_user"
        id = sa.Column(sa.Integer, primary_key=True)
        username = sa.Column(sa.String(64))

    guard.install_user_delete_listener(User)
    engine = _engine(uri)
    try:
        session = Session(engine)
        session.delete(session.get(User, 11))
        session.commit()  # used to raise InternalError (InFailedSqlTransaction)
        assert session.get(User, 11) is None
        session.close()
        with engine.connect() as conn:
            assert conn.execute(sa.text("SELECT count(*) FROM ab_user")).scalar() == 0
    finally:
        engine.dispose()


def test_a_failing_read_of_owned_objects_does_not_stop_the_delete(
    tmp_path, monkeypatch, caplog
):
    """The table is present, so the inspector's guard passes, and the SELECT
    itself fails (here the owner column renamed under the listener; on
    PostgreSQL a role without SELECT on the table, or a concurrent teardown
    between the inspector's read and the SELECT). The SELECT runs under a
    SAVEPOINT so the failure is rolled back to it and the flush's
    transaction stays usable: the user is deleted, nothing is emitted, one
    WARNING with the traceback says the read failed. SQLite would survive
    the failed statement without the savepoint, so the shape is pinned
    here -- SAVEPOINT before the read, ROLLBACK TO it after -- and the
    PostgreSQL test below runs the failure mode for real."""
    uri, user_model, session, emitted = _user_delete_world(tmp_path, monkeypatch)
    with _engine(uri).begin() as conn:
        conn.execute(
            sa.text(
                "ALTER TABLE ownership_object RENAME COLUMN owner_user_id TO owner_uid"
            )
        )
    caplog.set_level(logging.DEBUG, logger="superset_ownership.guard")
    statements = _statements_on(session.get_bind())

    session.delete(session.get(user_model, 11))
    session.commit()

    assert session.get(user_model, 11) is None, "the user is deleted"
    assert emitted == []
    reads = [i for i, st in enumerate(statements) if "from ownership_object" in st]
    assert len(reads) == 1
    (read,) = reads
    assert statements[read - 1].startswith("savepoint "), statements
    assert statements[read + 1].startswith("rollback to savepoint "), statements
    assert any(st.startswith("delete from ab_user") for st in statements[read:])
    guard_lines = [
        (r.levelno, r.getMessage(), r.exc_info is not None)
        for r in caplog.records
        if r.name == "superset_ownership.guard"
    ]
    assert guard_lines == [
        (
            logging.WARNING,
            "superset_ownership: could not read owned objects of user 11",
            True,
        )
    ]


def test_a_failing_read_of_owned_objects_does_not_stop_the_delete_on_postgresql():
    """The real failure mode behind the savepoint: PostgreSQL aborts the
    transaction on the first failed statement, so with the table present
    and the SELECT failing (the owner column renamed here; a role without
    SELECT on the table in the review's probe) the DELETE that followed
    used to fail with InFailedSqlTransaction and the user stayed. With
    the read under a SAVEPOINT the user is gone."""
    from sqlalchemy.orm import declarative_base, Session
    from superset_ownership import guard

    uri = _throwaway_postgresql_uri()
    _reset_throwaway_postgresql(uri)
    _create_ab_user(uri)
    migrate.upgrade("head", uri)
    with _engine(uri).begin() as conn:
        conn.execute(sa.text("INSERT INTO ab_user VALUES (11, 'ben'), (12, 'ada')"))
        conn.execute(
            sa.text(
                "ALTER TABLE ownership_object RENAME COLUMN owner_user_id TO owner_uid"
            )
        )

    class User(declarative_base()):
        __tablename__ = "ab_user"
        id = sa.Column(sa.Integer, primary_key=True)
        username = sa.Column(sa.String(64))

    guard.install_user_delete_listener(User)
    engine = _engine(uri)
    try:
        session = Session(engine)
        session.delete(session.get(User, 11))
        session.commit()  # used to raise InternalError (InFailedSqlTransaction)
        assert session.get(User, 11) is None
        session.close()
        with engine.connect() as conn:
            assert conn.execute(sa.text("SELECT id FROM ab_user")).scalars().all() == [
                12
            ]
    finally:
        engine.dispose()


def test_a_core_delete_of_a_user_fires_no_orm_event(tmp_path, monkeypatch):
    """Documented limit: `DELETE FROM ab_user` bypasses the mapper, so only
    the constraint's SET NULL happens (here, with SQLite told to enforce);
    `check`/`reconcile` are the record then."""
    uri, user_model, session, emitted = _user_delete_world(tmp_path, monkeypatch)
    session.close()

    with _enforcing(uri) as conn:
        conn.execute(sa.text("DELETE FROM ab_user WHERE id = 11"))
        conn.commit()

    assert emitted == []
    owners = {(r[1], r[2]): r[4] for r in _rows(uri, "ownership_object")}
    assert owners[("chart", 56)] is None


# ------------------------------------------ 12. the share insert under the FK


def _enforcing_stand_in(uri: str):
    """`service._db()` stand-in whose every connection enforces foreign keys."""
    from types import SimpleNamespace

    from sqlalchemy import event
    from sqlalchemy.orm import Session

    engine = _engine(uri)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    return SimpleNamespace(session=Session(engine), engine=engine)


def test_a_share_whose_object_row_vanished_is_refused_and_queues_nothing(
    tmp_path, monkeypatch
):
    """Probe G's shape: the object row is deleted between the route's lookup
    and the insert; the share -> object constraint refuses the insert. The
    savepoint pattern used to read that as the lost unique race, UPDATE
    zero rows, queue a store tuple and return True."""
    import pytest
    from superset_ownership import service

    uri = _uri(tmp_path)
    _make_fake_superset_world(uri)
    migrate.upgrade("head", uri)
    _seed_governed(uri)
    stand_in = _enforcing_stand_in(uri)
    monkeypatch.setattr(service, "_db", lambda: stand_in)
    queued: list[tuple] = []
    monkeypatch.setattr(
        service.outbox, "write_tuple", lambda *a: queued.append(a) or True
    )

    with pytest.raises(service.ShareTargetGoneError) as info:
        service.add_share("chart", 4040, "u-4040", "user:1", "viewer")

    assert (info.value.asset_type, info.value.object_id) == ("chart", 4040)
    assert queued == [], "no store tuple for an object with no row"
    stand_in.session.rollback()
    assert {(r[1], r[2]) for r in _rows(uri, "ownership_share")} == {
        ("chart", 56),
        ("dashboard", 5),
    }, "nothing written"


def test_losing_the_unique_race_still_updates_the_role(tmp_path, monkeypatch):
    """The other administrator's insert landed between this request's
    lookup and its insert: the row is there, its role is updated, nothing
    is raised. Driven at the savepoint helper, which is the seam."""
    from superset_ownership import service

    uri = _uri(tmp_path)
    _make_fake_superset_world(uri)
    migrate.upgrade("head", uri)
    _seed_governed(uri)  # chart 56 already shared with user:1 as viewer
    stand_in = _enforcing_stand_in(uri)

    service._insert_share_row(stand_in, "chart", 56, "user:1", "editor")
    stand_in.session.commit()

    roles = {(r[1], r[2], r[3]): r[4] for r in _rows(uri, "ownership_share")}
    assert roles[("chart", 56, "user:1")] == "editor"


def test_an_integrity_error_that_is_neither_race_is_raised_not_called_gone(
    tmp_path, caplog
):
    """The arbiter's third outcome. An IntegrityError the driver cannot
    classify (here a real NOT NULL on `role`, which SQLite reports without
    a constraint name the classifier knows) with no row present is
    neither the unique race nor the object foreign key: it is a defect,
    re-raised as the IntegrityError it is with an ERROR line, not
    answered 404 "no longer exists". Without the re-read this used to fall
    through to the role UPDATE and return as if written."""
    import pytest
    from sqlalchemy.exc import IntegrityError
    from superset_ownership import service

    uri = _uri(tmp_path)
    _make_fake_superset_world(uri)
    migrate.upgrade("head", uri)
    _seed_governed(uri)
    stand_in = _enforcing_stand_in(uri)
    caplog.set_level(logging.INFO, logger="superset_ownership.service")

    with pytest.raises(IntegrityError) as info:
        service._insert_share_row(stand_in, "chart", 56, "user:2", None)

    assert not isinstance(info.value, service.ShareTargetGoneError)
    assert "NOT NULL" in str(info.value.orig)
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "chart 56 -> user:2" in errors[0]
    assert "neither the unique race nor the object foreign key" in errors[0]
    stand_in.session.rollback()
    assert ("chart", 56, "user:2") not in {
        (r[1], r[2], r[3]) for r in _rows(uri, "ownership_share")
    }, "nothing written"


def test_integrity_kind_reads_the_driver_where_it_names_the_constraint():
    """psycopg2 gives a SQLSTATE and `diag.constraint_name`; SQLite gives a
    message. Both are classified; anything else is 'unknown' and left to
    the re-read."""
    from types import SimpleNamespace

    from sqlalchemy.exc import IntegrityError
    from superset_ownership import service

    def pg(code, name):
        orig = SimpleNamespace(pgcode=code, diag=SimpleNamespace(constraint_name=name))
        return IntegrityError("INSERT ...", {}, orig)

    assert service._integrity_kind(pg("23503", SHARE_FK)) == ("foreign_key", SHARE_FK)
    assert service._integrity_kind(pg("23505", "uq_ownership_share_subject")) == (
        "unique",
        "uq_ownership_share_subject",
    )
    assert service._integrity_kind(
        IntegrityError("INSERT ...", {}, Exception("FOREIGN KEY constraint failed"))
    ) == ("foreign_key", None)
    assert service._integrity_kind(
        IntegrityError(
            "INSERT ...",
            {},
            Exception(
                "UNIQUE constraint failed: ownership_share.asset_type, "
                "ownership_share.object_id, ownership_share.subject"
            ),
        )
    ) == (
        "unique",
        "ownership_share.asset_type, ownership_share.object_id, "
        "ownership_share.subject",
    )
    assert service._integrity_kind(
        IntegrityError("INSERT ...", {}, Exception("NOT NULL constraint failed"))
    ) == ("unknown", None)

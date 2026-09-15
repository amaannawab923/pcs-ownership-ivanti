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
"""
SQLAlchemy Core table definitions for the ownership module.

Deliberately NOT flask_appbuilder Models and NOT on Superset's shared
MetaData: this module owns its own `metadata`, which is what lets its
parallel Alembic chain (migrations/, migrate.py) manage these tables in
complete isolation from Superset's own chain. Schema changes are revisions
on that chain; create_tables() runs it to head.

Foreign keys (decision record qa/reviews/decision-owner-fk.md, revision 0003):

  * ownership_share -> ownership_object is declared below: both tables are
    on this MetaData, so a ForeignKeyConstraint is legitimate, and declaring
    it keeps create_all()/drop_all() ordering right.
  * ownership_object.owner_user_id -> ab_user.id is NOT declared here. A
    ForeignKey to a table on another MetaData raises NoReferencedTableError;
    the constraint exists in the database (hand-written DDL in revision
    0003, ON DELETE SET NULL, see constraints.py) but not on this Table.
  * ownership_object.object_id has no foreign key to dashboards/slices, by
    decision, not omission: `after_asset_delete` and `check`/`reconcile`
    keep that side consistent. The outbox references nothing; it outlives
    the object on purpose.
"""
from __future__ import annotations

import logging

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

metadata = MetaData()

ownership_object = Table(
    "ownership_object",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("asset_type", String(32), nullable=False),
    Column("object_id", Integer, nullable=False),
    Column("object_uuid", String(64), nullable=True),
    Column("owner_user_id", Integer, nullable=True),
    Column("visibility", String(16), nullable=False, server_default="private"),
    # Mirror of the store's `tenant` tuple (revision 0004): which tenant the
    # object belongs to, so "public within the tenant" is decided from the
    # row with no store call. NULL = outside every tenant / not yet stamped.
    Column("tenant_guid", String(64), nullable=True, index=True),
    UniqueConstraint("asset_type", "object_id", name="uq_ownership_object_type_id"),
)

# Local mirror of OpenFGA share tuples, kept only so the REST API can list
# "who is this shared with" without needing OpenFGA's ListUsers API. The
# actual access-control decision always goes through OpenFGA / the sentinel
# viewer, never this table.
ownership_share = Table(
    "ownership_share",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("asset_type", String(32), nullable=False),
    Column("object_id", Integer, nullable=False),
    Column("subject", String(128), nullable=False),  # e.g. "user:3"
    Column("role", String(32), nullable=False),  # e.g. "viewer"
    UniqueConstraint(
        "asset_type", "object_id", "subject", name="uq_ownership_share_subject"
    ),
    # A share row cannot outlive its object row. Intra-module, so declared;
    # revision 0003 adds it to installs that predate it.
    ForeignKeyConstraint(
        ["asset_type", "object_id"],
        ["ownership_object.asset_type", "ownership_object.object_id"],
        name="fk_ownership_share_object_ownership_object",
        ondelete="CASCADE",
    ),
)

# Transactional outbox for authorization-store writes (SOW §3 / spec §8.1).
#
# A share, transfer, visibility change or delete writes its local effect AND
# one row here in the SAME transaction. A background drain (outbox.py) then
# delivers each row to OpenFGA with retries. That keeps the OpenFGA call off
# the user's request path -- the request never waits on, or fails because of,
# the authorization store -- while guaranteeing the write cannot be lost: the
# row is committed with the change it describes, so there is no window in
# which the change exists but the instruction to sync it does not.
#
# `status` lifecycle: pending -> claimed -> done | dead. `claimed` is held by a
# drain in flight. `dead` means the store rejected it or retries ran out; it is
# never deleted, so an operator can see and replay it.
ownership_outbox = Table(
    "ownership_outbox",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    # write | delete | set_tenant | purge_object
    Column("op", String(16), nullable=False),
    Column("subject", String(256), nullable=True),  # FGA "user" field
    Column("relation", String(64), nullable=True),
    Column("object", String(128), nullable=False),  # "<asset_type>:<uuid>"
    Column("status", String(16), nullable=False, server_default="pending"),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("last_error", Text, nullable=True),
    # Set while a drain holds the row (status='claimed'); a claim older than
    # the lease is reaped. This is what lets several drains run at once.
    # The drain's outcome writes are guarded by `claimed_at = <the value this
    # drain wrote>`, so the column must keep the microseconds Python sends:
    # PostgreSQL `timestamp` (6) and SQLite (text) do; a MySQL `DATETIME`
    # without fractional precision would truncate and every settle would be
    # discarded as "not my claim". Not a supported target; noted so it is
    # never silently made one.
    Column("claimed_at", DateTime, nullable=True),
    # Written client-side in UTC by enqueue(); the server default is only a
    # safety net for rows inserted some other way.
    Column("created_on", DateTime, nullable=False, server_default=func.now()),
    Column("updated_on", DateTime, nullable=True),
    Column("done_on", DateTime, nullable=True),
)


def create_tables(engine: Engine) -> None:
    """Ensure the schema by running the module's Alembic chain to head.

    Historically ``metadata.create_all()``. It now delegates to the parallel
    Alembic chain (migrate.py / migrations/) so the schema has a real,
    versioned history and the SOW's "own migration chain" holds. The root
    revision is idempotent, so databases created by the old create_all() path
    are adopted in place -- and gain the indexes create_all() never made.

    Kept under the old name so every existing call site (the startup hook,
    backfill) is unchanged. Gated by OWNERSHIP_AUTO_MIGRATE (default on): the
    canonical PRODUCTION path is the explicit deploy step
    `superset ownership db upgrade` after `superset db upgrade`, so operators
    can set OWNERSHIP_AUTO_MIGRATE=false and stop workers migrating at boot.
    """
    from superset_ownership import settings

    if not settings.get("OWNERSHIP_AUTO_MIGRATE", True, settings.as_legacy_bool):
        logger.info("superset_ownership: auto-migrate disabled; run `superset ownership db upgrade`")
        return

    from superset_ownership import migrate

    uri = engine.url.render_as_string(hide_password=False)
    migrate.upgrade("head", database_uri=uri)
    logger.info("superset_ownership: schema at %s", migrate.current(uri))

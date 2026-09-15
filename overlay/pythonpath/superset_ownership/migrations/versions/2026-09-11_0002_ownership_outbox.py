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
"""ownership_outbox: transactional outbox for authorization-store writes.

Revision ID: 0002_ownership_outbox
Revises: 0001_object_ownership

Same idempotent style as 0001 (inspect before create) so a deploy pipeline can
call `superset ownership db upgrade` unconditionally.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_ownership_outbox"
down_revision = "0001_object_ownership"
branch_labels = None
depends_on = None


def _insp():
    return sa.inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return _insp().has_table(name)


def _has_index(table: str, index: str) -> bool:
    return any(ix["name"] == index for ix in _insp().get_indexes(table))


def upgrade() -> None:
    if not _has_table("ownership_outbox"):
        op.create_table(
            "ownership_outbox",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("op", sa.String(16), nullable=False),
            sa.Column("subject", sa.String(256), nullable=True),
            sa.Column("relation", sa.String(64), nullable=True),
            sa.Column("object", sa.String(128), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("claimed_at", sa.DateTime(), nullable=True),
            sa.Column(
                "created_on", sa.DateTime(), nullable=False, server_default=sa.func.now()
            ),
            sa.Column("updated_on", sa.DateTime(), nullable=True),
            sa.Column("done_on", sa.DateTime(), nullable=True),
        )
    # NOTE: a database already stamped at this revision will NOT re-run this
    # function -- Alembic only applies revisions it has not recorded. This
    # revision was amended before merge (claimed_at was added); no deployment
    # had applied the earlier cut. A development database that had must be
    # brought forward with `superset ownership db downgrade 0001_object_ownership`
    # then `upgrade`, not by editing this file further.
    # The drain selects `status = 'pending' ORDER BY id`; this is its index.
    if not _has_index("ownership_outbox", "ix_ownership_outbox_status_id"):
        op.create_index("ix_ownership_outbox_status_id", "ownership_outbox", ["status", "id"])
    # The read path asks "is a revocation for this object still undelivered?"
    # on every access decision (outbox.has_pending_revocation); this is its
    # index.
    if not _has_index("ownership_outbox", "ix_ownership_outbox_object"):
        op.create_index("ix_ownership_outbox_object", "ownership_outbox", ["object"])


def downgrade() -> None:
    """Dropping the outbox with undelivered rows discards authorization writes
    that were promised to the store: a revocation that never happens. Refuse
    unless the operator says so explicitly."""
    import os

    if _has_table("ownership_outbox"):
        undelivered = op.get_bind().execute(
            sa.text("SELECT count(*) FROM ownership_outbox WHERE status <> 'done'")
        ).scalar()
        if undelivered and os.environ.get("OWNERSHIP_OUTBOX_FORCE_DOWNGRADE") != "1":
            raise RuntimeError(
                f"ownership_outbox has {undelivered} undelivered row(s); draining them first "
                "(`superset ownership outbox drain`) or set OWNERSHIP_OUTBOX_FORCE_DOWNGRADE=1 "
                "to discard them knowingly."
            )
        op.drop_table("ownership_outbox")

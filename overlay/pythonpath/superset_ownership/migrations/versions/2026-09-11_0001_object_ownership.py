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
"""object_ownership: root revision of the ownership module's isolated chain.

Revision ID: 0001_object_ownership
Revises: None  -- a fresh, independent root. NOT a merge into Superset's chain.

IDEMPOTENT BY DESIGN, at the level of each table AND each index. Instances that
ran the prototype already have the two tables, created by
``metadata.create_all()`` before this chain existed -- and created WITHOUT the
indexes below. Inspecting per object (not just per table) means an adopted
install converges to the full schema instead of silently keeping a partial
one. This revision is therefore correct on:
  - a fresh database          creates tables + indexes, stamps the version table
  - a prototype-era database  skips the tables, ADDS the missing indexes, stamps
  - a re-run                  no-op
That is also what makes it safe for a deploy pipeline to call unconditionally
after every ``superset db upgrade``.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_object_ownership"
down_revision = None
branch_labels = None
depends_on = None


def _insp():
    return sa.inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return _insp().has_table(name)


def _has_index(table: str, index: str) -> bool:
    return any(ix["name"] == index for ix in _insp().get_indexes(table))


def _ensure_index(index: str, table: str, columns: list[str]) -> None:
    if not _has_index(table, index):
        op.create_index(index, table, columns)


def upgrade() -> None:
    if not _has_table("ownership_object"):
        op.create_table(
            "ownership_object",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("asset_type", sa.String(32), nullable=False),
            sa.Column("object_id", sa.Integer(), nullable=False),
            sa.Column("object_uuid", sa.String(64), nullable=True),
            # NULL = unowned (owner deactivated / never recorded). No declared
            # FK to ab_user: cross-MetaData, see migrations/env.py.
            sa.Column("owner_user_id", sa.Integer(), nullable=True),
            sa.Column(
                "visibility", sa.String(16), nullable=False, server_default="private"
            ),
            sa.UniqueConstraint(
                "asset_type", "object_id", name="uq_ownership_object_type_id"
            ),
        )
    # Indexes are ensured independently of the table so a prototype-era install
    # (table exists, indexes don't) still gets them.
    _ensure_index("ix_ownership_object_visibility", "ownership_object", ["visibility"])
    _ensure_index("ix_ownership_object_owner", "ownership_object", ["owner_user_id"])

    if not _has_table("ownership_share"):
        op.create_table(
            "ownership_share",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("asset_type", sa.String(32), nullable=False),
            sa.Column("object_id", sa.Integer(), nullable=False),
            sa.Column("subject", sa.String(128), nullable=False),
            sa.Column("role", sa.String(32), nullable=False),
            sa.UniqueConstraint(
                "asset_type", "object_id", "subject", name="uq_ownership_share_subject"
            ),
        )
    _ensure_index("ix_ownership_share_object", "ownership_share", ["asset_type", "object_id"])


def downgrade() -> None:
    # The deliberate uninstall path (feature removal / tenant purge).
    if _has_table("ownership_share"):
        op.drop_table("ownership_share")
    if _has_table("ownership_object"):
        op.drop_table("ownership_object")

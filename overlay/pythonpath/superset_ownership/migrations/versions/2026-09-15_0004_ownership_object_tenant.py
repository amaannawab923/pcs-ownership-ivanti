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
"""ownership_object_tenant: mirror each object's tenant on its ownership row.

Revision ID: 0004_ownership_object_tenant
Revises: 0003_ownership_foreign_keys

`public` means public WITHIN the object's tenant (OWNERSHIP_PUBLIC_SCOPE,
default "tenant"). The read gate decides that on every public access, and
it must not depend on the authorization store being reachable -- public
objects have always been zero-store-call and must stay readable through a
store outage -- so the object's tenant is mirrored here from the store's
`tenant` tuple, stamped by the same code paths that write the tuple
(creation, backfill, backfill-tenants, transfer, claim).

Nullable: a row that predates this revision, or an object outside every
tenant (created by an admin with no tenant role, or not yet backfilled),
has no tenant. `superset ownership backfill-tenants` fills it from the
store; until then such an object, once public, is readable by its owner
and by callers outside every tenant only -- by nobody in a tenant (fail
closed; `check` reports the ones a backfill can fill in).

Same idempotent style as 0001-0003 (inspect before alter).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_ownership_object_tenant"
down_revision = "0003_ownership_foreign_keys"
branch_labels = None
depends_on = None


def _insp():
    return sa.inspect(op.get_bind())


def _has_column(table: str, column: str) -> bool:
    return any(c["name"] == column for c in _insp().get_columns(table))


def _has_index(table: str, index: str) -> bool:
    return any(ix["name"] == index for ix in _insp().get_indexes(table))


def upgrade() -> None:
    if not _has_column("ownership_object", "tenant_guid"):
        op.add_column(
            "ownership_object", sa.Column("tenant_guid", sa.String(64), nullable=True)
        )
    if not _has_index("ownership_object", "ix_ownership_object_tenant_guid"):
        op.create_index(
            "ix_ownership_object_tenant_guid", "ownership_object", ["tenant_guid"]
        )


def downgrade() -> None:
    if _has_index("ownership_object", "ix_ownership_object_tenant_guid"):
        op.drop_index("ix_ownership_object_tenant_guid", table_name="ownership_object")
    if _has_column("ownership_object", "tenant_guid"):
        op.drop_column("ownership_object", "tenant_guid")

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
"""ownership_foreign_keys: the owner and share foreign keys (spec §4.1/§4.3).

Revision ID: 0003_ownership_foreign_keys
Revises: 0002_ownership_outbox

Decision record: qa/reviews/decision-owner-fk.md. Two constraints, both
idempotent (inspect before create, like 0001/0002):

  fk_ownership_object_owner_user_id_ab_user
      ownership_object.owner_user_id -> ab_user.id  ON DELETE SET NULL
      A deleted user leaves the object unowned -- the state the module
      already handles -- instead of a dangling id. Cross-MetaData, so it is
      the hand-written DDL migrations/env.py reserves for this case, and it
      references a table this chain does not own. On the deployed stack
      ab_user exists when this runs (Flask-AppBuilder creates the ab_*
      tables inside appbuilder.init_app, before FLASK_APP_MUTATOR runs
      this chain, and before the body of any `superset ownership db`
      command), so a fresh `superset db upgrade` creates the constraint
      right here, and so does `superset ownership db upgrade` run by hand
      first. When ab_user is absent -- reachable only outside the CLI: a
      direct migrate.upgrade() from an embedding or a custom entrypoint
      that creates no application, or FAB_CREATE_DB=False (under which
      Superset's own upgrade fails on ab_user anyway) -- the constraint is
      skipped (INFO), this revision is still recorded, and
      migrate.upgrade's re-check adds it on the next run, warning until
      then.

  fk_ownership_share_object_ownership_object
      ownership_share(asset_type, object_id)
        -> ownership_object(asset_type, object_id)  ON DELETE CASCADE
      A share row can no longer outlive its object row.

NOT added, on purpose: ownership_object.object_id -> dashboards/slices.
The decision record has the evidence; in short, one column cannot
reference two tables, a cascade would only duplicate the delete hook's row
cleanup while leaving the store tuples behind, and a restrict would block
every retention purge.

Locks: ADD CONSTRAINT validates the existing rows under SHARE ROW EXCLUSIVE
on both tables, so on PostgreSQL an UPDATE of ab_user (FAB's last_login
write on login) waits for the scan -- milliseconds at the current sizes.
The two-transaction NOT VALID / VALIDATE CONSTRAINT idiom for a large
table is not something one revision can do; constraints.py says how to
start it by hand if the table ever grows to need it, and this revision
(or the re-check after it) finishes it: a constraint found present but
NOT VALID has its rows repaired and is VALIDATEd here. Concurrent runs
are serialised with an advisory lock (constraints.py); the chain's own
revision transitions are serialised too, by a separate chain-level advisory
lock in migrations/env.py's run_migrations_online (issue #89).

Rows that would violate a constraint are repaired first, exactly as
`reconcile` would repair them: an owner_user_id naming a user that no
longer exists is set to NULL, a share row with no object row is deleted.
Both counts are logged once the constraint is in place. On PostgreSQL the
repair and the ADD CONSTRAINT are one transaction: a failed add rolls the
repair back. A downgrade drops the constraints only; it does not restore
an owner the repair set to NULL or a share it removed.
"""

from __future__ import annotations

from superset_ownership import constraints

revision = "0003_ownership_foreign_keys"
down_revision = "0002_ownership_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Owner first: on SQLite this rebuilds ownership_object, which the share
    # constraint then references by name.
    constraints.ensure_owner_fk(deferred=True)
    constraints.ensure_share_fk()


def downgrade() -> None:
    """Dropping a constraint discards no rows, so unlike 0002 there is
    nothing to guard: the tables and their data are exactly as they were,
    only unenforced. Idempotent: absent constraints are skipped."""
    constraints.drop_fk(constraints.SHARE_FK, "ownership_share")
    constraints.drop_fk(constraints.OWNER_FK, "ownership_object")

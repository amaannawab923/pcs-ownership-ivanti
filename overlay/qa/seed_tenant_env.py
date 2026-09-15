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
"""Make this instance a multi-tenant environment with NO tenantless object.

Ivanti runs one cross-tenant instance and keeps tenants apart at the data
layer, so every dataset belongs to a tenant and therefore every chart and
dashboard does too. Locally only two datasets were ever bound to a tenant,
which left most objects underivable and made the backfill look far weaker
than it is. This closes that gap so the local environment matches the real
one's invariant: NOTHING is tenantless.

What it does, idempotently:
  1. Partitions EVERY dataset between the two tenants (deterministic, and
     the two historically-bound datasets keep their tenant).
  2. Binds each dataset to its tenant BOTH ways -- an RLS filter bound to the
     tenant role (Ivanti's stated mechanism, and the path `_tenant_datasources`
     prefers) and a `datasource_access` grant to the same role.
  3. Ensures EVERY tenant has at least one administrator. A tenant without one
     has nobody to inherit its unowned objects, so the backfill can derive the
     tenant correctly and still leave everything unowned.

Afterwards run the backfill to stamp tenants and assign owners:
    python -c "from superset_ownership.backfill import run; run()"

Run inside the container:
    docker cp qa/seed_tenant_env.py pcs617-superset-light-1:/tmp/
    docker exec -e OWNERSHIP_ENABLED=true -e OWNERSHIP_AUTHORIZER=openfga \
        pcs617-superset-light-1 python /tmp/seed_tenant_env.py
"""

from __future__ import annotations

TENANT_A = "a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40"
TENANT_B = "b7f28e5a-9c14-4d6b-a2f0-5e3d8c1a6b92"

# Kept on their existing tenant so previously-seeded ownership stays coherent.
PINNED = {"video_game_sales": TENANT_A, "wb_health_population": TENANT_B}

# One administrator per tenant, from the identity seed.
ADMINS = {
    TENANT_A: "3f0a91c7-2d84-4e63-9b15-7c4e8a2f6d31",  # Ada
    TENANT_B: "9d7e35a1-8c62-4b04-a7f1-3d5e9b2c8a76",  # Cleo
}


def run() -> None:
    from superset import db, security_manager as sm
    from superset.connectors.sqla.models import (
        RowLevelSecurityFilter,
        SqlaTable,
    )
    from superset.subjects.models import Subject
    from superset.subjects.types import SubjectType
    from superset.utils.core import RowLevelSecurityFilterType

    from superset_ownership import fga
    from superset_ownership.identity import tenant_administrator_group

    tables = db.session.query(SqlaTable).order_by(SqlaTable.id).all()

    # ---- 1. every dataset gets a tenant -------------------------------------
    assignment: dict[int, str] = {}
    unpinned = [t for t in tables if t.table_name not in PINNED]
    for t in tables:
        if t.table_name in PINNED:
            assignment[t.id] = PINNED[t.table_name]
    # Deterministic split of the rest: alternate by position in id order, so a
    # re-run assigns the same dataset to the same tenant.
    for i, t in enumerate(unpinned):
        assignment[t.id] = TENANT_A if i % 2 == 0 else TENANT_B

    made = {"grants": 0, "rls": 0, "admins": 0, "tenant_tuples": 0}

    for t in tables:
        tenant = assignment[t.id]
        role = sm.find_role(f"tenant_{tenant}") or sm.add_role(f"tenant_{tenant}")
        db.session.commit()

        # ---- 2a. datasource_access grant -----------------------------------
        pvm = sm.find_permission_view_menu("datasource_access", t.perm)
        if pvm is None:
            pvm = sm.add_permission_view_menu("datasource_access", t.perm)
        if pvm is not None and pvm not in (role.permissions or []):
            sm.add_permission_role(role, pvm)
            made["grants"] += 1

        # ---- 2b. RLS filter bound to the tenant role -----------------------
        # A Subject wrapping the role is what rls_filter_subjects points at.
        subject = (
            db.session.query(Subject)
            .filter_by(role_id=role.id, type=int(SubjectType.ROLE))
            .one_or_none()
        )
        if subject is None:
            subject = Subject(
                label=role.name, type=int(SubjectType.ROLE), role_id=role.id, active=True
            )
            db.session.add(subject)
            db.session.flush()

        name = f"tenant_scope_{t.table_name}"[:255]
        rls = db.session.query(RowLevelSecurityFilter).filter_by(name=name).one_or_none()
        if rls is None:
            rls = RowLevelSecurityFilter(
                name=name,
                description=f"Binds {t.table_name} to tenant {tenant}",
                filter_type=RowLevelSecurityFilterType.REGULAR.value,
                group_key=f"tenant_{tenant}",
                # The datasets are partitioned per tenant here, so isolation
                # comes from the binding itself; the clause is a no-op that
                # keeps the filter structurally real.
                clause="1 = 1",
            )
            db.session.add(rls)
            made["rls"] += 1
        rls.tables = [t]
        rls.subjects = [subject]

    db.session.commit()

    # ---- 3. every tenant has an administrator -------------------------------
    for tenant, guid in ADMINS.items():
        user = sm.find_user(username=guid)
        if user is None:
            print(f"MARK WARNING: no Superset user {guid} for tenant {tenant}")
            continue
        if fga.write_tuple(f"user:{guid}", "member", f"tenant:{tenant}"):
            made["tenant_tuples"] += 1
        if fga.write_tuple(f"user:{guid}", "member", tenant_administrator_group(tenant)):
            made["admins"] += 1

    # ---- 4. cross-tenant dashboards need a HUMAN decision -------------------
    # A dashboard whose charts span two tenants has no tenant derivable from
    # data, and the backfill deliberately refuses to guess -- a wrong tenant
    # hands the object to one administrator and locks out the other. Deciding
    # belongs here, in the fixture, where the choice is explicit and logged.
    # Majority of charts wins; the minority's tiles then render the
    # "you do not have access to this chart" placeholder for that tenant,
    # which is exactly the case worth having in a test environment.
    from collections import Counter

    from superset.models.dashboard import Dashboard

    from superset_ownership import service

    made["dashboards_decided"] = 0
    for dash in db.session.query(Dashboard).all():
        tallies = Counter(
            assignment[s.datasource_id]
            for s in (dash.slices or [])
            if s.datasource_id in assignment
        )
        if len(tallies) < 2:
            continue  # single-tenant or empty: the backfill handles it
        tenant = tallies.most_common(1)[0][0]
        admin_guid = ADMINS.get(tenant)
        admin = sm.find_user(username=admin_guid) if admin_guid else None
        if admin is None:
            continue
        row = service.lookup("dashboard", dash.id)
        service.upsert_ownership(
            asset_type="dashboard",
            object_id=dash.id,
            object_uuid=str(dash.uuid) if dash.uuid else None,
            owner_user_id=admin.id,
            visibility=row.visibility if row else "public",
        )
        if dash.uuid:
            fga.set_object_tenant("dashboard", str(dash.uuid), tenant)
        made["dashboards_decided"] += 1
        print(
            f"MARK cross-tenant dashboard {dash.id} '{dash.dashboard_title}' "
            f"{dict(tallies)} -> tenant {tenant[:8]} (owner {admin})"
        )
    db.session.commit()

    print("MARK seed_tenant_env:", made)
    for tenant in (TENANT_A, TENANT_B):
        owned = [t.table_name for t in tables if assignment[t.id] == tenant]
        print(f"MARK tenant {tenant[:8]}: {len(owned)} datasets -> {sorted(owned)}")


if __name__ == "__main__":
    from superset.app import create_app

    app = create_app()
    with app.app_context():
        run()

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
"""Seed Superset with Ivanti-shaped identities.

Creates users whose username IS a GUID v4 (the member id), and tenant roles
whose name carries the tenant GUID. That is the assumption documented in
superset_ownership/identity.py -- change it there and re-run this.

Deliberately does NOT create Superset groups: the whole point of the demo is
that group membership lives in OpenFGA, and Superset never learns about it.

Run inside the container:
    python /tmp/seed_identity.py
"""

from __future__ import annotations

# Two tenants, GUID v4, matching the format Ivanti confirmed.
TENANT_A = "a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40"
TENANT_B = "b7f28e5a-9c14-4d6b-a2f0-5e3d8c1a6b92"

# Members, keyed by GUID as Ivanti keys them. The label is only for humans
# reading the Superset admin screens.
MEMBERS = [
    # guid,                                    label,               tenant,   dataset
    ("3f0a91c7-2d84-4e63-9b15-7c4e8a2f6d31", "Ada (tenant A)", TENANT_A, "video_game_sales"),
    ("6c2b48e9-5a71-4f92-8d03-2e9b7c1a4d53", "Ben (tenant A)", TENANT_A, "video_game_sales"),
    ("9d7e35a1-8c62-4b04-a7f1-3d5e9b2c8a76", "Cleo (tenant B)", TENANT_B, "wb_health_population"),
]


def run() -> None:
    from superset import db, security_manager as sm
    from superset.connectors.sqla.models import SqlaTable

    gamma = sm.find_role("Gamma")
    created = []

    for guid, label, tenant, dataset in MEMBERS:
        # Tenant role carries the tenant GUID in its name -- this is what
        # identity.resolve_tenant_guid() reads back out, and it mirrors
        # Ivanti binding their RLS rule to the tenant role.
        role_name = f"tenant_{tenant}"
        role = sm.find_role(role_name) or sm.add_role(role_name)

        table = db.session.query(SqlaTable).filter_by(table_name=dataset).one_or_none()
        if table is not None:
            pv = sm.find_permission_view_menu("datasource_access", table.perm)
            if pv and pv not in role.permissions:
                role.permissions.append(pv)
        db.session.commit()

        first, _, last = label.partition(" ")
        user = sm.find_user(username=guid)
        if not user:
            user = sm.add_user(
                guid, first, last.strip("()") or "User", f"{guid}@ivanti.example",
                [gamma, role], password="test1234",
            )
        else:
            user.roles = [gamma, role]
            db.session.commit()

        created.append((guid, label, tenant, getattr(user, "id", None)))

    for guid, label, tenant, uid in created:
        print(f"MARKER member {guid} superset_id={uid} tenant={tenant} ({label})")


if __name__ == "__main__":
    from superset.app import create_app

    app = create_app()
    with app.app_context():
        run()

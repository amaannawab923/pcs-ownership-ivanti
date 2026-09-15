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
"""Seed a diverse directory of members and groups for the sharing picker.

The sharing picker (`GET /api/v1/ownership/subjects`) is tenant-scoped and
sourced from OpenFGA: it lists the caller's tenant's MEMBERS (labelled by a
matching Superset user) and its GROUPS (from group-membership tuples). With
only three seeded people it looks empty, so this adds a realistic spread.

For every seeded person this writes BOTH:
  - a Superset user whose username IS the member GUID (so the picker shows a
    human name, not a raw GUID) with the tenant role `tenant_<guid>`, and
  - the OpenFGA tuples: `member` of the tenant, and `member` of one or more
    groups `group:<id>`, the id built by `identity.group_object` in the
    configured OWNERSHIP_GROUP_ID_FORMAT (default `<name>_<tenant>`).

Idempotent: users are found-or-created, and `fga.write_tuple` treats an
already-existing tuple as success. GUIDs are DETERMINISTIC (derived from the
person's key) and valid UUID v4, so re-running touches nothing new and the
same person always gets the same GUID.

Run inside the container:
    docker cp qa/seed_directory.py pcs617-superset-light-1:/tmp/
    docker exec -e OWNERSHIP_ENABLED=true -e OWNERSHIP_AUTHORIZER=openfga \
        pcs617-superset-light-1 python /tmp/seed_directory.py
"""

from __future__ import annotations

import hashlib

TENANT_A = "a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40"
TENANT_B = "b7f28e5a-9c14-4d6b-a2f0-5e3d8c1a6b92"


def guid_for(key: str) -> str:
    """A deterministic, valid UUID v4 for a stable key.

    identity.GUID_RE requires the version nibble `4` and an `[89ab]` variant
    nibble, so we can't use uuid5 (v5). Derive the layout from a hash and
    force those two nibbles -- stable across runs, and it matches the regex.
    """
    h = hashlib.sha256(key.encode()).hexdigest()
    variant = "89ab"[int(h[16], 16) % 4]
    return f"{h[0:8]}-{h[8:12]}-4{h[13:16]}-{variant}{h[17:20]}-{h[20:32]}"


# (first, last, [group short-names]) -- groups become group:<name>_<tenant>.
# The existing Ada/Ben/Cleo and their groups are left as-is; these are added.
DIRECTORY = {
    TENANT_A: [
        ("Priya", "Sharma", ["dashboard_designer", "data_analyst"]),
        ("Marcus", "Chen", ["chart_designer", "data_analyst"]),
        ("Elena", "Rossi", ["data_analyst"]),
        ("David", "Okafor", ["marketing_analytics"]),
        ("Sofia", "Nguyen", ["marketing_analytics", "dashboard_designer"]),
        ("James", "Patel", ["finance_reporting"]),
        ("Aisha", "Khan", ["finance_reporting", "data_analyst"]),
        ("Tom", "Muller", ["engineering_metrics"]),
        ("Lena", "Fischer", ["engineering_metrics", "chart_designer"]),
        ("Carlos", "Mendez", ["support_ops"]),
        ("Yuki", "Tanaka", ["executives"]),
        ("Grace", "Adeyemi", ["executives", "dashboard_designer"]),
    ],
    TENANT_B: [
        ("Omar", "Farouk", ["dashboard_designer", "sales_ops"]),
        ("Hannah", "Berg", ["data_analyst"]),
        ("Diego", "Alvarez", ["sales_ops"]),
        ("Mei", "Lin", ["data_analyst", "chart_designer"]),
        ("Nadia", "Haddad", ["executives"]),
        ("Peter", "Novak", ["support_ops"]),
        ("Ruth", "Owusu", ["finance_reporting", "executives"]),
    ],
}


def run() -> None:
    from superset import db, security_manager as sm

    from superset_ownership import fga
    from superset_ownership.identity import group_object

    gamma = sm.find_role("Gamma")
    made = {"users": 0, "existing_users": 0, "member_tuples": 0, "group_tuples": 0}
    groups_seen: dict[str, set] = {}

    for tenant, people in DIRECTORY.items():
        role_name = f"tenant_{tenant}"
        role = sm.find_role(role_name) or sm.add_role(role_name)
        db.session.commit()

        for first, last, groups in people:
            guid = guid_for(f"{tenant}:{first}:{last}")
            email = f"{first.lower()}.{last.lower()}@ivanti.example"

            user = sm.find_user(username=guid)
            if user is None:
                sm.add_user(guid, first, last, email, [gamma, role], password="test1234")
                made["users"] += 1
            else:
                user.roles = [gamma, role]
                db.session.commit()
                made["existing_users"] += 1

            # tenant membership + group memberships, in OpenFGA
            if fga.write_tuple(f"user:{guid}", "member", f"tenant:{tenant}"):
                made["member_tuples"] += 1
            for g in groups:
                grp = group_object(g, tenant)
                if fga.write_tuple(f"user:{guid}", "member", grp):
                    made["group_tuples"] += 1
                groups_seen.setdefault(tenant, set()).add(g)

    print("MARKER seed_directory:", made)
    for tenant, gs in groups_seen.items():
        print(f"MARKER tenant {tenant} groups: {sorted(gs)}")


if __name__ == "__main__":
    from superset.app import create_app

    app = create_app()
    with app.app_context():
        run()

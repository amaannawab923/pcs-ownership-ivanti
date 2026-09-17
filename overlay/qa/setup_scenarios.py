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
PCS-10243 object-ownership QA scenario builder.

Idempotent: safe to re-run against a live PCS v6.1.0.7 instance. Creates
roles/users/tenant-role mappings, assigns ownership/visibility on a set of
scenario dashboards/charts, and prints a MARKER line per artifact so a
wrapper script can capture ids without re-querying.

Run inside the superset-light container:
    docker exec pcs617-superset-light-1 python /tmp/setup_scenarios.py

Does NOT touch dashboard 5 / chart 51 (the pre-existing demo objects) --
those are reset separately by qa/reset-demo-objects.sh.
"""
from superset.app import create_app

app = create_app()

with app.app_context():
    from superset import db
    from superset import security_manager as sm
    from superset.connectors.sqla.models import SqlaTable
    from superset.models.dashboard import Dashboard
    from superset.models.slice import Slice
    from superset_ownership import service, sentinel
    from superset_ownership.backfill import run as backfill

    # 0. Make sure every pre-existing asset has a public/unowned row so
    #    nothing we didn't touch on purpose changes behaviour.
    backfill()

    # ---- Tenant GUIDs (mirrors Ivanti's per-tenant-role model) -----------
    TENANT_A = "a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40"
    TENANT_B = "b7f28e5a-9c14-4d6b-a2f0-5e3d8c1a6b92"

    def ds_role(role_name, table_name):
        """Get-or-create a role granting datasource_access on one dataset."""
        table = db.session.query(SqlaTable).filter_by(table_name=table_name).one()
        role = sm.find_role(role_name) or sm.add_role(role_name)
        pv = sm.find_permission_view_menu("datasource_access", table.perm)
        if pv and pv not in role.permissions:
            role.permissions.append(pv)
        db.session.commit()
        return role, table

    from superset_ownership.identity import tenant_role_name

    tenant_a_role, vgs_table = ds_role(tenant_role_name(TENANT_A), "video_game_sales")
    tenant_b_role, wb_table = ds_role(tenant_role_name(TENANT_B), "wb_health_population")
    designer_role, sales_table = ds_role("ds_cleaned_sales_data", "cleaned_sales_data")
    # keep the pre-existing single-dataset roles from the earlier demo too
    ds_role("ds_video_game_sales", "video_game_sales")
    ds_role("ds_wb_health_population", "wb_health_population")

    gamma = sm.find_role("Gamma")
    alpha = sm.find_role("Alpha")

    def get_or_create_user(username, roles, first="QA", last="Ownership"):
        u = sm.find_user(username=username)
        if not u:
            u = sm.add_user(
                username, first, last, f"{username}@qa.local", roles, password="test1234"
            )
        else:
            u.roles = roles
        db.session.commit()
        return u

    dash_designer = get_or_create_user("dash_designer", [gamma, designer_role])
    chart_designer = get_or_create_user("chart_designer", [gamma, designer_role])
    tenant_a_user1 = get_or_create_user("tenant_a_user1", [gamma, tenant_a_role])
    tenant_a_user2 = get_or_create_user("tenant_a_user2", [gamma, tenant_a_role])
    tenant_b_user1 = get_or_create_user("tenant_b_user1", [gamma, tenant_b_role])
    tenant_b_user2 = get_or_create_user("tenant_b_user2", [gamma, tenant_b_role])
    alpha_user = get_or_create_user("alpha_user", [alpha])
    admin = sm.find_user(username="admin")

    users = {
        "admin": admin,
        "dash_designer": dash_designer,
        "chart_designer": chart_designer,
        "tenant_a_user1": tenant_a_user1,
        "tenant_a_user2": tenant_a_user2,
        "tenant_b_user1": tenant_b_user1,
        "tenant_b_user2": tenant_b_user2,
        "alpha_user": alpha_user,
    }
    for name, u in users.items():
        print(f"MARKER user {name} id={u.id}")

    print(f"MARKER tenant_a_role={tenant_a_role.name}")
    print(f"MARKER tenant_b_role={tenant_b_role.name}")
    print(f"MARKER tenant_a_guid={TENANT_A}")
    print(f"MARKER tenant_b_guid={TENANT_B}")

    # ---- Scenario dashboards/charts ---------------------------------------
    # DASH_PUB: dashboard 6 "Sales Dashboard" -> public, owner=dash_designer
    dash6 = db.session.get(Dashboard, 6)
    service.upsert_ownership("dashboard", 6, str(dash6.uuid), dash_designer.id, "public")
    sentinel.remove_sentinel(dash6)

    # DASH_PRIV: dashboard 2 "World Bank's Data" -> private, owner=tenant_b_user1
    dash2 = db.session.get(Dashboard, 2)
    service.upsert_ownership("dashboard", 2, str(dash2.uuid), tenant_b_user1.id, "private")
    sentinel.add_sentinel(dash2)

    # DASH_PARTIAL: dashboard 8 "Misc Charts" (mixes datasets 2, 16, 21) ->
    # shared with tenant_b_user1 (has dataset 21 access only) for the
    # partial-render case: chart 82 (dataset 21) should render, 80/81 should not.
    dash8 = db.session.get(Dashboard, 8)
    service.upsert_ownership("dashboard", 8, str(dash8.uuid), admin.id, "shared")
    sentinel.add_sentinel(dash8)
    service.add_share("dashboard", 8, str(dash8.uuid), f"user:{tenant_b_user1.id}", "viewer")

    # DASH_UNOWNED: dashboard 9 left exactly as backfill created it
    # (owner=None, visibility=public) -- used for the "sharing unavailable
    # while unowned" and admin-claims-on-write tests. No mutation here.

    # New dashboards for group-sharing scenarios, built from existing charts
    # so dashboard 5 is never touched.
    def get_or_create_dashboard(slug, title, chart_ids):
        dash = db.session.query(Dashboard).filter_by(slug=slug).one_or_none()
        if dash is None:
            dash = Dashboard(dashboard_title=title, slug=slug, published=True)
            db.session.add(dash)
            db.session.flush()
        dash.slices = [db.session.get(Slice, cid) for cid in chart_ids]
        db.session.commit()
        db.session.flush()
        return dash

    dash_group_direct = get_or_create_dashboard(
        "qa-own-group-direct", "QA-OWN Group Shared (video_game_sales)", [52]
    )
    dash_group_nested = get_or_create_dashboard(
        "qa-own-group-nested", "QA-OWN Nested Group (wb_health_population)", [8]
    )
    for dash, owner in ((dash_group_direct, dash_designer), (dash_group_nested, tenant_b_user1)):
        if dash.uuid is None:
            db.session.flush()
        service.upsert_ownership("dashboard", dash.id, str(dash.uuid), owner.id, "shared")
        sentinel.add_sentinel(dash)
    db.session.commit()
    print(f"MARKER dash_group_direct id={dash_group_direct.id} uuid={dash_group_direct.uuid}")
    print(f"MARKER dash_group_nested id={dash_group_nested.id} uuid={dash_group_nested.uuid}")

    # ---- Charts ------------------------------------------------------------
    # CHART_OWNER_FASTPATH: chart 60 "Items Sold" -> private, owner=dash_designer
    chart60 = db.session.get(Slice, 60)
    service.upsert_ownership("chart", 60, str(chart60.uuid), dash_designer.id, "private")
    sentinel.add_sentinel(chart60)

    # CHART_ROLE_HIERARCHY: chart 61 "Total Revenue" -> shared, owner=chart_designer
    # shared with dash_designer as EDITOR (dataset 3, both designers have access)
    # and with tenant_a_user1 as EDITOR (no dataset 3 access -> AND-rule proof).
    chart61 = db.session.get(Slice, 61)
    service.upsert_ownership("chart", 61, str(chart61.uuid), chart_designer.id, "shared")
    sentinel.add_sentinel(chart61)
    service.add_share("chart", 61, str(chart61.uuid), f"user:{dash_designer.id}", "editor")
    service.add_share("chart", 61, str(chart61.uuid), f"user:{tenant_a_user1.id}", "editor")

    # CHART_UNOWNED_LIST: chart 62 left exactly as backfilled (public, owner=None).

    # CHART_ALPHA_GAP / CHART_DATA_BYPASS probe target: chart 60 above is
    # reused (private, owner=dash_designer, not shared with chart_designer
    # or alpha_user) -- see qa/run-suite.sh for the exact requests.

    db.session.commit()

    for cid in (2, 6, 8, 9, dash_group_direct.id, dash_group_nested.id):
        d = db.session.get(Dashboard, cid)
        row = service.lookup("dashboard", cid)
        print(f"MARKER dashboard {cid} uuid={d.uuid} owner={row.owner_user_id if row else None} vis={row.visibility if row else None}")
    for cid in (60, 61, 62):
        c = db.session.get(Slice, cid)
        row = service.lookup("chart", cid)
        print(f"MARKER chart {cid} uuid={c.uuid} owner={row.owner_user_id if row else None} vis={row.visibility if row else None}")

    print("MARKER setup_scenarios: done")

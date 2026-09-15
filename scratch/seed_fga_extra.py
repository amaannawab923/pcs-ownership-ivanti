# scratch-only addition, NOT part of overlay/qa (overlay/ is generated from
# client-test main and is never hand-edited -- see scratch/README.md).
#
# overlay/qa/seed_directory.py writes each seeded person's tenant membership
# and their group `member` tuples, but it does not write the group's own
# `tenant` relation (`tenant:<T>#member tenant group:<id>`) -- the tuple
# `directory.list_groups(tenant)`/`plugin verify`'s vocabulary/directory
# checks (group_ids_parse, administrator_group_exists, tenant_isolation) walk
# to enumerate "this tenant's groups". Without it every group seed_directory.py
# created is invisible to those checks (SKIP/WARN instead of PASS) even
# though membership itself works. This script is purely additive: it writes
# the missing `tenant` relation for every group short-name seed_directory.py
# used, plus the `tenant_administrator_<tenant>` group (Ada admins tenant A,
# Cleo admins tenant B) and the nested membership every tenant administrator
# is also a dashboard_designer (mirrors docker/seed.sh's own pattern for the
# 2-tenant demo store, extended to the full directory here).
#
# Idempotent: fga.write_tuple treats an already-existing tuple as success.
#
# Run inside the container, in app context, AFTER seed_identity.py and
# seed_directory.py:
#   docker exec <superset> python /tmp/seed_fga_extra.py
from __future__ import annotations

TENANT_A = "a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40"
TENANT_B = "b7f28e5a-9c14-4d6b-a2f0-5e3d8c1a6b92"

ADA = "3f0a91c7-2d84-4e63-9b15-7c4e8a2f6d31"
BEN = "6c2b48e9-5a71-4f92-8d03-2e9b7c1a4d53"
CLEO = "9d7e35a1-8c62-4b04-a7f1-3d5e9b2c8a76"

# seed_identity.py creates these three as Superset users with tenant roles
# but writes nothing to the store, and seed_directory.py leaves them alone
# on purpose -- so their tenant membership tuples are written HERE. Without
# them the OpenFGA directory (the sharing picker, users_of_tenant) cannot
# see them even though every access check for them works.
IDENTITY_MEMBERS = {ADA: TENANT_A, BEN: TENANT_A, CLEO: TENANT_B}

# Every group short-name seed_directory.py (plus seed_identity.py's tenant
# roles) referenced, per tenant -- must match those two files exactly.
GROUPS = {
    TENANT_A: [
        "dashboard_designer", "data_analyst", "chart_designer",
        "marketing_analytics", "finance_reporting", "engineering_metrics",
        "support_ops", "executives",
    ],
    TENANT_B: [
        "dashboard_designer", "data_analyst", "sales_ops", "chart_designer",
        "executives", "support_ops", "finance_reporting",
    ],
}

# One administrator per tenant -- Ada already tenant A's admin per the
# delivery brief; Cleo takes tenant B for symmetry (an
# administrator_group_exists check with zero admins on B would only WARN,
# not FAIL, but a real multi-tenant demo should have both).
TENANT_ADMINS = {TENANT_A: ADA, TENANT_B: CLEO}


def run() -> None:
    from superset_ownership import fga
    from superset_ownership.identity import group_object, tenant_administrator_group

    made = {
        "group_tenant_tuples": 0, "admin_group_tuples": 0, "admin_members": 0,
        "nested": 0, "identity_tenant_members": 0,
    }

    for guid, tenant in IDENTITY_MEMBERS.items():
        if fga.write_tuple(f"user:{guid}", "member", f"tenant:{tenant}"):
            made["identity_tenant_members"] += 1

    for tenant, names in GROUPS.items():
        for name in names:
            grp = group_object(name, tenant)
            if fga.write_tuple(f"tenant:{tenant}#member", "tenant", grp):
                made["group_tenant_tuples"] += 1

        admin_group = tenant_administrator_group(tenant)
        if fga.write_tuple(f"tenant:{tenant}#member", "tenant", admin_group):
            made["admin_group_tuples"] += 1
        admin_guid = TENANT_ADMINS[tenant]
        if fga.write_tuple(f"user:{admin_guid}", "member", admin_group):
            made["admin_members"] += 1

        # Nested membership: every tenant administrator is also a
        # dashboard_designer of the same tenant (mirrors docker/seed.sh).
        dd = group_object("dashboard_designer", tenant)
        if fga.write_tuple(f"{admin_group}#member", "member", dd):
            made["nested"] += 1

    print("MARK seed_fga_extra:", made)


if __name__ == "__main__":
    from superset.app import create_app

    app = create_app()
    with app.app_context():
        run()

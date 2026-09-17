# scratch-only addition, NOT part of overlay/qa (overlay/ is generated from
# client-test main and is never hand-edited -- see scratch/README.md).
#
# Writes what a Neurons deployment's own platform writes and the overlay
# seeds do not, in Neurons' shapes (PCS-10243, confirmed by Ivanti):
#
#   - the three identity members' tenant membership
#     (`user:<tenant>.<member> member tenant:<tenant>`): overlay/qa/
#     seed_identity.py creates them as Superset users with `Tenant_<guid>_
#     Role` but writes nothing to the store, and seed_directory.py leaves
#     them alone on purpose;
#   - each tenant's administrator as the platform's `admin` relation on
#     the tenant object (`user:<tenant>.<admin> admin tenant:<tenant>`),
#     which the module reads (`tenant_administrator_group` -> `tenant:<t>#
#     admin`) and never writes -- Ada administers tenant A, Cleo tenant B;
#   - one nested group per tenant: `group:<t>.chart_designer#member` is a
#     member of `group:<t>.dashboard_designer` (a group as a member of a
#     group, resolved by the store).
#
# Deliberately NOT written: any group->tenant tuple. Neurons carries the
# tenant in the group id (`group:<tenant>.<local id>`) and writes no such
# tuple; the directory walks the members instead
# (OWNERSHIP_DIRECTORY_GROUP_WALK="always", set in the config layer), and
# `plugin verify`'s group_has_tenant_tuple reports that as PASS.
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

IDENTITY_MEMBERS = {ADA: TENANT_A, BEN: TENANT_A, CLEO: TENANT_B}

# One administrator per tenant -- Ada already tenant A's admin per the
# delivery brief; Cleo takes tenant B for symmetry (a tenant with no
# administrator has nobody to inherit its unowned objects).
TENANT_ADMINS = {TENANT_A: ADA, TENANT_B: CLEO}


def run() -> None:
    from superset_ownership import fga
    from superset_ownership.identity import (
        compose_subject_id,
        group_object,
        split_userset,
        tenant_administrator_group,
    )

    made = {"identity_tenant_members": 0, "admins": 0, "nested": 0}

    for guid, tenant in IDENTITY_MEMBERS.items():
        subject = f"user:{compose_subject_id(tenant, guid)}"
        if fga.write_tuple(subject, "member", f"tenant:{tenant}"):
            made["identity_tenant_members"] += 1

    for tenant, admin_guid in TENANT_ADMINS.items():
        # `tenant:<t>#admin` by default; a group when a shape hook says so.
        obj, relation = split_userset(tenant_administrator_group(tenant))
        subject = f"user:{compose_subject_id(tenant, admin_guid)}"
        if fga.write_tuple(subject, relation, obj):
            made["admins"] += 1

        # Nested membership: chart designers are dashboard designers too.
        cd = group_object("chart_designer", tenant)
        dd = group_object("dashboard_designer", tenant)
        if fga.write_tuple(f"{cd}#member", "member", dd):
            made["nested"] += 1

    print("MARK seed_fga_extra:", made)


if __name__ == "__main__":
    from superset.app import create_app

    app = create_app()
    with app.app_context():
        run()

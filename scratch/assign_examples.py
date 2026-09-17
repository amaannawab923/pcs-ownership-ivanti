# scratch-only script (not part of overlay/qa): deterministic ownership
# attribution of the stock `superset load_examples` content across the two
# seeded tenants, so the ownership migration's backfill (see docker/
# entrypoint-ownership.sh) has a real "these were created by tenant users"
# history to work from instead of NULL created_by_fk everywhere.
#
# For every example dashboard AND its charts, sets:
#   - created_by_fk / changed_by_fk -> the assigned tenant user's ab_user.id
#     (what superset_ownership.backfill.run()'s OWNER FALLBACK reads first)
#   - the native "editors" many-to-many (dashboard_editors / chart_editors
#     in this build -- Dashboard/Slice have no `owners` relation any more,
#     `editors` is what replaced it pre-ownership-feature) -- so the
#     pre-existing-object history looks like a real brownfield instance, not
#     just a backfill fallback field.
#
# DATASET-ACCESS-AWARE (run AFTER scratch/seed_tenant_data.py, which grants
# datasource_access per tenant role): a dashboard must only be attributed to
# a tenant that can actually read every chart on it. For each dashboard this
# reads the GROUND TRUTH -- which tenant role(s) hold datasource_access on
# each of its charts' datasets -- and intersects across all its charts to
# get the dashboard's compatible tenant set:
#   - a chart on a SHARED dataset (cleaned_sales_data, birth_names,
#     video_game_sales, neurons_devices) is readable by both tenants
#   - a chart on an EXCLUSIVE dataset is readable by exactly the one tenant
#     seed_tenant_data.py bound it to
#   - {A, B}    -> either tenant works; the pin rules / round-robin pool
#                  below decide
#   - {A} / {B} -> that tenant is the only legal owner, overriding the pool
#   - {}        -> a real violation (the dashboard mixes charts from two
#                  DIFFERENT exclusive tenants) -- resolved by keeping the
#                  majority tenant's charts and REMOVING the others from
#                  the dashboard (dashboard_slices is many-to-many; the
#                  chart itself is not deleted, just unlinked from this
#                  dashboard), printed as a violation either way
#
# Deterministic distribution (when the compatible set allows a choice):
#   - "Video Game Sales", "Births" -> Ada (tenant A)   [shared datasets]
#   - "Sales", "Slack"             -> Marcus (tenant A) [shared: cleaned_sales_data]
#   - "FCC", "deck.gl"             -> Cleo (tenant B)
#   - everything else              -> round-robin across the rest of the
#                                      seeded directory (overlay/qa/
#                                      seed_directory.py), from the pool
#                                      matching the dashboard's compatible
#                                      tenant set
#
# Skips any dashboard scratch/seed_tenant_data.py already created and
# attributed directly (title ending "Device Compliance") -- those already
# have a real tenant-user creator/owner and must not be reassigned here.
#
# Idempotent: re-running reassigns the same dashboards to the same owners
# (the pin rules and the round-robin order are both fixed) and re-derives
# the same compatible sets from the same grants.
#
# Run inside the container, in app context, AFTER seed_identity.py +
# seed_directory.py (the users must already exist), AFTER `superset
# load_examples`, and AFTER scratch/seed_tenant_data.py (the dataset
# grants this script reads must already be in place):
#   docker exec <superset> python /tmp/assign_examples.py
from __future__ import annotations

import itertools
from collections import Counter

TENANT_A = "a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40"
TENANT_B = "b7f28e5a-9c14-4d6b-a2f0-5e3d8c1a6b92"

ADA = "3f0a91c7-2d84-4e63-9b15-7c4e8a2f6d31"
BEN = "6c2b48e9-5a71-4f92-8d03-2e9b7c1a4d53"
CLEO = "9d7e35a1-8c62-4b04-a7f1-3d5e9b2c8a76"
MARCUS = "4302756e-b4aa-4938-b599-ed593444aeaf"

# The rest of overlay/qa/seed_directory.py's directory, GUIDs precomputed
# from that file's own guid_for() (sha256, deterministic) -- see
# scratch/README.md for the full name table.
POOL_A: list[str] = [
    "9fcc709c-1a6b-4882-b8e3-1787afe31417",  # Priya Sharma
    "58a4685c-6e11-4c4f-aad3-444839fc846a",  # Elena Rossi
    "12fc0874-6358-48e8-96cb-6ada75fd8c76",  # David Okafor
    "e5a6a6c5-c81d-49a3-a9f8-c1614639fe6f",  # Sofia Nguyen
    "bb355a29-91b4-4353-96ae-714919c21afc",  # James Patel
    "b5807abf-02eb-409e-805d-35eceb60d67c",  # Aisha Khan
    "fa17aa75-9521-4c64-a848-8a0edd1bc316",  # Tom Muller
    "e19ffd42-45dc-44ae-bb0e-376ab953469c",  # Lena Fischer
    "b675fa48-ddb1-48c5-a233-d2d10d655526",  # Carlos Mendez
    "af2fb185-ca1c-4640-ba61-33dd9b1977b6",  # Yuki Tanaka
    "4ddb1932-8b1c-49be-93b1-a9920129874b",  # Grace Adeyemi
    BEN,
]
POOL_B: list[str] = [
    "60f317ea-66cb-48a0-8d45-e15699486409",  # Omar Farouk
    "e64caf0a-30fc-4f79-8f5a-e0a64dced4ad",  # Hannah Berg
    "a105bfbe-3b53-43d6-bc1c-953293a35c54",  # Diego Alvarez
    "21387ae6-c116-4a2a-bd19-c46828e2e40d",  # Mei Lin
    "3724dadf-3dd0-4678-bc43-46e49d49702b",  # Nadia Haddad
    "163da054-16e6-46e6-8587-6677e74fe516",  # Peter Novak
    "b7a007be-ed05-4971-99fc-6c7ade4faa86",  # Ruth Owusu
]


def _pin(title: str) -> tuple[str, str] | None:
    t = (title or "").lower()
    if "video game" in t or "birth" in t:
        return TENANT_A, ADA
    if "sales" in t or "slack" in t:
        return TENANT_A, MARCUS
    if "fcc" in t or "deck.gl" in t or "deckgl" in t:
        return TENANT_B, CLEO
    return None


def _dataset_accessible_tenants(sm, table_perm_cache: dict, ds) -> set[str]:
    """Ground truth: which tenant role(s) hold datasource_access on ds."""
    if ds is None:
        return {TENANT_A, TENANT_B}  # fail open: nothing to constrain on
    key = ds.id
    if key in table_perm_cache:
        return table_perm_cache[key]
    accessible = set()
    from superset_ownership.identity import tenant_role_name

    for tenant in (TENANT_A, TENANT_B):
        role = sm.find_role(tenant_role_name(tenant))
        if role is None:
            continue
        pvm = sm.find_permission_view_menu("datasource_access", ds.perm)
        if pvm is not None and pvm in (role.permissions or []):
            accessible.add(tenant)
    if not accessible:
        accessible = {TENANT_A, TENANT_B}  # not yet bound: fail open
    table_perm_cache[key] = accessible
    return accessible


def run() -> None:
    from superset import db, security_manager as sm
    from superset.models.dashboard import Dashboard
    from superset.subjects.utils import get_or_create_user_subject

    pool_a = itertools.cycle(POOL_A)
    pool_b = itertools.cycle(POOL_B)
    pool_either = itertools.cycle([TENANT_A, TENANT_B])
    rows: list[tuple[int, str, str, str, int]] = []
    violations: list[str] = []
    subject_cache: dict[int, object] = {}
    perm_cache: dict[int, set] = {}

    def user_subject(user_id: int):
        # dashboard_editors/chart_editors point at `subjects`, not ab_user
        # directly (this build's Subject abstraction wraps user/role/group)
        # -- appending a bare User to `.editors` raises FlushError. One
        # get-or-create per user, cached across dashboards/charts.
        if user_id not in subject_cache:
            subject_cache[user_id] = get_or_create_user_subject(user_id)
        return subject_cache[user_id]

    def next_guid(tenant: str) -> str:
        return next(pool_a) if tenant == TENANT_A else next(pool_b)

    for dash in db.session.query(Dashboard).order_by(Dashboard.id).all():
        if (dash.dashboard_title or "").endswith("Device Compliance"):
            continue  # scratch/seed_tenant_data.py already attributed this one

        slices = list(dash.slices or [])
        per_chart_tenants = {
            slc.id: _dataset_accessible_tenants(sm, perm_cache, slc.datasource)
            for slc in slices
        }
        compatible = set.intersection(*per_chart_tenants.values()) if per_chart_tenants else {TENANT_A, TENANT_B}

        removed = []
        if not compatible and per_chart_tenants:
            # Real violation: charts from two different exclusive tenants on
            # one dashboard. Keep the majority tenant's charts, unlink (not
            # delete) the rest.
            tally = Counter()
            for tset in per_chart_tenants.values():
                if len(tset) == 1:
                    tally[next(iter(tset))] += 1
            # Deterministic on a tie: `most_common` keeps insertion order,
            # which follows chart iteration order, so a 1:1 split flipped
            # between runs. Tenant A wins a tie, so "Featured Charts" is
            # Priya's every time, as the README says.
            majority = (
                max(sorted(tally), key=lambda t: (tally[t], t == TENANT_A))
                if tally
                else TENANT_A
            )
            for slc in slices:
                if majority not in per_chart_tenants[slc.id]:
                    dash.slices.remove(slc)
                    removed.append(slc.slice_name)
            compatible = {majority}
            msg = (
                f"VIOLATION dashboard {dash.id} '{dash.dashboard_title}': charts spanned "
                f"tenants {dict(tally)}; kept {majority[:8]}, removed from dashboard: {removed}"
            )
            violations.append(msg)
            print(f"MARK {msg}")
            slices = [s for s in slices if s.slice_name not in removed]

        pin = _pin(dash.dashboard_title or "")
        if pin is not None and pin[0] in compatible:
            tenant, guid = pin
        elif pin is not None:
            # Pin says one tenant, grants say another -- grants win (they're
            # the actual enforcement mechanism); note it and fall through.
            violations.append(
                f"pin mismatch dashboard {dash.id} '{dash.dashboard_title}': "
                f"pinned {pin[0][:8]} not in compatible {sorted(t[:8] for t in compatible)}"
            )
            tenant = next(pool_either) if compatible == {TENANT_A, TENANT_B} else next(iter(compatible))
            guid = next_guid(tenant)
        elif compatible == {TENANT_A, TENANT_B}:
            tenant = next(pool_either)
            guid = next_guid(tenant)
        else:
            tenant = next(iter(compatible))
            guid = next_guid(tenant)

        user = sm.find_user(username=guid)
        if user is None:
            print(f"MARK WARNING: no Superset user for {guid}; skipping dashboard {dash.id}")
            continue
        subject = user_subject(user.id)

        dash.created_by_fk = user.id
        dash.changed_by_fk = user.id
        if subject is not None and subject not in (dash.editors or []):
            dash.editors.append(subject)

        n_charts = 0
        for slice_ in slices:
            slice_.created_by_fk = user.id
            slice_.changed_by_fk = user.id
            if subject is not None and subject not in (slice_.editors or []):
                slice_.editors.append(subject)
            n_charts += 1

        owner_label = f"{user.first_name} ({guid[:8]})"
        rows.append((dash.id, dash.dashboard_title or "", owner_label, tenant, n_charts))

    db.session.commit()

    print(f"{'id':>4}  {'dashboard':42}  {'owner':26}  {'tenant':10}  charts")
    print("-" * 100)
    for id_, title, owner, tenant, n_charts in rows:
        print(f"{id_:>4}  {title[:42]:42}  {owner:26}  {tenant[:8]:10}  {n_charts}")
    print(f"MARK assign_examples: {len(rows)} dashboards attributed, {len(violations)} violation(s)")
    for v in violations:
        print(f"MARK   {v}")


if __name__ == "__main__":
    from superset.app import create_app

    app = create_app()
    with app.app_context():
        run()

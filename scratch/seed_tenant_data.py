# scratch-only script (not part of overlay/qa): real Ivanti-shaped row-level
# tenancy over the example content, replacing overlay/qa/seed_tenant_env.py's
# role in this pipeline (that script binds EVERY dataset exclusively to one
# tenant with a `1 = 1` marker RLS filter -- it has no notion of a dataset
# genuinely SHARED between tenants, and would clobber the shared datasets
# this script sets up if run against the same three tables). Its exclusive-
# binding BEHAVIOUR is reproduced here (section 2) for every dataset this
# script does not make shared; its "every tenant has an administrator" step
# is redundant with scratch/seed_fga_extra.py and is not repeated; its
# cross-tenant-dashboard resolution is superseded by the stronger invariant
# scratch/assign_examples.py now enforces (a dashboard attributed to a
# tenant may only contain charts on datasets that tenant can read).
#
# Three sections, run in order:
#
# 1. SHARED datasets (cleaned_sales_data, birth_names, video_game_sales):
#    add a real `tenant_id` column to the physical table, backfill it
#    deterministically so BOTH tenants own a visible, different slice,
#    refresh the Superset dataset's column metadata, then bind BOTH tenant
#    roles to it -- datasource_access for both, and one REGULAR RLS filter
#    PER TENANT with the real clause `tenant_id = '<that tenant's guid>'`.
#    Same chart, different rows, depending on who's asking -- see
#    scratch/proof-queries.sh's API proof.
#
# 2. EXCLUSIVE datasets: every other example dataset (including the two
#    seed_tenant_env.py used to pin, video_game_sales -- no longer, it's
#    shared now -- and wb_health_population) stays bound to exactly ONE
#    tenant: datasource_access to that tenant's role only, and a `1 = 1`
#    RLS filter on that role as the binding marker (no real per-row
#    tenancy -- Ivanti's per-tenant-dataset case, not the shared case).
#
# 3. A Neurons-shaped synthetic dataset (`neurons_devices`, ~400 rows,
#    device inventory/compliance shape) -- SHARED like section 1 (RLS +
#    access for both tenant roles) -- plus one dashboard per tenant
#    ("<Tenant> Device Compliance", three charts: compliance-by-state pie,
#    devices-by-OS bar, stale-devices table), created by that tenant's
#    administrator (Ada for A, Cleo for B) as creator AND native owner.
#
# Run inside the container, in app context, AFTER `superset load_examples`
# (section 1/2 need the physical tables) and AFTER identities exist
# (section 3 needs Ada/Cleo as Superset users), BEFORE scratch/
# assign_examples.py (attribution needs to read section 1/2's
# datasource_access grants to keep every dashboard's owner-tenant
# consistent with what that tenant can actually read).
#
# Idempotent: column/table/RLS/grant/dashboard creation all check for an
# existing row first.
from __future__ import annotations

import random
from datetime import datetime, timedelta

TENANT_A = "a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40"
TENANT_B = "b7f28e5a-9c14-4d6b-a2f0-5e3d8c1a6b92"
ADA = "3f0a91c7-2d84-4e63-9b15-7c4e8a2f6d31"
CLEO = "9d7e35a1-8c62-4b04-a7f1-3d5e9b2c8a76"

# table_name -> (tenant_id backfill SQL CASE expression, description)
SHARED_TABLES = {
    "cleaned_sales_data": (
        f"CASE WHEN deal_size IN ('Small','Medium') THEN '{TENANT_A}' ELSE '{TENANT_B}' END",
        "deal_size in (Small, Medium) -> tenant A, else tenant B",
    ),
    "birth_names": (
        f"CASE WHEN COALESCE(state,'') <= 'M' THEN '{TENANT_A}' ELSE '{TENANT_B}' END",
        "state alphabetically A-M -> tenant A, N-Z (and null) -> tenant B",
    ),
    "video_game_sales": (
        f"CASE WHEN COALESCE(platform,'') <= 'M' THEN '{TENANT_A}' ELSE '{TENANT_B}' END",
        "platform alphabetically A-M -> tenant A, N-Z -> tenant B",
    ),
}

# Datasets pinned to a specific tenant for continuity with earlier fixtures;
# everything else not in SHARED_TABLES alternates by id order.
EXCLUSIVE_PINNED = {"wb_health_population": TENANT_B}


def _tenant_role(sm, db, tenant: str):
    role = sm.find_role(f"tenant_{tenant}") or sm.add_role(f"tenant_{tenant}")
    db.session.commit()
    return role


def _role_subject(db, role):
    from superset.subjects.models import Subject
    from superset.subjects.types import SubjectType

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
    return subject


def _grant_datasource_access(sm, role, table) -> bool:
    pvm = sm.find_permission_view_menu("datasource_access", table.perm)
    if pvm is None:
        pvm = sm.add_permission_view_menu("datasource_access", table.perm)
    if pvm is not None and pvm not in (role.permissions or []):
        sm.add_permission_role(role, pvm)
        return True
    return False


def _upsert_rls(db, name: str, table, subjects: list, clause: str, group_key: str) -> bool:
    from superset.connectors.sqla.models import RowLevelSecurityFilter
    from superset.utils.core import RowLevelSecurityFilterType

    rls = db.session.query(RowLevelSecurityFilter).filter_by(name=name).one_or_none()
    created = False
    if rls is None:
        rls = RowLevelSecurityFilter(
            name=name,
            description=f"scratch/seed_tenant_data.py: {name}",
            filter_type=RowLevelSecurityFilterType.REGULAR.value,
            group_key=group_key,
            clause=clause,
        )
        db.session.add(rls)
        created = True
    else:
        rls.clause = clause
    rls.tables = [table]
    rls.subjects = subjects
    return created


# ---------------------------------------------------------------------------
# 1. SHARED datasets
# ---------------------------------------------------------------------------
def ensure_shared_datasets(db, sm) -> dict:
    from superset.connectors.sqla.models import SqlaTable
    from sqlalchemy import inspect, text

    role_a = _tenant_role(sm, db, TENANT_A)
    role_b = _tenant_role(sm, db, TENANT_B)
    subj_a = _role_subject(db, role_a)
    subj_b = _role_subject(db, role_b)

    made = {"columns_added": 0, "rows_backfilled": {}, "grants": 0, "rls": 0}

    examples_engine_table = None
    for table_name, (case_sql, note) in SHARED_TABLES.items():
        ds = (
            db.session.query(SqlaTable)
            .filter_by(table_name=table_name)
            .one_or_none()
        )
        if ds is None:
            print(f"MARK WARNING: shared dataset {table_name} not found (load_examples ran?); skipping")
            continue

        with ds.database.get_sqla_engine() as engine:
            examples_engine_table = engine
            existing_cols = {c["name"] for c in inspect(engine).get_columns(table_name)}
            with engine.begin() as conn:
                if "tenant_id" not in existing_cols:
                    conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN tenant_id VARCHAR(36)"))
                    made["columns_added"] += 1
                result = conn.execute(
                    text(f"UPDATE {table_name} SET tenant_id = {case_sql}")
                )
                made["rows_backfilled"][table_name] = result.rowcount

        # Refresh dataset metadata so tenant_id is a known TableColumn.
        ds.fetch_metadata()
        db.session.commit()

        if _grant_datasource_access(sm, role_a, ds):
            made["grants"] += 1
        if _grant_datasource_access(sm, role_b, ds):
            made["grants"] += 1

        if _upsert_rls(
            db, f"tenant_scope_{table_name}_A", ds, [subj_a],
            f"tenant_id = '{TENANT_A}'", f"tenant_{TENANT_A}",
        ):
            made["rls"] += 1
        if _upsert_rls(
            db, f"tenant_scope_{table_name}_B", ds, [subj_b],
            f"tenant_id = '{TENANT_B}'", f"tenant_{TENANT_B}",
        ):
            made["rls"] += 1

        print(f"MARK shared dataset {table_name}: {note}")

    db.session.commit()
    return made


# ---------------------------------------------------------------------------
# 2. EXCLUSIVE datasets
# ---------------------------------------------------------------------------
def ensure_exclusive_datasets(db, sm) -> dict:
    from superset.connectors.sqla.models import SqlaTable

    tables = db.session.query(SqlaTable).order_by(SqlaTable.id).all()
    exclusive = [t for t in tables if t.table_name not in SHARED_TABLES and t.table_name != "neurons_devices"]

    assignment: dict[int, str] = {}
    unpinned = [t for t in exclusive if t.table_name not in EXCLUSIVE_PINNED]
    for t in exclusive:
        if t.table_name in EXCLUSIVE_PINNED:
            assignment[t.id] = EXCLUSIVE_PINNED[t.table_name]
    for i, t in enumerate(unpinned):
        assignment[t.id] = TENANT_A if i % 2 == 0 else TENANT_B

    made = {"grants": 0, "rls": 0}
    for t in exclusive:
        tenant = assignment[t.id]
        role = _tenant_role(sm, db, tenant)
        subject = _role_subject(db, role)

        if _grant_datasource_access(sm, role, t):
            made["grants"] += 1

        name = f"tenant_scope_{t.table_name}"[:255]
        if _upsert_rls(db, name, t, [subject], "1 = 1", f"tenant_{tenant}"):
            made["rls"] += 1

    db.session.commit()
    print(f"MARK exclusive datasets: {len(exclusive)} bound -- "
          f"A={sum(1 for v in assignment.values() if v == TENANT_A)} "
          f"B={sum(1 for v in assignment.values() if v == TENANT_B)}")
    return made, {t.table_name: assignment[t.id] for t in exclusive}


# ---------------------------------------------------------------------------
# 3. Neurons-shaped synthetic dataset + per-tenant dashboards
# ---------------------------------------------------------------------------
def _gen_neurons_rows(n_per_tenant: int = 200) -> "pd.DataFrame":
    import pandas as pd

    rng = random.Random(20260913)  # fixed seed: deterministic re-runs
    oses = [
        ("Windows 11", "23H2"), ("Windows 10", "22H2"), ("macOS", "14.5"),
        ("macOS", "13.6"), ("Ubuntu", "22.04"), ("Ubuntu", "24.04"),
    ]
    groups = ["IT", "Finance", "Sales", "Engineering", "Support", "Executive"]
    states = ["compliant", "non_compliant", "pending"]
    state_weights = [0.7, 0.2, 0.1]
    now = datetime(2026, 9, 13, 12, 0, 0)

    rows = []
    device_seq = 1
    for tenant in (TENANT_A, TENANT_B):
        for _ in range(n_per_tenant):
            os_name, os_version = rng.choice(oses)
            # most devices seen recently; a stale tail (>30 days) for the
            # "stale devices" table to have real content.
            age_days = rng.choice([rng.randint(0, 14)] * 6 + [rng.randint(31, 120)] * 2 + [rng.randint(15, 30)] * 2)
            last_seen = now - timedelta(days=age_days, hours=rng.randint(0, 23))
            rows.append(
                {
                    "tenant_id": tenant,
                    "device_id": f"DEV-{device_seq:05d}",
                    "hostname": f"host-{tenant[:8]}-{device_seq:05d}",
                    "os": os_name,
                    "os_version": os_version,
                    "compliance_state": rng.choices(states, weights=state_weights)[0],
                    "last_seen": last_seen,
                    "owner_group": rng.choice(groups),
                }
            )
            device_seq += 1
    return pd.DataFrame(rows)


def ensure_neurons_dataset(db, sm) -> "SqlaTable":
    from sqlalchemy import inspect
    from sqlalchemy import DateTime, String
    from superset.utils.database import get_example_database
    from superset.examples.helpers import get_table_connector_registry

    database = get_example_database()
    tbl_name = "neurons_devices"

    with database.get_sqla_engine() as engine:
        schema = inspect(engine).default_schema_name
        table_exists = tbl_name in inspect(engine).get_table_names()
        if not table_exists:
            df = _gen_neurons_rows()
            df.to_sql(
                tbl_name, engine, schema=schema, if_exists="replace",
                index=False, method="multi", chunksize=500,
                dtype={
                    "tenant_id": String(36), "device_id": String(32),
                    "hostname": String(128), "os": String(64),
                    "os_version": String(32), "compliance_state": String(32),
                    "last_seen": DateTime, "owner_group": String(64),
                },
            )
            print(f"MARK neurons_devices: created table, {len(df)} rows")

    table_cls = get_table_connector_registry()
    ds = db.session.query(table_cls).filter_by(table_name=tbl_name, schema=schema).one_or_none()
    if ds is None:
        ds = table_cls(table_name=tbl_name, schema=schema)
        db.session.add(ds)
    ds.database = database
    ds.fetch_metadata()
    db.session.commit()

    role_a = _tenant_role(sm, db, TENANT_A)
    role_b = _tenant_role(sm, db, TENANT_B)
    subj_a = _role_subject(db, role_a)
    subj_b = _role_subject(db, role_b)
    _grant_datasource_access(sm, role_a, ds)
    _grant_datasource_access(sm, role_b, ds)
    _upsert_rls(db, "tenant_scope_neurons_devices_A", ds, [subj_a], f"tenant_id = '{TENANT_A}'", f"tenant_{TENANT_A}")
    _upsert_rls(db, "tenant_scope_neurons_devices_B", ds, [subj_b], f"tenant_id = '{TENANT_B}'", f"tenant_{TENANT_B}")
    db.session.commit()
    return ds


def _chart_params(ds_id: int, viz_type: str, **extra) -> str:
    from superset.utils import json

    base = {"datasource": f"{ds_id}__table", "viz_type": viz_type, "adhoc_filters": []}
    base.update(extra)
    return json.dumps(base)


def ensure_neurons_dashboards(db, sm, ds) -> list[tuple[str, str, str]]:
    from superset.models.dashboard import Dashboard
    from superset.models.slice import Slice
    from superset.subjects.utils import get_or_create_user_subject
    from superset.utils.core import DatasourceType

    results = []
    for tenant, admin_guid, label in ((TENANT_A, ADA, "Tenant A"), (TENANT_B, CLEO, "Tenant B")):
        admin = sm.find_user(username=admin_guid)
        if admin is None:
            print(f"MARK WARNING: no Superset user {admin_guid}; skipping neurons dashboard for {tenant}")
            continue
        subject = get_or_create_user_subject(admin.id)

        dash_title = f"{label} Device Compliance"
        dash = db.session.query(Dashboard).filter_by(dashboard_title=dash_title).one_or_none()
        if dash is None:
            dash = Dashboard(dashboard_title=dash_title, slug=None)
            db.session.add(dash)
        dash.created_by_fk = admin.id
        dash.changed_by_fk = admin.id
        if subject is not None and subject not in (dash.editors or []):
            dash.editors.append(subject)

        chart_specs = [
            ("compliance by state", "pie", _chart_params(
                ds.id, "pie", groupby=["compliance_state"], metric="count",
                row_limit=100, show_legend=True,
            )),
            ("devices by OS", "echarts_timeseries_bar", _chart_params(
                ds.id, "echarts_timeseries_bar", groupby=["os"], metrics=["count"],
                x_axis="last_seen", time_grain_sqla="P1M", row_limit=100,
                only_total=True, order_desc=True, orientation="vertical",
                x_axis_sort_asc=True,
            )),
            ("stale devices", "table", _chart_params(
                ds.id, "table",
                all_columns=["hostname", "os", "os_version", "last_seen", "owner_group", "compliance_state"],
                query_mode="raw", row_limit=200, order_by_cols=[],
            )),
        ]

        slices = []
        for short_name, viz_type, params in chart_specs:
            slice_name = f"{label}: {short_name}"
            slc = db.session.query(Slice).filter_by(slice_name=slice_name).one_or_none()
            if slc is None:
                slc = Slice(
                    slice_name=slice_name,
                    viz_type=viz_type,
                    datasource_id=ds.id,
                    datasource_type=DatasourceType.TABLE,
                )
                db.session.add(slc)
            slc.params = params
            slc.datasource_id = ds.id
            slc.datasource_type = DatasourceType.TABLE
            slc.created_by_fk = admin.id
            slc.changed_by_fk = admin.id
            if subject is not None and subject not in (slc.editors or []):
                slc.editors.append(subject)
            slices.append(slc)
        db.session.flush()

        for slc in slices:
            if slc not in (dash.slices or []):
                dash.slices.append(slc)

        db.session.commit()
        results.append((dash_title, f"{admin.first_name} ({admin_guid[:8]})", tenant))
        print(f"MARK neurons dashboard: {dash_title} owner={admin.first_name} tenant={tenant} charts={len(slices)}")

    return results


def run() -> None:
    from superset import db, security_manager as sm

    shared = ensure_shared_datasets(db, sm)
    print("MARK ensure_shared_datasets:", shared)

    exclusive, assignment = ensure_exclusive_datasets(db, sm)
    print("MARK ensure_exclusive_datasets:", exclusive)

    neurons_ds = ensure_neurons_dataset(db, sm)
    neurons_dashboards = ensure_neurons_dashboards(db, sm, neurons_ds)

    print("MARK seed_tenant_data: done", {
        "shared_tables": list(SHARED_TABLES),
        "exclusive_tables": len(assignment),
        "neurons_dashboards": len(neurons_dashboards),
    })


if __name__ == "__main__":
    from superset.app import create_app

    app = create_app()
    with app.app_context():
        run()

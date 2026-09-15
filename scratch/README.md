# scratch/ -- a brownfield multi-tenant instance for PCS-10243

The demo stack at the repo root (`docker-compose.pcs-setup.yml`) starts from
an **empty** instance: one admin user, `load_examples` off. That is fine for
proving the overlay lands and wires up, but it is not what a real deployment
looks like, and it is not a good test of the ownership **backfill** (there
is nothing pre-existing to backfill) or of Ivanti's actual data shape (one
Superset, many tenants, real row-level tenancy enforced by RLS).

`scratch/` builds the opposite: a **brownfield** instance shaped like a real
Ivanti multi-tenant PCS deployment --

- two tenants, each with JIT-shaped users (username = member GUID v4, role
  `tenant_<guid>`)
- a directory of ~22 people across both tenants, with OpenFGA-only role
  groups (`dashboard_designer`, `data_analyst`, `finance_reporting`, ...) and
  a tenant administrator group per tenant
- the stock Superset example dashboards/charts (`superset load_examples`),
  deterministically attributed to real tenant users as their creators/native
  editors -- a real "these were created by tenant users" history, and
  dataset-access-aware: no dashboard is ever attributed to a tenant that
  cannot read one of its own charts
- **real row-level tenancy**, not just per-dataset binding: three of the
  example datasets are genuinely SHARED between both tenants, with a real
  `tenant_id` column and one RLS filter per tenant role -- the same chart
  renders different rows depending on who's asking. Every other example
  dataset (Ivanti's more common case) is EXCLUSIVE to one tenant. A
  Neurons-shaped synthetic device-inventory dataset (shared, like the first
  group) backs one "Device Compliance" dashboard per tenant.
- a NEW OpenFGA store in the same `pcssetup` OpenFGA instance (the demo
  store from `docker/seed.sh` is untouched)

...and then proves the ownership migration's backfill against it: every
pre-existing dashboard/chart ends up with an explicit `ownership_object`
row, visibility **public**, owner = the tenant user who "created" it,
tenant stamped -- on a database that already looks like an existing
customer's, not a fresh install.

Same compose project (`pcssetup`), same postgres/redis/openfga services as
the demo stack -- only the `superset`/`worker`/`init` services are
repointed at separate databases (`superset_scratch`, `examples_scratch` --
see below) and a separate OpenFGA store. Building the scratch stack
**replaces** the demo stack's `superset`/`worker` containers (same names,
same port 8097); it does not run alongside it.

**Why two databases (`superset_scratch` AND `examples_scratch`)**: the
stock `SQLALCHEMY_EXAMPLES_URI` default is a SQLite file inside each
container's own non-persistent filesystem. It is written by whichever
one-off container ran `superset load_examples` and is gone the moment that
container exits -- the NEXT `superset`/`worker` container starts with an
empty SQLite file of its own. Real row-level tenancy needs `ALTER TABLE`
and durable `UPDATE`s on the actual data, and needs `superset`, `worker`
and one-off `init` runs to all see the SAME data -- so `scratch/
docker-compose.scratch.yml` repoints the "examples" connection
(`SUPERSET__SQLALCHEMY_EXAMPLES_URI`) at a second Postgres database on the
same server instead.

## Build it

```bash
./scratch/seed-multitenant.sh
```

Idempotent end to end, and always re-runnable from a dropped database (see
"Reset it" below) -- re-running it against an EXISTING scratch DB
repairs/no-ops rather than duplicating. It always uses:

```
docker compose -p pcssetup \
  -f docker-compose.pcs-setup.yml -f scratch/docker-compose.scratch.yml \
  <cmd>
```

### Ordering matters

A real brownfield customer instance has content, tenant users and
row-level data FIRST, and only THEN gets the ownership migration applied
to it. Getting this backwards is a real trap: `superset_ownership.
backfill.run()` never overwrites a row that already has an owner. If the
ownership migration ran before any tenant user existed, `load_examples`'
own content (its `created_by_fk` defaults to the first/only user, i.e.
admin, at that point) would get backfilled to admin-owned rows
**permanently** -- attributing `created_by_fk` to real tenant users
afterwards would then change nothing, because the rows are already
"owned". `seed-multitenant.sh` keeps `OWNERSHIP_ENABLED=false` (entrypoint-
ownership.sh's migration/backfill/check are gated on it) all the way
through attribution and row-level tenancy, and only flips it true and
restarts `superset` ONCE, at the very end.

What it does, in order (see the script's own header comment for the exact
mapping, and each script's own header for the full reasoning):

1. Creates the `superset_scratch` and `examples_scratch` Postgres databases
   (same postgres service) and a NEW OpenFGA store (`pcs-scratch`),
   installs the pinned ownership model into it, writes `scratch/
   .scratch-out/store.env` (gitignored).
2. Runs the base compose file's `init` service against that database/store
   with `OWNERSHIP_ENABLED=false`: stock `superset db upgrade`, admin user,
   `superset init`, `superset load_examples` -- the content. The
   entrypoint's ownership migration/backfill/check does NOT run yet -- the
   switch is off, so this is a true pre-migration brownfield state.
3. Brings up `superset`/`worker` on the scratch DB/store (still
   `OWNERSHIP_ENABLED=false`).
4. Seeds identities and a directory INSIDE the running container:
   `overlay/qa/seed_identity.py` (Ada, Ben, Cleo -- the three original
   fixture users) and `overlay/qa/seed_directory.py` (the other ~19),
   both run **verbatim** -- neither hardcodes FGA/STORE/PYTHONPATH, both
   resolve the connection from the running app's own config, which by now
   points at the scratch store. Plus `scratch/seed_fga_extra.py` (see its
   own header): additive tuples neither qa script writes -- each group's
   `tenant` relation, the `tenant_administrator_<tenant>` groups (Ada
   administers tenant A, Cleo tenant B), and the nested "every tenant
   administrator is also a dashboard_designer" membership.
5. `scratch/seed_tenant_data.py`: real row-level tenancy -- see "The data
   model" below. This REPLACES `overlay/qa/seed_tenant_env.py` in this
   pipeline; see that script's own header for why (it binds every dataset
   exclusively to one tenant and would fight the shared ones).
6. `scratch/assign_examples.py`: deterministic, **dataset-access-aware**
   ownership attribution of every example dashboard (and its charts) --
   sets `created_by_fk`/`changed_by_fk` AND the native `dashboard_editors`/
   `chart_editors` rows (this build's replacement for the old `owners`
   many-to-many; `Dashboard`/`Slice` have no `owners` relation any more).
   Reads step 5's `datasource_access` grants as ground truth so a
   dashboard is never attributed to a tenant that cannot read one of its
   own charts; a genuine conflict (charts from two different exclusive
   tenants on one dashboard) is resolved by unlinking the minority charts
   from the dashboard (not deleting them) and is printed as a violation.
7. Flips `OWNERSHIP_ENABLED=true` and restarts `superset`/`worker`. THE
   ownership migration runs now, exactly once: `superset ownership db
   upgrade` -> backfill (every dashboard/chart -> public, owner = the
   tenant user steps 4-6 gave it) -> `backfill-tenants` -> `check`.
8. `superset ownership plugin verify --tenant <A> --sample-users 10`.
9. Proof queries (`scratch/proof-queries.sh`, also runnable standalone).

## The data model

Every example dataset falls into one of three kinds:

| kind | datasets | access | RLS |
|---|---|---|---|
| **SHARED** (real row-level tenancy) | `cleaned_sales_data`, `birth_names`, `video_game_sales` | `datasource_access` granted to BOTH tenant roles | one REGULAR filter per tenant role, real clause `tenant_id = '<that tenant's guid>'` |
| **EXCLUSIVE** (Ivanti's per-tenant dataset) | every other example dataset | `datasource_access` granted to exactly ONE tenant role | one REGULAR filter on that role, `1 = 1` (a binding marker, not real per-row tenancy -- the dataset itself is single-tenant) |
| **Neurons-shaped synthetic** | `neurons_devices` (~400 rows, device inventory/compliance) | shared, like the first group | one filter per tenant role, `tenant_id = '<guid>'` |

`VIEWER_PROMISCUOUS_MODE = False` is set explicitly in `scratch/
scratch-superset-config-docker.py` (already Superset's own stock default;
made explicit here because it matters for this model -- see that file's
own comment) so RLS stays authoritative regardless of who shared what chart
or dashboard with whom: a shared chart never widens the querying user's
actual row access.

`cleaned_sales_data`/`birth_names`/`video_game_sales` backfill their
`tenant_id` deterministically (`scratch/seed_tenant_data.py`):

| dataset | rule |
|---|---|
| `cleaned_sales_data` | `deal_size IN ('Small','Medium')` -> tenant A, else tenant B |
| `birth_names` | `state` alphabetically A-M -> tenant A, N-Z (and null) -> tenant B |
| `video_game_sales` | `platform` alphabetically A-M -> tenant A, N-Z -> tenant B |

`neurons_devices` splits 50/50 by construction (200 rows generated per
tenant, fixed random seed).

Every EXCLUSIVE dataset is bound to exactly one tenant, alternating by
dataset id (deterministic; `wb_health_population` pinned to tenant B for
continuity with earlier fixtures) -- see `scratch-run.md` for the actual
per-dataset table this run produced.

Two Neurons dashboards are created directly (not by `assign_examples.py`,
which skips any dashboard titled `... Device Compliance`), each with three
charts, owned/created by that tenant's administrator:

| dashboard | owner | tenant | charts |
|---|---|---|---|
| Tenant A Device Compliance | Ada | A | compliance-by-state (pie), devices-by-OS (bar), stale devices (table) |
| Tenant B Device Compliance | Cleo | B | compliance-by-state (pie), devices-by-OS (bar), stale devices (table) |

## What's in the instance when it's done

- **Tenant A**: `a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40`
- **Tenant B**: `b7f28e5a-9c14-4d6b-a2f0-5e3d8c1a6b92`

| user | GUID (username) | tenant | notes |
|---|---|---|---|
| Ada | `3f0a91c7-2d84-4e63-9b15-7c4e8a2f6d31` | A | tenant A administrator |
| Ben | `6c2b48e9-5a71-4f92-8d03-2e9b7c1a4d53` | A | |
| Marcus Chen | `4302756e-b4aa-4938-b599-ed593444aeaf` | A | |
| Cleo | `9d7e35a1-8c62-4b04-a7f1-3d5e9b2c8a76` | B | tenant B administrator |

...plus 18 more (Priya, Elena, David, Sofia, James, Aisha, Tom, Lena,
Carlos, Yuki, Grace on tenant A; Omar, Hannah, Diego, Mei, Nadia, Peter,
Ruth on tenant B) from `overlay/qa/seed_directory.py` -- deterministic
GUIDs (sha256 of `<tenant>:<first>:<last>`, forced to a valid UUID v4; see
that file's `guid_for()`). Password for every seeded user: **test1234**.
Superset admin: **admin / admin**.

Dashboard attribution (`scratch/assign_examples.py`'s pin rules -- a
*preference*, not a guarantee: dataset access is ground truth and wins
when the two disagree, printed as a `pin mismatch`):

| dashboard title contains | preferred owner | preferred tenant |
|---|---|---|
| "Video Game Sales", "Births" | Ada | A |
| "Sales", "Slack" | Marcus | A |
| "FCC", "deck.gl" | Cleo | B |
| everything else | round-robin, filtered to the tenant(s) that can actually read the dashboard's charts | A or B |

"Video Game Sales"/"Births"/"Sales" land exactly on the preference (Ada/
Ada/Marcus, all tenant A) because their datasets are the SHARED ones.
"Slack", "FCC" and "deck.gl" do NOT: their datasets are EXCLUSIVE, and
`scratch/seed_tenant_data.py`'s deterministic (alternating-by-id) exclusive
binding happened to land every one of their underlying datasets on the
OTHER tenant from the preference table above -- Slack ended up owned by
Hannah Berg (B, not Marcus/A), FCC by Elena Rossi (A, not Cleo/B), deck.gl
by David Okafor (A, not Cleo/B). Every one of these is printed as a `pin
mismatch` by `assign_examples.py` and is visible, not silently papered
over -- see `scratch-run.md`'s actual output for the exact lines, plus
four genuine `VIOLATION`s (a dashboard whose charts spanned two different
exclusive tenants, resolved by unlinking the minority charts from the
dashboard). The end state is internally 100% consistent regardless: every
dashboard's owner belongs to a tenant that can read every chart still on
it -- `scratch-run.md`'s proof queries confirm this by construction (the
"every dashboard: owner + tenant" and "per dashboard: datasets ... and
which tenant(s) can read them" tables agree on every row).

Every other example dashboard/chart's `created_by_fk`/`changed_by_fk` and
`dashboard_editors`/`chart_editors` rows are set the same way -- see
`scratch-run.md` for the actual dashboard -> owner -> tenant -> dataset-kind
table this run produced.

## Reset it

```bash
# drop the scratch databases (same postgres service, demo DB untouched)
docker exec pcssetup-postgres-1 psql -U superset -d superset -c "DROP DATABASE superset_scratch"
docker exec pcssetup-postgres-1 psql -U superset -d superset -c "DROP DATABASE examples_scratch"
# delete the scratch OpenFGA store (id in scratch/.scratch-out/store.env)
curl -X DELETE "http://localhost:8199/stores/$(grep SCRATCH_OPENFGA_STORE_ID scratch/.scratch-out/store.env | cut -d= -f2)"
rm -rf scratch/.scratch-out
```

Then re-run `./scratch/seed-multitenant.sh` -- the whole pipeline is
designed to run end to end from nothing every time.

## Proof

See `scratch-run.md` for this delivery's actual run: the step-by-step
output, the `plugin verify` block, and the proof queries --
`ownership_object` counts by visibility and by owner's tenant, every
dashboard with its owner/tenant/visibility, per-dashboard dataset/tenant
readability, the RLS filter table (dataset, role, clause), RLS filter count
per tenant role, `alembic_version` vs `alembic_version_ownership`, row
counts by `tenant_id` for every shared/Neurons dataset, and the API proof:
the SAME chart (on a shared dataset) fetched as Ada and as Cleo, row counts
that differ and match the `tenant_id` split.

## What's scratch-only vs. verbatim from overlay/qa

`overlay/` is generated from client-test main (`scripts/sync-from-main.sh`)
and is never hand-edited here. `seed-multitenant.sh` runs two of its
scripts unmodified -- `seed_identity.py` and `seed_directory.py`, neither
of which hardcodes FGA/STORE/PYTHONPATH, so nothing needed adapting to
point them at the scratch store instead of pcs617's.

`overlay/qa/seed_tenant_env.py` is NOT run in this pipeline (a deliberate
diff from the original plan, not a silent drop): it binds EVERY dataset
exclusively to one tenant, which conflicts with three datasets this
pipeline needs genuinely SHARED with real per-row tenancy. Its exclusive-
binding behaviour (datasource_access + a `1 = 1` marker RLS filter) is
reproduced for the non-shared datasets directly in `scratch/
seed_tenant_data.py`; its "every tenant has an administrator" step is
redundant with `scratch/seed_fga_extra.py`; its cross-tenant-dashboard
resolution is superseded by the stronger, dataset-access-aware invariant
`scratch/assign_examples.py` now enforces. See both scripts' own headers.

Scratch-only additions:

- `scratch/seed_fga_extra.py` -- additive OpenFGA tuples `seed_directory.py`
  does not write (group `tenant` relations, `tenant_administrator_<tenant>`
  groups, nested membership). See its own header for exactly why.
- `scratch/seed_tenant_data.py` -- real row-level tenancy: shared datasets
  with a `tenant_id` column + per-tenant RLS, exclusive dataset binding
  (see above), and the Neurons-shaped synthetic dataset + dashboards.
- `scratch/assign_examples.py` -- dataset-access-aware ownership
  attribution of `load_examples`' content; nothing in `overlay/qa/` does
  this at all.
- `scratch/scratch-superset-config-docker.py` -- wraps `docker/
  demo-superset-config-docker.py` verbatim and adds one explicit setting
  (`VIEWER_PROMISCUOUS_MODE = False`) the scratch data model depends on;
  see that file's own header.
- `scratch/proof-queries.sh` -- the proof queries above, runnable
  standalone at any time.

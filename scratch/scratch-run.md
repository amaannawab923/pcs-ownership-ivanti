# scratch/ -- actual run output

Command: `./scratch/seed-multitenant.sh`, run end to end from a dropped
`superset_scratch`/`examples_scratch` (and a freshly created OpenFGA store)
-- the full pipeline described in `scratch/README.md`. Two prior runs
surfaced (and are fixed in the committed scripts) three real bugs:

1. **Ordering trap**: the ownership migration must never run before tenant
   users/attribution exist, or `load_examples`' admin-attributed content
   gets backfilled to admin permanently (`backfill.run()` never overwrites
   an already-owned row). Fixed by keeping `OWNERSHIP_ENABLED=false`
   through attribution and flipping it once, at the end (see `docker/
   entrypoint-ownership.sh` and `scratch/seed-multitenant.sh`'s header
   comments).
2. `superset_ownership.backfill.run()` needs an app context pushed around
   it (`create_app()` + `with app.app_context():`) -- it is a bare
   function, not a Flask CLI command. `python -c "from
   superset_ownership.backfill import run; run()"` alone fails before
   touching a single row. Fixed in `docker/entrypoint-ownership.sh`.
3. `dashboard_editors`/`chart_editors` point at this build's `Subject`
   abstraction, not `ab_user` directly -- appending a bare `User` raises
   `FlushError`. Fixed in `scratch/assign_examples.py` and
   `scratch/seed_tenant_data.py` (`get_or_create_user_subject`).

This is the clean, reproducible run against the corrected scripts (a
second independent run produced byte-identical MARK/violation/backfill/
proof output).

## Store

```
created store: 01M2CYH8GYHJEF1M2Q8524Y3CS
installed model: 01M2CYHJPW25WKXC3RY5WDFQWH
```

The pcssetup instance's pre-existing demo store (`01M2CSJA35ZGKJTKWZYRNQ1CMF`)
was never touched (confirmed before and after via `GET /stores` on the same
OpenFGA instance).

## Step 5: real row-level tenancy (`scratch/seed_tenant_data.py`)

```
MARK ensure_shared_datasets: {'columns_added': 3, 'rows_backfilled': {'cleaned_sales_data': 2823, 'birth_names': 75691, 'video_game_sales': 16595}, 'grants': 5, 'rls': 6}
MARK ensure_exclusive_datasets: {'grants': 17, 'rls': 18}
MARK neurons_devices: created table, 400 rows
MARK neurons dashboard: Tenant A Device Compliance owner=Ada tenant=a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40 charts=3
MARK neurons dashboard: Tenant B Device Compliance owner=Cleo tenant=b7f28e5a-9c14-4d6b-a2f0-5e3d8c1a6b92 charts=3
MARK seed_tenant_data: done {'shared_tables': ['cleaned_sales_data', 'birth_names', 'video_game_sales'], 'exclusive_tables': 18, 'neurons_dashboards': 2}
```

## Step 6: dataset-access-aware attribution (`scratch/assign_examples.py`)

```
  id  dashboard                                   owner                       tenant      charts
----------------------------------------------------------------------------------------------------
   1  Sales Dashboard                             Marcus (4302756e)           a1e4c2d0    10
   2  Misc Charts                                 Omar (60f317ea)             b7f28e5a    2
   3  Featured Charts                             Priya (9fcc709c)            a1e4c2d0    24
   4  Slack Dashboard                             Hannah (e64caf0a)           b7f28e5a    5
   5  World Bank's Data                           Diego (a105bfbe)            b7f28e5a    9
   6  FCC New Coder Survey 2018                   Elena (58a4685c)            a1e4c2d0    21
   7  USA Births Names                            Ada (3f0a91c7)              a1e4c2d0    10
   8  Video Game Sales                            Ada (3f0a91c7)              a1e4c2d0    8
   9  deck.gl Demo                                David (12fc0874)            a1e4c2d0    6

MARK assign_examples: 9 dashboards attributed, 7 violation(s)
MARK   VIOLATION dashboard 2 'Misc Charts': charts spanned tenants {'a1e4c2d0...': 1, 'b7f28e5a...': 2}; kept b7f28e5a, removed from dashboard: ['Unicode Cloud']
MARK   VIOLATION dashboard 3 'Featured Charts': charts spanned tenants {'a1e4c2d0...': 1, 'b7f28e5a...': 1}; kept a1e4c2d0, removed from dashboard: ['Gantt']
MARK   VIOLATION dashboard 4 'Slack Dashboard': charts spanned tenants {'a1e4c2d0...': 4, 'b7f28e5a...': 5}; kept b7f28e5a, removed from dashboard: ['Weekly Messages', 'Cross Channel Relationship', 'Cross Channel Relationship heatmap_v2', 'New Members per Month']
MARK   pin mismatch dashboard 4 'Slack Dashboard': pinned a1e4c2d0 not in compatible ['b7f28e5a']
MARK   pin mismatch dashboard 6 'FCC New Coder Survey 2018': pinned b7f28e5a not in compatible ['a1e4c2d0']
MARK   VIOLATION dashboard 9 'deck.gl Demo': charts spanned tenants {'a1e4c2d0...': 6, 'b7f28e5a...': 1}; kept a1e4c2d0, removed from dashboard: ['Deck.gl Arcs']
MARK   pin mismatch dashboard 9 'deck.gl Demo': pinned b7f28e5a not in compatible ['a1e4c2d0']
```

Plus the two Neurons dashboards (10, 11), attributed directly by
`seed_tenant_data.py` and skipped by `assign_examples.py`, for 11
dashboards total. Every `VIOLATION`/`pin mismatch` is explained in
`scratch/README.md`'s "Dashboard attribution" section -- the short version:
`assign_examples.py`'s pin rules are a *preference*; dataset access (what
`seed_tenant_data.py` actually granted) is ground truth and wins when they
disagree, and every disagreement is printed, not silently resolved.

## Step 8: ownership migration (runs once, over already-attributed data)

```
[entrypoint-ownership] superset ownership db upgrade (parallel chain, alembic_version_ownership)
{"chain": "ownership", "current": "0003_ownership_foreign_keys", "upgraded_to": "head"}
[entrypoint-ownership] superset ownership backfill (every pre-existing chart/dashboard -> public, owner=creator; idempotent)
superset_ownership backfill [dashboard]: 0 created, 11 re-attributed, 11 tenant stamped, 0 still UNOWNED
superset_ownership backfill [chart]: 0 created, 108 re-attributed, 108 tenant stamped, 1 still UNOWNED
[entrypoint-ownership] backfill summary: [dashboard]: 0 created, 11 re-attributed, 11 tenant stamped, 0 still UNOWNED;[chart]: 0 created, 108 re-attributed, 108 tenant stamped, 1 still UNOWNED
[entrypoint-ownership] superset ownership backfill-tenants (stamp a tenant on any object the sweep above could not)
[entrypoint-ownership] superset ownership check
```

`superset ownership check` (queried directly after the run, `docker exec
<superset> superset ownership check`):

```
ok = True
enabled_backend = True
enabled_ui = True
enabled_ui_runtime = True
flags_agree = True
ungoverned = None
mismatches = None
group_id_mismatch = []
group_untenanted = []
```

The 1 still-UNOWNED chart: `chart 83 "Pivot Table v2"` -- an orphaned
example chart from `load_examples` that is not linked to ANY dashboard
(`SELECT * FROM dashboard_slices WHERE slice_id = 83` returns 0 rows), so
`assign_examples.py` never had a dashboard through which to attribute it,
its `created_by_fk`/`changed_by_fk` are both NULL, and its dataset
(`birth_names`) is SHARED so there is no single tenant administrator to
fall back to either. Backfilled to `public`/unowned exactly as designed
(readable by everyone, claimable by an administrator) -- not a bug.

## `superset ownership plugin verify --tenant <A> --sample-users 10`

```
== load ==
  PASS plugins_load  authorizer, directory and identity all loaded
== connection ==
  PASS reachable  store answered
  PASS model_pinned  pinned to 01M2CYHJPW25WKXC3RY5WDFQWH
  PASS required_relations  every required type.relation is present
  PASS scratch_tuple_roundtrip  write/write/delete/delete all succeeded
== vocabulary ==
  PASS group_has_tenant_tuple  every walked group has a tenant tuple
  PASS member_guids_resolve  all 13 member(s) resolve to a Superset user
  PASS administrator_group_exists  group:tenant_administrator_a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40 exists
  PASS no_member_in_two_tenants  no member belongs to two tenants
  PASS group_ids_parse  every listed group id parses
== directory ==
  PASS tenant_isolation  a1e4c2d0-... and b7f28e5a-... share no groups
  PASS pagination_roundtrips  13 member(s), same set at both page sizes
  SKIP not_found_vs_empty  no known-empty group in this tenant to probe
  PASS user_in_group_agrees  90 pair(s) agree
  PASS health  model 01M2CYHJPW25WKXC3RY5WDFQWH: group.tenant present
  PASS latency  p95=1.6ms over 193 call(s)
== identity ==
  PASS guid_v4  10 sampled, all GUID v4
  FAIL tenant_known  tenant not known to the store for: ['3']
  PASS normalize_subject_roundtrip  10 sampled, all round-trip
== invariants ==
  SKIP owner_zero_calls  pass --object and --as to run this section
  SKIP non_owner_one_check  pass --object and --as to run this section
  SKIP public_zero_calls  pass --object and --as to run this section
== hooks ==
  SKIP resolved  no hook configured
  PASS shape_pair_roundtrip  11 group id(s) round-trip
  SKIP caller_side / directory_side / read_only / can_manage_narrow_only / fail_closed  no hook configured
SUMMARY pass=18 warn=0 fail=1 skip=10
```

**The one FAIL, explained**: `tenant_known` samples up to 10 "JIT users"
and checks each resolves to a known tenant in the store. The sample
includes Superset's own `admin` user (ab_user id `3` in this run's
particular numbering) -- `admin`'s username is not a member GUID at all
(it was never meant to be a JIT/Ivanti-provisioned identity), so
`identity.member_guid(admin)` returns nothing and the check reports it as
"tenant not known." This is expected for any instance that still has a
plain `admin` account seeded alongside JIT users, not a defect in the
seeding -- passing `--object`/`--as` (or excluding `admin` from the JIT
sample, which is the plugin_verify harness's own choice, not something
this delivery controls) would remove it, but it is unaffected by anything
`scratch/` seeds and is unrelated to the ownership feature itself.

Re-running the same command with `--tenant b7f28e5a-9c14-4d6b-a2f0-5e3d8c1a6b92`
(tenant B) produces the same shape (same FAIL, same reason -- `admin` is in
every sample regardless of which tenant is queried).

## Proof queries (`scratch/proof-queries.sh`)

```
--- ownership_object counts by visibility ---
 visibility | count
------------+-------
 public     |   120

--- ownership_object counts by owner's tenant ---
                tenant                | objects
--------------------------------------+---------
 a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40 |      94
 b7f28e5a-9c14-4d6b-a2f0-5e3d8c1a6b92 |      25
 (unowned)                            |       1

--- every dashboard: owner + tenant + visibility ---
 id |      dashboard_title       |     owner     |             owner_tenant             | visibility
----+----------------------------+---------------+--------------------------------------+------------
  1 | Sales Dashboard            | Marcus Chen   | a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40 | public
  2 | Misc Charts                | Omar Farouk   | b7f28e5a-9c14-4d6b-a2f0-5e3d8c1a6b92 | public
  3 | Featured Charts            | Priya Sharma  | a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40 | public
  4 | Slack Dashboard            | Hannah Berg   | b7f28e5a-9c14-4d6b-a2f0-5e3d8c1a6b92 | public
  5 | World Bank's Data          | Diego Alvarez | b7f28e5a-9c14-4d6b-a2f0-5e3d8c1a6b92 | public
  6 | FCC New Coder Survey 2018  | Elena Rossi   | a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40 | public
  7 | USA Births Names           | Ada tenant A  | a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40 | public
  8 | Video Game Sales           | Ada tenant A  | a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40 | public
  9 | deck.gl Demo               | David Okafor  | a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40 | public
 10 | Tenant A Device Compliance | Ada tenant A  | a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40 | public
 11 | Tenant B Device Compliance | Cleo tenant B | b7f28e5a-9c14-4d6b-a2f0-5e3d8c1a6b92 | public

--- per dashboard: datasets its charts use, and which tenant(s) can read them (via RLS binding) ---
 dashboard_id |      dashboard_title       |        dataset         |                    readable_by_tenant
--------------+----------------------------+------------------------+----------------------------------------------------------
            1 | Sales Dashboard            | cleaned_sales_data     | a1e4c2d0-..., b7f28e5a-...   (SHARED)
            2 | Misc Charts                | birth_france_by_region | b7f28e5a-...
            2 | Misc Charts                | wb_health_population   | b7f28e5a-...
            3 | Featured Charts            | cleaned_sales_data     | a1e4c2d0-..., b7f28e5a-...   (SHARED)
            3 | Featured Charts            | hierarchical_dataset   | a1e4c2d0-...
            4 | Slack Dashboard            | members_channels_2     | b7f28e5a-...
            4 | Slack Dashboard            | messages_channels      | b7f28e5a-...
            4 | Slack Dashboard            | threads                | b7f28e5a-...
            4 | Slack Dashboard            | users                  | b7f28e5a-...
            5 | World Bank's Data          | wb_health_population   | b7f28e5a-...
            6 | FCC New Coder Survey 2018  | FCC 2018 Survey        | a1e4c2d0-...
            7 | USA Births Names           | birth_names            | a1e4c2d0-..., b7f28e5a-...   (SHARED)
            8 | Video Game Sales           | video_game_sales       | a1e4c2d0-..., b7f28e5a-...   (SHARED)
            9 | deck.gl Demo               | bart_lines             | a1e4c2d0-...
            9 | deck.gl Demo               | long_lat               | a1e4c2d0-...
            9 | deck.gl Demo               | sf_population_polygons | a1e4c2d0-...
           10 | Tenant A Device Compliance | neurons_devices        | a1e4c2d0-..., b7f28e5a-...   (SHARED)
           11 | Tenant B Device Compliance | neurons_devices        | a1e4c2d0-..., b7f28e5a-...   (SHARED)

Every dashboard's owner_tenant (above) is a member of every one of its remaining
charts' readable_by_tenant set -- the invariant holds on every row, by construction.

--- RLS filter table: dataset, role, clause (26 rows total) ---
 birth_names          | tenant_a1e4c2d0-... | tenant_id = 'a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40'
 birth_names          | tenant_b7f28e5a-... | tenant_id = 'b7f28e5a-9c14-4d6b-a2f0-5e3d8c1a6b92'
 cleaned_sales_data    | tenant_a1e4c2d0-... | tenant_id = 'a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40'
 cleaned_sales_data    | tenant_b7f28e5a-... | tenant_id = 'b7f28e5a-9c14-4d6b-a2f0-5e3d8c1a6b92'
 video_game_sales      | tenant_a1e4c2d0-... | tenant_id = 'a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40'
 video_game_sales      | tenant_b7f28e5a-... | tenant_id = 'b7f28e5a-9c14-4d6b-a2f0-5e3d8c1a6b92'
 neurons_devices       | tenant_a1e4c2d0-... | tenant_id = 'a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40'
 neurons_devices       | tenant_b7f28e5a-... | tenant_id = 'b7f28e5a-9c14-4d6b-a2f0-5e3d8c1a6b92'
 bart_lines            | tenant_a1e4c2d0-... | 1 = 1
 FCC 2018 Survey       | tenant_a1e4c2d0-... | 1 = 1
 ... (18 exclusive datasets total, each one filter, clause "1 = 1")

--- RLS filter count per tenant role ---
 tenant_a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40 |  13
 tenant_b7f28e5a-9c14-4d6b-a2f0-5e3d8c1a6b92 |  13

--- alembic_version vs alembic_version_ownership ---
 core      | 39097d124752
 ownership | 0003_ownership_foreign_keys

--- cleaned_sales_data: count(*) by tenant_id ---
 a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40 | 2666
 b7f28e5a-9c14-4d6b-a2f0-5e3d8c1a6b92 |  157

--- birth_names: count(*) by tenant_id ---
 a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40 | 20170
 b7f28e5a-9c14-4d6b-a2f0-5e3d8c1a6b92 | 55521

--- video_game_sales: count(*) by tenant_id ---
 a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40 |  4363
 b7f28e5a-9c14-4d6b-a2f0-5e3d8c1a6b92 | 12232

--- neurons_devices: count(*) by tenant_id ---
 a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40 | 200
 b7f28e5a-9c14-4d6b-a2f0-5e3d8c1a6b92 | 200

--- API proof: same shared chart, two tenant users, RLS-scoped row counts ---
  chart under test: id=1 (cleaned_sales_data, datasource=3)
  wrote a minimal query_context onto chart 1 (COUNT(*) over cleaned_sales_data, no groupby)
  Ada  (tenant A) (3f0a91c7-2d84-4e63-9b15-7c4e8a2f6d31): rowcount=1 data=[{'count': 2666}]
  Cleo (tenant B) (9d7e35a1-8c62-4b04-a7f1-3d5e9b2c8a76): rowcount=1 data=[{'count': 157}]
```

**The API proof is the definitive end-to-end result**: `GET /api/v1/
chart/1/data/`, same chart, two different logged-in tenant users
(password `test1234`), and the row counts (2666 vs 157) match the
`cleaned_sales_data` `tenant_id` split exactly -- RLS is doing real,
per-request, per-tenant row filtering on a chart shared between both
tenants, with `VIEWER_PROMISCUOUS_MODE = False` keeping it authoritative
regardless of ownership sharing.

## What did not work / known gaps

- `plugin verify`'s `tenant_known` FAIL, explained above (the `admin`
  account in the JIT-user sample; not something this delivery's seeding
  causes or can fix from the seed side).
- `scratch/assign_examples.py`'s pin table (Slack/FCC/deck.gl -> a
  specific tenant) does not match where those dashboards' EXCLUSIVE
  datasets actually landed under the deterministic alternating-by-id
  binding in `scratch/seed_tenant_data.py`; every mismatch is detected and
  printed (`pin mismatch`), and dataset access (ground truth) wins. See
  `scratch/README.md`'s "Dashboard attribution" section.
- 1 chart (`Pivot Table v2`, id 83) stays unowned -- an orphaned example
  chart not linked to any dashboard, explained above; expected, not a bug.
- The scratch stack REPLACES the demo stack's `superset`/`worker`
  containers (same port 8097) rather than running alongside it -- by
  design (see `scratch/README.md`), not a limitation to fix.

## Stack state at the end of this run

Running at `http://localhost:8097`, `superset_scratch`/`examples_scratch`
databases, OpenFGA store `01M2CYH8GYHJEF1M2Q8524Y3CS`. Logins: `admin` /
`admin`, every seeded tenant user (GUID username) / `test1234` -- e.g. Ada
`3f0a91c7-2d84-4e63-9b15-7c4e8a2f6d31`, Cleo
`9d7e35a1-8c62-4b04-a7f1-3d5e9b2c8a76`. Worker is up (Celery outbox drain
available via `superset ownership outbox drain`, not scheduled by beat in
this demo -- same as the root demo stack).

## Re-run on `pcs-ownership:final` (overlay from client-test main cf5d6c7)

Same pipeline from dropped databases and a fresh store, image built from the
final main (issues #80 #82 #83 #84 #89 #91 #93 closed). Results identical in
shape to the first run:

- entrypoint with `OWNERSHIP_ENABLED=false` skipped the migration during seeding;
  after the flip, one boot ran migration -> backfill -> backfill-tenants -> check:
  `[dashboard]: 0 created, 11 re-attributed, 11 tenant stamped, 0 still UNOWNED;
  [chart]: 0 created, 108 re-attributed, 108 tenant stamped, 1 still UNOWNED`
  (the one orphan example chart); `superset ownership check` -> `ok: true`.
- owners are the tenant users (Ada, Marcus, Priya, Cleo, ...), every object `public`.
- API proof on chart 1 (`cleaned_sales_data`): Ada 2,666 rows, Cleo 157 rows.
- `plugin verify --tenant A`: pass=18 warn=0 fail=1 (admin sampled as a JIT user) skip=11.
- UI/API walk (Playwright): backfilled list; Sales Dashboard as Ada vs Cleo under RLS;
  Ada -> private on Video Game Sales -> Cleo and Ben 404; share to Ben -> 200 after drain;
  tenant admin manages Marcus's dashboard, Cleo cannot; pickers tenant-scoped.
- #93 on the final image: `-> private` revokes the share (Ben 404 immediately,
  0 share rows left), `POST /shares` on a private object -> 409.

## Re-run on `pcs-ownership:wheel` (overlay from client-test main 071d9ee13f; backend as a wheel)

Same stack, run as an IN-PLACE IMAGE UPGRADE (existing `superset_scratch`
database and store kept -- the way a customer instance would take the new
image), image built with the backend installed as the `superset-ownership`
wheel and nothing under `/app/superset` replaced (#104 #105 #106 #107 on
main; `scripts/verify-image.sh` 11/11 PASS, incl. the three formerly
replaced stock files sha256-equal to upstream and the dashboard-patch pins
matching the installed superset). Evidence: `scratch/runs/scratch-rerun-wheel.log`
(the harness), `scratch/runs/scratch-rerun-wheel.evidence.txt` (module
location, `check` JSON, Cleo's listing -- taken from the running containers
after the run) and `scratch/runs/drift-simulation-wheel.txt`.

- `superset_ownership.__file__` in both `superset` and `worker`:
  `/app/.venv/lib/python3.11/site-packages/superset_ownership/__init__.py`;
  nothing on `/app/pythonpath` but the config shim (+ the reference package).
- every boot logs `dashboard-chart access marker installed`; the flip to
  `OWNERSHIP_ENABLED=true` ran migration -> backfill -> backfill-tenants ->
  check once: chain at `0003_ownership_foreign_keys` (head); backfill
  `[dashboard]: 0 created, 0 re-attributed ... 0 still UNOWNED; [chart]: ...
  1 still UNOWNED` (the same orphan example chart; every other row already
  owned from the previous run, never overwritten); `superset ownership
  check` -> `ok: true`, `dashboard_chart_patch_installed: true`.
- API proof on chart 1 (`cleaned_sales_data`): Ada 2,666 rows, Cleo 157
  rows -- identical to the previous run; Cleo lists 8 dashboards.
- drift simulation on the same image (throwaway SQLite, pin deliberately
  broken from the config file): `ownership check` exits 1 with
  `dashboard_chart_patch_installed: false`; `create_app()` (web/worker)
  raises `DashboardPatchError`; `ownership status` proceeds with the ERROR
  line; `ownership backfill-tenants` refuses.

## From-scratch run on `pcs-ownership:pip` -- everything destroyed first, backend from the wheel only

Blast radius before the run: `docker compose -p pcssetup down -v` (every
container and volume of the PCS stack -- metadata Postgres, Redis, the old
in-stack OpenFGA and its data), every `pcs-ownership:*` image, `.seed-out/`,
`scratch/.scratch-out/`, and `docker builder prune -af`. Kept: the stock
`pcs-stock:v6.1.0.7` image (the client's untouched input) and `.pcs-src`.

OpenFGA is now its OWN compose project (`openfga/docker-compose.openfga.yml`,
project `pcsfga`, own Postgres + volume, network `pcsfga`): the PCS stack
joins that network and still reaches it as `http://openfga:8080`, and a
future `down -v` of the stack cannot delete a store. `openfga/up.sh` is
idempotent and is what setup.sh / the scratch harness call; `openfga/down.sh`
is the only thing that deletes stores and asks first.

1. `./setup.sh --pcs-image pcs-stock:v6.1.0.7 --skip-sync --tag pip --up`
   (`scratch/runs/setup-pip.log`): wheel built in the `wheel-builder` stage,
   `dashboard patch pins: ok` at build time, `verify-image.sh` 11/11 PASS,
   demo store created in the standalone instance, demo stack healthy,
   `superset ownership check` -> `ok: true`, `dashboard_chart_patch_installed: true`.
   Two shell fixes surfaced by running the `--up` path cold: `docker/seed.sh`
   now exports `OPENFGA_STORE_ID` right after creating the store (the demo
   config's guard otherwise refuses the CLI call that installs the model) and
   runs `superset db upgrade` before loading the app.
2. `PCS_OWNERSHIP_TAG=pip ./scratch/seed-multitenant.sh` from an empty
   database and a new store (`scratch/runs/scratch-from-scratch-pip.log`):
   identical to the recorded baseline -- `[dashboard]: 0 created, 11
   re-attributed, 11 tenant stamped, 0 still UNOWNED; [chart]: 0 created, 108
   re-attributed, 108 tenant stamped, 1 still UNOWNED`; RLS proof Ada 2,666 /
   Cleo 157 rows. (The `relation "subjects" does not exist` lines in step
   [3/9] are stock Superset's own bootstrap on an empty database, flag off.)
3. Functional walk over the REST API (`scratch/runs/ownership-walk-pip.txt`,
   dashboard 7 "USA Births Names", owner Ada): public -> Ada/Ben/Cleo 200;
   private -> Ada 200, Ben 404, Cleo 404; Ben's PUT visibility -> 403; shared
   with Ben -> Ben 200 after the outbox drain and the `viewer` tuple is in the
   standalone store next to the `owner` and `tenant` tuples; private again
   revokes (Ben 404, POST share -> 409); transfer to Ben -> Ben owner + 200,
   Ada (tenant admin, not owner) 404; restored; `check` ok.
4. UI walk (Chrome, :8097): chart 78 "Girl Name Cloud" private via API ->
   Ben opens dashboard 7 and sees the placeholder tile with the title and
   "reach out to the chart owner: Ada tenant A"; chart list shows Owner /
   Sharing columns; sharing drawer -> Shared -> picker finds "Ben tenant A"
   -> Apply -> row reads Shared, `viewer` tuple delivered (outbox 0 pending /
   0 dead), Ben's tile renders with data. The picker only found Ben after
   `scratch/seed_fga_extra.py` was fixed to write the tenant membership
   tuples for the three identity-seeded users (Ada, Ben, Cleo) -- the gap
   noted in the previous run and patched by hand then.

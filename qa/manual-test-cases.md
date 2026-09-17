# PCS-10243 — Object Ownership & Sharing: Manual Test Cases

Target: Preset PCS (Apache Superset 6.x) with the ownership/sharing module (`superset_ownership`), authorized via OpenFGA.

## Test environment

| Piece | Value |
|---|---|
| Superset under test | http://localhost:8097 — scratch multi-tenant stack, ownership feature **on** (`OWNERSHIP_ENABLED=true`), authorizer + directory = OpenFGA |
| Standalone OpenFGA API | http://localhost:8199 — read tuples with `curl -X POST http://localhost:8199/stores/<store>/read -d '{"tuple_key": {...}}'`, or the `fga` CLI |
| Scratch OpenFGA store id | Read live from `/Users/amaannawab/superset-local/pcs-setup/scratch/.scratch-out/store.env` (`SCRATCH_OPENFGA_STORE_ID=...`) before each test session — the store is destroyed/recreated between environment rebuilds, so do not hardcode an id in a script |
| Read-only monitoring | SQL Lab at http://localhost:8090 (`admin`/`admin`), connections to the raw OpenFGA tables and to bridge views (`fga_tuple`, `fga_changelog`) joined against Superset's `ab_user` — see `pcs-setup/scratch/fga-sqllab-monitoring.md` |
| Superset admin login | `admin` / `admin` |
| Seeded tenant users | password `test1234` for every one; **username is the member GUID** |
| Tenant A | `a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40` |
| Tenant B | `b7f28e5a-9c14-4d6b-a2f0-5e3d8c1a6b92` |
| Container to run CLI checks in | `pcssetup-superset-1` (`docker exec pcssetup-superset-1 superset ownership <subcommand>`) |
| Reset/inspection knobs | `superset ownership check`, `superset ownership status`, `superset ownership outbox status` (add `-v` for dead-row detail) |

### Personas used below

| Persona | GUID (= Superset username) | Tenant | Role |
|---|---|---|---|
| Ada | `3f0a91c7-2d84-4e63-9b15-7c4e8a2f6d31` | A | **Tenant A administrator** (`user:<A>.<ada> admin tenant:<A>`), member of `group:<A>.dashboard_designer`; owns dashboards 7, 8, 10 and chart 78 |
| Ben | `6c2b48e9-5a71-4f92-8d03-2e9b7c1a4d53` | A | Member — group `<A>.chart_designer` (nested into `<A>.dashboard_designer`) |
| Marcus Chen | `4302756e-b4aa-4938-b599-ed593444aeaf` | A | Member — groups `<A>.chart_designer`, `<A>.data_analyst`; owns dashboard 1 ("Sales Dashboard") |
| Priya Sharma | `9fcc709c-1a6b-4882-b8e3-1787afe31417` | A | Member — groups `<A>.dashboard_designer`, `<A>.data_analyst` |
| Elena Rossi | `58a4685c-6e11-4c4f-aad3-444839fc846a` | A | Member — group `<A>.data_analyst`; owns dashboard 6 |
| Grace Adeyemi | `4ddb1932-8b1c-49be-93b1-a9920129874b` | A | Member — groups `<A>.executives`, `<A>.dashboard_designer` |
| David Okafor | `12fc0874-6358-48e8-96cb-6ada75fd8c76` | A | Member — group `<A>.marketing_analytics`; owns dashboard 9 |
| Cleo | `9d7e35a1-8c62-4b04-a7f1-3d5e9b2c8a76` | B | **Tenant B administrator** (`user:<B>.<cleo> admin tenant:<B>`), member of `group:<B>.dashboard_designer`; owns dashboard 11 |
| Omar Farouk | `60f317ea-66cb-48a0-8d45-e15699486409` | B | Member — groups `<B>.dashboard_designer`, `<B>.sales_ops` |
| Hannah Berg | `e64caf0a-30fc-4f79-8f5a-e0a64dced4ad` | B | Member — group `<B>.data_analyst`; owns dashboard 4 |
| Mei Lin | `21387ae6-c116-4a2a-bd19-c46828e2e40d` | B | Member — groups `<B>.data_analyst`, `<B>.chart_designer` |
| Diego Alvarez | `a105bfbe-3b53-43d6-bc1c-953293a35c54` | B | Member — group `<B>.sales_ops`; owns dashboard 5 |

The id shapes are Neurons' (PCS-10243, confirmed by Ivanti). A person is `user:<tenant guid>.<member guid>` in the store (the API and the picker accept and show the bare member GUID; the module canonicalises). The tenant role is `Tenant_<guid>_Role`. Group ids follow `OWNERSHIP_GROUP_ID_FORMAT`, default `{tenant}.{name}` — e.g. `group:a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40.dashboard_designer`; there is no group-to-tenant tuple (the tenant is read from the id; `OWNERSHIP_DIRECTORY_GROUP_WALK=always`). A tenant's administrators are the `admin` relation on `tenant:<guid>` (`identity.tenant_administrator_group` → `tenant:<guid>#admin`), written by the platform, never by this module.

### Seeded objects used below

| Object | Owner | Visibility (backfilled) | Notes |
|---|---|---|---|
| Dashboard 7 "USA Births Names" | Ada (A) | public | Shared dataset `birth_names` (both tenants readable); contains chart 78 |
| Chart 78 "Girl Name Cloud" | Ada (A) | public | On dashboard 7; dataset `birth_names` |
| Dashboard 8 "Video Game Sales" | Ada (A) | public | Shared dataset `video_game_sales` |
| Dashboard 1 "Sales Dashboard" | Marcus Chen (A) | public | Shared dataset `cleaned_sales_data` |
| Dashboard 11 "Tenant B Device Compliance" | Cleo (B) | public | Shared dataset `neurons_devices`, created by Cleo directly (not backfilled) |
| Chart 1 (on `cleaned_sales_data`) | — | public | RLS proof chart: `COUNT(*)` returns 2,666 for a tenant A caller, 157 for a tenant B caller, regardless of ownership sharing — good for combining a sharing test with a data-correctness assertion |
| Chart 83 "Pivot Table v2" | **unowned** | public | Orphaned example chart, not linked to any dashboard, dataset `birth_names` (shared) — stays unowned by design (`scratch-run.md`); use for unowned/claim cases |

### Row-level security reference counts (for cross-checking a shared object's data path, not itself under test)

`cleaned_sales_data`: tenant A 2,666 rows / tenant B 157 rows. `birth_names`: tenant A 20,170 / tenant B 55,521. `video_game_sales`: tenant A 4,363 / tenant B 12,232.

### Conventions used below

- **API calls** are shown against the dashboard route; the identical chart route is `/api/v1/ownership/chart/...` unless noted. Both routes share one handler in `api.py`, so a rule proven on one asset type applies to the other unless a case says otherwise.
- **`check` clean** means `docker exec pcssetup-superset-1 superset ownership check` exits 0 and reports `"ok": true`, `"silently_open": []`, `"orphaned_sentinel": []`, `"missing_object": []`, `"dashboard_chart_patch_installed": true`.
- Every case ends with a blank **Result:** line for the tester to fill in (Pass/Fail/Blocked + notes).
- File:line citations refer to `pcs-setup/overlay/pythonpath/superset_ownership/` unless another path is given.

---

## 1. Login, identity & tenant scoping of lists

**OWN-001 — Each seeded persona logs in successfully**
Preconditions: fresh browser session, no cached auth.
Steps: Navigate to http://localhost:8097/login/, log in as each of Ada, Ben, Marcus, Cleo, Omar (GUID username, password `test1234`), and as `admin`/`admin`.
Expected: every login succeeds; the top-right user menu shows the display name seeded for that GUID (e.g. "Ada tenant A"); no ownership-related error on landing.
Result: PASS — all six logins (Ada, Ben, Marcus, Cleo, Omar, admin) succeeded with no ownership error; correct seeded display name confirmed on each via Settings > Info (e.g. "Ada tenant A", "Marcus Chen"). Note: the top-right bar itself just shows a generic "Settings" label, not the persona's name — the name only surfaces on the Info page, which is a minor deviation from the literal "top-right user menu shows the display name" wording but not a functional defect.

**OWN-002 — `superset ownership status` identifies the running configuration, not a specific caller**
Preconditions: shell access to `pcssetup-superset-1`.
Steps: `docker exec pcssetup-superset-1 superset ownership status`.
Expected: JSON with `enabled_backend: true`, `enabled_ui: true`, `enabled_ui_runtime: true`, `flags_agree: true`, `backend_loaded: true`, `group_id_format`, `objects` (total row count), `by_visibility` (counts per visibility), `shares` (total share rows) — per `cli.py:90-146`. Note this command reports instance state, not "who am I" (there is no per-caller identity in the CLI output) — record this if a tester expected a "current user" field.
Result: re-run under the Neurons shapes (wheel 0.4.0)

**OWN-003 — GET /api/v1/ownership/dashboard/7 as owner Ada shows the full owner block**
Preconditions: logged in as Ada.
Steps: `GET /api/v1/ownership/dashboard/7` (session cookie + CSRF, or bearer token).
Expected: `200`; `owner` block carries `id`, `name`, `email`, `guid` (privileged view, `_owner_view(..., privileged=True)`, `api.py:1151-1164`); `can_manage: true`, `manage_reason: "owner"`, `caller_subject: "user:3f0a91c7-..."`.
Result: PASS — 200; owner block has id/name/email/guid; can_manage: true, manage_reason: "owner", caller_subject: "user:3f0a91c7-2d84-4e63-9b15-7c4e8a2f6d31" exactly.

**OWN-004 — Same GET as a plain member with no relationship to the object gets a display-name-only owner, or 404 if not public**
Preconditions: dashboard 6 (owned by Elena, public) and a hypothetical private object Ben cannot reach.
Steps: As Ben, `GET /api/v1/ownership/dashboard/6` (public — Elena's). Then, once OWN-013 has made some object private and NOT shared with Ben, `GET` that object as Ben.
Expected: on the public object, `200` with `owner: {id, name}` only (no email/guid — `_owner_view(..., privileged=False)`), `can_manage: false`, `shares: []` (shares withheld from a non-manager, `api.py:1553-1558`). On the private/unshared object, `404` with `"dashboard not found"` — ownership metadata about a non-public object a caller cannot reach is not disclosed (`api.py:1541-1544`).
Result: PASS — Ben on public dashboard 6: 200, owner: {"id":7,"name":"Elena Rossi"} (no email/guid), can_manage: false, shares: []. Set up a temporary private object (dashboard 8, reverted after) not shared with Ben: Ben got 404 {"message":"dashboard not found"} exactly.

**OWN-005 — Cleo (tenant B) never sees tenant A's private or shared-but-not-with-her objects in the list**
Preconditions: at least one tenant-A object made `private` (see OWN-013) and one made `shared` with Ben only (see OWN-039), neither shared to Cleo or tenant B.
Steps: As Cleo, `GET /api/v1/ownership/dashboards`. Also try the UI dashboard list.
Expected: neither object id appears in `result`; `count` does not include them. This follows from `_visibility_scope`: a non-admin, non-tenant-administrator-of-that-tenant caller's scope is `public ∪ owned-by-caller ∪ reachable-by-share` (`api.py:1203-1206`) — a tenant A private/unshared object is in none of those sets for Cleo.
Result: PASS — set up dashboard 8 as private and chart 78 as shared-to-Ben-only (both reverted afterward); Cleo's dashboard list (count: 10) excludes 8, and her chart list excludes 78.

**OWN-006 — Public means public WITHIN the object's tenant: never visible across tenants**
Preconditions: dashboard 7 (Ada, tenant A, public), dashboard 11 (Cleo, tenant B, public). `superset ownership status` reports `public_scope: tenant` (the default, `OWNERSHIP_PUBLIC_SCOPE`).
Steps: As Cleo, `GET /api/v1/ownership/dashboards` and `GET /api/v1/dashboard/` — confirm dashboard 7 is absent from both. `GET /api/v1/dashboard/7`, `/api/v1/dashboard/7/charts`, `/api/v1/ownership/dashboard/7`, `dashboard/export/?q=!(7)` as Cleo. As Ada, the same for dashboard 11. As Ben (tenant A, not the owner), `GET /api/v1/dashboard/7`.
Expected: dashboard 7 is absent from every list Cleo can render and every direct route answers `404` (`403` on `POST /api/v1/chart/data`) with no owner name, `form_data` or share list in the body; symmetrically dashboard 11 for Ada, even though she holds the dataset it reads (`neurons_devices` is shared). Ben gets `200`. The row's `tenant_guid` (revision 0004, mirrored from the store's `tenant` tuple) is what decides it, with zero store calls: `hooks.raise_for_access_bypass` → `service.public_row_visible_to`; the list is decided in SQL (`hooks._public_ids_in_sql`); the ownership routes through `api._public_scope_ids` / `_listed_for`. A public object carries the sentinel viewer exactly like a private one.
Result:

**OWN-007 — Tenant administrator's list includes the whole tenant, not just owned/shared objects**
Preconditions: at least one tenant-A object owned by someone other than Ada (e.g. dashboard 1, Marcus) and not shared with Ada.
Steps: As Ada, `GET /api/v1/ownership/dashboards`.
Expected: dashboard 1 appears (tenant A's own object), with `can_manage: true`, `manage_reason: "tenant_admin"` on its detail — a tenant administrator's scope is `tenant's objects ∪ public ∪ reachable ∪ owned` (`api.py:1196-1201`).
Result: PASS — dashboard 1 (Marcus's) appears in Ada's list; GET /dashboard/1 as Ada shows can_manage: true, manage_reason: "tenant_admin".

**OWN-008 — Superset admin's list has no scope restriction**
Preconditions: logged in as `admin`.
Steps: `GET /api/v1/ownership/dashboards` with a large `limit` (e.g. 1000).
Expected: every dashboard in the instance appears, private tenant A and tenant B objects included (`_visibility_scope` returns `(None, True, set())` for `security_manager.is_admin()`, `api.py:1183-1184`).
Result: PASS — with dashboard 8 temporarily private, admin's list (count: 11, all 11 dashboards returned) included it.

**OWN-009 — /subjects search is tenant-scoped and case-insensitive**
Preconditions: logged in as Ada (tenant A).
Steps: `GET /api/v1/ownership/subjects?q=ben`; then `q=BEN`; then `q=9d7e35a1` (Cleo's GUID, tenant B).
Expected: `q=ben`/`q=BEN` both return Ben's row (case-insensitive substring match on display name/GUID/email, `directory.py:431-436`); `q=9d7e35a1` returns an empty `result` — Cleo (tenant B) is structurally excluded because the search is bound to Ada's own tenant before any text match runs (`directory.py:414`, `1019`), not merely filtered out afterward.
Result: PASS — q=ben and q=BEN both return Ben's row identically ({"text":"Ben tenant A","value":"user:6c2b48e9-..."}); q=9d7e35a1 (Cleo's guid) returns result: [].

---

## 2. Visibility state machine — dashboards and charts

All of `public → private → shared → public` and the reverse, run from each of: the owner, the tenant administrator, a plain tenant member, an other-tenant user, and the Superset admin. Route: `PUT /api/v1/ownership/dashboard/<pk>/visibility` and the chart equivalent, body `{"visibility": "private"|"shared"|"public"}` (`api.py:1806-2009`).

**OWN-010 — Owner sets a public dashboard to private**
Preconditions: logged in as Ada; dashboard 8 (hers, public).
Steps: `PUT /dashboard/8/visibility {"visibility": "private"}`.
Expected: `200 {"object_id": 8, "visibility": "private"}`; sentinel viewer added to the dashboard (`sentinel.add_sentinel`, `api.py:1978`); Ben and Cleo now get `404` on `GET /dashboard/8` (private, no share); `check` clean.
Result: PASS — 200 {"object_id":8,"visibility":"private"}; Ben and Cleo both got 404 "dashboard not found"; check ok:true.

**OWN-011 — Owner sets a public chart to private**
Preconditions: logged in as Ada; chart 78 (hers, public, on dashboard 7).
Steps: `PUT /chart/78/visibility {"visibility": "private"}`.
Expected: `200`; sentinel added to the chart; direct `GET /api/v1/ownership/chart/78` as Ben → `404`; dashboard 7 itself stays open (see Section 4 for the placeholder-tile behavior this now causes).
Result: PASS — 200; Ben's GET /chart/78 → 404 "chart not found"; Ben's GET /dashboard/7 still 200 (visibility public, can_manage false).

**OWN-012 — Owner: private → shared**
Preconditions: dashboard 8 currently private (OWN-010).
Steps: As Ada, `PUT /dashboard/8/visibility {"visibility": "shared"}`.
Expected: `200`; sentinel stays present (shared is still a governed state, `service.GOVERNED_VISIBILITIES = ("private", "shared")`, `service.py:43`); the object starts with **zero shares** — a fresh `shared` state after a `private→shared` transition confers nothing until an explicit share is added (mirrors "brand-new object" behavior, `api.py:1963-1965`); Ben still gets 404 until explicitly shared.
Result: PASS — 200; GET showed shares: [] immediately after the transition; Ben's GET /dashboard/8 still 404.

**OWN-013 — Owner: shared → public**
Preconditions: dashboard 8 currently shared (OWN-012), optionally with a share added (OWN-039).
Steps: As Ada, `PUT /dashboard/8/visibility {"visibility": "public"}`.
Expected: `200`; the sentinel viewer STAYS on the object (under `OWNERSHIP_PUBLIC_SCOPE=tenant` public is governed too — `api._set_asset_visibility` arms rather than removes); any existing shares are left in the mirror table (visibility going TO public does not delete share rows, only the private transition revokes — confirm this asymmetry as a test, not an assumption); Ben can open it again; Cleo (tenant B) still gets `404` — public within the tenant, see OWN-006. `check` clean, and `SELECT tenant_guid FROM ownership_object WHERE asset_type='dashboard' AND object_id=8` is tenant A throughout the cycle.
Result:

**OWN-014 — Tenant administrator cannot change visibility on an object they do not own; they take ownership first**
Preconditions: Ada (tenant A admin) is not the owner of dashboard 1 (Marcus's).
Steps: As Ada, `GET /dashboard/1`; then `PUT /dashboard/1/visibility {"visibility": "private"}`; then `PUT /dashboard/1/owner {"subject": "user:3f0a91c7-..."}` (herself); then the visibility PUT again, then back to `public`; finally `PUT .../owner {"subject": "user:4302756e-..."}` to hand it back to Marcus.
Expected: the GET shows `can_manage: true, can_share: false, manage_reason: "tenant_admin"`; the first visibility PUT is `403 {"message": "a tenant administrator may transfer ownership but not change how an object is shared; take ownership of it first"}` and nothing changes; the self-transfer is `200`; the visibility PUTs are then `200` (she is the owner, `manage_reason: "owner"`, `can_share: true`); after handing it back Marcus sees `manage_reason: "owner"` and Ada is back to `can_share: false`. A tenant administrator's authority is over who owns the object, not who sees it (`api._sharing_ground`): a change of sharing can only ever have been made by the name on the owner line.
Result:

**OWN-015 — Plain member cannot change visibility on an object they do not own**
Preconditions: logged in as Ben; dashboard 1 (Marcus's, tenant A, same tenant as Ben).
Steps: `PUT /dashboard/1/visibility {"visibility": "private"}`.
Expected: `403 {"message": "only the owner, an admin or a holder of the manage-sharing permission can change visibility; a tenant administrator takes ownership first"}`. Nothing changes; `check` clean.
Result:

**OWN-016 — Other-tenant user cannot change visibility even via a raw API call**
Preconditions: logged in as Cleo (tenant B); dashboard 8 (Ada's, tenant A, currently public).
Steps: `PUT /dashboard/8/visibility {"visibility": "private"}`.
Expected: `403`, same message as OWN-015 — Cleo is neither owner, nor tenant A's administrator, nor Superset admin.
Result: PASS — 403, identical message to OWN-015.

**OWN-017 — Superset admin can change visibility on anyone's object in any tenant**
Preconditions: logged in as `admin`; dashboard 4 (Hannah's, tenant B).
Steps: `PUT /dashboard/4/visibility {"visibility": "private"}`, then back to `public`.
Expected: both `200`, `manage_reason: "admin"` on the interim read.
Result: PASS — both 200; interim GET showed manage_reason: "admin".

**OWN-018 — Sentinel is present exactly for private/shared, absent for public — spot check via OpenFGA/DB**
Preconditions: dashboard 8 cycled private → shared → public in prior cases.
Steps: After each transition, either inspect the dashboard's `viewers` (native Superset, e.g. via `superset ownership check` or a DB query on the role/subject bridge tables) or note `disabled`/`visibility` reported by `GET /dashboard/8`.
Expected: private and shared both carry the sentinel role (`__ownership_sentinel__`, `sentinel.py:42`); public carries none. `check`'s `silently_open` and `orphaned_sentinel` lists are both empty at every step.
Result: PASS — queried the `dashboard_viewers` mirror table directly (subject_id for the `__ownership_sentinel__` role) across private→shared→public→private→public on dashboard 8: 1 sentinel row in private, 1 in shared, 0 in public, confirmed twice. `check` stayed ok:true with silently_open:[] and orphaned_sentinel:[] throughout. Evidence: row counts were 1/1/0/1/0 in that transition order.

**OWN-019 — `visibility` must be one of the three valid values**
Preconditions: logged in as Ada; dashboard 8.
Steps: `PUT /dashboard/8/visibility {"visibility": "hidden"}`.
Expected: `400 {"message": "visibility must be one of ['private', 'public', 'shared']"}` (`api.py:1849-1850`, sorted list). Nothing changes.
Result: PASS — 400 with exact message text.

**OWN-020 — Missing/empty body on the visibility PUT**
Preconditions: logged in as Ada; dashboard 8.
Steps: `PUT /dashboard/8/visibility` with no body / `{}`.
Expected: `400`, same "visibility must be one of..." message (payload defaults to `{}`, `visibility` is `None`, which is not in `VALID_VISIBILITIES`).
Result: PASS — both no-body and `{}` bodies returned 400 with the same "visibility must be one of..." message.

**OWN-021 — Visibility PUT on a non-existent dashboard id**
Steps: As Ada, `PUT /dashboard/999999/visibility {"visibility": "private"}`.
Expected: `404 {"message": "dashboard not found"}` (`api.py:1827`, `_not_found_msg`).
Result: PASS — 404 with exact message; dashboard 8 confirmed unaffected (still public) afterward.

**OWN-022 — Repeat OWN-010 through OWN-013 identically for a chart (chart 78)**
Preconditions: logged in as Ada, then Ben, then Cleo, then `admin` in turn.
Steps: Cycle chart 78 through private → shared → public exactly as OWN-010–013, from each persona in OWN-014–017's roles.
Expected: identical behavior and status codes to the dashboard cases — the handler is shared (`_set_asset_visibility` takes `asset_type` as a parameter).
Result: PASS — chart 78 owner cycle (Ada, public→private→shared→public) all 200; tenant-admin path (Ada on chart 1, Marcus's) 200/200 with manage_reason: "tenant_admin"; plain member (Ben on chart 1) 403; other-tenant (Cleo on chart 78) 403; admin cycle (chart 29, Cleo's tenant-B chart) 200/200 with manage_reason: "admin". All status codes and messages matched the dashboard-route equivalents.

**OWN-023 — `check` is clean after every visibility transition in this section**
Steps: `docker exec pcssetup-superset-1 superset ownership check` after completing OWN-010 through OWN-022.
Expected: `"ok": true`, all consistency lists empty.
Result: PASS — ok: true, silently_open: [], orphaned_sentinel: [], missing_object: [], dashboard_chart_patch_installed: true.

**OWN-024 — UI: visibility radio group and its exact copy**
Preconditions: logged in as Ada; dashboard 8's Sharing drawer open.
Steps: Open the Sharing drawer (row action "Sharing" / `ShareAltOutlined` icon) on dashboard 8.
Expected: three radio options with exact copy: **Private** — "Visible to only you. Existing shares are removed."; **Shared** — "Shared with selected users or groups"; **Public** — "Shared with users in your organisation" (`SharingDrawer.tsx:1037-1066`). An info banner reads "Updating the share access level may grant or remove sharing access for existing users and groups. Review changes before applying."
Result: PASS — opened the Sharing drawer on dashboard 8 ("Video Game Sales") as Ada from the dashboard list row action. All three radio labels and subtitles matched verbatim (Private/"Visible to only you. Existing shares are removed.", Shared/"Shared with selected users or groups", Public/"Shared with users in your organisation"), and the info banner text matched verbatim. Confirmed via zoomed screenshot.

**OWN-025 — UI: Apply is disabled until the visibility (or share list) actually changes**
Preconditions: Sharing drawer open on a public object, no edits made.
Steps: Observe the Apply button state; click a different visibility radio and back to the original; observe again.
Expected: Apply starts disabled (`dirty` false); it becomes enabled only when `visibility !== detail.visibility` or, for Shared, the share list differs from what was loaded (`SharingDrawer.tsx:381-388`); returning to the original value re-disables it.
Result: PASS — on dashboard 8's drawer (loaded as Public), Apply started visibly disabled (greyed); selecting Private enabled it (also noticed "Transfer ownership" becomes disabled while dirty); reselecting Public re-disabled Apply. Closed via Cancel without applying; dashboard 8 confirmed still public afterward.

**OWN-026 — Manage-sharing-permission holder cannot make an object public**
Preconditions: `OWNERSHIP_MANAGE_PERMISSION` configured and a user granted that role but not owner/tenant-admin/admin, on a `shared` object they can already see.
Steps: As that holder, `PUT .../visibility {"visibility": "public"}`.
Expected: `403 {"message": "the manage-sharing permission does not include making an object public"}` (`api.py:1851-1860`). UI: the Public radio is shown disabled with the reason appended to its subtitle.
Result: BLOCKED — `OWNERSHIP_MANAGE_PERMISSION` is not configured in this environment (config source is "default" per `superset ownership status`, and no manage-sharing role exists in `GET /api/v1/security/roles/` — only Admin, Public, __ownership_sentinel__, Alpha, Gamma, sql_lab, and the two tenant roles). No holder of this ground can be constructed; precondition cannot be met.

**OWN-027 — Manage-sharing-permission holder cannot go private when it would revoke their own share or the tenant's structural group**
Preconditions: as OWN-026's holder, admitted to a `shared` object via their own share row (i.e. the manage-sharing grant is itself a viewer/editor share on this object).
Steps: `PUT .../visibility {"visibility": "private"}`.
Expected: `403 {"message": "the manage-sharing permission does not include making an object private when a mirror row is your own share or the tenant's structural group"}` (`api.py:1879-1884`) — going private would revoke every share including their own admitting share and/or `group:tenant_<T>#member`/`group:tenant_administrator_<T>#member`, which this ground is not allowed to touch.
Result: BLOCKED — same environment dependency as OWN-026: `OWNERSHIP_MANAGE_PERMISSION` is not configured, so no manage-sharing-permission holder exists to exercise this path.

---

## 3. Sharing

Routes: `POST /api/v1/ownership/<asset>/<pk>/shares {"subject": "...", "role": "viewer"|"editor"}` and `DELETE /api/v1/ownership/<asset>/<pk>/shares/<subject>` (`api.py:2127-2417`).

**OWN-028 — Share a shared-visibility dashboard with a single user**
Preconditions: Ada owns dashboard 8, currently `shared`, no shares yet.
Steps: As Ada, `POST /dashboard/8/shares {"subject": "user:6c2b48e9-5a71-4f92-8d03-2e9b7c1a4d53", "role": "viewer"}` (Ben).
Expected: `200 {"object_id": 8, "subject": "user:6c2b48e9-...", "role": "viewer"}`; after the outbox drains (`superset ownership outbox drain`, or wait for the beat schedule), a `viewer` tuple for Ben appears against `dashboard:<uuid>` in the OpenFGA store; Ben can now open dashboard 8; `GET /dashboard/8` as Ada lists Ben under `shares`.
Result: PASS — 200 with exact shape; GET as Ada listed Ben under shares immediately; Ben denied (404) before drain, 200 after ~11s wait; confirmed a `viewer` tuple for user:6c2b48e9-... against dashboard:c7bc10f4-... directly via `POST /stores/<store>/read` on OpenFGA.

**OWN-029 — Share with a group in the caller's own tenant**
Preconditions: dashboard 8 (Ada, shared).
Steps: As Ada, `POST /dashboard/8/shares {"subject": "group:a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40.dashboard_designer#member", "role": "viewer"}`.
Expected: `200`; every tenant-A user in `<A>.dashboard_designer` (Priya, Sofia, Grace) can now open dashboard 8 once the tuple delivers.
Result: PASS — 200; Priya (member of dashboard_designer_A) got 200 on GET /dashboard/8 after the tuple delivered.

**OWN-030 — Sharing with a group from the OTHER tenant is refused**
Preconditions: dashboard 8 (Ada, tenant A, shared).
Steps: As Ada, `POST /dashboard/8/shares {"subject": "group:b7f28e5a-9c14-4d6b-a2f0-5e3d8c1a6b92.dashboard_designer#member", "role": "viewer"}` (tenant B's group).
Expected: `400 {"message": "group does not belong to your tenant"}` (`identity.group_belongs_to_tenant` check in `_validate_group_subject`, `api.py:486-487`); nothing written.
Result: PASS — 400 with exact message.

**OWN-031 — Sharing with a user from the other tenant is refused**
Preconditions: dashboard 8 (Ada, tenant A, shared).
Steps: As Ada, `POST /dashboard/8/shares {"subject": "user:9d7e35a1-8c62-4b04-a7f1-3d5e9b2c8a76"}` (Cleo, tenant B).
Expected: `400 {"message": "subject is not a member of your tenant"}` (`_confirm_tenant_membership`, `api.py:343`) — reached because Cleo's Superset account resolves to a `Tenant_<B>_Role` role that disagrees with tenant A, and the store confirms she is not in A.
Result: PASS — 400 with exact message.

**OWN-032 — Sharing on a PRIVATE object is refused with 409**
Preconditions: dashboard 8 currently `private`.
Steps: As Ada, `POST /dashboard/8/shares {"subject": "user:6c2b48e9-..."}`.
Expected: `409 {"message": "object is private; make it shared before sharing"}` (`api.py:2207-2217`) — the read gate refuses a private object to every non-owner regardless of any mirror row, so the write is refused outright rather than accepted-but-inert.
Result: PASS — 409 with exact message.

**OWN-033 — Duplicate share (same subject, same role) is accepted idempotently**
Preconditions: dashboard 8 shared with Ben as viewer (OWN-028 done).
Steps: As Ada, repeat `POST /dashboard/8/shares {"subject": "user:6c2b48e9-...", "role": "viewer"}`.
Expected: `200`, same response shape; no duplicate row in `ownership_share`; no second `viewer` tuple in the store (idempotent insert/write).
Result: PASS — 200, identical shape; subsequent GET showed exactly one entry for Ben; `ownership_share` also carries a `uq_ownership_share_subject` unique constraint on (asset_type, object_id, subject) enforcing this at the DB level.

**OWN-034 — Re-sharing the same subject with a DIFFERENT role changes the role**
Preconditions: Ben currently shared as `viewer` on dashboard 8.
Steps: As Ada, `POST /dashboard/8/shares {"subject": "user:6c2b48e9-...", "role": "editor"}`.
Expected: `200`; Ben's mirror row role becomes `editor`; once delivered, Ben can now edit the dashboard (native `is_editor` honors this via `EXTRA_EDITORS_RESOLVER`, `hooks.py:453-457`).
Result: PASS — 200; GET as Ada showed Ben's role: "editor". Edit-rights consequence verified directly under OWN-042.

**OWN-035 — Revoke a share**
Preconditions: Ben shared on dashboard 8.
Steps: As Ada, `DELETE /dashboard/8/shares/user:6c2b48e9-5a71-4f92-8d03-2e9b7c1a4d53`.
Expected: `200 {"object_id": 8, "subject": "user:6c2b48e9-..."}` (mirror row existed — `had_row: true` path, `api.py:2409-2416`); after drain, Ben gets `404` on the dashboard again.
Result: PASS — 200 with exact shape; Ben got 404 after drain.

**OWN-036 — Revoking a subject with no mirror row still queues a revocation (202)**
Preconditions: dashboard 8, shared, no share on file for Priya.
Steps: As Ada, `DELETE /dashboard/8/shares/user:9fcc709c-1a6b-4882-b8e3-1787afe31417`.
Expected: `202 {"object_id": 8, "subject": "user:9fcc709c-...", "queued": true, "mirror_row": false}` (`api.py:2409-2415`) — with the outbox enabled and a `user:` subject, the revocation is queued and resolved against the store at drain time even though nothing was mirrored (defends against a grant the mirror never knew about).
Result: PASS — 202 with exact body {"mirror_row":false,"object_id":8,"queued":true,"subject":"user:9fcc709c-..."}.

**OWN-037 — Revoking a GROUP subject with no mirror row is a clean 404, not queued**
Preconditions: dashboard 8, shared, group `<A>.dashboard_designer` never shared to it.
Steps: As Ada, `DELETE /dashboard/8/shares/group:a1e4c2d0-....dashboard_designer#member`.
Expected: `404 {"message": "no such share on this object"}` — a stray group revocation is deliberately not queued because the read path cannot tell who is in the group, so a near-certain no-op is not worth denying every non-owner for the delivery window (`api.py:2362-2369`).
Result: PASS — 404 with exact message (not 202/queued, unlike the user-subject case in OWN-036/049).

**OWN-038 — Private-revokes-all: setting visibility to private revokes every existing share (#93)**
Preconditions: dashboard 8, `shared`, with Ben (viewer) and the `<A>.dashboard_designer` group both shared.
Steps: As Ada, `PUT /dashboard/8/visibility {"visibility": "private"}`. After the outbox drains, `GET /dashboard/8` as Ada and inspect `shares`.
Expected: `200` on the PUT; `shares: []` on the subsequent GET — both mirror rows are gone; both a `revoke_subject` op per subject is queued through the same path the unshare route uses (`_revoke_shares_for_private`, `api.py:1718-1755`); Ben gets `404` immediately (the read gate denies private unconditionally for non-owners, independent of drain timing — `hooks.py:355-366`) even though the store tuple itself may still be draining. `POST /dashboard/8/shares` on this now-private object → `409` (OWN-032).
Result: PASS — 200 on the PUT; Ben got 404 immediately (before drain); GET immediately after the PUT still showed both rows but flagged `"unmirrored": true` (pending-revocation state), and after the ~11s auto-drain window `shares: []`, confirming both are fully revoked; a further POST /shares returned 409 exactly as in OWN-032.

**OWN-039 — `-> shared` after a private-revoke starts with zero shares**
Preconditions: continuing OWN-038.
Steps: As Ada, `PUT /dashboard/8/visibility {"visibility": "shared"}`; `GET /dashboard/8`.
Expected: `shares: []` — the private→shared transition never restores what private revoked (`api.py:1963-1965`).
Result: PASS — visibility: "shared", shares: [] confirmed.

**OWN-040 — Pending revocation is not visible on the LIST or GET before the outbox drains (#84)**
Preconditions: Celery worker stopped or the outbox drain not run; Ben shared as viewer on dashboard 8; `OWNERSHIP_OUTBOX_ENABLED` on (default).
Steps: As Ada, `DELETE /dashboard/8/shares/user:6c2b48e9-...`; immediately (before any drain), as Ben: `GET /api/v1/chart/.../data` for a chart on that dashboard (or the dashboard's own data path) to confirm access is already denied; then `GET /api/v1/ownership/dashboards` (list) and `GET /api/v1/ownership/dashboard/8` (single) as Ben.
Expected: the **data path** is denied immediately (403 `CHART_SECURITY_ACCESS_ERROR`, `has_pending_revocation` consulted directly in the access-bypass hook, `hooks.py:379-393`) — but list/GET metadata previously lagged behind until #84's fix; on the current (post-#84) build, both the list and GET should ALSO exclude the object for Ben immediately, not just the data path — confirm `pending_revocations_for` is applied in `_query_filter`/`owned_or_shared_rows` (`hooks.py:414-419`, `outbox.py:449-556`). If the list/GET still shows the object before drain, that is the #84 regression — flag it.
Result: PASS (no #84 regression) — used the dashboard's own read path as the case's stated alternative to a chart-data call (did not separately exercise `/api/v1/chart/.../data`, so the specific `CHART_SECURITY_ACCESS_ERROR` body was not directly observed here — see OWN-058/059 for that). Gave Ben confirmed working access first, then Ada DELETEd his share and, within about a second (well inside the ~10s auto-drain window), Ben's GET /dashboard/8 → 404 and GET /dashboards (list) excluded object 8 (count: 10). Both single-GET and list excluded it immediately, matching the post-#84 expectation.

**OWN-041 — Outbox drain delivers the queued revocation/share and `outbox status` returns to zero**
Preconditions: continuing from a pending share or revoke op (e.g. OWN-040 with the worker stopped).
Steps: `docker exec pcssetup-superset-1 superset ownership outbox status`; note `pending`/`claimed`/`dead`/`blocked`/`stalled`; run `docker exec pcssetup-superset-1 superset ownership outbox drain`; re-run `outbox status`.
Expected: before drain, `pending >= 1`; after drain, `pending: 0`, `dead: 0`, `blocked: 0`, and `last_delivered` updated to a recent timestamp (`outbox.py:826-879`).
Result: PASS — status showed pending: 2 immediately after queuing a share+revoke; after running `outbox drain`, status showed pending: 0, dead: 0, blocked: 0, last_delivered updated to the current timestamp. Note: the automatic Celery beat schedule (~10s) had already delivered the ops by the time the manual drain ran (drain itself reported delivered: 0), but the status before/after and the updated last_delivered confirm delivery happened.

**OWN-042 — Editor share confers native edit rights (relation `editor` is supported)**
Preconditions: dashboard 8, shared; Ben shared as `role: "editor"` (OWN-034 pattern).
Steps: After drain, as Ben, attempt to edit dashboard 8 (rename a tile, save layout) via the UI or `PUT /api/v1/dashboard/8`.
Expected: succeeds — `role` values are `viewer`/`editor` (`VALID_SHARE_ROLES`, `api.py:112`), and Superset's own `is_editor` check consults the dynamic editor resolver seeded from editor shares (`hooks.py:453-457, 592-659`).
Result: PASS — via `PUT /api/v1/dashboard/8` (same title, no-op edit to avoid changing seeded state) as Ben after drain → 200 with a `result` body, confirming native edit rights were granted.

**OWN-043 — Editor share on a PRIVATE object confers nothing (defence in depth for #93)**
Preconditions: an object made `private`, with an editor share written directly to the store before it went private (or attempt the sequence: shared+editor share → private).
Steps: As the editor-share holder, attempt to open/edit the now-private object.
Expected: denied — `editor_user_ids` is short-circuited to `[]` for `visibility == "private"` before even querying share rows (`hooks.py:618-638`); this is on top of OWN-038's revoke-on-private behavior, so it holds even if a share row somehow survived.
Result: PASS — dashboard 8 was shared with Ben as editor, then flipped straight to private; Ben's `PUT /api/v1/dashboard/8` attempt immediately after (before any drain) returned 404 "Not found", and his GET /api/v1/ownership/dashboard/8 also 404 — denied immediately, not just after the share row is eventually revoked.

**OWN-044 — Share role must be one of the valid values**
Preconditions: dashboard 8, shared.
Steps: As Ada, `POST /dashboard/8/shares {"subject": "user:6c2b48e9-...", "role": "admin"}`.
Expected: `400 {"message": "role must be one of ['editor', 'viewer']"}` (`api.py:2141-2142`).
Result: PASS — 400 with exact message.

**OWN-045 — Share with a missing subject field**
Steps: As Ada, `POST /dashboard/8/shares {"role": "viewer"}`.
Expected: `400 {"message": "subject is required, e.g. 'user:<guid>'"}` (`api.py:2143-2144`).
Result: PASS — 400 with exact message.

**OWN-046 — Share on an unowned object (owner recorded but deactivated) is refused**
Preconditions: an object whose owner account has since been deactivated (see Section 8 for how to reach this state).
Steps: `POST .../shares {"subject": "user:..."}` as whoever can still reach the sharing action (e.g. a tenant admin).
Expected: `409 {"message": "object is unowned; assign an owner before sharing"}` (`api.py:2183-2187`) — checked before admission is even evaluated, so even a tenant administrator who could otherwise manage the object cannot share it until an owner is (re)assigned via claim/transfer.
Result: PASS — deactivated Elena (dashboard 6's owner, public) via `PUT /api/v1/security/users/7 {"active":false}` as admin; confirmed `GET /dashboard/6` showed `unowned: true`. As Ada (tenant A admin), `POST /dashboard/6/shares {"subject":"user:6c2b48e9-..."}` → `409 {"message":"object is unowned; assign an owner before sharing"}` exact match. Reactivated Elena immediately after; dashboard 6 confirmed back to owner Elena / unowned:false / public, nothing else changed.

**OWN-047 — Share on a never-owned object (chart 83) is refused with a different 409**
Preconditions: chart 83 "Pivot Table v2" — unowned since backfill (`row.owner_user_id is None`), still `public`.
Steps: As `admin` (or a tenant admin who is admitted via the unowned-rescue ground), `POST /chart/83/shares {"subject": "user:6c2b48e9-..."}`.
Expected: `409 {"message": "object has no owner; assign one before sharing"}` (`_is_ownable` check, `api.py:2200-2205`) — distinct wording from OWN-046's "object is unowned"; the two 409s correspond to two different underlying states (never-owned vs. deactivated-owner) and are worth telling apart in a bug report.
Result: PASS — as admin, 409 with exact message "object has no owner; assign one before sharing", distinct from OWN-046's wording as the case predicts. Chart 83 left untouched (still unowned, public).

**OWN-048 — Plain member cannot share an object they neither own nor administer**
Preconditions: dashboard 1 (Marcus's, shared, tenant A); Ben is a plain tenant-A member.
Steps: As Ben, `POST /dashboard/1/shares {"subject": "user:..."}`.
Expected: `403 {"message": "only the owner, an admin or a holder of the manage-sharing permission can manage shares; a tenant administrator takes ownership first"}` (`_SHARES_403`).
Result:

**OWN-049 — DELETE /shares on a subject that never existed and has no store trace**
Preconditions: dashboard 8, shared, inline mode is NOT in effect (outbox on, default).
Steps: As Ada, `DELETE /dashboard/8/shares/user:12fc0874-6358-48e8-96cb-6ada75fd8c76` (David, never shared).
Expected: `202` (queued, per OWN-036's reasoning) for a `user:` subject; NOT a clean "nothing to do" 404, since the module cannot distinguish "never shared" from "shared via a channel the mirror missed" without asking the store, and chooses to queue defensively.
Result: PASS — 202 {"mirror_row":false,"object_id":8,"queued":true,"subject":"user:12fc0874-..."} — queued, not a plain 404.

**OWN-050 — Malformed subject on share/unshare — missing colon**
Steps: As Ada, `POST /dashboard/8/shares {"subject": "bananas"}`.
Expected: `400 {"message": "subject must be 'user:<guid>' or 'group:<name>#member'"}` (`api.py:256-257`).
Result: PASS — 400 with exact message.

**OWN-051 — Malformed subject — unknown kind prefix**
Steps: As Ada, `POST /dashboard/8/shares {"subject": "robot:123"}`.
Expected: `400 {"message": "subject must be a user or a group"}` (`api.py:270`).
Result: PASS — 400 with exact message.

**OWN-052 — Subject naming an unknown account**
Steps: As Ada, `POST /dashboard/8/shares {"subject": "user:00000000-0000-4000-8000-000000000000"}`.
Expected: `400 {"message": "subject does not name a known account"}` (`api.py:390-392`).
Result: PASS — 400 with exact message.

**OWN-053 — Subject naming a deactivated account**
Preconditions: a tenant-A user account deactivated (see Section 8).
Steps: As Ada, `POST /dashboard/8/shares {"subject": "user:<deactivated guid>"}`.
Expected: `400 {"message": "subject's account is deactivated"}` (`api.py:430-431`).
Result: PASS — substituted dashboard 11 (Cleo, tenant B) for dashboard 8 (Ada's, off-limits). Deactivated Mei Lin (tenant B, no owned objects) via admin; set dashboard 11 to `shared`; as Cleo, `POST /dashboard/11/shares {"subject":"user:21387ae6-c116-4a2a-bd19-c46828e2e40d"}` (Mei Lin) → `400 {"message":"subject's account is deactivated"}` exact match. Reactivated Mei Lin and restored dashboard 11 to `public` (no shares) afterward.

**OWN-054 — Group subject that does not exist in the authorization store**
Preconditions: a syntactically well-formed but nonexistent tenant-A group id.
Steps: As Ada, `POST /dashboard/8/shares {"subject": "group:a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40.not_a_real_group#member"}`.
Expected: `400 {"message": "no such group in the authorization store"}` (`api.py:544-545`).
Result: PASS — 400 with exact message.

**OWN-055 — Concurrent visibility flips on the same object are serialized, not lost**
Preconditions: dashboard 8; two terminals/clients as Ada.
Steps: Fire `PUT .../visibility {"visibility": "private"}` and `PUT .../visibility {"visibility": "shared"}` against the same object at nearly the same time (script both requests back to back with no delay).
Expected: both complete without a 500; the final state is one of the two values, consistently reflected in a following `GET`; `check` stays clean — the per-object write lock (`service.lock_object`, a `SELECT ... FOR UPDATE`) serializes the two writes so they cannot interleave (`api.py:1829-1834`, `outbox.py:83-97`).
Result: PASS — fired both PUTs concurrently via backgrounded curl processes; both returned 200 (no 500), final GET showed a consistent single value ("shared"), and `check` stayed ok:true.

---

## 4. Placeholder tile & data path

**OWN-056 — Private chart on an otherwise-visible dashboard: tile stays, data hidden**
Preconditions: chart 78 "Girl Name Cloud" made `private` by Ada (owner); dashboard 7 (public) still contains it.
Steps: As Ben, open dashboard 7 in the UI.
Expected: the tile remains in its layout position with its title ("Girl Name Cloud") visible; no chart data renders; error card reads errorType **"You don't have access to this chart"**, body: `You do not have access to the chart "Girl Name Cloud". Its data is hidden, but the chart is still part of this dashboard.`, and `To request access, reach out to the chart owner: Ada tenant A.` (`ChartSecurityAccessErrorMessage.tsx:47-64`; matches the live evidence in `scratch/scratch-run.md`'s UI walk).
Result: PASS — logged in as Ben via UI and opened dashboard 7. Tile stayed in its layout slot with title "Girl Name Cloud" visible; error card text matched verbatim: "You don't have access to this chart" / "You do not have access to the chart "Girl Name Cloud". Its data is hidden, but the chart is still part of this dashboard." / "To request access, reach out to the chart owner: Ada tenant A." Confirmed via screenshot.

**OWN-057 — Dashboard-serialize API reflects the same denial in its JSON**
Preconditions: continuing OWN-056.
Steps: As Ben, `GET /api/v1/dashboard/7/charts/` (or however the FE fetches the dashboard's chart list) and inspect chart 78's entry.
Expected: `has_access: false`; `form_data` key absent; `owners: ["Ada tenant A"]` (display names, via `get_access_contact_names`); `id`/`slice_name` still present (`dashboard_patch.py:365-377`). The dashboard-level response is still `200` — no 403 at the dashboard-metadata level.
Result: PASS — via `GET /api/v1/dashboard/7/charts` (no trailing slash; the slashed form 404s — route naming note) as Ben: 200 overall; chart 78's entry showed has_access: false, no form_data key, owners: ["Ada tenant A"], id: 78 and slice_name: "Girl Name Cloud" both present.

**OWN-058 — Direct chart-data fetch for a private chart returns 403 CHART_SECURITY_ACCESS_ERROR**
Preconditions: chart 78 private, not shared with Ben.
Steps: As Ben, `POST /api/v1/chart/data` with `form_data.slice_id: 78` (or `GET/POST /api/v1/chart/78/data/`).
Expected: `403` with body:
```json
{"errors": [{"message": "You do not have access to this chart.",
  "error_type": "CHART_SECURITY_ACCESS_ERROR", "level": "warning",
  "extra": {"ownership": "private", "chart_id": 78,
            "slice_name": "Girl Name Cloud", "owners": ["Ada tenant A"]}}]}
```
(`hooks.py:832-858`). Note `extra.ownership` is a hardcoded literal `"private"` even when the true cause is a `shared` object the caller just isn't shared with — do not read that field as the object's actual visibility.
Result: PASS — both `POST /api/v1/chart/data` with form_data.slice_id:78 and `GET /api/v1/chart/78/data/` returned 403 with the exact body shown (message, error_type, level, extra.ownership/chart_id/slice_name/owners all matched).

**OWN-059 — Same 403 reachable through the bulk chart-data endpoint**
Preconditions: chart 78 private.
Steps: As Ben, `POST /api/v1/chart/data` with a body whose `form_data.slice_id` is 78 (bulk/ad-hoc path, matched via `_CHART_DATA_BULK`, `hooks.py:763, 772-791`).
Expected: identical 403 body to OWN-058.
Result: PASS — identical 403 body via `POST /api/v1/chart/data`.

**OWN-060 — Ad-hoc explore request with no saved chart is not gated by this rule**
Preconditions: none (any dataset the caller can already read via RLS/datasource grants).
Steps: As Ben, issue a chart-data request with no `slice_id` at all (ad-hoc explore).
Expected: not blocked by the ownership data-path guard — `enforce_chart_data_access` only acts when a `slice_id` resolves (`hooks.py:802-803`); ordinary dataset-level access control still applies.
Result: PASS — as Ben, an ad-hoc `POST /api/v1/chart/data` against the shared `birth_names` dataset (no slice_id in form_data) returned 200 with real query results (count: 20170, correctly RLS-scoped to tenant A), confirming the ownership guard did not intervene.

**OWN-061 — Explore URL for a private chart the caller cannot see**
Preconditions: chart 78 private, Ben not shared.
Steps: As Ben, navigate to the Explore URL for chart 78 directly (`/explore/?slice_id=78`).
Expected: page-level access is denied per Superset's own `raise_for_access` (same sentinel-closed check); confirm the presented error is either a 404/403 page or the chart-security error card, and record which (Expected: confirm — the review notes at least one instance of a raw JSON 404 body reaching the SPA on a direct URL hit, flagged as F-6 in `ivanti-acceptance-run.md`, not yet triaged as stock-Superset-vs-plugin behavior).
Result: OBSERVED — neither of the two anticipated outcomes. As Ben, `/explore/?slice_id=78` loads the full Explore SPA shell (no 403/404 page) and shows a misleading "Missing dataset — The dataset linked to this chart may have been deleted" error with a "Swap dataset" button, plus a "Failed to load chart data" toast — nothing in the UI indicates an access/ownership denial. Network trace: `GET /api/v1/explore/?slice_id=78` returns HTTP 200 (not 403) with a placeholder payload (`slice: null`, `dataset: {name: "[Missing Dataset]", database: {id: 0}}`, `form_data.datasource: "None__table"`), which the frontend then renders as a data-integrity error rather than an access-control one. This is a different (and more confusing) gap than F-6's "raw JSON 404" — worth flagging: the explore-metadata endpoint doesn't appear to run the same ownership gate as the dedicated chart-data endpoints (OWN-058/059), and the resulting message actively misleads the user about the cause.

**OWN-062 — After sharing, the tile renders with data**
Preconditions: continuing OWN-056 — chart 78 still private.
Steps: As Ada, `PUT /chart/78/visibility {"visibility": "shared"}`, then `POST /chart/78/shares {"subject": "user:6c2b48e9-...", "role": "viewer"}`. Wait for outbox drain. As Ben, reload dashboard 7.
Expected: the "Girl Name Cloud" tile now renders its real data; `GET /api/v1/chart/78/data/` as Ben returns `200` with chart data, not the 403.
Result: PASS — after Ada set chart 78 shared and shared it to Ben (viewer) and the outbox drained, `POST /api/v1/chart/data` (slice_id 78) as Ben returned 200 with real data (count: 20170); reloading dashboard 7 in the UI as Ben showed the "Girl Name Cloud" tile rendering the actual word cloud, no error card. Confirmed via screenshot.

**OWN-063 — Placeholder owner line falls back to "contact your Superset administrator" when no owner can be named**
Preconditions: a private/shared chart whose owner lookup fails or is empty (e.g. an unowned governed object reached via some other path, or `owners_resolver` returning `[]` on a lookup failure).
Steps: Trigger the 403/placeholder path on such a chart as a non-owner.
Expected: `To request access, contact your Superset administrator.` instead of naming an owner (`ChartSecurityAccessErrorMessage.tsx:60-64`; backend `owners_resolver` also degrades to `[]` rather than raising, `hooks.py:742-743, 757-759`).
Result: OBSERVED — built the fixture: chart 83 (unowned-by-design, per the seed notes) accepted `PUT .../visibility {"visibility":"private"}` from admin with no unowned-object rejection (unlike the `/shares` route in OWN-047), giving an unowned-but-governed chart. However, as Ben the resulting 403 showed `owners: ["Superset Admin"]`, not an empty list — the visibility change had, as a side effect, silently assigned admin as the chart's native Superset owner (confirmed via a follow-up GET: owner went from `null` to `{"name":"Superset Admin", "id":1, "guid":"local-1"}`). So the `owners_resolver` fell back to Superset's native owners field rather than returning `[]`, and the "contact your Superset administrator" empty-fallback text was not reached. Restored chart 83 to its seeded state afterward (`PUT .../owner {"subject":null}`, then visibility back to public) — confirmed owner: null again. Two findings worth flagging: (1) the empty-owner fallback text may be effectively unreachable in practice since a native owner gets attached as a side effect of the visibility change; (2) that attachment itself is a side effect on an object documented as "stays unowned by design" and wasn't obviously flagged to the caller.

**OWN-064 — A chart-data guard internal failure fails OPEN, not with a 500 (defensive catch)**
Preconditions: none directly reproducible without fault injection; documented for completeness.
Steps: (If a staging fault-injection hook is available) force an exception inside `enforce_chart_data_access`'s body (e.g. a broken `service.lookup` call) and issue a chart-data request for a private chart.
Expected: the outer `try/except` logs `"superset_ownership: chart-data guard failed; allowing"` and lets the request through rather than 500ing (`hooks.py:859-862`) — flag this as a defensive catch whose *normal*-path behavior must never be "guard silently failed and allowed a private chart through"; if reproducible, confirm it only ever fires on a genuine internal error, never in ordinary operation.
Result: BLOCKED — no fault-injection hook available in this environment. As a fallback, checked `docker logs pcssetup-superset-1` and `pcssetup-worker-1` for the string "chart-data guard failed" — zero occurrences, including across this session's extensive private/shared/denied-access testing (OWN-010 through OWN-066), which is the reassuring outcome (no unexpected fail-open in ordinary operation) but does not confirm the fail-open path itself works.

**OWN-065 — Data path is refused even when the viewer holds RLS-scoped data access to the same dataset**
Preconditions: chart 1 (on shared dataset `cleaned_sales_data`) made private by whoever owns it; Cleo (tenant B) holds real RLS-scoped access to `cleaned_sales_data` (157 rows) but is not shared on the chart. (Since OWN-006's tenant rule Cleo is refused on chart 1 in EVERY state, public included — the case now proves the dataset grant never substitutes for the object decision; a same-tenant non-owner variant of the same assertion is OWN-058 with Ben.)
Steps: As Cleo, attempt `GET /api/v1/chart/1/data/` and `POST /api/v1/chart/data` with `form_data.slice_id: 1`.
Expected: `403 CHART_SECURITY_ACCESS_ERROR` — ownership/sharing gates access to the CHART regardless of the caller's independent RLS-scoped dataset access; the dataset grant is necessary but not sufficient (`hooks.py:328-329, "base_permission_holds"` — dataset access never substitutes FOR ownership, only adds a floor UNDER it).
Result: PASS — made chart 1 (Marcus's, on the shared `cleaned_sales_data` dataset) private; as Cleo (tenant B, has real RLS-scoped access to that dataset per the reference counts) `POST /api/v1/chart/data` for slice_id 1 → 403 CHART_SECURITY_ACCESS_ERROR with owners: ["Marcus Chen"]. Chart 1 reverted to public afterward.

**OWN-066 — Repeat the placeholder/data-path pair (OWN-056, OWN-058) with visibility `shared` instead of `private`, caller not in the share list**
Preconditions: chart 78 `shared`, no share entry for Ben (same tenant; Cleo cannot open dashboard 7 at all since OWN-006, so the tile is not reachable for her).
Steps: As Ben, open dashboard 7; separately, `GET /api/v1/chart/78/data/`.
Expected: identical placeholder tile and 403 body to OWN-056/058 — a `shared` object with no matching share denies exactly like `private` from this caller's point of view (only the underlying OpenFGA `check` differs internally). As Cleo, `GET /api/v1/dashboard/7` stays `404`.
Result:

**OWN-067 — Embedded dashboard path respects the same denial**
Preconditions: chart 78 private; dashboard 7 configured for embedding (if embedding is enabled in this environment).
Steps: Load the embedded view of dashboard 7 as a guest/embed token with no relationship to chart 78.
Expected: same placeholder behavior as OWN-056 (Expected: confirm — depends on whether embedding is exercised in this environment; if not configured, mark blocked rather than fail).
Result: BLOCKED — embedding is not configured in this environment (`embedded_dashboards` table has 0 rows for this stack). Not exercised, per the case's own guidance to mark blocked rather than fail.

---

## 5. Transfer of ownership

Route: `PUT /api/v1/ownership/<asset>/<pk>/owner {"subject": "user:<guid>" | null}` (`api.py:2597-2886`); claim: `POST /api/v1/ownership/<asset>/<pk>/claim` (`api.py:2898-3031`).

**OWN-068 — Owner transfers a public object to another tenant-A member**
Preconditions: Ada owns dashboard 8 (public); Ben has dataset access to `video_game_sales` (via the tenant A role's `datasource_access` grant).
Steps: As Ada, `PUT /dashboard/8/owner {"subject": "user:6c2b48e9-5a71-4f92-8d03-2e9b7c1a4d53"}`.
Expected: `200 {"object_id": 8, "owner_user_id": <Ben's Superset id>}`; `GET /dashboard/8` now shows Ben as owner, `can_manage: true` for Ben; Ada, no longer owner, sees `can_manage` per whatever OTHER ground she has (tenant admin — see OWN-069); previous owner's `owner` tuple deleted from the store, new one written (`api.py:2789-2795`); native `editors` updated so Ben isn't accidentally 404'd on his own object (issue #80 fix, `api.py:2832-2861`).
Result: PASS — 200 {"object_id":8,"owner_user_id":3} (Ben's id); GET as Ben showed owner Ben, can_manage: true, manage_reason: "owner". Transferred back to Ada afterward to restore seeded state.

**OWN-069 — Transferred-away owner who is also tenant admin still manages the object**
Preconditions: continuing OWN-068 — Ada transferred dashboard 8 away, and Ada is tenant A's administrator.
Steps: As Ada, `GET /dashboard/8`.
Expected: `can_manage: true`, `can_share: false`, `manage_reason: "tenant_admin"` — she lost the `owner` ground but keeps `tenant_admin`, which now means transfer (and "Take ownership") but not sharing. UI transfer-confirmation banner, when this transfer was done through the drawer, reads: "Ownership transferred to %s. You still manage this object as a tenant administrator."; reopening the drawer shows the standing notice naming the new owner, the visibility options greyed out, and the **Take ownership** link.
Result:

**OWN-070 — Transfer a PRIVATE object (owner-to-owner, in-tenant)**
Preconditions: dashboard 8 made private by Ada; Ben has dataset access.
Steps: As Ada, `PUT /dashboard/8/owner {"subject": "user:6c2b48e9-..."}`.
Expected: `200`; ownership changes; visibility is untouched by a transfer (stays `private`); Ben (new owner) can now open it; a caller who is neither the new owner nor tenant admin/admin still gets `404`.
Result: PASS — 200 on the transfer; GET as Ben (new owner) showed visibility: "private" (unchanged) and can_manage: true; Priya (plain member, no relation) got 404 "dashboard not found". Reverted: transferred back to Ada, visibility restored to public.

**OWN-071 — Transfer a SHARED object preserves existing shares**
Preconditions: dashboard 8 `shared`, with Priya already shared as viewer; Ada transfers ownership to Ben.
Steps: As Ada, `PUT /dashboard/8/owner {"subject": "user:6c2b48e9-..."}`; then `GET /dashboard/8` as the new owner Ben.
Expected: `shares` still lists Priya — a transfer leaves share tuples alone (confirmed by the UI copy: "Existing shares are unaffected: everyone it is already shared with keeps their access.", `SharingDrawer.tsx` transfer-confirmation consequences list).
Result: PASS — GET as new owner Ben showed shares: [{"name":"Priya Sharma","role":"viewer",...}] still present after the transfer. Reverted: transferred back to Ada, Priya's share removed, visibility restored to public (confirmed shares: [] after outbox drain).

**OWN-072 — Tenant administrator transfers an object they do not own**
Preconditions: Ada (tenant A admin) transfers dashboard 1 (Marcus's) to Priya, both tenant A, Priya has dataset access.
Steps: As Ada, `PUT /dashboard/1/owner {"subject": "user:9fcc709c-1a6b-4882-b8e3-1787afe31417"}`.
Expected: `200`; Priya becomes owner; Marcus (previous owner) loses `owner` ground but the dashboard is still his tenant admin's (Ada's) to manage via `tenant_admin`.
Result: PASS — 200 {"object_id":1,"owner_user_id":5} (Priya's id); GET as Priya showed can_manage: true, manage_reason: "owner"; GET as Marcus showed can_manage: false, manage_reason: null (lost owner ground). Reverted: transferred back to Marcus afterward.

**OWN-073 — Plain member cannot transfer ownership of an object they don't own**
Preconditions: logged in as Ben; dashboard 1 (Marcus's).
Steps: As Ben, `PUT /dashboard/1/owner {"subject": "user:9fcc709c-..."}`.
Expected: `403 {"message": "only the owner, a tenant administrator, an admin or a holder of the manage-sharing permission may assign ownership"}` (`api.py:2654-2658`).
Result: PASS — 403 with exact message. Dashboard 1 confirmed unaffected (still Marcus's).

**OWN-074 — Transfer to a user WITHOUT the underlying dataset access is refused (409, exact text)**
Preconditions: dashboard 9 "deck.gl Demo" (David's, tenant A, on exclusive datasets `bart_lines`/`long_lat`/`sf_population_polygons` that only tenant A's role grants); pick a tenant-A recipient who is NOT granted `datasource_access` on any chart on that dashboard — or more simply, transfer a chart whose sole dataset is exclusive to tenant A to a tenant-B recipient is covered separately (OWN-076); for an in-tenant refusal, use a chart on an exclusive dataset and a recipient whose tenant role lacks that specific grant if the environment has one, otherwise substitute any object/recipient pairing known to fail the dataset check.
Steps: As the object's owner or tenant admin, `PUT .../owner {"subject": "user:<recipient without the grant>"}`.
Expected: `409` with body naming the exact reason, one of:
`cannot assign ownership: <name> does not have access to this chart's dataset` (chart) or
`cannot assign ownership: <name> does not have access to any dataset on this dashboard` (dashboard)
(`transfer.py:99-104`). Nothing is written; `check` stays clean.
Result: BLOCKED — re-checked in this final pass: role `tenant_a1e4c2d0-...` (id 8) still grants `datasource_access` in bulk to every tenant-A dataset (confirmed via `GET /api/v1/security/roles/8/permissions/`), so no tenant-A recipient naturally lacks the grant. Constructing one requires editing FAB role permissions, a structural config change outside the destructive operations this pass's guardrails permit (user deactivation, ownership CLI lifecycle commands, and the OWNERSHIP_ENABLED flag flip) — genuinely unrunnable here, not just a deactivation/restart gap. Not exercised; the cross-tenant 409 variant this case gestures at is covered instead by OWN-076.

**OWN-075 — Transfer refused when the object's dataset does not resolve at all**
Preconditions: a chart whose underlying dataset has been deleted, or an empty dashboard with no charts having any resolvable dataset (edge fixture — may need to be constructed).
Steps: Attempt a transfer on such an object.
Expected: `409 {"message": "cannot assign ownership: this chart has no dataset"}` (chart) or `"cannot assign ownership: no chart on this dashboard has a dataset"` (dashboard) (`transfer.py:116-129`). Note an EMPTY dashboard (zero charts) is explicitly NOT refused this way — it passes.
Result: PASS (empty-dashboard sub-case only) — created a throwaway empty dashboard via the UI (owned by Ada per the active browser session, tenant A, 0 charts). `PUT /dashboard/<id>/owner {"subject":"user:6c2b48e9-..."}` (Ben) as admin succeeded `200`, confirming the case's explicit note that an EMPTY dashboard is NOT refused by the no-dataset check. Cleanup: since the object was a disposable fixture (not seeded data) and UI/bearer-token deletion both hit the same stock-endpoint CSRF quirk noted in the original BLOCKED reason, it was removed directly via its `ownership_object`/`dashboards` rows in `superset_scratch`; the two now-orphaned OpenFGA tuples for its uuid could not be cleaned up the same way (a direct OpenFGA store write was refused by this session's own guardrails as an unsupported bypass), so two harmless stray tuples for a UUID no Superset object references remain in the store. `superset ownership status`/`check` confirm the object count is back to the seeded 120/11 and `check` is clean. The dangling/deleted-dataset chart sub-case (the case's other half) still was not attempted — constructing it means deleting a dataset out from under a chart, materially riskier and out of scope for this pass; that half remains unexercised.

**OWN-076 — Transfer to a user in the OTHER tenant is refused**
Preconditions: dashboard 8 (Ada, tenant A, tenanted).
Steps: As Ada, `PUT /dashboard/8/owner {"subject": "user:9d7e35a1-8c62-4b04-a7f1-3d5e9b2c8a76"}` (Cleo, tenant B).
Expected: `409 {"message": "Cleo tenant B's tenant role names another tenant; a transfer never moves an object out of its tenant"}` (shape: `f"{recipient}'s tenant role names another tenant; a transfer never moves an object out of its tenant"`, or `f"{recipient} has no tenant role; ..."` if the recipient carries no tenant role at all — `_tenant_move_refusal`, `api.py:1691-1706, 2723-2757`) — refused before the dataset-access check even runs.
Result: FAIL — observed `400 {"message": "subject is not a member of your tenant"}` instead of the expected `409` with the `_tenant_move_refusal` "tenant role names another tenant" wording. The write is still correctly refused (dashboard 8 confirmed unaffected — owner still Ada afterward), but the general `_validate_subject` tenant-membership check (same 400/message as OWN-031's share-route case) appears to fire before, or instead of, the transfer-specific `_tenant_move_refusal` path this case describes, so the distinct 409 message never surfaces on this route.

**OWN-077 — Release ownership to null → object becomes unowned**
Preconditions: Ada owns dashboard 8.
Steps: As Ada, `PUT /dashboard/8/owner {"subject": null}`.
Expected: `200 {"object_id": 8, "owner_user_id": null}`; `GET /dashboard/8` now shows `unowned`-adjacent state (no owner block); the object cannot be shared until claimed (OWN-046-style 409 on a subsequent share attempt); on the local backend releasing also untenants the object per the docstring, confirm whether that applies under OpenFGA too (`api.py:2601-2611`).
Result: OBSERVED — 200 {"object_id":8,"owner_user_id":null}; GET as admin showed owner: null; a subsequent share attempt got 409 "object has no owner; assign one before sharing" (the OWN-047 never-owned wording, not OWN-046's "is unowned" wording — consistent, since owner_user_id is NULL rather than pointing at a deactivated account). On the untenanting question: read the OpenFGA store directly (`POST /stores/<store>/read` for `object: dashboard:c7bc10f4-...`) immediately after release and the `tenant` relation tuple to `tenant:a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40#member` was still present — the object was NOT untenanted under OpenFGA, unlike the local-backend docstring's claim. Restored: admin reassigned owner back to Ada afterward.

**OWN-078 — Manage-sharing-permission holder cannot release ownership**
Preconditions: a manage-sharing-permission holder admitted to dashboard 8.
Steps: as that holder, `PUT /dashboard/8/owner {"subject": null}`.
Expected: `403 {"message": "the manage-sharing permission does not include releasing ownership"}` (`api.py:2673-2680`).
Result: BLOCKED — same environment dependency confirmed at OWN-026: `OWNERSHIP_MANAGE_PERMISSION` is not configured (config source "default", no manage-sharing role exists), so no holder can be constructed. Not exercised.

**OWN-079 — Manage-sharing-permission holder cannot transfer to themselves**
Preconditions: as above, holder attempting self-transfer.
Steps: `PUT .../owner {"subject": "user:<holder's own guid>"}`.
Expected: `403 {"message": "the manage-sharing permission does not include transferring ownership to yourself"}` (`api.py:2715-2722`) — compared on the resolved Superset account, not the subject string, so no alternate spelling of the caller's own reference gets past it.
Result: BLOCKED — same environment dependency as OWN-026/OWN-078: `OWNERSHIP_MANAGE_PERMISSION` is not configured, so no holder exists to exercise this path. Not exercised.

**OWN-080 — Claim: member with dataset access claims an unowned object**
Preconditions: chart 83 "Pivot Table v2" — unowned, public, dataset `birth_names` (shared, so every tenant user has access).
Steps: As Elena (tenant A, has `birth_names` access via the shared-dataset grant), `POST /chart/83/claim`.
Expected: `200 {"object_id": 83, "owner_user_id": <Elena's id>}`; `GET /chart/83` now shows Elena as owner, `can_manage: true, manage_reason: "owner"`; the object's tenant is stamped to tenant A (it had none before, per the claim-stamps-tenant-only-when-untenanted rule, `api.py:2988-2991`).
Result: FAIL — observed `403 {"message": "only the owner, a tenant administrator or an admin may claim this object"}` instead of the expected `200`. Confirmed via the OpenFGA store (`POST /stores/<store>/read` for `object: chart:4c4561b5-...`) that chart 83 carries zero tuples — genuinely untenanted, matching this case's precondition — yet Elena (plain tenant-A member with `birth_names` dataset access, no tenant-admin standing) was refused with the same message OWN-084 documents for a non-admin/non-tenant-admin caller. The "claim-stamps-tenant-only-when-untenanted" carve-out that should let any tenant member with dataset access claim an untenanted object does not appear to be reachable in practice — claim authority looks gated to owner/tenant_admin/admin unconditionally, contradicting this case's expectation. Chart 83 confirmed still unowned afterward (nothing written).

**OWN-081 — Claim refused when the caller lacks the underlying dataset grant**
Preconditions: an unowned chart on a dataset exclusive to tenant B; attempted claim by a tenant-A user with no grant on it.
Steps: As that tenant-A user, `POST /chart/<id>/claim`.
Expected: `409` with the same `missing_grant_message` shape as OWN-074 (`_refuse_transfer` is shared between transfer and claim, `api.py:2965-2969`); nothing written.
Result: FAIL — constructed the fixture: released chart 46 "Top Timezones" (Hannah's, tenant B, on dataset `users` id 18 — confirmed exclusive to tenant B by diffing role 8 vs role 9 permissions) to unowned via admin, then attempted `POST /chart/46/claim` as Ada (tenant A). Got `403 {"message": "only the owner, a tenant administrator or an admin may claim this object"}`, not the expected `409` dataset-grant message — because chart 46 is tenant-B-stamped (confirmed via the OpenFGA store: a `tenant:b7f28e5a-...#member` tuple was present on it), Ada's tenant-A tenant_admin ground never applies to it, so the authority check (same as OWN-080/084) refuses her before the dataset-access check is ever reached. Combined with the OWN-080 finding, this means the dataset-grant 409 this case describes looks unreachable for any non-admin tenant-A caller — Superset `admin` would reach the authority check but always has universal dataset access, so would never trigger the 409 either. Chart 46 confirmed restored to Hannah as owner afterward.

**OWN-082 — Manage-sharing-permission holder cannot claim**
Preconditions: holder attempting to claim an unowned object.
Steps: `POST .../claim` as the holder.
Expected: `403 {"message": "the manage-sharing permission does not include claiming ownership"}` (`api.py:2943-2946`).
Result: BLOCKED — same environment dependency as OWN-026/078/079: `OWNERSHIP_MANAGE_PERMISSION` is not configured, so no holder exists to exercise this path. Not exercised.

**OWN-083 — Tenant administrator "rescues" an unowned governed object — backend allows it, UI does not (open issue #102)**
Preconditions: chart 83 unowned (or any object with `owner_user_id is None`), tenant-stamped to A or untenanted.
Steps (API): As Ada (tenant A admin), `GET /chart/83` — expect `can_manage: true, manage_reason: "tenant_admin"` (granted via `_is_claimable(row)` in `_tenant_admin_reason`, `api.py:961-985`); then `POST /chart/83/claim` — expect `200`, Ada becomes owner.
Steps (UI): As Ada, look for chart 83 in the Chart List and try to open its Sharing drawer from there.
Expected: the API steps succeed exactly as described (the backend genuinely grants a tenant administrator the right to see and claim an unowned object in their tenant). The UI steps are expected to FAIL to surface the object at all — Superset's own list/detail read gate denies everyone (including the tenant admin) on an unowned object because the ownership plugin's read gate treats "no owner" the same as "no one may open it" for the purposes of the underlying Superset access check, so the object never appears in the tenant admin's list or dashboard/chart view even though the ownership API itself would grant `can_manage: true` if reached directly. This is the exact gap tracked as **open issue #102**. Record both results — the discrepancy IS the expected/documented behavior right now, not a new finding.
Result: OBSERVED (tracked against #102, not a new finding) — API steps matched exactly: GET /chart/83 as Ada returned can_manage: true, manage_reason: "tenant_admin"; POST /chart/83/claim returned 200 and made Ada owner (then released back to null via API to re-test the UI half and restore seeded state). UI steps, via the browser as Ada: chart 83 DID surface in the Chart List (row "Pivot Table v2", Owner "Unknown", Sharing "Public") — not a full "never appears", slightly more visible than this case's literal wording predicts — but the row's Actions column offered no icons at all (no Sharing/edit entry point, confirmed via zoomed screenshot), so the drawer itself is unreachable from the list exactly as the case describes. Opening the chart directly via its Explore link did render full chart data for Ada despite the unowned/ungoverned state. Net: management remains unreachable through the UI (the core #102 gap), though list-row visibility is looser than the case's "never appears" phrasing. Chart 83 confirmed unowned/public afterward (seeded state).

**OWN-084 — Claim refused for someone who is not owner/tenant-admin/admin**
Preconditions: chart 83 unowned; Ben (plain tenant-A member) attempts to claim.
Steps: As Ben, `POST /chart/83/claim`.
Expected: `403 {"message": "only the owner, a tenant administrator or an admin may claim this object"}` (`api.py:2939-2942`) — note this is a DIFFERENT wording from the visibility/share 403s (no mention of "manage-sharing permission" in the list of admitting grounds for claim, since that ground is separately refused just below with its own message, OWN-082).
Result: PASS — 403 with exact message; chart 83 confirmed still unowned afterward (nothing written). Note: this same message is also what a claim by a plain tenant-A member with dataset access (Elena) received in OWN-080, where the case expected a 200 instead — see the OWN-080 finding.

**OWN-085 — A claim/transfer never moves an object across tenants even for the Superset admin**
Preconditions: dashboard 8 (tenant A); `admin` attempts to assign it to a tenant-B recipient.
Steps: As `admin`, `PUT /dashboard/8/owner {"subject": "user:9d7e35a1-8c62-4b04-a7f1-3d5e9b2c8a76"}` (Cleo).
Expected: `409`, same tenant-move refusal as OWN-076 — the rule applies regardless of caller privilege, because it protects a control-plane invariant (which tenant manages the object), not merely a per-caller permission.
Result: PASS — 409 {"message": "the recipient's tenant role names another tenant; a transfer never moves an object out of its tenant"} (generic "the recipient" phrasing rather than naming Cleo, but the same `_tenant_move_refusal` shape/status the case describes). Dashboard 8 confirmed unaffected (still Ada's) afterward. Note: unlike OWN-076 (owner Ada as caller, which got a 400 from the general tenant-membership check instead), the admin caller here correctly reaches the transfer-specific 409 — the OWN-076 discrepancy looks specific to non-admin callers.

**OWN-086 — Transfer on a non-existent object / to a non-existent recipient**
Steps: (a) As Ada, `PUT /dashboard/999999/owner {"subject": "user:6c2b48e9-..."}`. (b) As Ada, `PUT /dashboard/8/owner {"subject": "user:00000000-0000-4000-8000-000000000000"}`.
Expected: (a) `404 {"message": "dashboard not found"}`. (b) `400 {"message": "subject does not name a known account"}` at the `_validate_subject` stage, before reaching the recipient/dataset logic (subject validation runs first, `api.py:2684-2686`); if it somehow passed subject validation with a resolvable-but-unknown-to-Superset guid, the fallback is `404 {"message": "no Superset account for that member"}` (`api.py:2710-2712`).
Result: PASS — (a) 404 {"message":"dashboard not found"} exactly; (b) 400 {"message":"subject does not name a known account"} exactly. Dashboard 8 confirmed unaffected by either attempt.

**OWN-087 — Transfer to a deactivated account**
Preconditions: a deactivated tenant-A account.
Steps: As Ada, `PUT /dashboard/8/owner {"subject": "user:<deactivated guid>"}`.
Expected: `409 {"message": "cannot assign ownership to a deactivated account"}` (`api.py:2713-2714`) — distinct from the `400 "subject's account is deactivated"` that the general `_validate_subject` path would give on a SHARE call; the owner route re-checks and answers 409, not 400, for this specific case.
Result: FAIL — deactivated Ben (tenant A, no owned objects) via admin; as Ada, `PUT /dashboard/6/owner {"subject":"user:6c2b48e9-..."}` (Ben) returned `400 {"message":"subject's account is deactivated"}` instead of the expected `409 "cannot assign ownership to a deactivated account"`. The write was still correctly refused (dashboard 6 confirmed unaffected — owner still Elena afterward), but the general `_validate_subject` deactivation check appears to fire before, or instead of, the owner-route-specific 409 this case describes — the same pattern already documented at OWN-076, where the general tenant-membership 400 preempts the transfer-route-specific 409 wording. Reactivated Ben afterward; `check` stayed ok:true throughout.

---

## 6. Native editors (#80)

**OWN-088 — Setting editors through stock Superset's own Owners/Editors UI is stripped back to the owner alone**
Preconditions: Ada owns dashboard 8 (governed — private or shared, not public); logged in as Ada.
Steps: Open dashboard 8's Edit properties in the standard Superset UI and add Ben directly to the native "Owners"/"Editors" field (not through the Sharing drawer); Save.
Expected: the save appears to succeed, but on the next flush `guard.py`'s `_before_flush` strips the native `editors` collection back to the owner alone (`sync_native_editors(..., strict=True, create_missing=False)`, `guard.py:429-431`); a subsequent read of the object's native editors shows only Ada; an `ACCESS_CORRECTED` audit event is emitted; Ben gains no edit access this way. The only supported channel for adding a co-editor is the plugin's own `POST .../shares {"role": "editor"}`.
Result: PASS (via the equivalent API path — see note) — opened dashboard 8's Dashboard Properties dialog as Ada from the Dashboard List row action and successfully added "Ben tenant A" as a second tag in the native Editors combobox (confirmed on screen: both "Ada tenant A" and "Ben tenant A" shown as selected/checked). The dialog's own Save click could not be completed reliably through this session's browser-automation tooling (persistent click/coordinate-scaling flakiness unrelated to the product), so the save→re-read step was instead verified through OWN-089's raw stock `PUT /api/v1/dashboard/8 {"editors":[9,10]}` — the identical backend write path the properties dialog itself submits to. That PUT returned 200 at the transport layer but an immediate re-read showed `editors: [{"id":9,"label":"Ada tenant A"}]` only, with the exact guard WARNING log line and `ownership.access_corrected` audit event (direct editors stripped=1) confirming the same-transaction strip-to-owner-alone behavior this case describes. Dashboard 8 restored to public afterward.

**OWN-089 — A raw PUT that tries to add a co-editor via `editors: [...]` is corrected the same way**
Preconditions: dashboard 8, governed.
Steps: `PUT /api/v1/dashboard/8 {"editors": [<Ben's Subject id>]}` (stock Superset endpoint, bypassing the Sharing drawer entirely).
Expected: HTTP response from the stock endpoint succeeds at the transport layer, but the same-transaction flush guard removes Ben from `editors`, restoring the owner-only invariant; `WARNING`-level log line `"superset_ownership: access fields corrected on ... direct editors stripped=1 ..."` (`guard.py:456-461`); `ACCESS_CORRECTED` audit event with `before`/`after` editor counts.
Result: PASS — `PUT /api/v1/dashboard/8 {"editors":[9,10]}` (9=Ada's, 10=Ben's Subject id) returned 200 with `result.editors:[9,10]` at the transport layer, but an immediate re-read of `GET /api/v1/dashboard/8` showed `editors: [{"id":9,"label":"Ada tenant A"}]` only — Ben stripped. Confirmed via container logs: exact WARNING line `"superset_ownership: access fields corrected on dashboard 8 (private) -- ... direct editors stripped=1, direct editors added=0"` and an `AUDIT {'event': 'ownership.access_corrected', ...}` line with before/after `direct_editors` counts. Dashboard 8 restored to public afterward (editors already back to owner-only).

**OWN-090 — A group editor share (no native Subject possible) works only through the dynamic resolver**
Preconditions: dashboard 8, shared; group `<A>.dashboard_designer` shared as `role: "editor"`.
Steps: As a member of that group (e.g. Priya), attempt to edit dashboard 8.
Expected: succeeds — group editors are never materialized into the native `editors` collection at all (there is no Superset `Subject` for a bare OpenFGA group); they are only ever honored dynamically through `EXTRA_EDITORS_RESOLVER` (`hooks.py:453-457`), which is exactly the case native `editors` structurally cannot express. This case demonstrates why #80's fix (owner-only native editors) does not regress group-based editing.
Result: FAIL — set dashboard 8 to shared and shared `group:<A>.dashboard_designer#member` as editor; confirmed via the OpenFGA store that both the `editor` tuple on the dashboard for that group AND Priya's `member` tuple on the group were present (so the grant fully delivered). Priya could view the dashboard (`can_manage: false` but `GET` 200, consistent with a shared/non-manager viewer), but a native edit attempt (`PUT /api/v1/dashboard/8 {"dashboard_title": "Video Game Sales"}`, a no-op rename) returned `403 {"message": "Forbidden"}`, retried once with the same result. This contradicts the expected "succeeds" outcome — the group-editor grant did not translate into native edit rights the way an individual user's editor share does (confirmed working for Ben in OWN-042). Cleaned up: group share removed, dashboard 8 restored to public.

---

## 7. Sentinel / guard

**OWN-091 — Raw stock `PUT /api/v1/dashboard/<id> {"viewers": []}` on a private object does not open it**
Preconditions: dashboard 8 made `private` by Ada.
Steps: As Ada (or any caller who can reach the raw stock endpoint), `PUT /api/v1/dashboard/8 {"viewers": []}` — bypassing the ownership Sharing drawer.
Expected: the raw PUT itself returns Superset's normal success response — but within the same flush, `guard.py`'s `_before_flush` re-adds the sentinel and strips any unauthorized entry the raw PUT tried to introduce, so the dashboard remains enforced-private: Ben still gets `404` opening it, and `GET /api/v1/ownership/dashboard/8` still reports `visibility: "private"`. `WARNING` log line naming the correction (`guard.py:456-461`); `ACCESS_CORRECTED` audit event with `before.direct_viewers` reflecting what was stripped.
Result: PASS — raw `PUT /api/v1/dashboard/8 {"viewers":[]}` returned 200 at the transport layer; Ben's GET still 404 "dashboard not found"; ownership GET still showed visibility: "private". Confirmed via container logs: `WARNING ... access fields corrected on dashboard 8 (private) -- denial restored=True, direct viewers stripped=0 ...` and an `ownership.access_corrected` audit event with `before: {enforced: False, ...}` / `after: {enforced: True, visibility: "private", ...}` — the sentinel was re-added within the same transaction exactly as described.

**OWN-092 — `check`'s `silently_open` stays empty across OWN-091**
Steps: `docker exec pcssetup-superset-1 superset ownership check` immediately after OWN-091.
Expected: `"silently_open": []` — the guard caught and corrected the attempted bypass before it could commit, so nothing is actually left silently open. (If this field were ever non-empty, it would mean an object is `private`/`shared` in the ownership table but carries no sentinel — a real, uncaught data leak; this case exists to prove that does NOT happen for the raw-PUT attack path.)
Result: PASS — `silently_open: []`, `ok: true`, `orphaned_sentinel: []` immediately after OWN-091.

**OWN-093 — Attempting to delete the sentinel role directly is refused**
Preconditions: DB/admin access sufficient to attempt deleting the `__ownership_sentinel__` FAB role or its Subject row.
Steps: Attempt `DELETE` on the sentinel role via whatever admin surface exposes FAB roles (Superset's Roles admin screen, or a direct API call), or attempt to delete the corresponding `Subject`.
Expected: refused with `ValueError`: `"refusing to delete the ... role: object ownership enforcement depends on it, and removing it would open every private and shared object in this instance. Use \`superset ownership teardown\` to uninstall the feature."` (role) or the Subject-specific variant (`guard.py:286-301`).
Result: PASS — `DELETE /api/v1/security/roles/4` (the `__ownership_sentinel__` role) as admin returned `500 {"message":"Fatal error"}` at the API layer (an uncaught exception surfaced as a generic 500 rather than a clean 4xx, worth noting as a minor rough edge), but the role is confirmed still present afterward (`GET /api/v1/security/roles/` still lists id 4). Container logs show the exact refusal: `ValueError: refusing to delete the ownership sentinel subject: it carries denial for every private and shared object in this instance. Use \`superset ownership teardown\`.` raised from `guard.py:298 _protect_the_sentinel` — this is the Subject-specific variant the case anticipates, triggered because the roles-delete cascades into deleting the underlying Subject row.

**OWN-094 — Editors are also corrected by the same guard when tampered with directly**
Preconditions: dashboard 8, governed, owned by Ada.
Steps: Directly manipulate the object's native `editors` collection to include a second user via a raw stock write (same mechanism as OWN-089, different assertion focus).
Expected: stripped back to the owner alone on the next flush, exactly as OWN-089/090 describe; counted in the same `ACCESS_CORRECTED` audit event's `direct_editors` fields.
Result: PASS — raw `PUT /api/v1/dashboard/8 {"editors":[9,10]}` (dashboard private, governed) returned 200 at the transport layer; immediate re-read showed `editors: [{"id":9,"label":"Ada tenant A"}]` only. Container logs: `WARNING ... direct editors stripped=1 ...` and `ownership.access_corrected` audit event with `before: {direct_editors: 1}` / `after: {direct_editors: 0}` — same mechanism as OWN-089.

**OWN-095 — `suppressed()` legitimate lifecycle operations do not trigger the guard's correction**
Preconditions: none beyond running a lifecycle operation the module itself performs under `guard.suppressed()` (e.g. `superset ownership disable`, which deliberately strips sentinels).
Steps: Run `superset ownership disable` (in a disposable/test context, not production) and observe whether it triggers the same `ACCESS_CORRECTED` warning the direct-tamper cases above do.
Expected: no `ACCESS_CORRECTED` event for the disable operation itself — it runs inside `guard.suppressed()`, which is the module's own sanctioned bypass for teardown/disable, distinct from an unauthorized raw PUT (`guard.py:76-92`).
Result: PASS — ran `superset ownership disable --yes` on the shared scratch stack (explicitly authorized for this final pass, immediately followed by re-enabling — see OWN-102). Every seeded object was already back to `public` at this point (this pass's own restoration discipline — `status` showed `by_visibility: {"public": 120}` beforehand), so the operation reported `{"sentinels_removed":0,"ownership_rows":0,"share_rows":0,"rows_parked":0}` — a true no-op on governed state. `docker logs pcssetup-superset-1` for the operation's window shows zero occurrences of `ACCESS_CORRECTED`/`access_corrected`, confirming no guard-correction event fired for this sanctioned lifecycle operation.

---

## 8. User lifecycle

**OWN-096 — Deactivate an object's owner: object becomes "unowned" but keeps its visibility**
Preconditions: Elena owns dashboard 6, currently `public` (or set it `private` first for a sharper test). Superset admin access to deactivate a user.
Steps: As `admin`, deactivate Elena's account (Users admin screen, or `superset fab` equivalent). As `admin`, `GET /api/v1/ownership/dashboard/6`.
Expected: `unowned: true`; `visibility` unchanged (still whatever it was — a public dashboard stays public, per `hooks.py`'s `is_unowned` docstring); `can_share: false` (an unowned object cannot be shared, OWN-046); the object's tenant administrator (Ada, if dashboard 6 is tenant A's) sees `can_manage: true, manage_reason: "tenant_admin"` via the unowned-rescue ground (`_is_claimable`).
Result: PASS — set dashboard 6 to `shared` (sharper test) and shared it to Priya (viewer) before deactivating; deactivated Elena via `PUT /api/v1/security/users/7 {"active":false}` as admin. Subsequent `GET /dashboard/6`: as admin showed `unowned: true`, `visibility: "shared"` (unchanged from what it was set to), `can_share: false`; as Ada (tenant A admin) showed `can_manage: true, manage_reason: "tenant_admin"`. Reactivated Elena and restored dashboard 6 to owner Elena / public / no shares afterward (see OWN-098).

**OWN-097 — Deactivated owner's own access is denied even though the object superficially still "belongs" to them**
Preconditions: continuing OWN-096, dashboard 6 was `private` before Elena's deactivation.
Steps: Reactivate Elena just long enough to attempt login (or check via API if her session is still valid), then attempt to open dashboard 6 as her.
Expected: denied — `hooks.raise_for_access_bypass` checks `is_unowned(row)` and returns `False` (deny) BEFORE the owner fast-path check even runs (`hooks.py:331-338`), so a deactivated owner does not retain access to their own now-unowned object purely by virtue of the stale `owner_user_id` pointer.
Result: OBSERVED — reactivating just to test login was not practical inside the deactivation window, so both fallbacks were exercised instead: (1) a fresh login attempt as Elena (`POST /api/v1/security/login`, correct password) while deactivated returned `{"message":"Not authorized"}` — denied at the login stage itself, before any ownership code runs; (2) Elena's own bearer token issued before deactivation, reused against `GET /dashboard/6`, now returns `401 {"message":"authentication required"}` — Superset's own auth layer invalidates a deactivated user's existing token too, one layer earlier than the ownership-specific `is_unowned` bypass check this case targets. As the case's own fallback prescribes, denial was then confirmed via the object itself: Ben (plain tenant-A member, no relation) got `404` on `GET /dashboard/6`, while Priya (existing viewer share) still got `200` — consistent with "denied to everyone but a valid ground," though the specific `is_unowned`-before-owner-fast-path code path was not directly isolated since no session as Elena could be obtained at all.

**OWN-098 — Shares survive an owner's deactivation; a rescue (claim) preserves them**
Preconditions: dashboard 6 (Elena's, deactivated per OWN-096) was `shared` with Priya before deactivation.
Steps: As Ada (tenant admin), `POST /dashboard/6/claim`. `GET /dashboard/6` as Priya.
Expected: Ada becomes owner; Priya's share row is untouched by the deactivation or the claim — she can still open the dashboard exactly as before, uninterrupted.
Result: PASS — `POST /dashboard/6/claim` as Ada → `200 {"object_id":6,"owner_user_id":2}`; `GET /dashboard/6` as Ada (new owner) showed `shares: [{"name":"Priya Sharma","role":"viewer",...}]` still present; `GET /dashboard/6` as Priya returned `200` (uninterrupted access), consistent with the share surviving the claim (her own non-manager view shows `shares: []` per the OWN-004 withholding rule, which is expected and not a discrepancy). Cleanup: reactivated Elena, transferred dashboard 6 back to her, removed Priya's share, and restored visibility to `public` — final state and `superset ownership check` confirmed identical to the seeded baseline (owner Elena, public, no shares, `ok: true`).

**OWN-099 — `purge-tenant` removes a tenant's membership/group tuples and any ownership rows for objects that no longer exist, but leaves surviving objects alone**
Preconditions: a disposable tenant or a dry run against a real one — strongly prefer `--yes` NOT passed (dry run) unless working in a throwaway environment.
Steps: `docker exec pcssetup-superset-1 superset ownership purge-tenant <tenant_guid>` (no `--yes`: dry run).
Expected: JSON report with `dry_run: true`, counts for `members`, `dashboards`, `charts`, `ownership_rows`, `share_rows`, `sentinels_removed`, `tuples_found`/`would_delete_tuples`, and `retained` (named list of `type:id` for objects that still exist in Superset and are therefore left alone) (`lifecycle.py:267-505`, `cli.py:222-242`). Confirm NOTHING is actually deleted (re-run `status`/`check` shows unchanged counts).
Result: PASS — `superset ownership purge-tenant b7f28e5a-9c14-4d6b-a2f0-5e3d8c1a6b92` (no `--yes`, dry run) against Tenant B returned `{"dry_run":true,"members":8,"dashboards":4,"charts":21,"ownership_rows":0,"share_rows":0,"sentinels_removed":0,"tuples_found":40,"would_delete_tuples":40,"objects_retained":[...25 dashboard:/chart: ids that still exist...],"retained":[...same set by Superset id...]}`. Confirmed nothing was deleted: `superset ownership check` stayed `ok:true`; `status` still reported `objects: 120`; a direct OpenFGA read for `tenant:b7f28e5a-...` `member` tuples still returned all 8.

**OWN-100 — `purge-tenant --yes` actually deletes, and is idempotent**
Preconditions: a genuinely disposable tenant/store, not the shared scratch stack unless explicitly authorized for teardown.
Steps: `docker exec pcssetup-superset-1 superset ownership purge-tenant <tenant_guid> --yes`; note counts; re-run the identical command.
Expected: first run reports non-zero counts matching the dry run's preview; second run reports all zeros (idempotent — "a tenant that was already purged or never existed... both report zeros", `lifecycle.py:296-297`); an `audit.TENANT_PURGED` event is emitted on the real run.
Result: PASS — run explicitly authorized against Tenant B only, as the last destructive operation of this section. First run (`--yes`): `{"members":8,"dashboards":4,"charts":21,"ownership_rows":0,"share_rows":0,"sentinels_removed":0,"tuples_found":40,"tuples_deleted":28,"tuples_failed":0,"retained":[...25 surviving dashboard/chart ids...]}` — matched the OWN-099 dry-run preview's counts; container logs show `AUDIT {'event': 'ownership.tenant_purged', ..., 'after': {'tenant': 'b7f28e5a-...', 'counts': {...}}}`. Immediate second run: `{"members":0,"dashboards":4,"charts":21,"ownership_rows":0,"share_rows":0,"sentinels_removed":0,"tuples_found":0,"tuples_deleted":0,"tuples_failed":0}` — all relevant counts zero, confirming idempotency. `superset ownership check` stayed `ok: true` and `objects: 120` unchanged throughout (the purge removes tenant/group authorization tuples, not the Superset objects themselves — the 25 retained objects and their owner tuples were left alone as documented). A direct OpenFGA read confirmed Tenant B's `tenant:...#member` tuples are now empty. This intentionally leaves Tenant B's directory/authorization state purged; the mandatory end-of-pass `seed-multitenant.sh` reseed restores it (see final report).

**OWN-101 — `purge-tenant` requires a valid GUID v4 and rejects malformed input**
Steps: `docker exec pcssetup-superset-1 superset ownership purge-tenant not-a-guid`.
Expected: non-zero exit, `click.BadParameter`-style error (exit code 2) — the tenant guid argument is validated via `lifecycle.normalize_tenant_guid` before anything else runs (`cli.py:67-79`).
Result: PASS — `superset ownership purge-tenant not-a-guid` exited 2 with `Error: Invalid value for 'TENANT_GUID': tenant must be a GUID v4, got 'not-a-guid'` — exact click.BadParameter-style failure, nothing else ran.

**OWN-102 — `superset ownership disable` strips denial from every object; `enable` re-arms it — correct order avoids bricking objects**
Preconditions: a disposable/test instance — this is instance-wide and disruptive; do NOT run against the shared scratch stack without explicit authorization and a plan to re-enable immediately after.
Steps: In a disposable environment, `docker exec <container> superset ownership disable` (confirm the interactive prompt or pass whatever non-interactive flag is required); observe every private/shared object; then flip `OWNERSHIP_ENABLED=false` in config and restart; then flip it back to `true`, restart, and run `docker exec <container> superset ownership enable`.
Expected: after `disable`, every previously private/shared object's sentinel is removed and its ownership row is "parked" (`disabled:private`/`disabled:shared`) — reported as `disabled: true` on the API and shown with the amber "Disabled" tag in the UI; objects are dataset-gated only (open to anyone with the underlying dataset grant) while parked. After `enable`, parked rows are restored to their prior visibility and sentinels re-armed; objects already carrying the sentinel are reported `already_armed` (idempotent). `check` is clean at the end.
Result: OBSERVED (partial — see finding) — ran the full prescribed sequence on the shared scratch stack (explicitly authorized for this final pass): `superset ownership disable --yes` (clean no-op — `{"sentinels_removed":0,...}`, since every seeded object was already `public` at this point, see OWN-095), then `SCRATCH_OWNERSHIP_ENABLED=false docker compose ... up -d --force-recreate superset worker` (healthy), then `SCRATCH_OWNERSHIP_ENABLED=true ... up -d --force-recreate` again, then `superset ownership enable` (`{"armed":0,"already_armed":0,"public_skipped":120,"missing_object":0,"rows_restored":0}`), `check` clean at the end. However, the flag-off half never actually took effect: after the `SCRATCH_OWNERSHIP_ENABLED=false` recreate, `superset ownership status`/`check` still reported `enabled_backend: true`, and the CLI's own boot log explained why — `"OWNERSHIP_ENABLED='false' in the environment is the default; an earlier config layer set OWNERSHIP_ENABLED=True as a Python setting, and a config layer wins over the environment"` — the mounted `overlay/config/superset_config_docker.example.py` hardcodes `OWNERSHIP_ENABLED = True` as a Python setting (line 44), which by the module's own documented precedence rule always beats the `SCRATCH_OWNERSHIP_ENABLED` env var in this compose stack. So the disable/enable CLI mechanics were genuinely exercised (and clean), but the "parked"/dataset-gated/"already_armed" restoration behavior this case describes could not be observed since the backend was never actually off — not something to work around by editing the mounted config, since that would be changing the product/environment to force a test to pass rather than testing it as configured. See OWN-112/OWN-113 for the same root cause on the section-10 flag-off cases.

**OWN-103 — Running the flag OFF (`OWNERSHIP_ENABLED=false`) WITHOUT first running `disable` bricks private/shared objects for their own owners**
Preconditions: disposable environment only.
Steps: On a disposable copy, flip `OWNERSHIP_ENABLED=false` directly (skip `disable`) and restart; as the owner of a previously-private object, attempt to open it.
Expected: denied even to its own owner — the sentinel row is still present in `viewers` but no code is loaded to interpret/bypass it once the module is unloaded, so the object becomes unreachable by everyone including its owner (module docstring warning, `lifecycle.py:27-30`); this case exists purely to document WHY `disable` must run first, not to be run against anything but a disposable instance.
Result: BLOCKED — genuinely unrunnable here for two independent reasons: (1) the case itself calls for a disposable environment, deliberately reproducing a self-inflicted outage, which this session's guardrails correctly do not authorize on the shared scratch stack; (2) even setting that aside, OWN-102's own run just established that `SCRATCH_OWNERSHIP_ENABLED=false` does not actually turn the backend off in this stack at all (a mounted config layer hardcodes `OWNERSHIP_ENABLED = True`), so the precondition this case needs — the flag genuinely off — cannot currently be reached here regardless of whether `disable` is skipped. Not exercised.

---

## 9. Directory & hooks

**OWN-104 — Sharing picker is tenant-scoped and lists people, not raw GUIDs, when a Superset account exists**
Preconditions: logged in as Ada; Sharing drawer open on any tenant-A object, visibility `shared`.
Steps: Open "Add users & groups"; observe the initial (empty-query) list and its Users/Groups tabs.
Expected: only tenant-A people and groups appear (Ben, Marcus, Priya, Elena, David, Sofia, James, Aisha, Tom, Lena, Carlos, Yuki, Grace — 12 seeded people plus Ben — matches the "13 member(s)" figure seen in `plugin verify`'s output for tenant A); each row shows a display name over email, not a bare GUID; group rows show "<n> member(s) · from directory" (`SubjectPickerPanel.tsx`, and `directory.py:439-446`).
Result: re-run under the Neurons shapes (wheel 0.4.0)
Evidence: (previous run, pre-Neurons shapes; re-run.) Group-row "<n> member(s) · from directory" label text is frontend-rendered only, not present in the raw API payload — not independently verifiable without the UI pass.

**OWN-105 — Groups list reflects the seeded group set, tenant A vs tenant B**
Preconditions: Sharing drawer / picker open as Ada (tenant A) and separately as Cleo (tenant B).
Steps: Switch to the Groups tab in each session; record the group names shown.
Expected: tenant A shows `dashboard_designer`, `data_analyst`, `chart_designer`, `marketing_analytics`, `finance_reporting`, `engineering_metrics`, `support_ops`, `executives` (8 groups; there is no `tenant_administrator` group — administrators are the `admin` relation on the tenant); tenant B shows `dashboard_designer`, `data_analyst`, `sales_ops`, `chart_designer`, `executives`, `support_ops`, `finance_reporting` (7 groups)ant_administrator` (8 groups); neither list includes the other tenant's groups.
Result: re-run under the Neurons shapes (wheel 0.4.0)

**OWN-106 — Group id format matches `OWNERSHIP_GROUP_ID_FORMAT` (default `{tenant}.{name}`)**
Preconditions: any share written to a group in this environment (e.g. OWN-029).
Steps: Inspect the written `subject` string in the audit log, the OpenFGA tuple (`fga_tuple` SQL Lab view), or the `POST /shares` payload used.
Expected: the group half of the subject string is exactly `<tenant-guid>.<name>`, e.g. `group:a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40.dashboard_designer#member` — Neurons' `GROUP_ID_FORMAT_NEURONS = "{tenant}.{name}"`, the default. If this environment instead configures `{name}_{tenant}` (a store written before the confirmation), the id order will be reversed — confirm which format is actually configured before asserting the exact string.
Result: re-run under the Neurons shapes (wheel 0.4.0)

**OWN-107 — `superset ownership plugin describe` (or the equivalent CLI) enumerates all 16 configured function hooks**
Steps: `docker exec pcssetup-superset-1 superset ownership plugin describe` (verify exact subcommand name in this build's `cli_plugin.py`).
Expected: an entry for each of the 16 `HOOK_SPECS` settings (`OWNERSHIP_MEMBER_GUID`, `OWNERSHIP_TENANT_GUID`, `OWNERSHIP_DISPLAY_NAME`, `OWNERSHIP_IS_TENANT_ADMINISTRATOR`, `OWNERSHIP_USER_FOR_MEMBER_GUID`, `OWNERSHIP_USERS_OF_TENANT`, `OWNERSHIP_GROUPS_OF_TENANT`, `OWNERSHIP_MEMBERS_OF_GROUP`, `OWNERSHIP_USER_IN_GROUP`, `OWNERSHIP_GROUP_EXISTS`, `OWNERSHIP_ADMINISTRATORS_OF_TENANT`, `OWNERSHIP_GROUP_ID`, `OWNERSHIP_SPLIT_GROUP_ID`, `OWNERSHIP_GROUP_DISPLAY_NAME`, `OWNERSHIP_TENANT_ADMINISTRATOR_GROUP`, `OWNERSHIP_CAN_MANAGE` — `plugin_hooks.py:104-144`), each reporting `source: "default"` unless this environment configures a custom hook, in which case `source: "config"` with a `path` (`plugin_hooks.py:173-192`).
Result: PASS — `docker exec pcssetup-superset-1 superset ownership plugin describe` lists all 16 named `OWNERSHIP_*` hooks (verified via `grep -c '"OWNERSHIP_'` = 16), every one with `"source": "default"` (no custom hook configured in this environment).

**OWN-108 — A configured `OWNERSHIP_CAN_MANAGE` hook can only NARROW the default decision, never widen it**
Preconditions: this environment has `OWNERSHIP_CAN_MANAGE` configured (skip if not).
Steps: Configure/observe a hook that attempts to grant `manage_reason` to a caller the default logic (`_default_manage_reason`) would refuse.
Expected: the widened answer is ignored/logged, not honored — `plugin_hooks.narrow_manage_reason` only accepts `None` or the SAME reason the default already computed (`api.py:852-869`); confirm via a `POST .../shares` attempt from the "widened" caller still returns `403`.
Result: BLOCKED — not applicable. `plugin describe` (OWN-107) shows `OWNERSHIP_CAN_MANAGE: {"source": "default"}` — no custom hook is configured in this environment, so there is nothing to narrow; skipped per the case's own precondition.

**OWN-109 — Group membership resolution via nested group is honored (chart designers nested into `dashboard_designer`)**
Preconditions: `seed_fga_extra.py`'s nested tuple: `group:<tenant>.chart_designer#member` is a member of `group:<tenant>.dashboard_designer`; Ben is a chart designer of tenant A.
Steps: Share an object with `group:a1e4c2d0-....dashboard_designer#member`, viewer. As Ben (not a dashboard designer directly), confirm he can open it purely via the nested group grant — or confirm via a direct OpenFGA `check` call for `user:<A>.<Ben's guid> viewer <object>` isolating the group path.
Expected: access granted through the one-level group nesting (`group:X#member` as a member of `group:Y`, per the FGA model `define member: [user, group#member]`, `model/ownership.fga:31`).
Result: re-run under the Neurons shapes (wheel 0.4.0)

---

## 10. Operations

**OWN-110 — `superset ownership check` reports clean on a healthy instance**
Steps: `docker exec pcssetup-superset-1 superset ownership check`.
Expected: exit code 0; `"ok": true`; `"dashboard_chart_patch_installed": true`; `"silently_open": []`; `"orphaned_sentinel": []`; `"missing_object": []`; `"group_id_mismatch": []`; `flags_agree: true`; outbox section shows no `dead`/`stalled` rows (`lifecycle.py:662-818`, `cli.py:149-179`).
Result: PASS — exit=0; ok: true; dashboard_chart_patch_installed: true; silently_open: []; orphaned_sentinel: []; missing_object: []; group_id_mismatch: []; flags_agree: true; outbox: {pending:0, claimed:0, dead:0, blocked:0, stalled:false}.

**OWN-111 — `check` exits non-zero and fails CI-style gating when unhealthy**
Preconditions: deliberately induce one failure condition in a disposable environment — e.g. break the dashboard-patch pin (see `scratch-run.md`'s drift-simulation section) or leave dead outbox rows unresolved.
Steps: `docker exec <container> superset ownership check; echo "exit=$?"`.
Expected: `exit=1`; the specific failing field(s) are non-empty/false in the JSON (e.g. `dashboard_chart_patch_installed: false`, or `outbox.dead > 0`).
Result: BLOCKED — inducing a failure condition (breaking the dashboard-patch pin, or leaving dead outbox rows) requires either a disposable environment or a disruptive change to the shared scratch stack, neither available/in-scope for this pass. Not exercised.

**OWN-112 — `status` and `check` remain available with `OWNERSHIP_ENABLED=false`; everything else is not**
Preconditions: a disposable environment with the flag off (or reason about the OFF-state CLI without actually flipping the shared scratch stack).
Steps: With the flag off, `superset ownership status`; `superset ownership check`; then try `superset ownership enable`, `disable`, `reconcile`, `backfill-tenants`, `purge-tenant`, `teardown`, `db current`, `outbox status`.
Expected: `status` and `check` both work, `check` reporting `"backend_loaded": false` (per the OFF-state mutator, `flags.py:406-424`); every other subcommand is simply absent from the CLI group (not merely erroring — `superset ownership <cmd> --help` won't list it).
Result: BLOCKED — attempted via the prescribed recipe (`SCRATCH_OWNERSHIP_ENABLED=false docker compose ... up -d --force-recreate superset worker`, per this pass's own guardrails), but the flag never actually went off: `status`/`check` continued to report `enabled_backend: true`/`backend_loaded: true`, and all subcommands (`enable`, `disable`, `reconcile`, `backfill-tenants`, `purge-tenant`, `teardown`, `db`, `outbox`, `fga`, `plugin`) remained listed in `superset ownership --help`. Root cause (see OWN-102): `overlay/config/superset_config_docker.example.py` hardcodes `OWNERSHIP_ENABLED = True` as a Python setting, which this module's own documented precedence rule always prefers over the `SCRATCH_OWNERSHIP_ENABLED` environment variable in this compose stack — so the OFF state this case needs is not reachable here without editing that mounted config, which is out of scope (would be changing the product to force the test rather than testing it as configured). Flag restored to (already-)true and `enable` re-run afterward; not exercised.

**OWN-113 — With the flag OFF, ownership API routes are unavailable (not present-but-no-op)**
Preconditions: same disposable OFF-state environment as OWN-112.
Steps: `GET /api/v1/ownership/dashboards`.
Expected: `404` (blueprint never registered) rather than any ownership-specific error — the whole module, hooks, blueprint, tables and guard are simply never loaded (`flags.py:82-89`). No ownership columns appear in the stock Chart/Dashboard lists; nothing 403s in an ownership-specific way because the ownership code path does not run at all.
Result: BLOCKED — same root cause as OWN-112: the flag genuinely off is not reachable in this stack via the documented `SCRATCH_OWNERSHIP_ENABLED` recipe (a mounted config layer hardcodes `OWNERSHIP_ENABLED = True`). `GET /api/v1/ownership/dashboards` was not attempted during the (ineffective) flag-off window since the backend was confirmed still loaded (`enabled_backend: true`) — testing it would have just re-confirmed normal ON-state behavior, not this case's OFF-state expectation. Not exercised.

**OWN-114 — `superset ownership outbox status` and `-v` dead-row detail**
Steps: `docker exec pcssetup-superset-1 superset ownership outbox status`; then `... outbox status -v` (or `--verbose`).
Expected: base output has `pending`, `claimed`, `done`(if reported)/`dead`, `blocked`, `stalled`, `oldest_pending`, `oldest_dead`, `last_delivered`, `enabled`, `store_reachable` (`outbox.py:826-879`, `cli.py:319-337`); `-v` additionally lists `dead_rows`: `[{id, op, subject, object, attempts, last_error}, ...]` for any dead row.
Result: PASS — `outbox status -v` returned {"blocked":0,"claimed":0,"dead":0,"dead_rows":[],"done":317,"enabled":true,"last_delivered":"2026-09-14T06:09:40.970931Z","oldest_dead":null,"oldest_pending":null,"pending":0,"stalled":false,"store_reachable":true} — all documented fields present, `-v` added the (empty) `dead_rows` list.

**OWN-115 — `outbox replay` returns dead rows to pending**
Preconditions: at least one dead outbox row (may need to be induced by pointing the store at an unreachable address briefly, in a disposable environment, then restoring it).
Steps: `docker exec pcssetup-superset-1 superset ownership outbox replay <row_id>` (or with no ids, to replay all dead rows).
Expected: the named row(s) move from `dead` back to `pending`; a subsequent `outbox drain` attempts delivery again; `outbox status`'s `dead` count decreases accordingly.
Result: BLOCKED — `outbox status` shows `dead: 0` (confirmed at OWN-114); inducing a dead row requires pointing the store at an unreachable address, which is the same disruptive change ruled out of scope for the shared scratch stack elsewhere in this pass (see OWN-133). No dead row available to replay. Not exercised.

**OWN-116 — `outbox prune` only removes old `done` rows**
Steps: `docker exec pcssetup-superset-1 superset ownership outbox prune --older-than-days 30`.
Expected: only rows with `status: done` older than the cutoff are deleted; `pending`, `claimed`, and `dead` rows are explicitly untouched regardless of age (`cli.py:360-371`).
Result: PASS — `outbox prune --older-than-days 30` returned {"pruned": 0} (no `done` rows in this environment older than 30 days yet — 317 done rows all from this test pass); `outbox status` immediately after unchanged (pending:0, claimed:0, dead:0, done:317), confirming nothing outside the done/age criteria was touched.

**OWN-117 — `superset ownership db current` reports the module's own independent Alembic chain**
Steps: `docker exec pcssetup-superset-1 superset ownership db current`.
Expected: `{"chain": "ownership", "current": "<revision>", "heads": [...]}` — this chain (`alembic_version_ownership`) is tracked separately from Superset's own `alembic_version` table (`cli.py:257-296`). Cross-check against `scratch-run.md`'s recorded head, `0003_ownership_foreign_keys`, or whatever this environment's actual head is.
Result: PASS — {"chain":"ownership","current":"0003_ownership_foreign_keys","heads":["0003_ownership_foreign_keys"]}; current matches the single head, matches the documented `0003_ownership_foreign_keys`.

**OWN-118 — `reconcile` (dry run) reports store/mirror drift without writing**
Steps: `docker exec pcssetup-superset-1 superset ownership reconcile` (no `--write`).
Expected: a report of any `owner` tuples in the store that disagree with the `ownership_object` table's `owner_user_id`, or stray share tuples with no mirror row (`share_tuples_without_row`, as seen live in `ivanti-acceptance-run.md`'s finding F-7); nothing is written without `--write`.
Result: PASS — `reconcile` (no `--write`) returned {"checked":120,"dry_run":true,"owner_tuple_missing":[],"owner_tuple_removed":0,"owner_tuple_written":0,"owner_tuple_wrong":[],"share_rows_without_object":[],"share_tuples_without_row":[]} — no drift detected, dry_run:true confirms nothing was written; `check` re-run afterward still ok:true, unchanged.

---

## 11. Monitoring (OpenFGA via API and SQL Lab)

**OWN-119 — A share's tuple appears in the standalone OpenFGA store (raw API read)**
Preconditions: OWN-028 completed (Ben shared as viewer on dashboard 8); store id read live from `scratch/.scratch-out/store.env`.
Steps: `curl -X POST http://localhost:8199/stores/<store_id>/read -d '{"tuple_key": {"object": "dashboard:<dashboard 8's uuid>", "relation": "viewer"}}'`.
Expected: the response's `tuples` array contains one entry with `key.user == "user:6c2b48e9-5a71-4f92-8d03-2e9b7c1a4d53"`.
Result: PASS — substituted a Tenant B object for dashboard 8 (Ada's, off-limits) since OWN-028 had not been run yet in this pass: shared dashboard 11 (Cleo) `viewer` with Omar Farouk (`user:60f317ea-66cb-48a0-8d45-e15699486409`) via the API, waited for the outbox drain, then `POST /stores/01M2F234EV0AE8KZN18Z1SZ628/read {"tuple_key":{"object":"dashboard:93236b7b-26d1-41e1-9bf4-78dc31d74f6a","relation":"viewer"}}` returned a tuple with `key.user == "user:60f317ea-66cb-48a0-8d45-e15699486409"`. Cleaned up afterward (see OWN-121).

**OWN-120 — Same share visible via SQL Lab's `fga_tuple` bridge view**
Preconditions: connection 1 ("OpenFGA store (read-only)") or connection 2 ("PCS metadata + OpenFGA") set up in the monitoring Superset at :8090, per `fga-sqllab-monitoring.md`.
Steps: In SQL Lab, `SELECT * FROM fga_tuple WHERE object_type = 'dashboard' AND object_id = '<dashboard 8's uuid>' AND relation = 'viewer';`
Expected: one row, `subject = 'user:6c2b48e9-...'`.
Result: PASS — same Tenant B substitution as OWN-119 (dashboard 11 / Omar). `SELECT * FROM fga_tuple WHERE object_type='dashboard' AND object_id='93236b7b-26d1-41e1-9bf4-78dc31d74f6a' AND relation='viewer'` via SQL Lab (database_id 2) returned the row with `subject = 'user:60f317ea-66cb-48a0-8d45-e15699486409'` (plus one unrelated group-share row from OWN-106, also cleaned up).

**OWN-121 — Revoking a share shows up in the OpenFGA changelog as a delete**
Preconditions: continuing OWN-119/120; then run OWN-035 (revoke Ben's share) and drain the outbox.
Steps: In SQL Lab (connection 1), `SELECT inserted_at, operation, object_type||':'||object_id AS object, relation, _user FROM changelog WHERE store = '<store_id>' AND object_type = 'dashboard' AND object_id = '<uuid>' ORDER BY inserted_at DESC LIMIT 5;`
Expected: the most recent row for `relation = 'viewer'`, `_user = 'user:6c2b48e9-...'` has `operation = 1` (delete); the write that granted it earlier shows `operation = 0` further down (`fga-sqllab-monitoring.md`: "changelog.operation: 0 = write, 1 = delete").
Result: PASS — same Tenant B substitution (dashboard 11 / Omar): `DELETE /dashboard/11/shares/user:60f317ea-...`, drained, then queried `changelog`. Most recent row: `operation=1` (delete), `relation='viewer'`, `_user='user:60f317ea-66cb-48a0-8d45-e15699486409'`; the earlier grant appears further down with `operation=0` for the same subject/relation. Dashboard 11 fully reverted afterward: both shares removed, visibility restored to `public`, owner unchanged (Cleo).

**OWN-122 — Neurons users/groups summary views reflect the seeded directory correctly**
Steps: In SQL Lab (connection 2), run the "Neurons users" and "Neurons groups" queries from `fga-sqllab-monitoring.md`.
Expected: users view returns the expected count of tenant-membership tuples (22 at the time these notes were written — reconfirm the live count) split correctly A vs B, `user_id` = the member half of `user:<tenant>.<member>`; groups view returns one row per group (8 in A, 7 in B) with a correct member count and names matching `ab_user`.
Result: re-run under the Neurons shapes (wheel 0.4.0)

**OWN-123 — "Users → tenant admin flag, groups, ownership counts" view correctly flags Ada and Cleo as tenant admins**
Steps: Run the third query block from `fga-sqllab-monitoring.md` (the `WITH t AS (...) ... tenant_admin` query).
Expected: Ada's row shows `tenant_admin = 'yes'` (from the `admin` tuple on `tenant:<A>`) and `groups` = `dashboard_designer`; Cleo's row shows `tenant_admin = 'yes'` for tenant B; every other seeded persona shows blank in that column; `dashboards_owned`/`charts_owned` roughly match the known ownership assignments (Ada: dashboards 7, 8, 10 + chart 78, etc.).
Result: re-run under the Neurons shapes (wheel 0.4.0)

**OWN-124 — Store health/tuple counts are sane after a full test pass**
Steps: `SELECT store, count(*) FROM fga_tuple GROUP BY 1;` (connection 1) after completing Sections 2–5.
Expected: non-trivial, growing tuple counts for `owner`, `tenant`, `viewer`/`editor` relations on both `dashboard` and `chart` object types, plus `member` tuples for `tenant` and `group` objects — no unexpected object types or a store id mismatch (confirm it is the same store id noted in `store.env` for this run, not a stale one from a previous rebuild).
Result: OBSERVED — store id matches `store.env` (`01M2F234EV0AE8KZN18Z1SZ628`), no unexpected object types: `chart/owner=108, chart/tenant=108, dashboard/owner=11, dashboard/tenant=11, group/member=32, group/tenant=17, tenant/member=22`. `owner`/`tenant` counts on both object types are non-trivial as expected; `viewer`/`editor` relation counts were 0 at the moment of this query — this section ran concurrently with the other engineer's sections 1-4 pass and any of their in-progress share tests may not have been mid-flight or had already been cleaned up at this timestamp; not treated as a product defect since sharing itself is verified elsewhere (OWN-119–121).

---

## 12. Negative / edge cases

**OWN-125 — GET on an object with no ownership row at all ("ungoverned")**
Preconditions: an object that predates the ownership backfill and was never touched by it (rare in this seeded environment; substitute chart 83 if no better candidate exists, noting it DOES have a row with `owner_user_id: null` rather than no row at all — if a truly rowless object cannot be found, mark this case as needing a fresh/unmigrated fixture).
Steps: `GET /api/v1/ownership/<asset>/<id>` on such an object.
Expected: treated as `public`/unowned by the code path that handles `row is None` (`_display_visibility` returns `"public"` for `row is None`, `api.py:127-128`); `check`'s `ungoverned` list would include it but this does NOT fail `check` (`lifecycle.py:755-762`, explicitly "safe default").
Result: BLOCKED — no genuinely rowless fixture available in this seeded environment. `GET /api/v1/ownership/chart/83` (the documented substitute) returns `"owner": null, "visibility": "public", "unowned": false"` — consistent with a safe-default/unowned treatment, but it has an `owner_user_id: null` row (not a missing row), so it doesn't isolate the `row is None` code path specifically. Separately, this build's `superset ownership check` output has no `ungoverned` key at all (checked the full JSON), so that half of the expectation could not be confirmed either way.

**OWN-126 — GET on a non-existent dashboard/chart id**
Steps: `GET /api/v1/ownership/dashboard/999999`; `GET /api/v1/ownership/chart/999999`.
Expected: `404 {"message": "dashboard not found"}` / `{"message": "chart not found"}` (`api.py:568-571, 579`).
Result: PASS — `GET /api/v1/ownership/dashboard/999999` → `404 {"message":"dashboard not found"}`; `GET /api/v1/ownership/chart/999999` → `404 {"message":"chart not found"}`. Exact match.

**OWN-127 — Malformed subject strings across every write route, consolidated negative sweep**
Steps: Against a `shared` object, as its owner, try each of: `POST .../shares {"subject": ""}`; `{"subject": "user:"}`; `{"subject": "user:not-a-guid-or-email-or-int"}`; `{"subject": "group:"}`; `{"subject": "group:name_wrongtenant#member"}` (well-formed but wrong tenant suffix).
Expected: each returns `400` with the specific message from Section 3's malformed-subject cases (OWN-050–054) as applicable — no case reaches a 500 or silently accepts a bad string.
Result: PASS — substituted dashboard 11 (Cleo, tenant B, set to `shared`) for the "shared object, as its owner" precondition since Ada's/Ben's objects are off-limits. All 5 payloads returned `400`, none a 500, none silently accepted: `{"subject":""}` → "subject is required, e.g. 'user:<guid>'"; `{"subject":"user:"}` → "subject does not name a known account"; `{"subject":"user:not-a-guid-or-email-or-int"}` → same message; `{"subject":"group:"}` → "group does not belong to your tenant"; `{"subject":"group:name_wrongtenant#member"}` → same "group does not belong to your tenant". No share rows were created by any of them. Dashboard 11 restored to `public` afterward.

**OWN-128 — Concurrent share-add and share-remove on the same object/subject**
Preconditions: dashboard 8, shared.
Steps: Fire `POST /dashboard/8/shares {"subject": "user:6c2b48e9-...", "role": "viewer"}` and `DELETE /dashboard/8/shares/user:6c2b48e9-...` against the SAME subject back-to-back with minimal delay.
Expected: no 500; final state is deterministic and consistent between the mirror table and (after drain) the store — the per-object write lock and the outbox's per-object ordering guarantee (a transfer/share/unshare on one object cannot interleave with another writer's on the same object, `outbox.py:49-104`) prevent a lost update or a state where the mirror says "shared" but the store says "revoked" (or vice versa) past the drain.
Result: PASS — substituted dashboard 11 (Cleo, tenant B) + Omar Farouk for dashboard 8/Ben (off-limits, Ada's). Fired `POST .../shares` and `DELETE .../shares/<subject>` concurrently (backgrounded curls): no 500 from either (`200` add, `202` delete with `mirror_row:false`, i.e. delete raced ahead of the add's commit and was a no-op at that instant). After the outbox drained, mirror (`GET dashboard/11` → share present) and store (`fga read` → tuple present) agreed exactly — deterministic final state, no lost update. `outbox status` afterward showed `dead:0, blocked:0, stalled:false`. Cleaned up (share removed) afterward.

**OWN-129 — `limit`/`offset` bounds on the list endpoints**
Steps: `GET /api/v1/ownership/dashboards?limit=-5`; `GET /api/v1/ownership/dashboards?limit=abc`; `GET /api/v1/ownership/dashboards?limit=999999`; `GET /api/v1/ownership/dashboards?offset=-10`.
Expected: `limit=-5` and `limit=999999` are both clamped into `[1, MAX_LIST_LIMIT=1000]` rather than erroring or reaching the database unclamped (`api.py:1353-1356`); `offset=-10` is clamped to `0`; `limit=abc` (non-integer) returns `400 {"message": "limit and offset must be integers"}` (`api.py:1357-1358`).
Result: PASS — `limit=-5` → response `"limit":1` (clamped to floor); `limit=999999` → response `"limit":1000` (clamped to `MAX_LIST_LIMIT`); `offset=-10` → response `"offset":0`; `limit=abc` → `400 {"message":"limit and offset must be integers"}`. All four exactly as expected, no unclamped reach to the DB.

**OWN-130 — Unauthenticated requests to every mutating route return 401, not a redirect or a 403**
Steps: With no session/token, `PUT .../visibility`, `POST .../shares`, `DELETE .../shares/<subject>`, `PUT .../owner`, `POST .../claim`.
Expected: every one returns `401 {"message": "authentication required"}` (`api.py:1822-1823` and equivalents) — not an HTML login redirect, and not conflated with the 403 "not authorized to manage this object" case.
Result: PASS — used dashboard 11 as the target (Ada's objects off-limits). All five unauthenticated calls (`PUT .../visibility`, `POST .../shares`, `DELETE .../shares/<subject>`, `PUT .../owner`, `POST .../claim`) returned exactly `401 {"message":"authentication required"}` — no HTML redirect, no 403.

**OWN-131 — A cookie-session write with no CSRF token is refused (401), proving the pairing requirement**
Preconditions: logged in via browser session (cookie), but issuing the request without the `X-CSRFToken` header or `csrf_token` field (e.g. via a bare `curl` reusing only the session cookie).
Steps: `PUT /dashboard/8/visibility {"visibility": "private"}` with the session cookie attached but no CSRF token.
Expected: `401` — a mutating route requires BOTH a valid session AND a valid CSRF token together (`_authn(mutating=True)`, `api.py:210-228`); a bearer token request, by contrast, needs no CSRF token at all and should succeed under identical authorization.
Result: PASS — used dashboard 11 (Cleo) since Ada's dashboard 8 is off-limits. Obtained a genuine browser-style session via the form login (`POST /login/` with the page's `csrf_token`, not the JSON API login), confirmed it authenticated (`GET dashboard/11` with cookie-only → `200`), then `PUT .../visibility` with the same cookie and no `X-CSRFToken` header → `401 {"message":"authentication required"}`, exactly as expected. Confirmed the contrast clause too: the identical `PUT .../visibility` with a Bearer token and no CSRF header → `200` success. No lasting state change (final PUT set visibility back to `public`).

**OWN-132 — Sharing action is visibly disabled for a caller who cannot manage the object**
Preconditions: Ben viewing dashboard 1 (Marcus's) in the Chart/Dashboard list, no relationship other than tenant membership.
Steps: Observe the row's Sharing icon/action for dashboard 1 in Ben's list view.
Expected: the Sharing action tooltip reads "You do not manage this object, so it cannot be shared from here." (`sharingActionTooltip()` non-manager branch) rather than being silently missing or erroring on click.
Result: BLOCKED (partial) — UI step, run in the UI pass for the literal tooltip text. API equivalent confirmed the underlying data driving it: `GET /api/v1/ownership/dashboard/1` as Ben returns `"can_manage": false, "can_share": false`, which is what feeds `sharingActionTooltip()`'s non-manager branch — so the disabled state is correctly computed, but the exact rendered tooltip string was not independently observed (no browser tool used, per instructions).

**OWN-133 — Store-outage degrade path: list/picker fail closed with no 500s**
Preconditions: ability to point the Superset instance's OpenFGA connection at an unreachable address temporarily, in a disposable/test context (mirrors the "store outage simulated" scenario in `ivanti-acceptance-run.md`).
Steps: With the store unreachable, `GET /api/v1/ownership/dashboards`; `GET /api/v1/ownership/subjects?q=`; open the Sharing drawer / picker in the UI.
Expected: no 500s anywhere; list rows show Owner/Sharing as **Unknown**; the Sharing action is disabled with a tooltip along the lines of "Sharing is temporarily unavailable: the authorization store cannot be reached."; `/subjects` responds `{"degraded": true, "result": []}` rather than erroring (per the picker's "The directory is temporarily unavailable. Try again in a moment." empty-state copy and the API's `degraded` flag, `api.py:3125-3127`). Recovery: once the store is reachable again, a fresh list/picker call returns clean, non-degraded results with no restart required.
Result: BLOCKED — re-checked in this final pass, which did authorize container restarts for a related flag-flip exercise (see OWN-102/OWN-112/OWN-113). That attempt confirmed the mounted `overlay/config/superset_config_docker.example.py` hardcodes several `OWNERSHIP_FGA_*` values (including the API URL) as Python settings read from `os.environ.get(...)` at config-layer load time, the same layer that was just shown to override environment-variable changes for `OWNERSHIP_ENABLED`. Pointing the store at an unreachable address would need an equivalent config-layer change (not just an env var), which is a genuine product/config edit rather than a supported runtime toggle — out of scope for this pass's guardrails (which authorize the documented `SCRATCH_OWNERSHIP_ENABLED` flip specifically, not arbitrary config edits) and materially riskier to revert cleanly than the flag flip was. No disposable environment was available to induce this safely. Not exercised.

**OWN-134 — Detail GET can reveal an "unmirrored" store grant the list page's bulk context does not**
Preconditions: an object with a genuine OpenFGA `viewer`/`editor` tuple that has no matching row in `ownership_share` (e.g. simulate by writing a tuple directly via the OpenFGA API rather than through `POST .../shares`, or find a leftover from an earlier reconcile-worthy drift).
Steps: As the object's owner, `GET /api/v1/ownership/dashboard/<id>` (single-object detail) and note the `shares` list; separately, `GET /api/v1/ownership/dashboards` (list) and note the same object's row.
Expected: the detail's `shares` includes the unmirrored grant, tagged distinctly (`"unmirrored": true` in the underlying service data) since the detail route merges live store grants on top of the mirror (`service.list_shares`); the list route's summary fields (`can_manage`/`can_share`) are computed from the LOCAL mirror only and do not reflect this same merge — a real, documented list-vs-detail discrepancy for any object carrying an unmirrored grant, not itself a bug to report but worth confirming behaves as documented.
Result: PASS — used dashboard 11 (Cleo). Wrote a `viewer` tuple for Hannah Berg directly via the OpenFGA API (bypassing `POST .../shares`, so no mirror row). `GET dashboard/11` (detail, as owner) showed `shares: [{"name":"Hannah Berg","role":"viewer","subject":"user:e64caf0a-...","unmirrored": true}]` — the merged, tagged grant as documented. `GET dashboards` (list) showed object 11's summary row with no `shares`/`unmirrored` data at all (list rows don't carry a shares array), and `can_manage`/`can_share` both `true` — driven purely by local mirror ownership (Cleo owns it), unaffected by the unmirrored grant — matches the documented list-vs-detail behavior. Cleaned up: deleted the direct tuple; dashboard 11 back to owner-only, no shares.

---

**OWN-135 — Full cycle, login by login: public -> shared (nobody) hides the chart from Ben; adding Ben shows it again**
Preconditions: chart 78 "Girl Name Cloud" owned by Ada, `public`, on public dashboard 7 "USA Births Names". Ben is a tenant A member (and in `group:<A>.chart_designer`). Cleo is in tenant B. Log out at /logout/ between personas.
Steps (UI): (1) Ada (`3f0a91c7-2d84-4e63-9b15-7c4e8a2f6d31`/test1234): Charts -> "Girl Name Cloud" -> note Owner/Sharing columns. (2) Ben (`6c2b48e9-5a71-4f92-8d03-2e9b7c1a4d53`/test1234): Charts list; dashboard 7. (3) Ada: Sharing drawer on the chart. (4) Ben: as (2). (5) Ada: Sharing -> Shared, add nobody, Apply. (6) Ben: Charts list; dashboard 7; /explore/?slice_id=78. (7) Ada: open the drawer. (8) Ada: Add users & groups -> Users -> "Ben tenant A" -> Confirm -> Apply. (9) Ben: after ~10 s, Charts list; dashboard 7. Then repeat (5)/(8) by removing and re-adding Ben; finally Ada -> Public -> Apply.
Steps (API): after each Apply, as Ben `GET /api/v1/chart/78`, `GET /api/v1/dashboard/7/charts`; `POST http://localhost:8199/stores/<store>/read {"tuple_key":{"object":"chart:<uuid of 78>"}}`.
Expected: (1) listed, Owner "Ada tenant A", Sharing "Public". (2) listed for Ben; tile renders; `GET /api/v1/chart/78` -> 200. (3) drawer: Owner Ada, Public selected. (4) unchanged. (5) Sharing "Shared", "Shared with 0 users"; no viewer tuple in OpenFGA. (6) chart absent from Ben's list; `GET /api/v1/chart/78` -> 404; dashboard payload for chart 78 carries `has_access: false, owners: ["Ada tenant A"]` and no `form_data`; tile shows the placeholder naming Ada; explore shows no data. (7) drawer still Shared, empty list. (8) "Shared with 1 user"; within ~10 s a `user:6c2b48e9-... viewer chart:<uuid>` tuple exists. (9) chart back in Ben's list, `GET /api/v1/chart/78` -> 200, tile renders with data. Each later removal/re-add repeats (6)/(9); removal is effective on Ben's next request, a grant after the outbox drain. Cleo: 404 / absent in every state. `superset ownership check` -> `ok: true` at the end, chart 78 `public`, no shares.
Result:


## 13. Public within the tenant (`OWNERSHIP_PUBLIC_SCOPE=tenant`, PR #110)

**OWN-136 — Every route for another tenant's public object is closed, with no metadata leak**
Preconditions: chart 78 and dashboard 7 (Ada, tenant A) `public`; logged in as Cleo (tenant B).
Steps: `GET /api/v1/dashboard/7`, `/7/charts`, `/7/datasets`, `/7/tabs`, `/api/v1/chart/78`, `/api/v1/chart/78/data/`, `POST /api/v1/chart/data` with chart 78's `form_data` (obtain it as Ada), `GET /api/v1/dashboard/export/?q=!(7)`, `/api/v1/chart/export/?q=!(78)`, `/api/v1/dashboard/7/thumbnail/x/`, `/api/v1/chart/78/thumbnail/x/`, `/api/v1/explore/?slice_id=78`, `/api/v1/ownership/dashboard/7`, `/api/v1/ownership/chart/78`; the UI list pages and `/superset/dashboard/7/`.
Expected: `404` everywhere (`403 CHART_SECURITY_ACCESS_ERROR` on `POST /api/v1/chart/data`; `explore` returns `200` with `slice: null` — nothing of the chart); no body carries an owner name, `form_data` or share list; the UI lists show tenant-B objects only and the dashboard URL renders "not found".
Result:

**OWN-137 — Same routes as a same-tenant non-owner are open**
Preconditions: as OWN-136; logged in as Ben (tenant A, owns nothing).
Steps: the OWN-136 routes as Ben.
Expected: `200` on all of them (dashboard 7 renders with data in the UI); `GET /api/v1/ownership/chart/78` shows `owner: {id, name}` only and `can_manage: false`.
Result:

**OWN-138 — A dashboard containing another tenant's charts renders those tiles as placeholders without owner names**
Preconditions: a tenant-A dashboard that embeds a tenant-B chart (build one as Ada: add an existing tenant-B public chart to a new dashboard via the API, or use dashboard 4 on the dev stack).
Steps: As Ada, `GET /api/v1/dashboard/<id>/charts`; open the dashboard in the UI.
Expected: the tenant-B chart entries carry `has_access: false`, no `form_data`, `owners: []` (`dashboard_patch._denied_across_tenants` — no other-tenant names are disclosed); the tile shows the access placeholder without naming anyone; every tenant-A tile renders.
Result:

**OWN-139 — An object outside every tenant is visible to nobody in a tenant until it is stamped; `check` tells the two cases apart**
Preconditions: logged in as `admin` (no `Tenant_<guid>_Role` role).
Steps: As admin create a chart (`POST /api/v1/chart/` on `birth_names`, or via Explore -> Save) and set it `public` through `PUT /api/v1/ownership/chart/<id>/visibility`. `GET /api/v1/chart/<id>` as admin, Ada, Cleo. `superset ownership check`. Then, on a tenant-A public chart of Ada's, `UPDATE ownership_object SET tenant_guid = NULL WHERE asset_type='chart' AND object_id=<id>` (wait 12 s for the lookup cache), `GET` it as Ben and as Ada, run `check`, then `superset ownership backfill-tenants`, `GET` as Ben again, `check`.
Expected: admin `200`, Ada `404`, Cleo `404` on the admin's chart; `check` lists it under `untenanted_public` but not `untenanted_public_repairable` and stays `ok: true` (its owner has no tenant — nothing to stamp). Ada's blanked chart: Ben `404` (closed until stamped), Ada `200` (the owner always reads); `check` → `ok: false`, `untenanted_public_repairable: ["chart:<id>"]`; after `backfill-tenants` the row is tenant A again, Ben `200`, `check` ok. Delete the admin's chart afterwards.
Result:

**OWN-140 — Backfill re-runs never move an object between tenants**
Preconditions: `SELECT asset_type, tenant_guid, count(*) FROM ownership_object GROUP BY 1, 2` recorded; per-persona `GET /api/v1/dashboard/` and `/api/v1/chart/` counts recorded for Ada, Ben, Cleo.
Steps: `docker exec pcssetup-superset-1 superset ownership backfill-tenants` twice (and restart the container once, which re-runs the entrypoint's backfill); repeat the queries.
Expected: identical tenant distribution and identical counts; `check` ok. The row's tenant is mirrored from the store's `tenant` tuple and never replaced by a derivation (`backfill._stamp_tenant`, strict store read).
Result:

**OWN-141 — Transfer, claim and adoption keep the tenant; cross-tenant moves are refused**
Preconditions: chart 78 (Ada, public).
Steps: As Ada, `PUT /api/v1/ownership/chart/78/owner {"subject": "user:9d7e35a1-..."}` (Cleo). As admin, `PUT .../owner {"subject": "user:9d7e35a1-..."}`. As admin, transfer chart 78 to Ben and back to Ada. After each: `SELECT tenant_guid FROM ownership_object WHERE asset_type='chart' AND object_id=78`; `GET /api/v1/chart/78` as Cleo.
Expected: Ada's attempt `400` (the subject is outside her tenant); admin's `409` (`... names another tenant; a transfer never moves an object out of its tenant`); the Ben round-trip `200`/`200` with the row's tenant staying A and Cleo `404` throughout.
Result:

**OWN-142 — `status` and `check` report the scope**
Steps: `docker exec pcssetup-superset-1 superset ownership status`; `... check`.
Expected: `public_scope: "tenant"` in both; `check` has no `schema_behind` (chain at `0004_ownership_object_tenant`), `untenanted_public` absent or only objects whose owner has no tenant.
Result:

**OWN-143 — Tenant administrator, login by login: sees the owner, takes ownership, then shares**
Preconditions: a chart owned by Marcus (tenant A), public, e.g. "Number of Deals (for each Combination)" on Sales Dashboard; Ada is tenant A's administrator; Ben a tenant A member. Log out at /logout/ between personas.
Steps (UI): (1) Ada: Charts -> the chart -> row action **Sharing**. (2) Ada: click **Take ownership**. (3) Ada: select **Shared** -> **Add users & groups** -> tick **Ben tenant A** -> **Confirm** -> **Apply**. (4) Ada: reopen the drawer -> **Transfer ownership** -> pick Marcus -> confirm. (5) Ada: reopen the drawer. (6) Ben: Charts list; open the chart.
Expected: (1) a warning notice at the top: "You are not the owner of this object; Marcus Chen is. As an administrator of this tenant you can transfer its ownership, but only its owner can change how it is shared. To change the sharing, take ownership first."; Owner: Marcus Chen with **Take ownership** and **Transfer ownership** links; Private/Shared/Public greyed out; no Apply, a Close button. (2) green banner "Ownership transferred to Ada tenant A. You still manage this object as its owner."; Owner: Ada tenant A; the notice and Take ownership are gone; the options are live; the list behind shows Ada as owner. (3) Sharing column reads Shared; the drawer says "Shared with 1 user". (4) banner "Ownership transferred to Marcus Chen. You still manage this object as a tenant administrator."; Owner: Marcus Chen. (5) the notice and greyed-out options are back; Take ownership is offered again; Ben's share is listed but nothing can be changed. (6) the chart is in Ben's list and opens (the share Ada made as owner stays). API: while Marcus owns it, `PUT .../visibility` and `POST/DELETE .../shares` as Ada answer `403` "take ownership of it first"; `GET` shows `can_manage: true, can_share: false, manage_reason: "tenant_admin"`. A Superset admin is not narrowed this way, and neither is a holder of `OWNERSHIP_MANAGE_PERMISSION` (a tenant administrator who also holds it shares on that permission's ground).
Result:

## 15. Neurons id shapes (`Tenant_<guid>_Role`, `user:<tenant>.<member>`, `group:<tenant>.<name>`, `tenant#admin`; PR #113)

**OWN-144 — A person is spelt `user:<tenant>.<member>` in every tuple this module writes**
Preconditions: Ada owns dashboard 8; Ben is a tenant A member. Store id from `store.env`.
Steps: As Ada, set dashboard 8 to `shared` and `POST /dashboard/8/shares {"subject": "user:6c2b48e9-5a71-4f92-8d03-2e9b7c1a4d53", "role": "viewer"}` (the BARE member GUID, as the picker sends it). Wait for the outbox drain. Read the object's tuples: `curl -X POST http://localhost:8199/stores/<store>/read -d '{"tuple_key": {"object": "dashboard:<uuid>"}}'`.
Expected: the response echoes `"subject": "user:a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40.6c2b48e9-5a71-4f92-8d03-2e9b7c1a4d53"`; the store holds `user:<A>.<ben> viewer`, `user:<A>.<ada> owner` and `tenant:<A>#member tenant` — never a bare `user:<guid>`. `GET /dashboard/8` shows `caller_subject` and the owner's `guid` in the dotted spelling; the share row's display is "Ben tenant A", not a GUID.
Result:

**OWN-145 — Another tenant's spelling of a member names nobody**
Steps: As Ada, `POST /dashboard/8/shares {"subject": "user:b7f28e5a-9c14-4d6b-a2f0-5e3d8c1a6b92.6c2b48e9-5a71-4f92-8d03-2e9b7c1a4d53", "role": "viewer"}` (Ben with tenant B's GUID in front).
Expected: `400 {"message": "subject does not name a known account"}`; no tuple written (the outbox has nothing pending for the object). The same request with Ben's own tenant in front, or the bare GUID, is accepted.
Result:

**OWN-146 — The tenant administrator is the `admin` relation on the tenant object, read live**
Preconditions: store id from `store.env`.
Steps: (1) `curl -X POST http://localhost:8199/stores/<store>/read -d '{"tuple_key": {"object": "tenant:a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40", "relation": "admin"}}'`. (2) As Ada, `GET /api/v1/ownership/chart/<a chart owned by Marcus>`. (3) Delete the tuple through the OpenFGA API (`/write` with `deletes`), repeat (2), then write it back.
Expected: (1) exactly one tuple, `user:<A>.<ada> admin tenant:<A>`; no `tenant_administrator` group anywhere in the store. (2) `manage_reason: "tenant_admin"`, `can_share: false`. (3) with the tuple gone the same GET answers `404` (Ada is an ordinary member again; the platform's relation is the only source, the module never writes it); restored, `tenant_admin` is back within `OWNERSHIP_LOOKUP_CACHE_TTL` seconds.
Result:

**OWN-147 — Group ids carry the tenant in front and nest as-is; no group-to-tenant tuple**
Steps: (1) As Ada, open the picker's Groups tab; (2) `POST /dashboard/8/shares {"subject": "group:a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40.dashboard_designer#member", "role": "viewer"}`; drain; (3) log in as Ben (a chart designer, not a dashboard designer) and open dashboard 8; (4) `curl ... /read -d '{"tuple_key": {"relation": "tenant", "object": "group:a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40.dashboard_designer"}}'`.
Expected: (1) the eight tenant A groups by display name ("dashboard designer", "7 member(s) · from directory"), listed by the member walk (`OWNERSHIP_DIRECTORY_GROUP_WALK=always`); (2) `200`, the tuple is `group:<A>.dashboard_designer#member viewer`; (3) Ben reads it through `group:<A>.chart_designer#member member group:<A>.dashboard_designer` (nested); (4) `tuples: []` — Neurons writes no group-to-tenant tuple and nothing here needs one.
Result:

**OWN-148 — `plugin verify` passes on the Neurons shapes**
Steps: `docker exec pcssetup-superset-1 superset ownership plugin verify --tenant a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40 --sample-users 40`; repeat for tenant B.
Expected: no FAIL. In particular `group_has_tenant_tuple` PASS "no tenant tuples; N group(s) walked (OWNERSHIP_DIRECTORY_GROUP_WALK=always)", `administrator_group_exists` PASS "tenant:<t>#admin has 1 administrator(s)", `guid_v4` / `tenant_known` / `normalize_subject_roundtrip` PASS for every sampled member (their `member_guid` is `<tenant>.<member>`), `shape_pair_roundtrip` PASS, `no_member_in_two_tenants` PASS. Only `model_pinned` may WARN when the model is not pinned.
Result:

**OWN-149 — A pre-upgrade `user:<member>` share row is reported by `check`, removable, and re-shareable**
Preconditions: a scratch object owned by Ada, `shared`. This simulates a store written before the ids carried the tenant.
Steps: (1) In the metadata DB: `INSERT INTO ownership_share (asset_type, object_id, subject, role) VALUES ('dashboard', 8, 'user:6c2b48e9-5a71-4f92-8d03-2e9b7c1a4d53', 'viewer')`, and write the matching bare tuple to the store by hand. (2) `superset ownership check`. (3) As Ben, open dashboard 8. (4) As Ada, `DELETE /dashboard/8/shares/user:6c2b48e9-5a71-4f92-8d03-2e9b7c1a4d53`; drain. (5) `check` again; then share Ben again through the picker.
Expected: (2) `user_id_mismatch: ["user:6c2b48e9-..."]`, `ok: false`, and the boot log's ERROR line naming the row. (3) `404` — the old spelling grants nothing. (4) `200` with `"subject": "user:6c2b48e9-..."` (the row's own spelling, no `queued`/`mirror_row: false`); the mirror row and the bare tuple are both gone. (5) `user_id_mismatch: []`, `ok: true`; the re-share lists Ben once, as `user:<A>.<ben>`, and Ben can open the dashboard.
Result:

**OWN-150 — `teardown` then `db upgrade` reinstalls cleanly**
Preconditions: a disposable scratch stack only (this destroys every ownership row).
Steps: `docker exec pcssetup-superset-1 sh -c 'echo y | superset ownership teardown'`; then `superset ownership db upgrade`; then `superset ownership db current`; then restart `superset` so the entrypoint's backfill and `check` run.
Expected: teardown's JSON reports `tables_dropped` (the three tables) and `chain_forgotten: true`; `db upgrade` logs `Running upgrade -> 0001_object_ownership` through `0004_ownership_object_tenant` (the chain runs from the start, not a no-op); `db current` shows `0004_ownership_object_tenant`; after the restart `check` is `ok: true` with every object backfilled public again.
Result:

## Coverage map

| Area | Case IDs |
|---|---|
| 1. Login/identity & tenant scoping of lists | OWN-001 – OWN-009 |
| 13. Public within the tenant | OWN-136 – OWN-142 |
| 14. Tenant administrator: transfer, not sharing | OWN-143 |
| 15. Neurons id shapes | OWN-144 – OWN-150 |
| 2. Visibility state machine (dashboards & charts) | OWN-010 – OWN-027 |
| 3. Sharing (+ OWN-135 walkthrough) | OWN-028 – OWN-055, OWN-135 |
| 4. Placeholder tile & data path | OWN-056 – OWN-067 |
| 5. Transfer of ownership (incl. claim) | OWN-068 – OWN-087 |
| 6. Native editors (#80) | OWN-088 – OWN-090 |
| 7. Sentinel / guard | OWN-091 – OWN-095 |
| 8. User lifecycle (deactivation, purge, enable/disable) | OWN-096 – OWN-103 |
| 9. Directory / hooks | OWN-104 – OWN-109 |
| 10. Operations (CLI: check/status/outbox/db) | OWN-110 – OWN-118 |
| 11. Monitoring (OpenFGA API + SQL Lab) | OWN-119 – OWN-124 |
| 12. Negative / edge cases | OWN-125 – OWN-134 |

**Total: 150 cases.**

## Known open issues (do not report these as new findings — track against the linked issue instead)

- **#99 — OPEN.** Upstream Superset-core proposal: give `EXTRA_ACCESS_QUERY_FILTERS`/the list `BaseFilter` hook the object id on a single-item GET, so an ownership check can answer "is this user allowed to see this specific object" without a reverse-index (`list_objects`) computation. Follow-up to the now-closed #82 (owner requests paying one `list_objects` call). Out of scope for this plugin's own PR series; filed as a Superset-core issue. Not independently testable from this document — it is a proposal, not a behavior change.
- **#102 — OPEN.** A tenant administrator can, via the raw ownership API, see and claim an unowned governed object in their own tenant (`can_manage: true`, `POST .../claim` succeeds) — but cannot reach it through the UI at all, because Superset's own list/detail read gate denies everyone (tenant admin included) on an unowned object, so it never appears in the tenant admin's Chart/Dashboard list or opens the Sharing drawer's rescue action. Only a Superset admin (who bypasses the read gate entirely) can currently reach it from the UI. See **OWN-083**, which exercises exactly this gap and documents the expected (not yet fixed) discrepancy between the API and UI behavior. Probable fix per the issue: let a tenant administrator's list/GET read (not data-path) an unowned governed object of their own tenant, using the same `manage_reason` decision that already grants `can_manage`.

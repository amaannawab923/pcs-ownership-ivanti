<!--
Licensed to the Apache Software Foundation (ASF) under one
or more contributor license agreements.  See the NOTICE file
distributed with this work for additional information
regarding copyright ownership.  The ASF licenses this file
to you under the Apache License, Version 2.0 (the
"License"); you may not use this file except in compliance
with the License.  You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing,
software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
KIND, either express or implied.  See the License for the
specific language governing permissions and limitations
under the License.
-->

# Updating `superset_ownership`

Breaking and behaviour-relevant changes to this package, most recent first.
There is no precedent file at this path or elsewhere in the tree for this
module's own changes (the repository-wide `UPDATING.md` at the repo root
covers Superset core only); this file is the module-local equivalent,
scoped to `docker/pythonpath_dev/superset_ownership/`.

## A tenant administrator reads every object of their tenant (PCS-10243)

A tenant administrator now lists, opens and loads the data of every governed object of their own tenant -- private, shared, public or unowned -- decided from the ownership row's mirrored tenant (revision 0004) and one administrator check per user per request (cached for `OWNERSHIP_LOOKUP_CACHE_TTL` seconds, the same cache `OWNERSHIP_IS_TENANT_ADMINISTRATOR` uses), with no store call on the object. Before, an object not shared with the administrator was hidden from them -- and an object hidden is one they cannot transfer, take or claim: the row to act on was not there (issue #102 for the unowned case). Private now means "the owner, whoever it is shared with, and the tenant's administrators"; nothing about sharing changes: only the owner (or a holder of the manage-sharing permission) changes who else sees it, and the administrator's drawer still offers transfer and take-ownership only. Another tenant's administrator sees nothing, as before. A row with no tenant mirrored (created before revision 0004, or not yet stamped) is nobody's tenant: no administrator reads it on this ground, so until `backfill-tenants` has run the administrator sees only what they saw before; `check` lists the public ones (`untenanted_public`). The administrator check is cached per user: a newly granted administrator reads on their next request, a revoked one for at most `OWNERSHIP_LOOKUP_CACHE_TTL` seconds more. The administrator's list includes their tenant's draft dashboards (stock hides drafts from everyone but their owners; an administrator re-homes a draft too). Requires revision 0004 (`superset ownership db upgrade`) and stamped rows (`backfill-tenants`).

## The id shapes are Neurons' (PCS-10243, confirmed by Ivanti)

Ivanti confirmed how Neurons provisions users and writes to OpenFGA; the module's defaults now match, so pointing it at their store needs no hook for any of the three.

- **Tenant role: `Tenant_<guid>_Role`.** The default identity reads the tenant GUID from a role of that shape (the token's `tid` claim); the bare `tenant_<guid>` this module used before is still accepted. The role this module creates itself (`identity.tenant_role_name`, the local backend and the seeds) is Neurons'.
- **User subject: `user:<tenant-guid>.<member-guid>`.** Every tuple this module writes or checks names a person with the tenant in front and a dot; the member GUID is the Superset username (the `sub` claim). A user outside every tenant is the bare member GUID. The API, the picker and the share list accept and show the bare member GUID as before; `normalize_subject` canonicalises to the store spelling, so a share written by an older deployment as `user:<member>` no longer grants anything. `superset ownership check` lists every such row under `user_id_mismatch` and fails until they are gone; the operator unshares each by the spelling `check` lists (the delete revokes under that row's own spelling, so the old row and its tuple go together) and shares the person again.
- **Group id: `group:<tenant-guid>.<local-id>`.** `OWNERSHIP_GROUP_ID_FORMAT` defaults to `{tenant}.{name}`; the earlier `{name}_{tenant}` and `{tenant}_{name}` stay available. Groups nest as-is (`group:<t>.child#member` a member of `group:<t>.parent`) and there is no separate group-to-tenant tuple: the tenant is read from the id. An existing store written under the old format needs `OWNERSHIP_GROUP_ID_FORMAT = "{name}_{tenant}"` set, or its group tuples rewritten.
- **Tenant administrator: the `admin` relation on the tenant object.** `identity.tenant_administrator_group` names the userset `tenant:<guid>#admin` by default; `Directory.user_in_group` and the authorizers accept any `<object>#<relation>` userset. Administrators are users directly or a group named as admin, kept current by Ivanti's platform -- this module only reads the relation and never writes it. `OWNERSHIP_TENANT_ADMINISTRATOR_GROUP` may still name a group (`group:<id>`) for a deployment that keeps administrators as a group; the local backend maps the relation to the `tenant_administrator_<guid>` FAB role. The model's `tenant` type gains `define admin: [user, group#member]`, and the code reads it on every non-owner request: **run `superset ownership fga install-model` before the new code serves traffic**. Against a model without `tenant#admin`, the store answers every `is_tenant_administrator` check with a validation error and the object detail and list routes fail closed (500) for every non-owner. `fga show-model --check` now lists `tenant.admin` as required, so it reports the gap.
- **`fga install-model` merges.** A store that already has a model (Ivanti publishes theirs) gets its latest model with our `dashboard` and `chart` types added or replaced, a referenced type they lack added whole, and the relations our `tenant` and `group` types define that theirs lack (`tenant#admin`, `group#tenant`) added beside theirs -- nothing they define is rewritten, and a top-level `conditions` block is carried through. An empty store gets our model whole.
- **Set `OWNERSHIP_DIRECTORY_GROUP_WALK = "always"` on a Neurons store.** There is no group-to-tenant tuple there, so `list_groups`' fast path (`group#tenant`) is always empty; in the default `auto` mode every tenant's first listing per TTL walks the members anyway and logs a fallback warning each time. `"always"` skips the empty read and the warning.
- Seeds and the example package follow: `qa/seed_identity.py`, `seed_directory.py`, `seed_tenant_env.py`, `seed_fga.sh` write Neurons' roles, subjects, group ids and admin tuples.

## A tenant administrator transfers ownership but does not change sharing (PCS-10243)

- **Behaviour change.** A tenant administrator's authority over their tenant's objects is over who OWNS them, not over who sees them: `PUT .../owner` (a transfer, to a member or to themselves) and `POST .../claim` (an ownerless object) are theirs; `PUT .../visibility`, `POST .../shares` and `DELETE .../shares/<subject>` now answer `403` under the `tenant_admin` ground ("take ownership of it first"). A change of sharing can therefore only ever have been made by the name on the owner line, and an administrator's intervention leaves their name there. The detail and list routes report `can_share: false` with `can_manage: true` and `manage_reason: "tenant_admin"` for such a caller; the drawer shows the sharing controls inert, says why, and offers **Take ownership** (a transfer to the caller) beside **Transfer ownership**.
- The ownerless-object ADOPTION side effect of a visibility change is gone for tenant administrators (they claim first); it remains for Superset admins.
- Unchanged, on purpose: Superset admins (break-glass) and holders of `OWNERSHIP_MANAGE_PERMISSION` (an explicit opt-in whose purpose is sharing without owning) keep their sharing writes -- including a tenant administrator who also holds that role, who is admitted to a sharing write on the permission's ground (with its narrowing and its audit label) and reports `can_share: true`. Releasing ownership (`PUT .../owner {"subject": null}`) is an ownership action and stays with the tenant administrator.
- The drawer also shows a standing notice to anyone managing an object they do not own (a Superset admin, a manage-sharing holder), naming the owner and the ground.

## Public means public within the object's tenant (PCS-10243, PR #110)

- **New setting `OWNERSHIP_PUBLIC_SCOPE`, default `tenant`.** A `public` object is now visible to the members of the object's tenant only -- the tenant its row carries (revision `0004_ownership_object_tenant`, a mirror of the store's `tenant` tuple) -- plus its owner and Superset administrators; a member of another tenant gets the same 404 on the detail, list, data, export, thumbnail and ownership-metadata routes as for a private object, and a dashboard tile of another tenant's chart renders with `has_access: false`. `public` objects carry the sentinel viewer like private and shared ones; the read gate decides from the row alone (zero store calls). Set `OWNERSHIP_PUBLIC_SCOPE=instance` for the previous behaviour (Superset's dataset grant alone decides).
- **Fail closed on an untenanted public row.** A public object whose row carries no tenant is readable by its owner and by callers who are themselves outside every tenant (operators, service accounts), and by nobody in any tenant. It is never "public to everyone". `superset ownership check` lists such rows under `untenanted_public`; those whose owner belongs to a tenant -- a gap the mirror can be filled for -- are listed under `untenanted_public_repairable` and FAIL the check until `superset ownership backfill-tenants` (or a boot with `OWNERSHIP_REPAIR_ON_START=true`, which now runs that pass) stamps them. The row's tenant is kept lower-cased and compared case-insensitively.
- **Migrating an existing database:** `superset ownership db upgrade` (revision 0004), then `superset ownership backfill` (arms the sentinel on public objects and fills the mirror from the store, deriving a tenant only for objects the store has none for), then `superset ownership check`. Until the backfill runs, public objects of a migrated database are readable across tenants exactly as before (no sentinel yet); once it has run, `check` says what is left.
- **`backfill` stamps the owner's tenant (PR #111).** The tenant stamped on an object the store has none for is its OWNER's tenant (the invariant every transfer and claim enforces); the creator / last editor / history / dataset derivation only decides for an owner outside every tenant, or no owner at all, and still picks the administrators the attribution falls back to. Deriving from the creator first stamped the wrong tenant whenever the creator was not the owner the row named. A creator from another tenant stays a native Superset editor of the object until its next write (the flush guard strips them then); a backfill does not touch the native editors of an already-owned row.
- The store is the source of the row's tenant: a `backfill` re-run mirrors the tenant the store holds and never replaces it with a derived one, so a re-provisioned creator cannot move which tenant reads a public object. A transfer, claim or adoption reads the row's mirror when the store has no tenant yet (outbox lag), so it can no longer stamp a second tenant over a pending one.
- The list filter contributes the tenant's public objects through one SQL statement (the tenancy subquery on the ownership rows AND Superset's own dataset-grant filter, `published` for dashboards, as stock's own no-viewer rule) -- constant in the tenant's object count, no model load, no per-object permission check.
- The shared row-cache key version is `v2`: entries written before the `tenant_guid` column are unreachable after deploy rather than decoding as untenanted.
- Known limits of the tenant line, unchanged by this change and worth knowing on a multi-tenant instance: an account holding `all_datasource_access` or `all_database_access` bypasses Superset's own chart list filter entirely (`superset/charts/filters.py`), so such an account lists every chart definition -- do not grant either to a tenant-role account. An embedded guest token has no tenant; the host issues guest tokens per dashboard, and the tenant rule does not apply to embedded rendering. Every public read under tenant scope resolves the caller's tenant through the Identity seam once per request; an `OWNERSHIP_TENANT_GUID` hook that fails makes every tenanted public object unreadable for that caller (fail closed), and a hook that fails at creation leaves the new row untenanted -- `check` reports it as repairable and `backfill-tenants` fills it in once the hook answers. Deploy order: `superset ownership db upgrade` (or a boot with `OWNERSHIP_AUTO_MIGRATE` on) before any web worker runs this code -- the module reads the full `ownership_object` row and fails on every access, not just `check`, while the column is missing.
- An object whose owner belongs to no tenant (an operator's) is visible to tenant members only once it is in their tenant: transfer it to a member of that tenant (`PUT .../owner`), which stamps the recipient's tenant on an object that has none.

## Issue #82: the list base filter and single-object GET cache their store read

- The ownership plug-in's list base filter (`EXTRA_ACCESS_QUERY_FILTERS`, which FAB also applies to a single-object GET) now caches its `list_objects` reverse-index read for `OWNERSHIP_LOOKUP_CACHE_TTL` seconds (issue #82; same knob and cache primitive PR #75 introduced for the ownership-row lookup), request-locally and across requests. An owner's single-object GET, `/data/`, dashboard GET and `/charts` cost one store call per subject per TTL window instead of one per request, and the list route costs one per TTL instead of two per request. The cache is invalidated on this process's own share, unshare, transfer, visibility and claim writes before the write itself, so a share made in one request is visible to a list rendered in the very next one without waiting out the TTL -- or, if a list was rendered between the share and its delivery, within `OWNERSHIP_LOOKUP_CACHE_TTL` of delivery on a per-process cache backend (a shared backend such as Redis closes this immediately). A revocation -- unshare, group unshare, `-> private`, transfer away -- is denied from the moment it is queued, before the store has heard about it, and the cache never publishes or retains a subject's reverse-index entry while a revocation for that subject is undelivered, on every cache backend: a revoked, group-revoked or transferred-away subject's list and single-object GET stop naming the object at the same moment the access decision already denied it, not up to a TTL after the drain delivers. A `list_objects` answer the store could not actually be asked for (a timeout, a 5xx) is never cached either, so the store coming back is seen on the very next request rather than after a full TTL. Nothing to configure: this reuses the existing `OWNERSHIP_LOOKUP_CACHE_TTL` setting and its existing per-process-backend safety check.
- Moved here from the repo-root `UPDATING.md` (Superset core changes only) -- landed alongside PR #100, misfiled at the time.

## Issue #83: `{tenant}` placeholder in `OWNERSHIP_MANAGE_PERMISSION`

- `OWNERSHIP_MANAGE_PERMISSION` may now carry a `{tenant}` placeholder exactly once (e.g. `sharing_manager_{tenant}`), substituted with the caller's own tenant GUID before the role-name match, so one setting can serve a manage-sharing role provisioned per tenant instead of naming a single global role.
- Moved here from the repo-root `UPDATING.md` (R2-N1, `qa/reviews/review-manage-template-pr98.md`: the module gained its own `UPDATING.md` with PR #96, landed after this bullet was already written against the root file; the merge kept it there instead of moving it).

## Chain's own revision transitions serialised across a boot storm (#89)

- **The chain's own Alembic transitions (`superset ownership db upgrade`,
  and the `OWNERSHIP_AUTO_MIGRATE` boot hook) are now boot-storm safe on
  PostgreSQL.** Concurrent processes upgrading the chain at once (several
  web workers, the Celery worker and `superset init` booting together)
  used to race the chain's revision-transition bookkeeping and could fail
  the boot with `CommandError: Online migration expected to match one
  row...` or, from an empty database, a `CREATE TABLE` collision.
  `migrations/env.py` now takes a fixed `pg_advisory_xact_lock` around the
  chain's own migration run so concurrent boots serialise, and
  `migrate.upgrade` retries once -- narrowly, only when the failure
  matches that race's own shape -- as a backstop for what the lock does
  not reach.
- No operator action is required; the previous operational mitigation
  (upgrading from exactly one process, or running `superset ownership db
  upgrade` once in the deploy pipeline with `OWNERSHIP_AUTO_MIGRATE=false`
  everywhere else) is no longer necessary and is now optional.
- SQLite and other non-PostgreSQL dialects take no lock (documented
  single-process deploy assumption; unchanged).

## Issue #93: private revokes existing shares

**Behaviour change.** `PUT /<asset_type>/<pk>/visibility {"visibility": "private"}`
now revokes every existing share of the object as part of the same request:
each share's mirror row (`ownership_share`) is deleted and a
`revoke_subject` is queued through the outbox for every relation that
subject holds -- the same path `DELETE /<asset_type>/<pk>/shares/<subject>`
already used, so the read gate's pending-revocation check covers the
delivery window exactly as it does for an explicit unshare. Previously the
shares stayed in place and, worse, `raise_for_access_bypass` still honoured
them for a `private` object: a chart or dashboard set to Private stayed
readable by everyone it had been shared with. `private -> shared`
afterwards starts with zero shares, exactly as a brand-new object does; a
caller who wants the old shares back has to re-grant them.

One `ownership.share_removed` audit event is emitted per share revoked this
way, with `admitted_by: "visibility_change"` (a new value; see `audit.py`'s
ADMITTED_BY catalogue) rather than whichever of owner/tenant_admin/admin/
manage_permission admitted the caller to change the visibility -- that
ground is still on the `ownership.visibility_changed` event the same
request emits.

`raise_for_access_bypass` also gained its own defence in depth: a
non-owner is refused a `visibility == "private"` object before any store
call is made (matching the zero-store-call owner fast path), rather than
relying solely on the revocation having been delivered.

`DELETE /<asset_type>/<pk>/shares/<subject>` now accepts a subject that
still has a share row on the object even when it no longer resolves
through the identity seam or the authorization store (an account renamed,
a GUID relocated, a group removed from the directory since the share was
made) -- previously such a share could never be removed through the API,
answering `400 subject does not name a known account` forever. A subject
with neither a mirror row nor a valid resolution is still a 400.

The `SharingDrawer`'s Private option no longer implies shares are kept: its
copy now reads "Visible to only you. Existing shares are removed." and
`handleApply` no longer needs to (and does not) issue any `/shares` calls
when applying a `private` visibility -- the backend does the revoking.

### Review round 1 fixes (H1/H2/M1/M2)

- **H1 -- the hard deny now covers editor shares, not only viewer.**
  `hooks.editor_subject_ids` (the function PR #95 split
  `editor_share_user_ids` out of) answers owner-only when
  `row.visibility == "private"`, the same rule the read gate already
  applied -- an editor share on a still-private object used to open AND
  edit it, since Superset's own `is_editor` trusted this function outright
  and it did not know the private rule. `POST /<asset_type>/<pk>/shares`
  on a `private` object now answers `409 object is private; make it
  shared before sharing` instead of writing a grant the read gate ignores
  for a viewer and honoured for an editor.
- **H2 -- the list filter and single-object GET now agree with the hard
  deny.** `owned_or_shared_rows` skips a `private`, non-owner row outright
  (it can never survive the reachability check below it, so it is also
  never worth a `pending_revocations_for` probe). Previously the list
  filter and `GET` had no visibility test beyond `public`: for the whole
  drain window after `shared -> private` a revoked user's list still
  named the object and `GET` still answered `200`, and a share planted
  directly on a `private` object (never through the transition) was
  listed forever.
- **M1 -- a manage-sharing holder may no longer take an object `-> private`
  when doing so would revoke their own share or the tenant's structural
  group.** `-> private` revokes every mirror row, which for a holder
  admitted only by `manage_permission` can include the share that admits
  them and the tenant's whole-membership group -- exactly the two writes
  this same ground already refuses individually on `POST`/`DELETE
  /shares`. Reaching them through `-> private` instead let a holder lock
  themselves out with no way back (`-> shared` afterwards starts with no
  shares, so nothing re-admits them). Refused with `403`, same shape as
  the existing `-> public` refusal; a holder admitted through a
  third-party share only is unaffected (`200`, as before).
- **M2 -- inline mode (`OWNERSHIP_OUTBOX_ENABLED=false`) now rolls back
  and answers `502` on a store refusal mid-revoke.** With the outbox on,
  an enqueue failure raises and the whole transaction (visibility
  included) unwinds before `_revoke_shares_for_private` could ever report
  one; with it off, `service.remove_share` can return `False` for a
  refused delete, and this used to be discarded -- `private` committed,
  the mirror row was gone, and a `share_removed` event was emitted for a
  tuple the store still held in force. `_revoke_shares_for_private` now
  reports `(revoked, failed)`; a non-empty `failed` rolls the transaction
  back and answers `502` before the sentinel write -- the same shape the
  unshare route already uses for the identical refusal. The loop does not
  stop at the first refusal (R2-N2, `qa/reviews/review-private-revokes-
  pr96.md`): with two or more shares, an EARLIER one can already be gone
  from the store by the time a LATER one is refused and the 502 fires, so
  the message reads `the authorization store rejected this change; some
  grants may already be revoked -- retry to finish revoking the rest`, not
  the single-share "nothing was changed" the unshare route's identical 502
  still uses correctly (exactly one share per request there).
- **Nits.** `private -> private` normally has nothing to revoke, but
  revokes any stray share row it finds anyway (R2-N3, `qa/reviews/
  review-private-revokes-pr96.md`: a row planted directly at the service
  layer, bypassing the route that would have refused it on an already-
  private object -- see the "stray editor/viewer row" tests) and emits the
  `visibility_changed` audit event with no `share_removed` alongside it
  only in the genuinely-empty case -- intentional, not a bug. The `SharingDrawer`'s "Visible to only you" copy
  is also shown when a tenant administrator or a manage-sharing holder
  applies `private`, for whom "you" means the object's owner, not the
  caller -- pre-existing wording, left as is; M1 above is the behaviour
  change that makes a holder's own case matter more than it used to.
  `api._remove_asset_share` now reads the share table once
  (`service.list_shares`) instead of once via `list_share_rows` and again
  via `list_shares` a few lines later.
- **Conflicts with #94's entry below.** Both PRs append a new section
  under this file; this entry and the "Revocation window closed" section
  below it are independent and both apply -- H2's `owned_or_shared_rows`
  change and #94's `pending_revocations_for` subtraction are two separate
  conditions in the same function, not a rebase of one onto the other.
- **PR baseline.** This PR is based on `main` after #94
  (`5d59dd9`, closing #84) and #95 (`86d5e6`, closing #80) have both
  landed; the suite figure quoted in the PR body is measured against that
  baseline, not against `main` before either.

## Revocation window closed for the list filter and single-object GET (#84)

- **`owned_or_shared_rows`/`owned_or_shared_object_ids` now subtract any
  object with an undelivered revocation for the caller** (`outbox.
  pending_revocations_for`, no extra authorization-store call): before
  this, a revoked share kept appearing in `dashboard_query_filter`/
  `chart_query_filter` -- and, through FAB's list base filter, in
  `GET /api/v1/<asset>/<id>` -- for as long as the outbox took to drain,
  even though the access decision itself already denied it
  (`hooks.raise_for_access_bypass` via `outbox.has_pending_revocation`). A
  revoke of a `group:<id>#member` share is treated the same fail-closed way
  `has_pending_revocation` already treats one: it hides the object from
  every subject asking, member or not, until delivered, never only from
  members the request can otherwise prove membership for.
- **Consistent effect on the ownership metadata API (review round 1,
  M-2).** Because `api._reachable_ids` delegates to the same function, a
  holder with an undelivered revocation now gets `404` from
  `GET /api/v1/ownership/<asset>/<id>` (previously `200`, with management
  separately refused) and drops out of `GET /api/v1/ownership/<asset>s`
  scope, on the same timeline as the list filter and Superset's own detail
  route -- not a separate behaviour, the same one applied consistently.
- **Query shape (review round 1, H-1/M-1).** `pending_revocations_for`
  reads the undelivered backlog for the caller's subject in one indexed
  query (`ix_ownership_outbox_status_id`), then, only for `delete` rows in
  that backlog, probes `ownership_share` (`uq_ownership_share_subject`) for
  the role-change carve-out -- resolving each row's `object_id` from the
  candidate rows `owned_or_shared_rows` already scanned, never by
  re-reading `ownership_object`. Both reads are bounded by the size of the
  undelivered backlog, not by the size of the ownership mirror. The call is
  skipped entirely when the caller's scan has no actual SHARED candidate to
  subtract from: an owner-only request, or a subject the store lists
  nothing for, pays nothing at all. This is not simply "the store's viewer
  listing is empty" -- the model has `viewer` include `owner`
  (`model/ownership.fga`), so an owner's own objects appear in that listing
  too once their `owner` tuple has reached the store; the skip condition
  checks for a row that is governed, not owned by this caller, AND in that
  listing, not the listing's emptiness. `has_pending_revocation`'s own role-change carve-out
  takes the same shape when its caller passes the row's `object_id` (both
  production call sites do); omitted, it falls back to the pre-existing
  `object_uuid` join.

## PCS-10243 plug-in architecture

One PR: `settings.py` and `plugins.py` (§3/§4, the loader/registry and its
strict-vs-degraded boot), `FgaConnection` and the credentials/401-cooldown
machinery (§5), the `Identity` protocol and `DefaultIdentity` (§6), the
`Directory` protocol split out of `Authorizer` (§7), the model artefact and
`fga` CLI group (§9), and `superset ownership plugin verify`/`seed-scratch`
(§10) plus the worked `qa/ivanti_pcs_example/` override package. An empty
plug-in block reproduces the pre-PR baseline exactly -- no action is
required to keep running the local or default-OpenFGA backends. See
`qa/design/directory-hook/02-technical-spec.md` for the full design and
`qa/design/directory-hook/04-manual-test-round.md` for the runbook that
exercises every seam end to end on the shared stack.

### New configuration keys

All default to reproducing the baseline; setting none of them changes
anything. `OWNERSHIP_DIRECTORY`, `OWNERSHIP_IDENTITY`, `OWNERSHIP_FGA_CREDENTIALS`,
`OWNERSHIP_FGA_CONFIG_PROVIDER`, `OWNERSHIP_FGA_STORE`, `OWNERSHIP_FGA_MODEL`,
`OWNERSHIP_FGA_TIMEOUT`, `OWNERSHIP_DIRECTORY_GROUP_WALK`,
`OWNERSHIP_DIRECTORY_GROUP_WALK_TTL`. `docker-compose-light.override.yml`
passes through all nine with blank defaults.

### One precedence for every `OWNERSHIP_*` key (§3)

`settings.get`: a config layer, then the environment, then a typed
default. Behaviour changes from unifying every key onto this one rule:

- **Legacy boolean keys widened, not narrowed** (§3.1): `OWNERSHIP_OUTBOX_ENABLED`,
  `OWNERSHIP_AUTO_MIGRATE`, `OWNERSHIP_OUTBOX_CELERY`, `OWNERSHIP_REPAIR_ON_START`
  now accept `on`/`off` as well as their previous spellings; `1`/`yes` are
  newly ON for the two `False`-default keys (`OUTBOX_CELERY`,
  `REPAIR_ON_START`). Every spelling that used to work still works. An
  unrecognised spelling now logs one WARNING and reads as **off**,
  regardless of the key's own default -- a typo can no longer silently
  re-enable a feature an operator turned off on purpose.
- **`OWNERSHIP_MANAGE_PERMISSION` and `OWNERSHIP_LOOKUP_CACHE_TTL`**: an
  explicit blank value at the config-layer used to be read as a terminal
  "off"/"0" for these two keys specifically (a divergence from every other
  `OWNERSHIP_*` key). Both now follow the one rule like everything else --
  blank means "not set here", and the next level (environment, then
  default) answers instead. A literal `0` still turns the shared lookup
  cache off explicitly.
- **`fga.FGA_API_URL`/`FGA_STORE_ID`/`FGA_MODEL_ID`/`TIMEOUT`** are read-only
  now (a `__getattr__` shim resolving to the live connection); assigning to
  them no longer has any effect. Import-time `OWNERSHIP_FGA_*` reads are
  gone -- `superset ownership db upgrade`/`status` and a bare
  `migrate.upgrade()` touch no plug-in seam at all (closes #88's
  import-time half).
- **`OWNERSHIP_PRIVATE_ERROR`** had no reader anywhere in the tree; dropped
  from the settings migration table, noted here for completeness.
- **`OWNERSHIP_DEFAULT_OWNER`**: a non-integer value used to stop the boot
  (`int()` raised at `superset_config_docker_light.py`); it now logs one
  WARNING and reads as `None` (no default owner) instead.
- **`OWNERSHIP_AUDIT_SINK`** is now also readable from the environment (a
  dotted `pkg.mod:callable` string), which it never was before -- previously
  only a config-layer callable reference worked.
- **Maintenance commands degrade instead of blocking**: `superset ownership
  db upgrade|downgrade|current|stamp`, `status`, `fga
  reconnect|status|install-model|show-model` and `plugin verify|seed-scratch`
  now load DEGRADED (falling back to `LocalAuthorizer`/`LocalDirectory`/
  `DefaultIdentity`/no connection per failing seam) rather than refusing to
  start when a plug-in is misconfigured -- the failure prints to stderr
  first, `superset ownership status` reports `"degraded": [...]`. Every
  other process (web, Celery, `check`/`enable`/`reconcile`/`purge-tenant`/
  `backfill-tenants`/`outbox *`, a plain `superset db upgrade`) is strict as
  before: a broken plug-in still stops those. This match is on `sys.argv`
  shape, not fuzzy: a mistyped or shell-quoted subcommand (e.g. one argv
  element `"fga status"` instead of two separate ones) does not match the
  maintenance table and boots STRICT like web/Celery -- correct per spec
  §4.2 (an unrecognised argv silently downgrading to DEGRADED would make a
  typo indistinguishable from a real maintenance command), but it means a
  broken store configuration then surfaces as a raw `Failed to create app`
  traceback rather than the DEGRADED banner an operator would see from the
  correctly-spelled subcommand.
- **`cli.py status`** reports `plugins.describe()` (per-seam class, protocol
  version, degraded list) in place of the single `OWNERSHIP_AUTHORIZER`
  value it used to read straight out of `app.config`.
- **New `fga` CLI group**: `superset ownership fga install-model|show-model|
  reconnect|status`, alongside the existing `db`/`outbox` groups --
  `install-model` writes the shipped model (`model/ownership.fga`) to a
  store, `show-model [--check]` renders the store's model and can diff it
  against the required `type.relation` table, `reconnect` rebuilds the
  connection now (ignoring the 401 cooldown), `status` reports it. All four
  are maintenance commands (load DEGRADED, per the rule above).
- **`Authorizer` shrank**: `tenant_members`, `group_exists` and
  `groups_for_tenant` moved to the new `Directory` protocol (§7) and are no
  longer part of `Authorizer`. A custom `OWNERSHIP_AUTHORIZER` class written
  against the pre-PR protocol that implemented (or was expected to answer)
  those three methods keeps working as an authorizer, but nothing calls
  those methods on it anymore; a class that relied on being asked them
  should be reworked as a `Directory` override instead. `user_in_group`,
  `object_tenant`, `tenant_objects` and `set_object_tenant` are unchanged
  and stay on `Authorizer`.
- **`/subjects`**: a group's `extra.members` may now be `null` (the fast
  `list_groups` path -- reading straight off the `group.tenant` tuple --
  does not count members; the walking fallback still returns an integer).
  The picker shows `"N member(s) · from directory"` when a count is known
  and just `"from directory"` when it is `null`
  (`SubjectPickerPanel.tsx`). `source` on a group/user row now reflects
  the configured directory's alias/class (`plugins.describe()["directory"]`)
  instead of the literal string `"openfga"`.

### `plugin verify` / `qa/ivanti_pcs_example/`

- **New CLI group**: `superset ownership plugin verify|seed-scratch|describe`.
  `verify` runs the 22-check conformance kit (the technical spec's own §10
  table enumerates 22 distinct checks) against the configured
  authorizer/directory/identity and exits 0 (conformant), 1 (a check
  failed) or 2 (the plug-ins did not load, or the store could not be
  reached, before the rest of the checks could run) -- see
  `plugin_verify.py`'s module docstring for the full table. `--json` emits
  the same report as JSON, for a CI gate.
- **`qa/ivanti_pcs_example/`** is a new, deliberately NOT-installed package
  (`README.md` there is the "what Ivanti implements" page). It is not on
  `PYTHONPATH` in any default deployment; only the manual test round and
  this package's own tests add it. Nothing here changes runtime behaviour
  for an instance that does not point a plug-in seam at it.
- **`seed-scratch --api-url URL --store ID [--broken VARIANT]`** writes the
  kit's fixture tuples straight at `--store` through a one-off connection
  it builds itself; it refuses to run when `--store` is the same store the
  process is already configured against. Requires `--store`/`--api-url`
  explicitly -- there is no default.
  (An earlier draft of this command could not run at all, and a fixed
  version of it would have written into whatever store `OWNERSHIP_FGA_*`
  pointed the process at, ignoring `--store`; both are fixed before this
  note landed, not a change in behaviour anyone depended on.)
- **`verify` has no `--fixtures-broken` flag.** It was accepted and threaded
  through but read by no check. The red path is `plugin seed-scratch
  --broken VARIANT` against a scratch store, then a normal `verify` run
  against it.
- **Cutting over to a new authorization store** (`README.md` section 7)
  must delete the orphan `share_tuples_without_row` `superset ownership
  reconcile` reports before writing (`reconcile --write`), because the
  store is authoritative for shares and a stale `viewer`/`editor` tuple
  with no mirror row still grants access regardless of what Superset's own
  records say. (Moved here from the repo-root `UPDATING.md`, Superset core
  changes only -- misfiled at the time this landed.)

### Function hooks (§4.5)

New module `plugin_hooks.py`: sixteen `OWNERSHIP_*` settings, each a single
function answering one question instead of a whole `Identity`/`Directory`
class -- `qa/design/directory-hook/01-plugin-contract.md` §4.5 is the
contract; `qa/ivanti_pcs_example/README.md`'s "1b. Function hooks" section
is the short version. Every hook defaults to `None` (today's behaviour
unchanged); setting none of them changes anything.

- **The hooks**, by name: `OWNERSHIP_MEMBER_GUID`, `OWNERSHIP_TENANT_GUID`,
  `OWNERSHIP_DISPLAY_NAME`, `OWNERSHIP_IS_TENANT_ADMINISTRATOR`,
  `OWNERSHIP_USER_FOR_MEMBER_GUID` (caller-side, take the FAB `User` row);
  `OWNERSHIP_USERS_OF_TENANT`, `OWNERSHIP_GROUPS_OF_TENANT`,
  `OWNERSHIP_MEMBERS_OF_GROUP`, `OWNERSHIP_USER_IN_GROUP`,
  `OWNERSHIP_GROUP_EXISTS`, `OWNERSHIP_ADMINISTRATORS_OF_TENANT`
  (directory-side, take ids); `OWNERSHIP_GROUP_ID`,
  `OWNERSHIP_SPLIT_GROUP_ID`, `OWNERSHIP_GROUP_DISPLAY_NAME`,
  `OWNERSHIP_TENANT_ADMINISTRATOR_GROUP` (shape-side, pure); and
  `OWNERSHIP_CAN_MANAGE` (decision-side, may only narrow the default reason).
- **Spelling**: a dotted path (`"pkg.mod:function"`) or a callable set
  directly in the config module, exactly like the class seams. Resolved
  once at boot by `plugins.load()`, right after the three class seams; a
  non-callable or an unimportable path fails a strict boot the same way an
  unresolvable `OWNERSHIP_AUTHORIZER` does, and loads DEGRADED under the
  same maintenance-command table (§4.2) as the class seams.
- **`OWNERSHIP_GROUP_ID` and `OWNERSHIP_SPLIT_GROUP_ID` are a pair**:
  setting one without the other fails the boot (strict) or drops both back
  to the default (degraded) -- they must stay inverses.
- **Precedence**: a configured hook wins over the corresponding class-seam
  method, which wins over the built-in default. `plugins.get_identity()`
  and `plugins.get_directory()` now hand back a thin wrapper
  (`HookedIdentity`/`HookedDirectory`) that checks the hook first and falls
  through to the wrapped seam otherwise; every existing call site
  (`identity.resolve_member_guid`, `get_directory().list_groups(...)`, etc.)
  is unchanged and goes through the wrapper automatically. A new accessor,
  `plugins.get_hook(name)`, answers "is this one configured" directly, for
  callers with no corresponding seam method (the shape hooks in
  `identity.py`, `OWNERSHIP_CAN_MANAGE` in `api.py`).
- **Fail closed**: a hook that raises is logged at most once per (hook,
  exception class) per `OWNERSHIP_LOOKUP_CACHE_TTL` window (review round 1
  M-5: it used to be once per process, ever) -- the class name only, never
  the message, which may carry a credential -- and answered as
  `None`/`False`/`[]` per its own table row (a "list" hook's items are
  validated to the ref shape a directory page's items are; a malformed item
  is itself an "unknown" answer). `describe()` also now carries a running
  `failure_count` per hook once it has raised at least once. The pure
  "shape" hooks and `OWNERSHIP_DISPLAY_NAME` are the one exception: since
  every caller needs *some* answer, a raise there falls back to the
  built-in format-string/default computation instead
  (`plugin_hooks.call_or_default`); `hooks/fail_closed` does not check them
  for this reason (review round 1 H-2b: it used to, and could never pass a
  correctly-answering `OWNERSHIP_DISPLAY_NAME`).
- **Caching**: a per-user hook (`OWNERSHIP_MEMBER_GUID`, `OWNERSHIP_TENANT_GUID`,
  `OWNERSHIP_IS_TENANT_ADMINISTRATOR`) is cached per process for
  `OWNERSHIP_LOOKUP_CACHE_TTL` seconds, keyed by `user.id`; `OWNERSHIP_DISPLAY_NAME`
  and `OWNERSHIP_USER_FOR_MEMBER_GUID` are never cached. A directory hook
  (`OWNERSHIP_GROUPS_OF_TENANT`, `OWNERSHIP_MEMBERS_OF_GROUP`,
  `OWNERSHIP_ADMINISTRATORS_OF_TENANT`) is cached for `OWNERSHIP_DIRECTORY_GROUP_WALK_TTL`
  seconds (the package's one existing directory-cache TTL, reused rather
  than adding a second knob). Both layers reuse `service.py`'s shared-cache
  primitive (Superset's own Flask-Caching handle, gated by the same
  per-process-backend safety check) -- no second cache mechanism. Only a
  RETURNED answer is cached (review round 1 M-2: a raise used to be cached
  too, pinning "unknown" -- a denial, or an empty picker page -- for the
  rest of the TTL from one transient store blip); a demotion or a logout is
  therefore visible after at most one TTL window plus the in-flight
  request, the same staleness bound a successful answer going stale
  already has.
- **`is_tenant_administrator(user)`** (`api.py`) now checks
  `OWNERSHIP_IS_TENANT_ADMINISTRATOR` first; otherwise it reads membership
  through the `Directory` seam (`get_directory().user_in_group`) instead of
  the `Authorizer` directly, as every other "who is in which group"
  question in this module already does.
- **`OWNERSHIP_CAN_MANAGE(user, object_state) -> reason | None`**: called
  after the default reason (`_default_manage_reason`, the pre-existing
  `_manage_reason` body) is computed, with `object_state` = `{asset_type,
  object_id, object_uuid, owner (Superset user id), tenant, visibility,
  shares, default_reason}` -- not literally the detail endpoint's own
  response dict (which also carries this hook's OUTPUTS:
  `can_manage`/`can_share`/`unowned`/`manage_reason`). May only narrow:
  `None`, or the same reason back; a different, wider reason is logged (at
  most once per distinct (returned, default) pair per process, debug
  afterward -- review round 1 N-4) and ignored
  (`plugin_hooks.narrow_manage_reason`). A RAISING hook denies EVERY
  caller for that object, admins included -- fail-closed `None`, narrowed
  against any default, is `None` unconditionally; this is intentional
  (§4.5.1 item 4's "unknown always denies"), not "no narrowing" in the
  sense of "the default still stands". Wired into the one seam
  (`api._manage_reason`, called from the detail endpoint, the share/
  unshare/visibility/transfer route guards) and, separately, into the
  list-page's own bulk-computed per-row `can_manage` (`_list_assets`) --
  not resolved unless configured, so the zero-extra-store-calls property of
  an unconfigured deployment is unchanged; when it IS configured, the page
  builds `object_state["tenant"]`/`["shares"]` from data already computed
  for the whole page (the caller's own tenant-objects set) plus ONE batched
  share query, not a per-row store call and query (review round 1 M-3: it
  used to cost one `object_tenant` round trip and one `list_shares` query
  PER ROW).
- **Cross-tenant group guard (review round 1 M-6)**: a share to
  `group:<id>` re-confirms `OWNERSHIP_SPLIT_GROUP_ID`'s tenant claim against
  the store's own `group.tenant` tuple when the model has one
  (`OpenFGADirectory.group_tenant`, best-effort, not part of the `Directory`
  protocol); when it does not (the `local` backend, or a model with no such
  relation), the route instead asserts the hook's own answer round-trips --
  `group_id(*split_group_id(id)) == id` -- so a `split_group_id` that lies
  about which tenant a group belongs to can no longer make a cross-tenant
  share succeed.
- **`hooks/read_only` also watches `superset_ownership.fga`'s write entry
  point** (review round 1 M-8), not only `plugins.counting()`'s Authorizer/
  Directory objects -- a hook reading `fga.read_page` directly (as the
  reference `users_of_tenant`/`groups_of_tenant` do) is exactly the shape
  that could also write through `fga.write_tuple`/`delete_tuple` invisibly
  to the counting proxy; now it is not.
- **`plugins.describe()`** (and `plugin describe`) gained a `"hooks"` key:
  `{setting: {"source": "default" | "default (degraded)" | "config", ["path": ...], ["failure_count": N]}}`
  for all sixteen, in table order.
- **`normalize_subject` through a configured hook** (review round 1 H-1):
  `HookedIdentity.normalize_subject` now resolves the reverse/forward pair
  (`OWNERSHIP_USER_FOR_MEMBER_GUID` then `OWNERSHIP_MEMBER_GUID`, with the
  forward-mapping check `_default_normalize_subject` already applies)
  through ITSELF -- meaning through the configured hooks, falling back to
  the wrapped seam only when neither is set. It used to delegate straight
  to the wrapped seam's own `normalize_subject`, which never consulted the
  hooks at all: a hook that relocates the member GUID off the username
  (the entire point of the identity hook pair) could not actually be
  shared to -- `user:<new-guid>` always resolved to `None`.
- **Test-suite hermeticity (review round 1 M-1)**: `tests/superset_harness.py`'s
  generated config now sets every `OWNERSHIP_<hook>` setting (and
  `OWNERSHIP_IDENTITY`) to its default right after the environment's own
  `superset_config`/`superset_config_docker.py` is star-imported, generated
  from `plugin_hooks.HOOK_SPECS` so it cannot drift. The suite is therefore
  green regardless of whatever hook block the deployment config carries --
  before this, running it in a container whose config already had the
  acceptance reference block installed failed 37 tests with one `ERROR
  ownership: hook OWNERSHIP_MEMBER_GUID raised OperationalError` line
  (the reference hook querying a table the harness's throwaway SQLite has
  never heard of) and every share refused for the rest of the process.

# What Ivanti implements

This package is a worked example of the plug-in seams PCS-10243 adds to
`superset_ownership`. It is **not installed** and **not on `PYTHONPATH`** by
default -- the module docstrings, the manual test round
(`qa/design/directory-hook/04-manual-test-round.md`), and this package's own
tests add it explicitly. Nothing here is required reading to run a default
deployment; it exists so "what would an override actually look like" has a
runnable answer, using the real module and class names from
`docker/pythonpath_dev/superset_ownership/`.

For the full protocol reference (every method, every failure contract) see
`qa/design/directory-hook/01-plugin-contract.md`. This page is the short
version: what you (Ivanti) actually have to do, and how to prove it.

## 1. Fill in the configuration block

Everything lives in `superset_config.py` (or your overlay's config layer).
Every key has a default that reproduces the pre-override baseline -- an empty block
is a valid block:

```python
# --- PCS ownership: the plug-in block -------------------------------------
OWNERSHIP_ENABLED = True
OWNERSHIP_AUTHORIZER = "openfga"                    # or "local", or "pkg.mod:Class"

# How we reach OpenFGA. Either static values...
OWNERSHIP_FGA_API_URL = "https://openfga.ivanti.example"
OWNERSHIP_FGA_STORE   = "01H..."
OWNERSHIP_FGA_MODEL   = "01H..."                     # pinned; a deployment decision
OWNERSHIP_FGA_CREDENTIALS = {"type": "api_token", "token_env": "OPENFGA_TOKEN"}
# ...or a provider you write, for vaults / rotation / per-environment lookup:
# OWNERSHIP_FGA_CONFIG_PROVIDER = "ivanti_pcs_example.fga:connection"

# Who is who. Defaults assume the member GUID lands on the Superset
# username and the tenant on a `tenant_<guid>` FAB role. Override only if
# your JIT login puts the member id elsewhere:
# OWNERSHIP_IDENTITY = "ivanti_pcs_example.identity:AttributeIdentity"

# The directory: users, groups, members, administrators of a tenant.
OWNERSHIP_DIRECTORY = "openfga"                     # default: read from the store
# OWNERSHIP_DIRECTORY = "ivanti_pcs_example.directory:FixedGroupsDirectory"

OWNERSHIP_GROUP_ID_FORMAT = "{name}_{tenant}"       # or "{tenant}_{name}"
OWNERSHIP_MANAGE_PERMISSION = None                  # optional sharing-manager role
# OWNERSHIP_MANAGE_PERMISSION = "sharing_manager_{tenant}"  # or one role per tenant
OWNERSHIP_OUTBOX_ENABLED = True
```

`OWNERSHIP_MANAGE_PERMISSION` may name a single dedicated role
(`"sharing_manager"`) or carry the `{tenant}` placeholder exactly once
(`"sharing_manager_{tenant}"`, issue #83); at request time the placeholder
is substituted with the CALLER's own tenant GUID before the match, so one
setting serves every tenant's own role rather than one tenant's. A FAB
built-in or a tenant's structural role (`tenant_<guid>`,
`tenant_administrator_<guid>`, templated or not) is refused.

Every class-valued key accepts three spellings: a short alias we ship
(`"openfga"`, `"local"`), a dotted path (`"package.module:ClassName"`, as
used above), or an already-constructed instance. The loader runs once at
startup, logs one line per seam, and **fails the boot loudly** on an
unloadable class or an unsatisfied protocol -- a wrong plug-in never
degrades silently to a default (except for `superset ownership db|status|fga|plugin`,
which load DEGRADED so those commands still work while you fix it; see the
contract §6).

For most deployments, step 1 is all there is.

## 1b. Function hooks

Most of what a deployment needs to change is a single answer -- "where is
this user's tenant id", "who administers a tenant", "how is a group id laid
out" -- not a whole `Identity`/`Directory` class. The contract's §4.5 turns
every such question into its own `OWNERSHIP_*` setting: a dotted path
(`"pkg.mod:function"`) or a plain function set directly in the config
module, resolved once at boot exactly like the class seams above.
Everything not named keeps today's behaviour -- setting none of these
changes anything.

| setting | signature | default behaviour |
|---|---|---|
| `OWNERSHIP_MEMBER_GUID` | `(user) -> str \| None` | `user.username` if it is a GUID, else the first GUID in `user.email`, else `local-<id>` |
| `OWNERSHIP_TENANT_GUID` | `(user) -> str \| None` | GUID of the first `tenant_<guid>` role |
| `OWNERSHIP_DISPLAY_NAME` | `(user) -> str` | `first_name last_name`, else username |
| `OWNERSHIP_IS_TENANT_ADMINISTRATOR` | `(user) -> bool` | `user_in_group(member_guid, "tenant_administrator_<tenant>")` through the Directory seam |
| `OWNERSHIP_USER_FOR_MEMBER_GUID` | `(guid) -> user \| None` | `find_user(username=guid)` then by email |
| `OWNERSHIP_USERS_OF_TENANT` | `(tenant, query, limit, cursor) -> Page[UserRef]` | `tenant:<t>#member` tuples labelled from `ab_user` |
| `OWNERSHIP_GROUPS_OF_TENANT` | `(tenant, query, limit, cursor) -> Page[GroupRef]` | `group.tenant` tuples (fast path) or the walk |
| `OWNERSHIP_MEMBERS_OF_GROUP` | `(group_id, limit, cursor) -> Page[UserRef]` | `group:<id>#member` tuples, nested groups expanded |
| `OWNERSHIP_USER_IN_GROUP` | `(member_guid, group_id) -> bool` | one `check` |
| `OWNERSHIP_GROUP_EXISTS` | `(group_id) -> bool` | the group has a tenant tuple or at least one member |
| `OWNERSHIP_ADMINISTRATORS_OF_TENANT` | `(tenant) -> list[UserRef]` | members of `group:tenant_administrator_<tenant>` |
| `OWNERSHIP_GROUP_ID` | `(name, tenant) -> str` | `OWNERSHIP_GROUP_ID_FORMAT` rendered |
| `OWNERSHIP_SPLIT_GROUP_ID` | `(group_id) -> (name, tenant) \| None` | the format parsed back |
| `OWNERSHIP_GROUP_DISPLAY_NAME` | `(group_id) -> str` | the `name` part with `_` -> space |
| `OWNERSHIP_TENANT_ADMINISTRATOR_GROUP` | `(tenant) -> str` | the OBJECT reference `group:tenant_administrator_<tenant>` (not the bare name -- this is what `user_in_group` receives) |
| `OWNERSHIP_CAN_MANAGE` | `(user, object_state) -> reason \| None` | admin > owner > tenant administrator > manage permission |

Five rules apply to every hook (the contract's §4.5.1 has the full text):

1. **Context is passed in**, never read from `flask.g` -- a hook must work
   from a request, the CLI, backfill and the reverse lookups alike.
2. **Precedence**: a configured hook wins over the class-seam method above
   it, which wins over the built-in default.
3. **Fail closed**: a hook that raises is logged at most once per (hook,
   exception class) per `OWNERSHIP_LOOKUP_CACHE_TTL` window (the exception
   class name only, never its message) and answered as `None`/`False`/`[]`.
   Unknown always denies -- `OWNERSHIP_CAN_MANAGE` included: a raising
   `OWNERSHIP_CAN_MANAGE` denies EVERY caller for that object, admins
   included, not merely "no additional grant". The four shape hooks
   (`OWNERSHIP_GROUP_ID`/`OWNERSHIP_SPLIT_GROUP_ID`/
   `OWNERSHIP_GROUP_DISPLAY_NAME`/`OWNERSHIP_TENANT_ADMINISTRATOR_GROUP`) and
   `OWNERSHIP_DISPLAY_NAME` are the exception to "unknown": every caller
   needs *some* answer, so a raise there falls back to the built-in
   computation instead.
4. **Caching**: `OWNERSHIP_MEMBER_GUID`, `OWNERSHIP_TENANT_GUID` and
   `OWNERSHIP_IS_TENANT_ADMINISTRATOR` are cached per user for
   `OWNERSHIP_LOOKUP_CACHE_TTL` seconds, keyed by user id --
   `OWNERSHIP_DISPLAY_NAME` and `OWNERSHIP_USER_FOR_MEMBER_GUID` are NOT
   cached. A directory hook (`_OF_TENANT`/`_OF_GROUP`) is cached for
   `OWNERSHIP_DIRECTORY_GROUP_WALK_TTL` seconds. Only a RETURNED answer is
   cached, never a failure -- a one-off raise does not pin "unknown" for the
   rest of the TTL. A hook that calls an external API is therefore called
   at most once per key per TTL, not once per request. **`subject_display_name`
   costs one such (uncached) reverse lookup per share row** whose subject is
   not a plain username -- an orphaned row included -- on both the object
   detail and `OWNERSHIP_CAN_MANAGE`'s per-page `object_state`: up to two
   extra `find_user` calls with the built-in default, or one
   `OWNERSHIP_USER_FOR_MEMBER_GUID` call with the hook configured. Bounded by
   the share count on the object being rendered, not a page of many objects,
   so a slow reverse lookup (an external API, in particular) is a per-object
   cost worth knowing about, not an unbounded one.
5. **`OWNERSHIP_GROUP_ID` and `OWNERSHIP_SPLIT_GROUP_ID` must be set
   together, as inverses of each other** -- setting one without the other
   fails the boot. `OWNERSHIP_CAN_MANAGE` may only *narrow* the default
   reason (`None`, or the same reason back); a hook that returns a reason
   the default did not grant is logged and ignored, never honoured.

`superset ownership plugin describe` lists every hook alongside the class
seams, with its source (`default` / `config` plus the dotted path).

## 2. Keep writing the tuple vocabulary

Your identity/group sync already writes into the OpenFGA store we operate
for access decisions. Keep writing exactly this shape, and keep it current:

| Tuple | Meaning |
|---|---|
| `user:<memberGUID> member tenant:<tenantGUID>` | tenant membership (unchanged) |
| `user:<memberGUID> member group:<groupID>` | group membership (unchanged) |
| `group:<parentID>#member member group:<childID>` | nested group membership, if you have nested groups |
| `user:<memberGUID> member group:tenant_administrator_<tenantGUID>` | who administers a tenant, modelled as membership in a specially-named group |
| `tenant:<tenantGUID>#member tenant group:<groupID>` | **new for this PR**: write this when a group is created, even with zero members, so it is enumerable immediately (`OpenFGADirectory.list_groups`'s fast path reads it) |

Events to handle: user joins/leaves a tenant, group created/renamed/deleted,
membership added/removed, admin granted/revoked. A rename is
delete-old-id/create-new-id, not an in-place update -- the display name
lives inside the id.

`OWNERSHIP_GROUP_ID_FORMAT` (`{name}_{tenant}`, our default, or
`{tenant}_{name}`) is one setting on our side; your sync must write
whichever one is configured, consistently -- a mismatch is what
`superset ownership plugin verify`'s `vocabulary/group_ids_parse` check
catches.

## 3. Only if your JIT puts the member id somewhere other than the username

`superset_ownership.identity.DefaultIdentity` reads the member GUID off the
Superset username (falling back to email, then a `local-<id>` placeholder).
If your JIT login instead sets it on a per-user attribute of your own,
subclass `DefaultIdentity` and override the *pair* of methods that need to
change together -- the forward lookup (account -> GUID) and its reverse
(GUID -> account), which `normalize_subject` needs to validate a subject
reference the picker itself hands out:

```python
# identity.py
from superset_ownership.identity import DefaultIdentity

class AttributeIdentity(DefaultIdentity):
    def member_guid(self, user):
        return read_your_attribute(user) or super().member_guid(user)

    def user_for_member_guid(self, guid):
        found = look_up_by_your_attribute(guid)
        # Only accept a reverse match the forward direction agrees with.
        if found is not None and self.member_guid(found) == guid:
            return found
        return super().user_for_member_guid(guid)
```

**Where `read_your_attribute`/`look_up_by_your_attribute` read from is your
call, not ours** -- it is whatever your deployment already carries the
member GUID on. It is *not* FAB's `User.extra_attributes` relationship: on
Superset's stock schema that is `UserAttribute`
(`welcome_dashboard_id`/`avatar_url`/`sessions_invalidated_at`/
`password_must_change`), a fixed set of columns with no generic slot for a
GUID. `identity.py` in this package (`AttributeIdentity`) is the full,
runnable version, backed by a small table this package owns
(`ivanti_pcs_example_member_guid`) purely as a worked example of the
pattern -- your own attribute carrier will differ, and that is expected;
what has to carry over is overriding `member_guid`/`user_for_member_guid`
as a matched pair, and the forward-mapping check on the reverse one.
`tenant_guid`, `display_name` and `normalize_subject` are inherited
unchanged -- override only what needs it.

**`AttributeIdentity`'s own toy attribute table is created by calling
`install()` once, from a CLI context -- never automatically, and never
from a web request** (I-6: an earlier version created it lazily on first
use, which meant the `CREATE TABLE` could run inside the first
access-decision request, or the first `plugin verify` run, that happened
to reach this class). Your own attribute carrier will most likely already
exist and need no equivalent step; this only applies to this package's
worked example.

Installing the table, then setting the attribute for a test account, both
from a `flask shell`:

```python
from ivanti_pcs_example.identity import install, set_member_guid
install()  # once, before first use

from superset import security_manager
u = security_manager.find_user(username="ben")
set_member_guid(u, "<real GUID>")
```

## 4. Only if you want user search beyond Superset's own users

The default `OpenFGADirectory` answers the picker from the store's
`tenant:<guid>#member` tuples, labelled from `ab_user`. If you want a
directory that reads groups (or users) from somewhere else, implement the
`Directory` protocol -- eight read-only methods, listed in the contract §4.3.
`directory.py` in this package (`FixedGroupsDirectory`) is a minimal worked
example: every tenant has exactly two groups, `blue_<tenant>` and
`green_<tenant>`, answered statically, while user search still reads
Superset's own `ab_user` table. A real override only needs to answer the
questions it actually has a different source for.

## 4.5 Function hooks -- the reference file

Section 1b above is the contract's own table -- every `OWNERSHIP_*` hook
setting, its signature, and the default behaviour it stands in for.
`hooks.py` in this package is the reference the contract's §4.5.3 points to:
**every one of the sixteen hooks implemented, each answering the same
question its default answers, but reading it from a different source** --
so a run with all sixteen configured (`config_hooks_example.py`) proves the
hooks are a real seam, not sixteen functions that happen to call the
default's own logic under a new name. This section walks through it hook by
hook: the question, what the default reads, what this file reads instead,
and what a real Ivanti hook would plausibly read.

**One-time step, from a `flask shell` (never a web request -- see the
"install once" note in section 3 above; the same DDL-in-a-request concern
applies here):**

```python
from ivanti_pcs_example.hooks import install, sync_tenants_from_roles
install()                    # creates the two attribute tables below
sync_tenants_from_roles()    # backfills the tenant table from tenant_<guid> roles
```

`install()` creates two small tables: `ivanti_pcs_example_member_guid`
(reused from `identity.py` -- the same table `AttributeIdentity` reads, so
populating it once serves both) and a new one, `ivanti_pcs_example_tenant`
(`user_id` -> `tenant_guid`). `sync_tenants_from_roles()` populates the
tenant table for every account that already holds a `tenant_<guid>` role,
so `tenant_guid` below has something to read for existing accounts from the
moment the config block is switched on.

| hook | the question | the default reads | this file reads instead | Ivanti would likely read |
|---|---|---|---|---|
| `member_guid` | which store identity is this account? | a GUID inside the username or email | the `ivanti_pcs_example_member_guid` table (falling back to `DefaultIdentity`'s own computation -- a GUID in the username/email, else `local-<id>` -- when the account has no row, NOT the bare username: `local-<id>` is the documented shape §4.5.2 promises, a raw username is neither a GUID nor that placeholder) | whatever their JIT login already writes to the account -- a claim, an attribute, a linked-identity table of their own |
| `tenant_guid` | which tenant does this account act in? | the first `tenant_<guid>` FAB role | the new `ivanti_pcs_example_tenant` table (falling back to the same role scan, called on `DefaultIdentity` directly so the fallback itself is not another hook invocation) | a tenant id already carried on the account from JIT provisioning, without needing a role at all |
| `display_name` | what do we call this account in the UI? | `"First Last"` | `"Last, First"` -- deliberately the opposite order, so a screenshot alone proves the hook is live | whatever their own directory's display convention is |
| `is_tenant_administrator` | does this account administer its tenant? | membership of `tenant_administrator_<tenant>` through the Directory seam | a FAB role of our own naming, `neurons_tenant_admin_<tenant>`, checked first; only when the account holds no such role does it fall through to the store group via the Directory seam ("our source first, store second") | a role or claim their identity system already grants, checked before ever asking the store |
| `user_for_member_guid` | which Superset account does this GUID name? | `find_user(username=guid)`, then by email | the reverse of `member_guid`'s table (with the forward-mapping check, §4.2), then the same username/email fallback | the reverse lookup on whatever forward source they configured `member_guid` to read |
| `users_of_tenant` | who can be shared with, in this tenant? | `tenant:<t>#member` tuples, through `OpenFGADirectory` | the identical tuple shape, read through `fga.read_page` directly (one page, capped at 100, per call) | the same store query, or a call into their own user-directory API |
| `groups_of_tenant` | which groups exist in this tenant? | the `group.tenant` fast path (or the member walk), through `OpenFGADirectory` | the identical `group.tenant` Read shape, direct | the same store query, or their own group-management service |
| `members_of_group` | who is in this group? | `group:<id>#member` tuples, nested groups expanded | the same relation, one level, direct (no nested-group expansion -- see the docstring) | the same store query, expanded however deep their model nests |
| `user_in_group` | is this account in this group? | one `check` | the identical `check`, through `fga.check` directly | the same store query |
| `group_exists` | is this a real group? | a tenant tuple, or at least one member | both relations, read directly, capped at one row each | a group registry of their own, or the same store probe |
| `administrators_of_tenant` | who administers this tenant? | members of `group:tenant_administrator_<tenant>` | the union of the `neurons_tenant_admin_<tenant>` role holders (the same source `is_tenant_administrator` checks first) and that same store group, read directly | whichever of the two sources their deployment actually grants from -- this file demonstrates that a hook may need to reconcile more than one |
| `group_id` / `split_group_id` | how is a group id laid out, and taken apart again? | `OWNERSHIP_GROUP_ID_FORMAT` rendered/parsed (default `{name}_{tenant}`) | the identical `{name}_{tenant}` layout, implemented explicitly rather than delegated to the setting's own machinery -- proving the hook, not the format string, is what answers | whatever layout their sync already writes, as long as the pair is exact inverses |
| `group_display_name` | what do we call this group in the UI? | the bare name, underscores kept | title-case with spaces (`dashboard_designer` -> `Dashboard Designer`) | their own naming convention |
| `tenant_administrator_group` | which group's members administer this tenant? | `group:tenant_administrator_<tenant>` | the identical id, built through this file's own `group_id` | usually unchanged, unless their administrator group is named differently |
| `can_manage` | may this caller manage this object's sharing? | admin > owner > tenant administrator > manage permission | the default reason, unchanged, except one deliberate NARROWING: a `manage_permission` holder may not manage a `"private"`-visibility object (documented in the docstring as an example of narrowing -- a hook may only take grants away, never add one the default did not already give) | a narrower rule specific to their own compliance requirements |

**This is a reference, not a performance target.** `users_of_tenant` and
`members_of_group` call `user_for_member_guid` (one to three queries) for
EVERY tuple on the page -- up to 300 queries for a 100-row page at
`limit=3` worth of lookups per row -- and `administrators_of_tenant` scans
`security_manager.get_all_users()` rather than something scoped to the
tenant. Fine for a reference that has to be readable and correct against a
handful of fixture accounts; do not copy this shape into a production hook
for a tenant with thousands of members. A real implementation should batch
the reverse lookup (one query for every GUID on the page, not one per row)
or read a label the source system already carries instead of resolving
through Superset at all.

Every function in `hooks.py` takes its arguments explicitly, never imports
`flask.g`, reads only, and does not catch its own exceptions -- a genuine
DB or store error is left to propagate so `plugin_hooks.call`/
`call_or_default` (never this file) fail-close it, which is also what lets
`superset ownership plugin verify`'s `hooks/*` section see the raise instead
of a hook that swallowed it first.

Wire all sixteen in at once with `config_hooks_example.py`, copy-pasteable
into `superset_config_docker.py`:

```python
OWNERSHIP_MEMBER_GUID = "ivanti_pcs_example.hooks:member_guid"
OWNERSHIP_TENANT_GUID = "ivanti_pcs_example.hooks:tenant_guid"
OWNERSHIP_DISPLAY_NAME = "ivanti_pcs_example.hooks:display_name"
OWNERSHIP_IS_TENANT_ADMINISTRATOR = "ivanti_pcs_example.hooks:is_tenant_administrator"
OWNERSHIP_USER_FOR_MEMBER_GUID = "ivanti_pcs_example.hooks:user_for_member_guid"
OWNERSHIP_USERS_OF_TENANT = "ivanti_pcs_example.hooks:users_of_tenant"
OWNERSHIP_GROUPS_OF_TENANT = "ivanti_pcs_example.hooks:groups_of_tenant"
OWNERSHIP_MEMBERS_OF_GROUP = "ivanti_pcs_example.hooks:members_of_group"
OWNERSHIP_USER_IN_GROUP = "ivanti_pcs_example.hooks:user_in_group"
OWNERSHIP_GROUP_EXISTS = "ivanti_pcs_example.hooks:group_exists"
OWNERSHIP_ADMINISTRATORS_OF_TENANT = "ivanti_pcs_example.hooks:administrators_of_tenant"
OWNERSHIP_GROUP_ID = "ivanti_pcs_example.hooks:group_id"
OWNERSHIP_SPLIT_GROUP_ID = "ivanti_pcs_example.hooks:split_group_id"
OWNERSHIP_GROUP_DISPLAY_NAME = "ivanti_pcs_example.hooks:group_display_name"
OWNERSHIP_TENANT_ADMINISTRATOR_GROUP = "ivanti_pcs_example.hooks:tenant_administrator_group"
OWNERSHIP_CAN_MANAGE = "ivanti_pcs_example.hooks:can_manage"
```

`superset ownership plugin describe` (or `plugins.describe()["hooks"]`)
reports all sixteen as `source: "config"` with these exact dotted paths
once loaded.

## 5. Only if your OpenFGA model uses different id shapes

If your objects or users are keyed differently in the store (e.g.
`dashboard:pcs-<uuid>` instead of `dashboard:<uuid>`), subclass
`OpenFGAAuthorizer` and override its two reference-building hooks --
`_object_ref`/`_from_object_ref` and `_subject_ref`/`_from_subject_ref`.
Nothing else in the authorizer formats a string. `authorizer.py` in this
package (`PrefixedIdAuthorizer`) is the worked example from the contract
(issue #73): objects as `dashboard:pcs-<uuid>`, users as
`user:neurons|<guid>`.

## 6. Run `superset ownership plugin verify` and send us the output

Before cutover, run the 22-check conformance kit against a scratch store you
can self-certify against:

```bash
# seed a scratch store with our fixtures (qa/seed_fga.sh's shapes plus one
# tenant tuple per group)
superset ownership plugin seed-scratch --api-url https://openfga.scratch.example --store <scratch-store-id>

# then, with OWNERSHIP_* pointed at that scratch store:
superset ownership plugin verify --tenant <tenant-guid> --sample-users 5 \
    --object chart:56 --as <a-non-owner-username>
```

`seed-scratch` refuses to run when `--store` is the store this process is
already configured to talk to (`OWNERSHIP_FGA_STORE` or your
`OWNERSHIP_FGA_CONFIG_PROVIDER`) -- it always writes through a one-off
connection built from `--api-url`/`--store`, never through the configured
authorizer, so a typo or a copy-pasted production URL cannot land fixture
tuples over real data.

The `identity` section (`guid_v4`, `tenant_known`, `normalize_subject_roundtrip`)
samples JIT-provisioned users from the *Superset instance* `verify` runs
against -- not from whichever store `--object`/the connection point at. Run
it against a scratch store and those three checks will FAIL for your real
users (they are not members of the scratch store's fixtures); that is not a
defect in your plug-ins. Run `plugin verify --sample-users 0` pointed at a
scratch store to skip that noise (the default is 5, not "unset" --
omitting the flag still samples five users and still FAILs), or run the
full command against the store your instance's own users actually live
in.

Exit 0 means every check passed or warned; exit 1 means at least one check
failed (see the table it prints for which, and why); exit 2 means the
plug-ins did not load, or the store could not be reached, before the rest of
the checks could run. `--json` emits the same report as JSON for a CI gate.
The kit checks, in order: the plug-ins load; the store is reachable and its
model has every relation we require; a scratch tuple round-trips; your tuple
vocabulary (group-tenant tuples, GUID resolution, the admin group, no
cross-tenant member, id-format conformance, and -- if
`OWNERSHIP_MANAGE_PERMISSION` carries a `{tenant}` placeholder -- that it
renders for the tenant `--tenant` names); the directory (tenant
isolation, pagination, not-found-vs-empty, `user_in_group` agreement,
health, latency); identity (GUID v4, tenant known to the store,
`normalize_subject` round-trips); and our own invariants (owner and public
access make zero store calls; a non-owner's decision costs exactly one
`check`) -- the last section is a regression guard on OUR code, run against
YOUR loaded plug-ins, not something you need to do anything about.

We run `plugin verify` again at cutover, against production.

## 7. Cutover to your own store

Pointing a build already running against a scratch/staging store at your
real one -- or moving from our shared events store to one you own -- is one
ordered sequence, not independent steps:

```bash
# 1. Write the model to your store and pin it (prints the model id and the
#    exact OWNERSHIP_FGA_MODEL line to paste into config).
superset ownership fga install-model --store <your-store-id> --pin

# 2. Write the tuple vocabulary this contract expects (section 2 above:
#    tenant members, groups, the nested admin-group relation) and migrate
#    any existing object tuples (owner/viewer/editor) from wherever they
#    live today into the new store, in whatever way you sync group and
#    tenant membership.

# 3. Give every pre-existing object a tenant tuple -- objects created before
#    this cutover (or before ownership was enabled at all) have none, and
#    a tenant administrator's scoping and a tenant purge both depend on it.
superset ownership backfill-tenants

# 4. Realign the store's OWNER tuples with Superset's own ownership_object
#    rows -- a dry run first; it only reports.
superset ownership reconcile

# 5. The dry run's report includes `share_tuples_without_row`: SHARE tuples
#    (viewer/editor) in the store with no mirror row behind them in
#    Superset. `reconcile` never deletes these -- the store is authoritative
#    for shares, so a tuple the mirror has lost is the mirror's problem to
#    look at, not the store's to silently discard (see lifecycle.reconcile's
#    docstring). Go through that list by hand and delete, in the store,
#    whichever of those tuples should no longer exist before continuing.
#    This step is not optional: a stale `viewer` tuple with no mirror row
#    still GRANTS ACCESS to whatever object it names -- the store is
#    authoritative for shares, so Superset's own mirror having lost track of
#    it changes nothing about what the store will allow. Skipping this step
#    is exactly how a decommissioned account or an old share silently opens
#    a Private object on the new store, the same way F-7's own acceptance
#    run found a stale `viewer` tuple opening a Private object to a user who
#    should not have had access.

# 6. With the orphans cleared, write the OWNER-tuple fixes the dry run
#    reported.
superset ownership reconcile --write

# 7. Final gate: fails (non-zero exit) if anything is still wrong -- point
#    it at the new store before calling the cutover done.
superset ownership check
```

Run `plugin verify` (section 6) against the new store too, before and after
this sequence, the same way we do at cutover.

## What you do NOT need to build

No new HTTP API, no new authentication surface, no SCIM service. If you
already have a query/pull API you would like us to use for broader user
search, tell us -- it is an optional add-on (a v2 `HttpDirectory`), not a
requirement.

## Files in this package

| File | What it overrides | Configure with |
|---|---|---|
| `identity.py` (`AttributeIdentity`) | `Identity.member_guid` | `OWNERSHIP_IDENTITY=ivanti_pcs_example.identity:AttributeIdentity` |
| `directory.py` (`FixedGroupsDirectory`) | the whole `Directory` protocol | `OWNERSHIP_DIRECTORY=ivanti_pcs_example.directory:FixedGroupsDirectory` |
| `directory.py` (`Broken`) | (negative fixture: missing `health`) | `OWNERSHIP_DIRECTORY=ivanti_pcs_example.directory:Broken` |
| `authorizer.py` (`PrefixedIdAuthorizer`) | `OpenFGAAuthorizer`'s reference builders | `OWNERSHIP_AUTHORIZER=ivanti_pcs_example.authorizer:PrefixedIdAuthorizer` |
| `fga.py` (`connection`) | the FGA connection provider | `OWNERSHIP_FGA_CONFIG_PROVIDER=ivanti_pcs_example.fga:connection` |
| `hooks.py` | all sixteen §4.5.2 function hooks | see `config_hooks_example.py`, or section 4.5 above |
| `config_hooks_example.py` | (not a class -- the config block for `hooks.py`) | copy its contents into `superset_config_docker.py` |

None of these are wired into a default deployment; each is opt-in, one
config key at a time.

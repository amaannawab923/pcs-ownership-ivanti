# Object ownership & sharing for Preset PCS (PCS-10243)

Take a **stock** Preset PCS image, add the `superset-ownership` wheel and a
rebuilt frontend bundle, and get a PCS image with object ownership &
sharing: private-by-default objects, per-object / per-tenant sharing,
transfer of ownership, authorization in OpenFGA, and a parallel migration
chain that never touches the customer's own Superset schema.

Nothing under `superset/` is modified. The backend is one pip wheel that
wires itself in through Superset's config hooks; the frontend is an overlay
applied before `npm run build`; OpenFGA runs as its own compose project so
it survives every rebuild and teardown of the PCS stack.

## Two ways to use this

**A. Try it, with everything provided.** The `setup.sh` / seed path below
builds the image, starts an OpenFGA of its own, and seeds a two-tenant
world you can log into in about twenty minutes. Nothing to configure and
nothing of yours is touched. Start at [Quick setup](#quick-setup).

**B. Point it at your own OpenFGA.** If you already run OpenFGA and want
this feature against *your* store, your users and your groups, skip the
demo entirely: [Bring your own OpenFGA](#bring-your-own-openfga). You need
the wheel, one config file, and about ten minutes.

**Running Ivanti Neurons?** Your identifier shapes are already confirmed
and already match the defaults. Go straight to [your settings, written
out](#if-you-run-ivanti-neurons-your-settings-written-out) -- it is one
config block and no identity code.

The stock Preset PCS source is never part of this repository -- it is
fetched when you build.

## Bring your own OpenFGA

You need: a Preset PCS (or Superset) build you can add a wheel to, an
OpenFGA you can reach over HTTP, and permission to write an authorization
model into a store there. A fresh store is easiest; an existing store with
your own types in it also works -- step 2 merges rather than replaces.

### 1. Install the wheel and the one config file

```bash
packaging/build-wheel.sh            # -> dist/superset_ownership-*.whl
pip install dist/superset_ownership-*.whl
```

Put `overlay/pythonpath/superset_config_ownership.py` on your PYTHONPATH --
one file, never a directory mounted over an existing PYTHONPATH entry --
and call it from your `superset_config_docker.py`:

```python
from superset_config_ownership import configure
configure(globals())
```

That call is the whole wiring: it registers the feature flag, the access
hooks, the CLI and the module's own migration chain. It reads the settings
below out of the globals you pass it.

The frontend is separate. The backend refuses nothing without it, but the
sharing drawer and the Owner/Sharing columns only exist if you apply
`overlay/frontend/` to your Superset source before `npm run build`. See
[For pcs-ivanti](#for-pcs-ivanti-applying-this-to-your-fips-chainguard-build) for the
exact build-stage placement.

### 2. Configure it

The minimum, in `superset_config_docker.py` **before** `configure(globals())`:

```python
OWNERSHIP_ENABLED = True
OWNERSHIP_AUTHORIZER = "openfga"
OWNERSHIP_DIRECTORY = "openfga"
OWNERSHIP_FGA_API_URL = os.environ["OPENFGA_API_URL"]
OWNERSHIP_FGA_STORE = os.environ["OPENFGA_STORE_ID"]
OWNERSHIP_FGA_MODEL = os.environ.get("OPENFGA_MODEL_ID", "")  # pin it in production
OWNERSHIP_FGA_CREDENTIALS = {"type": "none"}
```

For a store behind a bearer token, replace the last line with:

```python
OWNERSHIP_FGA_CREDENTIALS = {"type": "api_token", "token_env": "OPENFGA_TOKEN"}
```

Secrets are named, never inlined: the module reads the environment variable
you name and re-reads it on every call, so a rotated token is picked up
without a restart and nothing sensitive lands in a config file or a log
line.

`none` and `api_token` are the two credential types this version accepts.
**OIDC client credentials are not built in** -- if your store sits behind
one, supply the whole connection yourself:

```python
OWNERSHIP_FGA_CONFIG_PROVIDER = "my_company.ownership:connection"
```

That callable returns an `FgaConnection` and owns how it was obtained, so
any scheme works. `overlay/pythonpath/ivanti_pcs_example/fga.py` is a
worked example.

Worth setting deliberately:

| Setting | Default | What it decides |
|---|---|---|
| `OWNERSHIP_PUBLIC_SCOPE` | `"tenant"` | Whether "public" means everyone in the object's tenant, or anyone holding the dataset grant (`"instance"`) |
| `OWNERSHIP_GROUP_ID_FORMAT` | `"{tenant}.{name}"` | How a group id is composed. Must match what is in your store |
| `OWNERSHIP_DIRECTORY_GROUP_WALK` | `"auto"` | Set to `"always"` if your store writes no `group -> tenant` tuples (a group id that already carries its tenant needs none). On `auto` every group listing first reads an empty page before falling back to the walk, and `plugin verify` reports WARN |
| `OWNERSHIP_LOOKUP_CACHE_TTL` | `10` | Seconds a row is served from the shared cache. Also the maximum lag before a revoked tenant administrator stops reading |
| `OWNERSHIP_OUTBOX_ENABLED` | `True` | Keep it on: writes are queued and delivered in order, so a store outage never loses a change |
| `OWNERSHIP_MANAGE_PERMISSION` | `None` | Optional role that may manage others' sharing |

### 3. Install the authorization model into your store

```bash
superset ownership fga install-model --pin
```

The `ownership` command group only exists once `OWNERSHIP_ENABLED` is true,
which is why this comes after the config. It uses the connection you just
configured; to aim it somewhere else for one run, pass
`--api-url`, `--store` and `--credentials-env` instead.

It writes five types -- `user`, `tenant`, `group`, `dashboard`, `chart` --
and prints the model id to pin. If your store already has a model, yours is
**merged**, not replaced: your types are left exactly as they are and ours
are added or updated. Re-running when nothing changed is a no-op rather than
a new model version.

The model is also readable as
`overlay/pythonpath/superset_ownership/model/ownership.fga` if you would
rather review it, or install it with your own tooling, before running that
command.

What it says, in short: an object carries `owner`, `editor`, `viewer` and
`tenant`; owning implies editing and editing implies viewing, so those two
are computed rather than stored; and `viewer`/`editor` accept a group's
member set, so sharing with a group is one tuple that stays correct as the
group's membership changes.

### 4. Make your identities resolvable

This is the step that decides whether it works, and the one most likely to
need you. The module has to turn a logged-in Superset user into a subject
your store recognises. Out of the box it assumes:

- the user's **member id** is their Superset username;
- their **tenant** comes from a role named `tenant_<guid>` or
  `Tenant_<guid>_Role`;
- the subject is therefore `user:<tenant>.<member>`;
- a **tenant administrator** holds `admin` on `tenant:<guid>`.

If that already describes your instance, there is nothing to do. **If the
tenant part does not, the mismatch is silent** -- no user resolves to a
tenant, nothing gets stamped, and `check` still answers ok; see [If
`check` says ok but `untenanted_public` lists
everything](#if-check-says-ok-but-untenanted_public-lists-everything) for
how to spot it. Rather than patch the module, you override the seams -- each is a
dotted path to your own callable, and you can replace one without touching
the others:

```python
OWNERSHIP_MEMBER_GUID = "my_company.ownership:member_guid"
OWNERSHIP_TENANT_GUID = "my_company.ownership:tenant_guid"
OWNERSHIP_IS_TENANT_ADMINISTRATOR = "my_company.ownership:is_tenant_administrator"
OWNERSHIP_GROUP_ID = "my_company.ownership:group_id"
OWNERSHIP_SPLIT_GROUP_ID = "my_company.ownership:split_group_id"
```

`overlay/pythonpath/ivanti_pcs_example/` is a complete, working example of
exactly this -- a real deployment's identity, directory, authorizer and
hooks, each one small. Read `hooks.py` first. A hook that raises fails
closed and is logged; it never grants.

### 5. Migrate, then adopt what is already there

```bash
superset ownership db upgrade      # this module's own chain; yours is untouched

python -c "
from superset.app import create_app
app = create_app()
with app.app_context():
    from superset_ownership.backfill import run
    run()
"

superset ownership backfill-tenants
```

The migration creates three tables of its own -- `ownership_object`,
`ownership_share`, `ownership_outbox` -- and modifies nothing of
Superset's. The backfill has no CLI command of its own because it is a
one-time adoption step, not an operation -- it needs an app context, which
is what that snippet builds; `docker/entrypoint-ownership.sh` runs exactly
this and is worth copying into your own entrypoint, so a deploy can never
migrate without adopting. The backfill gives every existing dashboard and
chart an owner (whoever created it) and makes it public, so an instance
that had no ownership yesterday behaves exactly as it did -- nothing
becomes invisible because you installed this.

The chain keeps its own Alembic bookkeeping in `alembic_version_ownership`
and never reads or writes Superset's `alembic_version`, so it runs against
a database carrying years of its own migration history without touching
it. Two things to know before you run it against one:

**Set `OWNERSHIP_DEFAULT_OWNER` before the backfill.** An existing
object's owner is resolved `created_by_fk` -> `changed_by_fk` ->
`OWNERSHIP_DEFAULT_OWNER`. Objects with no recorded creator -- imported
ones, examples, anything that arrived through a migration rather than
through the UI -- otherwise end up public with a **null** owner, which
means "owner unknown": nobody can take them private or share them,
because every management path needs an owner. Set it to a Superset user
id and those objects stay manageable:

```python
OWNERSHIP_DEFAULT_OWNER = 1
```

**The one constraint that touches your schema.** Revision 0003 adds a
foreign key from `ownership_object.owner_user_id` to `ab_user.id`, so a
deleted user leaves objects unowned rather than dangling. `ADD CONSTRAINT`
validates under `SHARE ROW EXCLUSIVE` on `ab_user`, so on PostgreSQL a
login's `last_login` write waits for that scan. It is milliseconds on an
ordinary user table; if yours is very large, migrate in a maintenance
window, or start the `NOT VALID` / `VALIDATE CONSTRAINT` split by hand as
`constraints.py` describes.

### 6. Check it

```bash
superset ownership status                       # is it on, and which backend
superset ownership fga show-model --check       # does the store's model match
superset ownership check                        # is anything inconsistent
superset ownership plugin verify --sample-users 40
```

`check` answering `"ok": true` is the state to keep. If it does not, the
field names say what to look at -- `silently_open`, `missing_object`,
`uuid_divergence`, `orphaned_sentinel`, and the `outbox` block -- and
`superset ownership reconcile --write` repairs the ones that are repairable.

`plugin verify` is the one to run after step 4: it takes real users out of
your directory and reports, per user, whether their identity resolves and
their tenant and groups come back the shape the module expects.

### What working looks like

With the frontend applied: create a chart, open its Sharing drawer, set it
to Private. Another user in your tenant gets a 404 on it -- not a
permission banner, not an empty chart. Share it with them and they see it
within about ten seconds (the outbox drain interval). Take the store
offline and the object stays private: denial never depends on the store
being reachable.

### Proving it without the frontend

The backend stands on its own, and you do not need the sharing drawer to
test it. The module adds these routes under `/api/v1/ownership`:

| Route | Does |
|---|---|
| `GET /charts`, `GET /dashboards` | what this caller may see, with owner and visibility |
| `GET /chart/<id>`, `GET /dashboard/<id>` | one object's ownership, shares and what you may do to it |
| `PUT /chart/<id>/visibility` | `{"visibility": "private" \| "shared" \| "public"}` |
| `POST /chart/<id>/shares` | `{"subject": "...", "role": "viewer" \| "editor"}` |
| `DELETE /chart/<id>/shares/<subject>` | unshare |
| `PUT /chart/<id>/owner` | transfer; `{"subject": null}` releases |
| `POST /chart/<id>/claim` | take ownership |
| `GET /subjects?q=` | the people and groups you may share with |

Every one of them takes a bearer token, writes included -- a JWT needs no
CSRF header, which is what makes the whole thing testable from a shell:

```bash
H=http://localhost:8088
tok() { curl -s $H/api/v1/security/login -H 'Content-Type: application/json' \
        -d "{\"username\":\"$1\",\"password\":\"$2\",\"provider\":\"db\",\"refresh\":false}" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])'; }
OWNER=$(tok the-owner   their-password)
OTHER=$(tok the-other-user  their-password)
A="-H Content-Type:application/json"
```

A worked check, with two users and one chart:

```bash
# as the owner: make it private
curl -X PUT $H/api/v1/ownership/chart/99/visibility \
     -H "Authorization: Bearer $OWNER" $A -d '{"visibility":"private"}'

# as the other user: the ownership detail is a 404, the chart is gone from
# their list, and the data route refuses
curl $H/api/v1/ownership/chart/99 -H "Authorization: Bearer $OTHER"
#  -> {"message":"chart not found"}
curl -X POST $H/api/v1/chart/99/data/ -H "Authorization: Bearer $OTHER"
#  -> 403

# as the owner: shared, then shared with Ben
curl -X PUT  $H/api/v1/ownership/chart/99/visibility \
     -H "Authorization: Bearer $OWNER" $A -d '{"visibility":"shared"}'
curl -X POST $H/api/v1/ownership/chart/99/shares \
     -H "Authorization: Bearer $OWNER" $A -d '{"subject":"user:2"}'
```

Repeat any of it against `/dashboard/<id>/...`; the two halves are the same
shape.

**Sharing requires `shared` first.** A private object refuses a share with
`object is private; make it shared before sharing` -- private means the
owner alone, and the two steps are deliberately separate.

**Getting the subject right** is the one thing that catches people. The
spelling depends on whether your users carry a tenant:

- **With tenants** (roles named `tenant_<guid>` or `Tenant_<guid>_Role`):
  `user:<tenant-guid>.<member-guid>`, and `GET /subjects` hands you exactly
  the string to send.
- **Without tenants** (a plain Superset, no tenant roles): send the bare
  Superset user id -- `user:2`. The response canonicalises it to
  `user:local-2`, and **sending `user:local-2` yourself is refused** with
  `subject does not name a known account`. Send what `GET /subjects`
  returns, or the bare id; never the canonical form by hand.

**Nothing drains the outbox unless something schedules it.** Until then a
share is committed locally and has not reached the store, so the recipient
still cannot see the object. With a worker, set `OWNERSHIP_OUTBOX_CELERY =
True`. Without one, deliver by hand:

```bash
superset ownership outbox drain     # delivered / failed / dead / skipped
superset ownership outbox status    # what is still waiting, and is the store up
```

The module says so itself at boot: *the outbox is ENABLED but no Celery
beat entry runs superset_ownership.outbox.drain*. That warning is worth
believing.

### If `check` says ok but `untenanted_public` lists everything

Read this one carefully: the same output means two opposite things, and
only one of them is fine.

`untenanted_public` is an object that is public but carries no tenant. An
object's tenant is derived from its **owner's tenant role**, so a row can
only be stamped when its owner holds one -- which is why `check` splits
the two cases. A row whose owner *does* have a tenant is listed as
`untenanted_public_repairable` and **fails** the check until
`backfill-tenants` stamps it. A row whose owner has no tenant at all is
listed under `untenanted_public` only and does **not** fail, because
nothing could stamp it.

- **Single-tenant, no tenant roles anywhere.** Every object lands in
  `untenanted_public`, `check` answers `"ok": true`, and that is correct:
  there is no tenant to record. Set `OWNERSHIP_PUBLIC_SCOPE = "instance"`
  and the field stops mattering to you.

- **Multi-tenant, and you expected tenants to be there.** The same output
  now means your users' tenant roles are not being recognised: so
  `backfill-tenants` stamped nothing, `check` still said ok, and
  tenant-scoped "public" cannot tell your tenants apart. Nothing breaks
  loudly; it just is not doing the thing you installed it for.

**How to tell which one you have**, before trusting a green `check`:

```bash
superset ownership plugin verify --sample-users 40
```

It finds its sample by looking for users holding a role named
`Tenant_<guid>_Role` or `tenant_<guid>`. If your roles are named anything
else it finds nobody, and every identity check comes back
`SKIP -- no sample JIT users supplied`. **On a multi-tenant instance, an
identity section that SKIPs is the warning.** The fix is not to rename
your roles: point the module at wherever your tenant actually lives, with
`OWNERSHIP_TENANT_GUID` (and `OWNERSHIP_MEMBER_GUID`, if the member id is
not the username) from step 4. Then re-run `backfill-tenants` and
`check`.

## If you run Ivanti Neurons: your settings, written out

Ivanti confirmed their identifier shapes in writing (PCS-10243). They
match what this module already assumes. **You do not need to write any
identity code.** Copy the block below, fill in the three store values,
and you are configured.

```python
OWNERSHIP_ENABLED = True
OWNERSHIP_AUTHORIZER = "openfga"
OWNERSHIP_DIRECTORY = "openfga"

OWNERSHIP_FGA_API_URL = os.environ["OPENFGA_API_URL"]
OWNERSHIP_FGA_STORE = os.environ["OPENFGA_STORE_ID"]
OWNERSHIP_FGA_MODEL = os.environ["OPENFGA_MODEL_ID"]
OWNERSHIP_FGA_CREDENTIALS = {"type": "none"}   # or api_token, see step 2

OWNERSHIP_PUBLIC_SCOPE = "tenant"              # public means "public in this tenant"
OWNERSHIP_GROUP_ID_FORMAT = "{tenant}.{name}"  # your group ids already look like this
OWNERSHIP_DIRECTORY_GROUP_WALK = "always"      # you write no group -> tenant tuples
OWNERSHIP_DEFAULT_OWNER = 1                    # a real user id; see "old objects" below
```

You do **not** need `OWNERSHIP_MEMBER_GUID`, `OWNERSHIP_TENANT_GUID`,
`OWNERSHIP_IS_TENANT_ADMINISTRATOR`, `OWNERSHIP_GROUP_ID` or
`OWNERSHIP_SPLIT_GROUP_ID`. Those exist for deployments shaped differently
from yours.

### How we work out who someone is

| Thing | Where we read it |
|---|---|
| Member GUID | the Superset **username** |
| Tenant GUID | the user's role, named `Tenant_<tenant-guid>_Role` |
| Person, in the store | `user:<tenant-guid>.<member-guid>` |
| Group, in the store | `group:<tenant-guid>.<group-id>` |
| A group's tenant | read from the front of the group id, not from a tuple |
| Tenant administrator | the `admin` relation on `tenant:<tenant-guid>` |

The rewritten email (`<tenant-guid>_<member-guid>__<original-email>`) is
handled too: if the username is not a GUID, we read the member GUID out of
the email instead. The role is what we use for the tenant.

Groups can contain other groups. We do not flatten them. OpenFGA follows
the chain for us, however deep it goes.

### What we write, and what we never touch

We write only four kinds of tuple, and only on dashboards and charts:
`owner`, `editor`, `viewer` and `tenant`.

We **never** write:

- tenant administrators (`admin` on a tenant) -- your platform owns that,
  we only read it;
- who is in a tenant (`member` on a tenant);
- who is in a group (`member` on a group);
- nested group links.

So nothing this module does can change who your platform says a person is.

### What we add to your authorization model

Running `superset ownership fga install-model --pin` does not replace your
model. It merges:

- `dashboard` and `chart` are **ours**. They are added whole. They are not
  in your store today, so nothing is lost.
- `user`, `tenant` and `group` are **yours**. We never rewrite a relation
  you already define. We only add one you do not have.
- The only thing we might add to yours is `group.tenant`. It is harmless:
  with `OWNERSHIP_DIRECTORY_GROUP_WALK = "always"` we never read it.

We need three relations to already exist in your model: `tenant.member`,
`tenant.admin` and `group.member`. All three are live in yours.

### Old objects, when you attach a database that already has data

Everything that already exists becomes **public** and gets an owner. So
nobody loses access on the day you install this. The owner is worked out
in this order: who created it, then who last changed it, then
`OWNERSHIP_DEFAULT_OWNER`.

That last one matters. Objects that were imported, or came from examples,
often have no recorded creator. Without a default owner they end up public
with **no owner at all**, and then nobody can share them or make them
private, because every one of those actions needs an owner. Set
`OWNERSHIP_DEFAULT_OWNER` to a real user id before you run the backfill.

One migration touches your schema: it adds a foreign key from our table to
`ab_user`. On PostgreSQL that briefly blocks writes to `ab_user` while it
checks existing rows, so logins wait for it. That is milliseconds on a
normal user table. If yours is very large, run the migration in a
maintenance window.

### Two things to know before you hit them

**An empty group cannot be shared with.** We check a group is real by
looking for its members. A group with nobody in it looks like a group that
does not exist, and a share to it is refused. Put someone in the group
first.

**A tenant administrator sees everything in their own tenant.** Private,
shared, public, even objects with no owner. They can also take ownership
or hand it to someone else. They cannot see another tenant's objects.


## Quick setup

**Prerequisites:** Docker Desktop running (BuildKit on, at least 8 GB RAM
for the frontend build), `git`, `curl`, `python3`, and git access to
`preset-io/preset-pcs` (the repo is private; the build clones the tag with
whatever GitHub credentials git already has -- `gh auth setup-git`, a
credential helper, or SSH). Ports 8097, 8199, 8183 and 3010 must be free.

### 1. Clone

```bash
git clone https://github.com/preset-io/pcs-ivanti-object-ownership.git
```

### 2. Build, start, seed

From inside the cloned folder:

```bash
./setup.sh --skip-sync --up && ./scratch/seed-multitenant.sh
```

First run 15-20 minutes (stock PCS v6.1.0.7 is cloned and built, then the
ownership image, then OpenFGA and Superset come up and the two-tenant data
is seeded). It ends with `scratch stack is up: http://localhost:8097`.

Log in at http://localhost:8097 -- password `test1234` for all of them,
`admin` / `admin` for the Superset admin:

| Persona | Username |
|---|---|
| Ada -- tenant A administrator | `3f0a91c7-2d84-4e63-9b15-7c4e8a2f6d31` |
| Ben -- tenant A member | `6c2b48e9-5a71-4f92-8d03-2e9b7c1a4d53` |
| Cleo -- tenant B administrator | `9d7e35a1-8c62-4b04-a7f1-3d5e9b2c8a76` |
| Marcus Chen, Priya Sharma, Elena Rossi ... (tenant A); Omar Farouk, Hannah Berg, Mei Lin ... (tenant B) | see "The seeded world" below |

Other ways to supply the stock source, if the clone cannot authenticate:
`PCS_GIT_URL=git@github.com:preset-io/preset-pcs.git` (SSH),
`PCS_LOCAL_CHECKOUT=<path to a preset-pcs clone>`, or
`PCS_TARBALL_URL=<release tarball URL or file://...>` (with `GITHUB_TOKEN`
for a private URL). Already have a stock PCS image?
`./setup.sh --pcs-image <image> --skip-sync --up` skips the stock build.
A different PCS tag: `./setup.sh --pcs-ref <tag> ...` (default v6.1.0.7).

### 3. First things to try

1. As **Ada**: Charts -> search "Girl Name Cloud" -> Sharing (row action) ->
   **Private** -> Apply. The Sharing column now reads Private.
2. Log out, log in as **Ben**: Dashboards -> "USA Births Names". The
   "Girl Name Cloud" tile keeps its title but shows *You don't have access to
   this chart ... reach out to the chart owner: Ada tenant A*. Every other tile
   renders.
3. Back as **Ada**: same drawer -> **Shared** -> Add users & groups -> pick
   "Ben tenant A" -> Confirm -> Apply. Within ~10 seconds (the outbox drains to
   OpenFGA) Ben's tile renders with data.
4. Log in as **Cleo** (tenant B): she never sees tenant A's objects -- not
   the private or shared ones, and not the public ones either: *public means
   public within the tenant* -- and the same shared datasets show her only
   tenant B's rows.
5. Transfer ownership: in Ada's drawer -> "Transfer ownership" -> pick Ben.
   Ben now owns the chart. Reopen the drawer as Ada: she is told she is not
   the owner, the sharing options are greyed out, and she is offered **Take
   ownership**. A tenant administrator decides who *owns* an object, never
   who *else* sees it -- to change the sharing she takes it first, and the
   Owner column then says so. She does see every object of her own tenant,
   private ones included (otherwise the row to act on would not be there);
   log in as Ben, make the chart Private, and it is still in Ada's list and
   opens for her, while Marcus (a plain member) no longer sees it.

### 4. Complete example, login by login: Ada and Ben and one chart

Chart: **"Girl Name Cloud"** (chart 78), owned by Ada, on the dashboard
**"USA Births Names"**. Both users are in tenant A. Every login is at
http://localhost:8097/login/ ; log out at http://localhost:8097/logout/
before switching.

| # | Log in as | Username / password | Do | You see |
|---|---|---|---|---|
| 1 | **Ada** | `3f0a91c7-2d84-4e63-9b15-7c4e8a2f6d31` / `test1234` | Charts -> search "Girl Name Cloud" | The chart is listed. Owner column: *Ada tenant A*. Sharing column: *Public* |
| 2 | **Ben** | `6c2b48e9-5a71-4f92-8d03-2e9b7c1a4d53` / `test1234` | Charts -> search "Girl Name Cloud"; Dashboards -> "USA Births Names" | The chart is listed for Ben too (Owner *Ada tenant A*); on the dashboard the word-cloud tile renders with data. Public means everyone in the tenant can open it |
| 3 | **Ada** | as above | Charts -> "Girl Name Cloud" -> row action **Sharing** | The drawer opens: *Owner: Ada tenant A*, **Public** selected, "Transfer ownership" link |
| 4 | **Ben** | as above | Same as step 2 | Still visible, still renders -- nothing has changed yet |
| 5 | **Ada** | as above | Charts -> "Girl Name Cloud" -> **Sharing** -> select **Shared** -> do NOT add anyone -> **Apply** | Sharing column now reads *Shared*; the drawer says "Shared with 0 users / 0 groups". Public -> Shared with an empty list is what revokes everyone but the owner |
| 6 | **Ben** | as above | Charts -> search "Girl Name Cloud"; Dashboards -> "USA Births Names" | The chart is **gone from Ben's Charts list**. On the dashboard its tile is a placeholder: *You don't have access to this chart. You do not have access to the chart "Girl Name Cloud" ... To request access, reach out to the chart owner: Ada tenant A*. Opening http://localhost:8097/explore/?slice_id=78 gives no data |
| 7 | **Ada** | as above | Charts -> "Girl Name Cloud" -> **Sharing** | Drawer shows **Shared**, empty share list |
| 8 | **Ada** | (still logged in) | **Add users & groups** -> Users tab -> type "Ben" -> tick **Ben tenant A** -> **Confirm** -> **Apply** | Drawer says "Shared with 1 user"; Sharing column still *Shared*. (Groups tab -> **chart_designer** works the same: Ben is in that group) |
| 9 | **Ben** | as above | Wait ~10 seconds, then Charts -> search "Girl Name Cloud"; Dashboards -> "USA Births Names" | The chart is **back in Ben's list**, and the tile renders with data again |

Repeat 5 -> 6 -> 8 -> 9 as often as you like (remove Ben with the x on his
row in the drawer, Apply; add him back, Apply): the chart disappears and
reappears for Ben each time. To end, Ada sets it back to **Public** -> Apply.

What is happening underneath: each Apply writes or deletes one tuple in
OpenFGA (`user:6c2b48e9-... viewer chart:<uuid>`). A removal takes effect on
Ben's next request; a grant lands after the outbox drains (a few seconds).
Cleo (tenant B, `9d7e35a1-8c62-4b04-a7f1-3d5e9b2c8a76` / `test1234`) never
sees the chart in any of these states. Throughout, `docker exec
pcssetup-superset-1 superset ownership check` stays `ok: true`. This is case
OWN-135 in `qa/manual-test-cases.md`.

The full set of 142 manual cases with expected results, as executed against
this build, is in `qa/manual-test-cases.md` (blank the `Result:` lines for a
clean sheet).

### Handy checks

```bash
docker exec pcssetup-superset-1 superset ownership check      # ok: true, dashboard_chart_patch_installed: true
```
```bash
docker exec pcssetup-superset-1 superset ownership status     # switches, object and share counts
```
```bash
scripts/verify-image.sh pcs-ownership:dev                     # the 11 image checks, any time
```
```bash
curl -s http://localhost:8199/stores | python3 -m json.tool   # the stores OpenFGA holds
```

### Start over

```bash
docker compose -p pcssetup -f docker-compose.pcs-setup.yml -f scratch/docker-compose.scratch.yml down -v && rm -rf .seed-out scratch/.scratch-out
```

OpenFGA and every store in it survive that (it is its own compose project,
`pcsfga`); `openfga/down.sh` removes them too, after asking. Re-running step 2
from there is exactly the from-scratch run recorded in
`scratch/scratch-run.md`.

## The seeded world: two tenants, who owns what

Everything the scratch harness creates is deterministic, so this is what
you get every time. Password is `test1234` for every user; the username is
the Neurons member GUID. The ids are Neurons' own shapes (confirmed by
Ivanti): a person is `user:<tenant guid>.<member guid>` in the store, the
tenant role is `Tenant_<guid>_Role`, a group is `group:<tenant guid>.<local
id>`, and a tenant's administrators hold the `admin` relation on the tenant
object itself (written by the platform, never by this module). Ben is a
chart designer, and chart designers are nested into dashboard designers.

### Tenants, users and groups

```mermaid
flowchart LR
  subgraph A["Tenant A  a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40"]
    direction TB
    ADA["Ada tenant A  (tenant administrator: admin on tenant:A)"]
    BEN["Ben tenant A"]
    MARCUS["Marcus Chen"]
    PRIYA["Priya Sharma"]
    ELENA["Elena Rossi"]
    DAVID["David Okafor"]
    A_OTHERS["Sofia Nguyen, James Patel, Aisha Khan, Tom Muller,\nLena Fischer, Carlos Mendez, Yuki Tanaka, Grace Adeyemi"]
    GA["groups, ids A.name: dashboard_designer, chart_designer,\ndata_analyst, marketing_analytics, finance_reporting,\nengineering_metrics, support_ops, executives"]
  end
  subgraph B["Tenant B  b7f28e5a-9c14-4d6b-a2f0-5e3d8c1a6b92"]
    direction TB
    CLEO["Cleo tenant B  (tenant administrator: admin on tenant:B)"]
    OMAR["Omar Farouk"]
    HANNAH["Hannah Berg"]
    DIEGO["Diego Alvarez"]
    B_OTHERS["Mei Lin, Nadia Haddad, Peter Novak, Ruth Owusu"]
    GB["groups, ids B.name: dashboard_designer, chart_designer,\ndata_analyst, sales_ops, finance_reporting, support_ops, executives"]
  end
  FGA[("OpenFGA\nuser:tenant.member, group:tenant.name, tenant#admin\nowner / editor / viewer tuples")]
  A --- FGA
  B --- FGA
```

A user only ever sees their own tenant: the sharing picker lists tenant
members and groups only, a share to the other tenant's group is refused, and
a private or shared object of tenant A is a 404 for everyone in tenant B.

### Data: shared datasets (row-level security) and exclusive datasets

```mermaid
flowchart TB
  subgraph SHARED["Shared datasets: one table, rows split by tenant_id (RLS)"]
    direction LR
    S1["cleaned_sales_data\nA: deal_size Small/Medium  |  B: the rest\n(Ada sees 2,666 rows, Cleo 157)"]
    S2["birth_names\nA: states A-M  |  B: states N-Z"]
    S3["video_game_sales\nA: platforms A-M  |  B: platforms N-Z"]
    S4["neurons_devices\n200 rows per tenant"]
  end
  subgraph EXA["Exclusive to Tenant A (9 datasets)"]
    direction LR
    EA["FCC 2018 Survey, hierarchical_dataset, bart_lines, long_lat,\nsf_population_polygons, messages, new_members_daily,\nunicode_test, users_channels"]
  end
  subgraph EXB["Exclusive to Tenant B (9 datasets)"]
    direction LR
    EB["wb_health_population, members_channels_2, messages_channels,\nthreads, users, flights, international_sales,\nbirth_france_by_region, project_management"]
  end
  RA["tenant A role"] --> SHARED
  RB["tenant B role"] --> SHARED
  RA --> EXA
  RB --> EXB
```

Datasets are the *data* boundary (Superset RLS and datasource grants, as
Ivanti already runs them). Ownership is the *object* boundary on top:
whoever owns a dashboard or chart decides who can open it, but nobody can be
given a chart whose dataset their tenant cannot read -- a share or transfer
to such a user is refused up front. And the object boundary never crosses the
tenant line: a **public** object is public *within its tenant* (its owner,
the members of its tenant, Superset admins), so a dashboard on a shared
dataset built by Ada is never listed for, or openable by, Cleo -- even
though Cleo can read that dataset's tenant-B rows herself. Every ownership
row carries the object's tenant (mirrored from OpenFGA's `tenant` tuple) and
the rule is decided from that row alone, with no store call.

### Dashboards and charts: who owns what

```mermaid
flowchart LR
  subgraph TENA["Tenant A"]
    direction TB
    ADA["Ada tenant A\n3f0a91c7-…"] --> D7["7 USA Births Names (10 charts)\nbirth_names (shared)"]
    ADA --> D8["8 Video Game Sales (8 charts)\nvideo_game_sales (shared)"]
    ADA --> D10["10 Tenant A Device Compliance (3 charts)\nneurons_devices (shared)"]
    MARCUS["Marcus Chen\n4302756e-…"] --> D1["1 Sales Dashboard (10 charts)\ncleaned_sales_data (shared)"]
    PRIYA["Priya Sharma\n9fcc709c-…"] --> D3["3 Featured Charts (24 charts)\nhierarchical_dataset (A only)"]
    ELENA["Elena Rossi\n58a4685c-…"] --> D6["6 FCC New Coder Survey 2018 (21 charts)\nFCC 2018 Survey (A only)"]
    DAVID["David Okafor\n12fc0874-…"] --> D9["9 deck.gl Demo (6 charts)\nbart_lines, long_lat, sf_population_polygons (A only)"]
    BEN["Ben tenant A\n6c2b48e9-…"] -.->|owns nothing: the recipient in share and transfer tests| D7
  end
  subgraph TENB["Tenant B"]
    direction TB
    CLEO["Cleo tenant B\n9d7e35a1-…"] --> D11["11 Tenant B Device Compliance (3 charts)\nneurons_devices (shared)"]
    OMAR["Omar Farouk\n60f317ea-…"] --> D2["2 Misc Charts (2 charts)\nB-only datasets"]
    HANNAH["Hannah Berg\ne64caf0a-…"] --> D4["4 Slack Dashboard (5 charts)\nmembers_channels_2, messages_channels, threads, users (B only)"]
    DIEGO["Diego Alvarez\na105bfbe-…"] --> D5["5 World Bank's Data (9 charts)\nwb_health_population (B only)"]
  end
```

Every chart on a dashboard is owned by that dashboard's owner (the
attribution script sets both), and every object starts `public` after the
backfill: 11 dashboards and 108 charts owned, one example chart left
unowned on purpose (it has no recoverable creator -- the "claim" scenario).

### The full list

| Persona | Username (GUID) | Tenant | Groups | Owns |
|---|---|---|---|---|
| Ada tenant A | `3f0a91c7-2d84-4e63-9b15-7c4e8a2f6d31` | A -- administrator (`admin` on `tenant:A`) | dashboard_designer | dashboards 7, 8, 10 and their 21 charts |
| Ben tenant A | `6c2b48e9-5a71-4f92-8d03-2e9b7c1a4d53` | A | chart_designer | nothing -- the recipient in share/transfer tests |
| Marcus Chen | `4302756e-b4aa-4938-b599-ed593444aeaf` | A | chart_designer, data_analyst | dashboard 1 Sales Dashboard, 10 charts |
| Priya Sharma | `9fcc709c-1a6b-4882-b8e3-1787afe31417` | A | dashboard_designer, data_analyst | dashboard 3 Featured Charts, 24 charts |
| Elena Rossi | `58a4685c-6e11-4c4f-aad3-444839fc846a` | A | data_analyst | dashboard 6 FCC New Coder Survey, 21 charts |
| David Okafor | `12fc0874-6358-48e8-96cb-6ada75fd8c76` | A | marketing_analytics | dashboard 9 deck.gl Demo, 6 charts |
| Cleo tenant B | `9d7e35a1-8c62-4b04-a7f1-3d5e9b2c8a76` | B -- administrator (`admin` on `tenant:B`) | dashboard_designer | dashboard 11 Tenant B Device Compliance, 3 charts |
| Omar Farouk | `60f317ea-66cb-48a0-8d45-e15699486409` | B | dashboard_designer, sales_ops | dashboard 2 Misc Charts, 2 charts |
| Hannah Berg | `e64caf0a-30fc-4f79-8f5a-e0a64dced4ad` | B | data_analyst | dashboard 4 Slack Dashboard, 5 charts |
| Diego Alvarez | `a105bfbe-3b53-43d6-bc1c-953293a35c54` | B | sales_ops | dashboard 5 World Bank's Data, 9 charts |
| `admin` / `admin` | -- | none (Superset admin) | -- | nothing; sees everything, can manage everything |

The other 12 seeded users (Sofia Nguyen, James Patel, Aisha Khan, Tom
Muller, Lena Fischer, Carlos Mendez, Yuki Tanaka, Grace Adeyemi in A; Mei
Lin, Nadia Haddad, Peter Novak, Ruth Owusu in B) own nothing and exist to
populate the picker and the groups; their GUIDs are printed by
`overlay/qa/seed_directory.py` when it runs.

Good demo pairs: **Ada -> Ben** (same tenant: share, transfer, placeholder
tile), **Ada -> Cleo** (other tenant: never visible, refused as a share
target), **Marcus's Sales Dashboard opened as Ada vs Cleo** (same dashboard,
different rows under RLS).

## The two-step picture

```
        step 1 (yours already,               step 2 (this shell)
         or built by ./setup.sh)
   ┌─────────────────────────┐          ┌──────────────────────────────┐
   │   stock PCS lean image  │  ─────►  │  overlay/frontend bundle +   │  ──►  pcs-ownership:<tag>
   │   (PCS_IMAGE)            │          │  superset-ownership wheel    │
   └─────────────────────────┘          └──────────────────────────────┘
```

Step 1 never changes: it's the PCS `Dockerfile`'s `lean` target, built from
an untouched PCS source tree. Step 2 (`docker/Dockerfile.pcs-ownership`)
rebuilds ONLY the frontend bundle (from the same stock source, with this
overlay's `overlay/frontend/` copied over it), builds the
`superset-ownership` wheel from `overlay/pythonpath/superset_ownership/`
(`packaging/`), and layers the built assets + the wheel (installed into the
image's own venv) + the one-line config shim onto step 1's image. **Nothing
under `/app/superset` is overwritten**: the backend installs as a package
and wires itself in at runtime through Superset's config hooks; the one
place it depends on stock code (the dashboard-tile access marker, a wrap of
`DashboardRestApi._serialize_dashboard_chart`) is pinned to the stock
method's shape and source and asserted at build time and at boot. The stock
image is never modified in place -- a PCS version bump is steps 1 and 2
again, not a merge.

## OpenFGA is its own project

`openfga/docker-compose.openfga.yml` (compose project `pcsfga`) runs OpenFGA
with its own Postgres and volume on the shared network `pcsfga`. The PCS
stack joins that network and reaches it as `http://openfga:8080`; nothing in
`docker compose -p pcssetup down -v` touches it, so stores survive every
image rebuild and every teardown of the stack. `openfga/up.sh` (idempotent;
called by `setup.sh` and `scratch/seed-multitenant.sh`) brings it up and
waits for `/healthz`; `openfga/down.sh` is the only thing that deletes it,
and asks first.

## What's in this shell

| Path | What |
|---|---|
| `overlay/pythonpath/superset_ownership/` | the module: API, hooks, CLI, Alembic chain (`migrations/`), the OpenFGA model (`model/ownership.fga`), settings/plugin loader, the config layer (`configure.py`) and the dashboard-tile wrap (`dashboard_patch.py`). Synced verbatim from client-test main; packaged as the `superset-ownership` wheel by `packaging/build-wheel.sh` (the Dockerfile's `wheel-builder` stage does the same). |
| `packaging/` | `superset-ownership/pyproject.toml` + README (hatchling; tests excluded; migrations/model data files included) and `build-wheel.sh`. Kept outside the synced module directory on purpose -- `sync-from-main.sh` regenerates that directory wholesale. |
| `overlay/pythonpath/superset_config_ownership.py` | a one-line shim re-exporting `superset_ownership.configure.configure`, so the documented `from superset_config_ownership import configure; configure(globals())` keeps working. The only file dropped on `PYTHONPATH` by default. |
| `overlay/pythonpath/ivanti_pcs_example/` | the reference plug-in package (worked examples of `Identity`/`Directory`/`Authorizer`/FGA-connection overrides). NOT wired into any default config. |
| `overlay/frontend/` | new + replaced frontend files, mirroring `superset-frontend/`'s own root (both `src/` and `packages/superset-ui-core/`). Tests included -- cheap, and they're the spec for what "correct" looks like. |
| `overlay/config/superset_config_docker.example.py` | the ONE file the client edits: the plug-in block (values) + a commented hooks block (only touch if the defaults aren't enough), ending in `configure(globals())`. |
| `overlay/qa/` | 11 operator scripts synced from client-test's `qa/` (seeding, scenario setup, parity checks) -- tooling, not part of the image. |
| `MANIFEST` | generated, one line per overlay file: `<kind> <path>`. Never hand-edited. |
| `UPSTREAM_SHA` | sha256, at upstream v6.1.0.7, of every stock file the overlay replaces (the 9 frontend files) plus the 2 reference-only sources -- the drift-guard input. |
| `scripts/fetch-pcs.sh` | materializes the stock PCS source tree into `.pcs-src/` (gitignored): a shallow clone of the tag by default, or from a local checkout or a release tarball URL. |
| `scripts/sync-from-main.sh` | **regenerates** `overlay/`, `MANIFEST`, `UPSTREAM_SHA` from a client-test ref, by diffing it against upstream v6.1.0.7 and sorting every changed path into a known bucket. Fails loudly on an unrecognized path -- see "Upgrade story" below. |
| `scripts/apply-overlay.sh` | drift guard, then copies the overlay onto an extracted PCS source tree (`--check-only` runs just the guard). This is the from-source path -- e.g. for pcs-ivanti's own FIPS Dockerfile; `docker/Dockerfile.pcs-ownership` does the equivalent inline for the two-step path. |
| `scripts/verify-image.sh` | post-build: module + config layer importable, `superset ownership --help` works, the built bundle contains the ownership strings. |
| `docker/Dockerfile.pcs-ownership` | the two-step build (`frontend` stage rebuilds the bundle, `final` stage layers onto `PCS_IMAGE`). |
| `docker/build.sh` | internal helper `setup.sh` calls for the two `docker build`s. |
| `docker/entrypoint-ownership.sh` | wraps the stock entrypoint: runs the stock init when asked, then the ownership migration, then execs the stock command. Idempotent. Applying the ownership migration backfills every existing object: `superset ownership db upgrade` is immediately followed by `superset_ownership.backfill.run()` (every pre-existing chart/dashboard gets an explicit `ownership_object` row, visibility **public**, owner resolved from its own creator -- `created_by_fk` -> `changed_by_fk` -> `OWNERSHIP_DEFAULT_OWNER` -> its tenant's administrator) and `superset ownership backfill-tenants`, THEN `superset ownership check`. This is one step, not a separate operator command that can be forgotten -- a brownfield instance is never left with an ungoverned object between "the migration ran" and "someone remembered to backfill". Nobody within the tenant loses access: visibility is always public here, so the Sharing column reads "Public" for every pre-existing object, and every member of the object's tenant opens it as before; what changes is that a pre-existing object on a shared dataset is no longer readable from the other tenant (public within the tenant). `backfill-tenants` is what stamps the tenant; `check` fails on any public object whose tenant could be stamped but was not. Idempotent on every container start: an object that already has a row is left alone (or, if it was previously left unowned, re-attributed once real history exists -- see `scratch/` for that in practice). |
| `docker/seed.sh` | demo-only: creates a scratch OpenFGA store, installs the model, writes a tiny tenant vocabulary (2 tenants, 4 users -- real GUID v4 strings, not slugs; identity.py requires it), 2 groups. Writes the resolved store/model id to `.seed-out/seed.env` (gitignored). |
| `docker/demo-superset-config-docker.py` | demo-only: the "rest of PCS's own docker config" (`DATABASE_*` -> `SQLALCHEMY_DATABASE_URI`, `CACHE_CONFIG`, `SECRET_KEY`) a real deployment already has, plus a guard that refuses to start rather than silently falling through to the module's shared-store default (see "a trap worth knowing" below) -- then `exec`s the actual `overlay/config/superset_config_docker.example.py` unmodified. |
| `docker-compose.pcs-setup.yml` | demo stack, project name **always** `pcssetup`. |

## Overlay counts (from the last sync)

Generated by `scripts/sync-from-main.sh` against `client-test/main`
(22c6565821) vs upstream `76151beade` (v6.1.0.7) -- the header line of
`MANIFEST` always says which:

```
module (superset_ownership):     65 files  (incl. configure.py, dashboard_patch.py, plugin_hooks.py,
                                             tests, 7 migrations, 2 model, 1 UPDATING.md; shipped as the wheel)
frontend:                        25 files  (13 replace: 10 core files incl. 2 superset-ui-core, 3 tests;
                                             12 new: 11 under src/features/ownership/ + the error-message
                                             component)
backend core:                     0 files  (nothing under superset/ is replaced; sync-from-main.sh refuses a
                                             main that forks it)
reference (ivanti_pcs_example):    8 files  (hooks.py, config_hooks_example.py, ...; NOT wired by default)
qa operator tooling:              11 scripts
---------------------------------------
overlay total:                   109 files
config layer (generated, not in the diff): 2 files (the 1-line superset_config_ownership.py shim, the .example.py)
UPSTREAM_SHA entries:             15  (the 13 frontend replace-files + the 2 reference-only config/entrypoint
                                       sources)
```

The sixteen function-level `OWNERSHIP_*` hook seams (`plugin_hooks.py`,
finer-grained than the four class-level seams `OWNERSHIP_IDENTITY` /
`DIRECTORY` / `AUTHORIZER` / `FGA_CONFIG_PROVIDER`) are listed, commented
out, in `overlay/config/superset_config_docker.example.py`'s hooks block;
`ivanti_pcs_example/hooks.py` implements all sixteen as a worked example.
None are wired by default.

Excluded on purpose (process/research, not delivered): `qa/reviews/*`,
`qa/design/*`, `.github/workflows/ownership-tests.yml`, `demo-ownership.sh`,
root `UPDATING.md`, `docker-compose-light.override.yml`,
`docker/pythonpath_dev/.gitignore`.

Two files are tracked in `UPSTREAM_SHA` for drift **but never copied** onto
the tree: `docker/pythonpath_dev/superset_config_docker_light.py` (the dev
config the wheel's `configure()` mirrors) and
`docker/entrypoints/run-server.sh` (source of the `SERVER_WORKER_AMOUNT`
export `entrypoint-ownership.sh` carries forward -- see below).

## What the client edits: one file

`overlay/config/superset_config_docker.example.py`, renamed to
`superset_config_docker.py` and mounted/copied to the image's PYTHONPATH
(`/app/pythonpath/superset_config_docker.py`, picked up via
`SUPERSET_CONFIG_PATH`). It has two parts:

1. **The plug-in block** -- values only. `OWNERSHIP_ENABLED`,
   `OWNERSHIP_AUTHORIZER` (`"local"` or `"openfga"`), the OpenFGA
   connection, `OWNERSHIP_GROUP_ID_FORMAT`, `OWNERSHIP_PUBLIC_SCOPE`
   (`"tenant"`, the default: public means public within the tenant;
   `"instance"`: Superset's dataset grant alone decides, as before), etc.
   Every other key defaults to reproducing the pre-feature baseline -- an
   empty block is valid.
2. **The hooks block** -- commented out. Only uncomment a line if the
   defaults in (1) don't fit your directory/identity/authorizer shape;
   `overlay/pythonpath/ivanti_pcs_example/README.md` has worked examples
   for each (`AttributeIdentity`, `FixedGroupsDirectory`,
   `PrefixedIdAuthorizer`, a custom FGA connection provider).

The file ends with `from superset_config_ownership import configure;
configure(globals())` -- that one call applies the whole feature layer
(switch derivation, `FEATURE_FLAGS`, `BLUEPRINTS`, hooks wiring, the Celery
outbox opt-in, every `OWNERSHIP_*` default) with the exact same precedence
and defaults as client-test main's own `superset_config_docker_light.py`.
Nothing else in this file needs to change for the common case.

## The upgrade story

1. Bump the PCS ref: `./setup.sh --pcs-ref v6.1.1.0` (or edit
   `PCS_REF` if calling `scripts/fetch-pcs.sh` directly).
2. Re-sync: `scripts/sync-from-main.sh <client-test ref>` regenerates
   `overlay/`, `MANIFEST` and `UPSTREAM_SHA` from the new upstream diff. It
   **fails loudly** if a changed file doesn't fall into a known bucket
   (module / frontend / reference / qa-tooling / config-source), and
   refuses outright if main forks anything under `superset/` -- the wheel
   cannot carry that. Update the bucket/exclude tables in the script (never
   hand-edit `MANIFEST`) when a new path appears.
3. Re-diff only the files `UPSTREAM_SHA` names against the new PCS release
   -- that's the whole "did upstream move under us" surface for the
   frontend, by construction. The backend's one dependency on stock code
   (the dashboard-tile wrap) is checked separately by the build step
   (`assert_stock_file()`) -- see "PCS version bump procedure" below.
4. `scripts/apply-overlay.sh --check-only .pcs-src` (or just re-run
   `./setup.sh`, which does this as step 3) -- fails if any replaced file
   drifted upstream since the overlay was cut.
5. `UPSTREAM_SHA` is regenerated automatically by step 2 above; nothing to
   refresh by hand.

End to end that is one command: `./setup.sh --pcs-ref <new ref> --up`
(with `--skip-sync` to build exactly what is committed).

## What is NOT in this shell

- No OpenFGA **server** for production -- `openfga/` runs one for the demo
  and test stacks only; production points `OWNERSHIP_FGA_API_URL` at
  whatever OpenFGA deployment the customer (or Ivanti) operates.
- No identity provider / SSO. `OWNERSHIP_IDENTITY` assumes a Superset user
  already exists (however that got provisioned -- JIT, SCIM, manual);
  `ivanti_pcs_example.identity` is a worked example of adapting the member
  GUID lookup, not an identity provider.
- The PCS source tree itself. `scripts/fetch-pcs.sh` materializes it into
  the gitignored `.pcs-src/` at build time; it is never committed here.

## Known stock defects (reproduced from the pcs-ivanti spike, if seen)

- **`npm ci` lock drift**: some PCS revisions have `package.json` /
  `package-lock.json` out of sync (`Missing: <pkg> from lock file`).
  `docker/Dockerfile.pcs-ownership`'s `frontend` stage falls back to `npm
  install --no-audit --no-fund` and prints a `WARNING` when this happens --
  this is a **pre-existing upstream condition**, twelve-plus lines before
  the overlay is ever copied in, not something this overlay causes. The
  real fix is resyncing the lock in preset-pcs/client-test.
- **`superset-core` / Python version floor**: not applicable to the v6.1.0.7
  base this shell targets (its `lean` target already copies
  `superset-core` and targets Python 3.11). Relevant only if pcs-ivanti's
  own FIPS base is still pinned below Python 3.11 -- see their own
  Dockerfile, not this one.
- **Postgres driver**: the stock `lean` target does not install one
  (`requirements/base.txt` treats it as the optional `apache-superset
  [postgres]` extra; only the dev image's bootstrap script installs it, at
  container start). `docker/Dockerfile.pcs-ownership`'s `final` stage adds
  `psycopg2-binary` explicitly, labeled as not-really-part-of-the-overlay,
  purely so this shell's own demo compose (a real Postgres) has something
  to connect with. A real client PCS image already talks to production
  Postgres and so already has a driver.

## A trap worth knowing: `OPENFGA_STORE_ID` and shell scope

`fga_connection.py` ships a hardcoded `DEFAULT_STORE_ID` fallback (a real,
shared development store) for exactly the case an unconfigured from-source
checkout wants zero-config access to the team's store. That is correct
there and dangerous here: `OWNERSHIP_FGA_STORE` in the example config reads
`os.environ.get("OPENFGA_STORE_ID", "")`, and a blank value is "not set
here" under `settings.get`'s precedence -- it falls through to that same
shared-store default. `docker compose --profile seed run --rm seed` writes
the demo's own store id to `.seed-out/seed.env`; if you `source` it in one
shell and then run `docker compose up` in a **different** shell (or a
different script invocation -- environment does not cross that boundary),
`OPENFGA_STORE_ID` is unset again and every ownership command silently
targets the shared store instead of the demo's. `docker/demo-superset-
config-docker.py` now refuses to start rather than do that silently
(`RuntimeError` naming exactly what's missing); `./setup.sh --up` sources
`.seed-out/seed.env` and brings up `superset`/`worker` in the same script
run, so it never hits this. Only a concern if you drive the steps by hand.

## For pcs-ivanti (applying this to your FIPS Chainguard build)

Written against `preset-io/pcs-ivanti` as it stands -- its
`docker/Dockerfile.ivanti-superset`, its `superset-oss/` source layout, and
its `docker/entrypoint-ivanti.sh`. Five additions to files you already have.
You do not need `docker/Dockerfile.pcs-ownership` from this repository at
all; that is this shell's own demonstration path, and its `uv`/`/app/.venv`
assumptions do not apply to your image.

**Python 3.11 or newer is required** -- the wheel declares
`requires-python = ">=3.11"` and pip refuses it on 3.10. Ivanti has
confirmed the FIPS base moves to 3.11.

### 1. The frontend overlay, before your `npm run build`

In the `superset-node` stage, immediately after the line that copies the
frontend source in:

```dockerfile
COPY ./superset-oss/superset-frontend /app/superset-frontend
COPY ./pcs-ownership/overlay/frontend/ /app/superset-frontend/     # <-- add
```

It overlays `src/` and `packages/superset-ui-core/`; nothing under
`superset/` is touched. `scripts/apply-overlay.sh <tree>` does the same
copy with a drift guard if you would rather run it against your extracted
`superset-oss/` before the build.

### 2. The wheel, in the BUILDER stage -- not the final one

Your `final` stage deliberately removes pip, setuptools and wheel for FIPS,
so nothing can be installed there. Install into `superset-builder`, whose
`site-packages` the final stage already copies across:

```dockerfile
# superset-builder, after `pip install --no-cache-dir -e .`
COPY ./pcs-ownership/dist/superset_ownership-0.6.0-py3-none-any.whl /tmp/
RUN pip install --no-cache-dir --no-deps /tmp/superset_ownership-0.6.0-py3-none-any.whl \
 && rm /tmp/superset_ownership-0.6.0-py3-none-any.whl
```

`--no-deps` because your requirements are already resolved and pinned, and
everything the wheel imports is in them. Build the wheel with
`packaging/build-wheel.sh`; it is pure Python, no compiled extensions, so
it carries no FIPS surface of its own.

### 3. The config shim, one file on PYTHONPATH

Also in `superset-builder`, so your existing
`COPY --from=superset-builder ${SUPERSET_HOME} ${SUPERSET_HOME}` carries it:

```dockerfile
COPY ./pcs-ownership/overlay/pythonpath/superset_config_ownership.py \
     ${SUPERSET_HOME}/pythonpath/superset_config_ownership.py

# fail the build on drift rather than at runtime: this reads the installed
# stock file and refuses if your PCS version changed the method the wheel
# wraps. No app, no database needed.
RUN python3 -c "from superset_ownership.dashboard_patch import assert_stock_file; assert_stock_file()"
```

A single **file**, never a directory copied or mounted over
`${SUPERSET_HOME}/pythonpath` -- a directory-level mount hides everything
already there, which is the trap the earlier pcs-ivanti spike hit. And do
not also copy `superset_ownership/` onto PYTHONPATH: it would shadow the
installed wheel.

Then call it from your own `superset_config_docker.py`, after your settings:

```python
from superset_config_ownership import configure
configure(globals())
```

### 4. The migration, in your entrypoint

`docker/entrypoint-ivanti.sh` already runs `superset db upgrade` and
`superset init`. The ownership chain is separate and must run with them --
otherwise an upgraded image serves requests against a schema that has not
caught up:

```bash
superset db upgrade          # yours, unchanged
superset ownership db upgrade # this module's own chain; touches nothing of yours
superset init                # yours, unchanged
superset ownership check     # refuses to start quiet if something is inconsistent
```

On the **first** deploy only, adopt what is already in the database --
every existing dashboard and chart gets an owner and becomes public, so
nothing a user could see yesterday disappears today:

```bash
python -c "
from superset.app import create_app
app = create_app()
with app.app_context():
    from superset_ownership.backfill import run
    run()
"
superset ownership backfill-tenants
```

`docker/entrypoint-ownership.sh` in this repository is a worked version of
exactly this, including its exit-code handling; it is worth reading before
you write your own.

### 5. The outbox drain, on your worker

Your Celery worker and beat images need the same wheel and the same shim.
Sharing changes are committed locally and delivered to OpenFGA by a
background drain -- without it they are written and never arrive. You do
not hand-write the schedule; set

```python
OWNERSHIP_OUTBOX_CELERY = True          # needs a redis broker
OWNERSHIP_OUTBOX_DRAIN_SECONDS = 10.0   # optional, this is the default
```

and `configure()` registers the beat entry (`superset_ownership.outbox.drain`)
for you.

**One thing to check before you enable it:** with `OWNERSHIP_OUTBOX_CELERY`
on, `configure()` **assigns `CELERY_CONFIG`** -- it does not merge. If your
own `superset_config_docker.py` already defines `CELERY_CONFIG`, yours is
replaced and your own schedules and broker settings are lost. If you have
one, leave `OWNERSHIP_OUTBOX_CELERY` off and add the entry to your own
config instead:

```python
class CeleryConfig:                      # yours, with one addition
    imports = ("superset_ownership.outbox", ...)
    beat_schedule = {
        ...,
        "superset_ownership.outbox.drain": {
            "task": "superset_ownership.outbox.drain",
            "schedule": 10.0,
            "options": {"expires": 10.0},
        },
    }
```

`expires` matters: a tick that could not run inside one interval should be
dropped, not queued, so a worker outage does not replay hundreds of drains
when it comes back.

If you run no worker at all, `superset ownership outbox drain` delivers
pending rows by hand, and `superset ownership outbox status` shows what is
waiting.

### Optional

`overlay/pythonpath/ivanti_pcs_example/` is the reference hook package --
identity, directory, authorizer and config-provider implementations for a
deployment shaped like yours. Copy it as a second individual pythonpath
entry for a scratch or dev image; it is not part of a default deployment.

### PCS version bump procedure

1. `scripts/fetch-pcs.sh <new-ref>`; `scripts/apply-overlay.sh --check-only
   .pcs-src` -- the frontend drift guard says which replace-files moved.
2. Build. The wheel's build-time `assert_stock_file()` fails if
   `_serialize_dashboard_chart` changed. If it did: read the new stock
   method, update the restated body in `dashboard_patch.install()` to
   match, re-derive the two pins from the real method -- inside the new
   image, `superset shell` then
   `from superset.dashboards.api import DashboardRestApi; from superset_ownership.dashboard_patch import _pins_for; _pins_for(DashboardRestApi._serialize_dashboard_chart)`
   (the import needs an initialised app, which `superset shell` provides;
   `_pins_from_file()` gives the same pair from the file with no app) --
   never hand-typed; run
   `test_dashboard_patch.py` (it asserts the pins equal the live method and
   that the wrapper equals the stock method differentially), bump the wheel
   version.
3. `scripts/verify-image.sh <image>` then `scratch/seed-multitenant.sh`.

## Build and prove it

Run `./setup.sh --pcs-ref v6.1.0.7 --up` end to end, or drive the steps
individually -- see each script's own `--help`/header comment. Results from
the run this delivery was built and verified against are pasted in the PR /
delivery notes, not duplicated here (they'd go stale the next time
`sync-from-main.sh` runs).

The demo compose stack above starts from an EMPTY instance (one admin user,
`load_examples` off by default). `scratch/` goes further: it seeds a
**brownfield** instance shaped like a real Ivanti multi-tenant deployment
(two tenants, JIT-shaped users, `load_examples`' content attributed to real
tenant users, tenant-bound datasets with RLS) on a separate database and
OpenFGA store within this same `pcssetup` compose project, so the backfill
described above can be exercised against something that looks like an
existing customer instance instead of a fresh one. See `scratch/README.md`.

## Not run here

This shell does not run `superset_ownership`'s own pytest suite -- that is
covered on `client-test main`. `verify-image.sh` and the compose proof are
about the delivery mechanism (does the overlay actually land and wire up in
a *stock* image), not the module's own correctness.

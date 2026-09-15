# Monitoring OpenFGA through Superset SQL Lab (`fga-superset`, port 8090)

A standalone, stock Superset that queries the scratch OpenFGA store **read-only**
through SQL Lab. It exists so a client can watch the authorization store live
without touching either PCS stack. Nothing in it is part of the deliverable.

## What is running

| Piece | Value |
|---|---|
| Container | `fga-superset` — `apache/superset:6.0.0`, started with `docker run` (no compose project, no bind mounts) |
| URL / login | http://localhost:8090 — `admin` / `admin` |
| Network | joined to `pcssetup_default` (PCS metadata Postgres) and `pcsfga` (the standalone OpenFGA project's network: `docker network connect pcsfga fga-superset`), so it reaches `postgres` and `fga-postgres` by service name |
| Metadata DB | SQLite inside the container (`/app/superset_home/superset.db`). Survives `docker stop/start`; **`docker rm` loses the connections and saved tabs** — do not remove it |
| Extra package | `psycopg2-binary` installed into `/app/.venv` (stock image lacks it) |
| Do not touch | the PCS stacks (`pcssetup-*` on :8097, `pcs617-*` on :8096/:9006) and the other compose projects on this machine |

OpenFGA's datastore lives in the STANDALONE OpenFGA project (`pcsfga`,
`openfga/docker-compose.openfga.yml`): container `pcsfga-fga-postgres-1`,
database `openfga`, superuser `fga`/`fga`. It survives every `down -v` of the
PCS stack. The PCS metadata database (`superset_scratch`) lives in
`pcssetup-postgres-1`, which also joins the `pcsfga` network so dblink can
reach `fga-postgres`.

## Read-only roles (created once, SELECT only, no superuser)

```sql
-- in database openfga, on pcsfga-fga-postgres-1 (psql -U fga -d openfga)
CREATE ROLE fga_reader LOGIN PASSWORD 'fga_reader';
GRANT CONNECT ON DATABASE openfga TO fga_reader;
GRANT USAGE ON SCHEMA public TO fga_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO fga_reader;

-- in database superset_scratch (the pcs-setup PCS metadata DB)
CREATE ROLE pcs_reader LOGIN PASSWORD 'pcs_reader';
GRANT CONNECT ON DATABASE superset_scratch TO pcs_reader;
GRANT USAGE ON SCHEMA public TO pcs_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO pcs_reader;
```

## Database connections in fga-superset (both `expose_in_sqllab`, `allow_dml = false`)

| id | Name | URI |
|---|---|---|
| 1 | OpenFGA store (read-only) | `postgresql+psycopg2://fga_reader:fga_reader@fga-postgres:5432/openfga` |
| 2 | PCS metadata + OpenFGA (read-only) | `postgresql+psycopg2://pcs_reader:pcs_reader@postgres:5432/superset_scratch` |

Connection 1 shows the raw OpenFGA tables (`store`, `authorization_model`,
`tuple`, `changelog`, `assertion`). Connection 2 is where the joined
"Neurons" tables run, because it can see both the FGA tuples (via dblink) and
Superset's `ab_user`.

## Bridge views in `superset_scratch` (dblink → openfga)

dblink from a non-superuser can fail with `password or GSSAPI delegated
credentials required` (Postgres refuses a non-superuser dblink unless the
password was actually used to authenticate). The views therefore go through
`SECURITY DEFINER` functions owned by the `superset` superuser; `pcs_reader`
only gets EXECUTE/SELECT.

```sql
CREATE EXTENSION IF NOT EXISTS dblink;

CREATE OR REPLACE FUNCTION fga_tuple_fn()
RETURNS TABLE(store text, object_type text, object_id text, relation text,
              subject text, user_type text, inserted_at timestamptz)
LANGUAGE sql SECURITY DEFINER STABLE AS $$
  SELECT * FROM dblink('dbname=openfga host=fga-postgres user=fga_reader password=fga_reader',
    'select store, object_type, object_id, relation, _user, user_type, inserted_at from tuple')
  AS t(store text, object_type text, object_id text, relation text,
       subject text, user_type text, inserted_at timestamptz)
$$;

CREATE OR REPLACE FUNCTION fga_changelog_fn()
RETURNS TABLE(store text, object_type text, object_id text, relation text,
              subject text, operation int, inserted_at timestamptz)
LANGUAGE sql SECURITY DEFINER STABLE AS $$
  SELECT * FROM dblink('dbname=openfga host=fga-postgres user=fga_reader password=fga_reader',
    'select store, object_type, object_id, relation, _user, operation, inserted_at from changelog')
  AS t(store text, object_type text, object_id text, relation text,
       subject text, operation int, inserted_at timestamptz)
$$;

CREATE OR REPLACE VIEW fga_tuple     AS SELECT * FROM fga_tuple_fn();
CREATE OR REPLACE VIEW fga_changelog AS SELECT * FROM fga_changelog_fn();
GRANT EXECUTE ON FUNCTION fga_tuple_fn(), fga_changelog_fn() TO pcs_reader;
GRANT SELECT ON fga_tuple, fga_changelog TO pcs_reader;
```

`changelog.operation`: `0` = write, `1` = delete.

**Known limit:** the dblink body has no WHERE, so every query pulls the whole
`tuple` table across the link before filtering. Fine for the scratch store
(~1,300 tuples); it would not survive a production-sized store — see "Is this
what production looks like?" below.

## The store being watched

| | |
|---|---|
| Store id | `01M2F234EV0AE8KZN18Z1SZ628` (`pcs-scratch`, from the from-scratch run on `pcs-ownership:pip`; older store ids in these notes refer to the previous, destroyed instance -- `select store, count(*) from fga_tuple group by 1` lists what exists now) |
| Tenants | A = `a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40`, B = `b7f28e5a-9c14-4d6b-a2f0-5e3d8c1a6b92` |
| Tuple counts at time of writing | chart owner 427 · chart tenant 427 · chart viewer 4 · dashboard owner 42 · dashboard tenant 42 · dashboard viewer 1 · group member 165 · group tenant 89 · tenant member 110 |

## SQL Lab tabs (connection 2)

### Neurons groups — one row per group, members named

```sql
WITH members AS (
  SELECT split_part(t.object_id, '_' || right(t.object_id, 36), 1) AS group_name,
         right(t.object_id, 36)                                    AS tenant_guid,
         replace(t.subject, 'user:', '')                            AS member_guid
  FROM fga_tuple t
  WHERE t.store = '01M2F234EV0AE8KZN18Z1SZ628'
    AND t.object_type = 'group' AND t.relation = 'member' AND t.user_type = 'user'
)
SELECT CASE m.tenant_guid
         WHEN 'a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40' THEN 'Tenant A'
         WHEN 'b7f28e5a-9c14-4d6b-a2f0-5e3d8c1a6b92' THEN 'Tenant B'
         ELSE m.tenant_guid END AS tenant,
       m.group_name AS "group",
       count(*) AS members,
       string_agg(coalesce(u.first_name || ' ' || u.last_name, m.member_guid), ', ' ORDER BY u.first_name) AS who
FROM members m
LEFT JOIN ab_user u ON u.username = m.member_guid
GROUP BY 1, 2 ORDER BY 1, 2
```
Result: 17 rows (9 groups in Tenant A, 8 in Tenant B).

### Neurons users — one row per user with user id and tenant id

```sql
SELECT replace(t.subject, 'user:', '')                                   AS user_id,
       t.object_id                                                       AS tenant_id,
       CASE t.object_id
         WHEN 'a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40' THEN 'Tenant A'
         WHEN 'b7f28e5a-9c14-4d6b-a2f0-5e3d8c1a6b92' THEN 'Tenant B'
         ELSE t.object_id END                                            AS tenant_name,
       coalesce(u.first_name || ' ' || u.last_name, '(not in Superset)') AS display_name,
       t.subject                                                         AS fga_user,
       'tenant:' || t.object_id                                          AS fga_object,
       t.inserted_at                                                     AS written_at
FROM fga_tuple t
LEFT JOIN ab_user u ON u.username = replace(t.subject, 'user:', '')
WHERE t.store = '01M2F234EV0AE8KZN18Z1SZ628'
  AND t.object_type = 'tenant' AND t.relation = 'member' AND t.user_type = 'user'
ORDER BY tenant_name, display_name
```
Result: 22 rows (14 in Tenant A, 8 in Tenant B).

### Users → tenant admin flag, groups, ownership counts (tested, not yet a tab)

```sql
WITH t AS (SELECT * FROM fga_tuple WHERE store = '01M2F234EV0AE8KZN18Z1SZ628'),
tenant_of AS (
  SELECT replace(subject,'user:','') AS member_guid, object_id AS tenant_guid
  FROM t WHERE object_type='tenant' AND relation='member' AND user_type='user'),
groups_of AS (
  SELECT replace(subject,'user:','') AS member_guid,
         string_agg(split_part(object_id, '_' || right(object_id,36), 1), ', ' ORDER BY object_id) AS groups,
         bool_or(object_id LIKE 'tenant_administrator_%') AS is_tenant_admin
  FROM t WHERE object_type='group' AND relation='member' AND user_type='user' GROUP BY 1),
owns AS (
  SELECT replace(subject,'user:','') AS member_guid,
         count(*) FILTER (WHERE object_type='dashboard') AS dashboards_owned,
         count(*) FILTER (WHERE object_type='chart')     AS charts_owned
  FROM t WHERE relation='owner' AND user_type='user' GROUP BY 1)
SELECT CASE tenant_of.tenant_guid
         WHEN 'a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40' THEN 'Tenant A'
         WHEN 'b7f28e5a-9c14-4d6b-a2f0-5e3d8c1a6b92' THEN 'Tenant B'
         ELSE coalesce(tenant_of.tenant_guid,'(no tenant tuple)') END AS tenant,
       coalesce(u.first_name||' '||u.last_name, tenant_of.member_guid) AS "user",
       tenant_of.member_guid AS member_guid,
       CASE WHEN coalesce(g.is_tenant_admin,false) THEN 'yes' ELSE '' END AS tenant_admin,
       coalesce(g.groups,'') AS groups,
       coalesce(o.dashboards_owned,0) AS dashboards_owned,
       coalesce(o.charts_owned,0) AS charts_owned,
       CASE WHEN u.active THEN 'active' WHEN u.id IS NULL THEN 'not in Superset' ELSE 'deactivated' END AS superset_status
FROM tenant_of
LEFT JOIN groups_of g ON g.member_guid = tenant_of.member_guid
LEFT JOIN owns o      ON o.member_guid = tenant_of.member_guid
LEFT JOIN ab_user u   ON u.username    = tenant_of.member_guid
ORDER BY 1, tenant_admin DESC, 2
```

### Handy raw queries (connection 1, `openfga` database)

```sql
-- what changed in the last hour, newest first
SELECT inserted_at, CASE operation WHEN 0 THEN 'write' WHEN 1 THEN 'delete' END AS op,
       object_type || ':' || object_id AS object, relation, _user AS subject
FROM changelog WHERE store = '01M2F234EV0AE8KZN18Z1SZ628'
  AND inserted_at > now() - interval '1 hour' ORDER BY inserted_at DESC;

-- who can see a given dashboard (direct tuples only; usersets are not expanded here)
SELECT relation, _user FROM tuple
WHERE store = '01M2F234EV0AE8KZN18Z1SZ628' AND object_type = 'dashboard' AND object_id = '<dashboard uuid>';
```

Command-line alternative that works against any OpenFGA (including hosted
ones, where there is no database to connect to): `fga tuple read --store-id
01M2F234EV0AE8KZN18Z1SZ628 --api-url http://localhost:8199` (`brew install
openfga/tap/fga`).

## Is this what production OpenFGA looks like? — no, and why

What matches: the tuple vocabulary is ours by contract (`user:`, `tenant:`,
`group:`, `owner`/`viewer`/`tenant`), and the datastore schema is the real
OpenFGA one.

What does not, ordered by how much it would bite in a client demo:

1. **Scale.** 2 tenants / 22 users / 17 groups here; Neurons is thousands of
   tenants and six-figure users, millions of tuples. The dblink views pull the
   whole `tuple` table per query; a production version needs pushdown
   (parameterised dblink or `postgres_fdw` foreign tables) and filtering on the
   store side.
2. **Directory tuples may not be in the store at all.** These tables exist
   because the scratch stack runs `OWNERSHIP_DIRECTORY=openfga`. Under the
   function-hooks configuration built for Ivanti (`USERS_OF_TENANT`,
   `MEMBERS_OF_GROUP`, `TENANT_GUID`, …answered from Neurons), the store holds
   only ownership and share tuples, and the "Neurons users/groups" queries
   return 0 rows — correctly.
3. **Group id format is a placeholder.** `group:<name>_<tenant_guid>` and the
   `split_part / right(…, 36)` parsing are ours; the `GROUP_ID` /
   `SPLIT_GROUP_ID` hooks exist because Neurons' real group ids are unknown.
4. **Display names are not in OpenFGA.** They come from Superset `ab_user`
   here; in production most Neurons users never log into PCS, so most rows
   would read "(not in Superset)". Names come from the `DISPLAY_NAME` hook.
5. **Under-represented shapes.** Few userset tuples (`group:X#member` as the
   subject of a share), no soft-deleted users, no unclaimed owners.
6. **Access.** This works because we own the OpenFGA Postgres. If Ivanti hosts
   the store, the inspection path is the OpenFGA Read API (`fga` CLI, the
   module's `plugin describe` / verify commands), not SQL — unless they expose
   a read-only replica.

Decisions to get from Ivanti before promising any monitoring: which directory
mode they run (tuples in FGA vs hooks), and who hosts the store.

## Lifecycle

```bash
docker stop fga-superset    # pause without losing anything
docker start fga-superset   # resume; log in again at :8090
```
Never `docker rm fga-superset` — the SQLite metadata (connections, tabs) is
inside the container. The Postgres roles/views live in `pcssetup-postgres-1`
and survive independently.

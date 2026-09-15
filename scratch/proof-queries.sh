#!/usr/bin/env bash
# Proof queries against the scratch stack -- run standalone any time after
# scratch/seed-multitenant.sh has completed, or as its own last step.
#
# Tenant is not a column on ownership_object (it lives in the OpenFGA
# store, on the object's own `tenant` tuple); the ownership-side queries
# derive "owner's tenant" from the owner's Superset role (`tenant_<guid>`,
# one per seeded user -- see overlay/qa/seed_identity.py /
# seed_directory.py), which is the same fact the backfill itself derives
# ownership from. The RLS-side queries read the actual row_level_security_
# filters/subjects tables scratch/seed_tenant_data.py wrote -- ground
# truth, not a re-derivation.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

PSQL="docker exec -i pcssetup-postgres-1 psql -U superset -d superset_scratch -v ON_ERROR_STOP=1"
PSQL_EX="docker exec -i pcssetup-postgres-1 psql -U superset -d examples_scratch -v ON_ERROR_STOP=1"
COMPOSE="docker compose -p pcssetup -f docker-compose.pcs-setup.yml -f scratch/docker-compose.scratch.yml"

echo "--- ownership_object counts by visibility ---"
$PSQL -c "SELECT visibility, count(*) FROM ownership_object GROUP BY visibility ORDER BY visibility;"

echo "--- ownership_object counts by owner's tenant (derived from the owner's tenant_<guid> role) ---"
# NOTE: the tenant-role join is a scalar subquery, not a plain LEFT JOIN --
# ab_user_role has one row per role a user holds (Gamma AND tenant_<guid>),
# and joining ab_role straight off ab_user_role would emit one output row
# per role (double-counting every object). The subquery picks the (exactly
# one, by design) tenant_% role per user first.
$PSQL -c "
SELECT
  COALESCE(tr.tenant, CASE WHEN o.owner_user_id IS NULL THEN '(unowned)' ELSE '(no tenant role, e.g. admin)' END) AS tenant,
  count(*) AS objects
FROM ownership_object o
LEFT JOIN ab_user u ON u.id = o.owner_user_id
LEFT JOIN LATERAL (
  SELECT substring(r.name from 'tenant_(.*)') AS tenant
  FROM ab_user_role ur JOIN ab_role r ON r.id = ur.role_id
  WHERE ur.user_id = u.id AND r.name LIKE 'tenant\_%' ESCAPE '\\'
  LIMIT 1
) tr ON true
GROUP BY 1
ORDER BY 1;
"

echo "--- every dashboard: owner + tenant + visibility ---"
$PSQL -c "
SELECT
  d.id,
  d.dashboard_title,
  COALESCE(u.first_name || ' ' || u.last_name, '(unowned)') AS owner,
  COALESCE(tr.tenant, '-') AS owner_tenant,
  o.visibility
FROM dashboards d
LEFT JOIN ownership_object o ON o.asset_type = 'dashboard' AND o.object_id = d.id
LEFT JOIN ab_user u ON u.id = o.owner_user_id
LEFT JOIN LATERAL (
  SELECT substring(r.name from 'tenant_(.*)') AS tenant
  FROM ab_user_role ur JOIN ab_role r ON r.id = ur.role_id
  WHERE ur.user_id = u.id AND r.name LIKE 'tenant\_%' ESCAPE '\\'
  LIMIT 1
) tr ON true
ORDER BY d.id;
"

echo "--- per dashboard: datasets its charts use, and which tenant(s) can read them (via RLS binding) ---"
$PSQL -c "
SELECT
  d.id AS dashboard_id,
  d.dashboard_title,
  tb.table_name AS dataset,
  COALESCE(string_agg(DISTINCT substring(r.name from 'tenant_(.*)'), ', ' ORDER BY substring(r.name from 'tenant_(.*)')), '(no RLS binding)') AS readable_by_tenant
FROM dashboards d
JOIN dashboard_slices ds ON ds.dashboard_id = d.id
JOIN slices sl ON sl.id = ds.slice_id
JOIN tables tb ON tb.id = sl.datasource_id
LEFT JOIN rls_filter_tables ft ON ft.table_id = tb.id
LEFT JOIN rls_filter_subjects fs ON fs.rls_filter_id = ft.rls_filter_id
LEFT JOIN subjects s ON s.id = fs.subject_id
LEFT JOIN ab_role r ON r.id = s.role_id
GROUP BY d.id, d.dashboard_title, tb.table_name
ORDER BY d.id, tb.table_name;
"

echo "--- RLS filter table: dataset, role, clause ---"
$PSQL -c "
SELECT
  tb.table_name AS dataset,
  r.name AS role,
  f.clause
FROM row_level_security_filters f
JOIN rls_filter_tables ft ON ft.rls_filter_id = f.id
JOIN tables tb ON tb.id = ft.table_id
JOIN rls_filter_subjects fs ON fs.rls_filter_id = f.id
JOIN subjects s ON s.id = fs.subject_id
JOIN ab_role r ON r.id = s.role_id
ORDER BY tb.table_name, r.name;
"

echo "--- RLS filter count per tenant role ---"
$PSQL -c "
SELECT r.name AS tenant_role, count(DISTINCT f.id) AS rls_filters
FROM ab_role r
JOIN subjects s ON s.role_id = r.id
JOIN rls_filter_subjects fs ON fs.subject_id = s.id
JOIN row_level_security_filters f ON f.id = fs.rls_filter_id
WHERE r.name LIKE 'tenant\_%' ESCAPE '\\'
GROUP BY r.name
ORDER BY r.name;
"

echo "--- alembic_version vs alembic_version_ownership ---"
$PSQL -c "SELECT 'core' AS chain, version_num FROM alembic_version
UNION ALL
SELECT 'ownership' AS chain, version_num FROM alembic_version_ownership;"

echo "--- cleaned_sales_data: count(*) by tenant_id (real row-level tenancy, examples_scratch DB) ---"
$PSQL_EX -c "SELECT tenant_id, count(*) FROM cleaned_sales_data GROUP BY tenant_id ORDER BY tenant_id;"

echo "--- birth_names: count(*) by tenant_id ---"
$PSQL_EX -c "SELECT tenant_id, count(*) FROM birth_names GROUP BY tenant_id ORDER BY tenant_id;"

echo "--- video_game_sales: count(*) by tenant_id ---"
$PSQL_EX -c "SELECT tenant_id, count(*) FROM video_game_sales GROUP BY tenant_id ORDER BY tenant_id;"

echo "--- neurons_devices: count(*) by tenant_id ---"
$PSQL_EX -c "SELECT tenant_id, count(*) FROM neurons_devices GROUP BY tenant_id ORDER BY tenant_id;"

# ---------------------------------------------------------------------------
# API proof: the SAME chart, queried as two different tenant users, must
# return different row counts that match the tenant_id split above. Picks
# the first chart built on cleaned_sales_data (a SHARED dataset).
#
# `superset load_examples`' own charts are saved with query_context=null
# (confirmed against this build: GET /api/v1/chart/<pk>/data/ answers 400
# "Chart has no query context saved" for every one of them -- the legacy
# params-only shape the example loader writes is not enough for this
# endpoint, only for the editor UI, which builds+saves one on first open).
# Rather than pick an arbitrary chart and silently skip the proof this
# script writes a minimal, real query_context (same shape `api/v1/chart/
# data` itself expects -- datasource + one QueryObject) onto the ONE chart
# under test, via SQL, before running it as each tenant user.
# ---------------------------------------------------------------------------
echo
echo "--- API proof: same shared chart, two tenant users, RLS-scoped row counts ---"

CHART_ID=$($PSQL -tA -c "
SELECT sl.id FROM slices sl
JOIN tables tb ON tb.id = sl.datasource_id
WHERE tb.table_name = 'cleaned_sales_data'
ORDER BY sl.id LIMIT 1;
")
CHART_ID=$(echo "$CHART_ID" | tr -d '[:space:]')
DATASOURCE_ID=$($PSQL -tA -c "SELECT datasource_id FROM slices WHERE id = $CHART_ID;" 2>/dev/null | tr -d '[:space:]')

if [ -z "$CHART_ID" ]; then
  echo "  no chart found on cleaned_sales_data; skipping API proof"
else
  echo "  chart under test: id=$CHART_ID (cleaned_sales_data, datasource=$DATASOURCE_ID)"

  QUERY_CONTEXT=$(python3 -c "
import json
print(json.dumps({
    'datasource': {'id': $DATASOURCE_ID, 'type': 'table'},
    'queries': [{
        'columns': [],
        'metrics': ['count'],
        'row_limit': 10000,
        'granularity': None,
        'time_range': 'No filter',
        'filters': [],
        'extras': {'where': ''},
        'having': '',
        'where': '',
    }],
    'result_type': 'full',
    'form_data': {'viz_type': 'table', 'datasource': f'{$DATASOURCE_ID}__table'},
}))
")
  # (embedded directly, not via psql -v/:'var' interpolation -- that only
  # applies to scripts/-f, not -c -- safe here since json.dumps never emits
  # a literal single quote)
  $PSQL -c "UPDATE slices SET query_context = '$QUERY_CONTEXT' WHERE id = $CHART_ID;" >/dev/null
  echo "  wrote a minimal query_context onto chart $CHART_ID (COUNT(*) over cleaned_sales_data, no groupby)"

  BASE_URL="http://localhost:8097"

  login_and_fetch() {
    local username="$1" label="$2"
    local login_resp access_token data_resp rowcount http_code
    login_resp=$(curl -s -X POST "$BASE_URL/api/v1/security/login" \
      -H 'Content-Type: application/json' \
      -d "{\"username\":\"$username\",\"password\":\"test1234\",\"provider\":\"db\",\"refresh\":true}")
    access_token=$(echo "$login_resp" | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null)
    if [ -z "$access_token" ]; then
      echo "  $label ($username): LOGIN FAILED: $login_resp"
      return
    fi
    http_code=$(curl -s -o /tmp/chart_data_resp.json -w '%{http_code}' \
      "$BASE_URL/api/v1/chart/$CHART_ID/data/" -H "Authorization: Bearer $access_token")
    data_resp=$(cat /tmp/chart_data_resp.json)
    if [ "$http_code" != "200" ]; then
      echo "  $label ($username): HTTP $http_code: $(echo "$data_resp" | head -c 300)"
      return
    fi
    rowcount=$(echo "$data_resp" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    r = d['result'][0]
    print('rowcount=' + str(r.get('rowcount')) + ' data=' + str(r.get('data')))
except Exception as exc:
    print('PARSE ERROR: ' + str(exc) + ' body[:200]=' + sys.stdin.read()[:200])
" 2>/dev/null)
    echo "  $label ($username): $rowcount"
  }

  login_and_fetch "3f0a91c7-2d84-4e63-9b15-7c4e8a2f6d31" "Ada  (tenant A)"
  login_and_fetch "9d7e35a1-8c62-4b04-a7f1-3d5e9b2c8a76" "Cleo (tenant B)"
  echo "  (compare these row counts against the cleaned_sales_data tenant_id split above -- they must differ and match)"
fi

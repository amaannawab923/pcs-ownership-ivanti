#!/bin/sh
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
# PCS-10243 object-ownership QA -- OpenFGA scenario data.
#
# Idempotent: OpenFGA writes are upserts. Run after qa/setup_scenarios.py
# (needs the dashboard uuids it prints). Talks to OpenFGA directly on the
# host port (localhost:8098) -- this is the shared OpenFGA instance, NOT
# part of the "events" docker project's containers, so no container is
# touched here.
set -eu

FGA=http://localhost:8098
STORE=01M1TT1CJ9PWQVF6KWBEJNSKB8
MODEL=01M1TT1CKT0Q4006M9TY8YPA73

# user ids from qa/setup_scenarios.py MARKER output
UID_TENANT_A_USER1=6
UID_TENANT_A_USER2=7
UID_TENANT_B_USER1=8
UID_TENANT_B_USER2=9

# dashboard uuids from qa/setup_scenarios.py MARKER output
DASH_GROUP_DIRECT_UUID=${DASH_GROUP_DIRECT_UUID:-dc768963-85aa-4af6-a3fc-7aa7bcb18251}
DASH_GROUP_NESTED_UUID=${DASH_GROUP_NESTED_UUID:-c2072d4b-8334-4482-bdc4-5035a08f73fd}

echo "== 1. group membership tuples =="
curl -s -X POST "$FGA/stores/$STORE/write" -H 'Content-Type: application/json' -d '{
  "authorization_model_id": "'"$MODEL"'",
  "writes": {"tuple_keys": [
    {"user": "user:'"$UID_TENANT_A_USER1"'", "relation": "member", "object": "group:eng"},
    {"user": "user:'"$UID_TENANT_A_USER2"'", "relation": "member", "object": "group:eng"},
    {"user": "user:'"$UID_TENANT_B_USER1"'", "relation": "member", "object": "group:leads"},
    {"user": "group:leads#member", "relation": "member", "object": "group:eng"}
  ]}
}' -w '\nhttp=%{http_code}\n'

echo "== 2. share the two group-scenario dashboards with group:eng#member as viewer =="
curl -s -X POST "$FGA/stores/$STORE/write" -H 'Content-Type: application/json' -d '{
  "authorization_model_id": "'"$MODEL"'",
  "writes": {"tuple_keys": [
    {"user": "group:eng#member", "relation": "viewer", "object": "dashboard:'"$DASH_GROUP_DIRECT_UUID"'"},
    {"user": "group:eng#member", "relation": "viewer", "object": "dashboard:'"$DASH_GROUP_NESTED_UUID"'"}
  ]}
}' -w '\nhttp=%{http_code}\n'

echo "== 3. read back: who does group:eng resolve to on each dashboard (expand) =="
for uuid in "$DASH_GROUP_DIRECT_UUID" "$DASH_GROUP_NESTED_UUID"; do
  echo "-- dashboard:$uuid --"
  curl -s -X POST "$FGA/stores/$STORE/expand" -H 'Content-Type: application/json' -d '{
    "authorization_model_id": "'"$MODEL"'",
    "tuple_key": {"relation": "viewer", "object": "dashboard:'"$uuid"'"}
  }' | python3 -m json.tool
done

echo "== 4. tenant-scoping model extension (new version, additive only) =="
# Build on the CURRENT model's type_definitions, adding:
#   - a "tenant" type (same shape as "group": a memberless-by-default
#     container with a "member" relation for users)
#   - a "tenant" relation on dashboard/chart, directly-related to
#     tenant#member, that is NOT wired into the owner/editor/viewer unions.
# Not wiring it in is deliberate: the running module's checks
# (authz.py / fga.py) only ever ask about "owner"/"viewer"/"editor" and
# never pass a tenant into the check -- wiring "tenant" into those unions
# would silently change what viewer/editor mean without any module code
# change asking for it. As written, this is model-only: the tuples below
# are real and queryable directly against OpenFGA, but nothing in
# superset_ownership enforces them. See the "Known gaps" note in the
# ownership test-case doc.
CURRENT=$(curl -s "$FGA/stores/$STORE/authorization-models/$MODEL")
python3 - "$CURRENT" <<'PYEOF'
import json, sys, urllib.request

current = json.loads(sys.argv[1])["authorization_model"]
type_defs = current["type_definitions"]

tenant_type = {
    "type": "tenant",
    "relations": {"member": {"this": {}}},
    "metadata": {
        "relations": {
            "member": {
                "directly_related_user_types": [{"type": "user", "condition": ""}],
                "module": "", "source_info": None,
            }
        },
        "module": "", "source_info": None,
    },
}

def add_tenant_relation(td):
    td["relations"]["tenant"] = {"this": {}}
    td["metadata"]["relations"]["tenant"] = {
        "directly_related_user_types": [{"type": "tenant", "relation": "member", "condition": ""}],
        "module": "", "source_info": None,
    }

new_defs = []
for td in type_defs:
    if td["type"] in ("dashboard", "chart"):
        add_tenant_relation(td)
    new_defs.append(td)
new_defs.append(tenant_type)

body = {"schema_version": "1.1", "type_definitions": new_defs}
req = urllib.request.Request(
    "http://localhost:8098/stores/01M1TT1CJ9PWQVF6KWBEJNSKB8/authorization-models",
    data=json.dumps(body).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(req) as resp:
    print(resp.status, resp.read().decode())
PYEOF

echo "== 5. fetch the new model id, write illustrative tenant tuples ============"
NEWMODEL=$(curl -s "$FGA/stores/$STORE/authorization-models?page_size=5" | python3 -c "import sys,json;print(json.load(sys.stdin)['authorization_models'][0]['id'])")
echo "new model id: $NEWMODEL"
TENANT_A_GUID=${TENANT_A_GUID:-a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40}
TENANT_B_GUID=${TENANT_B_GUID:-b7f28e5a-9c14-4d6b-a2f0-5e3d8c1a6b92}
curl -s -X POST "$FGA/stores/$STORE/write" -H 'Content-Type: application/json' -d '{
  "authorization_model_id": "'"$NEWMODEL"'",
  "writes": {"tuple_keys": [
    {"user": "user:'"$UID_TENANT_A_USER1"'", "relation": "member", "object": "tenant:'"$TENANT_A_GUID"'"},
    {"user": "user:'"$UID_TENANT_A_USER2"'", "relation": "member", "object": "tenant:'"$TENANT_A_GUID"'"},
    {"user": "user:'"$UID_TENANT_B_USER1"'", "relation": "member", "object": "tenant:'"$TENANT_B_GUID"'"},
    {"user": "user:'"$UID_TENANT_B_USER2"'", "relation": "member", "object": "tenant:'"$TENANT_B_GUID"'"},
    {"user": "tenant:'"$TENANT_A_GUID"'#member", "relation": "tenant", "object": "dashboard:'"$DASH_GROUP_DIRECT_UUID"'"},
    {"user": "tenant:'"$TENANT_B_GUID"'#member", "relation": "tenant", "object": "dashboard:'"$DASH_GROUP_NESTED_UUID"'"}
  ]}
}' -w '\nhttp=%{http_code}\n'

echo "== 6. confirm both model versions still exist =="
curl -s "$FGA/stores/$STORE/authorization-models" | python3 -c "import sys,json;d=json.load(sys.stdin);[print(m['id']) for m in d['authorization_models']]"

echo "== done =="

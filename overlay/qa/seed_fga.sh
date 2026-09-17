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
# Seed OpenFGA as the source of truth for tenants, roles and group membership.
#
# Nothing written here exists in Superset. Superset has no "dashboard_designer"
# group and no record that Ada is in one -- if access is granted through these
# tuples, OpenFGA is authoritative and not merely mirroring Superset.
#
# Re-runnable: OpenFGA writes are NOT upserts -- it rejects a tuple that
# already exists, and because Write is atomic, one duplicate in a batch
# discards every other tuple in that same batch. So each tuple is written
# on its own and "already exists" is reported as ok. (This is the same trap
# that made the first tenant-purge implementation report deletions it had
# never performed.)
#
# `set -e` matters here: a failed `$(...)` in an assignment stops the script.
# Without it a bad OWNERSHIP_GROUP_ID_FORMAT left every group id empty and
# the tenant tuples were written before the group block posted `"object": ""`.
set -eu
FGA=${FGA:-http://localhost:8098}
STORE=${STORE:-01M1TT1CJ9PWQVF6KWBEJNSKB8}
HERE=$(cd "$(dirname "$0")" && pwd)
PYTHONPATH_DEV="$HERE/../docker/pythonpath_dev"

# Validate the group id format ONCE, before anything is written: the same
# helper the running instance reads, so this seed and that instance can
# never disagree about the shape. A bad value stops here with the setting
# and the value named, and no tuple has been posted. Only stdout is
# captured: a warning the host Python prints during the import goes to the
# terminal instead of being echoed as part of the format below.
FMT=$(python3 -c '
import sys
sys.path.insert(0, sys.argv[1])
from superset_ownership.identity import GroupIdFormatError, group_id_format
try:
    print(group_id_format())
except GroupIdFormatError as exc:
    sys.exit(
        "seed_fga: OWNERSHIP_GROUP_ID_FORMAT is not usable; nothing was written:\n"
        f"{exc}"
    )
' "$PYTHONPATH_DEV") || exit 1

# The store's current model, read before any write so an unreachable store
# is reported as that -- not as a traceback from the JSON parse of an empty
# response, and never as every tuple posting ERROR against an empty model id.
MODELS=$(curl -s --fail "$FGA/stores/$STORE/authorization-models?page_size=1") || {
  echo "seed_fga: cannot reach OpenFGA at $FGA (store $STORE, curl exit $?); nothing was written" >&2
  exit 1
}
MODEL=$(echo "$MODELS" | python3 -c '
import json, sys
models = json.load(sys.stdin).get("authorization_models") or []
if not models:
    sys.exit(1)
print(models[0]["id"])
' 2>/dev/null) || {
  echo "seed_fga: store $STORE at $FGA has no authorization model; nothing was written" >&2
  exit 1
}

TENANT_A=a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40
TENANT_B=b7f28e5a-9c14-4d6b-a2f0-5e3d8c1a6b92

ADA=3f0a91c7-2d84-4e63-9b15-7c4e8a2f6d31   # tenant A
BEN=6c2b48e9-5a71-4f92-8d03-2e9b7c1a4d53   # tenant A
CLEO=9d7e35a1-8c62-4b04-a7f1-3d5e9b2c8a76  # tenant B

# Role groups are per-tenant: a dashboard designer in tenant A is not one in
# tenant B. The group id carries the tenant so the two can never collide --
# and WHERE it carries it is OWNERSHIP_GROUP_ID_FORMAT ("{tenant}.{name}" by
# default, Neurons' shape; "{name}_{tenant}" for a store written before the
# confirmation), read by the module's own helper (validated above). Export
# the same value to both this seed and the running instance.
group_object() {
  python3 -c '
import sys
sys.path.insert(0, sys.argv[3])
from superset_ownership.identity import group_object
print(group_object(sys.argv[1], sys.argv[2]))
' "$1" "$2" "$PYTHONPATH_DEV"
}
DD_A=$(group_object dashboard_designer "$TENANT_A")
CD_A=$(group_object chart_designer "$TENANT_A")
DD_B=$(group_object dashboard_designer "$TENANT_B")
# A person in the store is `user:<tenant-guid>.<member-guid>` (Neurons'
# spelling, confirmed by Ivanti); a tenant administrator is the `admin`
# relation on the tenant object, not a group.
U_ADA="user:$TENANT_A.$ADA"
U_BEN="user:$TENANT_A.$BEN"
U_CLEO="user:$TENANT_B.$CLEO"

# One tuple per request, so an existing tuple never takes its batch down
# with it. Prints ok / exists / the error, per tuple.
write() {
  echo "$1" | python3 -c '
import json, subprocess, sys
fga, store, model = sys.argv[1], sys.argv[2], sys.argv[3]
for t in json.load(sys.stdin):
    body = json.dumps({"authorization_model_id": model, "writes": {"tuple_keys": [t]}})
    out = subprocess.run(
        ["curl", "-s", "-X", "POST", f"{fga}/stores/{store}/write",
         "-H", "content-type: application/json", "-d", body],
        capture_output=True, text=True).stdout
    label = " ".join(t[k] for k in ("user", "relation", "object"))
    try:
        d = json.loads(out)
    except Exception:
        print(f"    ERROR {label}: {out[:100]}"); continue
    if d == {}:
        print(f"    ok     {label}")
    elif "already exists" in d.get("message", ""):
        print(f"    exists {label}")
    else:
        print(f"    ERROR  {label}: " + str(d.get("message", json.dumps(d)))[:120])
' "$FGA" "$STORE" "$MODEL"
}

echo "  model: $MODEL"
# The format as the helper resolved it (blank or unset means the default),
# not a shell re-derivation of it.
echo "  group id format: $FMT  e.g. $DD_A"

echo "  tenants and their members"
write '[
  {"user":"'"$U_ADA"'",  "relation":"member","object":"tenant:'"$TENANT_A"'"},
  {"user":"'"$U_BEN"'",  "relation":"member","object":"tenant:'"$TENANT_A"'"},
  {"user":"'"$U_CLEO"'", "relation":"member","object":"tenant:'"$TENANT_B"'"}
]'

echo "  tenant administrators: the admin relation on the tenant object"
write '[
  {"user":"'"$U_ADA"'",  "relation":"admin","object":"tenant:'"$TENANT_A"'"},
  {"user":"'"$U_CLEO"'", "relation":"admin","object":"tenant:'"$TENANT_B"'"}
]'

echo "  role groups (exist ONLY in OpenFGA)"
write '[
  {"user":"'"$U_ADA"'",  "relation":"member","object":"'"$DD_A"'"},
  {"user":"'"$U_BEN"'",  "relation":"member","object":"'"$CD_A"'"},
  {"user":"'"$U_CLEO"'", "relation":"member","object":"'"$DD_B"'"}
]'

echo "  nested membership: chart designers are also dashboard designers (a group as a member of a group)"
write '[
  {"user":"'"$CD_A"'#member","relation":"member","object":"'"$DD_A"'"}
]'

echo
echo "  seeded identities:"
echo "    tenant A $TENANT_A  -> Ada (dashboard_designer, tenant admin), Ben (chart_designer, nested into dashboard_designer)"
echo "    tenant B $TENANT_B  -> Cleo (dashboard_designer, tenant admin)"

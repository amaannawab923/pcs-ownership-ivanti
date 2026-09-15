#!/bin/sh
#
# RUNS INSIDE THE CONTAINER. `docker cp`'d in and executed with `docker exec`,
# so 8088 is the app's own port. The host-side published port (8096) does not
# resolve from in here -- this is not a stale value. Override with B= to run
# it somewhere else.
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
# Probe a representative slice of the API and print a stable fingerprint.
B=${B:-http://localhost:8088}
A=$(curl -s -X POST $B/api/v1/security/login -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin","provider":"db","refresh":false}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
for p in "dashboard/" "chart/" "dashboard/5" "chart/51" "dataset/" "security/roles/"; do
  code=$(curl -s -H "Authorization: Bearer $A" -o /tmp/b.json -w '%{http_code}' "$B/api/v1/$p")
  n=$(python3 -c "import json;d=json.load(open('/tmp/b.json'));print(d.get('count', 'n/a'))" 2>/dev/null || echo err)
  printf "%-16s %s count=%s\n" "$p" "$code" "$n"
done
printf "%-16s %s\n" "ownership/dashboards" "$(curl -s -H "Authorization: Bearer $A" -o /dev/null -w '%{http_code}' $B/api/v1/ownership/dashboards)"

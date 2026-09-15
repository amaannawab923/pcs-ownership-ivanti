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
# Restores dashboard 5 / chart 51 (the original demo-ownership.sh objects)
# to their baseline state: public, owner=admin, no shares. Safe to re-run.
# Does NOT touch any of the qa/setup_scenarios.py scenario objects.
set -eu
B=${B:-http://localhost:8096}
A=$(curl -s -X POST "$B/api/v1/security/login" -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin","provider":"db","refresh":false}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

# A group subject (e.g. "group:eng#member") has a literal `#` in it; passed
# unescaped in a URL, curl (like any URL parser) reads everything from the
# `#` on as a FRAGMENT and never sends it, so the share is "revoked" with a
# subject that silently lost its "#member" suffix -- a no-op against a
# subject nothing was ever shared under. Percent-encode it first.
urlencode() { python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"; }

curl -s -X PUT -H "Authorization: Bearer $A" -H 'Content-Type: application/json' \
  "$B/api/v1/ownership/dashboard/5/visibility" -d '{"visibility":"public"}' -o /dev/null -w 'dashboard 5 -> public: %{http_code}\n'
curl -s -X PUT -H "Authorization: Bearer $A" -H 'Content-Type: application/json' \
  "$B/api/v1/ownership/chart/51/visibility" -d '{"visibility":"public"}' -o /dev/null -w 'chart 51 -> public: %{http_code}\n'

for s in $(curl -s -H "Authorization: Bearer $A" "$B/api/v1/ownership/dashboard/5" | python3 -c "import sys,json;[print(s['subject']) for s in json.load(sys.stdin)['shares']]"); do
  curl -s -X DELETE -H "Authorization: Bearer $A" "$B/api/v1/ownership/dashboard/5/shares/$(urlencode "$s")" -o /dev/null -w "revoke dashboard 5 share $s: %{http_code}\n"
done
for s in $(curl -s -H "Authorization: Bearer $A" "$B/api/v1/ownership/chart/51" | python3 -c "import sys,json;[print(s['subject']) for s in json.load(sys.stdin)['shares']]"); do
  curl -s -X DELETE -H "Authorization: Bearer $A" "$B/api/v1/ownership/chart/51/shares/$(urlencode "$s")" -o /dev/null -w "revoke chart 51 share $s: %{http_code}\n"
done

echo "done -- dashboard 5 / chart 51 restored to public, no shares"

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
# Shared helpers for the PCS-10243 ownership QA suite. Source this, don't run it.
B=${B:-http://localhost:8096}
tok() {
  curl -s -X POST "$B/api/v1/security/login" -H 'Content-Type: application/json' \
    -d "{\"username\":\"$1\",\"password\":\"$2\",\"provider\":\"db\",\"refresh\":false}" \
    | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("access_token",""))'
}
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

# Superset rate-limits /api/v1/security/login. A test that logs in per assertion
# gets an empty token back and reads the resulting 401 as an authorization
# failure -- which is how an hour went into a "cross-tenant deny" that was only
# a throttled login. Log in ONCE per user, and fail loudly if it did not work.
tok_or_die() {
  t=$(tok "$1" "$2")
  case "$t" in
    ey*) printf '%s' "$t" ;;
    *) echo "FATAL: could not authenticate $1 (rate-limited login?)" >&2; exit 1 ;;
  esac
}

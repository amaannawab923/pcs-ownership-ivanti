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
# The access matrix for one object, run from inside the container.
#
#   sh access_matrix.sh dashboard 5
#
# Every ownership write reports its own HTTP status. Without that, a write
# rejected with a 400 is indistinguishable from a correct denial: the matrix
# shows 404, which is exactly what a working denial looks like, and the run
# reads as a pass. The share step here did precisely that.
B=${B:-http://localhost:8088}
KIND=$1; ID=$2

tok() {
  curl -s -X POST $B/api/v1/security/login -H 'Content-Type: application/json' \
    -d "{\"username\":\"$1\",\"password\":\"$2\",\"provider\":\"db\",\"refresh\":false}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("access_token",""))'
}
A=$(tok admin admin); V=$(tok vgs_user test1234); W=$(tok wb_user test1234)
for t in "$A" "$V" "$W"; do
  case "$t" in ey*) ;; *) echo "   FATAL: a login failed (rate-limited?)"; exit 1 ;; esac
done

uid() {
  curl -s -H "Authorization: Bearer $A" \
    "$B/api/v1/security/users/?q=(filters:!((col:username,opr:eq,value:$1)))" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["result"][0]["id"])'
}
VID=$(uid vgs_user); WID=$(uid wb_user)

# A write, with its status shown and a non-2xx called out.
w() {
  label=$1; shift
  c=$(curl -s -H "Authorization: Bearer $A" -H 'Content-Type: application/json' \
        -o /dev/null -w '%{http_code}' "$@")
  case "$c" in
    2*) printf "   . %-28s %s\n" "$label" "$c" ;;
    *)  printf "   ! %-28s %s  <- WRITE REJECTED\n" "$label" "$c" ;;
  esac
}
p() {
  printf "   %-8s %s/%s -> %s\n" "$1" "$KIND" "$ID" \
    "$(curl -s -H "Authorization: Bearer $2" -o /dev/null -w '%{http_code}' $B/api/v1/$KIND/$ID)"
}
# A group subject (e.g. "group:eng#member") has a literal `#` in it; passed
# unescaped in a URL, curl (like any URL parser) reads everything from the
# `#` on as a FRAGMENT and never sends it, so an unshare would silently drop
# the "#member" suffix. Percent-encode it first. The two subjects this
# script unshares are plain `user:<id>` refs (no `#`), so this only matters
# the next time someone extends the matrix to a group.
urlencode() { python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"; }

echo "   (vgs_user=user:$VID  wb_user=user:$WID)"

w "visibility=public" -X PUT $B/api/v1/ownership/$KIND/$ID/visibility -d '{"visibility":"public"}'
echo " PUBLIC";  p vgs_user "$V"; p wb_user "$W"

w "visibility=private" -X PUT $B/api/v1/ownership/$KIND/$ID/visibility -d '{"visibility":"private"}'
echo " PRIVATE"; p vgs_user "$V"; p wb_user "$W"

w "share vgs_user viewer" -X POST $B/api/v1/ownership/$KIND/$ID/shares -d "{\"subject\":\"user:$VID\",\"role\":\"viewer\"}"
w "share wb_user viewer"  -X POST $B/api/v1/ownership/$KIND/$ID/shares -d "{\"subject\":\"user:$WID\",\"role\":\"viewer\"}"
echo " SHARED with BOTH"
echo "   (vgs_user holds the dataset grant and should be allowed;"
echo "    wb_user does not, and a share must never substitute for it)"
p vgs_user "$V"; p wb_user "$W"

for s in "user:$VID" "user:$WID"; do
  w "unshare $s" -X DELETE $B/api/v1/ownership/$KIND/$ID/shares/$(urlencode "$s")
done
w "visibility=public" -X PUT $B/api/v1/ownership/$KIND/$ID/visibility -d '{"visibility":"public"}'
echo " RESTORED"; p vgs_user "$V"; p wb_user "$W"

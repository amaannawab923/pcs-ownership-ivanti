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
"""An `OWNERSHIP_FGA_CONFIG_PROVIDER` for a deployment that rotates its
OpenFGA bearer token out-of-band (a vault sidecar, a secrets manager) rather
than through an environment variable a restart would be needed to pick up.

`connection()` is called at load, on `superset ownership fga reconnect`, and
at most once per 30s per process on a 401 (spec §5.2/§5.3) -- so reading the
token fresh on every call is what makes rotation-without-restart work.

    OWNERSHIP_FGA_CONFIG_PROVIDER = "ivanti_pcs_example.fga:connection"

Needed for the manual test round's credential-rotation scenario
(`qa/design/directory-hook/04-manual-test-round.md` §(b)4).
"""

from __future__ import annotations

import os

TOKEN_FILE = "/tmp/openfga.token"  # noqa: S105, S108 - a path, not a secret; the rotation scenario needs a well-known one


def connection():
    """Build a `FgaConnection` from the environment's static fields, plus a
    bearer token re-read from `TOKEN_FILE` on every call. A missing or empty
    file means no `Authorization` header -- the same as `{"type": "none"}` --
    rather than a load-time failure, so a fresh deployment can install the
    model before the token file exists.
    """
    from superset_ownership.fga_connection import (
        DEFAULT_API_URL,
        DEFAULT_STORE_ID,
        FgaConnection,
    )

    api_url = os.environ.get("OWNERSHIP_FGA_API_URL", DEFAULT_API_URL)
    # Falls back to the same default `fga_connection.StaticProvider` uses,
    # not "" -- an empty store id built a connection nothing could ever
    # reach, which is worse than picking a wrong-but-real default a
    # deployment would notice and override (PR90 review M-4).
    store_id = os.environ.get("OWNERSHIP_FGA_STORE") or DEFAULT_STORE_ID
    model_id = os.environ.get("OWNERSHIP_FGA_MODEL") or None
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            token = f.read().strip()
    except OSError:
        token = ""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return FgaConnection(
        api_url=api_url, store_id=store_id, model_id=model_id, headers=headers
    )

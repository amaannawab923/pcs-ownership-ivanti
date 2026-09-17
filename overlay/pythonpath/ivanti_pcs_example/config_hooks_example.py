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
"""The config block that wires all sixteen of `hooks.py`'s function hooks
in at once (contract §4.5.2). Copy-pasteable, as-is, into
`superset_config_docker.py` (or your own config layer) -- see
`README.md` §4.5 for what each hook actually does and why.

NOT imported by any default deployment, and not a substitute for
`superset_config_docker_light.py`'s own `OWNERSHIP_*` block -- this is the
seam settings only; `OWNERSHIP_ENABLED`, `OWNERSHIP_OUTBOX_ENABLED` and the
rest of the non-hook settings stay wherever your instance already sets
them.

Before pointing a running instance at this block, run once, from a `flask
shell` (I-6: never from a web request -- see `hooks.py`'s module
docstring):

    from ivanti_pcs_example.hooks import install, sync_tenants_from_roles
    install()
    sync_tenants_from_roles()

`install()` creates this package's two attribute tables
(`ivanti_pcs_example_member_guid`, reused from `identity.py`, and the new
`ivanti_pcs_example_tenant`); `sync_tenants_from_roles()` populates the
tenant table for every account that already holds a `tenant_<guid>` role,
so `OWNERSHIP_TENANT_GUID` below has something to read on the very first
request after cutover instead of falling through to the default for every
account until each one is touched individually.
"""

from __future__ import annotations

# The class seams these hooks are commonly paired with in the acceptance
# run -- not required by the hooks themselves (every hook below works
# against whichever OWNERSHIP_AUTHORIZER/OWNERSHIP_DIRECTORY is already
# configured), but the directory-side hooks assume an OpenFGA store to read
# directly, so "openfga" is what the acceptance runbook uses.
OWNERSHIP_AUTHORIZER = "openfga"
OWNERSHIP_DIRECTORY = "openfga"
# Neurons writes no group-to-tenant tuple (the tenant is read from the
# group id), so the built-in `list_groups` fast path is always empty
# there: walk the members outright rather than read an empty page and
# warn about it once per tenant per TTL.
OWNERSHIP_DIRECTORY_GROUP_WALK = "always"

# -- caller-side: take the Flask-AppBuilder User row -----------------------
OWNERSHIP_MEMBER_GUID = "ivanti_pcs_example.hooks:member_guid"
OWNERSHIP_TENANT_GUID = "ivanti_pcs_example.hooks:tenant_guid"
OWNERSHIP_DISPLAY_NAME = "ivanti_pcs_example.hooks:display_name"
OWNERSHIP_IS_TENANT_ADMINISTRATOR = "ivanti_pcs_example.hooks:is_tenant_administrator"
OWNERSHIP_USER_FOR_MEMBER_GUID = "ivanti_pcs_example.hooks:user_for_member_guid"

# -- directory-side: take ids; read the OpenFGA store through `fga` directly
OWNERSHIP_USERS_OF_TENANT = "ivanti_pcs_example.hooks:users_of_tenant"
OWNERSHIP_GROUPS_OF_TENANT = "ivanti_pcs_example.hooks:groups_of_tenant"
OWNERSHIP_MEMBERS_OF_GROUP = "ivanti_pcs_example.hooks:members_of_group"
OWNERSHIP_USER_IN_GROUP = "ivanti_pcs_example.hooks:user_in_group"
OWNERSHIP_GROUP_EXISTS = "ivanti_pcs_example.hooks:group_exists"
OWNERSHIP_ADMINISTRATORS_OF_TENANT = "ivanti_pcs_example.hooks:administrators_of_tenant"

# -- shape-side: pure, no I/O. OWNERSHIP_GROUP_ID and OWNERSHIP_SPLIT_GROUP_ID
# must both be set (or neither) -- they are required to be exact inverses.
OWNERSHIP_GROUP_ID = "ivanti_pcs_example.hooks:group_id"
OWNERSHIP_SPLIT_GROUP_ID = "ivanti_pcs_example.hooks:split_group_id"
OWNERSHIP_GROUP_DISPLAY_NAME = "ivanti_pcs_example.hooks:group_display_name"
OWNERSHIP_TENANT_ADMINISTRATOR_GROUP = (
    "ivanti_pcs_example.hooks:tenant_administrator_group"
)

# -- decision-side: may only narrow the default reason ---------------------
OWNERSHIP_CAN_MANAGE = "ivanti_pcs_example.hooks:can_manage"

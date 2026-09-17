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
#
# PCS-10243 -- object ownership & sharing.
#
# This is the ONE file the client edits. It replaces (or is appended to)
# whatever `superset_config_docker.py` PCS already ships/expects at
# /app/pythonpath/superset_config_docker.py. Everything above the plug-in
# block is standard PCS config; only the two blocks below are new, and only
# the plug-in block usually needs a value changed.
#
# Rename this file to `superset_config_docker.py` before mounting or
# building it in -- `.example.py` is not imported by Superset.

import os

# --- your PCS / superset_config_docker.py content goes here, unchanged ----
# (database URI, SECRET_KEY, mapbox key, whatever PCS already sets). This
# example only shows the two new blocks; it does not attempt to reproduce
# the rest of PCS's own docker config.

# --- PCS ownership: the plug-in block -------------------------------------
# Every key has a default that reproduces the pre-override baseline -- an
# empty block is a valid block and matches running with no plug-ins at all
# (OWNERSHIP_AUTHORIZER="local", no OpenFGA, no directory/identity override).
# Full reference: overlay/pythonpath/ivanti_pcs_example/README.md in this
# delivery, and qa/design/directory-hook/01-plugin-contract.md on
# client-test main.

OWNERSHIP_ENABLED = True
OWNERSHIP_AUTHORIZER = "openfga"  # or "local", or "pkg.mod:Class"

# How we reach OpenFGA. Either static values...
OWNERSHIP_FGA_API_URL = os.environ.get("OPENFGA_API_URL", "http://openfga:8080")
OWNERSHIP_FGA_STORE = os.environ.get("OPENFGA_STORE_ID", "")
OWNERSHIP_FGA_MODEL = os.environ.get("OPENFGA_MODEL_ID", "")  # pinned; a deployment decision
# "none" matches this shell's own demo compose stack (no auth configured on
# that OpenFGA). For a production store that requires a token, switch to:
#   OWNERSHIP_FGA_CREDENTIALS = {"type": "api_token", "token_env": "OPENFGA_TOKEN"}
# and set OPENFGA_TOKEN in the environment -- an api_token type with no
# resolvable token_env value fails the plug-in loader at boot (deliberate;
# see fga_connection.py), it does not silently fall back to no auth.
OWNERSHIP_FGA_CREDENTIALS = {"type": "none"}
# ...or a provider you write, for vaults / rotation / per-environment lookup:
# OWNERSHIP_FGA_CONFIG_PROVIDER = "ivanti_pcs_example.fga:connection"

# Who is who. Defaults are Neurons' (PCS-10243, confirmed by Ivanti): the
# member GUID (the token's `sub`) is the Superset username, the tenant
# (`tid`) is the `Tenant_<guid>_Role` FAB role, and the store spells a
# person `user:<tenant>.<member>`. Override only if your JIT login puts
# the member id elsewhere:
# OWNERSHIP_IDENTITY = "ivanti_pcs_example.identity:AttributeIdentity"

# The directory: users, groups, members, administrators of a tenant.
OWNERSHIP_DIRECTORY = "openfga"  # default: read from the store
# OWNERSHIP_DIRECTORY = "ivanti_pcs_example.directory:FixedGroupsDirectory"

OWNERSHIP_GROUP_ID_FORMAT = "{tenant}.{name}"  # Neurons' `group:<tenant>.<local id>`; "{name}_{tenant}" for a store written before the confirmation
# Neurons writes no group-to-tenant tuple (the tenant is read from the group
# id), so list a tenant's groups by walking its members outright rather than
# reading an always-empty `group#tenant` page first and warning about it.
OWNERSHIP_DIRECTORY_GROUP_WALK = "always"
OWNERSHIP_PUBLIC_SCOPE = "tenant"  # public = within the object's tenant; "instance": dataset grant alone
OWNERSHIP_MANAGE_PERMISSION = None  # optional sharing-manager role
OWNERSHIP_OUTBOX_ENABLED = True
# OWNERSHIP_OUTBOX_CELERY = True   # needs a `redis` service + a worker+beat process
# ---------------------------------------------------------------------------

# --- PCS ownership: hooks block (commented out; uncomment only what you
#     actually need to override) -------------------------------------------
# Uncomment and point at your own class only if the defaults in the block
# above are not enough for your identity/directory/authorizer shape. Every
# one of these is optional; see overlay/pythonpath/ivanti_pcs_example/ for
# runnable worked examples of each.
#
# OWNERSHIP_IDENTITY = "ivanti_pcs_example.identity:AttributeIdentity"
# OWNERSHIP_DIRECTORY = "ivanti_pcs_example.directory:FixedGroupsDirectory"
# OWNERSHIP_AUTHORIZER = "ivanti_pcs_example.authorizer:PrefixedIdAuthorizer"
# OWNERSHIP_FGA_CONFIG_PROVIDER = "ivanti_pcs_example.fga:connection"
# ---------------------------------------------------------------------------

# --- PCS ownership: function hooks (commented out; finer-grained than the
#     class seams above -- override one function instead of a whole
#     Identity/Directory/Authorizer class) --------------------------------
# All sixteen are optional and independent: set only the ones your
# deployment actually needs a different answer for; every other hook keeps
# whichever class-level seam is configured above (or the module's own
# default). `OWNERSHIP_GROUP_ID` and `OWNERSHIP_SPLIT_GROUP_ID` are the one
# pair that must be set together (or not at all) -- they are required to be
# exact inverses of each other. The runnable versions of every hook below,
# plus the copy-pasteable block this is drawn from, are in
# overlay/pythonpath/ivanti_pcs_example/hooks.py and
# overlay/pythonpath/ivanti_pcs_example/config_hooks_example.py.
#
# -- caller-side: take the Flask-AppBuilder User row -----------------------
# OWNERSHIP_MEMBER_GUID = "ivanti_pcs_example.hooks:member_guid"
# OWNERSHIP_TENANT_GUID = "ivanti_pcs_example.hooks:tenant_guid"
# OWNERSHIP_DISPLAY_NAME = "ivanti_pcs_example.hooks:display_name"
# OWNERSHIP_IS_TENANT_ADMINISTRATOR = "ivanti_pcs_example.hooks:is_tenant_administrator"
# OWNERSHIP_USER_FOR_MEMBER_GUID = "ivanti_pcs_example.hooks:user_for_member_guid"
#
# -- directory-side: take ids; read the OpenFGA store through `fga` directly
# OWNERSHIP_USERS_OF_TENANT = "ivanti_pcs_example.hooks:users_of_tenant"
# OWNERSHIP_GROUPS_OF_TENANT = "ivanti_pcs_example.hooks:groups_of_tenant"
# OWNERSHIP_MEMBERS_OF_GROUP = "ivanti_pcs_example.hooks:members_of_group"
# OWNERSHIP_USER_IN_GROUP = "ivanti_pcs_example.hooks:user_in_group"
# OWNERSHIP_GROUP_EXISTS = "ivanti_pcs_example.hooks:group_exists"
# OWNERSHIP_ADMINISTRATORS_OF_TENANT = "ivanti_pcs_example.hooks:administrators_of_tenant"
#
# -- shape-side: pure, no I/O -----------------------------------------------
# OWNERSHIP_GROUP_ID = "ivanti_pcs_example.hooks:group_id"
# OWNERSHIP_SPLIT_GROUP_ID = "ivanti_pcs_example.hooks:split_group_id"
# OWNERSHIP_GROUP_DISPLAY_NAME = "ivanti_pcs_example.hooks:group_display_name"
# OWNERSHIP_TENANT_ADMINISTRATOR_GROUP = "ivanti_pcs_example.hooks:tenant_administrator_group"
#
# -- decision-side: may only narrow the default reason ----------------------
# OWNERSHIP_CAN_MANAGE = "ivanti_pcs_example.hooks:can_manage"
# ---------------------------------------------------------------------------

# Apply the whole feature layer (switch derivation, FEATURE_FLAGS,
# BLUEPRINTS, hooks wiring, celery outbox opt-in, all OWNERSHIP_* defaults)
# to this module's namespace. MUST be the last line: everything above this
# call is read by `configure()`, nothing after it is seen.
from superset_config_ownership import configure  # noqa: E402

configure(globals())

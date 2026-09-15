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
"""
The sentinel: the only mechanism Superset's extension points allow for
DENYING access (hooks only grant). See raise_for_access in
superset/security/manager.py around line 4871:

    if dashboard.viewers:
        if dashboard.published and self.is_viewer(dashboard): return
    else:
        <permissive datasource fallback>
    raise

An empty `viewers` list means "anyone with the underlying dataset can open
it" (the permissive fallback branch). We close that branch for
private/shared objects by adding ONE viewer subject that matches nobody: a
memberless FAB role `__ownership_sentinel__`, wrapped in a `subjects` row
of type ROLE. Since the role has no members, `is_viewer()` (which checks
`user_subject_ids & viewer_subject_ids`) can never match it, and Superset
falls through to `raise` on its own -- no bespoke denial code needed.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

SENTINEL_ROLE_NAME = "__ownership_sentinel__"


def get_sentinel_subject():
    """Get or create the memberless role + its Subject wrapper."""
    from superset import security_manager
    from superset.subjects.utils import get_or_create_role_subject

    role = security_manager.find_role(SENTINEL_ROLE_NAME)
    if not role:
        role = security_manager.add_role(SENTINEL_ROLE_NAME)
        logger.info("superset_ownership: created sentinel role %s", SENTINEL_ROLE_NAME)
    return get_or_create_role_subject(role.id)


def add_sentinel(asset) -> None:
    """Add the sentinel to asset.viewers (asset = Dashboard or Slice)."""
    subject = get_sentinel_subject()
    if subject is not None and subject not in asset.viewers:
        asset.viewers.append(subject)


def remove_sentinel(asset) -> None:
    """Remove the sentinel from asset.viewers, restoring default behavior."""
    subject = get_sentinel_subject()
    if subject is not None and subject in asset.viewers:
        asset.viewers.remove(subject)

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
"""A `Directory` override for a deployment that wants its own group list
instead of reading `OpenFGADirectory`'s tuple vocabulary -- e.g. groups that
live in a system OpenFGA never sees.

`FixedGroupsDirectory` is deliberately trivial: every tenant has exactly two
groups, `blue_<tenant>` and `green_<tenant>`, and both are answered
statically -- everything about USERS still comes from Superset's own
`ab_user` table, filtered by the tenant role, exactly as
`superset_ownership.identity`'s helpers already read it. That split is the
point of the example: a real override only has to answer the questions it
actually has a different source for.

`Broken` is deliberately missing `health` -- the fixture the manual test
round (`qa/design/directory-hook/04-manual-test-round.md` §(c)6) points
`OWNERSHIP_DIRECTORY` at to prove a class that does not satisfy the
`Directory` protocol fails the boot loudly, naming the missing method,
instead of degrading silently.

`list_groups`/`group_members`/`search_users` return plain dicts in the
`Page[...]` shape (spec §7.1: `{"items": [...], "next_cursor": ...}`) rather
than importing `Page` itself -- a `TypedDict` carries no runtime behaviour to
subclass or construct against, so a plain dict is both correct and simpler
here; `test_ivanti_pcs_example.py` pins the field names against the real
protocol. `health()` returns the real `DirectoryHealth` dataclass.
"""

from __future__ import annotations

from typing import Any

from superset_ownership.directory import DirectoryHealth

FIXED_GROUP_NAMES = ("blue", "green")


class FixedGroupsDirectory:
    protocol_version = 1

    # -- groups ----------------------------------------------------------

    def list_groups(self, tenant: str, *, cursor: str | None = None) -> dict[str, Any]:
        # I-3: `id` is the store OBJECT reference (`group:<id>`, contract
        # §4.3), not the bare id `group_id()` returns -- `OpenFGADirectory`,
        # `LocalDirectory` and the harness's counting fake all emit
        # `group:<id>` here, and `/subjects` renders the picker's value as
        # `f"{grp['id']}#member"` verbatim, so a bare id produced a value
        # the share route rejected outright (`400 subject must be
        # 'user:<guid>' or 'group:<name>#member'`).
        from superset_ownership.identity import group_display_name, group_object

        items = [
            {
                "id": group_object(name, tenant),
                "display_name": group_display_name(group_object(name, tenant)),
                "tenant": tenant,
                "members": None,
            }
            for name in FIXED_GROUP_NAMES
        ]
        return {"items": items, "next_cursor": None}

    def group_exists(self, group_id: str) -> bool:
        from superset_ownership.identity import split_group_id

        parsed = split_group_id(group_id)
        return parsed is not None and parsed[0] in FIXED_GROUP_NAMES

    def group_members(
        self, group_id: str, *, cursor: str | None = None
    ) -> dict[str, Any]:
        from superset_ownership.identity import split_group_id

        parsed = split_group_id(group_id)
        if parsed is None:
            return {"items": [], "next_cursor": None}
        _, tenant = parsed
        return self.search_users(tenant, "")

    def user_in_group(self, member_guid: str, group_id: str) -> bool:
        from superset_ownership.identity import split_group_id

        parsed = split_group_id(group_id)
        if parsed is None:
            return False
        _, tenant = parsed
        return any(
            u["guid"] == member_guid for u in self.search_users(tenant, "")["items"]
        )

    # -- users -------------------------------------------------------------

    def search_users(
        self, tenant: str, query: str, *, limit: int = 100, cursor: str | None = None
    ) -> dict[str, Any]:
        from superset_ownership.identity import resolve_member_guid, resolve_tenant_guid

        from superset import security_manager

        items = []
        for user in security_manager.get_all_users():
            if resolve_tenant_guid(user) != tenant:
                continue
            guid = resolve_member_guid(user)
            if not guid:
                continue
            label = (
                f"{user.first_name or ''} {user.last_name or ''}".strip()
                or user.username
            )
            if (
                query
                and query.lower() not in label.lower()
                and query.lower() not in guid.lower()
            ):
                continue
            items.append(
                {
                    "guid": guid,
                    "display_name": label,
                    "email": user.email,
                    "superset_id": user.id,
                }
            )
        # Sorted for a stable, deterministic page order -- `security_manager
        # .get_all_users()` makes no ordering guarantee, and an offset
        # cursor over an unstable order can skip or repeat rows across
        # pages. `plugin verify`'s `pagination_roundtrips` check (spec §10)
        # depends on `limit=2` and `limit=100` walks returning the exact
        # same set; an earlier version of this method ignored `limit`/
        # `cursor` entirely and always returned everything in one page,
        # which trivially agreed rather than actually being exercised
        # (PR90 review H-3 -- the worked example shipped to Ivanti did not
        # satisfy the semantics the kit checks).
        items.sort(key=lambda u: u["guid"])
        start = int(cursor) if cursor else 0
        page = items[start : start + limit]
        next_cursor = str(start + limit) if start + limit < len(items) else None
        return {"items": page, "next_cursor": next_cursor}

    def user_in_tenant(self, member_guid: str, tenant: str) -> bool:
        return any(
            u["guid"] == member_guid for u in self.search_users(tenant, "")["items"]
        )

    def tenant_administrators(self, tenant: str) -> list[dict[str, Any]]:
        # This example models no administrator group of its own -- a real
        # override either delegates to OpenFGADirectory's algorithm or
        # answers from whatever system tracks administrators for it.
        return []

    # -- health --------------------------------------------------------

    def health(self) -> DirectoryHealth:
        return DirectoryHealth(ok=True, detail="fixed two-group directory")


class Broken:
    """Every `Directory` method except `health` -- deliberately, for the
    negative case in the manual test round: `OWNERSHIP_DIRECTORY` pointed
    here must fail the boot naming `missing: health`, not degrade."""

    protocol_version = 1

    def __init__(self) -> None:
        self._delegate = FixedGroupsDirectory()

    def list_groups(self, tenant: str, *, cursor: str | None = None) -> dict[str, Any]:
        return self._delegate.list_groups(tenant, cursor=cursor)

    def group_exists(self, group_id: str) -> bool:
        return self._delegate.group_exists(group_id)

    def group_members(
        self, group_id: str, *, cursor: str | None = None
    ) -> dict[str, Any]:
        return self._delegate.group_members(group_id, cursor=cursor)

    def user_in_group(self, member_guid: str, group_id: str) -> bool:
        return self._delegate.user_in_group(member_guid, group_id)

    def search_users(
        self, tenant: str, query: str, *, limit: int = 100, cursor: str | None = None
    ) -> dict[str, Any]:
        return self._delegate.search_users(tenant, query, limit=limit, cursor=cursor)

    def user_in_tenant(self, member_guid: str, tenant: str) -> bool:
        return self._delegate.user_in_tenant(member_guid, tenant)

    def tenant_administrators(self, tenant: str) -> list[dict[str, Any]]:
        return self._delegate.tenant_administrators(tenant)

    # No `health` method: that omission is the fixture.

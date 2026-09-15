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
"""An `Identity` override for a deployment whose JIT login does not put the
member GUID on the Superset username (the default's assumption -- see
`superset_ownership.identity.DefaultIdentity`), but on a per-user attribute
instead.

Configure with:

    OWNERSHIP_IDENTITY = "ivanti_pcs_example.identity:AttributeIdentity"

and, once, before first use, create this package's own table from a CLI
context -- a `flask shell`, never a web request (I-6):

    from ivanti_pcs_example.identity import install
    install()

then set the attribute once per user, e.g. from that same shell:

    from superset import security_manager
    from ivanti_pcs_example.identity import set_member_guid
    u = security_manager.find_user(username="ben")
    set_member_guid(u, "<real GUID>")

Reads and writes a small table this package owns
(`ivanti_pcs_example_member_guid`: `user_id` (FK `ab_user.id`) ->
`member_guid`) -- NOT FAB's own `User.extra_attributes`
relationship. On this Superset checkout that relationship is
`superset.models.user_attributes.UserAttribute`, a fixed-schema, one-row-
per-user table (`welcome_dashboard_id`, `avatar_url`,
`sessions_invalidated_at`, `password_must_change`) with no generic key/value
slot a plug-in could store a member GUID in (an earlier version of this
override assumed `extra_attributes` behaved like a JSON dict, which raises
`TypeError: Incompatible collection type` the moment a real account is
written to, and made `user_for_member_guid`'s query build a `->>` operator
against a relationship, which has none -- PR90 review N-1). A real Ivanti
deployment's own attribute store will differ from this package's toy table;
what this override demonstrates is the PATTERN -- a forward lookup by
account, a matched reverse lookup by GUID -- not this table's schema.

`AttributeIdentity` subclasses `superset_ownership.identity.DefaultIdentity`
(spec §6) directly -- an ordinary class, like any other override in this
package.

`member_guid` and its reverse, `user_for_member_guid`, are overridden
together (part 2's H-3 fix): `member_guid` is the forward direction (account
-> GUID), `user_for_member_guid` is the reverse (GUID -> account) that
`normalize_subject` needs to validate a subject reference the picker itself
emitted. An override that relocates the GUID off username/email and
implements only the forward direction leaves the reverse one answering from
`DefaultIdentity`'s username/email scan, so a share to the GUID this
identity itself handed out would fail to resolve back to the account.
`user_for_member_guid` also honours part 2's forward-mapping rule: a match
is only returned when `member_guid(user) == guid` for that same account, so
a stale or orphaned row can never disagree with the forward direction.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from superset_ownership.identity import DefaultIdentity

_METADATA = sa.MetaData()

# One member GUID per Superset account. A real deployment's own attribute
# store replaces this table; the column names are this package's choice,
# not a contract `Identity` or `Directory` impose.
member_guid_table = sa.Table(
    "ivanti_pcs_example_member_guid",
    _METADATA,
    # Not a real `ForeignKey("ab_user.id")`: that constraint needs `ab_user`
    # registered on the SAME `MetaData` object to resolve, and this table's
    # own `_METADATA` is deliberately standalone (this package must not
    # import, let alone mutate, Superset's own model metadata). `user_id`
    # is still an `ab_user.id` value by convention; nothing enforces it at
    # the schema level, the same trade-off any standalone extension table
    # outside an app's own migrations makes.
    sa.Column("user_id", sa.Integer, primary_key=True),
    sa.Column("member_guid", sa.String(64), nullable=False, unique=True, index=True),
)


def install() -> None:
    """One-time setup for this example's toy attribute table -- run this
    from a CLI context (`flask shell`, or any other maintenance
    entrypoint) BEFORE the table's first use, documented in this package's
    README §3 and the manual test round's (d).

    I-6: `member_guid`/`user_for_member_guid`/`set_member_guid` used to
    call a lazy `_ensure_table()` on first use, so `CREATE TABLE
    ivanti_pcs_example_member_guid` could run inside the FIRST
    access-decision web request, or the first `plugin verify` run, that
    happened to reach this class -- DDL from inside a request is not
    something a production deployment's web path should ever do. Those
    three now assume the table already exists; call this once, first,
    instead. A real Ivanti integration's own attribute store already
    exists on their side -- this function only matters for this package's
    own toy table."""
    from superset import db

    member_guid_table.create(bind=db.engine, checkfirst=True)


def set_member_guid(user: Any, guid: str) -> None:
    """Record `user`'s member GUID -- the `flask shell` snippet above, and
    what a real JIT login integration would call instead at provisioning
    time. Replaces any GUID already on file for this account.

    Assumes `install()` has already created the table (I-6): this is
    itself normally run from a CLI context, but does not issue DDL of its
    own so a caller that reaches it from elsewhere never triggers a
    `CREATE TABLE` by surprise."""
    from superset import db

    db.session.execute(
        sa.delete(member_guid_table).where(member_guid_table.c.user_id == user.id)
    )
    db.session.execute(
        member_guid_table.insert().values(user_id=user.id, member_guid=guid)
    )
    db.session.commit()


class AttributeIdentity(DefaultIdentity):
    """Reads/writes the member GUID from `member_guid_table` -- see the
    module docstring for why not `user.extra_attributes` -- and falls back
    to `DefaultIdentity`'s username/email scan when a user has no row yet
    (a brand-new account, or one created outside the JIT flow, still
    resolves). `tenant_guid`, `display_name` and `normalize_subject` are
    inherited unchanged, per the contract's "override only what needs it"
    rule (§9); `member_guid` and `user_for_member_guid` are the matched
    forward/reverse pair this override actually needs."""

    protocol_version = 1

    def member_guid(self, user: Any) -> str | None:
        # I-6: `None` for no user, like `DefaultIdentity`/the protocol's own
        # docstring say ("or None for no user") -- `current_member_guid()`
        # passes `getattr(g, "user", None)`, `member_ref(int)` passes
        # `None` for a deleted account, and both used to hit
        # `user.id` on `None` here (an `AttributeError`) on paths where
        # `DefaultIdentity` answers `None` cleanly.
        if user is None:
            return None
        from superset import db

        row = (
            db.session.execute(
                member_guid_table.select().where(member_guid_table.c.user_id == user.id)
            )
            .mappings()
            .first()
        )
        if row is not None:
            return row["member_guid"]
        return super().member_guid(user)

    def user_for_member_guid(self, guid: str) -> Any:
        from superset import db, security_manager

        row = (
            db.session.execute(
                member_guid_table.select().where(
                    member_guid_table.c.member_guid == guid
                )
            )
            .mappings()
            .first()
        )
        if row is not None:
            user = security_manager.get_user_by_id(row["user_id"])
            # Part 2's forward-mapping rule: only accept a reverse match
            # that the forward direction agrees with.
            if user is not None and self.member_guid(user) == guid:
                return user
        # `DefaultIdentity.user_for_member_guid` (part 2, H-3) -- not yet on
        # every branch this example is exercised from; fall back to it when
        # present, else there is nothing more this override can answer.
        base = getattr(super(), "user_for_member_guid", None)
        return base(guid) if base is not None else None

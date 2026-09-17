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
"""`qa/ivanti_pcs_example/` is NOT installed and NOT on `PYTHONPATH` by
default (see its README) -- these tests add it explicitly, the same way the
manual test round does with `PYTHONPATH=/app/docker/pythonpath_dev:/app/qa`.

Two things are checked:

  1. Structural conformance: each example class has exactly the methods the
     protocol it stands in for requires (spec §6 `Identity`, §7.1
     `Directory`, §8 `OpenFGAAuthorizer`'s two reference hooks), asserted
     both ways -- `hasattr`/`callable` by name (a precise "missing: X" on
     failure) AND `isinstance` against the real, `@runtime_checkable`
     `Directory`/`Identity` protocols, since those are on this branch now.
  2. Documentation: `qa/ivanti_pcs_example/README.md` names every
     configuration key the plugin contract's §3 table defines
     (`qa/design/directory-hook/01-plugin-contract.md`), so the "what
     Ivanti implements" page can never drift silently out of sync with the
     contract it is adapted from.
"""

from __future__ import annotations

import os
import sys

import pytest


def _find_qa_dir(start: str, *, max_levels: int = 8) -> str | None:
    """Search upward from `start` for a `qa/ivanti_pcs_example` directory,
    rather than assume a fixed number of levels (PR90 review T-1: the
    original "tests/ -> superset_ownership/ -> pythonpath_dev/ -> docker/
    -> repo root" count only held for the exact
    `docker/pythonpath_dev/superset_ownership/tests` layout, and broke --
    with a confusing "no such file", not a clear reason -- the moment a
    container copy used any other one, including the flat copy every
    earlier ad hoc reproduction of this module used)."""
    current = os.path.abspath(start)
    for _ in range(max_levels):
        if os.path.isdir(os.path.join(current, "qa", "ivanti_pcs_example")):
            return os.path.join(current, "qa")
        parent = os.path.dirname(current)
        if parent == current:  # filesystem root
            break
        current = parent
    return None


QA_DIR = _find_qa_dir(os.path.dirname(os.path.abspath(__file__)))

if QA_DIR is None:
    pytest.skip(
        "qa/ivanti_pcs_example not found searching upward from "
        f"{os.path.dirname(os.path.abspath(__file__))} -- this file needs "
        "the repo's qa/ directory reachable from an ancestor of this "
        "package's own directory (a flat copy of the package alone will "
        "not have it)",
        allow_module_level=True,
    )

README_PATH = os.path.join(QA_DIR, "ivanti_pcs_example", "README.md")


@pytest.fixture
def example_on_path():
    """Puts `qa/` on `sys.path` for the duration of one test, exactly as the
    manual round's `PYTHONPATH=/app/docker/pythonpath_dev:/app/qa` does --
    and removes it afterward, so this package's absence-by-default is not
    accidentally undone for every other test in the run."""
    added = QA_DIR not in sys.path
    if added:
        sys.path.insert(0, QA_DIR)
    for name in list(sys.modules):
        if name == "ivanti_pcs_example" or name.startswith("ivanti_pcs_example."):
            del sys.modules[name]
    try:
        yield
    finally:
        if added:
            sys.path.remove(QA_DIR)
        for name in list(sys.modules):
            if name == "ivanti_pcs_example" or name.startswith("ivanti_pcs_example."):
                del sys.modules[name]


def test_not_on_the_path_by_default():
    assert QA_DIR not in sys.path
    assert "ivanti_pcs_example" not in sys.modules


# ---------------------------------------------------------------------------
# identity.py
#
# `AttributeIdentity` subclasses `superset_ownership.identity.DefaultIdentity`
# (spec §6) directly, like any other override in this package -- an ordinary
# class, not built lazily (PR90 review M-5: an earlier draft built it behind
# a module `__getattr__` because `DefaultIdentity` had not landed yet; the
# boot log named the class
# `ivanti_pcs_example.identity:_build_attribute_identity.<locals>.AttributeIdentity`
# because of it, which is not what Ivanti's own boot would show).
# ---------------------------------------------------------------------------


def test_identity_module_imports(example_on_path):
    import ivanti_pcs_example.identity as example_identity

    # The table this override reads/writes, not a dict-shaped attribute
    # (PR90 review N-1) -- proves the module imports cleanly without
    # needing an app/DB at all (I-6: the table is no longer created
    # lazily on first use; `install()` -- a CLI-only, never-from-a-
    # web-request step -- creates it, exercised explicitly by the tests
    # below that touch the table).
    assert example_identity.member_guid_table.name == "ivanti_pcs_example_member_guid"


def test_attribute_identity_subclasses_default_identity(example_on_path):
    import ivanti_pcs_example.identity as example_identity
    from superset_ownership.identity import DefaultIdentity, Identity

    cls = example_identity.AttributeIdentity
    assert issubclass(cls, DefaultIdentity)
    assert cls.protocol_version == 1
    # An ordinary class object -- the qualname a boot log would print names
    # the real class, not a closure inside a builder function.
    assert cls.__qualname__ == "AttributeIdentity"
    assert isinstance(cls(), Identity)


def test_attribute_identity_round_trips_on_a_real_superset_user(
    example_on_path, harness
):
    """N-1: driven against a REAL FAB user (`harness.world`), not a
    dataclass standing in for one -- the dataclass-backed version of this
    test passed while `AttributeIdentity` was permanently inert against an
    actual account (`user.extra_attributes` is FAB's own `UserAttribute`
    relationship, not a dict; setting it to one raised `TypeError:
    Incompatible collection type`, and the reverse lookup's `->>` operator
    has no meaning against a relationship at all). Round-trips
    `member_guid` -> `user_for_member_guid`, matched by part 2's
    forward-mapping rule; the full chain through `normalize_subject`
    additionally needs part 2's own `DefaultIdentity` wiring, not on this
    branch yet -- see the comment where that assertion would go."""
    import ivanti_pcs_example.identity as example_identity

    new_guid = "6fbe1a2c-9d34-4a1b-8c2e-7f5a3b9d1e60"
    with harness.ctx():
        # I-6: the table is no longer created lazily on first use --
        # `install()` is the documented, CLI-only setup step (README §3,
        # runbook (d)); a test that touches the table calls it explicitly,
        # the same way a real deployer would from a `flask shell` before
        # first use.
        example_identity.install()

        from superset import security_manager

        w = harness.world
        user = security_manager.get_user_by_id(w.ben.id)
        identity = example_identity.AttributeIdentity()

        # Before this package's attribute is set: falls back to
        # DefaultIdentity's own scan -- the harness world's usernames are
        # already GUID-shaped (`superset_harness.guid(2)`), so that scan
        # finds Ben's username directly, without reaching this override's
        # table at all.
        assert identity.member_guid(user) == w.ben.username

        example_identity.set_member_guid(user, new_guid)
        user = security_manager.get_user_by_id(w.ben.id)  # re-fetch post-commit

        # Forward: account -> GUID.
        assert identity.member_guid(user) == new_guid
        # Reverse: GUID -> account (part 2's H-3), matched against the
        # forward direction (part 2's forward-mapping rule).
        found = identity.user_for_member_guid(new_guid)
        assert found is not None
        assert found.id == w.ben.id
        # A GUID nobody has must not resolve to anybody.
        assert (
            identity.user_for_member_guid("00000000-0000-4000-8000-000000000000")
            is None
        )

        # `normalize_subject` (inherited from `DefaultIdentity`) consults
        # `user_for_member_guid` on its reverse path (H-3/H-1): `self` in
        # `_default_normalize_subject` is this `AttributeIdentity` instance,
        # so its own overridden `member_guid`/`user_for_member_guid` are the
        # ones asked, and the relocated GUID resolves to the same reference
        # `member_ref` would build directly.
        from superset_ownership.identity import member_ref

        assert identity.normalize_subject(f"user:{new_guid}") == member_ref(user)
        # A GUID nobody has still names nobody.
        assert (
            identity.normalize_subject("user:00000000-0000-4000-8000-000000000000")
            is None
        )


def test_attribute_identity_falls_back_to_default_identity_when_unset(
    example_on_path, harness
):
    """An account with no row in `member_guid_table` (and a non-GUID
    username/email, so `DefaultIdentity`'s own scan can't find a GUID
    either) still resolves, through the `local-<id>` placeholder."""
    import ivanti_pcs_example.identity as example_identity

    with harness.ctx():
        example_identity.install()  # I-6: explicit, CLI-shaped setup step
        identity = example_identity.AttributeIdentity()
        # The harness world's stock people all have GUID-shaped usernames
        # (`superset_harness.guid(n)`); a plain one is needed to actually
        # reach the placeholder instead of DefaultIdentity's username scan.
        person = harness.add_person("plainname", "PlainName", "Gamma")
        from superset import security_manager

        user = security_manager.get_user_by_id(person.id)
        assert identity.member_guid(user) == f"local-{person.id}"


# ---------------------------------------------------------------------------
# directory.py -- structural conformance to spec §7.1's Directory protocol
# ---------------------------------------------------------------------------

DIRECTORY_METHODS = (
    "search_users",
    "user_in_tenant",
    "list_groups",
    "group_members",
    "user_in_group",
    "group_exists",
    "tenant_administrators",
    "health",
)


def test_fixed_groups_directory_has_every_directory_method(example_on_path):
    from ivanti_pcs_example.directory import FixedGroupsDirectory
    from superset_ownership.directory import Directory

    directory = FixedGroupsDirectory()
    assert directory.protocol_version == 1
    for name in DIRECTORY_METHODS:
        assert callable(getattr(directory, name, None)), (
            f"missing Directory method: {name}"
        )
    # The real, `@runtime_checkable` protocol -- not only the named-method
    # check above (PR90 review M-5: `superset_ownership.directory` is on
    # this branch now).
    assert isinstance(directory, Directory)


def test_broken_directory_is_missing_health_only(example_on_path):
    from ivanti_pcs_example.directory import Broken
    from superset_ownership.directory import Directory

    broken = Broken()
    assert not hasattr(broken, "health"), (
        "Broken must be missing `health` -- that is the fixture the manual "
        "test round's negative case (§(c)6) depends on"
    )
    for name in DIRECTORY_METHODS:
        if name == "health":
            continue
        assert callable(getattr(broken, name, None)), (
            f"missing Directory method: {name}"
        )
    assert not isinstance(broken, Directory), (
        "missing health: must not satisfy Directory"
    )


def test_fixed_groups_directory_lists_exactly_two_groups(example_on_path):
    from ivanti_pcs_example.directory import FixedGroupsDirectory
    from superset_ownership.identity import split_group_id

    directory = FixedGroupsDirectory()
    tenant = "a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40"
    page = directory.list_groups(tenant)
    assert "items" in page
    assert "next_cursor" in page
    # I-3: `id` is the store OBJECT reference (`group:<id>`, contract §4.3)
    # -- a bare id (what `.split("_")[0]` alone would still pass against)
    # is the exact defect this test used to miss: the picker's value for
    # such a row is rejected by the share route outright. Round-tripped
    # through `split_group_id` -- the same parser `/subjects` and
    # `_validate_subject` use -- rather than re-deriving the format here.
    for item in page["items"]:
        assert item["id"].startswith("group:"), item["id"]
        assert split_group_id(item["id"]) is not None, item["id"]
    parsed = {item["id"]: split_group_id(item["id"]) for item in page["items"]}
    names = {name for name, _ in parsed.values()}
    assert names == {"blue", "green"}
    for _name, group_tenant in parsed.values():
        assert group_tenant == tenant.lower()
    for item in page["items"]:
        assert set(item) >= {"id", "display_name", "tenant", "members"}


def test_fixed_groups_directory_group_picker_value_round_trips_through_validation(
    example_on_path, monkeypatch
):
    """I-3: the exact picker VALUE `/subjects` would render for a row this
    directory answers (`f"{grp['id']}#member"`) must be accepted by
    `_validate_subject` -- the share route's own gate, not re-derived here
    -- rather than rejected as malformed. Before the fix,
    `FixedGroupsDirectory.list_groups` built `id` from `identity.group_id()`
    (the bare id, `blue_<tenant>`), and `/subjects` renders
    `f"{grp['id']}#member"` verbatim, so the picker's own value for an
    example-directory row (`blue_<tenant>#member`) was missing the
    `group:` object prefix and `_validate_subject` rejected it outright
    (`400 subject must be 'user:<guid>' or 'group:<name>#member'`) --
    runbook (c4) only ever passed because its curl hard-coded the prefix."""
    from ivanti_pcs_example.directory import FixedGroupsDirectory
    from superset_ownership import api

    tenant = "a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40"
    directory = FixedGroupsDirectory()
    page = directory.list_groups(tenant)
    row = page["items"][0]
    # Exactly what `api.py::_subjects_group_rows` builds for the picker.
    picker_value = f"{row['id']}#member"

    monkeypatch.setattr(api, "get_directory", lambda: directory)

    class _Role:
        def __init__(self, name):
            self.name = name

    class _Caller:
        id = 999
        roles = [_Role(f"tenant_{tenant}")]

    ok, why = api._validate_subject(picker_value, _Caller())
    assert ok is True, why


def test_fixed_groups_directory_search_users_paginates_for_real(
    example_on_path, harness
):
    """`limit=1` walked to completion must return the same set `limit=100`
    does in one page -- an example that slices `items[:limit]` and always
    reports `next_cursor: None` agrees trivially instead of being exercised
    (PR90 review H-3; part 2 made `OpenFGADirectory.search_users` paginate
    for real, so the worked example shipping to Ivanti has to as well)."""
    from ivanti_pcs_example.directory import FixedGroupsDirectory
    from superset_harness import guid

    tenant = "c7a1b2d3-4e5f-4061-8a2b-3c4d5e6f7081"
    role = f"tenant_{tenant}"
    people = [
        harness.add_person(guid(0x50 + n), f"Pager{n}", "Gamma", role) for n in range(4)
    ]
    directory = FixedGroupsDirectory()

    def walk(limit: int) -> list[str]:
        guids: list[str] = []
        cursor = None
        while True:
            page = directory.search_users(tenant, "", limit=limit, cursor=cursor)
            guids.extend(u["guid"] for u in page["items"])
            cursor = page.get("next_cursor")
            if not cursor:
                break
        return guids

    with harness.ctx():
        small = walk(1)
        large = walk(100)
    assert len(small) == len(set(small)), "no duplicate across small pages"
    # The guid is the store id: `<tenant>.<member>` (PR #113).
    assert set(small) == set(large) == {p.ref.removeprefix("user:") for p in people}


def test_fixed_groups_directory_group_exists_and_not_found(example_on_path):
    from ivanti_pcs_example.directory import FixedGroupsDirectory
    from superset_ownership.identity import group_id

    directory = FixedGroupsDirectory()
    tenant = "a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40"
    assert directory.group_exists(group_id("blue", tenant)) is True
    assert directory.group_exists(f"eng_{tenant}") is False


def test_example_user_in_group_takes_a_tenant_admin_userset_apart(
    example_on_path, monkeypatch
):
    """PR #113 review: `OWNERSHIP_USER_IN_GROUP` is asked about
    `tenant:<t>#admin` (the tenant-administrator question under Neurons'
    shape) as well as group ids; the example used to prepend `group:` to
    whatever arrived, so `group:tenant:<t>#admin` reached the store and
    nobody was ever an administrator through that hook."""
    from ivanti_pcs_example import hooks
    from superset_ownership import fga

    tenant = "a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40"
    member = f"{tenant}.3f0a91c7-2d84-4e63-9b15-7c4e8a2f6d31"
    asked: list[tuple[str, str, str]] = []

    def fake_check(user, relation, obj, **_kw):
        asked.append((user, relation, obj))
        return True

    monkeypatch.setattr(fga, "check", fake_check)
    assert hooks.user_in_group(member, f"tenant:{tenant}#admin") is True
    assert hooks.user_in_group(member, f"{tenant}.eng") is True
    assert hooks.user_in_group(member, f"group:{tenant}.eng#member") is True
    assert asked == [
        (f"user:{member}", "admin", f"tenant:{tenant}"),
        (f"user:{member}", "member", f"group:{tenant}.eng"),
        (f"user:{member}", "member", f"group:{tenant}.eng"),
    ]


def test_fixed_groups_directory_health_reports_ok(example_on_path):
    from ivanti_pcs_example.directory import FixedGroupsDirectory
    from superset_ownership.directory import DirectoryHealth

    health = FixedGroupsDirectory().health()
    # A real `DirectoryHealth` (spec §7.1), not a dict standing in for one --
    # `superset_ownership.directory` is on this branch now, so the example
    # package uses the real type like everything else does (PR90 review
    # M-5).
    assert isinstance(health, DirectoryHealth)
    assert health.ok is True


# ---------------------------------------------------------------------------
# authorizer.py -- the two OpenFGAAuthorizer reference-building hooks
# ---------------------------------------------------------------------------


def test_prefixed_id_authorizer_subclasses_openfga_authorizer(example_on_path):
    from ivanti_pcs_example.authorizer import PrefixedIdAuthorizer
    from superset_ownership.authz import OpenFGAAuthorizer

    assert issubclass(PrefixedIdAuthorizer, OpenFGAAuthorizer)


def test_prefixed_id_authorizer_object_ref_round_trips(example_on_path):
    from ivanti_pcs_example.authorizer import PrefixedIdAuthorizer

    authorizer = PrefixedIdAuthorizer.__new__(
        PrefixedIdAuthorizer
    )  # no __init__ needed
    ref = authorizer._object_ref("dashboard:abc-123")
    assert ref == "dashboard:pcs-abc-123"
    assert authorizer._from_object_ref(ref) == "dashboard:abc-123"


def test_prefixed_id_authorizer_subject_ref_round_trips(example_on_path):
    from ivanti_pcs_example.authorizer import PrefixedIdAuthorizer

    authorizer = PrefixedIdAuthorizer.__new__(PrefixedIdAuthorizer)
    ref = authorizer._subject_ref("user:3f0a91c7-2d84-4e63-9b15-7c4e8a2f6d31")
    assert ref == "user:neurons|3f0a91c7-2d84-4e63-9b15-7c4e8a2f6d31"
    assert (
        authorizer._from_subject_ref(ref) == "user:3f0a91c7-2d84-4e63-9b15-7c4e8a2f6d31"
    )

    # A non-"user:" subject (a group reference) passes through unchanged.
    group_ref = "group:blue_t1#member"
    assert authorizer._subject_ref(group_ref) == group_ref


# ---------------------------------------------------------------------------
# fga.py
# ---------------------------------------------------------------------------


def test_fga_connection_reads_token_file(example_on_path, tmp_path, monkeypatch):
    """`connection()` needs `superset_ownership.fga_connection.FgaConnection`
    (spec §5), which is on this branch now -- reads the bearer token fresh
    from `TOKEN_FILE` on every call (rotation without a restart)."""
    from ivanti_pcs_example import fga as example_fga

    token_file = tmp_path / "openfga.token"
    token_file.write_text("secret-1\n")
    monkeypatch.setattr(example_fga, "TOKEN_FILE", str(token_file))
    monkeypatch.setenv("OWNERSHIP_FGA_API_URL", "http://scratch:8080")
    monkeypatch.setenv("OWNERSHIP_FGA_STORE", "01H...")

    conn = example_fga.connection()
    assert conn.api_url == "http://scratch:8080"
    assert conn.store_id == "01H..."
    assert conn.headers == {"Authorization": "Bearer secret-1"}

    # Rotation: a later read picks up the new token, no process restart.
    token_file.write_text("secret-2\n")
    assert example_fga.connection().headers == {"Authorization": "Bearer secret-2"}


def test_fga_connection_with_no_token_file_omits_the_header(
    example_on_path, tmp_path, monkeypatch
):
    from ivanti_pcs_example import fga as example_fga

    monkeypatch.setattr(example_fga, "TOKEN_FILE", str(tmp_path / "absent.token"))
    monkeypatch.setenv("OWNERSHIP_FGA_API_URL", "http://scratch:8080")
    monkeypatch.setenv("OWNERSHIP_FGA_STORE", "01H...")

    assert example_fga.connection().headers == {}


def test_fga_connection_defaults_to_a_real_store_not_an_empty_one(
    example_on_path, tmp_path, monkeypatch
):
    """`OWNERSHIP_FGA_STORE` unset must not build a connection with
    `store_id == ""` -- every store-scoped request from it would target
    nothing. Falls back to the same default `fga_connection.StaticProvider`
    uses (PR90 review M-4)."""
    from ivanti_pcs_example import fga as example_fga
    from superset_ownership.fga_connection import DEFAULT_API_URL, DEFAULT_STORE_ID

    monkeypatch.setattr(example_fga, "TOKEN_FILE", str(tmp_path / "absent.token"))
    monkeypatch.delenv("OWNERSHIP_FGA_API_URL", raising=False)
    monkeypatch.delenv("OWNERSHIP_FGA_STORE", raising=False)

    conn = example_fga.connection()
    assert conn.api_url == DEFAULT_API_URL
    assert conn.store_id == DEFAULT_STORE_ID
    assert conn.store_id != ""


# ---------------------------------------------------------------------------
# hooks.py -- the sixteen function hooks (contract §4.5.2)
#
# The reference file: every hook answers the same question its default
# answers, but from a different source. Most of these tests need a real FAB
# user (the `harness` fixture, needing Superset) because the caller-side
# hooks read/write this package's own attribute tables through `superset.db`
# and check real FAB roles; the shape-side and decision-side hooks are pure
# and need neither.
# ---------------------------------------------------------------------------


def test_hooks_module_imports(example_on_path):
    import ivanti_pcs_example.hooks as hooks
    from ivanti_pcs_example.identity import member_guid_table

    assert hooks.tenant_table.name == "ivanti_pcs_example_tenant"
    # Reused, not redefined, from identity.py (the module docstring's point:
    # a deployment that already populated it for AttributeIdentity answers
    # the same fact for these hooks without a second table).
    assert hooks.member_guid_table is member_guid_table


def test_member_guid_reads_table_then_falls_back_to_default_computation(
    example_on_path, harness
):
    """A dedicated person, not `harness.world.ben` -- the session-scoped
    world is shared with `identity.py`'s own tests (same table, by design),
    so a fresh account is what actually has "no row yet" regardless of test
    order. A GUID of its own, too: `member_guid_table.member_guid` is
    UNIQUE, and `6fbe1a2c-...` is the one `identity.py`'s own test already
    assigns to `harness.world.ben`."""
    import ivanti_pcs_example.hooks as hooks
    from ivanti_pcs_example.identity import set_member_guid

    new_guid = "9c8b7a6f-5e4d-4c3b-8a2f-1e0d9c8b7a6f"
    with harness.ctx():
        hooks.install()
        person = harness.add_person(
            "member-guid-table-test", "MemberGuidTableTest", "Gamma"
        )
        from superset import security_manager

        user = security_manager.get_user_by_id(person.id)
        # No row yet: falls back to `DefaultIdentity`'s own computation
        # (H-2c) -- a non-GUID username/email answers `local-<id>`, NOT the
        # bare username, which is neither a GUID nor that documented
        # placeholder.
        assert hooks.member_guid(user) == f"local-{person.id}"

        set_member_guid(user, new_guid)
        user = security_manager.get_user_by_id(person.id)  # re-fetch post-commit
        assert hooks.member_guid(user) == new_guid


def test_member_guid_falls_back_to_the_same_placeholder_as_the_default(
    example_on_path, harness
):
    """A plain (non-GUID) username with no attribute row: falls back to
    `DefaultIdentity`'s own computation (H-2c) -- `local-<id>`, the
    documented shape (§4.5.2 row 1), not the bare username (which is
    neither a GUID nor that placeholder, and used to be what this hook
    fell back to -- the exact false-FAIL H-2 found in `plugin verify`'s
    `caller_side` check against this very hook)."""
    import ivanti_pcs_example.hooks as hooks

    with harness.ctx():
        hooks.install()
        person = harness.add_person("plainname-hook", "PlainNameHook", "Gamma")
        from superset import security_manager

        user = security_manager.get_user_by_id(person.id)
        assert hooks.member_guid(user) == f"local-{person.id}"


def test_tenant_guid_reads_table_then_falls_back_to_role(example_on_path, harness):
    import ivanti_pcs_example.hooks as hooks

    tenant = "c7a1b2d3-4e5f-4061-8a2b-3c4d5e6f7081"
    other_tenant = "a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40"
    with harness.ctx():
        hooks.install()
        person = harness.add_person(
            "tenant-guid-test", "TenantGuidTest", "Gamma", f"tenant_{tenant}"
        )
        from superset import security_manager

        user = security_manager.get_user_by_id(person.id)
        # No row yet: falls back to the tenant_<guid> role, via
        # DefaultIdentity called directly (never identity.resolve_tenant_guid
        # -- the module docstring's recursion note).
        assert hooks.tenant_guid(user) == tenant

        hooks.set_tenant(user, other_tenant)
        user = security_manager.get_user_by_id(person.id)
        # The table now wins over the role.
        assert hooks.tenant_guid(user) == other_tenant


def test_sync_tenants_from_roles_backfills_the_table(example_on_path, harness):
    import ivanti_pcs_example.hooks as hooks

    tenant = "b2c3d4e5-6f70-4081-9a2b-3c4d5e6f7092"
    with harness.ctx():
        hooks.install()
        person = harness.add_person(
            "sync-test", "SyncTest", "Gamma", f"tenant_{tenant}"
        )
        from superset import db, security_manager

        count = hooks.sync_tenants_from_roles()
        assert count >= 1
        row = (
            db.session.execute(
                hooks.tenant_table.select().where(
                    hooks.tenant_table.c.user_id == person.id
                )
            )
            .mappings()
            .first()
        )
        assert row is not None
        assert row["tenant_guid"] == tenant
        user = security_manager.get_user_by_id(person.id)
        assert hooks.tenant_guid(user) == tenant


def test_display_name_is_last_comma_first(example_on_path, harness):
    """Deliberately the opposite of the default's "First Last" -- see
    `hooks.display_name`'s own docstring; a screenshot alone proves the
    hook is live."""
    import ivanti_pcs_example.hooks as hooks

    with harness.ctx():
        from superset import security_manager

        w = harness.world
        user = security_manager.get_user_by_id(w.ben.id)
        default_order = f"{user.first_name} {user.last_name}"
        assert hooks.display_name(user) == f"{user.last_name}, {user.first_name}"
        assert hooks.display_name(user) != default_order


def test_display_name_falls_back_to_username(example_on_path, harness):
    import ivanti_pcs_example.hooks as hooks

    with harness.ctx():
        from superset import db, security_manager

        w = harness.world
        user = security_manager.get_user_by_id(w.ben.id)
        user.first_name = ""
        user.last_name = ""
        db.session.commit()
        try:
            assert hooks.display_name(user) == user.username
        finally:
            user.first_name, user.last_name = "Ben", "Test"
            db.session.commit()


def test_is_tenant_administrator_prefers_the_role_over_the_store(
    example_on_path, harness
):
    """ "Our source first, store second" (`hooks.is_tenant_administrator`'s
    docstring): a `neurons_tenant_admin_<tenant>` role holder is an
    administrator even with no matching store group at all."""
    import ivanti_pcs_example.hooks as hooks

    tenant = "d3e4f5a6-7081-4092-8a3c-4d5e6f708193"
    with harness.ctx():
        hooks.install()
        person = harness.add_person(
            "role-admin-test",
            "RoleAdminTest",
            "Gamma",
            f"tenant_{tenant}",
            f"neurons_tenant_admin_{tenant}",
        )
        from superset import security_manager

        user = security_manager.get_user_by_id(person.id)
        assert hooks.is_tenant_administrator(user) is True


def test_is_tenant_administrator_false_with_neither_role_nor_group(
    example_on_path, harness
):
    import ivanti_pcs_example.hooks as hooks

    tenant = "e4f5a6b7-8091-40a3-8b4c-5d6e70819234"
    with harness.ctx():
        hooks.install()
        person = harness.add_person(
            "no-admin-test", "NoAdminTest", "Gamma", f"tenant_{tenant}"
        )
        from superset import security_manager

        user = security_manager.get_user_by_id(person.id)
        # No neurons_tenant_admin_<tenant> role, and the harness's Directory
        # seam (CountingDirectory, backed by the in-memory counting store --
        # conftest.py's superset_world fixture) has no group registered for
        # this tenant's administrator group either: falls through cleanly to
        # False rather than raising.
        assert hooks.is_tenant_administrator(user) is False


def test_user_for_member_guid_round_trips_with_forward_mapping_check(
    example_on_path, harness
):
    import ivanti_pcs_example.hooks as hooks
    from ivanti_pcs_example.identity import set_member_guid

    new_guid = "f5a6b7c8-9021-4a34-8b5c-6d7e80819345"
    with harness.ctx():
        hooks.install()
        from superset import security_manager

        w = harness.world
        user = security_manager.get_user_by_id(w.ben.id)
        set_member_guid(user, new_guid)
        user = security_manager.get_user_by_id(w.ben.id)

        found = hooks.user_for_member_guid(new_guid)
        assert found is not None
        assert found.id == w.ben.id

        # A GUID nobody has must not resolve to anybody.
        assert (
            hooks.user_for_member_guid("00000000-0000-4000-8000-000000000000") is None
        )


def test_user_for_member_guid_falls_back_to_username_lookup(example_on_path, harness):
    """No attribute row, but a GUID-shaped username: `member_guid`'s own
    fallback (`DefaultIdentity`'s scan) finds the GUID on the username, and
    the reverse direction finds the same account back -- the
    forward-mapping check (§4.2) this hook applies to every candidate."""
    import ivanti_pcs_example.hooks as hooks

    guid_username = "6fbe1a2c-9d34-4a1b-8c2e-7f5a3b9d1e61"
    with harness.ctx():
        hooks.install()
        person = harness.add_person("plain-reverse-hook", "PlainReverseHook", "Gamma")
        from superset import security_manager, db

        user = security_manager.get_user_by_id(person.id)
        user.username = guid_username
        db.session.commit()
        user = security_manager.get_user_by_id(person.id)  # re-fetch post-commit
        assert hooks.member_guid(user) == guid_username
        found = hooks.user_for_member_guid(guid_username)
        assert found is not None
        assert found.id == person.id


def test_user_for_member_guid_cannot_reverse_the_local_id_placeholder(
    example_on_path, harness
):
    """No attribute row and no GUID anywhere on the account: `member_guid`
    falls back to `local-<id>` (H-2c) -- a placeholder that carries no
    lookup key (not a username, not an email), so the reverse direction
    cannot invert it. The SAME asymmetry `DefaultIdentity`'s own
    `_default_user_for_member_guid` has (it also only tries
    username/email); documented here so it is not mistaken for a bug in
    this hook specifically."""
    import ivanti_pcs_example.hooks as hooks

    with harness.ctx():
        hooks.install()
        person = harness.add_person(
            "plain-reverse-hook-noguid", "PlainReverseHookNoGuid", "Gamma"
        )
        from superset import security_manager

        user = security_manager.get_user_by_id(person.id)
        placeholder = hooks.member_guid(user)
        assert placeholder == f"local-{person.id}"
        assert hooks.user_for_member_guid(placeholder) is None


# -- group_id / split_group_id: exact-inverse round trip ---------------------


def test_group_id_split_group_id_round_trip(example_on_path):
    import ivanti_pcs_example.hooks as hooks

    tenant = "a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40"
    for name in ("dashboard_designer", "eng", "blue"):
        built = hooks.group_id(name, tenant)
        assert built == f"{name}_{tenant}"
        assert hooks.split_group_id(built) == (name, tenant.lower())
        # The OBJECT and SUBJECT reference forms round-trip too.
        assert hooks.split_group_id(f"group:{built}") == (name, tenant.lower())
        assert hooks.split_group_id(f"group:{built}#member") == (name, tenant.lower())


def test_group_id_split_group_id_round_trip_for_tenant_administrator_group(
    example_on_path,
):
    import ivanti_pcs_example.hooks as hooks

    tenant = "b2c3d4e5-6f70-4081-9a2b-3c4d5e6f7092"
    group_ref = hooks.tenant_administrator_group(tenant)
    assert group_ref == f"group:tenant_administrator_{tenant}"
    assert hooks.split_group_id(group_ref) == ("tenant_administrator", tenant.lower())


def test_split_group_id_rejects_what_does_not_parse(example_on_path):
    import ivanti_pcs_example.hooks as hooks

    assert hooks.split_group_id(None) is None
    assert hooks.split_group_id("") is None
    assert hooks.split_group_id("not-a-group-id") is None
    assert hooks.split_group_id("blue_not-a-guid") is None


def test_group_display_name_is_title_case_with_spaces(example_on_path):
    import ivanti_pcs_example.hooks as hooks

    tenant = "a1e4c2d0-3b5f-4a91-8c2e-1f6a9d3b7c40"
    group_ref = hooks.group_id("dashboard_designer", tenant)
    assert hooks.group_display_name(group_ref) == "Dashboard Designer"
    assert hooks.group_display_name(f"group:{group_ref}") == "Dashboard Designer"
    assert hooks.group_display_name(f"group:{group_ref}#member") == "Dashboard Designer"


# -- can_manage: narrowing only -----------------------------------------------


def test_can_manage_passes_through_admin_owner_tenant_admin_unchanged(example_on_path):
    import ivanti_pcs_example.hooks as hooks

    for reason in ("admin", "owner", "tenant_admin"):
        state = {"default_reason": reason, "visibility": "private"}
        assert hooks.can_manage(None, state) == reason


def test_can_manage_narrows_manage_permission_on_private_objects(example_on_path):
    import ivanti_pcs_example.hooks as hooks

    state = {"default_reason": "manage_permission", "visibility": "private"}
    assert hooks.can_manage(None, state) is None


def test_can_manage_leaves_manage_permission_on_non_private_objects(example_on_path):
    import ivanti_pcs_example.hooks as hooks

    for visibility in ("tenant", "public", None):
        state = {"default_reason": "manage_permission", "visibility": visibility}
        assert hooks.can_manage(None, state) == "manage_permission"


def test_can_manage_passes_through_none(example_on_path):
    import ivanti_pcs_example.hooks as hooks

    state = {"default_reason": None, "visibility": "private"}
    assert hooks.can_manage(None, state) is None


# -- the loader: the full config block resolves ------------------------------


@pytest.fixture
def hooks_flask_app():
    pytest.importorskip("flask")
    from flask import Flask

    return Flask("ivanti-pcs-example-hooks-loader-test")


def test_config_hooks_example_loads_all_sixteen_as_config(
    example_on_path, hooks_flask_app, monkeypatch
):
    """`config_hooks_example.py`'s block, loaded through the real
    `plugins.load(strict=True)`, resolves every one of the sixteen hooks to
    this package's `hooks.py`, and `describe()["hooks"]` reports each as
    `source: "config"` with its exact dotted path (contract §4.5.1 item 7)."""
    for key in list(os.environ):
        if key.startswith("OWNERSHIP_"):
            monkeypatch.delenv(key, raising=False)

    import ivanti_pcs_example.config_hooks_example as config
    from superset_ownership import plugins

    hook_settings = [
        name
        for name in vars(config)
        if name.startswith("OWNERSHIP_")
        and name
        not in (
            "OWNERSHIP_AUTHORIZER",
            "OWNERSHIP_DIRECTORY",
            "OWNERSHIP_DIRECTORY_GROUP_WALK",
        )
    ]
    assert len(hook_settings) == 16, sorted(hook_settings)

    hooks_flask_app.config["OWNERSHIP_AUTHORIZER"] = config.OWNERSHIP_AUTHORIZER
    hooks_flask_app.config["OWNERSHIP_DIRECTORY"] = config.OWNERSHIP_DIRECTORY
    for name in hook_settings:
        hooks_flask_app.config[name] = getattr(config, name)

    reg = plugins.load(hooks_flask_app, strict=True)
    assert reg.failures == {}

    with hooks_flask_app.app_context():
        description = plugins.describe()

    for name in hook_settings:
        entry = description["hooks"][name]
        assert entry["source"] == "config", (name, entry)
        assert entry["path"] == getattr(config, name)


# ---------------------------------------------------------------------------
# README.md -- every §3 contract config key is documented
# ---------------------------------------------------------------------------

# qa/design/directory-hook/01-plugin-contract.md §3, "The configuration
# block Ivanti fills in" -- the Ivanti-facing key set (not the technical
# spec's full internal migration table, most of which is not something an
# override ever sets).
CONTRACT_SECTION_3_KEYS = (
    "OWNERSHIP_ENABLED",
    "OWNERSHIP_AUTHORIZER",
    "OWNERSHIP_FGA_API_URL",
    "OWNERSHIP_FGA_STORE",
    "OWNERSHIP_FGA_MODEL",
    "OWNERSHIP_FGA_CREDENTIALS",
    "OWNERSHIP_FGA_CONFIG_PROVIDER",
    "OWNERSHIP_IDENTITY",
    "OWNERSHIP_DIRECTORY",
    "OWNERSHIP_GROUP_ID_FORMAT",
    "OWNERSHIP_MANAGE_PERMISSION",
    "OWNERSHIP_OUTBOX_ENABLED",
)


def test_readme_exists():
    assert os.path.isfile(README_PATH), README_PATH


def test_readme_documents_every_contract_config_key():
    with open(README_PATH, encoding="utf-8") as f:
        text = f.read()
    missing = [key for key in CONTRACT_SECTION_3_KEYS if key not in text]
    assert not missing, f"README.md is missing config key(s): {missing}"


def test_readme_names_every_class_this_package_ships():
    with open(README_PATH, encoding="utf-8") as f:
        text = f.read()
    for name in (
        "AttributeIdentity",
        "FixedGroupsDirectory",
        "PrefixedIdAuthorizer",
    ):
        assert name in text, f"README.md does not mention {name}"

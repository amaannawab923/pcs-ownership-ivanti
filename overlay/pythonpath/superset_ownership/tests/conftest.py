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
"""Make `superset_ownership` importable regardless of where pytest is invoked.

Most of these are PURE unit tests: no Flask app, no Superset, no Docker, no
Postgres. Each test gets a throwaway SQLite database, so they run anywhere
SQLAlchemy and Alembic are installed and finish in well under a second.

The exception is the authorization-model suite below, which needs Superset.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from superset_harness import Harness

# tests/ -> superset_ownership/ -> pythonpath_dev/  (the importable root)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))


@pytest.fixture(autouse=True)
def _clean_plugin_hooks_module_state() -> Iterator[None]:
    """`plugin_hooks.py`'s rate-limit/failure-count state (M-5) is
    deliberately process-global, not per-`HookRegistry` -- the whole point
    is to survive across app instances within one worker. That means it
    also survives across UNRELATED tests: a test in `test_plugin_verify.py`
    or `test_plugin_hooks.py` that makes a hook raise on purpose left
    `_FAILURE_COUNTS`/`_LOGGED` non-empty for whatever ran next, which broke
    `test_plugins.py::test_describe_shape_with_a_loaded_registry`'s "every
    hook is unset by default" assertion purely from test ORDER. Session-wide
    (every test file, not just one module's own tests) rather than
    duplicated per file."""
    from superset_ownership import plugin_hooks

    plugin_hooks._LOGGED.clear()  # noqa: SLF001
    plugin_hooks._SUPPRESSED_SINCE_LOG.clear()  # noqa: SLF001
    plugin_hooks._FAILURE_COUNTS.clear()  # noqa: SLF001
    plugin_hooks._WIDENING_WARNED.clear()  # noqa: SLF001
    yield
    plugin_hooks._LOGGED.clear()  # noqa: SLF001
    plugin_hooks._SUPPRESSED_SINCE_LOG.clear()  # noqa: SLF001
    plugin_hooks._FAILURE_COUNTS.clear()  # noqa: SLF001
    plugin_hooks._WIDENING_WARNED.clear()  # noqa: SLF001


# --- the Superset-backed authorization-model suite ---------------------------
#
# test_access_model.py and test_endpoints.py run against a REAL Superset
# application (superset_harness.py) with a counting fake where OpenFGA would
# be. They need `superset` importable, which it is in the container and not
# in the pure CI job; the `harness` fixture skips them there, and every test
# that uses it carries the `superset` marker so a run can report them as a
# group (`pytest -m superset`, `-rs`).

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "superset: needs an importable Superset; skipped where it is absent "
        "(the pure CI job), runs in the container",
    )


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "harness" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.superset)


@pytest.fixture(scope="session")
def superset_world(tmp_path_factory: pytest.TempPathFactory) -> Harness:
    """One application, one schema, one set of users for the whole session.
    Objects (charts, dashboards, shares) are created per test."""
    pytest.importorskip(
        "superset.app",
        reason=(
            "superset not importable; the authorization-model suite runs in the "
            "container"
        ),
    )
    import superset_harness as h
    from superset_ownership import authz, directory

    authorizer = h.CountingAuthorizer()
    # Selected the way a deployment selects a backend: by name, through
    # OWNERSHIP_AUTHORIZER (set in the generated config) and get_authorizer().
    authz._BACKENDS["counting"] = lambda: authorizer
    # Same for the Directory seam (OWNERSHIP_DIRECTORY = "counting", set in
    # the same generated config): a CountingDirectory sharing this SAME
    # authorizer's tuples/groups/calls, so a directory read and a relation
    # call land on the one CallLog the invariant assertions read.
    directory._DIRECTORIES["counting"] = lambda: h.CountingDirectory(authorizer)

    directory = str(tmp_path_factory.mktemp("ownership-app"))
    app = h.create_test_app(directory, ownership=True)
    world = h.populate(app)
    # The group exists in the authorization store only (no Superset role),
    # which is the OpenFGA deployment's shape.
    authorizer.groups[h.GROUP] = {world.dee.ref}
    return h.Harness(app, authorizer, world)


@pytest.fixture
def harness(superset_world: Harness) -> Iterator[Harness]:
    """The session world with the counters, failure switches, audit log and
    shared-layer caches reset for this test.

    `service.invalidate_all()` (issue #82) is the addition: the row cache
    (issue #75) never needed this -- it is keyed by object id, and every
    test creates its OWN fresh objects, so nothing an earlier test cached
    could ever be read by a later one. The list_objects cache is keyed by
    SUBJECT, and this fixture's world -- Ada, Ben, Cy, Dee, the group --
    is the SAME one every test in the session shares (`superset_world` is
    session-scoped).

    Hygiene, not load-bearing (review round 2, PR #100, question 9):
    without this reset the suite still passes, because every
    `create_chart(w.ada)` writes an owner tuple through `outbox.write_tuple`
    (invalidating Ada's own entry) and every share invalidates the target
    subject's -- between them, the subjects this fixture's tests actually
    read a cached list_objects answer FOR are almost always invalidated by
    something they themselves just did. Kept anyway: it is a cheap,
    explicit "start clean" that does not depend on that coincidence holding
    as the suite grows, most visibly for a GROUP share, whose fallback
    invalidation (`_invalidate_all_list_objects`, M-2) does not target any
    one member's entry precisely.
    """
    superset_world.authorizer.reset()
    superset_world.audit.clear()
    with superset_world.ctx():
        from superset_ownership import service

        service.invalidate_all()
    yield superset_world
    superset_world.authorizer.reset()

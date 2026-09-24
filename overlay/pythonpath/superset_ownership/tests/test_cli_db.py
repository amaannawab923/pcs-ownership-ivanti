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
"""`superset ownership db downgrade` must say what it is about to destroy.

Alembic downgrades TO a revision, so the command runs every step between
the current one and the target. Issue #121: from head, `downgrade
0002_ownership_outbox` drops `ownership_object.tenant_guid` on its way past
0004 -- an operator reaching for the outbox's own guard never sees 0002's
refusal, because the column is already gone by the time the chain gets
there. These tests pin the pre-flight that now stands in front of it.

The real `migrate.downgrade` is replaced throughout: what is under test is
whether the command REACHES it, not what Alembic then does (test_migrations
owns that).
"""

from __future__ import annotations

from typing import Any

import pytest
from click.testing import CliRunner

from superset_ownership import cli, migrate, outbox


@pytest.fixture
def run(harness: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Invoke `db downgrade` against the harness's application, with the
    migration itself stubbed out. Returns (result, calls)."""
    from flask.cli import ScriptInfo

    calls: list[str] = []
    monkeypatch.setattr(migrate, "downgrade", lambda rev: calls.append(rev))

    def _run(*args: str, stdin: str = "") -> tuple[Any, list[str]]:
        result = CliRunner().invoke(
            cli.db_downgrade,
            list(args),
            input=stdin,
            obj=ScriptInfo(create_app=lambda: harness.app),
        )
        return result, calls

    return _run


def test_a_downgrade_that_would_destroy_data_is_refused_and_names_the_steps(run):
    result, calls = run("0002_ownership_outbox")

    assert result.exit_code != 0
    assert calls == [], "the refusal has to come BEFORE alembic runs anything"
    assert "0004_ownership_object_tenant" in result.output
    assert "tenant_guid" in result.output, "it names what is lost, not just the step"
    assert "--yes" in result.output, "and how to mean it"


def test_the_refusal_also_counts_the_undelivered_outbox_rows(run, harness, monkeypatch):
    monkeypatch.setattr(
        outbox,
        "status",
        lambda: {"pending": 3, "claimed": 1, "dead": 2, "delivered": 0},
    )

    result, calls = run("base")

    assert calls == []
    assert "6 undelivered row(s)" in result.output


def test_an_unreadable_outbox_does_not_swallow_the_refusal(run, monkeypatch):
    """The count is a courtesy. If the table is already gone, or the database
    is unreachable, the operator still gets told what the downgrade destroys."""

    def _boom() -> dict[str, int]:
        raise RuntimeError("no such table: ownership_outbox")

    monkeypatch.setattr(outbox, "status", _boom)

    result, calls = run("base")

    assert result.exit_code != 0
    assert calls == []
    assert "0002_ownership_outbox" in result.output
    assert "undelivered row(s)" not in result.output, "no count, no crash"


def test_yes_proceeds(run):
    result, calls = run("base", "--yes")

    assert result.exit_code == 0, result.output
    assert calls == ["base"]


def test_a_harmless_downgrade_still_asks_before_running(run, monkeypatch):
    """0003 only drops constraints, so there is nothing to refuse -- but a
    downgrade is still a downgrade, and answering no runs nothing."""
    monkeypatch.setattr(migrate, "destructive_steps", lambda rev: [])

    refused, calls = run("0003_ownership_foreign_keys", stdin="n\n")
    assert refused.exit_code != 0
    assert calls == []

    accepted, calls = run("0003_ownership_foreign_keys", stdin="y\n")
    assert accepted.exit_code == 0, accepted.output
    assert calls == ["0003_ownership_foreign_keys"]


@pytest.mark.parametrize("target", ["0009_not_a_revision", "-- -1", "nope"])
def test_a_target_the_chain_cannot_resolve_is_refused(run, target):
    """Review round 1: an unresolvable target used to produce an EMPTY
    pre-flight, which reads as "nothing destructive" and waved the caller
    straight through to the prompt."""
    result, calls = run(*target.split(), stdin="y\n")

    assert result.exit_code != 0
    assert calls == []
    assert "does not name a revision" in result.output


def test_an_operators_short_revision_id_is_refused_like_the_long_one(run):
    """`db downgrade 0002` is the command issue #121 is named after. It
    reported nothing destructive because the pre-flight compared the raw
    string against full revision ids."""
    result, calls = run("0002")

    assert result.exit_code != 0
    assert calls == []
    assert "0004_ownership_object_tenant" in result.output
    assert "tenant_guid" in result.output

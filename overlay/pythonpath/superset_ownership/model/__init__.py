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
"""The authorization model artefact (spec §9).

``MODEL`` is the JSON body OpenFGA's ``POST /authorization-models`` accepts,
and is the single source of truth: it is what ``fga install-model`` writes
and what :func:`render_dsl` renders into ``model/ownership.fga`` for a human
to read. A test pins the two equal, so the DSL file can never drift from
what actually gets installed.

Derived from ``superset-events/ownership-spike/model.json`` plus
``qa/setup_fga.sh`` §4 (the ``tenant`` type and the ``tenant`` relation on
``dashboard``/``chart``), with one addition not yet live in any store this
module has touched: ``group.tenant``, so a tenant boundary can be asked of
a group the same way it already can of an object. Additive only -- every
tuple valid under a deployed store's existing model stays valid under this
one.

``REQUIRED`` is the minimum every relation-bearing type must declare;
``superset ownership fga show-model --check`` diffs a store's actual model
against it. It intentionally does not require ``group.tenant`` to already
exist anywhere except here -- a store that has not yet run
``install-model`` is expected to be missing it, and that is exactly the
gap the command is for.
"""

from __future__ import annotations

from typing import Any

SCHEMA_VERSION = "1.1"

MODEL: dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    "type_definitions": [
        {"type": "user"},
        {
            "type": "tenant",
            "relations": {
                "member": {"this": {}},
                # Confirmed by Ivanti: a tenant administrator is the `admin`
                # relation on the tenant object, held by users directly or
                # by a group's members, and kept current by their platform.
                # This module reads it and never writes it.
                "admin": {"this": {}},
            },
            "metadata": {
                "relations": {
                    "member": {"directly_related_user_types": [{"type": "user"}]},
                    "admin": {
                        "directly_related_user_types": [
                            {"type": "user"},
                            {"type": "group", "relation": "member"},
                        ]
                    },
                },
            },
        },
        {
            "type": "group",
            "relations": {
                "member": {"this": {}},
                "tenant": {"this": {}},
            },
            "metadata": {
                "relations": {
                    "member": {
                        "directly_related_user_types": [
                            {"type": "user"},
                            {"type": "group", "relation": "member"},
                        ],
                    },
                    "tenant": {
                        "directly_related_user_types": [
                            {"type": "tenant", "relation": "member"}
                        ],
                    },
                },
            },
        },
        {
            "type": "dashboard",
            "relations": {
                "owner": {"this": {}},
                "editor": {
                    "union": {
                        "child": [
                            {"this": {}},
                            {"computedUserset": {"relation": "owner"}},
                        ]
                    }
                },
                "viewer": {
                    "union": {
                        "child": [
                            {"this": {}},
                            {"computedUserset": {"relation": "editor"}},
                        ]
                    }
                },
                "tenant": {"this": {}},
            },
            "metadata": {
                "relations": {
                    "owner": {"directly_related_user_types": [{"type": "user"}]},
                    "editor": {
                        "directly_related_user_types": [
                            {"type": "user"},
                            {"type": "group", "relation": "member"},
                        ],
                    },
                    "viewer": {
                        "directly_related_user_types": [
                            {"type": "user"},
                            {"type": "group", "relation": "member"},
                        ],
                    },
                    "tenant": {
                        "directly_related_user_types": [
                            {"type": "tenant", "relation": "member"}
                        ],
                    },
                },
            },
        },
        {
            "type": "chart",
            "relations": {
                "owner": {"this": {}},
                "editor": {
                    "union": {
                        "child": [
                            {"this": {}},
                            {"computedUserset": {"relation": "owner"}},
                        ]
                    }
                },
                "viewer": {
                    "union": {
                        "child": [
                            {"this": {}},
                            {"computedUserset": {"relation": "editor"}},
                        ]
                    }
                },
                "tenant": {"this": {}},
            },
            "metadata": {
                "relations": {
                    "owner": {"directly_related_user_types": [{"type": "user"}]},
                    "editor": {
                        "directly_related_user_types": [
                            {"type": "user"},
                            {"type": "group", "relation": "member"},
                        ],
                    },
                    "viewer": {
                        "directly_related_user_types": [
                            {"type": "user"},
                            {"type": "group", "relation": "member"},
                        ],
                    },
                    "tenant": {
                        "directly_related_user_types": [
                            {"type": "tenant", "relation": "member"}
                        ],
                    },
                },
            },
        },
    ],
}

# type -> the relations `fga show-model --check` requires it to declare.
REQUIRED: dict[str, list[str]] = {
    "user": [],
    "tenant": ["member", "admin"],
    "group": ["member", "tenant"],
    "dashboard": ["owner", "editor", "viewer", "tenant"],
    "chart": ["owner", "editor", "viewer", "tenant"],
}


# The types this module OWNS in a store, and the ones it only REFERENCES.
# Ivanti publishes the store's authorization model (user, tenant, group and
# their platform's own types); the dashboard and chart types are ours to
# add. `merge_into` puts our types into a published model without touching
# theirs -- the model that results is what `install-model` writes when the
# store already has one.
OWNED_TYPES = ("dashboard", "chart")
REFERENCED_TYPES = ("user", "tenant", "group")
# The relations our reads and writes rely on in the referenced types; a
# published model missing one cannot serve this module (`missing_relations`).
REQUIRED_REFERENCED_RELATIONS = {
    "tenant": ("member", "admin"),
    "group": ("member",),
}


def _type_map(model_json: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {t["type"]: t for t in model_json.get("type_definitions", [])}


def merge_into(published: dict[str, Any]) -> dict[str, Any]:
    """`published` with this module's owned types added or replaced, its
    referenced types added where the published model has none, and -- on a
    referenced type it does have -- only the relations ours defines that
    theirs lacks added (`tenant#admin` on a model written before it was
    there; `group#tenant`, which only the group walk's fast path reads and
    which is harmless without a tuple). A relation the platform already
    defines is never rewritten: what it means is theirs, and adding a
    relation breaks no tuple. An OWNED type (`dashboard`, `chart`) is
    replaced whole, by design: its relations are this module's, and a
    relation someone had added to it is dropped. Everything else at the
    top level of the published model (`conditions`, and any key a later
    schema adds) is carried through untouched."""
    ours = _type_map(MODEL)
    out: list[dict[str, Any]] = []
    present: set[str] = set()
    for t in published.get("type_definitions", []):
        name = t["type"]
        present.add(name)
        if name in OWNED_TYPES:
            out.append(ours[name])
        elif name in ours:
            merged = dict(t)
            theirs_rel = dict(merged.get("relations") or {})
            theirs_meta = dict((merged.get("metadata") or {}).get("relations") or {})
            for rel, definition in (ours[name].get("relations") or {}).items():
                if rel not in theirs_rel:
                    theirs_rel[rel] = definition
                    meta = (ours[name].get("metadata") or {}).get("relations", {})
                    if rel in meta:
                        theirs_meta[rel] = meta[rel]
            if theirs_rel:
                merged["relations"] = theirs_rel
            if theirs_meta:
                merged["metadata"] = {
                    **(merged.get("metadata") or {}),
                    "relations": theirs_meta,
                }
            out.append(merged)
        else:
            out.append(t)
    for name in (*REFERENCED_TYPES, *OWNED_TYPES):
        if name not in present:
            out.append(ours[name])
    merged_model = {
        k: v
        for k, v in published.items()
        if k not in ("schema_version", "type_definitions", "id")
    }
    merged_model["schema_version"] = published.get("schema_version", SCHEMA_VERSION)
    merged_model["type_definitions"] = out
    return merged_model


def missing_relations(model_json: dict[str, Any]) -> list[str]:
    """`type#relation` pairs this module needs that `model_json` lacks:
    every relation of our owned types, and the referenced relations above.
    Empty means the model can serve the module."""
    have = {
        (t["type"], r)
        for t in model_json.get("type_definitions", [])
        for r in (t.get("relations") or {})
    }
    types = {t["type"] for t in model_json.get("type_definitions", [])}
    missing: list[str] = []
    for name in OWNED_TYPES:
        for r in _type_map(MODEL)[name]["relations"]:
            if (name, r) not in have:
                missing.append(f"{name}#{r}")
    for name, rels in REQUIRED_REFERENCED_RELATIONS.items():
        if name not in types:
            missing.append(name)
            continue
        for r in rels:
            if (name, r) not in have:
                missing.append(f"{name}#{r}")
    return missing


def _related_type_signature(
    ref: dict[str, Any],
) -> tuple[str, str | None, bool, str | None]:
    """One directly-related-user-type ref, normalised for comparison:
    ``(type, relation, is_wildcard, condition)``. ``condition`` is
    ``None`` for both "no condition" and the store's own added
    ``"condition": ""`` noise on every OTHER ref -- only a real condition
    name is a signature-affecting difference (R2-2)."""
    return (
        ref["type"],
        ref.get("relation"),
        "wildcard" in ref,
        ref.get("condition") or None,
    )


def model_signature(model_json: dict[str, Any]) -> tuple[str | None, frozenset]:
    """A comparison key for a model that is stable across a round trip
    through an OpenFGA store (spec §9, ``install-model`` S6): the store
    echoes back extra fields this module never sent (``condition: ""`` on
    every unconditioned userset ref, ``module``/``source_info``, an
    explicit ``"relations": {}, "metadata": null`` for a type with
    neither) and, per N8, relations do not necessarily come back in
    definition order either. Two models are "the same" for
    ``install-model``'s idempotency check when they declare the same
    ``schema_version`` (R2-2: schema 1.0 and 1.1 read type_definitions
    differently, so a bare version bump IS a real model change) and, per
    type, the same relations, the same directly-related
    ``(type, relation, wildcard, condition)`` tuples per relation (R2-2:
    a store that also grants ``user:*`` -- public, wildcard access -- or
    adds a real ABAC ``condition`` must NOT compare equal to a MODEL that
    grants neither; ``install-model`` would otherwise call a widened or
    conditioned store "already current" and leave it exactly as widened),
    and the same ``or <relation>`` rewrite -- regardless of key order or
    the store's own added noise. NOT a substitute for exact equality
    anywhere the literal JSON body matters (writing MODEL itself always
    sends the literal dict).
    """
    types: dict[str, frozenset] = {}
    for type_def in model_json.get("type_definitions", []):
        relations = type_def.get("relations") or {}
        metadata_relations = (type_def.get("metadata") or {}).get("relations") or {}
        rel_sig: dict[str, tuple[frozenset, str | None]] = {}
        for name, rewrite in relations.items():
            directly_related = metadata_relations.get(name, {}).get(
                "directly_related_user_types", []
            )
            related_types = frozenset(
                _related_type_signature(ref) for ref in directly_related
            )
            or_relation = None
            union = (rewrite or {}).get("union")
            if union:
                for child in union.get("child", []):
                    computed = child.get("computedUserset")
                    if computed:
                        or_relation = computed["relation"]
            rel_sig[name] = (related_types, or_relation)
        types[type_def["type"]] = frozenset(rel_sig.items())
    return model_json.get("schema_version"), frozenset(types.items())


def _userset_types(directly_related: list[dict[str, str]]) -> list[str]:
    out = []
    for ref in directly_related:
        if "wildcard" in ref:
            text = f"{ref['type']}:*"
        elif "relation" in ref:
            text = f"{ref['type']}#{ref['relation']}"
        else:
            text = ref["type"]
        condition = ref.get("condition")
        if condition:
            text = f"{text} with {condition}"
        out.append(text)
    return out


def _relation_line(
    name: str, rewrite: dict[str, Any], directly_related: list[dict[str, str]]
) -> str:
    """One ``define`` line: the directly-related types in brackets, plus an
    ``or <relation>`` suffix when the rewrite is a union with a
    computed-userset child (the only rewrite shape this model uses beyond a
    bare ``this``)."""
    types = _userset_types(directly_related)
    userset = f"[{', '.join(types)}]" if types else "[]"
    or_clause = ""
    union = rewrite.get("union")
    if union:
        for child in union.get("child", []):
            computed = child.get("computedUserset")
            if computed:
                or_clause = f" or {computed['relation']}"
    return f"    define {name}: {userset}{or_clause}"


def render_dsl(model: dict[str, Any]) -> str:
    """``MODEL`` (OpenFGA's JSON authorization-model shape) rendered as the
    FGA DSL text a human reads -- and what ``model/ownership.fga`` pins to,
    so the two can never drift apart (spec §9)."""
    lines: list[str] = ["model", f"  schema {model['schema_version']}", ""]
    for type_def in model["type_definitions"]:
        type_name = type_def["type"]
        relations = type_def.get("relations")
        lines.append(f"type {type_name}")
        if relations:
            lines.append("  relations")
            metadata_relations = type_def.get("metadata", {}).get("relations", {})
            for relation_name, rewrite in relations.items():
                directly_related = metadata_relations.get(relation_name, {}).get(
                    "directly_related_user_types", []
                )
                lines.append(_relation_line(relation_name, rewrite, directly_related))
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"

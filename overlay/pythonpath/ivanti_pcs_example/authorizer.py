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
"""An `Authorizer` override for a deployment whose OpenFGA model uses
different id shapes than ours (spec §8, issue #73): objects keyed
`dashboard:pcs-<uuid>` instead of `dashboard:<uuid>`, and users keyed
`user:neurons|<guid>` instead of `user:<guid>`.

`OpenFGAAuthorizer` builds every reference it sends to the store through
exactly two hooks -- `_object_ref` and `_subject_ref` -- and nothing else
formats a string, so an id-shape difference is a two-method subclass, not a
rewrite. `_from_object_ref`/`_from_subject_ref` invert them for the calls
that read references back out of the store (`list_objects`, `list_grants`,
`list_relations`).

Configure with:

    OWNERSHIP_AUTHORIZER = "ivanti_pcs_example.authorizer:PrefixedIdAuthorizer"
"""

from __future__ import annotations

from superset_ownership.authz import OpenFGAAuthorizer


class PrefixedIdAuthorizer(OpenFGAAuthorizer):
    """Their model keys objects as `dashboard:pcs-<uuid>` and users as
    `user:neurons|<guid>`."""

    def _object_ref(self, obj: str) -> str:
        type_, _, uuid_ = obj.partition(":")
        return f"{type_}:pcs-{uuid_}"

    def _from_object_ref(self, ref: str) -> str:
        type_, _, uuid_ = ref.partition(":")
        return f"{type_}:{uuid_.removeprefix('pcs-')}"

    def _subject_ref(self, subject: str) -> str:
        if subject.startswith("user:"):
            return subject.replace("user:", "user:neurons|", 1)
        return subject

    def _from_subject_ref(self, ref: str) -> str:
        return ref.replace("user:neurons|", "user:", 1)

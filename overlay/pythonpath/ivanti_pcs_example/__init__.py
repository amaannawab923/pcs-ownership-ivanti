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
"""A worked example override package for PCS-10243's plug-in seams.

NOT installed and NOT on `PYTHONPATH` by default -- see `README.md`. It
exists so the contract (`qa/design/directory-hook/01-plugin-contract.md`)
has a runnable answer to "what does an override actually look like", and so
the manual test round
(`qa/design/directory-hook/04-manual-test-round.md`) has something to point
`OWNERSHIP_IDENTITY`, `OWNERSHIP_DIRECTORY` and `OWNERSHIP_AUTHORIZER` at.

Every class here is deliberately small: each overrides exactly the one thing
a real Ivanti deployment might need to override, and falls back to (or
subclasses) this package's own default implementation for everything else --
the same shape `README.md` §9 of the contract asks Ivanti to follow.

`hooks.py` is the odd one out in scope, not in spirit: instead of one class
overriding one thing, it is every one of the contract's sixteen §4.5.2
function hooks, each answering the same question its default answers from a
different source -- `config_hooks_example.py` is the config block that wires
all sixteen in at once. See `README.md` §4.5.
"""

from __future__ import annotations

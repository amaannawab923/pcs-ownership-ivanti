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
superset_ownership -- prototype object-ownership module.

Wires into a running Superset instance purely through documented extension
points (AFTER_ASSET_CREATE, EXTRA_RAISE_FOR_ACCESS_BYPASS,
EXTRA_ACCESS_QUERY_FILTERS, BLUEPRINTS). See superset_config_docker_light.py
for the wiring.

Schema is managed by the module's own parallel Alembic chain (migrations/,
migrate.py) tracked in `alembic_version_ownership`, fully independent of
Superset's chain. Run `superset ownership db upgrade` after
`superset db upgrade`; see tests/test_migrations.py for the isolation and
adoption guarantees.
"""

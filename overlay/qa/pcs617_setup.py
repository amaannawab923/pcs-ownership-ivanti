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
from superset.app import create_app
app = create_app()
with app.app_context():
    from superset import db, security_manager as sm
    from superset.connectors.sqla.models import SqlaTable
    from superset_ownership.backfill import run as backfill
    n = backfill()
    print("MARKER backfill:", n)
    gamma = sm.find_role("Gamma")
    made = []
    for uname, tbl in (("vgs_user", "video_game_sales"), ("wb_user", "wb_health_population")):
        t = db.session.query(SqlaTable).filter_by(table_name=tbl).one_or_none()
        if t is None:
            print("MARKER missing dataset", tbl); continue
        role = sm.find_role(f"ds_{tbl}") or sm.add_role(f"ds_{tbl}")
        pv = sm.find_permission_view_menu("datasource_access", t.perm)
        if pv and pv not in role.permissions:
            role.permissions.append(pv)
        u = sm.find_user(username=uname)
        if not u:
            u = sm.add_user(uname, uname, "T", f"{uname}@x.com", [gamma, role], password="test1234")
        else:
            u.roles = [gamma, role]
        db.session.commit()
        made.append((uname, u.id, t.perm))
    for m in made: print("MARKER user", m)

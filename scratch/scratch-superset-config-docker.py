# scratch-only config layer -- mounted at /app/pythonpath/superset_config_docker.py
# for the scratch stack ONLY (see scratch/docker-compose.scratch.yml), in
# place of docker/demo-superset-config-docker.py. Wraps that file VERBATIM
# (same DATABASE_*/CACHE_CONFIG/SECRET_KEY/OPENFGA_STORE_ID-guard, then the
# real overlay/config/superset_config_docker.example.py) and adds exactly
# one more line the demo stack does not need.
#
# VIEWER_PROMISCUOUS_MODE = False: already Superset's own stock default
# (superset/config.py) -- nothing in this delivery's overlay touches it.
# Made EXPLICIT here because the scratch data model (scratch/README.md)
# depends on it: three example datasets are SHARED between both tenants
# (cleaned_sales_data, birth_names, video_game_sales) with a real
# tenant-scoped RLS filter per tenant role, and a chart or dashboard built
# on one of them can be SHARED across tenants by superset_ownership's own
# sharing feature. Promiscuous mode would let a viewer's OWN roles decide
# which RLS rules apply to a query made through someone else's chart/
# dashboard context, which can widen the visible rows past the viewer's
# actual tenant. With it False (the default), RLS is evaluated for the
# rows returned to a given querying context consistently, so a shared
# chart never shows one tenant's viewer the other tenant's rows -- the
# tenant_id filter stays authoritative regardless of who shared what with
# whom. See scratch/proof-queries.sh's API proof (log in as two different
# tenant users, fetch the SAME chart's data, the row counts must differ
# and match the tenant_id split) for this exercised end to end.
with open("/app/pythonpath/demo-superset-config-docker.py", "rb") as _fh:
    exec(compile(_fh.read(), "demo-superset-config-docker.py", "exec"))  # noqa: S102

VIEWER_PROMISCUOUS_MODE = False

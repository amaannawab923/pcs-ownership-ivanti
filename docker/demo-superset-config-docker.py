# DEMO ONLY -- not part of the delivered overlay (that's
# overlay/config/superset_config_docker.example.py, which this file wraps).
#
# A real PCS deployment already has its own working superset_config_docker.py
# (DATABASE_*/REDIS_* env-var wiring, SECRET_KEY, etc. -- see client-test
# main's docker/pythonpath_dev/superset_config.py for the pattern this
# mirrors) *before* the client ever adds the ownership blocks to it. This
# compose stack has no such pre-existing file to layer onto, so this one
# file plays both parts: the "PCS's own docker config" half (DATABASE_* ->
# SQLALCHEMY_DATABASE_URI, matching the real file's own pattern) AND then
# imports the actual client-facing example verbatim for the ownership half,
# so the demo proves the SAME file setup.sh ships, not a rewritten copy.
import os

DATABASE_DIALECT = os.getenv("DATABASE_DIALECT", "postgresql+psycopg2")
DATABASE_USER = os.getenv("DATABASE_USER")
DATABASE_PASSWORD = os.getenv("DATABASE_PASSWORD")
DATABASE_HOST = os.getenv("DATABASE_HOST")
DATABASE_PORT = os.getenv("DATABASE_PORT")
DATABASE_DB = os.getenv("DATABASE_DB")

SQLALCHEMY_DATABASE_URI = (
    f"{DATABASE_DIALECT}://"
    f"{DATABASE_USER}:{DATABASE_PASSWORD}@"
    f"{DATABASE_HOST}:{DATABASE_PORT}/{DATABASE_DB}"
)

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = os.getenv("REDIS_PORT", "6379")
CACHE_CONFIG = {
    "CACHE_TYPE": "RedisCache",
    "CACHE_REDIS_HOST": REDIS_HOST,
    "CACHE_REDIS_PORT": REDIS_PORT,
    "CACHE_REDIS_DB": os.getenv("REDIS_RESULTS_DB", "1"),
}
DATA_CACHE_CONFIG = CACHE_CONFIG

SECRET_KEY = os.getenv("SUPERSET_SECRET_KEY", "pcs-setup-demo-not-for-production")

# Fail LOUD, not silently: an unset/blank OWNERSHIP_FGA_STORE (empty string,
# the client-facing example's own os.environ.get(..., "") default) is "not
# set here" under settings.get's one precedence -- it falls through to
# fga_connection.py's own DEFAULT_STORE_ID, a real shared development store
# this branch was explicitly told never to reference. That fallback is
# correct and desired for a REAL deployment (it is how a from-source dev
# checkout points at the team's shared store with zero config); it is
# exactly wrong for this demo, which must only ever talk to the store
# docker/seed.sh just created. `docker compose run --rm seed` writes
# OPENFGA_STORE_ID to .seed-out/seed.env; export it (`set -a; source
# .seed-out/seed.env; set +a`) in the SAME shell invocation that then runs
# `docker compose up`/`run` -- environment does not carry across separate
# shells. setup.sh already does this correctly end to end.
if os.getenv("OWNERSHIP_ENABLED", "").strip().lower() == "true" and not os.getenv(
    "OPENFGA_STORE_ID"
):
    raise RuntimeError(
        "demo-superset-config-docker.py: OPENFGA_STORE_ID is unset. Run "
        "`docker compose --profile seed run --rm seed` first, then `source "
        ".seed-out/seed.env` in the SAME shell before starting superset/"
        "worker/init -- see setup.sh's --up flow. Refusing to start rather "
        "than silently falling through to the module's shared-store default."
    )

# --- the actual client-facing file, unmodified -----------------------------
# Executed here (not imported as a package -- it isn't one) so its trailing
# `configure(globals())` call applies against THIS module's namespace, which
# by this point already has FEATURE_FLAGS (from superset.config's import
# inside superset_config_ownership.configure()'s own fallback) and the DB/
# cache config above. This is demo plumbing only; the client does not do
# this -- they add the same two blocks directly to their own existing file.
with open("/app/pythonpath/superset_config_docker.example.py", "rb") as _fh:
    exec(compile(_fh.read(), "superset_config_docker.example.py", "exec"))  # noqa: S102

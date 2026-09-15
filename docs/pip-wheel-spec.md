# PCS-10243 — object ownership & sharing as a pip wheel

Technical spec. Deliver the backend as an installable wheel on top of the
stock Preset PCS 6.1.0.7 image, on the assumption that **no upstream
change (apache/superset or preset-pcs) will ever land**. This is the
"worst case, ship it anyway" design.

**Citation convention.** A bare path (`overlay/pythonpath/superset_ownership/cli.py:374`)
is relative to the `pcs-setup` worktree (`/Users/amaannawab/superset-local/pcs-setup`,
branch `pcs-setup`, HEAD `4e15726`). A path starting `superset/` or
`superset-frontend/` is relative to the upstream PCS tree, read via the
`pcs-617` worktree (`/Users/amaannawab/superset-local/pcs-617`), diffed
`76151beade` (upstream base) against `cf5d6c7e2c` (client-test main, the
ref the current overlay was cut from). Every code claim below was read
from one of these two trees this session; none is from memory.

---

## 1. Goal, non-goals, assumptions

**Goal.** Ship `superset_ownership` (and its config glue) as a versioned
wheel that installs into the stock PCS image's own virtualenv with **zero
bytes of any stock backend file overwritten** — not `superset/config.py`,
not `superset/security/manager.py`, not `superset/dashboards/api.py`. The
frontend cannot follow this path (React compiles into the webpack bundle
at image-build time; no post-build injection point exists — §7) and stays
an image-build overlay.

**Non-goals.**
- No upstream PR to `apache/superset`/`preset-pcs`. If one lands anyway
  (e.g. a future serialize hook), §3's wrap becomes deletable, but nothing
  here depends on that.
- No change to the OpenFGA/local authorization model, the directory/
  identity plug-in seams, or the outbox (`plugins.py`, `outbox.py`).
- No fix for Superset's `.supx` extension-system gap (no contribution
  point for dashboard tiles or list columns —
  `/Users/amaannawab/superset-local/pcs-617/qa/reviews/delivery-options.md:54-65`).

**Assumptions.**
- **PCS version pin: v6.1.0.7** (`UPSTREAM_SHA` base `76151beade`). §8.5
  has the version-bump procedure for when this changes.
- Ivanti builds from source in their own FIPS pipeline. **Known blocker,
  not solved here**: PCS's `pyproject.toml` declares
  `requires-python = ">=3.11"` (confirmed in both `pcs-617/pyproject.toml:27`
  and the fetched stock tree `.pcs-src/pyproject.toml:27`); if Ivanti's
  FIPS base is still pinned below 3.11, PCS's own pins (e.g. `numpy==2.4.6`)
  won't resolve. A base-image task for Ivanti, not something a wheel
  routes around — §11 Q1.
- The wheel installs into the **same** virtualenv PCS's Dockerfile builds
  (`/app/.venv`), not a sidecar venv. Every dependency the module imports
  is already pinned in PCS's `requirements/base.txt` (§2.3).

---

## 2. Package design

### 2.1 Names

| | Value |
|---|---|
| Distribution name | `superset-ownership` |
| Import name | `superset_ownership` (unchanged) |
| Reference plug-in package | `superset-ownership-examples`, import name `ivanti_pcs_example` — **separate wheel** (§2.5) |
| Config shim | `superset_config_ownership` — stays on **pythonpath**, shrunk (§2.6) |
| Version scheme | SemVer from `0.1.0`. `MAJOR` for a config-key rename or a change to §3's wrap contract, `MINOR` for a new hook/setting, `PATCH` for fixes — independent of the PCS version (see §8.5's compatibility check). |

### 2.2 Current state

`pcs-617/qa/reviews/acceptance-vs-elizabeth-spec.md:23` records the gap
this closes: *"no `pyproject.toml`, no `setup.py`, no `config_snippet.py`
anywhere under `superset_ownership/`"* — re-confirmed this session (no
`pyproject.toml` anywhere under `overlay/`). Today the module is a
directory dropped onto `PYTHONPATH` (58 files per `MANIFEST:26-83`, ~22k
lines of package code, ~28k lines of tests).

### 2.3 `pyproject.toml`

Build backend: **hatchling** — no compiled extensions (verified below).
`force-include` names the three non-`.py` package-data files migrations
need (§5) explicitly, rather than relying on hatchling's own default
file-selection behavior for a plain `packages = [...]` target (not
independently verified this session either way) to include them — if that
default already would, this entry is redundant but harmless; if it
wouldn't, it's load-bearing. Either way, explicit beats implicit for
files a broken build would otherwise fail on only at deploy time.

```toml
[build-system]
requires = ["hatchling>=1.25"]
build-backend = "hatchling.build"

[project]
name = "superset-ownership"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  # Already pinned in PCS's own requirements/base.txt (below). Declared
  # for documentation / a standalone dev install; the Dockerfile installs
  # with --no-deps (§6) so this list never drives a resolver pass against
  # the host's already-pinned venv.
  "flask>=2.3,<3", "flask-login>=0.6,<0.7", "flask-jwt-extended>=4.7,<5",
  "sqlalchemy>=2.0,<2.1", "alembic>=1.15,<2", "click>=8.4,<9", "requests>=2.33,<3",
]

[project.optional-dependencies]
dev = ["pytest>=8"]

[tool.hatch.build.targets.wheel]
packages = ["superset_ownership"]
exclude = ["superset_ownership/tests/**", "superset_ownership/UPDATING.md"]

[tool.hatch.build.targets.wheel.force-include]
"superset_ownership/migrations/alembic.ini" = "superset_ownership/migrations/alembic.ini"
"superset_ownership/migrations/script.py.mako" = "superset_ownership/migrations/script.py.mako"
"superset_ownership/model/ownership.fga" = "superset_ownership/model/ownership.fga"
```

**Dependencies, verified against the host.** Grepped every top-level
`import`/`from` across the 33 non-test `.py` files: stdlib plus `alembic`,
`click`, `flask`, `flask.cli.with_appcontext`,
`flask_jwt_extended.verify_jwt_in_request`, `flask_login.current_user`,
`requests`, `sqlalchemy` — nothing else. In particular **no `openfga-sdk`**
(hand-rolled `requests` client, matching
`acceptance-vs-elizabeth-spec.md:47`'s "DEVIATES (accepted)") and **no
`celery`** (`grep -c "^import celery\|^from celery"` across the package
returns nothing — the outbox drain runs inside Superset's own Celery app
via `CELERY_CONFIG.imports`, `superset_config_ownership.py:241`, not
imported by the module). All seven deps are pinned in
`pcs-617/requirements/base.txt`: `flask==2.3.3`, `flask-login==0.6.3`,
`flask-jwt-extended==4.7.1`, `sqlalchemy==2.0.52`, `alembic==1.15.2`,
`click==8.4.2`, `requests==2.33.0`. No C-extension dependency. **No
`[celery]` or `[fga]` extra** — nothing to extra-depend on (the brief
suggested `[celery]`; dropped after verifying the import).

### 2.4 Console entry points

**None added.** `superset ownership …` (`cli.py`, `cli_fga.py`,
`cli_plugin.py`) is a Flask CLI group registered at runtime —
`cli.py:374-380`'s `install(app)` calls `app.cli.add_command(ownership)`
from the ON-state `FLASK_APP_MUTATOR`. This already works identically
whether `superset_ownership` was found via `PYTHONPATH` or
`site-packages`; no `[project.scripts]` entry is needed or added.

`cli_plugin.py`'s `seed_scratch` (`cli_plugin.py:169-181`) genuinely needs
no live Superset app, but is nested under the Flask-CLI `ownership` group
(only reachable via `cli.install(app)`), so it isn't independently
invocable today. Left as-is for v0.1.0 — a small, isolated follow-on, not
a blocker here.

### 2.5 The reference plug-in package: separate wheel

Decision: **`superset-ownership-examples`, a second wheel**, not
`superset_ownership.examples.*` inside the main one.

`ivanti_pcs_example/README.md:1-9`: *"not installed and not on PYTHONPATH
by default … Nothing here is required reading to run a default
deployment."* Folding it into the production wheel means every deployment
carries eight files of "worked-example, not maintained-forever" code
whether asked for or not, and risks a hook dotted-path typo accidentally
resolving into it. A second wheel preserves the documented "opt-in only"
property, versions independently (an example edit doesn't force a
`superset-ownership` bump), and matches how it's consumed today.
`test_ivanti_pcs_example.py` (`MANIFEST:76`) stays in the main package's
test tree behind `pytest.importorskip("ivanti_pcs_example")` — one CI run,
skipped when the examples package isn't installed.

### 2.6 Config glue stays on pythonpath, shrunk to a shim

`superset_config_ownership.py` (421 lines,
`overlay/pythonpath/superset_config_ownership.py:47-421`) isn't library
code — it's meant to be the **last line of the customer's own
`superset_config_docker.py`**: `from superset_config_ownership import
configure; configure(globals())`. That import spelling is documented in
three places already (`overlay/config/superset_config_docker.example.py:131`,
`overlay/pythonpath/ivanti_pcs_example/README.md:18-19`, this doc's §4) —
changing it costs every deployment a config edit for no gain.

Decision: move the 421-line body into the wheel as
`superset_ownership/configure.py` (byte-identical logic), shrink the
pythonpath file to:

```python
# overlay/pythonpath/superset_config_ownership.py -- thin shim
from superset_ownership.configure import configure  # noqa: F401
```

This is the **only** file left on `PYTHONPATH` by default — down from
three entries today (`Dockerfile.pcs-ownership:121-123`) to one 2-line
shim. It still needs a pythonpath drop because `superset_config_docker.py`
is conventionally the customer's own file, not shipped inside a wheel.

---

## 3. Moving the three backend edits into the wheel

| Stock file | Diff today (`76151beade`→`cf5d6c7e2c`, re-verified via `git diff`) | Wheel-world destination |
|---|---|---|
| `superset/config.py` | +14 (`DEFAULT_FEATURE_FLAGS["OBJECT_OWNERSHIP"]=False`+comment, 9 lines; `EXTRA_OWNERS_RESOLVER: Callable[..., list[str]] \| None = None`, 5 lines) | Both become **runtime `app.config[...]` sets**, no file touched (§3.1) |
| `superset/security/manager.py` | +36 (`get_access_contact_names(resource)`) | **Module function inside the wheel** (§3.2), never installed onto `superset.security.manager` |
| `superset/dashboards/api.py` | +20/−1 (`_serialize_dashboard_chart` gains `has_access`/`owners`) | **Runtime method wrap**, installed from `FLASK_APP_MUTATOR` (§3.3) |

### 3.1 `config.py`'s two additions

Neither needs pre-declaration:

- `FEATURE_FLAGS["OBJECT_OWNERSHIP"]=False` is a dict default.
  `superset_config_ownership.py:132`'s derivation
  (`ns["FEATURE_FLAGS"] = {**ns["FEATURE_FLAGS"], "OBJECT_OWNERSHIP": _ownership_enabled}`)
  overwrites it unconditionally whenever `configure()` runs — which it
  does even with the switch off (`:56-97`, not gated). Same runtime value
  either way; stock `config.py` untouched.
- `EXTRA_OWNERS_RESOLVER: … = None` is a type-annotated default for
  documentation, matching the sibling `EXTRA_EDITORS_RESOLVER` pattern
  already in stock `config.py:3160-3163`. `app.config` is a plain dict —
  `current_app.config.get("EXTRA_OWNERS_RESOLVER")` returns `None`
  whether or not `config.py` pre-declared the key. The ON-mutator already
  sets it (`superset_config_ownership.py:340`). No behavior change.

### 3.2 `security/manager.py`'s `get_access_contact_names`

Moves verbatim into `superset_ownership/dashboard_patch.py:get_access_contact_names(resource)`
— same fallback chain (resolver → `resource.owners` → `resource.created_by`,
`pcs-617 superset/security/manager.py:173-208`). **Not** installed onto
`superset.security.manager` — narrower blast radius, and nothing else in
stock Superset calls it. Only caller: the wrap in §3.3.

### 3.3 `dashboards/api.py`'s `_serialize_dashboard_chart` — the runtime wrap

**Where installed.** From the existing ON-mutator, alongside
`guard.install()`/`cli.install(app)` (`superset_config_ownership.py:390-398`),
gated the same way — only when `_ownership_enabled` is true (the early
return at `:210-211` already covers this; OFF stays stock).

**Why the composition is safe.** The *unmodified* upstream method already
strips `form_data` on denial:

```python
# stock, unmodified — superset/dashboards/api.py:761-766
def _serialize_dashboard_chart(self, chart: Any) -> dict[str, Any]:
    serialized = self.chart_entity_response_schema.dump(chart)
    if not security_manager.can_access_chart(chart):
        serialized.pop("form_data", None)
    return serialized
```

Note what stock code itself imports to get that `security_manager`:
`superset/dashboards/api.py:141`, `from superset.extensions import
event_logger, security_manager` — the module-level `LocalProxy`
(`superset/extensions/__init__.py:204`:
`security_manager: SupersetSecurityManager = LocalProxy(lambda:
appbuilder.sm)`) wrapping the live Flask-AppBuilder instance.
`can_access_chart` is **not** a free function in `superset/security/manager.py`
— it's an instance method on `SupersetSecurityManager`
(`superset/security/manager.py:2376`, 4-space indented under the class
def at `:1737`; confirmed no module-level `security_manager = ...`
singleton exists in that file — `grep -n "^security_manager\s*="
superset/security/manager.py` has no match). The wrap must import the
same proxy stock code does, not the module.

PCS-10243's whole diff is two lines **inside** the `if`. So the wrap
calls the original first, then layers the marker on using the same public
predicate (`can_access_chart`) — not by inferring "was it stripped" from
the dict's shape, which would couple to an implementation detail instead
of the contract:

```python
# superset_ownership/dashboard_patch.py (new module)
import hashlib, inspect, logging, sys
logger = logging.getLogger(__name__)

# Pinned against superset/dashboards/api.py:761 at UPSTREAM_SHA 76151beade.
# Update both on every PCS version bump (§8.5) -- never widen to "close enough".
#
# Parameter NAMES and KINDS, not a stringified inspect.signature(). A
# stringified signature bakes in annotation formatting, which is cosmetic
# and version-fragile: dashboards/api.py carries no `from __future__
# import annotations` (confirmed absent -- `grep -n "from __future__"
# superset/dashboards/api.py`, no match) and imports `Any` plainly
# (`superset/dashboards/api.py:23`), so the REAL
# str(inspect.signature(DashboardRestApi._serialize_dashboard_chart)) is
# `"(self, chart: Any) -> dict[str, typing.Any]"` -- reproduced directly
# against an equivalent method this session, not assumed -- which is a
# different string from what a `from __future__ import annotations`
# module would produce for the identical code. What actually matters for
# `_original(self, chart)` to keep being a valid call is the parameter
# NAMES and ORDER and KINDS, which annotation style never changes; that
# is what this check compares instead.
_EXPECTED_PARAMS = (
    ("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    ("chart", inspect.Parameter.POSITIONAL_OR_KEYWORD),
)
_EXPECTED_SOURCE_SHA256 = "<sha256 of inspect.getsource(original), pinned at packaging time>"

class DashboardPatchError(RuntimeError):
    """Stock _serialize_dashboard_chart no longer matches this wheel's build,
    or its source could not be read at all (see _assert_wrappable)."""

def _assert_wrappable(method) -> None:
    actual = tuple(
        (name, p.kind) for name, p in inspect.signature(method).parameters.items()
    )
    if actual != _EXPECTED_PARAMS:
        raise DashboardPatchError(
            f"parameter shape changed: expected {_EXPECTED_PARAMS!r}, got {actual!r}")
    try:
        source = inspect.getsource(method)
    except OSError as exc:
        # A base image shipping bytecode-only (no .py sources) for
        # dashboards/api.py -- this repo's own Dockerfile never strips
        # sources (only `compileall` for caching), but Ivanti's FIPS
        # pipeline must preserve them too, or this assertion can never run.
        # Re-raised as the same error type so every caller sees one
        # failure class, not a bare, unexplained OSError.
        raise DashboardPatchError(
            f"could not read _serialize_dashboard_chart's source ({exc}); "
            "the PCS base image must ship .py sources for "
            "superset/dashboards/api.py, not bytecode-only"
        ) from exc
    h = hashlib.sha256(source.encode()).hexdigest()
    if h != _EXPECTED_SOURCE_SHA256:
        raise DashboardPatchError(
            f"source changed upstream (hash {h} != {_EXPECTED_SOURCE_SHA256})")

def _degrade_on_failure() -> bool:
    """True for the diagnostic/recovery commands that must be able to
    report or repair a problem instead of crashing before they even start
    -- the exact set `superset_ownership.plugins.maintenance_invocation()`
    already carves out for `plugins.load()` one call above this one
    (`superset_config_ownership.py:366`,
    `plugins.load(app, strict=not plugins.maintenance_invocation())`),
    widened by exactly one case: `ownership check` itself, so the field
    this wrap feeds (`dashboard_chart_patch_installed`) stays reachable
    even when the assertion that would set it True instead failed.
    Everything else -- the web app, Celery
    (`superset/tasks/celery_app.py:32`'s own `create_app()` call), the
    plain `superset` CLI's `FlaskGroup` (`superset/cli/main.py`),
    `ownership backfill-tenants`/`enable`/`disable`/`reconcile`/
    `purge-tenant`/`outbox *`, a plain `superset db upgrade`, and the
    entrypoint's bare backfill script (`docker/entrypoint-ownership.sh:106-112`,
    `python -c "from superset.app import create_app; ..."`, whose
    `sys.argv` never contains "ownership" at all) -- stays strict,
    matching `maintenance_invocation()`'s own docstring on exactly which
    commands are allowed to run degraded and which must fail loudly
    because they read authoritative state or write data."""
    from superset_ownership import plugins
    if plugins.maintenance_invocation():
        return True
    args = sys.argv
    try:
        i = args.index("ownership")
    except ValueError:
        return False
    rest = args[i + 1:]
    return bool(rest) and rest[0] == "check"

def install() -> None:
    from superset.dashboards.api import DashboardRestApi
    from superset.extensions import security_manager

    original = DashboardRestApi._serialize_dashboard_chart
    if getattr(original, "_superset_ownership_patched", False):
        return  # idempotent: a second FLASK_APP_MUTATOR run must not double-wrap

    try:
        _assert_wrappable(original)
    except DashboardPatchError as exc:
        if _degrade_on_failure():
            logger.error(
                "superset_ownership: dashboard-chart patch NOT installed (%s); "
                "this process is a diagnostic/recovery command and is allowed "
                "to continue without it -- `superset ownership check` will "
                "report dashboard_chart_patch_installed: false. Every other "
                "process (web, Celery, a plain `superset db upgrade`, the "
                "backfill step) still refuses to start on this failure.",
                exc,
            )
            return
        raise

    def patched(self, chart, *, _original=original, _sm=security_manager):
        serialized = _original(self, chart)
        if not _sm.can_access_chart(chart):
            serialized["has_access"] = False
            serialized["owners"] = get_access_contact_names(chart)
        return serialized

    patched._superset_ownership_patched = True
    DashboardRestApi._serialize_dashboard_chart = patched
    logger.info("superset_ownership: dashboard-chart access-marker patch installed")
```

**Frontend contract preserved.** `slice_name`/`id` come from the
unmodified `.dump()` inside the original, untouched by the wrap — the
tile keeps its title (cross-user referencing per
`eval-elizabeth-architecture.md:76-79`). `form_data` is withheld by the
*original*. `has_access`/`owners` match today's replace-file shapes
exactly, so `overlay/frontend/src/dashboard/components/gridComponents/Chart/Chart.tsx`
and `ChartSecurityAccessErrorMessage.tsx` need no change.

**Failure mode: refuse to start for web/worker; degrade for
diagnostics.** This wrap's whole job is to stop a private chart's data
leaking through a dashboard tile — a blanket degrade-and-warn mode would
silently reopen exactly that leak on every render, with no visible
symptom. So for the web app and Celery, `DashboardPatchError` is left
uncaught and propagates out of `FLASK_APP_MUTATOR`, crashing
`create_app()`, the same as before. But that mutator runs on **every**
`create_app()` call — the web/gunicorn process, Celery
(`superset/tasks/celery_app.py:32`), every `superset ...` CLI invocation
(`superset/cli/main.py`'s `FlaskGroup`), and the entrypoint's own backfill
step — so an unconditional raise would also prevent `superset ownership
check`, the one command an operator needs to *diagnose* the failure, from
ever running. `_degrade_on_failure()` above closes that gap the same way
`plugins.load()` already does one line earlier in the same mutator
(`superset_config_ownership.py:366`): a mismatch during a
maintenance/recovery command (`ownership db upgrade/downgrade/current/
stamp`, `ownership status`, `ownership fga *`, `ownership plugin
verify/seed-scratch/describe`, and `ownership check` itself) logs at
ERROR and lets the process boot with the wrap absent, so `check` can
still report `dashboard_chart_patch_installed: false` and exit 1. Every
other invocation — including `ownership backfill-tenants` and the raw
backfill script, both of which write data — stays strict, matching how
`plugins.load()` already treats them today (`maintenance_invocation()`'s
own docstring explicitly excludes `check`/`backfill-tenants`/`enable`/
`disable`/`reconcile`/`purge-tenant`/`outbox *`/a plain `superset db
upgrade` from the degraded set, for the same "these mutate or must be
authoritative" reason).

This also mirrors the project's own existing philosophy elsewhere:
`Dockerfile.pcs-ownership:79-108`'s drift guard hard-fails the *build* on
a bare sha256 mismatch for files carrying no security logic at all; the
one process type that actually serves chart data gets at least the same
treatment, at boot (build time can't see it — see below) — while the
tooling built specifically to diagnose or repair a broken install keeps
working, exactly as it already does for a broken plug-in seam.

**Build-time check too.** One `RUN` step in the `final` stage (§6.1),
importing the *installed* wheel against the *installed* stock file:

```dockerfile
RUN /app/.venv/bin/python -c "\
from superset.dashboards.api import DashboardRestApi; \
from superset_ownership.dashboard_patch import _assert_wrappable; \
_assert_wrappable(DashboardRestApi._serialize_dashboard_chart)"
```

Catches drift at `docker build` time for the normal path (this check has
no invocation-kind concept — a build is a build — so it always hard-fails
on mismatch, which is correct: better to stop the build than ship an
image whose only safety net is the boot-time degrade path above). The
boot-time assertion in `install()` is the backstop for a from-source
build that skips this line — the situation §6.4 describes for Ivanti — or
`configure()` running twice in one process.

**`superset ownership check` addition.** One new field on
`check_consistency()`'s report (`cli.py:151-179`):
`dashboard_chart_patch_installed` — `true` iff
`DashboardRestApi._serialize_dashboard_chart` currently carries the
`_superset_ownership_patched` marker. With the degrade path above, this
is the field's real job, not a rare-regression backstop: it's how
`check` reports a genuine assertion failure instead of the process simply
refusing to answer at all. `check` already exits 1 on any `false`
(`cli.py:178-179`).

---

## 4. Config surface after packaging

**Minimum viable block** (unchanged — the point of §2.6 is that nothing
here changes):

```python
OWNERSHIP_ENABLED = True
OWNERSHIP_AUTHORIZER = "local"  # or "openfga", or a dotted class path
from superset_config_ownership import configure  # noqa: E402
configure(globals())
```

Full reference: `overlay/config/superset_config_docker.example.py:44-125`,
unchanged by this spec.

**`configure(globals())` interaction.** No change from a config author's
view. `superset_config_ownership.configure` is now a 1-line re-export
(§2.6) of the same function body — same precedence (`OWNERSHIP_ENABLED`:
config layer wins, then `os.environ`, then `"false"` —
`superset_config_ownership.py:84-97`), same `FEATURE_FLAGS` merge order
(`:108-132`), same two mutators.

**Feature-flag derivation.** Unchanged: `FEATURE_FLAGS["OBJECT_OWNERSHIP"]`
is derived from `OWNERSHIP_ENABLED`, never set independently
(`:126-132`); `flags.py`'s `warn_if_flags_disagree` (`flags.py:256-355`)
still runs from both mutators and still warns if a later layer or a
`GET_FEATURE_FLAGS_FUNC`/`IS_FEATURE_ENABLED_FUNC` hook moves it. Lives
entirely inside the wheel already; no change for packaging.

**Function hooks.** Unchanged mechanism: sixteen `OWNERSHIP_*` dotted-path
settings (`overlay/config/superset_config_docker.example.py:89-125`),
resolved once by `plugin_hooks.py`'s `resolve()` (`plugin_hooks.py:248`)
through the same precedence `settings.get()` uses (`settings.py:250-300`:
explicit layer → `os.environ` → typed default). **hook > seam > default**
is enforced by resolution order, not packaging: a configured function hook
is read before the class-level seam, which itself falls back to the
module's built-in default. Nothing in §2/§3 touches this file.

---

## 5. Migrations

**Where the chain lives once installed.** `migrate.py:70` computes
`MIGRATIONS_DIR` as `os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")`
— relative to the module's own `__file__`, not a `PYTHONPATH`-root
assumption. This already resolves correctly for a pip-installed package
with **zero code change**: once installed at (e.g.)
`/app/.venv/lib/python3.11/site-packages/superset_ownership/`,
`MIGRATIONS_DIR` resolves to the sibling `migrations/` — which is exactly
why §2.3's `force-include` entries for `alembic.ini`/`script.py.mako`/the
DSL file matter (`versions/*.py` are `.py`, auto-included).
`alembic_version_ownership` stays the version table
(`migrations/alembic.ini`, `migrations/env.py`'s `VERSION_TABLE`), fully
independent of Superset's own `alembic_version` (`migrate.py:17-26`). No
change from packaging.

**Ordering with `superset db upgrade`.** Unchanged:
`docker/entrypoint-ownership.sh:78-133`'s `ownership_migrate_and_check()`
already runs `superset ownership db upgrade`, then backfill, then
`backfill-tenants`, then `superset ownership check`, right after the stock
`superset db upgrade`. Lives in the entrypoint script, untouched by the
wheel migration.

**Backfill / idempotency / rollback.** Unchanged and already idempotent —
`entrypoint-ownership.sh:87-99` (backfill only touches unowned rows, safe
on every container start), `migrate.py:271-282`'s `stamp()` (refuses to
record 0003 without its constraints present). Rollback:
`superset ownership db downgrade <rev>` (`cli.py:278-287`), `'base'` drops
the tables. None of this ever referenced the delivery mechanism (pip vs.
pythonpath) — it's all `__file__`- or Superset-config-relative.

---

## 6. Image/build integration

### 6.1 New `Dockerfile.pcs-ownership` shape

A new `wheel-builder` stage, and a smaller `final` stage:

```dockerfile
######################################################################
# wheel-builder -- self-contained; no external artifact registry needed
# by default. Ivanti's own FIPS pipeline may build+sign the wheel in a
# separate audited step and COPY it in instead -- swap this stage for
# `COPY <prebuilt>/*.whl /wheel/` and nothing else in this file changes.
######################################################################
FROM python:3.11-slim AS wheel-builder
# `pip` is fine here -- this is a plain, standard python:3.11-slim image,
# not the PCS venv (which ships no pip module at all; see the final stage
# below and §8's note).
RUN pip install --no-cache-dir build
WORKDIR /src
COPY overlay/pythonpath/superset_ownership/pyproject.toml ./pyproject.toml
COPY overlay/pythonpath/superset_ownership/superset_ownership ./superset_ownership
RUN python -m build --wheel --outdir /wheel

######################################################################
# final -- frontend stage unchanged (§7); backend shrinks to a wheel
# install. No overlay/backend/ COPY -- that directory no longer exists.
######################################################################
FROM ${PCS_IMAGE} AS final
USER root

# drift guard: FRONTEND ONLY now (backend bucket retired, §6.3)
COPY UPSTREAM_SHA /tmp/UPSTREAM_SHA
# ... existing frontend-only loop from Dockerfile.pcs-ownership:88-107,
# with the `superset/config.py|dashboards/api.py|security/manager.py` case
# arm removed -- nothing left to guard for those three.

COPY --from=frontend /app/superset/static/assets /app/superset/static/assets

# backend: wheel install, --no-deps (host venv is already fully pinned;
# a resolving install risks upgrading/downgrading a Superset-pinned dep)
COPY --from=wheel-builder /wheel/*.whl /tmp/
RUN uv pip install --python /app/.venv/bin/python --no-deps /tmp/*.whl && rm -f /tmp/*.whl

# build-time boot-assertion check (§3.3)
RUN /app/.venv/bin/python -c "\
from superset.dashboards.api import DashboardRestApi; \
from superset_ownership.dashboard_patch import _assert_wrappable; \
_assert_wrappable(DashboardRestApi._serialize_dashboard_chart)"

# config shim (only remaining pythonpath entry by default, §2.6)
COPY overlay/pythonpath/superset_config_ownership.py /app/pythonpath/superset_config_ownership.py
# ivanti_pcs_example NOT installed by default (§2.5) -- uncomment for a
# scratch/dev image: install superset-ownership-examples the same way.

RUN uv pip install --python /app/.venv/bin/python psycopg2-binary==2.9.12  # unchanged, Dockerfile.pcs-ownership:129-139

COPY --chmod=755 docker/entrypoint-ownership.sh /app/docker/entrypoint-ownership.sh
USER superset
WORKDIR /app
ENTRYPOINT ["/app/docker/entrypoint-ownership.sh"]
CMD ["app"]
```

### 6.2 What is removed

| Removed | Why |
|---|---|
| `overlay/backend/` (all of `superset/config.py`, `dashboards/api.py`, `security/manager.py`) | §3 moved all three into the wheel or a runtime wrap |
| Dockerfile's backend-file drift guard (`:88-108`'s 3-path `case` arm) | Nothing left to copy; §3.3's assertion is the narrower, behavior-aware replacement |
| 2 of 3 `overlay/pythonpath/*` `COPY` lines (`:121-123`) | `superset_ownership` is now a wheel install; `ivanti_pcs_example` moved to its own optional wheel |

### 6.3 What the scripts become

`scripts/sync-from-main.sh`'s `BACKEND` bucket (`sync-from-main.sh:82-83`)
becomes **advisory only**: still run it to detect whether client-test main
touched those 3 files, but its output no longer feeds a `COPY` — it
triggers re-deriving §3's destinations by hand and bumping the wheel.
`MANIFEST`'s `# --- backend core (3 files) ---` section (`:9-11`) is
deleted. `UPSTREAM_SHA`'s three backend entries (`:11-13`) are kept as
input to "did upstream move" detection but read by nothing at `COPY` time
— only by §3.3's `_assert_wrappable`, a strictly narrower and more precise
check. `apply-overlay.sh`'s drift guard (`:41-90`) drops its
`superset/config.py|dashboards/api.py|security/manager.py` case arm
(`:53`) — the from-source path no longer overwrites those files either.

Structural implication: **client-test main no longer needs to carry a
fork of these three files** — they rebase cleanly onto every future
upstream/PCS bump, since nothing in the pipeline depends on their content
differing from stock.

### 6.4 Ivanti's own pipeline

Replaces `README.md:218-249`'s three-`COPY`-line recipe with:

```dockerfile
# before npm run build -- unchanged:
COPY overlay/frontend/ /app/superset-frontend/

# final/runtime stage. `pip` is not present in the stock venv (it ships
# no pip module at all -- package management there goes through `uv`,
# already used by this image's own Postgres-driver install line); use the
# same tool:
RUN uv pip install --python /app/.venv/bin/python --no-deps superset_ownership-0.1.0-py3-none-any.whl
COPY overlay/pythonpath/superset_config_ownership.py /app/pythonpath/superset_config_ownership.py

# fail the build fast on drift, same check §6.1's own final stage runs --
# not deliberately omitted here, transcribe it:
RUN /app/.venv/bin/python -c "\
from superset.dashboards.api import DashboardRestApi; \
from superset_ownership.dashboard_patch import _assert_wrappable; \
_assert_wrappable(DashboardRestApi._serialize_dashboard_chart)"

# entrypoint: unchanged -- `superset ownership db upgrade` + `check`
# after your own `superset db upgrade`/init.
```

Down from "3 `COPY` lines + a directory-shadowing trap to avoid" to "1
`uv pip install` + 1 `COPY` of a 1-line shim + 1 build-time check" for the
backend half. The shadowing trap (§9) still applies to whatever else
Ivanti keeps on `PYTHONPATH` — it no longer applies to
`superset_ownership` itself. **Note the tool**: `pip` itself is absent
from the stock venv (§8), so every install line in this section uses `uv`
the same way the existing Dockerfile already does for `psycopg2-binary`
(`Dockerfile.pcs-ownership:139`) — a plain `pip install` here fails with
`ModuleNotFoundError: No module named pip`.

---

## 7. Frontend overlay

### 7.1 The tiers, precisely

| Tier | Mechanism | Count today | Risk |
|---|---|---|---|
| New files | file copy | 10 (`ChartSecurityAccessErrorMessage.tsx` + 9 under `src/features/ownership/`: `OwnerCell.tsx`, `SharingDrawer.tsx`+test, `SubjectPickerPanel.tsx`+test, `VisibilityTag.tsx`, `api.ts`+test, `types.ts` — `MANIFEST:15,25-33`) | none |
| Stub override | overwrite an intentionally-empty upstream stub | 0 today, §7.2 proposes 1 | none |
| Core-file replace | whole-file overwrite, sha256 drift guard | 10 (`MANIFEST:14-23`) | real — the entire risk surface |

### 7.2 One concrete reduction, and one considered and rejected

**`ActionButton/index.tsx` stays a replace file — considered eliminating
it, decided against it.** The diff adds a real HTML `disabled` attribute
and a hover-preserving `<span>` wrapper so a disabled button still shows
its tooltip. Verified this session: the overlay's current
`ActionButton/index.tsx` is byte-identical to client-test main's patched
version (`diff overlay/frontend/packages/superset-ui-core/src/components/ActionButton/index.tsx
pcs-617/superset-frontend/packages/.../ActionButton/index.tsx` — no
output), and that fix is **live today for every consumer** of the shared
component, not just the new Share action — 21 other files import
`ActionButton` (`ChartList/index.tsx`, `DashboardList/index.tsx`,
`ListView/ActionsBar.tsx`, `FilterBar/index.tsx`, `SqlEditor` and
`SaveQuery` components, etc., per a grep across `superset-frontend/src`).
An earlier draft of this spec proposed wrapping the *unmodified* upstream
component locally (a new `ShareActionButton.tsx`, used only by the new
Share button) to drop this file from the replace set. That would silently
**revert** the `disabled`-attribute fix for all 21 existing consumers —
buttons that are, today, genuinely inert when disabled would go back to
being only `aria-disabled` (screen-reader-inert but still clickable
through anything that doesn't route through the aria attribute, which is
exactly the defect the original patch's own comment describes). That is
not a free reduction; it is a real, silent accessibility regression for
everyone except the one button that would keep the fix. **Decision: keep
`ActionButton/index.tsx` in the replace set.** If this fix is worth
having application-wide (it is — it is a small, generic accessibility
correction, arguably a better upstream-PR candidate than any of the other
nine replace-set files, unlike the ownership-specific ones), the right
path is a genuine upstream contribution, not a packaging-time exclusion
that quietly narrows its effect. That is a follow-on outside this spec's
scope, not something to fold into a "reduce the replace count" line item.

**`setupErrorMessages.ts` — use the stub PCS already ships empty.**
Confirmed this session: `pcs-617 superset-frontend/src/setup/setupErrorMessages.ts:34`
imports `setupErrorMessagesExtra`, `:187` calls it unconditionally. The
stub (`setupErrorMessagesExtra.ts`) ships as an empty function — an
intentional override point (`delivery-options.md:69-83`). Today's overlay
patches the *core* file instead (5-line diff: 1 import, 1
`registerValue` call). **Decision: move those 5 lines into
`overlay/frontend/src/setup/setupErrorMessagesExtra.ts`** and drop
`setupErrorMessages.ts` from the replace set — conflict-free the same way
`delivery-options.md:87` describes, since upstream's content there is
permanently empty. Removes 1 of 10 replace entries (→9); this is the only
reduction this spec makes.

### 7.3 What's left, and why

| File | Why it must stay |
|---|---|
| `ActionButton/index.tsx` | Real HTML `disabled` + tooltip-preserving wrapper, live for 21 consumers today (§7.2) — kept deliberately, not a candidate for local wrapping |
| `featureFlags.ts` | 1-line `FeatureFlag` enum addition — no override point for a TS enum member; smallest, most upstreamable edit remaining |
| `ErrorMessage/index.tsx` | 1-line registration wiring |
| `dashboard/actions/hydrate.ts` | 41+/1− — carries ownership data through Redux hydration |
| `Chart.tsx` | 86+/40− — tile-level placeholder for `has_access: false` |
| `ChartList/index.tsx` + test | Owner column, drawer wiring, Share action |
| `DashboardList/index.tsx` + test | Same, for dashboards |

Seven table rows, 9 files (down from 10 files across 10 rows — `ChartList`
and `DashboardList` each count as one row but two files, their
`.test.tsx` companion included), all touching logic upstream has no
extension point for except the one deliberately-kept `ActionButton`
case above — matches `delivery-options.md:135-140`'s own note that the
two list views are the bulk of what remains, removable only via an
upstream list-column contribution point, out of scope under this spec's
"no upstream changes ever land" assumption.

### 7.4 Mechanism: whole-file replace stays; `git apply` patches rejected

`delivery-options.md:109` (written before this project's own sync tooling
existed) tentatively proposed `git apply --check` hunks for this tier.
**Recommendation: keep whole-file replace** — but not for the reason an
earlier draft of this spec gave. That draft claimed
`scripts/sync-from-main.sh` "generates overlay files from a real git
branch already rebased/merged against upstream via git's own three-way
merge." Checked against the actual history: `git log --oneline --merges
76151beade..cf5d6c7e2c` returns **zero** merge commits — `client-test/main`
is 23 linear feature commits built directly on `76151beade`, which has
never moved since this overlay was cut (§1's own pin). There has been no
three-way merge to point to, because upstream has never needed
re-integrating into this branch yet. And `sync-from-main.sh` itself
doesn't merge anything regardless — it materializes each frontend file
with a plain snapshot, `git show "$REMOTE/$REF:$path" > overlay/frontend/...`
(`scripts/sync-from-main.sh`'s `for entry in "${FRONTEND[@]}"` loop). That
claim is retracted.

The actual argument for whole-file replace is narrower: the sha256 drift
guard (`Dockerfile.pcs-ownership:79-108`, `apply-overlay.sh:41-90`) plus a
real diff review at PR time already catches "did someone forget to
re-sync" with a hard build failure, which is the guard's whole job, and a
hunk-patch mechanism would only add fragility (failing on unrelated
nearby reformatting a whole-file replace doesn't care about) without
buying anything that check doesn't already provide. What this argument
does **not** yet have is evidence for how conflict resolution behaves the
first time it's actually exercised: `client-test/main` has never had to
catch up to a moved upstream base. **Recommendation stands (keep
whole-file replace), but treat it as unverified against a real version
bump until §8.5's procedure is run for the first time** — if that first
bump surfaces a case a plain snapshot handles badly, revisit then, with
real evidence instead of an assumed merge history.

---

## 8. Verification plan

**Unit tests run from source, not the installed wheel.** The test tree
(~28k lines, `MANIFEST:35-58`) is excluded from the wheel (§2.3's
`exclude`) and never lands in `site-packages`. CI runs `pytest
overlay/pythonpath/superset_ownership/tests` from a `pcs-package`
checkout, same as today — packaging changes *where the runtime artifact
ships*, not where tests run. Most are pure unit tests, no Flask/Superset
(`tests/conftest.py:17-21`); the authorization-model suite needs Superset
and runs in the existing dev container.

New coverage this spec requires: `test_dashboard_patch.py` — first,
`_assert_wrappable(DashboardRestApi._serialize_dashboard_chart)` against
the **real, imported stock method** (not a hand-built reproduction),
asserting it passes cleanly — catches a wrong pinned constant immediately
rather than at review time; idempotency (`install()` called twice patches
once); `_assert_wrappable` raising on a mutated parameter shape (a
renamed or reordered/added parameter), on a mutated source, and on a
source read that raises `OSError`; `_degrade_on_failure()` returning `True` for `ownership check`
and every `plugins.maintenance_invocation()` case and `False` for a bare
`superset db upgrade`/web/worker `sys.argv` shape; and (against real,
*unmodified* stock code, with the real `from superset.extensions import
security_manager` proxy, not a mock of it) a private chart on an
accessible dashboard returning `has_access: false` + `owners` + no
`form_data`.

**`superset ownership check`** gains `dashboard_chart_patch_installed`
(§3.3); exit-code semantics unchanged (`cli.py:151-179`).

**`scripts/verify-image.sh` additions**, after the existing
import/CLI/bundle checks (`:24-74`):

**On tooling: the stock venv has no `pip`.** `/app/.venv/bin/python -m
pip --version` against the actual stock image raises `ModuleNotFoundError:
No module named pip` — the venv's `PATH` puts `/app/.venv/bin` first, and
package management in this image goes through `uv` exclusively (already
the tool the existing Dockerfile uses for `psycopg2-binary`,
`Dockerfile.pcs-ownership:139`, and for the wheel install in §6.1/§6.4).
Every check below avoids `pip` for that reason — either `uv pip`, or pure
`importlib.metadata` (stdlib, needs neither `pip` nor `uv` on `PATH`).

```bash
check "superset_ownership installed as a wheel (not a loose pythonpath dir)" \
  docker run --rm "$IMAGE" python -c \
  "import importlib.metadata as m; p = str(m.distribution('superset-ownership').locate_file('')); \
   assert p.startswith('/app/.venv'), p; print(p)"
check "no stray pythonpath copy shadowing the wheel" \
  docker run --rm "$IMAGE" sh -c '[ ! -d /app/pythonpath/superset_ownership ]'
check "dashboard-chart patch installed" \
  docker run --rm -e SUPERSET_SECRET_KEY="verify-$(date +%s)" \
  -e SUPERSET_CONFIG_PATH=/app/pythonpath/superset_config_docker.py \
  -v "$VERIFY_CONFIG_DIR/superset_config_docker.py:/app/pythonpath/superset_config_docker.py:ro" \
  "$IMAGE" superset ownership check
```

(Reuses the minimal `OWNERSHIP_ENABLED=True, OWNERSHIP_AUTHORIZER=local`
config `verify-image.sh:41-46` already builds.) **No separate
whole-class-hash check.** An earlier draft of this section also hashed
`inspect.getsource(DashboardRestApi)` — the *entire* class, not just the
one method the wrap depends on. Confirmed this session: `DashboardRestApi`
is the only top-level class in `superset/dashboards/api.py`
(`grep -n "^class " superset/dashboards/api.py` → one hit, `:304`), and
the file is 3,047 lines long, so that hash covered roughly
`:304-3047` — ~2,743 lines of dozens of unrelated REST endpoints. Any
unrelated change anywhere in that class (a new endpoint, a docstring fix,
a lint reformat) would fail this check even when
`_serialize_dashboard_chart` and the wrap's own contract are both
completely intact — a worse failure mode for an acceptance gate than
staying silent, and it was never reconciled with §8.5's own bump
procedure, which only mentions refreshing `_assert_wrappable`'s two
constants. Dropped entirely; the narrow, single-method check already
lives in `dashboard_patch._assert_wrappable` (run at both build time,
§6.1/§6.4's `RUN` step, and boot time) and its outcome is exactly what the
`dashboard-chart patch installed` check above already confirms — a second,
broader, unsynchronized mechanism would duplicate that signal while being
strictly easier to break for unrelated reasons.

**Acceptance gate: `scratch/seed-multitenant.sh`.** The existing
end-to-end proof (real tenant data, real OpenFGA store, real backfill;
output recorded in `scratch/scratch-run.md`) stays the gate — run it once
against an image built from the wheel-based Dockerfile before merging the
PR that removes `overlay/backend/`. Any divergence from the recorded
MARK/row-count lines signals the migration broke something the current
build has already proven.

### 8.5 PCS version bump procedure

1. `scripts/fetch-pcs.sh` against the new tag.
2. `scripts/sync-from-main.sh` — regenerates the **frontend-only** overlay
   (backend bucket is advisory, §6.3).
3. `scripts/apply-overlay.sh --check-only` against the new `.pcs-src`.
4. Diff the new `_serialize_dashboard_chart` against `_EXPECTED_PARAMS`/
   `_EXPECTED_SOURCE_SHA256`. If changed, update both — ideally by running
   `_assert_wrappable`'s own parameter-derivation logic against the newly
   pinned source and taking its output verbatim, rather than hand-typing
   either constant (§12's revision log has the reasoning for why the
   hand-typed form is exactly what went wrong once already) — re-verify
   §3.3's composition still holds (the method may have grown branches an
   unconditional wrap would clobber), bump `MINOR`.
5. Diff `config.py`'s `DEFAULT_FEATURE_FLAGS`/`EXTRA_*` and
   `security/manager.py` for anything breaking §3.1/§3.2's
   no-pre-declaration reasoning.
6. Rebuild the wheel and image, run `scripts/verify-image.sh` (§8, incl.
   the Dockerfile's own build-time assertion).
7. Re-run `scratch/seed-multitenant.sh` end-to-end.
8. Update `PCS_IMAGE`/`UPSTREAM_SHA`, tag the new wheel version, update
   §1's pin if the PCS minor version changed.

---

## 9. Risks and mitigations, ranked

| # | Risk | Mitigation |
|---|---|---|
| 1 | **Method-wrap fragility across PCS versions** — `_serialize_dashboard_chart` renamed, split, or its own check moved. | §3.3's signature **and** source-hash check, at both build and boot time, fails loudly for web/worker; degrades (logs, skips the wrap) for `ownership check`/maintenance commands via `_degrade_on_failure()` so the failure is diagnosable rather than un-bootable. §8.5 makes re-verifying the two pinned constants an explicit, checked step of every version bump. |
| 2 | **Import-order/circular-import risk** — the wheel reaches `superset.dashboards.api`, `superset.extensions.security_manager`, `superset.extensions.feature_flag_manager`, and (inside `_degrade_on_failure`) `superset_ownership.plugins` only from inside `FLASK_APP_MUTATOR` (after Superset's own app/extension init), exactly as `plugins.load`/`create_tables`/`csrf.exempt` already do. Not new, but a future contributor could import one of these at `dashboard_patch.py` module scope instead of lazily — or could reach for `from superset.security import manager as security_manager` (the *module*, no `can_access_chart` on it) instead of the `superset.extensions` proxy stock code itself uses (`superset/dashboards/api.py:141`), an easy mistake this spec's own first draft made. | Keep every `superset.*` import inside function bodies, matching the rest of the package's own stated rule (`cli_plugin.py:45-48`: "business logic is imported inside each command body, never at module scope"), and keep §3.3's real-stock integration test (§8) in CI so this specific import mistake fails a test rather than shipping. Reviewer checklist item for this file. |
| 3 | **Celery worker/beat needing the wheel too** — the outbox drain runs in a separate process from the web workers. | Low risk in this repo's own demo: `docker-compose.pcs-setup.yml:138-139`'s `worker` service uses the **same** `pcs-ownership:${TAG}` image as `superset`/`init`. Real risk for Ivanti's own pipeline if web/worker images are built from diverging Dockerfiles — silent failure mode is queued outbox rows that never drain, not a crash. `outbox status --verbose`'s backlog/`store_reachable` reporting (`cli.py:319-337`) already surfaces this; document that **every** process type must come from an image that ran the wheel-install step. |
| 4 | **`pythonpath` shadowing trap**, recurring even after §2.6 — the one remaining shim (and `ivanti_pcs_example`, if ever put on pythonpath against §2.5's recommendation) must still be copied as individual entries, never a directory-level mount (`Dockerfile.pcs-ownership:116-120`). | §6.1 keeps individual `COPY` lines, smaller surface than today. Called out explicitly in the Ivanti recipe (§6.4) since a well-meaning "just `COPY overlay/pythonpath/`" would reintroduce the exact bug this project already fixed once. |
| 5 | **Wheel vs. pythonpath double-install** — a stale `COPY overlay/pythonpath/superset_ownership …` line surviving alongside the new `pip install` (e.g. a bad merge during the `pcs-package` migration) leaves two copies reachable; Python's import resolution picks whichever is first on `sys.path`, silently, with no error — especially dangerous if the two disagree on §3.3's pinned constants. | Detected, not just avoided: §8's `[ ! -d /app/pythonpath/superset_ownership ]` check makes this build-time-caught, not a runtime mystery. Log the resolved `superset_ownership.__file__` at boot for visibility in every deployment's logs. |
| 6 | **FIPS constraints on dependencies.** | Zero C-extension runtime deps (§2.3). The real FIPS-adjacent constraint is the Python 3.10-vs-3.11 floor (§1, §11 Q1) — a base-image problem, not a dependency-pin problem, out of this wheel's control. `hashlib.sha256` (§3.3) is FIPS-approved; no MD5/SHA1 surfaced in this session's grep. |

---

## 10. Work breakdown

Working branch: `pcs-package`, cut from `pcs-setup` (HEAD `4e15726`). Each
PR below is `pcs-package` → `pcs-setup`; the next PR branches from the
updated `pcs-package` after the previous merges.

PR 3 below intentionally bundles what earlier drafts split into three
separate PRs (the wrap, the config-default confirmation, and removing
`overlay/backend/`/reshaping the Dockerfile). They cannot be usefully
separated: until `overlay/backend/` is gone, `Dockerfile.pcs-ownership`
still runs `COPY overlay/backend/superset/ /app/superset/` — the *old*
whole-file replace of `dashboards/api.py`/`security/manager.py`/`config.py`
is still the live mechanism on a real build, so a standalone "PR 3" wrap
would either be un-exercisable against the actual pipeline (its own
`_assert_wrappable` would immediately fail boot, since the file present is
already the PCS-10243-patched version, not stock) or its "`git diff`
against stock is empty" acceptance criterion would have nothing meaningful
to evaluate against. Landing all three together removes that
inconsistent intermediate state entirely.

| # | PR | Size | Acceptance criteria |
|---|---|---|---|
| 1 | `pyproject.toml` for `overlay/pythonpath/superset_ownership/`; no code moves. | S | `python -m build --wheel` produces the wheel; `pip install --no-deps` (a throwaway dev venv, not the PCS image — plain `pip` is fine there) + `import superset_ownership` succeeds; `unzip -l` shows the 3 force-include files present, `tests/` absent. |
| 2 | Split `configure()` into `superset_ownership/configure.py`; shrink the shim (§2.6). | S | Existing tests referencing `superset_config_ownership.configure` pass unmodified; `scratch/` scripts run unchanged. |
| 3 | Remove `overlay/backend/`; `dashboard_patch.py` (wrap, `_assert_wrappable`, `_degrade_on_failure`, `install()`, `check` field); confirm `EXTRA_OWNERS_RESOLVER`/`FEATURE_FLAGS["OBJECT_OWNERSHIP"]` need no stock declaration (§3.1); new Dockerfile shape (§6.1); `sync-from-main.sh`'s `BACKEND` bucket → advisory; `MANIFEST` regeneration. | L | `docker build` succeeds with no `overlay/backend/` present; `test_dashboard_patch.py` passes (idempotency, both mismatch raises, the `_degrade_on_failure` carve-out for `ownership check`/maintenance commands, and — against real, unmodified stock code — the integration case); `git diff` against stock `dashboards/api.py`/`security/manager.py`/`config.py` is empty against the built image's own source tree; `superset ownership status` reports `flags_agree: true` in both switch states; with `OWNERSHIP_ENABLED=true` and a deliberately-broken `_EXPECTED_SOURCE_SHA256` (simulating drift — the wrap is only ever attempted with the switch on, §3.3's early-return gate means `OWNERSHIP_ENABLED=false` never reaches `_assert_wrappable` at all and proves nothing about the degrade path), `superset ownership check` **boots and exits 1**, its output naming `dashboard_chart_patch_installed: false`, while a web/worker boot under the same broken constant **crashes `create_app()`** instead. |
| 4 | Second wheel `superset-ownership-examples`; drop it from the default pythonpath copy; `importorskip` guard. | M | Builds/installs independently; default `docker build` has no `ivanti_pcs_example` importable; `verify-image.sh` unaffected. |
| 5 | Frontend: move error-message registration into `setupErrorMessagesExtra.ts` (§7.2). `ActionButton/index.tsx` stays a replace file, unchanged. | S | `MANIFEST` frontend replace count 10→9; existing list-page tests pass unmodified. |
| 6 | `verify-image.sh` additions (§8): wheel-location check (`importlib.metadata`, no `pip`), no-stray-pythonpath check, patch-installed check. | S | All three new checks pass; each fails loudly when deliberately broken (tested by reintroducing a pythonpath copy, and by breaking the pinned constants). |
| 7 | Rebuild from the fully-migrated branch; re-run `scratch/seed-multitenant.sh`. | M | Output matches `scratch-run.md`'s recorded lines (or documents/justifies divergence); becomes the new recorded run. |
| 8 | Docs: rewrite `README.md`'s "For pcs-ivanti" section (§6.4, `uv pip install`, not `pip`); add the version-bump procedure (§8.5). | S | No instruction left to directory-`COPY` `superset_ownership` itself, and none invokes bare `pip` against the PCS venv; a fresh reader reaches a working image end to end. |

---

## 11. Open questions for Ivanti

Only what blocks this specific work — not the already-tracked
directory/tuple-shape questions (`acceptance-vs-elizabeth-spec.md` Q1–Q10),
which are FGA-contract questions independent of packaging.

1. **Python 3.10 FIPS floor** — is Ivanti's FIPS base still pinned below
   3.11? PCS 6.1.0.7 itself requires `>=3.11` (§1); if their base is
   older they cannot build the *stock* image at all, independent of
   anything here. Blocks everything else in this document.
2. **Wheel provenance** — does Ivanti want a Preset-built, signed wheel
   artifact, or to build it themselves inside their own audited FIPS
   pipeline (§6.1's self-contained `wheel-builder` stage)? Decides which
   of §6.1's two paths is their default and whether Preset needs an
   artifact-signing/publishing story.
3. **Reference-package appetite** — does Ivanti want `ivanti_pcs_example`
   as an installable second wheel (§2.5), or is an in-repo, never-packaged
   reference enough? Scopes PR 4 only, not a blocker for the rest.
4. **Confirm the PCS version pin** — still v6.1.0.7 (`76151beade`) for
   Ivanti's first build, or has their base moved? Determines whether
   §8.5's procedure needs to be exercised once before handoff (recommended
   regardless, as proof it works) or can wait.

---

## 12. Revision log

Response to the first review (rated 6/10, GO-WITH-CHANGES). Each item
re-verified against the actual code this pass, not reasoned from memory.

| Finding | What changed |
|---|---|
| 1 (blocking) — wrap imported `superset.security.manager` as a module and called `can_access_chart` on it, which doesn't exist there | §3.3: import `from superset.extensions import security_manager` (the `LocalProxy` stock `dashboards/api.py:141` itself uses), matching `superset/extensions/__init__.py:204`. Confirmed `can_access_chart` is an instance method (`superset/security/manager.py:2376`, class at `:1737`) and no module-level singleton exists in that file. |
| 2 (blocking) — boot assertion refused to start unconditionally, also blocking `db upgrade`/`check`/recovery commands | §3.3: added `_degrade_on_failure()`, reusing `plugins.maintenance_invocation()` (`plugins.py:849-879`, already used one line above for `plugins.load` at `superset_config_ownership.py:366`) plus one carve-out for `ownership check` itself. Web/Celery/backfill/data-mutating commands stay strict; maintenance + `check` degrade (log, skip the wrap, let `check` report `dashboard_chart_patch_installed: false`). |
| 3 (blocking) — `verify-image.sh`'s check used `python -m pip`, which doesn't exist in the stock venv | §8: replaced with `importlib.metadata` (stdlib, no `pip`/`uv` dependency); §6.4's Ivanti recipe switched from `pip install` to `uv pip install --python /app/.venv/bin/python`, matching the existing Dockerfile's own `psycopg2-binary` line. |
| 4 (blocking) — a proposed check hashed the entire 2,743-line `DashboardRestApi` class instead of the one method the wrap depends on | §8: dropped that check entirely. Confirmed `DashboardRestApi` is the file's only top-level class (`superset/dashboards/api.py:304`, file is 3,047 lines). Reliance is now solely on `_assert_wrappable`'s narrow per-method check (build- and boot-time) plus `superset ownership check`'s field — one mechanism, not two unsynchronized ones. |
| 5 (blocking) — dropping `ActionButton/index.tsx` from the replace set was claimed to be a free reduction | §7.2/§7.3: reverted. Confirmed the file is byte-identical to client-test main's version and its `disabled`-attribute fix is live today for 21 other consumers; eliminating it would have silently regressed them. `ActionButton/index.tsx` stays a replace file; only the `setupErrorMessagesExtra.ts` stub move remains as a real reduction (10→9). |
| 6 (blocking) — §7.4 cited a "three-way merge" that never happened | §7.4: retracted the claim. Confirmed `git log --oneline --merges 76151beade..cf5d6c7e2c` returns zero merges (23 linear commits) and `sync-from-main.sh` does a plain `git show` snapshot, not a merge. Recommendation (keep whole-file replace) unchanged, but now argued from the sha256 drift guard alone, and flagged as unverified against a real version bump until one actually happens. |
| 7 (blocking) — PR 3's acceptance criterion depended on PR 5, which ran later | §10: merged the old PR 3/4/5 into one PR (now PR 3, sized L), since the wrap can't be meaningfully tested while `overlay/backend/` still replaces the same files. Remaining PRs renumbered 4–8; internal references (§7.2, §11 Q3) updated to match. |
| Non-blocking — frontend new-file count off by one | §7.1: corrected 9→10 (`ChartSecurityAccessErrorMessage.tsx` + 9 files under `src/features/ownership/`, not 8); §7.3 now distinguishes table rows (7) from files (9). |
| Non-blocking — `inspect.getsource` can fail on a bytecode-only install | §3.3: `_assert_wrappable` now catches `OSError` from `inspect.getsource` and re-raises as `DashboardPatchError` naming the requirement explicitly (PCS base image must ship `.py` sources for `dashboards/api.py`), instead of leaking a bare `OSError`. |
| Non-blocking — `force-include`'s necessity vs. hatchling defaults unverified | §2.3: reworded to say explicitly that hatchling's default behavior wasn't checked either way, and that the explicit entries are defensive regardless of which way that resolves. |

**Round 2** (re-review of the above, rated 7/10, GO-WITH-CHANGES).

| Finding | What changed |
|---|---|
| A (blocking) — the pinned `_EXPECTED_SIGNATURE` constant does not match what `inspect.signature()` actually returns for the real, unmodified stock method | §3.3: replaced the stringified-signature comparison with a parameter NAMES-and-KINDS comparison (`_EXPECTED_PARAMS`, `tuple((name, p.kind) for name, p in inspect.signature(method).parameters.items())`), which is invariant to annotation formatting. Reproduced the actual bug independently this session: `dashboards/api.py` has no `from __future__ import annotations` (`grep -n "from __future__" superset/dashboards/api.py` — no match) and imports `Any` plainly (`superset/dashboards/api.py:23`), so `str(inspect.signature(...))` for an equivalent method is `"(self, chart: Any) -> dict[str, typing.Any]"`, not the quoted-annotation form the old constant assumed — confirmed by running both variants through `inspect.signature()` locally. The new check depends only on parameter identity/order/kind, which is what actually has to hold for `_original(self, chart)` to remain a valid call; annotation spelling is cosmetic and no longer checked. §8's test coverage and §8.5 step 4 updated to match (derive `_EXPECTED_PARAMS` from the real, imported stock method — not hand-typed — and test that derivation directly). |
| B (blocking) — PR 3's acceptance criterion tested the wrong thing for the degrade path | §10: rewritten. The old text used `OWNERSHIP_ENABLED=false`, under which §3.3's own early-return means `_assert_wrappable` is never called at all — nothing was being exercised. Corrected to `OWNERSHIP_ENABLED=true` with a deliberately-broken `_EXPECTED_SOURCE_SHA256`, asserting `superset ownership check` **boots and exits 1** (reporting `dashboard_chart_patch_installed: false`) while a web/worker boot under the same broken constant **crashes** — the actual invariant `_degrade_on_failure()` is meant to guarantee, not "check succeeds." |

---

## 13. Implementation notes (deviations from the sections above, as built)

Recorded as the PRs landed; the sections above are left as reviewed.

| Spec | As built | Why |
|---|---|---|
| §2.3 / §6.1: `pyproject.toml` inside `overlay/pythonpath/superset_ownership/`, Dockerfile `COPY`s it from there | `packaging/superset-ownership/{pyproject.toml,README.md}` + `packaging/build-wheel.sh`; the `wheel-builder` stage assembles `{pyproject, README} + overlay/pythonpath/superset_ownership` into `/src` | `scripts/sync-from-main.sh` `rm -rf`s and regenerates the module directory from `client-test/main`; anything placed inside it is wiped on the next sync |
| §10: every PR against `pcs-setup` | module code (`configure.py`, `dashboard_patch.py`, tests, the dev config, the error-message stub) landed on `client-test/main` (#104, #105, #106, #107) and was synced in; only the delivery shell (packaging, Dockerfile, scripts, docs) changed on `pcs-package` | same reason: the overlay is generated from main, not edited |
| §3.3 / §6.1: build-time check `from superset.dashboards.api import DashboardRestApi; _assert_wrappable(...)` | `dashboard_patch.assert_stock_file()` — locates the method in the installed `superset/dashboards/api.py` with `ast` and cuts its block with `inspect.getblock`, byte-identical to `inspect.getsource` (asserted by test) | importing `superset.dashboards.api` needs an initialised app (`superset/utils/encrypt.py:247` via `models/core.py:185`); a build step has none |
| §3.3: wrapper calls the original then layers the marker | wrapper restates the pinned four-line stock body and adds the marker | `can_access_chart` runs once per tile instead of twice; the source pin (asserted immediately before the wrapper is defined) is what makes restating safe; a differential test holds the wrapper to the stock method's output |
| §2.3: seven dependencies | nine: adds `flask-appbuilder` and `flask-wtf` (unguarded runtime imports in `hooks.py`/`api.py`/`plugin_verify.py`); all host-provided, `--no-deps` install | review of #103 |
| §6.3: `BACKEND` bucket advisory | `sync-from-main.sh` exits 1 if main forks any stock backend file | nothing copies those files any more; a non-empty bucket is a regression, not information |
| §7.2: `setupErrorMessagesExtra.ts` stub | done (#106); frontend replace count 10 → 9; the stub is still a `replace` entry in `MANIFEST` (the script has no `stub` kind) | naming gap only |
| §2.5 / §10 PR 4: `ivanti_pcs_example` as a second wheel | not done; the reference package still rides on `PYTHONPATH` as an individual entry (Dockerfile + `apply-overlay.sh`) | parked on Ivanti's answer to §11 Q3; nothing in a default deployment depends on it |
| §8: verify-image additions | wheel-in-venv (`importlib.metadata`), no stray pythonpath copy, the three stock files sha256-equal to upstream, `assert_stock_file()` | as specified, plus the stock-file equality check |

# Review: PCS-10243 object ownership as a pip wheel

**Rating: 6/10**
**Verdict: GO-WITH-CHANGES**

The packaging design (wheel over the stock PCS venv, config glue as a
2-line pythonpath shim, migrations resolved off `__file__`, zero
third-party dependencies) is sound and, on the parts I could check
against the actual trees and a running stock image, verified accurate
line-for-line. But the spec ships one runtime-breaking code bug in the
centerpiece mechanism (the dashboard-chart wrap), two verification-script
checks that cannot pass against the real target image, a factual claim
about a "zero behavior change" frontend change that the diff itself
contradicts, and a load-bearing justification (§7.4) built on a claim
about git history that the history doesn't support. None of these require
re-architecting the approach — they're concrete, fixable defects — but
they need to be fixed before PR 3 (the wrap), PR 7 (the frontend
reduction) and PR 8 (verify-image.sh) can be merged as specified.

---

## Blocking findings

### 1. The dashboard-chart wrap calls a method that doesn't exist on the object it holds — it will crash on the exact path it's meant to protect

`pip-wheel-spec.md:271` and `:278-280`:

```python
from superset.security import manager as security_manager
...
def patched(self, chart, *, _original=original, _sm=security_manager):
    serialized = _original(self, chart)
    if not _sm.can_access_chart(chart):
```

`from superset.security import manager as security_manager` binds the
name to the **module** `superset.security.manager`, not to a security
manager instance. `can_access_chart` is not a module-level function in
that file — it's a method of the `SupersetSecurityManager` class defined
inside it (`superset/security/manager.py:1737` class def, `:2376`
`def can_access_chart(self, chart) -> bool:`, 4-space indented, i.e. an
instance method). Confirmed no module-level `security_manager = ...`
singleton exists in that file either
(`grep -n "^security_manager\s*=" superset/security/manager.py` — no
match).

The object stock `dashboards/api.py` itself calls `can_access_chart` on
is a different thing entirely: `superset/dashboards/api.py:141`,
`from superset.extensions import event_logger, security_manager` — a
`LocalProxy` (`superset/extensions/__init__.py:204`,
`security_manager: SupersetSecurityManager = LocalProxy(lambda: appbuilder.sm)`)
wrapping the live Flask-AppBuilder security-manager instance.

As written, `patched()` raises `AttributeError: module
'superset.security.manager' has no attribute 'can_access_chart'` the
first time it runs against a chart the caller cannot access — i.e. on
the one code path this whole feature exists to protect. This isn't a
theoretical edge case; it's the primary branch.

**Fix.** Import the same object stock code imports:
`from superset.extensions import security_manager` (matching
`superset/dashboards/api.py:141`), not
`from superset.security import manager as security_manager`. §3.2's own
`get_access_contact_names` move gets this right (it never imports
`security_manager` at all); §3.3's wrap does not.

### 2. "Refuse to start, never degrade" is wired without the maintenance-command carve-out the codebase already established for exactly this class of failure — and for Ivanti's actual pipeline, it's the *only* safety net

§3.3 (`pip-wheel-spec.md:298-308`) commits to letting `DashboardPatchError`
propagate out of `FLASK_APP_MUTATOR` uncaught, crashing `create_app()`.
The spec frames this as mirroring "the project's own existing
philosophy." It doesn't: the project's actual existing philosophy, one
scroll up in the same mutator, is to *distinguish* invocation contexts.
`overlay/pythonpath/superset_config_ownership.py:361`,
`plugins.load(app, strict=not plugins.maintenance_invocation())` —
`plugins.maintenance_invocation()`
(`overlay/pythonpath/superset_ownership/plugins.py:849-879`) exists
specifically so that `superset ownership db upgrade`, `status`, `fga
reconnect/status`, etc. keep working in a DEGRADED mode when something
else is broken, "the situation an operator reaches for it in" per that
function's own docstring.

`dashboard_patch.install()` is to be wired into the same
`_flask_app_mutator_on` (§3.3, "alongside `guard.install()`/
`cli.install(app)`" — see `superset_config_ownership.py:390-398`), but
with no such gate. `_flask_app_mutator_on` runs on *every*
`create_app()` call, and `create_app()` is called by:
- the web/gunicorn process,
- `superset/tasks/celery_app.py:32` (`flask_app = create_app()` —
  "the main entrypoint used by Celery workers"),
- `superset/cli/main.py`'s `FlaskGroup` (every `superset ...` CLI
  invocation),
- and `docker/entrypoint-ownership.sh:106-112`'s own backfill step
  (`python -c "from superset.app import create_app; app = create_app(); ..."`).

So a signature/source-hash mismatch on `_serialize_dashboard_chart` — a
method with nothing to do with migrations, backfill, or `check` — would
also prevent `superset ownership db upgrade`, `backfill-tenants`, and
`check` from ever running, i.e. the exact commands an operator needs to
either recover or even confirm what's wrong.

This is not a rare corner: §6.4's Ivanti recipe
(`pip-wheel-spec.md:499-509`) has **no** build-time assertion step at
all — the boot-time assertion in `install()` is the *entire* safety net
for the pipeline the spec's own §1 assumptions call out as the real
target ("Ivanti builds from source in their own FIPS pipeline").

**Fix.** At minimum, gate `dashboard_patch.install()`'s failure mode the
same way `plugins.load` is gated — degrade (log loudly, skip the wrap,
leave the endpoint at stock un-narrowed behavior *or* refuse only the
dashboard blueprint, not the whole app) for
`plugins.maintenance_invocation()`-style commands, hard-fail for
web/worker. Alternatively, install the wrap lazily (on first actual
dashboard-chart serialization) rather than at every `create_app()`, so a
`db upgrade` invocation never touches it at all.

### 3. `verify-image.sh`'s wheel-install check cannot pass against the actual target image — `pip` is not installed in the venv

§8 (`pip-wheel-spec.md:613-614`):

```bash
check "superset_ownership installed as a wheel" \
  docker run --rm "$IMAGE" sh -c 'python -m pip show superset_ownership | grep -q "^Location: /app/.venv"'
```

Checked directly against the actual stock image (read-only inspection,
no compose/containers started):

```
$ docker run --rm --entrypoint sh pcs-stock:v6.1.0.7 -c \
    '/app/.venv/bin/python -m pip --version'
Traceback (most recent call last):
  ...
ModuleNotFoundError: No module named pip
```

`PATH` in the image puts `/app/.venv/bin` first (confirmed:
`which python` → `/app/.venv/bin/python`), so `python -m pip` in the
check resolves to the venv's python, which has no `pip` module — package
management in this image goes through `uv` (`which uv` →
`/usr/local/bin/uv`, already used by the Dockerfile's own
`psycopg2-binary` install line, and by §6.1's proposed wheel-install
line). This check fails unconditionally, whether or not the wheel is
correctly installed, so it can never validate what it's named for.

**Fix.** Use `uv pip show superset_ownership --python /app/.venv/bin/python`,
or a pure-Python check via `importlib.metadata`
(`python -c "import importlib.metadata as m; print(m.distribution('superset-ownership')._path)"`),
neither of which depends on `pip` being present.

### 4. `verify-image.sh`'s "source matches" check hashes the entire 2,743-line `DashboardRestApi` class, not the one method the wrap actually depends on — and nothing keeps it in sync with §3.3's own check

`pip-wheel-spec.md:617-620`:

```bash
check "DashboardRestApi source matches PCS_IMAGE's own" \
  docker run --rm "$IMAGE" python -c \
  "import hashlib, inspect; from superset.dashboards.api import DashboardRestApi; \
   assert hashlib.sha256(inspect.getsource(DashboardRestApi).encode()).hexdigest() == '<pinned, refreshed per §8.5>'"
```

`inspect.getsource(DashboardRestApi)` returns the source of the *whole
class*. Confirmed: `DashboardRestApi` is the only top-level class in
`superset/dashboards/api.py` (`grep -n "^class " superset/dashboards/api.py`
→ one hit, `:304`), and the file is 3,047 lines long — i.e. the class
spans roughly `superset/dashboards/api.py:304-3047`, ~2,743 lines
covering dozens of unrelated REST endpoints.

This is a far stricter check than `dashboard_patch._assert_wrappable`'s
own per-method signature+hash check, and it isn't reconciled with it
anywhere: §8.5's version-bump procedure (step 4,
`pip-wheel-spec.md:645-648`) only mentions refreshing
`_EXPECTED_SIGNATURE`/`_EXPECTED_SOURCE_SHA256` — it says nothing about
this second, independent whole-class hash also needing a refresh. Any
unrelated change anywhere else in that class (a new endpoint, a
docstring fix, a lint-driven reformat) breaks this check and fails
`verify-image.sh`, even when `_serialize_dashboard_chart` and the wrap's
own contract are both completely intact. That directly undercuts PR 8's
own acceptance criterion ("each [check] fails loudly when deliberately
broken") — it will *also* fail loudly when nothing relevant is broken,
which is a worse failure mode for an acceptance gate than being silent.

**Fix.** Hash the same narrow surface `_assert_wrappable` does (drop this
check and rely on `superset ownership check`'s
`dashboard_chart_patch_installed` field plus the boot-time assertion —
already proposed elsewhere in §3.3/§8 — instead of a second, broader,
un-synchronized mechanism), or scope this hash to
`inspect.getsource(DashboardRestApi._serialize_dashboard_chart)`
specifically and fold its maintenance into the same §8.5 step 4.

### 5. §7.2(a)'s "zero behavior change elsewhere" claim for dropping `ActionButton/index.tsx` from the replace set is false

`pip-wheel-spec.md:539-540`: "Removes 1 of 10 replace entries (→9), zero
behavior change elsewhere — this file was a general accessibility fix
riding along in the patch set."

Checked: the overlay's current `ActionButton/index.tsx` is byte-identical
to client-test main's patched version
(`diff overlay/frontend/packages/superset-ui-core/src/components/ActionButton/index.tsx pcs-617/superset-frontend/packages/superset-ui-core/src/components/ActionButton/index.tsx`
→ no output). That patch adds a real HTML `disabled` attribute (plus a
tooltip-preserving span) to the shared `ActionButton` component — this
is currently live for **every** consumer of that component, not just the
future Share button. Counted 21 other files importing `ActionButton`
today (`ChartList/index.tsx`, `DashboardList/index.tsx`,
`ListView/ActionsBar.tsx`, `FilterBar/index.tsx`,
`SqlEditor/index.tsx`, `SaveQuery/index.tsx`, etc. — full list gathered
via grep).

§7.2(a)'s decision is to stop replacing `ActionButton/index.tsx` and
instead wrap the *unmodified upstream* component locally in a new
`ShareActionButton.tsx`, used only by the Share button. That reverts the
`disabled`-attribute fix for all ~21 existing consumers — buttons that
are, today, genuinely inert when disabled would go back to being only
`aria-disabled` (screen-reader-inert but still clickable via anything
that doesn't route through the aria attribute — exactly the defect the
original patch's own comment describes). "Zero behavior change
elsewhere" is the opposite of what happens; it's a real, silent
regression for everyone except the one new button that keeps the fix.

**Fix.** Either keep `ActionButton/index.tsx` on the replace list (accept
the 10th entry doesn't shrink), or — if the goal is genuinely to
upstream this fix instead — say so explicitly and treat it as a
deliberate, called-out behavior change to sign off on, not a free
reduction.

### 6. §7.4's argument for keeping whole-file replace over `git apply` cites a merge that never happened

`pip-wheel-spec.md:576-578`: "`scripts/sync-from-main.sh` already
generates overlay files from a **real git branch** (`client-test/main`)
already rebased/merged against upstream via git's own three-way merge —
strictly more capable than `patch`/`git apply`'s line-context matching."

Checked against the actual history:
`git log --oneline --merges 76151beade..cf5d6c7e2c` returns **zero**
merge commits. `client-test/main` is 23 linear feature commits
(`302011f048` down to `7e8dc4c4b4`) built directly on top of
`76151beade`, and `76151beade` has never moved — §1's own assumption is
that the PCS pin (`v6.1.0.7`, `76151beade`) hasn't changed since the
overlay was cut. There has been no three-way merge to point to, because
upstream has never needed to be re-integrated into this branch yet. And
`sync-from-main.sh` itself doesn't merge anything — it materializes
files with a plain `git show "$REMOTE/$REF:$path" > ...` snapshot
(`scripts/sync-from-main.sh`, the `for entry in "${FRONTEND[@]}"` loop).

The underlying recommendation (whole-file replace over `git apply` hunks)
may still be the right call on other grounds — the sha256 drift guard
plus a real diff review at PR time is a reasonable substitute for hunk
matching — but the specific evidence cited for it doesn't exist yet.
When the PCS version *does* bump and `client-test/main` has to actually
catch up to a newer upstream, whatever process does that (rebase, merge,
manual reconciliation) is exactly what determines whether this argument
holds — and that process is unverified here.

**Fix.** Drop the "already... via git's own three-way merge" claim, or
replace it with what's actually true: the argument rests on `MANIFEST`'s
sha256 drift guard catching staleness, not on a merge that has already
resolved conflicts.

### 7. PR 3's acceptance criterion can't be satisfied at the point it's supposed to run

PR 3 (`pip-wheel-spec.md:683`) is "`dashboard_patch.py`: wrap,
`_assert_wrappable`, `install()`, ON-mutator wiring, `check` field,"
with acceptance criteria including "`git diff` against stock
`dashboards/api.py`/`security/manager.py` is empty." PR 5
(`pip-wheel-spec.md:685`) is "Remove `overlay/backend/`; new Dockerfile
shape." PRs are ordered 3 before 5.

Until PR 5 lands, the Dockerfile still runs
`COPY overlay/backend/superset/ /app/superset/`
(`docker/Dockerfile.pcs-ownership`, "backend core: the 3 files, verbatim
replace" step) — i.e. the *old* whole-file replace of
`dashboards/api.py`/`security/manager.py` is still the live mechanism
through the PR3-PR4 window, on top of whatever the wheel does. A built
image at PR 3's stage would still show `dashboards/api.py` diverging
from stock via the pre-existing replace file, independent of anything
the wrap does — so "`git diff` against stock is empty" cannot be
evaluated against the actual build pipeline at that point without
`overlay/backend/` already being gone (which is PR 5's job) or the test
being run against some other tree the spec doesn't describe (e.g. a
scratch checkout of pristine `76151beade` with only the wheel installed,
bypassing the Dockerfile).

**Fix.** Either state explicitly how PR 3's acceptance test is isolated
from the still-live `overlay/backend/` replacement (e.g. "run against a
throwaway venv built from pristine upstream + the wheel, not the full
Dockerfile"), or reorder so PR 5's Dockerfile change and PR 3's wrap land
together as one PR.

---

## Non-blocking findings

- **Frontend file-count arithmetic is off by one, twice.** §1's table
  (`pip-wheel-spec.md:524`) says "9 (`ChartSecurityAccessErrorMessage.tsx`
  + 8 under `src/features/ownership/`)" — `MANIFEST` lists 9 files under
  `src/features/ownership/` (`OwnerCell.tsx`, `SharingDrawer.tsx`+test,
  `SubjectPickerPanel.tsx`+test, `VisibilityTag.tsx`, `api.ts`+test,
  `types.ts`), so the true "new" count is 10, not 9. Separately, §7.3's
  header "Six files (down from ten)" (`pip-wheel-spec.md:565`) counts
  table *rows*, not files — two of those six rows are "+ test" pairs, so
  the actual remaining file count is 8, matching §7.2's own "→8" a few
  lines up. Cosmetic, but worth fixing before it's read as a real
  discrepancy.

- **`inspect.getsource()`'s dependency on `.py` source files being present
  is untested against Ivanti's actual FIPS build.** If Ivanti's own
  "build from source in their own FIPS pipeline" (§1) strips `.py`
  sources and ships only compiled bytecode — a common hardening step for
  compliance-oriented images — `_assert_wrappable`'s
  `inspect.getsource(method)` raises `OSError: could not get source
  code`, a different and more confusing failure than the intended
  `DashboardPatchError`, though it still fails closed. Worth confirming
  with Ivanti rather than assuming (this repo's own Dockerfile only runs
  `compileall` for bytecode caching, `.py` files are not removed — but
  that's this repo's pipeline, not Ivanti's).

- **`force-include`'s necessity vs. hatchling's defaults isn't
  established.** §2.3 justifies `force-include` over relying on VCS-file
  tracking, but doesn't check whether hatchling's default wheel-building
  behavior for a plain `packages = [...]` target already includes
  non-`.py` files under the package directory. If it does, `force-include`
  is redundant-but-harmless; if it doesn't, it's load-bearing as
  described. Either way this doesn't change the conclusion (verified:
  `alembic.ini`, `script.py.mako`, `model/ownership.fga` are indeed the
  only non-`.py`, non-excluded package files today), just the "why."

---

## What the spec gets right

- **The zero-third-party-dependency claim is accurate.** Restricting the
  grep to genuine module-scope imports (as opposed to the many
  function-body-local imports the package's own stated convention
  requires) turns up exactly `alembic`, `click`, `flask`,
  `flask.cli.with_appcontext`, `flask_jwt_extended`, `flask_login`,
  `requests`, `sqlalchemy` — nothing else — and all seven are pinned in
  `pcs-617/requirements/base.txt` at the exact versions cited, confirmed
  by importing them inside the actual stock image's venv.
- **The migration force-include set is exactly right.** `alembic.ini`,
  `script.py.mako`, and `model/ownership.fga` are indeed the only
  non-`.py`, non-test, non-excluded files in the package tree; nothing
  else needs a force-include entry.
- **`FLASK_APP_MUTATOR` timing works out, even though the spec doesn't
  spell out why.** It runs at `superset/initialization/__init__.py:1050-1051`,
  *before* `init_views()` (`:1057`) registers `DashboardRestApi` with
  Flask-AppBuilder. Since `_serialize_dashboard_chart` is called via
  `self._serialize_dashboard_chart(...)` (an ordinary dynamic attribute
  lookup, not a decorator-captured reference — confirmed only call site
  is `superset/dashboards/api.py:888`), patching the class attribute
  works regardless of registration order, and here it happens to run
  even before registration.
- **The "refuse to start" philosophy is right for both Celery and the
  CLI** (setting aside finding #2's gating problem) — both really do go
  through `create_app()`, confirmed via `superset/tasks/celery_app.py:32`
  and `superset/cli/main.py`'s `FlaskGroup`.
- **The config-pre-declaration reasoning (§3.1) is accurate**: neither
  `FEATURE_FLAGS["OBJECT_OWNERSHIP"]` nor `EXTRA_OWNERS_RESOLVER` needs a
  stock default — confirmed via `FeatureFlagManager.init_app()`'s
  `dict.update()` merge and `current_app.config.get(...)`'s plain
  `None`-on-missing behavior.
- **Import-cycle risk in `configure()` is already handled**: every
  `superset.*` reference in `superset_config_ownership.py` is inside a
  function body, none at module scope — moving the 421-line body
  verbatim into the wheel doesn't introduce anything new here.
- **The double-install shadowing risk (§9 #4/#5) is grounded in a real,
  previously-hit bug**, not speculative, and the proposed
  `[ ! -d /app/pythonpath/superset_ownership ]` check targets the actual
  current copy path correctly.
- All cited line-count diffs (`config.py` +14, `dashboards/api.py`
  +20/−1, `security/manager.py` +36) match `git diff --stat` exactly.

---

## Questions for the author

1. Finding #1 makes we ask: was §3.3's code block actually exercised
   (even informally) against a running Superset, or written from reading
   the diff alone? The `security_manager` import mistake is the kind of
   thing a single manual test would have caught immediately.
2. For finding #2: is there an appetite for a "degraded" mode for the
   dashboard-chart wrap specifically — e.g. log and fall back to
   stock (un-narrowed) chart serialization rather than crashing the
   whole process — or is a hard app-wide crash actually the intended
   behavior for *every* invocation type, accepting that this blocks
   `db upgrade`/`check` too when it fires?
3. For §6.4/Ivanti's pipeline: is the build-time assertion step
   deliberately left out of Ivanti's recipe, or just not yet
   transcribed? If deliberate, finding #2 is even more load-bearing than
   written above.
4. For §7.2(a): is reverting the `ActionButton` accessibility fix for
   existing consumers actually acceptable, or should this be pursued as
   a genuine upstream contribution instead (it's a small, generic
   accessibility fix — a better upstream candidate than most of the
   other nine replace-set files)?

---

## Round 2 (re-review of the revised spec, 948 lines, §12 revision log)

**Updated rating: 7/10**
**Verdict: GO-WITH-CHANGES**

Six of the seven blocking findings hold up against the actual code — the
revision log is not just a claim, the fixes are real, and in two cases
(finding 3's `importlib.metadata` swap, finding 4's dropped whole-class
hash) I reproduced the relevant behavior locally to confirm rather than
reading the diff and trusting it. But re-checking the *corrected* wrap
code line by line surfaced a new defect in the same mechanism finding 1
touched: the pinned `_EXPECTED_SIGNATURE` constant does not match what
`inspect.signature()` actually produces against the real, unmodified
stock method, so `_assert_wrappable` — and therefore both the build-time
check and the boot-time `install()` — would fail against genuinely
correct, undrifted code, every time, everywhere it's checked. That's a
"the wrap can never successfully install, and the build-time RUN step
in §6.1/§6.4 fails unconditionally" level of defect, not a cosmetic one.
It was present in the original spec too (round 1 missed it — it's a
different bug from the `security_manager` import mistake finding 1
originally flagged), so it isn't something the revision introduced, but
it does mean finding 1 is only partially closed. One more concrete,
narrow, mechanical fix (correct the constant) is needed before this is
implementation-ready.

### Per-finding re-verification

**1 — wrap's security-manager call (partially resolved; new defect found in the same block).**
The specific bug from round 1 is fixed: §3.3 now does
`from superset.extensions import security_manager`
(`pip-wheel-spec.md:338`), matching stock `dashboards/api.py:141`'s own
import and the `LocalProxy` at `superset/extensions/__init__.py:204`.
`_sm.can_access_chart(chart)` now resolves correctly through the proxy.

But re-deriving the same code's `_EXPECTED_SIGNATURE`
(`pip-wheel-spec.md:273`, `"(self, chart: 'Any') -> 'dict[str, Any]'"`)
against the real target function shows it's wrong. `dashboards/api.py`
has no `from __future__ import annotations` (checked: `grep -n "from
__future__" superset/dashboards/api.py` — no match), and imports `Any`
plainly (`superset/dashboards/api.py:23`, `from typing import Any,
Callable, cast`). Reproduced the exact signature shape locally
(Python 3.11.11; the stock image runs 3.11.14, and `inspect`'s
annotation-formatting logic does not change between patch releases):

```
>>> class C:
...     def _serialize_dashboard_chart(self, chart: Any) -> dict[str, Any]:
...         return {}
>>> str(inspect.signature(C._serialize_dashboard_chart))
'(self, chart: Any) -> dict[str, typing.Any]'
```

That's the real value — no quotes, `typing.Any` (not `Any`) in the return
position, because `dict[str, Any]`'s own `repr()` doesn't get the same
"drop the `typing.` prefix" special-case `inspect.formatannotation()`
applies to a bare top-level annotation. The spec's pinned string, with
`Any` quoted in both positions and no `typing.` prefix, is exactly what
`inspect.signature()` produces when `from __future__ import annotations`
*is* active (confirmed by reproducing that variant too:
`str(inspect.signature(...))` →
`"(self, chart: 'Any') -> 'dict[str, Any]'"` verbatim) — i.e. the
constant looks like it was derived against a mental model or a test
reproduction that assumed stringified annotations, not against the real
file, which doesn't use them.

Consequence: `_assert_wrappable`'s signature check
(`pip-wheel-spec.md:280-284`) would raise `DashboardPatchError` against
*correct, unmodified* stock code — always, not just on real drift. That
breaks:
- the build-time `RUN` step in both §6.1 (`pip-wheel-spec.md:563-566`)
  and §6.4 (`:626-629`) — every `docker build` fails unconditionally;
- `install()`'s boot-time call — for web/worker this now crashes
  `create_app()` on a correctly-built image, i.e. the feature could
  never actually turn on;
- the new integration test §8 asks for ("against real, *unmodified*
  stock code... a private chart... returning `has_access: false`" —
  `pip-wheel-spec.md:775-779`) would also fail to reach that assertion,
  since `install()` never gets past `_assert_wrappable`.

**Fix.** Don't hand-type this constant. Derive it mechanically — a small
script (or a CI step) that imports the actual pinned upstream source at
packaging time and captures `str(inspect.signature(...))` verbatim —
and add a test that fails loudly if the checked-in constant and the
freshly-derived one disagree, so a transcription mistake like this one
is caught automatically rather than by manual review next time (see
residual risks below).

**2 — degrade gating (resolved).** Verified `_degrade_on_failure()`
(`pip-wheel-spec.md:304-334`) correctly layers on top of
`plugins.maintenance_invocation()` (confirmed unchanged at
`overlay/pythonpath/superset_ownership/plugins.py:849-879`, which
explicitly excludes `check` from its own maintenance set) with exactly
one added carve-out (`rest[0] == "check"`). Traced all the invocation
shapes named in the docstring: Celery's `sys.argv` never contains
"ownership" (`superset/tasks/celery_app.py:32` calls `create_app()`
directly, no argv parsing involved) → strict; the entrypoint's bare
`python -c "from superset.app import create_app; ..."` backfill step
(`docker/entrypoint-ownership.sh:106-112`) has `sys.argv == ['-c']` →
strict; `superset ownership check` has `sys.argv` ending in
`['ownership', 'check']` → degrades. The degrade branch itself
(`install()`, `pip-wheel-spec.md:344-358`) correctly `return`s before
ever assigning `patched`, so a degraded process never silently serves
the un-narrowed stock behavior as if it were the wrap — it just doesn't
serve at all for the strict paths, and the diagnostic paths get stock
behavior with the failure logged and reported by `check`. No issue
found in the gating logic itself (independent of finding 1's constant
bug, which prevents this logic from ever seeing the "assertion actually
passed" branch against real code today).

**3 — `verify-image.sh`'s wheel-location check (resolved).** Reproduced
the replacement check's core operation locally
(`importlib.metadata.distribution('pip').locate_file('')` →
`/Users/.../lib/python3.11/site-packages`, i.e. exactly the venv's
`site-packages` directory) — confirms `p.startswith('/app/.venv')`
(`pip-wheel-spec.md:799-800`) is a sound test against a real wheel
install and needs neither `pip` nor `uv` on `PATH`. Also confirmed §6.4
now uses `uv pip install` throughout (`:621`), consistent with §8's own
"stock venv has no pip" note (`:787-794`).

**4 — whole-class hash check (resolved).** Confirmed dropped
(`pip-wheel-spec.md:811-830`); the section now relies solely on
`_assert_wrappable`'s narrow per-method check (build- and boot-time) plus
`superset ownership check`'s `dashboard_chart_patch_installed` field via
the new `"dashboard-chart patch installed"` `verify-image.sh` check
(`:803-807`), which is sufficient coverage without the broader,
unsynchronized mechanism the original draft had.

**5 — `ActionButton/index.tsx` (resolved).** §7.2 now keeps it in the
replace set (`pip-wheel-spec.md:659-687`) and the "zero behavior change"
framing is gone; §7.3's table (`:706`) explicitly lists it with the real
reason it must stay. Cross-checked the file counts this revision claims:
MANIFEST's frontend section still has the same 10 replace entries;
dropping only `setupErrorMessages.ts` (§7.2's remaining reduction) yields
9, matching `:699` and the corrected table.

**6 — §7.4's git-merge claim (resolved).** The retraction
(`pip-wheel-spec.md:727-740`) matches what I verified in round 1 (`git
log --oneline --merges 76151beade..cf5d6c7e2c` → no merge commits) and
now correctly attributes `sync-from-main.sh`'s frontend materialization
to a plain `git show` snapshot, not a merge. The revised argument (drift
guard + PR-time diff review) is honest about not yet being tested against
a real version bump (`:748-755`) rather than overclaiming — a reasonable
place to land.

**7 — PR sequencing (resolved, with one new problem in the merged PR's
own acceptance text — see new finding B below).** Confirmed PR 3 now
bundles the wrap, the config-default confirmation, and the
`overlay/backend/` removal/Dockerfile reshape into one PR
(`pip-wheel-spec.md:894-898`), with an explicit rationale
(`:880-892`) for why they can't be split. Renumbering is internally
consistent — checked `§7.2`'s "PR 5" reference (`:700`, now correctly
"Frontend: move error-message registration...") against the table's PR 5
row (`:900`), and `§11 Q3`'s "Scopes PR 4 only" (`:924`) against the
table's PR 4 (second wheel, `:899`) — both line up.

### New findings (Round 2)

**A. `_EXPECTED_SIGNATURE` doesn't match real `inspect.signature()`
output — covered in detail under finding 1 above.** This is the
headline issue of this round: it's not a new mistake introduced by the
revision (the constant is untouched from round 1's code block, and round
1's review didn't catch it either), but it's every bit as blocking as
the issues that were fixed, and it sits in exactly the code path finding
1 was about. Flagging it as a numbered finding in its own right because
fixing finding 1's `security_manager` bug without also fixing this one
leaves the wrap just as non-functional as before, for a different
reason.

**B. PR 3's new acceptance-criteria text tests the wrong thing for the
degrade path.** `pip-wheel-spec.md:898`: "`superset ownership check`
succeeds with `OWNERSHIP_ENABLED=false` even with a deliberately-broken
assertion (proves the degrade path)." Two problems with this sentence:

1. With `OWNERSHIP_ENABLED=false`, `_flask_app_mutator_on` (the ON-state
   mutator that would call `dashboard_patch.install()`) never runs at all
   — §3.3 says the wrap is "gated the same way — only when
   `_ownership_enabled` is true" (`pip-wheel-spec.md:231-232`, unchanged
   from round 1). So `_assert_wrappable` is never even called in this
   scenario; there is nothing to degrade from, and this test would pass
   trivially regardless of whether `_degrade_on_failure()` works at all.
   It tests the pre-existing OFF-switch early-return, not the new
   degrade logic.
2. Even with the right setup (`OWNERSHIP_ENABLED=true` + a genuinely
   broken constant), "succeeds" is the wrong word for what `check` does
   in the intended degrade scenario: `dashboard_chart_patch_installed:
   false` is exactly the kind of `false` field `cli.py:178-179` already
   exits 1 on (per this spec's own §3.3 text, unchanged: "`check`
   already exits 1 on any `false`," `pip-wheel-spec.md:441-442`). The
   correct, intended behavior is that the *process boots* (doesn't crash)
   while `check` *reports failure* (non-zero exit) — not that the command
   "succeeds."

**Fix.** Rewrite the acceptance criterion to something like: "with
`OWNERSHIP_ENABLED=true` and a deliberately-broken
`_EXPECTED_SOURCE_SHA256`, `superset ownership check` exits 1 but does
not crash, and its output includes `dashboard_chart_patch_installed:
false`; the same broken constant crashes `create_app()` for the web
process." That's the actual invariant `_degrade_on_failure()` is meant
to guarantee, and it's a meaningfully different test than the one
currently written down.

### Residual risks to track during implementation

- **Hand-typed pinned constants are demonstrably error-prone** (finding
  A is exactly this class of mistake). Recommend a packaging-time or
  CI-time script that derives `_EXPECTED_SIGNATURE` (and ideally
  `_EXPECTED_SOURCE_SHA256`) mechanically from the actual pinned upstream
  source, plus a test that fails if the checked-in constants drift from
  what that script produces — not just a human re-reading §8.5 step 4 at
  every version bump.
- **§7.4's whole-file-replace recommendation is explicitly unverified**
  against a real conflict-resolution scenario (the spec says so itself
  now, `pip-wheel-spec.md:748-755`) — worth a deliberate check-in the
  first time `scripts/fetch-pcs.sh` actually targets a new PCS tag,
  rather than assuming it'll be fine because it's simpler.
- **The `inspect.getsource` `OSError` fallback is real but still
  unconfirmed against Ivanti's actual FIPS pipeline** — reproduced
  locally that a bytecode-only import raises exactly `OSError: source
  code not available` while `inspect.signature` keeps working even
  without source, so the fallback's error message is accurate and useful
  as written; whether Ivanti's build actually strips `.py` sources is
  still an open question (§11 doesn't list it explicitly — worth adding
  as a fifth open question).
- **No test in the plan currently exercises `_assert_wrappable` against
  the real, imported stock function** (as opposed to a hand-constructed
  reproduction) **before this spec is implemented** — finding A would
  have been caught immediately by running `_assert_wrappable(DashboardRestApi._serialize_dashboard_chart)`
  against a real stock checkout once. Worth doing that as a first step
  of PR 3, before writing the rest of the wrap, rather than after.
- **PR 3 is now sized L and carries the entire wrap + Dockerfile reshape
  in one unit** (a direct consequence of the finding-7 fix) — worth
  planning for that PR to take longer / need more review attention than
  its neighbors, since it's no longer three independently-reviewable
  units.

---

## Round 3 (re-review of the two Round 2 fixes, 983 lines, §12's "Round 2" block)

**Final rating: 9/10**
**Verdict: GO**

Both Round 2 findings are genuinely fixed, not just described as fixed,
and re-verifying them surfaced no new problems.

**Finding A (pinned signature constant) — resolved, reproduced
directly.** §3.3 (`pip-wheel-spec.md:288-304`) now compares parameter
*names and kinds* instead of a stringified signature:

```python
_EXPECTED_PARAMS = (
    ("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    ("chart", inspect.Parameter.POSITIONAL_OR_KEYWORD),
)
...
actual = tuple((name, p.kind) for name, p in inspect.signature(method).parameters.items())
if actual != _EXPECTED_PARAMS: raise DashboardPatchError(...)
```

Reproduced this exact comparison against a method with the real stock
signature shape (`self, chart: Any`, no `from __future__ import
annotations`, matching `dashboards/api.py:23`'s plain `Any` import):

```
actual:   (('self', <POSITIONAL_OR_KEYWORD>), ('chart', <POSITIONAL_OR_KEYWORD>))
expected: (('self', <POSITIONAL_OR_KEYWORD>), ('chart', <POSITIONAL_OR_KEYWORD>))
match: True
```

This is annotation-format-invariant by construction (it never touches
`str(inspect.signature(...))`), so the class of bug Round 2 found — a
hand-typed constant that assumed the wrong annotation-rendering context
— cannot recur here regardless of how `Any`/`dict[str, Any]` happen to
stringify on a given Python patch version. The separate, stricter
`_EXPECTED_SOURCE_SHA256` check is untouched and still catches any other
change (including pure annotation/cosmetic edits) via the exact source
hash — a sound division of labor: the parameter check asks "is this
still a valid call," the hash check asks "did anything change at all."
Also confirmed the fix's downstream references are consistent, not just
the code block itself: §8.5 step 4 (`:870-871`) now names
`_EXPECTED_PARAMS`/`_EXPECTED_SOURCE_SHA256` (no orphaned
`_EXPECTED_SIGNATURE` reference anywhere outside the historical §12 log
entries, checked via `grep -n "_EXPECTED_SIGNATURE\|_EXPECTED_PARAMS" pip-wheel-spec.md`),
and it now explicitly recommends deriving both constants by running
`_assert_wrappable`'s own logic against the pinned source rather than
hand-typing them — directly closing the Round 2 residual-risk note about
hand-typed constants being error-prone. §8's test-coverage paragraph
(`:790-793`) goes further than I asked for: it now requires a test that
runs `_assert_wrappable` against the **real, imported stock method**
first and asserts it passes cleanly, specifically framed as catching "a
wrong pinned constant immediately rather than at review time" — this is
exactly the residual risk I flagged in Round 2 ("no test exercises
`_assert_wrappable` against real stock code"), closed without being
asked twice.

**Finding B (PR 3 acceptance criterion) — resolved.** `pip-wheel-spec.md:926`
now reads: "with `OWNERSHIP_ENABLED=true` and a deliberately-broken
`_EXPECTED_SOURCE_SHA256` (simulating drift — the wrap is only ever
attempted with the switch on, §3.3's early-return gate means
`OWNERSHIP_ENABLED=false` never reaches `_assert_wrappable` at all and
proves nothing about the degrade path), `superset ownership check`
**boots and exits 1**, its output naming
`dashboard_chart_patch_installed: false`, while a web/worker boot under
the same broken constant **crashes `create_app()`** instead." Traced
this against `install()`/`_degrade_on_failure()` (`:298-354`): a broken
`_EXPECTED_SOURCE_SHA256` passes the (now-correct) parameter check but
fails the hash check, raising `DashboardPatchError`; for `ownership
check`'s `sys.argv` shape `_degrade_on_failure()` returns `True` →
`install()` logs and returns without patching → `check_consistency()`
reports `dashboard_chart_patch_installed: false` → `cli.py:178-179`'s
existing "exits 1 on any `false`" rule fires, i.e. exit 1, not a crash.
For web/worker, `_degrade_on_failure()` returns `False` → the exception
propagates out of `install()` uncaught → `create_app()` crashes. Both
halves of the sentence are accurate descriptions of what the code
actually does; the criterion now tests the real invariant instead of a
vacuous one.

**Nothing new broke.** Scanned the rest of the diff surface the two
fixes touch (§3.3 in full, §8's test-coverage paragraph, §8.5 step 4,
§9 risk #1's summary line, §12's new log entries) for internal
consistency — no dangling references to the retired
`_EXPECTED_SIGNATURE` name, no reintroduced string-signature comparison
anywhere, and the idempotency check in `install()`
(`getattr(original, "_superset_ownership_patched", False)`, still
ordered *before* `_assert_wrappable` is ever called on a second run) is
unchanged and still correctly prevents the new parameter check from ever
being evaluated against an already-patched (and therefore
intentionally-different-shaped, keyword-only-argument-bearing) function.

### Residual risks to track during implementation

These are the same three items Round 2 flagged as open, still open —
none is a spec defect at this point, each is either an explicit,
correctly-labeled unknown or an execution-time discipline the spec now
directs but implementation still has to actually follow:

- **§7.4's whole-file-replace argument is still explicitly unverified**
  against a real upstream conflict (`pip-wheel-spec.md:748-755`,
  unchanged since Round 1's retraction) — track this at the first real
  `scripts/fetch-pcs.sh` version bump, not before.
- **Whether Ivanti's FIPS build ships `.py` sources for
  `dashboards/api.py`** is still not in §11's open-questions list, even
  though `_assert_wrappable`'s `OSError` fallback (`:305-318`) exists
  specifically for that scenario — worth adding as an explicit Q5
  alongside the Python-3.11-floor question, so it's tracked with the
  same visibility rather than living only in a code comment.
- **§8.5 step 4 now *recommends* deriving `_EXPECTED_PARAMS`/
  `_EXPECTED_SOURCE_SHA256` mechanically instead of hand-typing them, but
  nothing enforces it** beyond the new "test against real stock code"
  requirement in §8 — that test catches a bad constant, but doesn't stop
  someone from hand-typing a replacement the same way this document's
  own first draft did. Worth turning the recommendation into the actual
  mechanism (a one-off script or `make` target that emits both constants
  from a pinned checkout) rather than prose guidance, the first time
  §8.5 is executed for real.

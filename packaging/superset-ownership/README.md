# superset-ownership

Object ownership and sharing for Apache Superset / Preset PCS (PCS-10243),
authorized by OpenFGA. Installs as one wheel on top of a stock PCS image and
wires itself in through Superset's documented config hooks; it never modifies
Superset's own schema or source files.

Install into the PCS virtualenv (the stock venv ships `uv`, not `pip`):

    uv pip install --python /app/.venv/bin/python --no-deps superset_ownership-<version>-py3-none-any.whl

Then, as the last line of your `superset_config.py`:

    from superset_ownership.configure import configure; configure(globals())

(The delivery shell also drops a one-line `superset_config_ownership` shim on
`PYTHONPATH` that re-exports `configure` under that older spelling; the wheel
itself does not contain it.)

Run `superset ownership check` after boot to confirm the install. Full
deployment guide: `docs/pip-wheel-spec.md` in the delivery repository.

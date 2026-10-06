# Testing and compatibility evidence

CI uses host Python 3.13 with three isolated Django constraints: `>=5.2,<5.3`, `>=6.0,<6.1`, and `>=6.1,<6.2`. It builds one wheel, installs that wheel with the row's Django plus test/Workers tooling into a fresh environment, asserts the resolved Django version is inside the exact row constraint, and runs the contract suite. An explicit constraint mismatch fails the assertion; execution must not sync or re-resolve from `uv.lock`.

The Worker consumer uses `workers-py` (the package providing the `pywrangler` CLI), `workers-runtime-sdk`, local Wrangler/workerd KV emulation, and the same Django constraint. Never install the unrelated PyPI placeholder named `pywrangler`. The matrix contract command filters out the 12 Worker-dependent tests; a separate `tests/integration` invocation runs the 16 WSGI, native async, runtime-gate, and local-binding tests for each row. Local emulator results prove only the exercised local runtime behavior, not deployed service or worldwide consistency.

The Worker runtime's observed Python is 3.14.2 with Pyodide 314.0.6. This is not the host Python 3.13 matrix. Tooling baseline recorded for the current environment: uv 0.12.22, Node 24.16.0, Wrangler 4.147.0, `workers-py` 1.17.6, and `workers-runtime-sdk` 1.9.2.

Run the project checks from the project environment:

```sh
rtk uv run ruff check src tests
rtk uv run ruff format --check src tests
rtk uv run basedpyright
rtk uv run pytest tests --ignore=tests/integration -q
rtk uv run pytest tests/test_package.py -q
rtk uv build
```

Runtime checks use a built wheel, disposable consumer environments, isolated local persistence and ports, and actual local HTTP requests. `compatibility-matrix.json` records each row's environment, resolved versions, commands, exit statuses, collected/passed counts, and integration results. Do not infer a pass for a row absent from that evidence.

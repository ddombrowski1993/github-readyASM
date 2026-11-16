# Targeted BET import-chain repair — November 16, 2026

## Scope and current verification boundary

The reported Streamlit traceback stops before the final exception message. No
signed-in Streamlit Cloud log connection is available in this workspace. The
exact **production** exception text and missing symbol are therefore **not
verified**. No assumed missing symbol is presented as a production finding.

The source audit and local import tests below are verified. Live Map Center
recovery still needs a deployment/reboot and check in the owner session.

## Required source audit

| Symbol | Exists | Canonical file | Exact class name | Importable from src.models |
|---|---|---|---|---|
| BetServiceCompletion | Yes | src/models.py | BetServiceCompletion | Yes |
| BetServiceExpectation | Yes | src/models.py | BetServiceExpectation | Yes |
| AuditLog | Yes | src/models.py | AuditLog | Yes |
| Employee | Yes | src/models.py | Employee | Yes |
| MapArea | Yes | src/models.py | MapArea | Yes |
| Store | Yes | src/models.py | Store | Yes |
| Team | Yes | src/models.py | Team | Yes |

Both BET classes were committed during the original feature. Before this repair,
local `src/models.py`, `src/bet_services.py`, `src/bet_service_map.py`, and
`src/bet_service_store.py` matched the GitHub `main` files byte-for-byte:

- models: `afbd3d802139385d839bfc36f5e27f8343555e22`
- services: `2251bbd2f526271aeeec13deb0f23f89d42e8ce6`
- map: `aa750b5e8fe77f0cb1c0ab91c3403d58b985229e`
- adapter: `7209b75d0fa86780b323f203ea26dc0dfa5d4a95`

No missing/uncommitted model definition, alternate model location, class-name
mismatch, or circular model/business import was found. `src.models` imports only
SQLAlchemy and datetime, not BET business or UI modules. Neither model was changed
or duplicated for this repair. The expectation model is actually used for
per-store/service/year applicability reads and completion validation; it was not
removed.

## Confirmed wiring problem and exact changes

Map Center imported display helpers from `src.bet_service_map`. That module also
imported service business logic, reporting, and entry UI at module scope. The
entry UI further imported the workspace/database adapter. This unnecessarily made
an existing map depend on every optional BET tracker/ORM import at page startup.

- Added `src/bet_service_types.py` as the single dependency-free source of the
  unchanged service identifiers/labels.
- `src.bet_services` imports/re-exports that same `SERVICES` object. All canonical
  model imports remain from their real location, `src.models`.
- `src.bet_service_map` imports only the service identifiers at module scope.
  The tracker reader/report/form imports execute inside `render_service_map` when
  that tracker is actually rendered. Marker colors, halos, geometry, and click
  functions are unchanged.
- The existing optional halo-loader `try` block in Map Center now includes the
  optional imports. On failure it logs the complete exception, shows a visible
  error, displays exact ImportError text, and clears partial overlay state. It
  does not suppress the failure with `except: pass`.

This isolation is not a claim that the unavailable production exception has
been diagnosed. Tests demonstrate that an intentionally stale already-loaded
model module can reproduce a missing-model ImportError; they also demonstrate
that this condition no longer prevents the existing map helpers from importing.
A fresh deployed Python process must still load the complete committed models.
No runtime SQLAlchemy model reload, class alias, or duplicate definition was added.

Checkpoint: `611b08a`, branch
`checkpoint-before-bet-import-fix-20261116`.

## Read-only PostgreSQL inspection

A direct read-only transaction queried `information_schema.columns` and
`pg_catalog.pg_class` for `bet_service_completions` and
`bet_service_expectations`. **Neither table exists in the database inspected**;
there are no existing columns to remap. No initializer was invoked. No tables
were created, dropped, recreated, reset, or migrated. No PostgreSQL completion,
assignment, or schedule records were changed. Production connection credentials
were not printed or committed.

## Test results

All three requested direct import checks passed in fresh Python processes:

```text
models ok
services ok
map helpers ok
```

Tests also forbid `Engine.connect` and `sqlalchemy.create_engine` during import,
confirming that model/module import performs no database initialization.

Final local suite: **46 passed, 27 skipped**. The skips comprise PostgreSQL
parametrizations without a configured disposable PostgreSQL server and the
SQLite row-lock concurrency limitation. This repair did not start a test
PostgreSQL server or run schema-creating tests against PostgreSQL.

Additional import/renderer checks prove:

- Map display helpers do not import models, business services, reporting,
  workspace storage, or entry UI.
- An intentionally stale model module cannot break map-helper imports.
- Optional-layer failure exposes the exact ImportError and leaves no partial
  overlay data.
- Map Center's complete import/function-definition section executes with
  database access forbidden. Normal data loading is stopped for this smoke test;
  this is **not** a claimed live-production page check.
- The actual existing renderer retains store dots and existing polygons in
  PMT, Brand Enhancement, and Calibration views.
- Search/status and map-entry regression tests pass using safe local fixtures.
  Opening a service map with existing status does not call save/correction or
  connect to a database.
- The existing map renderer, manual assignment editor, and ownership-writing
  functions were compared with the checkpoint and remain unchanged.
- Model definitions, database initialization, PMT Scheduler, Calibration,
  Landscaping, scheduling, and completion business behavior are unchanged.
- SQL safety and whitespace checks pass.

## Files changed

- `src/bet_service_types.py`
- `src/bet_services.py` (constant import only)
- `src/bet_service_map.py` (dependency placement only)
- `pages/4_Map_Center.py` (optional import/error boundary only)
- `tests/test_bet_import_chain.py`
- `docs/BET_IMPORT_FIX.md`

After pushing the repair, reboot the correct app at
`https://7-11-ss.streamlit.app` to load a fresh Python process. Verify the existing
maps first, then enable BET completion separately. If an import still fails, the
optional layer displays its actual ImportError and records the full traceback in
private Cloud logs; obtain that exact line before diagnosing a missing symbol.

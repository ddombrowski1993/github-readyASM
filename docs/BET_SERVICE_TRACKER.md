# BET Service Completion Tracker

The tracker adds work history to the existing Brand Enhancement assignment model.
`stores.assigned_brand_team_id` and `stores.assigned_brand_employee_id` remain the
source of territory ownership. The tracker never writes either assignment, and
never writes PMT, Calibration, MapArea, or schedule records.

## Where to use it

Open **BET Service Tracker** in the sidebar or the Service Completion link from
Brand Enhancement in Areas and Maps. Its four tabs are **Overview**, **Log Service
Completion**, **Service Map**, and **Completed Stores / History**. Links lead back
to existing BET Map / Areas and BET Scheduling / Routes. No second scheduler or
territory model was added.

Choose **Service Year**, search a store number from the master Stores list,
review its current technician/team, check Power Washing and/or Lot Striping, and
Save. Existing completions load checked and cannot be silently unchecked.
Map selection opens the same entry form. Optional details include notes and a
work order number; save the services separately for different work orders.

The default completion date is today in **America/New_York**, the region already
used by the app's weather integration, rather than the server's UTC date. Earlier
dates can be entered. Dates must be within the selected service year and cannot
be in the future. The entered timestamp is stored separately as UTC with timezone.

Current team ownership is used for credit. If the store has no explicit Brand
employee, the adapter resolves an unambiguous employee from existing Brand
MapArea/employee-team relationships. If no unique employee exists, credit remains
team-based rather than guessing. Admins/managers may override credit with a reason.
Actual authenticated identity is recorded as Entered By during impersonation.

## Persistence and safe correction

`BetServiceCompletion` / `bet_service_completions` stores store ID/number,
`power_washing` or `lot_striping`, service year, completion date/timestamp,
historical employee/team IDs **and names**, entered-by ID/name, source, optional
work order/notes, active flag, created/updated timestamps, and void author/time/reason.

`BetServiceExpectation` / `bet_service_expectations` holds optional per-store,
service, and year applicability overrides. Version 1 defaults both services to
applicable for current active Standard/field-service BET-assigned stores. Other
store types or unassigned stores are not included as BET workload by default.
The applicability model is ready for power-wash-only or striping-only stores;
there is no applicability administration screen in Version 1.

Both tables live in the app's existing effective account PostgreSQL schema.
BET tables are excluded from runtime schema initialization. Data loading validates
that both tables and their expected columns exist and reports a migration error
without creating tables. Production keeps the existing PostgreSQL configuration;
authentication and workspace selection are unchanged.

Run the controlled migration for each existing effective workspace schema using
the deployed database configuration (replace `workspace_schema`):

```bash
python scripts/migrate_bet_services.py --schema workspace_schema --apply
python scripts/migrate_bet_services.py --schema workspace_schema
```

The command defaults to a read-only check. `--apply` creates only missing BET
tables in one transaction under a PostgreSQL advisory lock. Repeated execution
preserves existing tables and completion data. It never creates workspace schemas,
drops/recreates tables, seeds rows, or changes assignments. Existing tables missing
required columns produce an error for explicit review rather than destructive repair.

The canonical classes are in `src/bet_models.py`, sharing the SQLAlchemy registry
in `src/model_base.py`. `src.models` re-exports the identical classes for compatibility.
The service and schema adapter import BET classes from their canonical module.

The shared core writer is `save_bet_service_completion` in `src/bet_services.py`.
`save_store_services` in `src/bet_service_store.py` provides the authenticated,
workspace-aware transaction. Store search and map entry both use
`render_completion_form` and that same writer. A checked service adds only a new
completion; unchecked services never remove history. Repeated saves leave
existing dates, credit, notes, and work orders unchanged.

A master-store row lock serializes concurrent saves/corrections in PostgreSQL.
A partial unique index enforces one active `(store_id, service_type, service_year)`
record. `void_bet_service_completion` deactivates a confirmed entry, retaining its
original work details and adding correction author, timestamp and reason. The
entry author or an admin/manager can void. Re-log corrected date or credit as a
new record. Both writes also add an audit-log entry in the same transaction.
BET writes deliberately do not use the generic whole-table undo mechanism.
Historical store/employee/team IDs are snapshots, so master-record changes or
legacy territory undo do not delete the work history.

The shared core reader is `read_bet_service_data`, with the workspace adapter
`load_bet_services`. It reads stores, existing employees/teams/Brand areas,
completions and applicability in six bulk queries, independent of marker count.
Map rendering performs no database queries per store. Reads fail visibly instead
of returning fake zero-completion data on a connection error.

## Progress, historical credit, and maps

Current territory progress counts current applicable responsibilities and active
cycle completions, whoever performed the work. Historical productivity counts the
snapshot worker/team; reassignment never changes that credit. Team and technician
views are both available. Applicable-service denominators exclude inapplicable
work, without deleting completed work from history. Store counts and service
counts are separate everywhere.

Expand a team/technician row to see the stores behind Power Washing, Lot Striping,
both, or remaining counts. History has service, status, store, current owner,
historical worker, and date filters, with CSV exports and voided entries included
by default. Today/This Week/This Month/Current BET Cycle/Custom Date Range reports
are scoped to the selected service year. A compact dashboard expander shows
current-year progress and productivity, with a link to the detailed tracker.

The Service Map reads existing Brand area geometry without creating new areas.
Its inner marker uses the **same assignment-state color helper as Areas & Maps**:
assigned team red, selected team green, no area gray. No new territory-color system
was invented. Existing previews/selection styles are unchanged.

The separate service ring is absent with zero completions, yellow with exactly
one service, green with both. Individual Power Washing / Lot Striping modes use a
green ring for that service alone. Assignment and completion legends are separate.
Popups include current ownership, status, date and historical credit. Halos are
non-interactive so clicks reach the original marker. Search or map-click entry
uses the same save function. Areas & Maps also has an optional BET-only service
halo toggle and completion form. The existing inner-marker expression and
ownership-editing functions are retained. PMT and Calibration ignore the overlay.

## Acceptance and regression verification

Tests use seeded Jason / Store 45555 fixtures, never production stores:

1. Power Washing alone retains Jason as owner, leaves striping incomplete,
   creates one record and shows a yellow halo.
2. Later Lot Striping adds its own date/work order, turns the halo green, and
   results in two services across one fully completed store.
3. Reopening shows checked statuses; repeated and concurrent saves do not add
   duplicate active records.
4. Reassignment to another BET changes remaining responsibility while historical
   Jason credit and names survive employee/team renaming.
5. Unconfirmed correction is rejected. Confirmed correction preserves the voided
   entry/reason and updates active counts; re-completion adds a separate record.
6. Committed data survives new engines/connections. A separate manual automated
   check also restarted the isolated PostgreSQL server and read the same owner,
   PW=1, LS=1, both=1, total services=2 from a new Python process.
7. Ten-store acceptance: Power Washing=7, Lot Striping=5, both=4, total services=12,
   remaining applicable services=8.
8. Daily acceptance: Power Washing=4, striping=3, total services=7, distinct stores=5.
9. Cycle rollover retains old history and starts the new year incomplete.
10. Applicability overrides change denominators and do not erase historical work.
11. Manager overrides require authorization and reason, with ownership unchanged.
12. Query-count checks use the same six reads for 10 and 110 stores.
13. Streamlit AppTest exercises search, checkboxes, Save, preloaded statuses,
    confirmed correction, map entry, drilldowns, dashboard counts and all four tabs.
14. The actual Areas & Maps renderer is tested with halos on/off: inner marker
    options are identical, and PMT/Calibration receive no BET rings.
15. Completed/past schedule rows do not produce service-completion records.
16. Authenticated Entered By identity is retained during impersonation; missing
    hosted workspace context is rejected before engine access.
17. An invalid two-service save rolls back both; deleted master stores retain
    their historical completion snapshot.
18. Existing account regression tests remain in the suite. SQL safety, all-source
    Python syntax, and whitespace checks are also run.

Run SQLite/UI tests:

```bash
python -m pytest tests -q
```

Run both SQLite and real PostgreSQL tests against a **disposable test database**:

```bash
BET_TEST_POSTGRES_URL=postgresql+psycopg2://user:password@localhost/test_database python -m pytest tests -q
```

Tests create and remove isolated schemas with random `bet_test_` names. CI has a
PostgreSQL 16 service and runs the same suite. The sole expected skip when a test
PostgreSQL server is configured is SQLite's row-lock concurrency test; that test
runs on PostgreSQL.

## Files

Added: `src/bet_services.py`, `src/bet_service_store.py`,
`src/bet_service_ui.py`, `src/bet_service_reporting.py`,
`src/bet_service_map.py`, `src/bet_service_dashboard.py`,
`pages/21_BET_Service_Tracker.py`, `tests/test_bet_services.py`,
`tests/test_bet_map_regression.py`, and this document.

Updated: `src/models.py` (additive tables), `src/utils.py` (sidebar link),
`pages/4_Map_Center.py` (BET-only links/optional completion layer), `app.py`
(compact dashboard section), `.github/workflows/predeploy-checks.yml` (test job),
and `README.md` (feature entry).

## Final local verification — November 16, 2026

- Full suite: **65 passed, 1 expected SQLite-only concurrency skip**, with real
  PostgreSQL 16 enabled. PostgreSQL concurrency itself passed.
- Full PostgreSQL server restart + new Python process: **passed**, Jason ownership
  retained, power washing=1, lot striping=1, both-complete stores=1, total services=2.
- Python syntax: **52 files passed**.
- SQL bind safety and `git diff --check`: **passed**.
- Existing ownership-writing functions were compared against the initial Git
  checkpoint and are unchanged. PMT/Calibration scheduler source files are unchanged.
- No production writes were used for testing. Live deployment is still pending.

Checkpoint branches were created before the overall feature and before entry,
overview, map halos, and dashboard phases. Each completed phase is committed
separately for review and rollback.

## Dependency and controlled-schema repair — November 16, 2026

Checkpoint: `checkpoint-before-bet-schema-fix-20261116` (`5a6cecc`).
Both original direct imports passed before this repair; the production ImportError
was not reproduced locally. The service now imports BET models directly from
`src.bet_models`, and regression coverage removes the compatibility exports from
an already-loaded `src.models` to verify that this no longer blocks the service.
The shared base has no service, database, authentication, or page dependencies.
There is one mapping per table; model columns and constraints are unchanged.

Confirmed schema defect: normal BET data loading created tables, and three broad
runtime metadata initializers could also create them. All runtime paths now exclude
BET DDL. The explicit migration was applied twice to the six existing `fp_`
workspace schemas containing stores in the configured PostgreSQL database. Both
runs and subsequent schema checks succeeded. No application rows were inserted,
updated, or deleted; no existing tables were dropped or recreated.

Local verification: full suite **50 passed, 27 skipped** (disposable PostgreSQL
server not configured). Final targeted migration/import/page tests **12 passed,
1 skipped**. Map Center import and the full four-tab tracker passed Streamlit
AppTest with isolated fixtures. Direct model/service/map imports, SQL safety, and
whitespace checks passed. Streamlit Cloud deployment itself was not inspected.

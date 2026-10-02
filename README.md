# Aviator Intelligence Engine (AIE)

AIE is a staged research project for investigating whether legitimately available
pre-round information contains useful predictive information. The project does
not assume that exact prediction is possible.

**Current development phase: Phase 6 — Round export and verified local backups.**

Phase 1 supplied configuration, logging, project directories, and SQLite
connections. Phase 2 added a completed-round model, a versioned schema, and an
append-only repository. Phase 3 added validated local JSON imports, dry runs,
and atomic batch storage. Phase 4 summarizes stored historical records from a
read-only database snapshot and optionally exports an aggregate JSON report.
Phase 5 presents these summaries in a local browser dashboard with source and
UTC time filters, metadata coverage, and aggregate downloads.
Phase 6 exports portable completed-round JSON and creates verified SQLite
backups. Both operations are explicit local commands and preserve the live
database.
**It does not connect to Aviator services, collect live data, make predictions,
or recommend bets.** Startup never inserts sample rounds or imports files
automatically.

## Requirements

- Python 3.12 or newer, including standard-library `sqlite3` with JSON functions.
- Git for version control.
- VS Code or GitHub Codespaces, optionally with the Python extension.

Only two third-party packages are needed: `python-dotenv` for configuration and
`pytest` for tests. SQLite, pathlib, logging, typing, datetime, JSON, and Decimal
come with Python. The Phase 5 server uses standard-library HTTP support; its
interface uses plain HTML, CSS, and JavaScript. There are no new dependencies,
CDN scripts, external fonts, or external data requests.
Phase 6 uses only standard-library SQLite, JSON, hashing, and filesystem tools.

## GitHub Codespaces, Linux, or macOS installation

Open a terminal in the repository folder. In this project's Codespace, that is
`/workspaces/AVIATOR`. Use `python3` on Linux; the Windows `py` launcher is not
available there. For a new installation:

```bash
python3 --version
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

The version must be Python 3.12 or newer. In VS Code, use **Python: Select
Interpreter** and choose the interpreter inside `.venv`.

For an existing installation, keep your `.env`, database, and virtual environment.
After updating the source files, run:

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
python main.py
python -m pytest
```

## VS Code on Windows installation

Open the project folder using **File → Open Folder**, then open PowerShell:

```powershell
py -3 --version
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Choose Python 3.12 or newer. If several versions are installed, specify one,
for example `py -3.12 -m venv .venv`. If activation is blocked, use the
virtual environment's interpreter directly:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
.\.venv\Scripts\python.exe -m pytest
```

In Command Prompt, activate with `.venv\Scripts\activate.bat` and copy with
`copy .env.example .env`. Copy the example only for a new installation, so
existing configuration is preserved.

## Configuration and logging

The project root is resolved from `config/settings.py` using pathlib.
Configuration loads the `.env` in that root; existing environment variables
take precedence. Defaults work without a `.env` file.

| Variable | Default | Accepted values |
| --- | --- | --- |
| `AIE_ENVIRONMENT` | `DEVELOPMENT` | `DEVELOPMENT`, `TEST`, `PRODUCTION` |
| `AIE_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `AIE_DATABASE_FILENAME` | `aie.sqlite3` | A plain filename ending in `.db`, `.sqlite`, or `.sqlite3` |

Database files stay inside `data/database/`; the setting accepts a filename,
not a path. Startup creates missing `data/raw`, `data/processed`, and
`data/database` directories. Configuration imports create no files. Invalid
settings, inaccessible directories, or incompatible schemas cause startup to
return a nonzero exit code.

Logs contain timestamp, level, module logger name, and message. They stay on the
console; the startup log goes to standard error. Application modules use
`logging.getLogger("aie.<module>")`.

No passwords, cookies, tokens, or credentials are needed. Do not add them to
configuration, round payloads, or logs. The model recursively rejects several
common authentication field names, but this is not a complete secret detector.
Future sources must sanitize and allowlist payload fields before storage.
Database operations never log payload contents.

## Run the application

With the virtual environment activated:

```bash
python main.py
```

Expected standard output on a fresh database with default settings:

```text
Aviator Intelligence Engine
Phase: 6
Status: INITIALIZED
Database: READY
Schema version: 1
Rounds stored: 0
Environment: DEVELOPMENT
```

The count reflects stored records on later runs. **Development phase 6 keeps
database schema version 1**, introduced in Phase 2. Existing Phase 2 records
are preserved. Startup checks SQLite, initializes or verifies the schema,
reports the count, and closes its connection. It does not import data.

## Phase 3: import a local JSON file

Phase 3 supplies an AIE file format and an explicit import command. It accepts
completed-round records from a local file supplied by the user. This is an
internal application format; no external provider's export format or API is
assumed. Map an authorized export into it using accurate source information.
Do not guess missing IDs, timestamps, results, or observation times.

The repository includes `examples/rounds.empty.json`, containing no game data:

```json
{
  "format_version": 1,
  "rounds": []
}
```

Validate this empty template without opening or changing the database:

```bash
python import_rounds.py examples/rounds.empty.json --dry-run
```

Expected output begins:

```text
Aviator Intelligence Engine
Phase: 6
Import: VALIDATED
Rows validated: 0
Database check: NOT RUN
Database changes: NONE
```

An `Input SHA256` line identifies the exact input bytes. This fingerprint is
not proof that the source data is genuine and does not verify a game's result.

Place your actual input file at `data/raw/rounds.json` or another local path.
Each object in the `rounds` array must supply these five fields:

| Field | JSON type and requirement |
| --- | --- |
| `source` | Nonempty string identifying the actual source |
| `round_id` | Nonempty string containing the source's round identifier |
| `timestamp` | ISO 8601 string with a timezone, describing result completion |
| `collection_timestamp` | ISO 8601 string with a timezone, at or after result completion |
| `multiplier` | Decimal **string**, finite and at least 1; JSON numbers are rejected here |

Optional fields are `started_at`, `pre_round_observed_at`, `pre_round_data`,
`post_round_data`, and `raw_data`, as defined in the round model below. Optional
times may be omitted or `null`. Payloads must be JSON objects. Unknown fields
are rejected, so do not include database-assigned `id` or `ingested_at` fields.

Input must be UTF-8 JSON, optionally with a UTF-8 BOM, with a `.json` extension.
The top-level object must contain exactly `format_version` (integer 1) and
`rounds` (an array). Limits are **10 MiB per file** and **10,000 records**.
Duplicate object keys, nonfinite JSON numbers, invalid timestamps, unsupported
versions, and invalid records are rejected before opening the database.
Use strings for precision-sensitive payload values: ordinary JSON payload
numbers are decoded into Python numeric types, which may lose decimal precision.
The original input file is left unchanged; it is not automatically copied or archived.

Validate a real file first, then explicitly import it:

```bash
python import_rounds.py data/raw/rounds.json --dry-run
python import_rounds.py data/raw/rounds.json
```

Dry runs check file format and record values only. They do not query the
database or check duplicate identities, including conflicts within a file.
Passing a dry run therefore does not guarantee that the database will accept
the import. Real imports initialize or verify the schema, then insert the whole
batch in one transaction. Exact replays are skipped and counted. A conflicting
duplicate or failed insert rolls back every new round in that batch; existing
records remain unchanged. An empty import adds zero rounds.

Successful imports print `Rows read`, `Inserted`, `Duplicates`, and the input
fingerprint. Diagnostics print fixed error codes and, where applicable, a
1-based round number; they do not echo file paths, source identifiers, or payload
values. No persistent import-report table or report file is created in this
phase. The command returns 0 on success, 1 on an import failure, and 2 for an
invalid command-line usage. Use `python import_rounds.py --help` for usage.

## Phase 4: descriptive analysis

Initialize the application first with `python main.py`, then analyze the stored
records without creating a report file:

```bash
python analyze_rounds.py
```

Analyze and save an aggregate JSON report under `data/processed`:

```bash
python analyze_rounds.py --output analysis.json
```

For an empty database, this is successful and prints:

```text
Aviator Intelligence Engine
Phase: 6
Analysis: NO_DATA
Database rounds: 0
Selected rounds: 0
Sources: 0
No rounds matched this selection.
Report: SAVED
```

The report is `data/processed/analysis.json`. Unavailable statistics and
percentages are JSON `null`, not fabricated zero values. Bucket counts are
zero. A filtered selection can also return `NO_DATA` even when other records
exist in the database.

Optional `--source`, `--start`, and `--end` arguments restrict the selection.
The source must match a stored source identifier. Times must be ISO 8601 strings
with explicit timezones; they are normalized to UTC. `--start` is inclusive and
`--end` is exclusive. For example:

```bash
python analyze_rounds.py --start 2026-10-01T00:00:00Z --end 2026-10-02T00:00:00Z
```

There is no pagination or silent sampling: every selected record is analyzed.
Selections above **100,000 rounds** are refused; narrow the source or time
interval. Source names and dates are bound as SQL parameters. Use
`python analyze_rounds.py --help` for all options.

| Report content | Definition |
| --- | --- |
| Counts and scope | Total database records, selected records, selected source count, filters, and UTC generation time |
| Result range | First and last selected result-completion times |
| Multiplier summary | Minimum, maximum, median, and mean of selected historical multipliers |
| Historical buckets | `[1, 2)`, `[2, 5)`, `[5, 10)`, and `[10, infinity)` counts and percentages |
| Metadata coverage | Counts with known start, pre-round observation time, and nonempty pre/post/raw payloads |
| Repeated result timestamps | Extra records beyond the first at the same source and result time |
| Collection delays | Minimum, maximum, median, and mean seconds between result completion and completed-event collection |

Multiplier calculations use `Decimal`, never SQLite floating-point casts or
text ordering. The arithmetic context uses 600 significant digits, sufficient
for the supported stored values and selection limit. Extrema and medians retain
their exact decimal values. Means are rounded to **six decimal places** and
percentages to **two**, using `ROUND_HALF_EVEN`. JSON stores decimal values as
strings and trims trailing fractional zeros. Individually rounded percentages
may not sum to exactly 100. The original stored multipliers remain unchanged.

Analysis opens an existing database in SQLite read-only mode and verifies the
managed schema without migrating it. Counts and values come from one read
transaction, so concurrent imports cannot produce a mixed snapshot. Missing,
uninitialized, or incompatible databases cause a nonzero exit code; analysis
does not create a database. Invalid selected stored values or timing cause
failure rather than silent skipping. The checks apply to the selected records.
Normal application startup retains its existing schema-initialization behavior.

JSON reports contain aggregates and selection metadata, with report version 1
and database schema version 1. They exclude round IDs, raw payload values,
cookies, and authentication fields. Publication uses a flushed temporary file
and an atomic operation in the same directory. Existing reports are preserved
by default, including a target created concurrently. Choose a new filename or
explicitly allow replacement:

```bash
python analyze_rounds.py --output analysis.json --overwrite
```

`--output` accepts a plain `.json` filename, not a path. `--overwrite` requires
`--output`. Report export is the only analysis operation that creates files.
Reports are local generated data and are excluded by `.gitignore`.

Metadata absence is not automatically a data error: these fields are optional.
Repeated timestamps can reflect limited timestamp precision and do not prove
duplicate rounds. Observed bucket percentages describe this stored sample;
they are not forecasts or betting signals. Analysis cannot establish source
authenticity, prove complete capture, or verify the asserted pre-round timing.

## Phase 5: local dashboard

Initialize the application, then start the dashboard:

```bash
python main.py
python run_dashboard.py
```

Expected dashboard startup output:

```text
Aviator Intelligence Engine
Phase: 6
Dashboard: RUNNING
URL: http://127.0.0.1:8000
Press Ctrl+C to stop.
```

On your own computer, open `http://127.0.0.1:8000` in your browser. In GitHub
Codespaces, click the terminal URL, or open **PORTS**, add **8000** if it is not
listed, and choose **Open in Browser** for that port. Keep **Port Visibility**
set to **Private**. Use the standalone browser tab. The server listens only on
loopback; Codespaces supplies the forwarded connection. Port-forwarding guide:
[GitHub Docs](https://docs.github.com/en/codespaces/developing-in-a-codespace/forwarding-ports-in-your-codespace).

Keep the terminal running while viewing the dashboard. **Ctrl+C** stops it;
use another terminal for imports, tests, or Git commands. For a different port,
run `python run_dashboard.py --port 8001` and forward port 8001 instead.

The interface displays:

- Selected and total record counts, mean, median, and minimum/maximum multipliers.
- Four historical multiplier ranges with actual counts and percentages.
- Optional metadata coverage, mean collection delay, and repeated result timestamps.
- First/last completion times, represented source count, schema, and environment.
- Actual project status: local import available, live collection not connected,
  and prediction engine not implemented.
  Phase 6 also lists round export and database backups as available via CLI.

Enter an exact source name, or leave it blank for all sources. Suggestions are
limited to the first 1,000 stored source names; other names can be typed manually.
**From** and **Until** controls explicitly use **UTC**, regardless of the
browser's timezone. Start is inclusive and end is exclusive. Click **Apply
filters** to update the selection, **Reset** to clear the filters, or **Refresh**
to read newly imported records with the currently applied filters. This phase
does not poll or collect live data.

**Download JSON** saves the aggregate report currently displayed to your
browser's download folder. It preserves decimal strings and includes the
selection filters and generation time; no round IDs or payload values are
included. A valid empty selection can be downloaded as a `NO_DATA` report.
Downloads create no report file on the server and never alter the database.

The dashboard opens the existing database in SQLite read-only mode and reuses
Phase 4's snapshot analysis and 100,000-selected-round limit. It performs no
schema initialization or migration. On a missing database, run `python main.py`
and refresh. Invalid filters, unreadable/incompatible databases, invalid stored
records, and oversized selections show an error and clear the prior report.
A fresh zero-round database displays zero counts and unavailable statistics;
no demonstration rounds are inserted.

This is a local development dashboard using Python's HTTP server, not a
production hosting service. It has no application login or authorization system;
private Codespaces forwarding provides access control in that environment.
There are no import, edit, delete, betting, or prediction controls in the UI.
The layout adapts to smaller screens and includes keyboard labels, a skip link,
and progress labels.

For development, the server serves only allowlisted static assets and these
read-only application endpoints:

| Endpoint | Response |
| --- | --- |
| `GET /api/status` | Schema, environment, bounded source suggestions, and implemented-feature status |
| `GET /api/summary` | Aggregate Phase 4 report using optional `source`, `start`, and `end` filters |
| `GET /api/report` | The same aggregate format with an attachment header; each request reads a fresh snapshot |

API times must include a timezone. Unknown/repeated filters and mutating HTTP
methods are refused. Asset paths cannot expose arbitrary project files. Host
and browser-origin checks support localhost and this Codespace's configured
forwarded domain. Cookies, authorization headers, request targets, and payloads
are not logged. Metadata suggestions and the analysis report use separate read
transactions; all report counts and statistics share one snapshot.

## Phase 6: export completed-round data

Initialize with `python main.py`, then export stored rounds:

```bash
python export_rounds.py --output rounds-phase6.json
```

The file is `data/processed/rounds-phase6.json`. It uses the same AIE
`format_version: 1` format accepted by `import_rounds.py`, with only
`format_version` and `rounds` at the top level. A zero-round database produces a
valid empty array and reports `Rounds exported: 0`; no records are fabricated.

Exports retain source-scoped round IDs, exact decimal multiplier strings, all
completion/collection/optional observation times, and complete stored pre/post/raw
payloads. These are round-data exports, unlike the dashboard's aggregate JSON
download. Database-assigned IDs and ingestion times are excluded from the
portable format; importing into another database assigns new ones. Use a SQLite
backup when those database details must also be preserved.

The whole selection is read in one read-only transaction and ordered by
completion time, source, and round ID. Source and time filters work as in analysis:

```bash
python export_rounds.py --start 2026-10-01T00:00:00Z --end 2026-10-02T00:00:00Z --output selected-rounds.json
```

`--source` selects one exact source; start is inclusive, end is exclusive, and
times must include a timezone. The command refuses selections above **10,000
rounds** or output above **10 MiB**, matching the existing importer. Narrow the
filters to export smaller selections. There is no silent truncation, automatic
splitting, or record omission. Whole-database backups have no import-format
row-count or file-size limit.

Complete stored fields are validated through the existing round model. Invalid
or noncanonical records are refused, including duplicate JSON keys, authentication
fields caught by the model, invalid timing, or values whose normalization would
alter an exact replay. Export failures leave the live database and existing
outputs unchanged. The validation is not a complete secret detector; the payload
sanitization requirements above still apply.

Files are fully written to a private temporary file, flushed, then atomically
published. Existing outputs are preserved by default. To deliberately replace
an export:

```bash
python export_rounds.py --output rounds-phase6.json --overwrite
```

`--output` accepts a plain JSON filename, not a path. Successful commands print
the row counts, local output file, byte count, and SHA256 of the exact export
bytes. These fingerprints identify a file; they do not authenticate the source
or verify a game's result. `python export_rounds.py --help` lists all options.

The exported file can be validated using the existing importer:

```bash
python import_rounds.py data/processed/rounds-phase6.json --dry-run
```

An actual re-import into the unchanged original database counts exact replays
as duplicates and inserts nothing. Source/round-ID conflicts still fail and
roll back the entire batch. Dry runs continue to check the file only, without
checking database duplicates or conflicts.

## Phase 6: create and verify a database backup

Create a named backup and verify it again:

```bash
python backup_database.py --output phase6.sqlite3
python backup_database.py --verify phase6.sqlite3
```

The file is `data/database/backups/phase6.sqlite3`. Backup paths come from the
configuration's `Settings.backup_dir` property. The directory is created only
by an explicit backup attempt after the source schema is verified. Normal
startup does not create backups or this extra directory.

For a new timestamped filename on each run:

```bash
python backup_database.py
```

Generated names contain a UTC timestamp including microseconds. Backups
**never overwrite existing files**, including a concurrent target or symbolic
link. Choose a different name if the requested one exists. The command does not
offer an overwrite or restore option.

The source is opened in SQLite read-only mode. A pinned read transaction gives
the source count and SQLite backup API the same snapshot. The resulting copy
retains round data, database-assigned IDs, ingestion times, migration history,
indexes, and append-only triggers. It is made standalone in DELETE journal mode
so the published file needs no accompanying WAL or journal file.

Before publication, verification checks the managed schema, SQLite integrity
and foreign keys, all stored round fields, canonical database metadata, and the
source-snapshot count. Model checks are explicit because read-only integrity
checking alone did not flag an invalid multiplier in the tested SQLite runtime.
Invalid data is rejected without changing or repairing the source. Verification
does not establish the authenticity of supplied round data or asserted timing.

Copy progress, SQL work, record validation, and hashing check a 30-second
processing budget. Contention or failed verification aborts publication;
temporary database and journal files are cleaned up. The live database is never
replaced. Snapshot reads may delay a writer in rollback-journal mode; WAL-mode
concurrent insert checks confirm that newer commits are excluded consistently
from the already pinned snapshot.

Successful output begins:

```text
Aviator Intelligence Engine
Phase: 6
Backup: VERIFIED
Mode: CREATED
Schema version: 1
Rounds backed up: 0
File: data/database/backups/phase6.sqlite3
```

The round count reflects the copied snapshot. Byte count, SHA256, and
`Database changes: NONE` follow. `--verify` reports `Mode: VERIFY_ONLY` and opens
the named backup read-only; it works even when the live database is missing.
Only regular files inside the configured backup directory are accepted, with a
plain `.db`, `.sqlite`, or `.sqlite3` filename. Existing WAL-mode files are
refused as non-standalone backups.

You can compare against a previously recorded SHA256 with
`--verify FILENAME --expect-sha256 DIGEST`. The digest must contain 64 hexadecimal
characters; a mismatch fails verification. Without an expected digest, the
command checks structure and data and reports the current file fingerprint.
A digest is not a signature or proof of source authenticity. Use
`python backup_database.py --help` for usage.

These are **local backups in the same workspace**. For an independent copy,
download the backup file from the Codespace Explorer to another storage location.
Exports and backups are ignored by Git and can contain the complete stored
payloads. No `.env`, raw input files, or separate processed reports
are copied into the SQLite backup. Only the database contents are included.

The dashboard remains read-only: it lists export and backup availability, but
file creation is performed only by these command-line actions. Both commands
return 0 on success, 1 on an operation failure, and 2 for invalid CLI usage.

## Database migration and preservation

The default file is `data/database/aie.sqlite3`. First startup upgrades an empty
Phase 1 database in one transaction. It creates the `rounds` table,
`schema_migrations` history, chronological indexes, and triggers rejecting
updates and deletes of stored rounds. `PRAGMA user_version` tracks the schema
version alongside the migration history.

Initialization is idempotent; failed migration statements roll back together.
Unversioned databases with existing tables and unsupported schema versions are
refused rather than overwritten. Missing required schema objects also cause
startup to fail. Back up an existing database before future migrations.

Transactions serialize concurrent writers. The connection timeout is five
seconds; persistent contention raises a SQLite error rather than silently
discarding a record. This is local storage, not a distributed database.

## Round model

`database/models.py` defines immutable `RoundRecord` and `StoredRound` classes.

| Field | Meaning |
| --- | --- |
| `source`, `round_id` | Nonempty strings; their combination uniquely identifies a round |
| `timestamp` | Result completion time, not the start time |
| `multiplier` | Finite decimal result of at least 1, stored as exact decimal text |
| `collection_timestamp` | Completed-event collection time; cannot precede its result |
| `started_at` | Optional known start; cannot follow completion |
| `pre_round_observed_at` | Optional observation time strictly before a known start |
| `pre_round_data` | JSON object for asserted pre-round information; requires valid observation timing |
| `post_round_data` | Separate JSON object for information available after completion |
| `raw_data` | Sanitized JSON object preserving source fields and original formatting where needed |
| `id`, `ingested_at` | Database-assigned identity and UTC storage time, exposed by `StoredRound` |

Supplied times must be timezone-aware; they are normalized to UTC and stored
with fixed-width microseconds. Naive datetimes, nonfinite numbers, unsupported
JSON objects, cyclic payloads, and invalid timing are rejected. Payloads are
copied and frozen recursively, so changing the original dictionary cannot
change a validated record.

Use a decimal string or `Decimal` when exact source precision matters; a float
cannot recover precision already lost upstream. Canonical multiplier text is
limited to 256 characters, with a bounded exponent. This is a storage limit,
not an assumed game maximum. Numeric analysis must convert the stored text
back to `Decimal` rather than sort it as text.

Pre-round timestamps are caller-supplied assertions. Validation does not prove
live observation or detect information concealed in an arbitrary payload.
A future collector must establish provenance and exclude post-round information
from pre-round inputs. AIE saves completed records only; it does not capture
pre-round events in real time.

## Repository interface

`database/repository.py` provides `RoundRepository` over a connection opened
with `connect_database(settings)` and initialized with
`initialize_schema(connection)`. The caller closes the connection, for example
with `contextlib.closing`. SQLite's own context manager does not close it.

| Method | Behavior |
| --- | --- |
| `insert(record)` | `True` for a new record; `False` for an exact replay |
| `insert_many(records)` | Atomically inserts a batch and returns inserted/duplicate counts |
| `get(source, round_id)` | Returns a `StoredRound`, or `None` |
| `count(source=None)` | Counts all records or one source |
| `list_rounds(...)` | Returns records oldest first, with pagination and optional source/time filters |

An existing `(source, round_id)` with any different stored field raises
`DuplicateRoundError`. This includes a changed collection timestamp. The
original record, ID, and ingestion time are retained. Equivalent decimal
formatting and JSON key ordering are normalized for replay comparison.
There is no silent correction, replacement, or deletion API in this phase.
`insert_many` validates and serializes all supplied records before starting the
transaction, and rolls back the entire batch on a conflicting record or SQL
failure. `insert` uses the same implementation for a single record.

`list_rounds` orders by result timestamp, then source and round ID for stable
ties. It accepts `limit` (default 100, maximum 10000), `offset`, `source`,
`start`, and `end`; the interval includes `start` and excludes `end`.
Queries bind external values as parameters. Inserts and migrations require an
idle connection so they cannot alter a caller's open transaction.

Append-only triggers protect against ordinary accidental updates and deletes.
They cannot prevent a person with direct database access from modifying the
schema or file. Cryptographic integrity and correction history are not
implemented in this phase.

## Tests

```bash
pytest
```

Or explicitly use the selected interpreter:

```bash
python -m pytest
```

The suite retains the 25 Phase 1 checks, 67 Phase 2 cases, 60 Phase 3 cases,
59 Phase 4 cases, and 49 Phase 5 cases, and adds 55 Phase 6 cases. It covers
configuration, logging, directories, startup, migration rollback and version
checks, validation, exact decimal/JSON persistence, chronological queries,
duplicate conflicts, concurrent inserts, and preservation across restarts,
plus file parsing, import limits, dry runs, batch rollback, and import CLI behavior.
Phase 4 tests cover empty selections, bucket boundaries, high-precision decimals,
rounding and context isolation, filtering, snapshot consistency during writes,
read-only connections, stored-data rejection, report publication and overwrite
protection, and the analysis CLI.
Phase 5 tests exercise static assets, HTTP summaries and downloads, empty states,
source and time filters, fixed error responses, missing/incompatible databases,
invalid stored values, file-access boundaries, host/origin checks, rejected
mutations, bounded source suggestions, analysis contention, log redaction,
port validation, startup/shutdown, and unchanged database bytes.
Phase 6 checks cover empty exports/backups, exact round-data replay, export into
a fresh database, deterministic chronological ordering, filters, size/count
limits, stored-field rejection, whole-database preservation, immutable triggers
in the copy, verification and digest failures, corruption, failed flushing,
timeouts, protected publication races, WAL snapshot consistency, private file
permissions, and CLI behavior outside the project working directory.
Database-writing tests use temporary directories. Synthetic records are labeled
as test fixtures and never inserted into the application database by the suite
or startup.

Phase 2 checkpoint: 92 tests passed locally on Python 3.12.14 and 3.14.2, and
in the user's Codespace on Python 3.14.2. Phase 3 checkpoint: 152 tests passed
locally on each version; the source was pushed to GitHub as commit `a8827de`.
Phase 4 checkpoint: 211 tests passed locally on each version; the user's saved
GitHub commit is `35b0111`. Phase 5 checkpoint: 260 tests passed on each version;
the saved GitHub commit is `33be399`, and the user confirmed the dashboard opens
in Codespaces. Phase 6 validation: **315 tests passed on each of Python 3.12.14
and 3.14.2** on Linux, using `python-dotenv` **1.2.4** and `pytest` **9.1.1**.
A native Windows run has not been verified.
Chromium browser checks verified filters with a non-UTC browser timezone,
downloaded report contents, Reset and Refresh, escaped source names, error
recovery without stale statistics, populated/empty states, no external requests,
and layouts at widths of 320, 390, 768, 1024, and 1440 pixels. Browser checks
used disposable synthetic fixtures, not real Aviator data. Dashboard regression
checks also passed after the Phase 6 status display update.

## Phase 6 source changes

| File | Change |
| --- | --- |
| `export_rounds.py` | New completed-round JSON export CLI |
| `backup_database.py` | New backup creation and verification CLI |
| `database/export.py` | Read-only snapshot selection and import-compatible round export |
| `database/backup.py` | SQLite snapshot copy, verification, hashing, and protected publication |
| `database/records.py` | Shared complete stored-round validation and portable serialization |
| `utils/__init__.py`, `utils/files.py` | Portable output names, flushed writing, and atomic publication |
| `config/settings.py` | Phase 6 constant and configured backup-directory property |
| `dashboard/static/index.html`, `dashboard/static/app.js` | Current phase display and CLI export/backup status |
| `tests/test_data_management.py` | New Phase 6 tests |
| `tests/test_analysis.py`, `tests/test_setup.py`, `tests/test_dashboard.py` | Current-phase expectations |
| `README.md` | Export, backup, verification, checks, and limitations |

Configuration, directories, `.env.example`, `.gitignore`, and requirements
remain compatible with Phase 5. Database schema version remains 1. Other
feature packages remain placeholders.

## Git and current limitations

`.gitignore` excludes `.env`, common credential files, virtual environments,
SQLite files and journals, Python caches, logs, local data, and editor metadata.
`.gitkeep` files preserve the data directories. Review changes before committing;
ignore rules cannot identify every possible secret.

After reviewing and testing a source update in your own checkout:

```bash
git status
git add README.md config dashboard database utils export_rounds.py backup_database.py tests
git commit -m "Implement AIE Phase 6 round export and verified backups"
git push
```

Current limitations:

- No BetPawa connection, login, credentials, browser automation, scraper, or API integration.
- No automatic acquisition of real or historical Aviator data; imports require a supplied local file.
- No cryptographic verifier, inferential hypothesis tests, prediction engine, or ML models.
- No prediction accuracy claims, betting features, or transactions.
- Local development dashboard only; no production deployment or application authentication.
- Local backups only; no scheduled/offsite backup service or automatic restore.

Development stops at Phase 6. Phase 7 requires a separate instruction.

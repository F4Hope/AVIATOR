# Aviator Intelligence Engine (AIE)

AIE is a staged research project for investigating whether legitimately available
pre-round information contains useful predictive information. The project does
not assume that exact prediction is possible.

**Current development phase: Phase 2 — Validated round storage.**

Phase 1 supplied configuration, logging, project directories, and SQLite
connections. Phase 2 adds a completed-round model, a versioned schema, and an
append-only repository. **It does not collect Aviator data or make predictions.**
Startup creates an empty schema, never sample rounds.

## Requirements

- Python 3.12 or newer, including standard-library `sqlite3` with JSON functions.
- Git for version control.
- VS Code or GitHub Codespaces, optionally with the Python extension.

Only two third-party packages are needed: `python-dotenv` for configuration and
`pytest` for tests. SQLite, pathlib, logging, typing, datetime, JSON, and Decimal
come with Python. Phase 2 adds no dependencies.

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

For an existing Phase 1 installation, keep your `.env`, database, and virtual
environment. After updating the source files, run:

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
Phase: 2
Status: INITIALIZED
Database: READY
Schema version: 1
Rounds stored: 0
Environment: DEVELOPMENT
```

The count reflects stored records on later runs. **Development phase 2 uses
database schema version 1**: this is the first application schema, since
Phase 1 had no tables. Startup checks SQLite, initializes or verifies the schema,
reports the count, and closes its connection.

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
from pre-round inputs. Phase 2 saves completed records only; it does not capture
pre-round events in real time.

## Repository interface

`database/repository.py` provides `RoundRepository` over a connection opened
with `connect_database(settings)` and initialized with
`initialize_schema(connection)`. The caller closes the connection, for example
with `contextlib.closing`. SQLite's own context manager does not close it.

| Method | Behavior |
| --- | --- |
| `insert(record)` | `True` for a new record; `False` for an exact replay |
| `get(source, round_id)` | Returns a `StoredRound`, or `None` |
| `count(source=None)` | Counts all records or one source |
| `list_rounds(...)` | Returns records oldest first, with pagination and optional source/time filters |

An existing `(source, round_id)` with any different stored field raises
`DuplicateRoundError`. This includes a changed collection timestamp. The
original record, ID, and ingestion time are retained. Equivalent decimal
formatting and JSON key ordering are normalized for replay comparison.
There is no silent correction, replacement, or deletion API in this phase.

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

The suite retains the 25 Phase 1 checks and adds 67 Phase 2 cases. It covers
configuration, logging, directories, startup, migration rollback and version
checks, validation, exact decimal/JSON persistence, chronological queries,
duplicate conflicts, concurrent inserts, and preservation across restarts.
Database-writing tests use temporary directories. Synthetic records are labeled
as test fixtures and never inserted into the application database by the suite
or startup.

Phase 2 validation: **92 tests passed on each of Python 3.12.14 and 3.14.2**
on Linux, using `python-dotenv` **1.2.4** and `pytest` **9.1.1**. Fresh and
repeated startup succeeded with zero rounds. A native Windows run has not
been verified here.

## Phase 2 source changes

| File | Change |
| --- | --- |
| `database/models.py` | New validated, immutable record classes |
| `database/migrations.py` | New transactional schema initialization |
| `database/repository.py` | New round insertion and retrieval interface |
| `database/database.py` | Explicit transaction configuration and row access |
| `database/__init__.py` | Updated package description |
| `main.py` | Schema initialization and round-count startup status |
| `tests/conftest.py` | Shared configuration and database isolation |
| `tests/test_database.py` | New Phase 2 tests |
| `tests/test_setup.py` | Existing checks adapted to Phase 2 startup |
| `README.md` | Updated installation, usage, and limitations |

Configuration, directories, `.env.example`, `.gitignore`, and requirements
remain compatible with Phase 1. Other feature packages remain placeholders.

## Git and current limitations

`.gitignore` excludes `.env`, common credential files, virtual environments,
SQLite files and journals, Python caches, logs, local data, and editor metadata.
`.gitkeep` files preserve the data directories. Review changes before committing;
ignore rules cannot identify every possible secret.

After reviewing and testing a source update in your own checkout:

```bash
git status
git add README.md main.py database tests
git commit -m "Implement AIE Phase 2 round storage"
git push
```

Current limitations:

- No BetPawa connection, login, credentials, browser automation, scraper, or API integration.
- No real or historical Aviator data collection and no user-facing import command.
- No cryptographic verifier, statistical analysis, prediction engine, or ML models.
- No prediction accuracy claims, dashboard implementation, betting features, or transactions.

Development stops at Phase 2. Phase 3 requires a separate instruction.

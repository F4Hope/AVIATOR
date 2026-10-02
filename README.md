# Aviator Intelligence Engine (AIE)

AIE is a staged research project for investigating whether legitimately available
pre-round information contains useful predictive information. The project does
not assume that exact prediction is possible.

**Current development phase: Phase 1 — Project setup.**

Phase 1 provides configuration, console logging, data directories, SQLite
connection infrastructure, and startup tests. **It does not collect Aviator data
or make predictions.** Later-phase packages contain placeholders only.

## Requirements

- Python 3.12 or newer, with the standard-library `sqlite3` module.
- Git for local version control.
- VS Code, optionally with its Python extension.

Only two third-party packages are needed: `python-dotenv` for configuration and
`pytest` for tests. SQLite, pathlib, logging, and typing come with Python.

## Installation in VS Code on Windows

Extract the project ZIP, then open the `aviator-intelligence` folder in VS Code
with **File → Open Folder**. Open a PowerShell terminal in that folder.

```powershell
py -3 --version
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

The first command must report Python 3.12 or newer. If several Python versions
are installed, choose an appropriate interpreter; for Python 3.12 use
`py -3.12 -m venv .venv`. In VS Code, use **Python: Select Interpreter** and
select the interpreter inside `.venv`.

If PowerShell blocks activation, use the virtual environment's interpreter
directly; activation is optional:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
.\.venv\Scripts\python.exe -m pytest
```

In Windows Command Prompt, activation is `.venv\Scripts\activate.bat`, and
copying the configuration example is `copy .env.example .env`.

### macOS / Linux

```bash
python3 --version
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

## Configuration

The project root is resolved from `config/settings.py` using pathlib. Configuration
loads the `.env` in that root; existing process environment variables take
precedence. The application runs with defaults if `.env` does not exist.

| Variable | Default | Accepted values |
| --- | --- | --- |
| `AIE_ENVIRONMENT` | `DEVELOPMENT` | `DEVELOPMENT`, `TEST`, `PRODUCTION` |
| `AIE_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `AIE_DATABASE_FILENAME` | `aie.sqlite3` | A plain filename ending in `.db`, `.sqlite`, or `.sqlite3` |

All database files stay inside `data/database/`; this setting accepts a filename,
not a path. Startup creates missing `data/raw`, `data/processed`, and
`data/database` directories. Invalid settings or inaccessible directories cause
a nonzero exit code. Configuration modules do not create files during import.

No passwords, cookies, tokens, or other credentials are needed. Do not add them
to configuration or log messages. `.env` is ignored by Git.

## Run the application

With the virtual environment activated, run from the project folder:

```bash
python main.py
```

Expected standard output with default settings:

```text
Aviator Intelligence Engine
Status: INITIALIZED
Database: READY
Environment: DEVELOPMENT
```

An informational log is also written to standard error, for example:

```text
2026-10-01T15:00:00+0000 | INFO | aie.main | Phase 1 initialization complete.
```

The timestamp and timezone reflect your computer. The log format contains
timestamp, level, module logger name, and message. Logs stay on the console.
Application modules can use `logging.getLogger("aie.<module>")`.

The SQLite readiness check executes `SELECT 1`, creates no application tables,
and closes its connection before returning. Callers of `connect_database`
own the connection and should close it explicitly, for example with
`contextlib.closing`.

## Run tests

```bash
pytest
```

Or explicitly use the selected interpreter:

```bash
python -m pytest
```

Tests cover defaults, `.env` loading, environment precedence, rejected settings,
working-directory-independent paths, directory creation and failure, SQLite
queries without a schema, connection closure, all logging levels, and successful
and failed startup. Database-writing tests use temporary directories.

Phase 1 validation: all **25 tests passed** on Linux with Python **3.12.14**,
`python-dotenv` **1.2.4**, and `pytest` **9.1.1**. Default application startup
also completed successfully. The Windows instructions are provided for your
workstation; a native Windows run has not been verified here.

## Git

The delivered archive contains source files only. Initialize a local repository
after extraction if the folder is not already a repository:

```bash
git init -b main
git add .
git status
```

Optionally create the first local commit after configuring your own Git identity:

```bash
git commit -m "Initialize AIE Phase 1 foundation"
```

`.gitignore` excludes `.env`, common credential files, virtual environments,
SQLite files and journals, Python caches, logs, and editor metadata. `.gitkeep`
files preserve the three empty data directories in Git. Review files before
committing; ignore rules cannot identify every possible secret.

## Current limitations

- No rounds table, rounds data model, or data migrations; these belong to Phase 2.
- No BetPawa connection, browser automation, scraper, API integration, or login.
- No historical data collection, cryptographic verifier, or statistical analysis.
- No prediction engine, machine-learning models, or prediction accuracy claims.
- No dashboard, betting features, or financial transactions.

Development stops at Phase 1 until the next phase is requested.

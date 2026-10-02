"""Load non-secret settings without creating files during import."""

from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
DEVELOPMENT_PHASE: int = 7
LOG_LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
ENVIRONMENTS: tuple[str, ...] = ("DEVELOPMENT", "TEST", "PRODUCTION")


@dataclass(frozen=True, slots=True)
class Settings:
    """Resolved paths and validated runtime settings; no credentials."""

    project_root: Path
    data_dir: Path
    raw_data_dir: Path
    processed_data_dir: Path
    database_dir: Path
    database_path: Path
    environment: str
    log_level: str

    @property
    def backup_dir(self) -> Path:
        """Backups are local data; create this directory only on explicit backup."""
        return self.database_dir / "backups"

    @property
    def required_directories(self) -> tuple[Path, ...]:
        """Directories that application startup must create or verify."""
        return (
            self.data_dir,
            self.raw_data_dir,
            self.processed_data_dir,
            self.database_dir,
        )


def load_settings(project_root: Path | None = None) -> Settings:
    """Read the project's .env while preserving existing environment values.

    The optional root makes tests independent of the real project database.
    Relative paths are resolved from the project, not the working directory.
    """
    root = (project_root if project_root is not None else PROJECT_ROOT).resolve()
    load_dotenv(dotenv_path=root / ".env", override=False, encoding="utf-8")

    environment = os.getenv("AIE_ENVIRONMENT", "DEVELOPMENT").strip().upper()
    log_level = os.getenv("AIE_LOG_LEVEL", "INFO").strip().upper()
    filename = os.getenv("AIE_DATABASE_FILENAME", "aie.sqlite3").strip()

    if environment not in ENVIRONMENTS:
        raise ValueError("AIE_ENVIRONMENT must be DEVELOPMENT, TEST, or PRODUCTION.")
    if log_level not in LOG_LEVELS:
        raise ValueError("AIE_LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL.")
    if (
        not filename
        or filename in {".", ".."}
        or any(character in filename for character in '/\\:*?"<>|')
        or filename.endswith(".")
        or Path(filename).suffix.lower() not in {".db", ".sqlite", ".sqlite3"}
    ):
        raise ValueError(
            "AIE_DATABASE_FILENAME must be a plain .db, .sqlite, or .sqlite3 filename."
        )

    data_dir = root / "data"
    database_dir = data_dir / "database"
    return Settings(
        project_root=root,
        data_dir=data_dir,
        raw_data_dir=data_dir / "raw",
        processed_data_dir=data_dir / "processed",
        database_dir=database_dir,
        database_path=database_dir / filename,
        environment=environment,
        log_level=log_level,
    )


def ensure_directories(settings: Settings) -> None:
    """Create missing data directories, raising an OS error on failure."""
    for directory in settings.required_directories:
        directory.mkdir(parents=True, exist_ok=True)

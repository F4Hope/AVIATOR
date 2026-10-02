"""Serialize aggregate statistics and publish JSON reports without partial writes."""

from dataclasses import fields, is_dataclass
from datetime import datetime
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile

from analysis.descriptive import (
    ARITHMETIC_PRECISION, MEAN_DECIMAL_PLACES, PERCENTAGE_DECIMAL_PLACES, AnalysisReport,
)
from config.settings import Settings
from database.models import timestamp_text


REPORT_VERSION = 1


def decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _json_value(value: object) -> object:
    if is_dataclass(value):
        return {field.name: _json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Decimal):
        return decimal_text(value)
    if isinstance(value, datetime):
        return timestamp_text(value)
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


def report_to_dict(report: AnalysisReport) -> dict[str, object]:
    """Use decimal strings and JSON null for unavailable statistics; no raw rows."""
    if not isinstance(report, AnalysisReport):
        raise TypeError("report must be AnalysisReport.")
    result = {field.name: _json_value(getattr(report, field.name)) for field in fields(report)}
    result.update({
        "report_version": REPORT_VERSION, "phase": 4, "status": report.status,
        "numeric_policy": {
            "arithmetic_precision": ARITHMETIC_PRECISION,
            "mean_decimal_places": MEAN_DECIMAL_PLACES,
            "percentage_decimal_places": PERCENTAGE_DECIMAL_PLACES,
            "rounding": "ROUND_HALF_EVEN",
        },
        "interpretation": "Historical descriptive statistics; not forecasts or betting recommendations.",
    })
    return result


def write_json_report(
    report: AnalysisReport, settings: Settings, filename: str = "analysis.json", *, overwrite: bool = False,
) -> Path:
    """Publish inside data/processed; refuse existing reports unless explicitly allowed.

    A same-directory temporary file is fully written and flushed before
    publication. A hard link publishes without overwriting an existing target;
    explicit overwrites use atomic replacement. Temporary files are cleaned up.
    """
    if (
        not isinstance(filename, str) or not filename.strip() or filename != filename.strip()
        or any(character in filename for character in '/\\:*?"<>|')
        or any(ord(character) < 32 or ord(character) == 127 for character in filename)
        or Path(filename).suffix.lower() != ".json" or filename.startswith(".")
    ):
        raise ValueError("Report output must be a plain JSON filename.")
    if type(overwrite) is not bool:
        raise TypeError("overwrite must be a boolean.")
    content = json.dumps(report_to_dict(report), indent=2, sort_keys=True, allow_nan=False) + "\n"
    directory = settings.processed_data_dir
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename
    if not overwrite and (target.exists() or target.is_symlink()):
        raise FileExistsError("The report already exists; use a new filename or explicit overwrite.")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory, prefix=".aie-report-", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if overwrite:
            os.replace(temporary, target)
        else:
            os.link(temporary, target)
        return target
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

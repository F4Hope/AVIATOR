"""Serialize and publish Phase 8 ML experiment reports."""

from dataclasses import fields, is_dataclass
from decimal import Decimal
import json
from pathlib import Path

from config.settings import DEVELOPMENT_PHASE, Settings
from evaluation.ml_experiment import ExperimentReport
from utils.files import write_bytes


REPORT_VERSION = 1


def _json_value(value: object) -> object:
    if is_dataclass(value):
        return {field.name: _json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Decimal):
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


def experiment_to_dict(report: ExperimentReport) -> dict[str, object]:
    if not isinstance(report, ExperimentReport):
        raise TypeError("report must be ExperimentReport.")
    document = {field.name: _json_value(getattr(report, field.name)) for field in fields(report)}
    document.update({
        "report_version": REPORT_VERSION,
        "phase": DEVELOPMENT_PHASE,
        "model_name": "logistic-threshold-classifier",
        "model_version": "1",
        "interpretation": (
            "Chronological historical ML experiment; not evidence of future predictability "
            "and not a betting recommendation."
        ),
    })
    return document


def write_experiment_report(
    report: ExperimentReport,
    settings: Settings,
    filename: str = "ml-experiment.json",
    *,
    overwrite: bool = False,
) -> Path:
    if not isinstance(filename, str) or Path(filename).suffix.lower() != ".json":
        raise ValueError("Experiment output must be a JSON filename.")
    content = (
        json.dumps(experiment_to_dict(report), indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    return write_bytes(settings.processed_data_dir, filename, content, overwrite=overwrite)

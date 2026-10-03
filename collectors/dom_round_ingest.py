"""Convert detected DOM history insertions into local AIE completed-round records."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
import hashlib
from pathlib import Path

from collectors.probe_analysis import extract_dom_completed_rounds
from config.settings import Settings, ensure_directories
from database.database import connect_database
from database.migrations import initialize_schema
from database.models import RoundRecord
from database.repository import RoundRepository


DOM_OBSERVATION_SOURCE = "AIE_DOM_HISTORY_BETPAWA_CM"


@dataclass(frozen=True, slots=True)
class DomIngestReport:
    extracted: int
    inserted: int | None
    duplicates: int | None
    dry_run: bool


def _observation_round_id(observed_at: str, multiplier: str, edge: str, y_bucket: int) -> str:
    material = f"{observed_at}|{multiplier}|{edge}|{y_bucket}".encode("utf-8")
    return "aie-dom-" + hashlib.sha256(material).hexdigest()[:24]


def load_dom_round_records(path: Path) -> tuple[RoundRecord, ...]:
    """Build validated POST-ROUND records from detected DOM history insertions.

    The source and IDs deliberately identify AIE's local observation layer.
    They must not be interpreted as provider-issued round identifiers.
    """
    events = extract_dom_completed_rounds(path)
    records: list[RoundRecord] = []
    for event in events:
        observed_at = datetime.fromisoformat(event.observed_at.replace("Z", "+00:00"))
        records.append(
            RoundRecord(
                source=DOM_OBSERVATION_SOURCE,
                round_id=_observation_round_id(
                    event.observed_at, event.multiplier, event.edge, event.y_bucket
                ),
                timestamp=observed_at,
                collection_timestamp=observed_at,
                multiplier=event.multiplier,
                post_round_data={
                    "observation_method": "dom_history_shift",
                    "timestamp_semantics": "first_observed_history_insertion",
                    "provider_round_id_available": False,
                    "edge": event.edge,
                    "overlap": event.overlap,
                    "history_size": event.history_size,
                    "y_bucket": event.y_bucket,
                },
            )
        )
    return tuple(records)


def ingest_dom_rounds(path: Path, settings: Settings, *, dry_run: bool = False) -> DomIngestReport:
    """Extract and atomically insert locally observed completed rounds."""
    records = load_dom_round_records(path)
    if dry_run:
        return DomIngestReport(len(records), None, None, True)

    ensure_directories(settings)
    with closing(connect_database(settings)) as connection:
        initialize_schema(connection)
        result = RoundRepository(connection).insert_many(records)
    return DomIngestReport(len(records), result.inserted, result.duplicates, False)

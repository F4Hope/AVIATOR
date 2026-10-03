"""Interactive visual round marker for correlating Aviator UI state with sanitized network metadata."""

from __future__ import annotations

import math
import sys

from collectors.network_probe import ProbeWriter
from config.settings import load_settings


MARKER_FILENAME = "aviator-round-markers.jsonl"


def _parse_multiplier(value: str) -> float:
    multiplier = float(value)
    if not math.isfinite(multiplier) or multiplier < 1.0 or multiplier > 1_000_000.0:
        raise ValueError("multiplier must be finite and between 1.0 and 1,000,000.")
    return multiplier


def _write_marker(writer: ProbeWriter, event: str, multiplier: float | None = None) -> None:
    payload: dict[str, object] = {
        "kind": "visual_marker",
        "event": event,
    }
    if multiplier is not None:
        payload["multiplier"] = multiplier
    if not writer.append(payload):
        raise OSError("marker file size limit reached")


def main() -> int:
    settings = load_settings()
    writer = ProbeWriter(settings.raw_data_dir / MARKER_FILENAME, max_bytes=2 * 1024 * 1024)

    print(
        "AIE visual round marker\n"
        "Commands:\n"
        "  s            mark visible ROUND START\n"
        "  c <value>    mark visible ROUND COMPLETE and final multiplier\n"
        "  q            quit\n"
        "Example: c 4.97"
    )

    while True:
        try:
            command = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if not command:
            continue
        if command.lower() == "q":
            return 0
        if command.lower() == "s":
            _write_marker(writer, "round_start")
            print("marked round_start")
            continue

        parts = command.split()
        if len(parts) == 2 and parts[0].lower() == "c":
            try:
                multiplier = _parse_multiplier(parts[1])
                _write_marker(writer, "round_complete", multiplier)
            except (ValueError, OSError) as exc:
                print(f"marker rejected: {exc}", file=sys.stderr)
                continue
            print(f"marked round_complete multiplier={multiplier:g}")
            continue

        print("Use: s | c <multiplier> | q", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())

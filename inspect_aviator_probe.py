"""Print a value-free summary of sanitized Aviator network observations."""

import argparse
from pathlib import Path
import sys

from collectors.probe_analysis import summarize_probe
from config.settings import DEVELOPMENT_PHASE, load_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect sanitized Aviator probe structure without payload values."
    )
    parser.add_argument("--input", default="aviator-network-probe.jsonl")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)
    if "/" in args.input or "\\" in args.input or not args.input.endswith(".jsonl"):
        parser.error("--input must be a plain .jsonl filename.")
    if not 1 <= args.limit <= 100:
        parser.error("--limit must be between 1 and 100.")
    try:
        settings = load_settings()
        candidates = summarize_probe(settings.raw_data_dir / args.input)
    except (OSError, ValueError):
        print("Probe inspection failed. Check the probe filename and configuration.", file=sys.stderr)
        return 1

    print(
        "Aviator Intelligence Engine\n"
        f"Phase: {DEVELOPMENT_PHASE}\n"
        "Probe inspection: COMPLETE\n"
        f"Observed groups: {len(candidates)}"
    )
    for index, candidate in enumerate(candidates[: args.limit], start=1):
        print(
            f"\n[{index}] kind={candidate.kind} events={candidate.events}\n"
            f"url={candidate.url}"
        )
        if candidate.candidate_paths:
            print("candidate_paths:")
            for path, count in candidate.candidate_paths[:20]:
                print(f"  {path} ({count})")
        elif candidate.json_paths:
            print("json_paths:")
            for path, count in candidate.json_paths[:10]:
                print(f"  {path} ({count})")
        else:
            print("json_paths: none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

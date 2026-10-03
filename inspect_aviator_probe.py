"""Print a value-free summary of sanitized Aviator network observations."""

import argparse
from pathlib import Path
import sys

from collectors.probe_analysis import summarize_binary_frame_classes, summarize_live_transports, summarize_probe
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
        probe_path = settings.raw_data_dir / args.input
        live_candidates = summarize_live_transports(probe_path)
        frame_classes = summarize_binary_frame_classes(probe_path)
        candidates = summarize_probe(probe_path)
    except (OSError, ValueError):
        print("Probe inspection failed. Check the probe filename and configuration.", file=sys.stderr)
        return 1

    print(
        "Aviator Intelligence Engine\n"
        f"Phase: {DEVELOPMENT_PHASE}\n"
        "Probe inspection: COMPLETE\n"
        f"Observed groups: {len(candidates)}\n"
        f"Live transport candidates: {len(live_candidates)}\n"
        f"Binary frame classes: {len(frame_classes)}"
    )

    if live_candidates:
        print("\nLIVE TRANSPORT CANDIDATES")
        for index, candidate in enumerate(live_candidates[:10], start=1):
            print(
                f"\n[L{index}] score={candidate.score} events={candidate.events} "
                f"received={candidate.received_events} sent={candidate.sent_events}\n"
                f"url={candidate.url}\n"
                f"formats: json={candidate.json_events} text={candidate.text_events} "
                f"binary={candidate.binary_events}\n"
                f"sizes: min={candidate.min_bytes} max={candidate.max_bytes} "
                f"distinct={candidate.distinct_sizes}"
            )
            if candidate.opcode_counts:
                print("opcodes:")
                for opcode, count in candidate.opcode_counts:
                    print(f"  {opcode}: {count}")
            if candidate.entropy_buckets:
                print("binary_fingerprint:")
                entropy = ", ".join(f"{value}:{count}" for value, count in candidate.entropy_buckets[:10])
                printable = ", ".join(f"{value}:{count}" for value, count in candidate.printable_ratio_buckets[:10])
                zero = ", ".join(f"{value}:{count}" for value, count in candidate.zero_ratio_buckets[:10])
                high_bit = ", ".join(f"{value}:{count}" for value, count in candidate.high_bit_ratio_buckets[:10])
                unique = ", ".join(f"{value}:{count}" for value, count in candidate.unique_byte_buckets[:10])
                print(f"  entropy={entropy}")
                print(f"  printable_ratio={printable}")
                print(f"  zero_ratio={zero}")
                print(f"  high_bit_ratio={high_bit}")
                print(f"  unique_byte_bucket={unique}")
            if candidate.candidate_paths:
                print("candidate_paths:")
                for path, count in candidate.candidate_paths[:20]:
                    print(f"  {path} ({count})")
    if frame_classes:
        print("\nBINARY FRAME CLASSES")
        for index, frame_class in enumerate(frame_classes[:15], start=1):
            print(
                f"\n[B{index}] events={frame_class.events} direction={frame_class.direction} "
                f"size={frame_class.size_bucket}\n"
                f"url={frame_class.url}\n"
                f"timing_ms: median={frame_class.median_gap_ms} "
                f"min={frame_class.min_gap_ms} max={frame_class.max_gap_ms} "
                f"periodicity={frame_class.periodicity_score}\n"
                f"fingerprint_mode: entropy={frame_class.entropy_mode} "
                f"zero={frame_class.zero_ratio_mode} "
                f"high_bit={frame_class.high_bit_ratio_mode} "
                f"unique={frame_class.unique_byte_mode}"
            )

    for index, candidate in enumerate(candidates[: args.limit], start=1):
        print(
            f"\n[{index}] kind={candidate.kind} events={candidate.events}\n"
            f"url={candidate.url}"
        )
        if candidate.status_counts:
            print("status_counts:")
            for status, count in candidate.status_counts:
                print(f"  {status}: {count}")
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

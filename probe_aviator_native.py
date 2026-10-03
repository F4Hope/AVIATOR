"""CLI for sanitized Aviator discovery using a local persistent Edge/Chrome profile."""

import argparse
import logging
import os
import signal
import sys

from collectors.native_browser_probe import NativeBrowserProbe, NativeBrowserProbeConfig
from config.logging_config import configure_logging
from config.settings import DEVELOPMENT_PHASE, load_settings


DEFAULT_SOURCE = "BETPAWA_CM_AVIATOR"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Observe sanitized Aviator traffic in a local persistent browser."
    )
    parser.add_argument("--output", default="aviator-network-probe.jsonl")
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9222")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if "/" in args.output or "\\" in args.output or not args.output.endswith(".jsonl"):
        print("Probe failed: --output must be a plain .jsonl filename.", file=sys.stderr)
        return 2

    try:
        settings = load_settings()
        configure_logging(settings.log_level)
        target_url = os.environ["AIE_BETPAWA_AVIATOR_URL"].strip()
        probe = NativeBrowserProbe(
            NativeBrowserProbeConfig(
                target_url=target_url,
                output_path=settings.raw_data_dir / args.output,
                cdp_url=args.cdp_url,
                dom_output_path=settings.raw_data_dir / "aviator-dom-multipliers.jsonl",
                heartbeat_path=settings.raw_data_dir / "aviator-dom-heartbeat.json",
            )
        )
    except (KeyError, OSError, ValueError):
        print(
            "Probe failed: set AIE_BETPAWA_AVIATOR_URL to the exact HTTPS Aviator page.",
            file=sys.stderr,
        )
        return 1

    def stop_probe(signum: int, frame: object) -> None:
        probe.stop()

    signal.signal(signal.SIGINT, stop_probe)
    signal.signal(signal.SIGTERM, stop_probe)

    print(
        "Aviator Intelligence Engine\n"
        f"Phase: {DEVELOPMENT_PHASE}\n"
        "Native browser probe: RUNNING\n"
        f"CDP endpoint: {probe.config.cdp_url}\n"
        f"Source label: {DEFAULT_SOURCE}\n"
        f"Sanitized output: {probe.config.output_path}\n"
        f"DOM multiplier output: {probe.config.dom_output_path}\n"
        f"Collector heartbeat: {probe.config.heartbeat_path}\n"
        "Use the opened browser manually. No cookies, headers, query strings, passwords, "
        "tokens, or raw payload values are written to the probe file."
    )
    try:
        probe.run()
    except (OSError, RuntimeError, ValueError) as exc:
        logging.getLogger("aie.probe_aviator_native").error(
            "Native browser probe stopped unexpectedly (%s). "
            "The supervisor may reattach if Edge CDP remains available.",
            type(exc).__name__,
        )
        return 1
    except Exception as exc:
        logging.getLogger("aie.probe_aviator_native").exception(
            "Native browser probe crashed with an unexpected %s.",
            type(exc).__name__,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

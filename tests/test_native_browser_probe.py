"""Tests for the native persistent browser probe."""

from pathlib import Path
import pytest

from collectors.native_browser_probe import NativeBrowserProbeConfig


def test_native_probe_config_accepts_edge(tmp_path: Path) -> None:
    config = NativeBrowserProbeConfig(
        target_url="https://example.test/aviator",
        output_path=tmp_path / "probe.jsonl",
        profile_dir=tmp_path / "profile",
        browser_channel="msedge",
    )
    assert config.browser_channel == "msedge"


def test_native_probe_config_rejects_http(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        NativeBrowserProbeConfig(
            target_url="http://example.test/aviator",
            output_path=tmp_path / "probe.jsonl",
            profile_dir=tmp_path / "profile",
            browser_channel="msedge",
        )


def test_native_probe_config_rejects_unknown_browser(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        NativeBrowserProbeConfig(
            target_url="https://example.test/aviator",
            output_path=tmp_path / "probe.jsonl",
            profile_dir=tmp_path / "profile",
            browser_channel="firefox",
        )

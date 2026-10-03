"""Tests for the native Edge CDP probe."""

from pathlib import Path
import pytest

from collectors.native_browser_probe import NativeBrowserProbeConfig


def test_native_probe_config_accepts_local_cdp(tmp_path: Path) -> None:
    config = NativeBrowserProbeConfig(
        target_url="https://example.test/aviator",
        output_path=tmp_path / "probe.jsonl",
        cdp_url="http://127.0.0.1:9222",
    )
    assert config.cdp_url == "http://127.0.0.1:9222"


def test_native_probe_config_rejects_http_target(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        NativeBrowserProbeConfig(
            target_url="http://example.test/aviator",
            output_path=tmp_path / "probe.jsonl",
        )


def test_native_probe_config_rejects_remote_cdp(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        NativeBrowserProbeConfig(
            target_url="https://example.test/aviator",
            output_path=tmp_path / "probe.jsonl",
            cdp_url="http://192.0.2.10:9222",
        )


def test_native_probe_config_rejects_non_jsonl_output(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        NativeBrowserProbeConfig(
            target_url="https://example.test/aviator",
            output_path=tmp_path / "probe.txt",
        )

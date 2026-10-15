"""Hosted Browserless collector configuration tests."""

from pathlib import Path

import pytest

from collectors.native_browser_probe import NativeBrowserProbeConfig
from probe_aviator_hosted import _endpoint


def test_remote_cdp_requires_explicit_opt_in(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        NativeBrowserProbeConfig(
            target_url="https://example.test/",
            output_path=tmp_path / "probe.jsonl",
            cdp_url="wss://browser.example.test/",
        )


def test_remote_cdp_allowed_for_hosted_collector(tmp_path: Path) -> None:
    config = NativeBrowserProbeConfig(
        target_url="https://example.test/",
        output_path=tmp_path / "probe.jsonl",
        cdp_url="wss://browser.example.test/",
        allow_remote_cdp=True,
        live_session_path=tmp_path / "session.json",
    )
    assert config.allow_remote_cdp is True


def test_hosted_endpoint_adds_token_and_persistent_profile_launch() -> None:
    value = _endpoint("wss://browser.example.test", "synthetic-token")
    assert value.startswith("wss://browser.example.test?")
    assert "token=synthetic-token" in value
    assert "launch=" in value
    assert "synthetic-token" not in value.split("token=", 1)[0]


def test_live_session_path_must_be_json(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        NativeBrowserProbeConfig(
            target_url="https://example.test/",
            output_path=tmp_path / "probe.jsonl",
            live_session_path=tmp_path / "session.txt",
        )

"""Tests for the native Edge CDP probe."""

from pathlib import Path
import pytest

from collectors.native_browser_probe import NativeBrowserProbeConfig, _cdp_frame_payload, _socket_id


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



def test_socket_id_is_stable_and_value_free() -> None:
    assert _socket_id("request-123") == _socket_id("request-123")
    assert _socket_id("request-123") != _socket_id("request-124")
    assert "request-123" not in _socket_id("request-123")


def test_cdp_frame_payload_preserves_text_shape() -> None:
    payload = _cdp_frame_payload({"opcode": 1, "payloadData": '{"roundId":1,"value":2}'})
    assert payload == '{"roundId":1,"value":2}'


def test_cdp_frame_payload_decodes_binary_base64() -> None:
    payload = _cdp_frame_payload({"opcode": 2, "payloadData": "aGVsbG8="})
    assert payload == b"hello"

"""Tests for the native Edge CDP probe."""

from pathlib import Path
import pytest

from collectors.native_browser_probe import NativeBrowserProbe, NativeBrowserProbeConfig, _cdp_frame_payload, _normalize_dom_candidates, _socket_id


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



def test_dom_candidate_normalization_keeps_only_multiplier_text() -> None:
    raw = [
        {"text": "4.97x", "x": 913.2, "y": 21.4, "w": 45.0, "h": 19.0},
        {"text": "Balance 1000", "x": 0, "y": 0, "w": 50, "h": 10},
        {"text": "1.00X", "x": 400, "y": 60, "w": 30, "h": 15},
    ]
    result = _normalize_dom_candidates(raw)
    assert [item["multiplier"] for item in result] == ["4.97", "1.00"]
    assert result[0]["x_bucket"] == 910
    assert result[0]["y_bucket"] == 20


def test_dom_candidate_normalization_rejects_out_of_range_values() -> None:
    raw = [
        {"text": "0.99x", "x": 1, "y": 1, "w": 10, "h": 10},
        {"text": "1000001x", "x": 1, "y": 1, "w": 10, "h": 10},
    ]
    assert _normalize_dom_candidates(raw) == ()


def test_native_probe_config_accepts_dom_jsonl_output(tmp_path: Path) -> None:
    config = NativeBrowserProbeConfig(
        target_url="https://example.test/aviator",
        output_path=tmp_path / "probe.jsonl",
        dom_output_path=tmp_path / "dom.jsonl",
    )
    assert config.dom_output_path == tmp_path / "dom.jsonl"



def test_native_probe_config_accepts_heartbeat_json(tmp_path: Path) -> None:
    config = NativeBrowserProbeConfig(
        target_url="https://example.test/aviator",
        output_path=tmp_path / "probe.jsonl",
        heartbeat_path=tmp_path / "heartbeat.json",
    )
    assert config.heartbeat_path == tmp_path / "heartbeat.json"


def test_native_probe_writes_session_heartbeat(tmp_path: Path) -> None:
    import json

    heartbeat = tmp_path / "heartbeat.json"
    probe = NativeBrowserProbe(
        NativeBrowserProbeConfig(
            target_url="https://example.test/aviator",
            output_path=tmp_path / "probe.jsonl",
            heartbeat_path=heartbeat,
        )
    )
    probe._write_heartbeat()
    value = json.loads(heartbeat.read_text(encoding="utf-8"))
    assert value["kind"] == "dom_probe_heartbeat"
    assert value["collector_session_id"] == probe.collector_session_id
    assert value["observed_at"].endswith("Z")

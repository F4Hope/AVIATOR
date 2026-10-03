"""Phase 9 browser-probe tests use synthetic payloads only."""

import json
from pathlib import Path

import pytest

from collectors.browser_probe import BrowserProbeConfig
from collectors.network_probe import ProbeWriter, binary_fingerprint, json_shape, payload_metadata, safe_url
from collectors.probe_analysis import summarize_binary_frame_classes, summarize_live_transports, summarize_probe


def test_safe_url_removes_query_and_fragment() -> None:
    assert safe_url("wss://example.test/socket?token=secret&x=1#fragment") == (
        "wss://example.test/socket"
    )


def test_payload_metadata_contains_shape_not_values() -> None:
    marker = "SENSITIVE_VALUE_MARKER"
    payload = json.dumps({
        "round": {"id": marker, "multiplier": 2.5},
        "authorization": marker,
        "nested": [{"sessionToken": marker, "status": "complete"}],
    })
    result = payload_metadata(payload)
    serialized = json.dumps(result, sort_keys=True)
    assert marker not in serialized
    assert result["format"] == "json"
    paths = {item["path"] for item in result["shape"]}
    assert "round" in paths
    assert "round.id" in paths
    assert "round.multiplier" in paths
    assert "authorization" not in paths
    assert all("session" not in path.lower() for path in paths)


def test_json_shape_is_bounded_and_ignores_sensitive_keys() -> None:
    value = {
        "cookie": "secret",
        "public": {"token": "secret", "round_id": "r1"},
    }
    paths = {item["path"] for item in json_shape(value)}
    assert "cookie" not in paths
    assert "public.token" not in paths
    assert "public.round_id" in paths


def test_probe_writer_appends_sanitized_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "probe.jsonl"
    writer = ProbeWriter(path, max_bytes=4096)
    assert writer.append({"kind": "fixture", "url": "https://example.test/socket"})
    line = json.loads(path.read_text(encoding="utf-8"))
    assert line["kind"] == "fixture"
    assert line["url"] == "https://example.test/socket"
    assert line["observed_at"].endswith("Z")


def test_probe_writer_enforces_budget(tmp_path: Path) -> None:
    path = tmp_path / "probe.jsonl"
    writer = ProbeWriter(path, max_bytes=100)
    assert writer.append({"kind": "small"}) is True
    before = path.read_bytes()
    assert writer.append({"kind": "large", "value": "x" * 500}) is False
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "changes",
    [
        {"ws_origin": "https://browser.test"},
        {"token": ""},
        {"token": "has spaces"},
        {"target_url": "http://example.test"},
    ],
)
def test_browser_probe_config_rejects_unsafe_values(
    tmp_path: Path, changes: dict[str, str]
) -> None:
    values = {
        "ws_origin": "ws://browserless:3000",
        "token": "synthetic-token",
        "target_url": "https://example.test/aviator",
        "output_path": tmp_path / "probe.jsonl",
    }
    values.update(changes)
    with pytest.raises(ValueError):
        BrowserProbeConfig(**values)



def test_probe_summary_ranks_candidate_paths(tmp_path: Path) -> None:
    path = tmp_path / "probe.jsonl"
    writer = ProbeWriter(path, max_bytes=4096)
    writer.append({
        "kind": "websocket_received",
        "url": "wss://example.test/socket",
        "payload": {
            "format": "json",
            "shape": [
                {"path": "game.round_id", "type": "string"},
                {"path": "game.multiplier", "type": "number"},
                {"path": "other", "type": "string"},
            ],
        },
    })
    result = summarize_probe(path)
    assert len(result) == 1
    assert result[0].kind == "websocket_received"
    assert {path for path, _ in result[0].candidate_paths} == {
        "game.round_id", "game.multiplier"
    }



def test_probe_summary_includes_http_status_counts(tmp_path: Path) -> None:
    path = tmp_path / "probe.jsonl"
    writer = ProbeWriter(path, max_bytes=4096)
    writer.append({
        "kind": "response",
        "url": "https://example.test/game",
        "status": 200,
        "resource_type": "xhr",
        "content_type": "application/json",
    })
    writer.append({
        "kind": "response",
        "url": "https://example.test/game",
        "status": 403,
        "resource_type": "xhr",
        "content_type": "application/json",
    })
    result = summarize_probe(path)
    assert result[0].status_counts == ((200, 1), (403, 1))



def test_live_transport_ranking_prefers_round_like_inbound_stream(tmp_path: Path) -> None:
    path = tmp_path / "probe.jsonl"
    writer = ProbeWriter(path, max_bytes=20000)

    for _ in range(3):
        writer.append({
            "kind": "cdp_websocket_received",
            "url": "wss://example.test/noise",
            "opcode": 1,
            "payload": {"format": "text", "bytes": 12},
        })

    for size in (48, 52, 61, 58, 64):
        writer.append({
            "kind": "cdp_websocket_received",
            "url": "wss://example.test/live",
            "opcode": 1,
            "payload": {
                "format": "json",
                "bytes": size,
                "shape": [
                    {"path": "game.round_id", "type": "string"},
                    {"path": "game.multiplier", "type": "number"},
                    {"path": "game.status", "type": "string"},
                ],
            },
        })

    result = summarize_live_transports(path)
    assert len(result) == 2
    assert result[0].url == "wss://example.test/live"
    assert result[0].received_events == 5
    assert result[0].json_events == 5
    assert result[0].distinct_sizes == 5
    assert {path for path, _ in result[0].candidate_paths} == {
        "game.round_id",
        "game.multiplier",
        "game.status",
    }


def test_live_transport_summary_ignores_non_live_events(tmp_path: Path) -> None:
    path = tmp_path / "probe.jsonl"
    writer = ProbeWriter(path, max_bytes=4096)
    writer.append({
        "kind": "response",
        "url": "https://example.test/api",
        "status": 200,
        "payload": {"format": "json", "bytes": 20, "shape": []},
    })
    assert summarize_live_transports(path) == ()



def test_binary_fingerprint_is_coarse_and_value_free() -> None:
    raw = bytes(range(64))
    result = binary_fingerprint(raw)
    assert set(result) == {
        "entropy_bucket",
        "printable_ratio_bucket",
        "zero_ratio_bucket",
        "high_bit_ratio_bucket",
        "unique_byte_bucket",
    }
    assert result["unique_byte_bucket"] % 8 == 0
    serialized = json.dumps(result, sort_keys=True)
    assert raw.hex() not in serialized


def test_payload_metadata_adds_binary_fingerprint() -> None:
    result = payload_metadata(b"\x00\x01\x02\x03" * 8)
    assert result["format"] == "binary"
    assert "binary_fingerprint" in result


def test_live_transport_summary_aggregates_binary_fingerprints(tmp_path: Path) -> None:
    path = tmp_path / "probe.jsonl"
    writer = ProbeWriter(path, max_bytes=10000)
    for payload in (b"\x00\x01\x02\x03" * 8, b"\x10\x11\x12\x13" * 8):
        writer.append({
            "kind": "cdp_websocket_received",
            "url": "wss://example.test/binary",
            "opcode": 2,
            "payload": payload_metadata(payload),
        })
    result = summarize_live_transports(path)
    assert len(result) == 1
    candidate = result[0]
    assert candidate.binary_events == 2
    assert candidate.entropy_buckets
    assert candidate.unique_byte_buckets



def test_binary_frame_structure_discovery_clusters_by_size_and_timing(tmp_path: Path) -> None:
    path = tmp_path / "probe.jsonl"
    rows = [
        ("2026-10-03T00:00:00.000000Z", 80),
        ("2026-10-03T00:00:00.100000Z", 82),
        ("2026-10-03T00:00:00.200000Z", 84),
        ("2026-10-03T00:00:01.000000Z", 600),
        ("2026-10-03T00:00:03.000000Z", 620),
    ]
    for observed_at, size in rows:
        payload = payload_metadata(bytes([0, 1, 2, 3]) * (size // 4))
        document = {
            "observed_at": observed_at,
            "kind": "cdp_websocket_received",
            "url": "cdp://websocket/test",
            "opcode": 2,
            "payload": payload,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(document) + "\n")

    classes = summarize_binary_frame_classes(path)
    assert len(classes) == 2
    assert classes[0].size_bucket == "64-127"
    assert classes[0].events == 3
    assert classes[0].median_gap_ms == pytest.approx(100.0, abs=0.1)
    assert classes[1].size_bucket == "512-1023"
    assert classes[1].events == 2
    assert classes[1].median_gap_ms == pytest.approx(2000.0, abs=0.1)


def test_binary_frame_structure_discovery_ignores_nonbinary(tmp_path: Path) -> None:
    path = tmp_path / "probe.jsonl"
    writer = ProbeWriter(path, max_bytes=4096)
    writer.append({
        "kind": "cdp_websocket_received",
        "url": "wss://example.test/text",
        "opcode": 1,
        "payload": {"format": "text", "bytes": 5},
    })
    assert summarize_binary_frame_classes(path) == ()

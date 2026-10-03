"""Phase 9 browser-probe tests use synthetic payloads only."""

import json
from pathlib import Path

import pytest

from collectors.browser_probe import BrowserProbeConfig
from collectors.network_probe import ProbeWriter, binary_fingerprint, json_shape, payload_metadata, safe_url
from collectors.probe_analysis import extract_dom_completed_rounds, summarize_binary_frame_classes, summarize_live_transports, summarize_marker_class_stats, summarize_marker_correlations, summarize_marker_signature_stats, summarize_probe


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
    assert writer.can_append() is True
    assert writer.append({"kind": "small"}) is True
    before = path.read_bytes()
    assert writer.append({"kind": "large", "value": "x" * 500}) is False
    assert path.read_bytes() == before


def test_probe_writer_reports_not_writable_at_capacity(tmp_path: Path) -> None:
    path = tmp_path / "probe.jsonl"
    path.write_bytes(b"x" * 100)
    writer = ProbeWriter(path, max_bytes=100)
    assert writer.can_append() is False


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



def test_marker_correlation_counts_nearby_binary_classes(tmp_path: Path) -> None:
    probe = tmp_path / "probe.jsonl"
    markers = tmp_path / "markers.jsonl"

    probe_rows = [
        {
            "observed_at": "2026-10-03T00:00:00.500000Z",
            "kind": "cdp_websocket_received",
            "url": "cdp://websocket/test",
            "opcode": 2,
            "payload": payload_metadata(b"\x00\x01\x02\x03" * 20),
        },
        {
            "observed_at": "2026-10-03T00:00:10.000000Z",
            "kind": "cdp_websocket_received",
            "url": "cdp://websocket/test",
            "opcode": 2,
            "payload": payload_metadata(b"\x00\x01\x02\x03" * 40),
        },
    ]
    marker_rows = [
        {
            "observed_at": "2026-10-03T00:00:00.000000Z",
            "kind": "visual_marker",
            "event": "round_start",
        }
    ]
    probe.write_text("\n".join(json.dumps(row) for row in probe_rows) + "\n", encoding="utf-8")
    markers.write_text("\n".join(json.dumps(row) for row in marker_rows) + "\n", encoding="utf-8")

    result = summarize_marker_correlations(probe, markers, window_seconds=1.0)
    assert len(result) == 1
    assert result[0].event == "round_start"
    assert result[0].markers == 1
    assert result[0].nearby_classes[0][1] == "received:64-127"
    assert result[0].nearby_classes[0][2] == 1


def test_marker_correlation_returns_empty_without_marker_file(tmp_path: Path) -> None:
    probe = tmp_path / "probe.jsonl"
    probe.write_text("", encoding="utf-8")
    assert summarize_marker_correlations(probe, tmp_path / "missing.jsonl") == ()



def test_marker_class_stats_normalize_background_rate(tmp_path: Path) -> None:
    probe = tmp_path / "probe.jsonl"
    markers = tmp_path / "markers.jsonl"

    rows = []
    # High-rate background class across ten seconds.
    for index in range(100):
        rows.append({
            "observed_at": f"2026-10-03T00:00:{index / 10:04.1f}Z",
            "kind": "cdp_websocket_received",
            "url": "cdp://websocket/test",
            "opcode": 2,
            "payload": payload_metadata(b"x" * 40),
        })
    # Sparse class appears right after both marked starts.
    for stamp in ("2026-10-03T00:00:02.100000Z", "2026-10-03T00:00:07.100000Z"):
        rows.append({
            "observed_at": stamp,
            "kind": "cdp_websocket_received",
            "url": "cdp://websocket/test",
            "opcode": 2,
            "payload": payload_metadata(b"x" * 80),
        })

    def sort_key(row: dict[str, object]) -> str:
        return str(row["observed_at"])

    probe.write_text(
        "\n".join(json.dumps(row) for row in sorted(rows, key=sort_key)) + "\n",
        encoding="utf-8",
    )
    marker_rows = [
        {"observed_at": "2026-10-03T00:00:02.000000Z", "kind": "visual_marker", "event": "round_start"},
        {"observed_at": "2026-10-03T00:00:07.000000Z", "kind": "visual_marker", "event": "round_start"},
    ]
    markers.write_text("\n".join(json.dumps(row) for row in marker_rows) + "\n", encoding="utf-8")

    stats = summarize_marker_class_stats(probe, markers, window_seconds=0.25)
    sparse = next(item for item in stats if item.frame_class == "received:64-127")
    assert sparse.hits == 2
    assert sparse.hit_rate == 1.0
    assert sparse.enrichment > 5.0
    assert sparse.median_nearest_offset_ms == pytest.approx(100.0, abs=1.0)


def test_marker_class_stats_returns_empty_without_markers(tmp_path: Path) -> None:
    probe = tmp_path / "probe.jsonl"
    probe.write_text("", encoding="utf-8")
    assert summarize_marker_class_stats(probe, tmp_path / "missing.jsonl") == ()



def test_marker_signature_stats_separate_fingerprint_patterns(tmp_path: Path) -> None:
    probe = tmp_path / "probe.jsonl"
    markers = tmp_path / "markers.jsonl"

    rows = []
    # Background signature, frequent and unrelated.
    for index in range(40):
        rows.append({
            "observed_at": f"2026-10-03T00:00:{index / 4:06.3f}Z",
            "kind": "cdp_websocket_received",
            "url": "cdp://websocket/test",
            "opcode": 2,
            "payload": payload_metadata(b"\x00\x01\x02\x03" * 20),
        })
    # Distinct signature appearing near both starts.
    special = payload_metadata(bytes(range(80)))
    for stamp in ("2026-10-03T00:00:02.100000Z", "2026-10-03T00:00:07.100000Z"):
        rows.append({
            "observed_at": stamp,
            "kind": "cdp_websocket_received",
            "url": "cdp://websocket/test",
            "opcode": 2,
            "payload": special,
        })

    probe.write_text(
        "\n".join(json.dumps(row) for row in sorted(rows, key=lambda r: str(r["observed_at"]))) + "\n",
        encoding="utf-8",
    )
    marker_rows = [
        {"observed_at": "2026-10-03T00:00:02.000000Z", "kind": "visual_marker", "event": "round_start"},
        {"observed_at": "2026-10-03T00:00:07.000000Z", "kind": "visual_marker", "event": "round_start"},
    ]
    markers.write_text("\n".join(json.dumps(row) for row in marker_rows) + "\n", encoding="utf-8")

    stats = summarize_marker_signature_stats(
        probe, markers, window_seconds=0.25, min_total_frames=2
    )
    assert stats
    best = next(item for item in stats if item.hits == 2 and item.hit_rate == 1.0)
    assert "received:64-127" in best.signature
    assert best.enrichment > 5.0
    assert best.median_nearest_offset_ms == pytest.approx(100.0, abs=1.0)


def test_marker_signature_stats_returns_empty_without_marker_file(tmp_path: Path) -> None:
    probe = tmp_path / "probe.jsonl"
    probe.write_text("", encoding="utf-8")
    assert summarize_marker_signature_stats(probe, tmp_path / "missing.jsonl") == ()



def test_dom_history_extractor_detects_left_edge_insert(tmp_path: Path) -> None:
    path = tmp_path / "dom.jsonl"
    previous = [
        {"multiplier": value, "x_bucket": x, "y_bucket": 50, "width_bucket": 40, "height_bucket": 20}
        for x, value in zip((10, 60, 110, 150, 200, 250, 290), ("1.07", "2.41", "1.64", "17.42", "1.33", "1.17", "4.27"))
    ]
    current = [
        {"multiplier": value, "x_bucket": x, "y_bucket": 50, "width_bucket": 40, "height_bucket": 20}
        for x, value in zip((10, 60, 110, 150, 200, 250, 290, 340), ("44.88", "1.07", "2.41", "1.64", "17.42", "1.33", "1.17", "4.27"))
    ]
    rows = [
        {"kind": "dom_multiplier_snapshot", "observed_at": "2026-10-03T00:00:00Z", "candidates": previous},
        {"kind": "dom_multiplier_snapshot", "observed_at": "2026-10-03T00:00:01Z", "candidates": current},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    result = extract_dom_completed_rounds(path)
    assert len(result) == 1
    assert result[0].multiplier == "44.88"
    assert result[0].edge == "left"
    assert result[0].overlap >= 5


def test_dom_history_extractor_ignores_wide_scrolling_values(tmp_path: Path) -> None:
    path = tmp_path / "dom.jsonl"
    row = [
        {"multiplier": str(1 + index / 10), "x_bucket": 270, "y_bucket": 700 + index * 40, "width_bucket": 120, "height_bucket": 20}
        for index in range(10)
    ]
    path.write_text(
        json.dumps({"kind": "dom_multiplier_snapshot", "observed_at": "2026-10-03T00:00:00Z", "candidates": row}) + "\n",
        encoding="utf-8",
    )
    assert extract_dom_completed_rounds(path) == ()


def test_dom_history_extractor_requires_sequence_shift(tmp_path: Path) -> None:
    path = tmp_path / "dom.jsonl"
    same = [
        {"multiplier": value, "x_bucket": x, "y_bucket": 50, "width_bucket": 40, "height_bucket": 20}
        for x, value in zip((10, 60, 110, 150, 200, 250), ("1.07", "2.41", "1.64", "17.42", "1.33", "1.17"))
    ]
    rows = [
        {"kind": "dom_multiplier_snapshot", "observed_at": "2026-10-03T00:00:00Z", "candidates": same},
        {"kind": "dom_multiplier_snapshot", "observed_at": "2026-10-03T00:00:01Z", "candidates": same},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    assert extract_dom_completed_rounds(path) == ()

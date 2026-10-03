"""Phase 9 browser-probe tests use synthetic payloads only."""

import json
from pathlib import Path

import pytest

from collectors.browser_probe import BrowserProbeConfig
from collectors.network_probe import ProbeWriter, json_shape, payload_metadata, safe_url
from collectors.probe_analysis import summarize_probe


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

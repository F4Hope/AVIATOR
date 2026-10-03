"""Sanitized network-observation primitives for authenticated browser discovery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


MAX_PROBE_BYTES = 256 * 1024 * 1024
MAX_JSON_PATHS = 200
MAX_DEPTH = 8
SENSITIVE_KEY_PARTS = (
    "password", "passwd", "authorization", "cookie", "token", "secret",
    "session", "credential", "apikey", "api_key",
)

GAME_NUMERIC_KEYS = frozenset(
    {
        "multiplier",
        "crashmultiplier",
        "crashpoint",
        "coefficient",
        "coef",
        "payoutmultiplier",
        "finalmultiplier",
        "resultmultiplier",
        "nextmultiplier",
    }
)
GAME_IDENTIFIER_KEYS = frozenset(
    {
        "roundid",
        "nextroundid",
        "gameid",
        "gamehash",
        "hash",
        "commitment",
        "serverseedhash",
        "clientseedhash",
    }
)
CONTROL_STRING_KEYS = frozenset(
    {
        "event",
        "eventtype",
        "type",
        "state",
        "status",
        "phase",
        "action",
        "method",
        "command",
        "cmd",
        "topic",
        "messageType".lower(),
        "gamestate",
        "roundstate",
    }
)
MAX_GAME_FIELDS = 64
MAX_CONTROL_FIELDS = 64


def safe_url(value: str) -> str:
    """Strip query strings and fragments so tokens cannot be persisted in URLs."""
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _sensitive_key(key: str) -> bool:
    normalized = "".join(character.lower() for character in key if character.isalnum() or character == "_")
    return any(part.replace("_", "") in normalized.replace("_", "") for part in SENSITIVE_KEY_PARTS)


def json_shape(value: Any) -> tuple[dict[str, str], ...]:
    """Return JSON key paths and value types without preserving payload values."""
    paths: list[dict[str, str]] = []

    def visit(item: Any, path: str, depth: int) -> None:
        if len(paths) >= MAX_JSON_PATHS or depth > MAX_DEPTH:
            return
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or _sensitive_key(key):
                    continue
                child_path = f"{path}.{key}" if path else key
                paths.append({"path": child_path, "type": type_name(child)})
                visit(child, child_path, depth + 1)
        elif isinstance(item, list) and item:
            child_path = f"{path}[]" if path else "[]"
            paths.append({"path": child_path, "type": type_name(item[0])})
            visit(item[0], child_path, depth + 1)

    visit(value, "", 0)
    return tuple(paths)


def _normalized_key(value: str) -> str:
    return "".join(character.lower() for character in value if character.isalnum())


def game_field_metadata(value: Any) -> tuple[dict[str, object], ...]:
    """Extract only allowlisted game-integrity fields from decoded JSON.

    Numeric outcome-like fields retain their numeric value so a pre-round
    disclosure can be tested directly. Round/hash/commitment identifiers are
    irreversibly hashed before persistence. Authentication/session/token fields
    remain excluded by the existing sensitive-key policy.
    """
    fields: list[dict[str, object]] = []

    def visit(item: Any, path: tuple[str, ...], depth: int) -> None:
        if len(fields) >= MAX_GAME_FIELDS or depth > MAX_DEPTH:
            return
        if isinstance(item, dict):
            for key, child in item.items():
                if (
                    len(fields) >= MAX_GAME_FIELDS
                    or not isinstance(key, str)
                    or _sensitive_key(key)
                ):
                    continue
                normalized = _normalized_key(key)
                child_path = (*path, normalized or "field")
                path_text = ".".join(child_path)

                if normalized in GAME_NUMERIC_KEYS and not isinstance(child, bool):
                    numeric: float | None = None
                    if isinstance(child, (int, float)):
                        numeric = float(child)
                    elif isinstance(child, str):
                        try:
                            numeric = float(child.strip().rstrip("xX"))
                        except ValueError:
                            numeric = None
                    if (
                        numeric is not None
                        and math.isfinite(numeric)
                        and 0.0 <= numeric <= 1_000_000.0
                    ):
                        fields.append(
                            {
                                "path": path_text,
                                "kind": "numeric",
                                "value": numeric,
                            }
                        )

                elif normalized in GAME_IDENTIFIER_KEYS and isinstance(
                    child, (str, int, float)
                ) and not isinstance(child, bool):
                    raw = str(child).encode("utf-8", errors="replace")
                    fields.append(
                        {
                            "path": path_text,
                            "kind": "identifier_hash",
                            "sha256_24": hashlib.sha256(raw).hexdigest()[:24],
                            "length": len(raw),
                        }
                    )

                visit(child, child_path, depth + 1)

        elif isinstance(item, list):
            for child in item[:20]:
                visit(child, (*path, "[]"), depth + 1)

    visit(value, (), 0)
    return tuple(fields)



def categorical_field_metadata(value: Any) -> tuple[dict[str, object], ...]:
    """Extract a small allowlist of non-sensitive protocol-state strings.

    Values are retained only for short, low-complexity categorical strings under
    explicitly allowlisted keys such as event/state/phase/status. Long values,
    URLs, whitespace-rich text, and token-like strings are excluded.
    """
    fields: list[dict[str, object]] = []

    def safe_category(raw: object) -> str | None:
        if not isinstance(raw, str):
            return None
        text = raw.strip()
        if not 1 <= len(text) <= 48:
            return None
        if any(character.isspace() for character in text):
            return None
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-/")
        if any(character not in allowed for character in text):
            return None
        if "://" in text or "/" in text:
            return None
        compact = "".join(character.lower() for character in text if character.isalnum())
        if len(compact) >= 20 and all(character in "0123456789abcdef" for character in compact):
            return None
        return text.lower()

    def visit(item: Any, path: tuple[str, ...], depth: int) -> None:
        if len(fields) >= MAX_CONTROL_FIELDS or depth > MAX_DEPTH:
            return
        if isinstance(item, dict):
            for key, child in item.items():
                if (
                    len(fields) >= MAX_CONTROL_FIELDS
                    or not isinstance(key, str)
                    or _sensitive_key(key)
                ):
                    continue
                normalized = _normalized_key(key)
                child_path = (*path, normalized or "field")
                if normalized in CONTROL_STRING_KEYS:
                    category = safe_category(child)
                    if category is not None:
                        fields.append(
                            {
                                "path": ".".join(child_path),
                                "value": category,
                            }
                        )
                visit(child, child_path, depth + 1)
        elif isinstance(item, list):
            for child in item[:20]:
                visit(child, (*path, "[]"), depth + 1)

    visit(value, (), 0)
    return tuple(fields)


def type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return "unknown"




def _bucket_ratio(value: float) -> float:
    """Bucket a ratio to 5% increments to avoid preserving exact byte distributions."""
    return round(round(max(0.0, min(1.0, value)) / 0.05) * 0.05, 2)


def binary_fingerprint(raw: bytes) -> dict[str, object]:
    """Return coarse, non-reversible structural metadata for a binary frame."""
    if not raw:
        return {
            "entropy_bucket": 0.0,
            "printable_ratio_bucket": 0.0,
            "zero_ratio_bucket": 0.0,
            "high_bit_ratio_bucket": 0.0,
            "unique_byte_bucket": 0,
        }

    counts = Counter(raw)
    length = len(raw)
    entropy = -sum(
        (count / length) * math.log2(count / length)
        for count in counts.values()
    )
    printable = sum(1 for value in raw if 32 <= value <= 126)
    zeroes = counts.get(0, 0)
    high_bit = sum(count for value, count in counts.items() if value >= 128)

    # Entropy is rounded to quarter-bit increments; unique-byte cardinality is
    # rounded up to an 8-value bucket. Neither exposes byte values or prefixes.
    return {
        "entropy_bucket": round(round(entropy / 0.25) * 0.25, 2),
        "printable_ratio_bucket": _bucket_ratio(printable / length),
        "zero_ratio_bucket": _bucket_ratio(zeroes / length),
        "high_bit_ratio_bucket": _bucket_ratio(high_bit / length),
        "unique_byte_bucket": min(256, ((len(counts) + 7) // 8) * 8),
    }


def payload_metadata(payload: str | bytes) -> dict[str, object]:
    """Fingerprint a frame and expose sanitized JSON/game-integrity metadata."""
    raw = payload.encode("utf-8", errors="replace") if isinstance(payload, str) else bytes(payload)
    result: dict[str, object] = {
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "format": "binary" if isinstance(payload, bytes) else "text",
    }
    if isinstance(payload, bytes):
        result["binary_fingerprint"] = binary_fingerprint(raw)
        return result

    try:
        parsed = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return result
    result["format"] = "json"
    result["root_type"] = type_name(parsed)
    result["shape"] = list(json_shape(parsed))
    game_fields = game_field_metadata(parsed)
    if game_fields:
        result["game_fields"] = list(game_fields)
    categorical_fields = categorical_field_metadata(parsed)
    if categorical_fields:
        result["categorical_fields"] = list(categorical_fields)
    return result


@dataclass(slots=True)
class ProbeWriter:
    """Append bounded, sanitized JSONL observations under data/raw."""

    path: Path
    max_bytes: int = MAX_PROBE_BYTES

    def can_append(self) -> bool:
        if type(self.max_bytes) is not int or self.max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer.")
        try:
            return not self.path.exists() or self.path.stat().st_size < self.max_bytes
        except OSError:
            return False

    def append(self, event: dict[str, object]) -> bool:
        if type(self.max_bytes) is not int or self.max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.can_append():
            return False
        document = {
            "observed_at": datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
            **event,
        }
        line = (json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
        if self.path.exists() and self.path.stat().st_size + len(line) > self.max_bytes:
            return False
        with self.path.open("ab") as handle:
            handle.write(line)
            handle.flush()
        return True


def read_recent_probe_events(path: Path, limit: int = 100) -> list[dict[str, object]]:
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500.")
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    result: list[dict[str, object]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            result.append(value)
    return result

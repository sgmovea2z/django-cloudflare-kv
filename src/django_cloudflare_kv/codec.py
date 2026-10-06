"""Versioned serialization envelope and logical expiry helpers."""

from __future__ import annotations

import base64
import binascii
import json
import math
import pickle
import time
from dataclasses import dataclass
from typing import Final, cast

from .errors import CacheSerializationError

FORMAT_VERSION: Final = 1
PICKLE_PROTOCOL: Final = 4
MAX_ENVELOPE_BYTES: Final = 25 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class DecodedValue[T = object]:
    """A successfully decoded cached value, including a cached ``None``."""

    value: T
    expires_at: float | None = None


def encode_value(value: object, *, expires_at: float | None) -> str:
    """Serialize a Python cache value into the exact v1 text envelope."""
    try:
        return _encode_envelope(value, expires_at)
    except (OverflowError, TypeError, ValueError, pickle.PickleError) as error:
        message = "cache value could not be serialized"
        raise CacheSerializationError(message) from error


def _encode_envelope(value: object, expires_at: float | None) -> str:
    """Validate encoding inputs and produce the compact v1 text envelope."""
    if expires_at is not None and type(expires_at) not in (int, float):
        message = "expires_at must be a finite number or null"
        raise TypeError(message)
    if expires_at is not None and not math.isfinite(expires_at):
        message = "expires_at must be finite"
        raise ValueError(message)
    payload = base64.b64encode(pickle.dumps(value, protocol=PICKLE_PROTOCOL))
    envelope = json.dumps(
        {
            "format": FORMAT_VERSION,
            "expires_at": expires_at,
            "payload": payload.decode("ascii"),
        },
        allow_nan=False,
        separators=(",", ":"),
    )
    if len(envelope.encode("utf-8")) > MAX_ENVELOPE_BYTES:
        message = "serialized cache value exceeds the KV size limit"
        raise ValueError(message)
    return envelope


def decode_value(
    envelope_text: str, *, now: float | None = None
) -> DecodedValue[object] | None:
    """Decode a v1 envelope, returning ``None`` only for logical expiry."""
    current_time = time.time() if now is None else now
    try:
        fields = _parse_envelope_fields(envelope_text)
        expires_at = _parse_expiration(fields["expires_at"])
        payload = _parse_payload(fields["payload"])
    except (
        UnicodeEncodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        binascii.Error,
        OverflowError,
    ) as error:
        message = "cache value envelope is invalid"
        raise CacheSerializationError(message) from error

    if is_expired(expires_at, now=current_time):
        return None
    try:
        # Pickle is intentionally dynamic; expose its result only as opaque data.
        value = cast("object", pickle.loads(payload))  # noqa: S301 -- serialized by encode_value
        return DecodedValue(value, expires_at)
    except Exception as error:
        message = "cache value payload is invalid"
        raise CacheSerializationError(message) from error


def _parse_envelope_fields(envelope_text: str) -> dict[str, object]:
    """Parse and validate the v1 JSON envelope fields."""
    parsed = cast(
        "object",
        json.loads(
            envelope_text,
            object_pairs_hook=_unique_json_fields,
            parse_constant=_reject_json_constant,
        ),
    )
    if not isinstance(parsed, dict):
        message = "envelope must be a JSON object"
        raise TypeError(message)
    fields = cast("dict[str, object]", parsed)
    if set(fields) != {"format", "expires_at", "payload"}:
        message = "envelope fields do not match v1"
        raise ValueError(message)
    version = fields["format"]
    if type(version) is not int or version != FORMAT_VERSION:
        message = "unsupported envelope format"
        raise ValueError(message)
    return fields


def _parse_expiration(value: object) -> float | None:
    """Validate the optional logical expiry from untrusted JSON."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        message = "expires_at must be a finite number or null"
        raise TypeError(message)
    if not math.isfinite(value):
        message = "expires_at must be a finite number or null"
        raise TypeError(message)
    return value


def _parse_payload(value: object) -> bytes:
    """Validate and decode the base64 payload from untrusted JSON."""
    if not isinstance(value, str):
        message = "payload must be an ASCII base64 string"
        raise TypeError(message)
    return base64.b64decode(value.encode("ascii"), validate=True)


def is_expired(expires_at: float | None, *, now: float) -> bool:
    """Return whether a finite logical deadline has been reached."""
    return expires_at is not None and expires_at <= now


def physical_ttl(expires_at: float | None, *, now: float) -> int | None:
    """Return KV's relative TTL, preserving logical lifetime and 60s minimum."""
    if expires_at is None:
        return None
    return max(60, math.ceil(expires_at - now))


def _reject_json_constant(value: str) -> None:
    """Reject JavaScript-style non-finite JSON numbers."""
    message = f"invalid JSON number: {value}"
    raise ValueError(message)


def _unique_json_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate keys instead of silently accepting ambiguous headers."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            message = "duplicate envelope field"
            raise ValueError(message)
        result[key] = value
    return result

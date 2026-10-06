"""Tests for the versioned cache value envelope."""

from __future__ import annotations

import base64
import json
import pickle
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Final, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from django_cloudflare_kv.codec import (
    MAX_ENVELOPE_BYTES,
    decode_value,
    encode_value,
)
from django_cloudflare_kv.errors import CacheBackendError, CacheSerializationError

if TYPE_CHECKING:
    from .fakes import FakeClock

BASE_TIME: Final = 1_700_000_000.0


def _decode_json_object(value: str) -> dict[str, object]:
    """Parse a known test envelope as a string-keyed JSON object."""
    # JSON is runtime-validated by the assertions at each call site.
    return cast("dict[str, object]", json.loads(value))


@pytest.mark.parametrize(
    "value",
    [
        None,
        b"bytes",
        datetime.fromisoformat("2024-01-02T03:04:05"),
        datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC),
        Decimal("123.4500"),
        False,
        0,
        "",
        {"nested": [1, {"value": (None, b"x")}]},
    ],
)
def test_value_round_trips_through_exact_v1_envelope(value: object) -> None:
    """Given any supported Python value, encoding then decoding preserves it."""
    encoded = encode_value(value, expires_at=None)

    assert json.loads(encoded) == {
        "format": 1,
        "expires_at": None,
        "payload": base64.b64encode(pickle.dumps(value, protocol=4)).decode("ascii"),
    }
    decoded = decode_value(encoded)
    assert decoded is not None
    assert decoded.value == value


def test_cached_none_is_distinct_from_expiry(fake_clock: FakeClock) -> None:
    encoded = encode_value(None, expires_at=None)

    decoded = decode_value(encoded, now=fake_clock())

    assert decoded is not None
    assert decoded.value is None


@given(
    st.recursive(
        st.none() | st.booleans() | st.integers() | st.text() | st.binary(),
        lambda children: st.lists(children) | st.dictionaries(st.text(), children),
        max_leaves=20,
    )
)
def test_generated_nested_values_round_trip(value: object) -> None:
    decoded = decode_value(encode_value(value, expires_at=None))

    assert decoded is not None
    assert decoded.value == value


def test_envelope_has_exactly_required_fields() -> None:
    """Given an encoded value, its JSON object contains only the v1 fields."""
    envelope = _decode_json_object(encode_value("item", expires_at=BASE_TIME))

    assert set(envelope) == {"format", "expires_at", "payload"}
    assert type(envelope["format"]) is int


def test_boolean_expiry_is_not_serialized_as_a_deadline() -> None:
    with pytest.raises(CacheSerializationError) as error:
        _ = encode_value("item", expires_at=True)

    assert isinstance(error.value.__cause__, TypeError)


@pytest.mark.parametrize(
    "raw",
    [
        "{",
        '{"expires_at":null,"payload":""}',
        '{"format":2,"expires_at":null,"payload":""}',
        '{"format":true,"expires_at":null,"payload":""}',
        '{"format":1.0,"expires_at":null,"payload":""}',
        '{"format":1,"expires_at":null,"payload":"","extra":0}',
        '{"format":1,"payload":""}',
        '{"format":1,"expires_at":NaN,"payload":""}',
        '{"format":1,"expires_at":true,"payload":""}',
        '{"format":1,"expires_at":"42","payload":""}',
        '{"format":1,"expires_at":null,"payload":"é"}',
        '{"format":1,"expires_at":null,"payload":"%%%"}',
        '{"format":1,"expires_at":null,"payload":42}',
        '{"format":1,"format":1,"expires_at":null,"payload":""}',
        '{"format":1,"expires_at":null,"payload":"bm90IHBpY2tsZQ=="}',
    ],
)
def test_malformed_envelopes_raise_chained_serialization_error(raw: str) -> None:
    """Given malformed envelope text, decoding reports a chained public error."""
    with pytest.raises(CacheSerializationError) as error:
        _ = decode_value(raw)

    assert error.value.__cause__ is not None


def test_pickle_payload_uses_protocol_four() -> None:
    """Given a value, encoded payload contains protocol-four pickle bytes."""
    envelope = _decode_json_object(encode_value(["payload"], expires_at=None))
    encoded_payload = envelope["payload"]
    assert isinstance(encoded_payload, str)
    payload = base64.b64decode(encoded_payload.encode("ascii"), validate=True)

    assert payload.startswith(b"\x80\x04")


def test_full_text_size_limit_accepts_exact_boundary() -> None:
    """Given a valid envelope exactly at the limit, encoding accepts it."""
    expires_at = 1
    empty_envelope = json.dumps(
        {"format": 1, "expires_at": expires_at, "payload": ""},
        separators=(",", ":"),
    )
    prefix_bytes = len(empty_envelope.encode("utf-8")) - 2
    base64_target_bytes = MAX_ENVELOPE_BYTES - prefix_bytes
    pickle_target_bytes = base64_target_bytes // 4 * 3
    estimated_length = pickle_target_bytes - 19
    value_length = next(
        length
        for length in range(estimated_length - 32, estimated_length + 33)
        if len(pickle.dumps("x" * length, protocol=4)) == pickle_target_bytes
    )
    value = "x" * value_length
    encoded = encode_value(value, expires_at=expires_at)

    assert len(encoded.encode("utf-8")) == MAX_ENVELOPE_BYTES
    assert len(encoded.encode("utf-8")) == MAX_ENVELOPE_BYTES

    with pytest.raises(CacheSerializationError) as error:
        _ = encode_value("x" * (value_length + 1), expires_at=expires_at)

    assert isinstance(error.value, CacheBackendError)
    assert error.value.__cause__ is not None


def test_full_text_size_limit_rejects_one_byte_over() -> None:
    """Given a value whose envelope exceeds the limit, encoding rejects it."""
    value = "x" * MAX_ENVELOPE_BYTES

    with pytest.raises(CacheSerializationError) as error:
        _ = encode_value(value, expires_at=None)

    assert error.value.__cause__ is not None

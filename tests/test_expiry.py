"""Tests for logical expiry and Cloudflare KV TTL translation."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import pytest

from django_cloudflare_kv.codec import (
    decode_value,
    encode_value,
    is_expired,
    physical_ttl,
)

if TYPE_CHECKING:
    from .fakes import FakeClock


@pytest.mark.parametrize(
    ("timeout", "expected_ttl"),
    [
        (1, 60),
        (59, 60),
        (60, 60),
        (60.25, 61),
        (61, 61),
        (1.25, 60),
        (300, 300),
    ],
)
def test_physical_ttl_never_shortens_logical_lifetime(
    fake_clock: FakeClock, timeout: float, expected_ttl: int
) -> None:
    """Given a future logical deadline, TTL rounds up and honors 60s minimum."""
    deadline = fake_clock() + timeout

    assert physical_ttl(deadline, now=fake_clock()) == expected_ttl


def test_physical_ttl_omitted_for_no_expiry(fake_clock: FakeClock) -> None:
    """Given no deadline, the binding must receive no physical TTL option."""
    assert physical_ttl(None, now=fake_clock()) is None


@pytest.mark.parametrize(
    ("offset", "expired"),
    [(-math.ulp(1000.0), True), (0.0, True), (math.ulp(1000.0), False)],
)
def test_logical_deadline_boundary(
    fake_clock: FakeClock, offset: float, expired: bool
) -> None:
    """Given a deadline around now, expiry is inclusive at the deadline."""
    deadline = fake_clock() + offset

    assert is_expired(deadline, now=fake_clock()) is expired


@pytest.mark.parametrize("timeout", [1, 59, 60, 61, 1.25, 300, None])
def test_decoding_respects_deadline_using_controlled_clock(
    fake_clock: FakeClock, timeout: float | None
) -> None:
    """Given encoded value and clock, decode separates live value from expiry."""
    deadline = None if timeout is None else fake_clock() + timeout
    encoded = encode_value("live", expires_at=deadline)

    result = decode_value(encoded, now=fake_clock())

    assert result is not None
    assert result.value == "live"


def test_expired_value_is_miss_without_mutating_serialized_record(
    fake_clock: FakeClock,
) -> None:
    """Given an expired record, logical check reports miss without cleanup."""
    encoded = encode_value("value", expires_at=fake_clock() + 1)
    fake_clock.advance(1)

    assert decode_value(encoded, now=fake_clock()) is None
    assert encoded == encode_value("value", expires_at=1001.0)


@pytest.mark.parametrize(
    ("offset", "is_hit"),
    [(-math.ulp(1000.0), False), (0.0, False), (math.ulp(1000.0), True)],
)
def test_decode_deadline_hit_boundary(
    fake_clock: FakeClock, offset: float, is_hit: bool
) -> None:
    deadline = fake_clock() + offset
    encoded = encode_value("value", expires_at=deadline)

    decoded = decode_value(encoded, now=fake_clock())

    assert (decoded is not None) is is_hit


@pytest.mark.parametrize("delta", [0.0, -1.0, -1000.0])
def test_expired_deadline_keeps_minimum_physical_ttl(
    fake_clock: FakeClock, delta: float
) -> None:
    assert physical_ttl(fake_clock() + delta, now=fake_clock()) == 60

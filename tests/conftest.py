"""Shared test fixtures."""

from __future__ import annotations

import pytest

from .fakes import FakeBinding, FakeClock


@pytest.fixture
def fake_clock() -> FakeClock:
    """Provide a deterministic clock starting at Unix epoch 1000."""
    return FakeClock(now=1000.0)


@pytest.fixture
def fake_binding(fake_clock: FakeClock) -> FakeBinding:
    """Provide an empty fake binding using the test's controlled clock."""
    return FakeBinding(clock=fake_clock)

"""Test-owned simulation for the Workers KV binding contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Literal

Operation = Literal["get", "put", "delete", "list"]


@dataclass(frozen=True, slots=True)
class Call:
    """One observed binding invocation."""

    operation: Operation
    key: str | None = None
    options: tuple[tuple[str, str | int], ...] = ()


@dataclass(frozen=True, slots=True)
class Failure:
    """An operation-specific failure raised by the fake binding."""

    operation: Operation
    error: Exception


@dataclass(slots=True)
class FakeClock:
    """Mutable clock whose deadline can be moved deterministically."""

    now: float

    def __call__(self) -> float:
        """Return current simulated Unix time."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Advance simulated time by the supplied number of seconds."""
        self.now += seconds


@dataclass(slots=True)
class _Entry:
    value: str
    expires_at: float | None
    visible_at: float


@dataclass(slots=True)
class FakeBinding:
    """In-memory KV fake with deterministic timing and failure controls."""

    clock: FakeClock
    page_size: int = 100
    empty_pages_before_results: int = 0
    visibility_delay: float = 0.0
    calls: list[Call] = field(default_factory=list)
    failures: list[Failure] = field(default_factory=list)
    _entries: dict[str, _Entry] = field(default_factory=dict)
    _empty_pages_remaining: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        self._empty_pages_remaining = self.empty_pages_before_results

    def get(self, key: str) -> str | None:
        """Return a currently visible, unexpired value."""
        self._record("get", key)
        entry = self._entries.get(key)
        if entry is None or entry.visible_at > self.clock():
            return None
        if entry.expires_at is not None and entry.expires_at <= self.clock():
            del self._entries[key]
            return None
        return entry.value

    def put(self, key: str, value: str, *, expiration_ttl: int | None = None) -> None:
        """Store a value with optional relative expiration in seconds."""
        options: tuple[tuple[str, str | int], ...] = (
            () if expiration_ttl is None else (("expirationTtl", expiration_ttl),)
        )
        self._record("put", key, options)
        expires_at = None if expiration_ttl is None else self.clock() + expiration_ttl
        self._entries[key] = _Entry(
            value=value,
            expires_at=expires_at,
            visible_at=self.clock() + self.visibility_delay,
        )

    def delete(self, key: str) -> None:
        """Delete a value if present."""
        self._record("delete", key)
        _ = self._entries.pop(key, None)

    def list(
        self, prefix: str, *, cursor: str | None = None
    ) -> tuple[tuple[str, ...], str | None, bool]:
        """Return one prefix page as keys, next cursor, and completion flag."""
        options = () if cursor is None else (("cursor", cursor),)
        self._record("list", prefix, options)
        if self._empty_pages_remaining > 0:
            self._empty_pages_remaining -= 1
            return (), cursor or "0", False

        keys: Final[tuple[str, ...]] = tuple(
            sorted(key for key in self._entries if key.startswith(prefix))
        )
        offset = int(cursor or "0")
        page = keys[offset : offset + self.page_size]
        next_offset = offset + len(page)
        return page, str(next_offset), next_offset >= len(keys)

    def fail_next(self, operation: Operation, error: Exception) -> None:
        """Inject a specific exception into the next matching call."""
        self.failures.append(Failure(operation=operation, error=error))

    def _record(
        self,
        operation: Operation,
        key: str | None,
        options: tuple[tuple[str, str | int], ...] = (),
    ) -> None:
        self.calls.append(Call(operation=operation, key=key, options=options))
        failure_index = next(
            (
                index
                for index, failure in enumerate(self.failures)
                if failure.operation == operation
            ),
            None,
        )
        if failure_index is not None:
            failure = self.failures.pop(failure_index)
            raise failure.error

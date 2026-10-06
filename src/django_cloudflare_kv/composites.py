"""Django composite cache operations over native asynchronous primitives."""

from __future__ import annotations

import time
from typing import Final, cast, override

from django.core.cache.backends import base as django_cache_base
from django.core.cache.backends.base import BaseCache

from .codec import DecodedValue, decode_value, encode_value, physical_ttl
from .errors import CacheOperationLimitError
from .keys import make_physical_key
from .runtime import KVNamespace, Runtime
from .runtime import run_sync as _run_sync

_DEFAULT_TIMEOUT: Final[int | float | None] = cast(
    "int | float | None", django_cache_base.DEFAULT_TIMEOUT
)


class CompositeMethods(BaseCache):
    """Native async implementations of Django's composite cache methods."""

    location: str = ""
    version: int = 1
    cache_namespace: str = ""
    operation_limit: int = 900
    _MISSING_VALUE_MESSAGE: Final = "cache key not found"

    def _physical_key(self, key: str, version: int | None = None) -> str:
        return make_physical_key(self, key, version)

    def _consume(self, budget: list[int], count: int = 1) -> None:
        self._check_budget(budget, count)
        budget[0] += count

    def _check_budget(self, budget: list[int], count: int) -> None:
        if budget[0] + count > self.operation_limit:
            message = "cache operation limit exceeded"
            raise CacheOperationLimitError(message)

    async def _read_with_binding(
        self, binding: KVNamespace, key: str, budget: list[int]
    ) -> DecodedValue | object:
        self._consume(budget)
        raw = await binding.get(key)
        if raw is None:
            return self._missing_key
        decoded = decode_value(raw, now=time.time())
        return self._missing_key if decoded is None else decoded

    @override
    async def aget_or_set(
        self,
        key: str,
        default: object,
        timeout: float | None = _DEFAULT_TIMEOUT,
        version: int | None = None,
    ) -> object:
        """Get a value or best-effort add a value from a synchronous factory."""
        physical_key = self._physical_key(key, version)
        budget = [0]
        binding = Runtime().resolve_binding(self.location)
        value = await self._read_with_binding(binding, physical_key, budget)
        if isinstance(value, DecodedValue):
            return cast("object", value.value)

        produced = default() if callable(default) else default
        self._check_budget(budget, 3)
        stored = await self._read_with_binding(binding, physical_key, budget)
        if stored is self._missing_key:
            expires_at = self.get_backend_timeout(timeout)
            now = time.time()
            if expires_at is None or expires_at > now:
                serialized = encode_value(produced, expires_at=expires_at)
                ttl = physical_ttl(expires_at, now=now)
                self._consume(budget)
                if ttl is None:
                    await binding.put(physical_key, serialized)
                else:
                    await binding.put(physical_key, serialized, expiration_ttl=ttl)
        result = await self._read_with_binding(binding, physical_key, budget)
        if isinstance(result, DecodedValue):
            return cast("object", result.value)
        return produced

    @override
    async def aincr(self, key: str, delta: int = 1, version: int | None = None) -> int:
        """Apply non-atomic Python addition while preserving the deadline."""
        return await self._mutate_counter(key, delta, version)

    @override
    async def adecr(self, key: str, delta: int = 1, version: int | None = None) -> int:
        """Apply non-atomic Python subtraction while preserving the deadline."""
        return await self._mutate_counter(key, -delta, version)

    async def _mutate_counter(self, key: str, delta: int, version: int | None) -> int:
        physical_key = self._physical_key(key, version)
        budget = [0]
        binding = Runtime().resolve_binding(self.location)
        stored = await self._read_with_binding(binding, physical_key, budget)
        if stored is self._missing_key:
            raise ValueError(self._MISSING_VALUE_MESSAGE)
        if not isinstance(stored, DecodedValue):
            raise TypeError
        # Django's API declares int while preserving Python's value arithmetic.
        new_value = cast("int", stored.value) + delta
        self._check_budget(budget, 1)
        serialized = encode_value(new_value, expires_at=stored.expires_at)
        ttl = physical_ttl(stored.expires_at, now=time.time())
        self._consume(budget)
        if ttl is None:
            await binding.put(physical_key, serialized)
        else:
            await binding.put(physical_key, serialized, expiration_ttl=ttl)
        return new_value

    @override
    async def aincr_version(
        self, key: str, delta: int = 1, version: int | None = None
    ) -> int:
        """Move a live key to a new version, then delete the old key."""
        old_version = self.version if version is None else version
        new_version = old_version + delta
        old_key = self._physical_key(key, old_version)
        new_key = self._physical_key(key, new_version)
        budget = [0]
        binding = Runtime().resolve_binding(self.location)
        stored = await self._read_with_binding(binding, old_key, budget)
        if stored is self._missing_key:
            raise ValueError(self._MISSING_VALUE_MESSAGE)
        if not isinstance(stored, DecodedValue):
            raise TypeError
        self._check_budget(budget, 2)
        serialized = encode_value(
            cast("object", stored.value), expires_at=stored.expires_at
        )
        ttl = physical_ttl(stored.expires_at, now=time.time())
        self._consume(budget)
        if ttl is None:
            await binding.put(new_key, serialized)
        else:
            await binding.put(new_key, serialized, expiration_ttl=ttl)
        self._consume(budget)
        await binding.delete(old_key)
        return new_version

    @override
    async def adecr_version(
        self, key: str, delta: int = 1, version: int | None = None
    ) -> int:
        """Move a live key to a lower version."""
        return await self.aincr_version(key, -delta, version)

    @override
    def get_or_set(
        self,
        key: str,
        default: object,
        timeout: float | None = _DEFAULT_TIMEOUT,
        version: int | None = None,
    ) -> object:
        """Synchronously bridge the complete async get-or-set operation."""
        return _run_sync(self.aget_or_set(key, default, timeout, version))

    @override
    def incr(self, key: str, delta: int = 1, version: int | None = None) -> int:
        """Synchronously bridge one complete increment coroutine."""
        return _run_sync(self.aincr(key, delta, version))

    @override
    def decr(self, key: str, delta: int = 1, version: int | None = None) -> int:
        """Synchronously bridge one complete decrement coroutine."""
        return _run_sync(self.adecr(key, delta, version))

    @override
    def incr_version(self, key: str, delta: int = 1, version: int | None = None) -> int:
        """Synchronously bridge one complete version move."""
        return _run_sync(self.aincr_version(key, delta, version))

    @override
    def decr_version(self, key: str, delta: int = 1, version: int | None = None) -> int:
        """Synchronously bridge one complete decrement-version coroutine."""
        return _run_sync(self.adecr_version(key, delta, version))

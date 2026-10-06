"""Configuration surface for the Cloudflare Workers KV cache backend."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Final, cast, override

from django.core.cache.backends import base as django_cache_base
from django.core.exceptions import ImproperlyConfigured

from .batches import BatchCacheMixin
from .codec import DecodedValue, decode_value, encode_value, physical_ttl
from .composites import CompositeMethods
from .keys import clear_prefix_for_namespace
from .runtime import Runtime
from .runtime import run_sync as _run_sync

_NAMESPACE_PATTERN: Final = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_OPERATION_LIMIT: Final = 900
# Django's runtime sentinel is untyped; this cast preserves its identity while
# keeping the public timeout type useful to static callers.
DEFAULT_TIMEOUT: Final[int | float | None] = cast(
    "int | float | None", django_cache_base.DEFAULT_TIMEOUT
)
type CacheKeyFunction = Callable[[str, str, int], str]
type CacheOption = (
    str | int | float | bool | CacheKeyFunction | dict[str, CacheOption] | None
)


class KVCache(CompositeMethods, BatchCacheMixin):
    """Django cache backed by native asynchronous Workers KV operations."""

    location: str
    cache_namespace: str
    operation_limit: int

    def __init__(
        self,
        params: dict[str, CacheOption] | str,
        options: dict[str, CacheOption] | None = None,
    ) -> None:
        """Parse and validate backend configuration without accessing bindings."""
        if isinstance(params, str):
            configuration = {"LOCATION": params, **(options or {})}
        else:
            configuration = params

        location = configuration.get("LOCATION")
        if not isinstance(location, str) or not location.strip():
            msg = "Cloudflare KV LOCATION must be a nonempty binding name."
            raise ImproperlyConfigured(msg)

        raw_options = configuration.get("OPTIONS", {})
        if not isinstance(raw_options, dict):
            msg = "Cloudflare KV OPTIONS must be a mapping."
            raise ImproperlyConfigured(msg)
        options = raw_options

        raw_namespace = options.get("CACHE_NAMESPACE", "default")
        if (
            not isinstance(raw_namespace, str)
            or _NAMESPACE_PATTERN.fullmatch(raw_namespace) is None
        ):
            msg = "CACHE_NAMESPACE must match [A-Za-z0-9_-]{1,64}."
            raise ImproperlyConfigured(msg)

        namespace = raw_namespace
        raw_operation_limit = options.get("OPERATION_LIMIT", _MAX_OPERATION_LIMIT)
        if (
            isinstance(raw_operation_limit, bool)
            or not isinstance(raw_operation_limit, int)
            or not 1 <= raw_operation_limit <= _MAX_OPERATION_LIMIT
        ):
            msg = "OPERATION_LIMIT must be an integer in the range 1..900."
            raise ImproperlyConfigured(msg)

        operation_limit = raw_operation_limit
        super().__init__(configuration)
        self.location = location
        # This library-owned key prefix is inside the bound KV namespace, not a
        # Cloudflare resource ID. Aliases sharing it intentionally share clear scope.
        self.cache_namespace = namespace
        self.operation_limit = operation_limit

    async def _read(self, key: str, budget: list[int]) -> DecodedValue | object:
        self._consume(budget)
        raw = await Runtime().resolve_binding(self.location).get(key)
        if raw is None:
            return self._missing_key
        decoded = decode_value(raw, now=time.time())
        return self._missing_key if decoded is None else decoded

    @override
    async def aget(
        self, key: str, default: object | None = None, version: int | None = None
    ) -> object:
        """Return a live cached value or the supplied default."""
        physical_key = self._physical_key(key, version)
        value = await self._read(physical_key, [0])
        if isinstance(value, DecodedValue):
            return cast("object", value.value)
        return default

    @override
    async def aset(
        self,
        key: str,
        value: object,
        timeout: float | None = DEFAULT_TIMEOUT,
        version: int | None = None,
    ) -> None:
        """Store a value, or remove its physical key for a nonpositive timeout."""
        physical_key = self._physical_key(key, version)
        now = time.time()
        expires_at = self.get_backend_timeout(timeout)
        budget = [0]
        binding = Runtime().resolve_binding(self.location)
        if expires_at is not None and expires_at <= now:
            self._consume(budget)
            await binding.delete(physical_key)
            return
        serialized = encode_value(value, expires_at=expires_at)
        ttl = physical_ttl(expires_at, now=now)
        self._consume(budget)
        if ttl is None:
            await binding.put(physical_key, serialized)
        else:
            await binding.put(physical_key, serialized, expiration_ttl=ttl)

    @override
    async def aadd(
        self,
        key: str,
        value: object,
        timeout: float | None = DEFAULT_TIMEOUT,
        version: int | None = None,
    ) -> bool:
        """Store only when this operation observes a miss."""
        physical_key = self._physical_key(key, version)
        budget = [0]
        binding = Runtime().resolve_binding(self.location)
        stored = await self._read_with_binding(binding, physical_key, budget)
        if stored is not self._missing_key:
            return False
        expires_at = self.get_backend_timeout(timeout)
        now = time.time()
        if expires_at is not None and expires_at <= now:
            return True
        serialized = encode_value(value, expires_at=expires_at)
        ttl = physical_ttl(expires_at, now=now)
        self._consume(budget)
        if ttl is None:
            await binding.put(physical_key, serialized)
        else:
            await binding.put(physical_key, serialized, expiration_ttl=ttl)
        return True

    @override
    async def atouch(
        self,
        key: str,
        timeout: float | None = DEFAULT_TIMEOUT,
        version: int | None = None,
    ) -> bool:
        """Refresh a live value's logical deadline without changing its value."""
        physical_key = self._physical_key(key, version)
        budget = [0]
        binding = Runtime().resolve_binding(self.location)
        stored = await self._read_with_binding(binding, physical_key, budget)
        if not isinstance(stored, DecodedValue):
            return False
        now = time.time()
        expires_at = self.get_backend_timeout(timeout)
        if expires_at is not None and expires_at <= now:
            self._consume(budget)
            await binding.delete(physical_key)
            return True
        serialized = encode_value(cast("object", stored.value), expires_at=expires_at)
        ttl = physical_ttl(expires_at, now=now)
        self._consume(budget)
        if ttl is None:
            await binding.put(physical_key, serialized)
        else:
            await binding.put(physical_key, serialized, expiration_ttl=ttl)
        return True

    @override
    async def adelete(self, key: str, version: int | None = None) -> bool:
        """Delete the key and return whether the preceding read observed it."""
        physical_key = self._physical_key(key, version)
        budget = [0]
        binding = Runtime().resolve_binding(self.location)
        stored = await self._read_with_binding(binding, physical_key, budget)
        self._consume(budget)
        await binding.delete(physical_key)
        return stored is not self._missing_key

    @override
    async def ahas_key(self, key: str, version: int | None = None) -> bool:
        """Return whether the read observed a live cache entry."""
        physical_key = self._physical_key(key, version)
        return await self._read(physical_key, [0]) is not self._missing_key

    @override
    async def aclose(self, **kwargs: object) -> None:
        """Close no resources; bindings are resolved per operation."""
        _ = kwargs

    @override
    def get(
        self, key: str, default: object | None = None, version: int | None = None
    ) -> object:
        """Synchronously return a live cached value or the supplied default."""
        return _run_sync(self.aget(key, default, version))

    @override
    def set(
        self,
        key: str,
        value: object,
        timeout: float | None = DEFAULT_TIMEOUT,
        version: int | None = None,
    ) -> None:
        """Synchronously store a value or delete its key for nonpositive timeout."""
        return _run_sync(self.aset(key, value, timeout, version))

    @override
    def add(
        self,
        key: str,
        value: object,
        timeout: float | None = DEFAULT_TIMEOUT,
        version: int | None = None,
    ) -> bool:
        """Synchronously store only when a read observes a miss."""
        return _run_sync(self.aadd(key, value, timeout, version))

    @override
    def touch(
        self,
        key: str,
        timeout: float | None = DEFAULT_TIMEOUT,
        version: int | None = None,
    ) -> bool:
        """Synchronously refresh a live value's logical deadline."""
        return _run_sync(self.atouch(key, timeout, version))

    @override
    def delete(self, key: str, version: int | None = None) -> bool:
        """Synchronously delete and return whether the preceding read saw it."""
        return _run_sync(self.adelete(key, version))

    @override
    def has_key(self, key: str, version: int | None = None) -> bool:
        """Synchronously report live entry presence."""
        return _run_sync(self.ahas_key(key, version))

    @override
    def __contains__(self, key: str) -> bool:
        """Support Python's ``key in cache`` presence check."""
        return self.has_key(key)

    @override
    def close(self, **kwargs: object) -> None:
        """Close no resources; bindings are resolved per operation."""
        _ = kwargs

    @property
    @override
    def clear_prefix(self) -> str:
        """Prefix owned by this logical cache namespace inside the KV binding."""
        return clear_prefix_for_namespace(self.cache_namespace)

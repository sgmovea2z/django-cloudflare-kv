"""Bounded multi-key operations for :class:`KVCache`."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Final, cast, override

from django.core.cache.backends import base as django_cache_base
from django.core.cache.backends.base import BaseCache

from .codec import DecodedValue, encode_value, physical_ttl
from .errors import CacheOperationError
from .runtime import KVNamespace, Runtime
from .runtime import run_sync as _run_sync

if TYPE_CHECKING:
    from collections.abc import Iterable

DEFAULT_TIMEOUT: Final[int | float | None] = cast(
    "int | float | None", django_cache_base.DEFAULT_TIMEOUT
)
_LIST_TUPLE_SIZE: Final = 3
_MALFORMED_LIST_RESULT: Final = "KV key listing returned a malformed result."
_FOREIGN_LIST_KEY: Final = "KV key listing returned a key outside the cache namespace."
_MALFORMED_CURSOR: Final = "KV key listing returned a malformed cursor."
_LIST_FAILURE: Final = "KV key listing failed."
_REPEATED_CURSOR: Final = "KV key listing cursor did not advance."


def _listed_names(result: object, prefix: str) -> tuple[list[str], str | None, bool]:
    message = _MALFORMED_LIST_RESULT
    if (
        isinstance(result, tuple)
        and len(cast("tuple[object, ...]", result)) == _LIST_TUPLE_SIZE
    ):
        page_keys, next_cursor, list_complete = cast(
            "tuple[object, object, object]", result
        )
    elif isinstance(result, dict):
        result_mapping = cast("dict[str, object]", result)
        page_keys = result_mapping.get("keys")
        next_cursor = result_mapping.get("cursor")
        list_complete = result_mapping.get("list_complete")
    else:
        raise CacheOperationError(message)

    if not isinstance(page_keys, (tuple, list)) or not isinstance(list_complete, bool):
        raise CacheOperationError(message)
    names: list[str] = []
    for item in cast("tuple[object, ...] | list[object]", page_keys):
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict):
            name = cast("dict[str, object]", item).get("name")
            if not isinstance(name, str):
                raise CacheOperationError(message)
        else:
            raise CacheOperationError(message)
        if not name.startswith(prefix):
            raise CacheOperationError(_FOREIGN_LIST_KEY)
        names.append(name)
    cursor = next_cursor if isinstance(next_cursor, str) else None
    if not list_complete and cursor is None:
        raise CacheOperationError(_MALFORMED_CURSOR)
    return names, cursor, list_complete


class BatchCacheMixin(BaseCache):
    """Supply Django's bounded multi-key sync and native async API."""

    location: str = ""
    operation_limit: int = 0

    def _physical_key(self, key: str, version: int | None = None) -> str:
        _ = key, version
        raise NotImplementedError

    @property
    def clear_prefix(self) -> str:
        """Exact physical key prefix owned by this configured cache namespace."""
        raise NotImplementedError

    def _consume(self, budget: list[int], count: int = 1) -> None:
        _ = budget, count
        raise NotImplementedError

    def _check_budget(self, budget: list[int], count: int) -> None:
        _ = budget, count
        raise NotImplementedError

    async def _read_with_binding(
        self, binding: KVNamespace, key: str, budget: list[int]
    ) -> DecodedValue | object:
        _ = binding, key, budget
        raise NotImplementedError

    @override
    async def aget_many(
        self, keys: Iterable[str], version: int | None = None
    ) -> dict[str, object]:
        """Return live values under their original logical keys."""
        requested = list(dict.fromkeys(keys))
        if not requested:
            return {}
        self._check_budget([0], len(requested))
        physical = [(key, self._physical_key(key, version)) for key in requested]
        binding = Runtime().resolve_binding(self.location)
        budget = [0]
        values: dict[str, object] = {}
        for logical_key, physical_key in physical:
            value = await self._read_with_binding(binding, physical_key, budget)
            if isinstance(value, DecodedValue):
                values[logical_key] = cast("object", value.value)
        return values

    @override
    async def aset_many(
        self,
        data: dict[str, object],
        timeout: float | None = DEFAULT_TIMEOUT,
        version: int | None = None,
    ) -> list[str]:
        """Write independently, returning original keys whose writes failed."""
        if not data:
            return []
        items = list(data.items())
        self._check_budget([0], len(items))
        now = time.time()
        expires_at = self.get_backend_timeout(timeout)
        physical_items: list[tuple[str, str, str, int | None, bool]] = []
        for logical_key, value in items:
            physical_key = self._physical_key(logical_key, version)
            serialized = encode_value(value, expires_at=expires_at)
            ttl = physical_ttl(expires_at, now=now)
            delete = expires_at is not None and expires_at <= now
            physical_items.append((logical_key, physical_key, serialized, ttl, delete))

        binding = Runtime().resolve_binding(self.location)
        budget = [0]
        failed_keys: list[str] = []
        for logical_key, physical_key, serialized, ttl, delete in physical_items:
            self._consume(budget)
            try:
                if delete:
                    await binding.delete(physical_key)
                elif ttl is None:
                    await binding.put(physical_key, serialized)
                else:
                    await binding.put(physical_key, serialized, expiration_ttl=ttl)
            except Exception:  # noqa: BLE001
                failed_keys.append(logical_key)
        return failed_keys

    @override
    async def adelete_many(
        self, keys: Iterable[str], version: int | None = None
    ) -> None:
        """Delete individual keys; a later failure may follow completed deletes."""
        requested = list(keys)
        if not requested:
            return
        self._check_budget([0], len(requested))
        physical = [self._physical_key(key, version) for key in requested]
        binding = Runtime().resolve_binding(self.location)
        budget = [0]
        for physical_key in physical:
            self._consume(budget)
            await binding.delete(physical_key)

    @override
    async def aclear(self) -> bool:  # pyright: ignore[reportIncompatibleMethodOverride] -- backend contract returns True.
        """Delete listed keys; this pass is not a global emptiness guarantee."""
        binding = Runtime().resolve_binding(self.location)
        prefix = self.clear_prefix
        cursor: str | None = None
        seen_cursors: set[str] = set()
        keys: set[str] = set()
        list_calls = 0

        while True:
            list_calls += 1
            self._check_budget([0], list_calls)
            try:
                result = await binding.list(prefix=prefix, cursor=cursor)
            except Exception as exc:
                raise CacheOperationError(_LIST_FAILURE) from exc

            page, next_cursor, list_complete = _listed_names(result, prefix)
            keys.update(page)

            if list_complete:
                break
            if next_cursor is None:
                raise CacheOperationError(_MALFORMED_CURSOR)
            if next_cursor in seen_cursors or next_cursor == cursor:
                raise CacheOperationError(_REPEATED_CURSOR)
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        self._check_budget([0], list_calls + len(keys))
        for key in sorted(keys):
            await binding.delete(key)
        return True

    @override
    def get_many(
        self, keys: Iterable[str], version: int | None = None
    ) -> dict[str, object]:
        """Synchronously fetch multiple keys through one coroutine bridge."""
        requested = list(keys)
        if not requested:
            return {}
        return _run_sync(self.aget_many(requested, version))

    @override
    def set_many(
        self,
        data: dict[str, object],
        timeout: float | None = DEFAULT_TIMEOUT,
        version: int | None = None,
    ) -> list[str]:
        """Synchronously write multiple keys through one coroutine bridge."""
        if not data:
            return []
        return _run_sync(self.aset_many(data, timeout, version))

    @override
    def delete_many(self, keys: Iterable[str], version: int | None = None) -> None:
        """Synchronously delete multiple keys through one coroutine bridge."""
        requested = list(keys)
        if requested:
            _run_sync(self.adelete_many(requested, version))

    @override
    def clear(self) -> bool:  # pyright: ignore[reportIncompatibleMethodOverride] -- backend contract returns True.
        """Bridge one canonical clear coroutine; partial deletes may remain on error."""
        return _run_sync(self.aclear())

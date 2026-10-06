"""Django-compatible logical to physical KV key transformation."""

from __future__ import annotations

import hashlib
from typing import Final, Protocol


class _KeyCache(Protocol):
    cache_namespace: str

    def make_and_validate_key(self, key: str, version: int | None = None) -> str: ...


KEY_PREFIX: Final = "dckv:v1:"


def clear_prefix_for_namespace(namespace: str) -> str:
    """Return the exact list/clear prefix for one configured cache namespace."""
    return f"{KEY_PREFIX}{namespace}:"


def make_physical_key(cache: _KeyCache, key: str, version: int | None = None) -> str:
    """Hash Django's canonical key exactly once into the bounded KV namespace."""
    canonical_key = cache.make_and_validate_key(key, version)
    digest = hashlib.sha256(canonical_key.encode("utf-8")).hexdigest()
    return f"{clear_prefix_for_namespace(cache.cache_namespace)}{digest}"

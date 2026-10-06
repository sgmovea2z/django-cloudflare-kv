"""Physical key transformation contracts."""

from __future__ import annotations

import hashlib
import re
import warnings

from django.core.cache.backends.base import CacheKeyWarning
from hypothesis import example, given, settings
from hypothesis import strategies as st

from django_cloudflare_kv.backends import CacheOption, KVCache
from django_cloudflare_kv.keys import make_physical_key


def cache(namespace: str = "default", **params: CacheOption) -> KVCache:
    return KVCache(
        {"LOCATION": "KV", "OPTIONS": {"CACHE_NAMESPACE": namespace}, **params}
    )


def test_key_uses_django_canonical_key_once() -> None:
    calls: list[tuple[str, str, int]] = []

    def key_function(key: str, prefix: str, version: int) -> str:
        calls.append((key, prefix, version))
        return f"{prefix}|{version}|{key}"

    backend = cache(
        "tenant-cache", KEY_PREFIX="tenant", VERSION=7, KEY_FUNCTION=key_function
    )
    result = make_physical_key(backend, "logical")
    expected = hashlib.sha256(b"tenant|7|logical").hexdigest()

    assert result == f"dckv:v1:tenant-cache:{expected}"
    assert calls == [("logical", "tenant", 7)]


def test_custom_key_function_calls_once_per_transform() -> None:
    calls = 0

    def key_function(key: str, prefix: str, version: int) -> str:
        nonlocal calls
        calls += 1
        return f"{prefix}:{version}:{key}"

    backend = cache(KEY_FUNCTION=key_function)
    result = make_physical_key(backend, "k")
    assert result.startswith("dckv:v1:default:")
    assert calls == 1


def test_django_portability_warning_is_preserved() -> None:
    backend = cache()
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always", CacheKeyWarning)
        _ = make_physical_key(backend, "has space")
    assert any(item.category is CacheKeyWarning for item in captured)


def test_prefix_and_version_are_isolated() -> None:
    first = cache(KEY_PREFIX="one", VERSION=1)
    second_prefix = cache(KEY_PREFIX="two", VERSION=1)
    second_version = cache(KEY_PREFIX="one", VERSION=2)

    assert make_physical_key(first, "same") != make_physical_key(second_prefix, "same")
    assert make_physical_key(first, "same") != make_physical_key(second_version, "same")


def test_configured_namespace_is_used_for_key_and_clear_prefix() -> None:
    backend = cache("tenant-cache")

    assert make_physical_key(backend, "logical").startswith("dckv:v1:tenant-cache:")
    assert backend.clear_prefix == "dckv:v1:tenant-cache:"


@given(
    key=st.text(max_size=100_000),
    namespace=st.text(
        alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-",
        min_size=1,
        max_size=64,
    ),
)
@settings(max_examples=80)
@example(key="", namespace="default")
@example(key="𐐷", namespace="astral")
@example(key="e\u0301", namespace="combining")
@example(key="k" * 100_000, namespace="long")
def test_physical_key_properties(key: str, namespace: str) -> None:
    backend = cache(namespace)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", CacheKeyWarning)
        first = make_physical_key(backend, key)
        second = make_physical_key(backend, key)

    assert first == second
    assert first.encode("utf-8").decode("utf-8") == first
    assert len(first.encode("utf-8")) < 512
    assert re.fullmatch(rf"dckv:v1:{re.escape(namespace)}:[0-9a-f]{{64}}", first)


def test_empty_and_very_long_logical_keys_are_hashed() -> None:
    backend = cache("x" * 64)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", CacheKeyWarning)
        empty = make_physical_key(backend, "")
        long = make_physical_key(backend, "k" * 1_000_000)

    assert empty != long
    assert len(long.encode("utf-8")) < 512

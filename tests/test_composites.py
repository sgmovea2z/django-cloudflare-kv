from __future__ import annotations

import time
from typing import TYPE_CHECKING, cast

import pytest

from django_cloudflare_kv.backends import KVCache
from django_cloudflare_kv.codec import decode_value
from django_cloudflare_kv.errors import CacheOperationLimitError
from django_cloudflare_kv.keys import make_physical_key
from django_cloudflare_kv.runtime import KVNamespace, Runtime

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from typing import Literal

    from .fakes import FakeBinding, FakeClock


def _complete[T](coroutine: Coroutine[object, object, T]) -> T:
    try:
        _ = coroutine.send(None)
    except StopIteration as result:
        return cast("T", result.value)
    raise AssertionError


def _cached_callable() -> str:
    return "cached"


def _resolver(binding: FakeBinding) -> Callable[[Runtime, str], KVNamespace]:
    class AsyncBinding:
        async def get(self, key: str) -> str | None:
            return binding.get(key)

        async def put(
            self, key: str, value: str, *, expiration_ttl: int | None = None
        ) -> None:
            binding.put(key, value, expiration_ttl=expiration_ttl)

        async def list(self, *, prefix: str, cursor: str | None = None) -> object:
            return binding.list(prefix, cursor=cursor)

        async def delete(self, key: str) -> None:
            binding.delete(key)

    def resolve(runtime: Runtime, name: str) -> KVNamespace:
        _ = runtime, name
        return AsyncBinding()

    return resolve


def _deadline(binding: FakeBinding, key: str, clock: FakeClock) -> float | None:
    raw = binding.get(key)
    assert raw is not None
    decoded = decode_value(raw, now=clock())
    assert decoded is not None
    return decoded.expires_at


@pytest.fixture
def composite_cache(
    fake_binding: FakeBinding,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> KVCache:
    monkeypatch.setattr(Runtime, "resolve_binding", _resolver(fake_binding))
    monkeypatch.setattr(time, "time", fake_clock)
    monkeypatch.setattr("django_cloudflare_kv.composites._run_sync", _complete)
    monkeypatch.setattr("django_cloudflare_kv.backends._run_sync", _complete)
    return KVCache({"LOCATION": "KV", "TIMEOUT": 30})


@pytest.fixture
def composite_sync_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("django_cloudflare_kv.composites._run_sync", _complete)
    monkeypatch.setattr("django_cloudflare_kv.backends._run_sync", _complete)


@pytest.mark.parametrize("default", ["value", lambda: "value"])
def test_get_or_set_returns_hit_without_evaluating_factory(
    fake_binding: FakeBinding,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    default: str | Callable[[], str],
    composite_sync_bridge: None,
) -> None:
    _ = composite_sync_bridge
    monkeypatch.setattr(Runtime, "resolve_binding", _resolver(fake_binding))
    monkeypatch.setattr(time, "time", fake_clock)
    cache = KVCache({"LOCATION": "KV"})
    _ = cache.set("k", "present")
    calls = 0

    def factory() -> str:
        nonlocal calls
        calls += 1
        return "value"

    actual = cache.get_or_set("k", factory if callable(default) else default)

    assert actual == "present"
    assert calls == 0


def test_get_or_set_miss_uses_factory_once_and_reread_fallback(
    fake_binding: FakeBinding,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    composite_sync_bridge: None,
) -> None:
    _ = composite_sync_bridge
    monkeypatch.setattr(Runtime, "resolve_binding", _resolver(fake_binding))
    monkeypatch.setattr(time, "time", fake_clock)
    cache = KVCache({"LOCATION": "KV"})
    calls = 0

    def factory() -> str:
        nonlocal calls
        calls += 1
        return "created"

    assert cache.get_or_set("k", factory) == "created"
    assert calls == 1


def test_get_or_set_miss_stores_plain_default(
    composite_cache: KVCache,
) -> None:
    assert composite_cache.get_or_set("plain", "default") == "default"


def test_get_or_set_returns_cached_callable_value(
    composite_cache: KVCache,
) -> None:
    _ = _complete(composite_cache.aset("callable", _cached_callable))

    actual = composite_cache.get_or_set("callable", "fallback")
    assert actual is _cached_callable
    assert callable(actual)
    assert actual() == "cached"


@pytest.mark.asyncio
async def test_aget_or_set_accepts_sync_factory_and_avoids_sync_dispatch(
    fake_binding: FakeBinding,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    composite_sync_bridge: None,
) -> None:
    _ = composite_sync_bridge
    monkeypatch.setattr(Runtime, "resolve_binding", _resolver(fake_binding))
    monkeypatch.setattr(time, "time", fake_clock)

    def fail(*args: object, **kwargs: object) -> object:
        _ = args, kwargs
        raise AssertionError

    monkeypatch.setattr("django.core.cache.backends.base.sync_to_async", fail)
    monkeypatch.setattr("asgiref.sync.sync_to_async", fail)
    monkeypatch.setattr("django_cloudflare_kv.backends._run_sync", fail, raising=False)
    monkeypatch.setattr("django_cloudflare_kv.composites._run_sync", fail)
    cache = KVCache({"LOCATION": "KV"})
    calls = 0

    def factory() -> str:
        nonlocal calls
        calls += 1
        return "created"

    assert await cache.aget_or_set("k", factory) == "created"
    assert calls == 1


@pytest.mark.parametrize(
    "case",
    [
        ("incr", 10, 3, 13),
        ("incr", 10, -3, 7),
        ("decr", 10, 3, 7),
        ("decr", 10, -3, 13),
    ],
)
def test_counter_mutation_preserves_original_deadline(
    fake_binding: FakeBinding,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[Literal["incr", "decr"], int, int, int],
    composite_sync_bridge: None,
) -> None:
    _ = composite_sync_bridge
    method, initial, delta, expected = case
    monkeypatch.setattr(Runtime, "resolve_binding", _resolver(fake_binding))
    monkeypatch.setattr(time, "time", fake_clock)
    cache = KVCache({"LOCATION": "KV"})
    key = make_physical_key(cache, "counter")
    _ = _complete(cache.aset("counter", initial, timeout=100))
    deadline_before = _deadline(fake_binding, key, fake_clock)
    match method:
        case "incr":
            actual = cache.incr("counter", delta)
        case "decr":
            actual = cache.decr("counter", delta)

    raw = fake_binding.get(key)
    assert raw is not None
    stored = decode_value(raw, now=fake_clock())
    assert actual == expected
    assert stored is not None
    assert cast("int", stored.value) == expected
    assert stored.expires_at == deadline_before


@pytest.mark.asyncio
async def test_aincr_preserves_deadline_without_thread_dispatch(
    fake_binding: FakeBinding,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Runtime, "resolve_binding", _resolver(fake_binding))
    monkeypatch.setattr(time, "time", fake_clock)

    def fail(*args: object, **kwargs: object) -> object:
        _ = args, kwargs
        raise AssertionError

    monkeypatch.setattr("django.core.cache.backends.base.sync_to_async", fail)
    monkeypatch.setattr("asgiref.sync.sync_to_async", fail)
    monkeypatch.setattr("django_cloudflare_kv.backends._run_sync", fail, raising=False)
    monkeypatch.setattr("django_cloudflare_kv.composites._run_sync", fail)
    cache = KVCache({"LOCATION": "KV"})
    key = make_physical_key(cache, "counter")
    await cache.aset("counter", 2, timeout=100)
    deadline_before = _deadline(fake_binding, key, fake_clock)

    assert await cache.aincr("counter", 2) == 4
    assert _deadline(fake_binding, key, fake_clock) == deadline_before
    assert await cache.adecr("counter", 1) == 3


@pytest.mark.parametrize("method", ["incr", "decr"])
def test_counter_missing_raises_value_error(
    composite_cache: KVCache, method: Literal["incr", "decr"]
) -> None:
    match method:
        case "incr":
            operation = composite_cache.incr
        case "decr":
            operation = composite_cache.decr
    with pytest.raises(ValueError, match="cache key not found"):
        _ = operation("absent")


def test_counter_invalid_arithmetic_propagates_type_error(
    composite_cache: KVCache, composite_sync_bridge: None
) -> None:
    _ = composite_sync_bridge
    _ = composite_cache.set("text", "not numeric")

    with pytest.raises(TypeError):
        _ = composite_cache.incr("text")


def test_version_move_preserves_deadline_and_isolates_versions(
    fake_binding: FakeBinding,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    composite_sync_bridge: None,
) -> None:
    _ = composite_sync_bridge
    monkeypatch.setattr(Runtime, "resolve_binding", _resolver(fake_binding))
    monkeypatch.setattr(time, "time", fake_clock)
    cache = KVCache({"LOCATION": "KV", "VERSION": 4})
    old_key = make_physical_key(cache, "k", 4)
    new_key = make_physical_key(cache, "k", 7)
    _ = _complete(cache.aset("k", "value", timeout=100, version=4))
    original_deadline = _deadline(fake_binding, old_key, fake_clock)

    assert cache.incr_version("k", delta=3, version=4) == 7
    assert cache.get("k", version=4) is None
    moved_raw = fake_binding.get(new_key)
    assert moved_raw is not None
    moved = decode_value(moved_raw, now=fake_clock())
    assert moved is not None
    assert cast("str", moved.value) == "value"
    assert moved.expires_at == original_deadline


@pytest.mark.asyncio
async def test_aincr_version_default_version_moves_without_sync_dispatch(
    fake_binding: FakeBinding,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Runtime, "resolve_binding", _resolver(fake_binding))
    monkeypatch.setattr(time, "time", fake_clock)

    def fail(*args: object, **kwargs: object) -> object:
        _ = args, kwargs
        raise AssertionError

    monkeypatch.setattr("django.core.cache.backends.base.sync_to_async", fail)
    monkeypatch.setattr("asgiref.sync.sync_to_async", fail)
    monkeypatch.setattr("django_cloudflare_kv.backends._run_sync", fail, raising=False)
    monkeypatch.setattr("django_cloudflare_kv.composites._run_sync", fail)
    cache = KVCache({"LOCATION": "KV", "VERSION": 4})
    new_key = make_physical_key(cache, "k", 5)
    await cache.aset("k", "value", timeout=100)

    assert await cache.aincr_version("k") == 5
    assert await cache.aget("k", version=4) is None
    moved_raw = fake_binding.get(new_key)
    assert moved_raw is not None
    moved = decode_value(moved_raw, now=fake_clock())
    assert moved is not None
    assert cast("str", moved.value) == "value"

    await cache.aset("lower", "value", version=4)
    assert await cache.adecr_version("lower") == 3
    assert await cache.aget("lower", version=4) is None


@pytest.mark.parametrize("method", ["incr_version", "decr_version"])
def test_version_move_missing_raises_value_error(
    composite_cache: KVCache,
    method: Literal["incr_version", "decr_version"],
    composite_sync_bridge: None,
) -> None:
    _ = composite_sync_bridge
    match method:
        case "incr_version":
            operation = composite_cache.incr_version
        case "decr_version":
            operation = composite_cache.decr_version
    with pytest.raises(ValueError, match="cache key not found"):
        _ = operation("absent")


def test_factory_exception_propagates(
    composite_cache: KVCache, composite_sync_bridge: None
) -> None:
    _ = composite_sync_bridge

    def factory() -> str:
        raise LookupError

    with pytest.raises(LookupError):
        _ = composite_cache.get_or_set("k", factory)


def test_failed_version_destination_write_keeps_source(
    composite_cache: KVCache,
    fake_binding: FakeBinding,
    composite_sync_bridge: None,
) -> None:
    _ = composite_sync_bridge
    _ = _complete(composite_cache.aset("k", "value", version=3))
    fake_binding.fail_next("put", RuntimeError("write failed"))

    with pytest.raises(RuntimeError, match="write failed"):
        _ = composite_cache.incr_version("k", version=3)

    assert _complete(composite_cache.aget("k", version=3)) == "value"


def test_failed_version_source_delete_raises_with_possible_duplicate(
    composite_cache: KVCache,
    fake_binding: FakeBinding,
    composite_sync_bridge: None,
) -> None:
    _ = composite_sync_bridge
    _ = _complete(composite_cache.aset("k", "value", version=3))
    fake_binding.fail_next("delete", RuntimeError("delete failed"))

    with pytest.raises(RuntimeError, match="delete failed"):
        _ = composite_cache.incr_version("k", version=3)

    assert _complete(composite_cache.aget("k", version=3)) == "value"
    assert _complete(composite_cache.aget("k", version=4)) == "value"


def test_composite_preflights_operation_budget_before_mutation(
    fake_binding: FakeBinding,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    composite_sync_bridge: None,
) -> None:
    _ = composite_sync_bridge
    monkeypatch.setattr(Runtime, "resolve_binding", _resolver(fake_binding))
    monkeypatch.setattr(time, "time", fake_clock)
    cache = KVCache({"LOCATION": "KV", "OPTIONS": {"OPERATION_LIMIT": 1}})
    _ = cache.set("k", 1)
    fake_binding.calls.clear()

    with pytest.raises(CacheOperationLimitError):
        _ = cache.incr("k")

    assert [call.operation for call in fake_binding.calls] == ["get"]

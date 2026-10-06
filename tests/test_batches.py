from __future__ import annotations

import time
from typing import TYPE_CHECKING, cast

import pytest

from django_cloudflare_kv.backends import KVCache
from django_cloudflare_kv.codec import MAX_ENVELOPE_BYTES
from django_cloudflare_kv.errors import CacheSerializationError
from django_cloudflare_kv.keys import make_physical_key
from django_cloudflare_kv.runtime import KVNamespace, Runtime
from tests.fakes import Call, FakeBinding

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from tests.fakes import FakeClock


def _complete[T](coroutine: Coroutine[object, object, T]) -> T:
    try:
        _ = coroutine.send(None)
    except StopIteration as result:
        return cast("T", result.value)
    raise AssertionError


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


@pytest.fixture
def cache(
    fake_binding: FakeBinding,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> KVCache:
    monkeypatch.setattr(Runtime, "resolve_binding", _resolver(fake_binding))
    monkeypatch.setattr(time, "time", fake_clock)
    monkeypatch.setattr("django_cloudflare_kv.batches._run_sync", _complete)
    monkeypatch.setattr("django_cloudflare_kv.backends._run_sync", _complete)
    return KVCache({"LOCATION": "DJANGO_CACHE"})


def test_get_many_preserves_original_keys_none_and_request_order(
    cache: KVCache,
) -> None:
    cache.set("first", None, version=2)
    cache.set("second", "value", version=3)

    assert cache.get_many(["first", "missing", "second"], version=2) == {"first": None}
    assert cache.get_many(["first", "second"], version=3) == {"second": "value"}


def test_get_many_deduplicates_logical_keys_in_input_order(
    cache: KVCache, fake_binding: FakeBinding
) -> None:
    cache.set("a", 1)
    cache.set("b", 2)
    fake_binding.calls.clear()

    assert cache.get_many(["b", "a", "b"]) == {"b": 2, "a": 1}
    assert [call.operation for call in fake_binding.calls] == ["get", "get"]


def test_set_many_returns_failures_in_input_order_and_continues(
    cache: KVCache, fake_binding: FakeBinding, monkeypatch: pytest.MonkeyPatch
) -> None:
    class KVRateLimitError(RuntimeError):
        pass

    failed_physical_key = make_physical_key(cache, "failed")
    original_put = FakeBinding.put

    def fail_selected(
        binding: FakeBinding,
        key: str,
        value: str,
        *,
        expiration_ttl: int | None = None,
    ) -> None:
        if key == failed_physical_key:
            binding.calls.append(Call(operation="put", key=key))
            raise KVRateLimitError
        original_put(binding, key, value, expiration_ttl=expiration_ttl)

    monkeypatch.setattr(FakeBinding, "put", fail_selected)

    assert cache.set_many({"first": 1, "failed": 2, "last": 3}) == ["failed"]
    assert [call.operation for call in fake_binding.calls] == ["put", "put", "put"]
    assert cache.get("last") == 3


def test_set_many_prevalidates_all_values_before_writing(
    cache: KVCache, fake_binding: FakeBinding
) -> None:
    fake_binding.calls.clear()

    with pytest.raises(CacheSerializationError):
        _ = cache.set_many({"small": "ok", "oversized": "x" * MAX_ENVELOPE_BYTES})

    assert fake_binding.calls == []


def test_delete_many_deletes_individually_and_returns_none(
    cache: KVCache, fake_binding: FakeBinding
) -> None:
    assert cache.set_many({"a": 1, "b": 2}, version=5) == []
    fake_binding.calls.clear()

    assert cache.delete_many(["a", "b"], version=5) is None
    assert [call.operation for call in fake_binding.calls] == ["delete", "delete"]


def test_delete_many_propagates_first_error_after_prior_delete(
    cache: KVCache,
    fake_binding: FakeBinding,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = cache.set_many({"first": 1, "failed": 2})
    fake_binding.calls.clear()
    failed_physical_key = make_physical_key(cache, "failed")
    original_delete = FakeBinding.delete

    def fail_selected(binding: FakeBinding, key: str) -> None:
        if key == failed_physical_key:
            binding.calls.append(Call(operation="delete", key=key))
            raise RuntimeError
        original_delete(binding, key)

    monkeypatch.setattr(FakeBinding, "delete", fail_selected)

    with pytest.raises(RuntimeError):
        cache.delete_many(["first", "failed"])

    assert [call.operation for call in fake_binding.calls] == ["delete", "delete"]
    assert fake_binding.get(make_physical_key(cache, "first")) is None


@pytest.mark.parametrize("operation", ["get_many", "set_many", "delete_many"])
def test_empty_batch_performs_no_binding_io(
    cache: KVCache, fake_binding: FakeBinding, operation: str
) -> None:
    methods = {
        "get_many": lambda: cache.get_many([]),
        "set_many": lambda: cache.set_many({}),
        "delete_many": lambda: cache.delete_many([]),
    }

    expected: dict[str, object] = {"get_many": {}, "set_many": [], "delete_many": None}
    assert methods[operation]() == expected[operation]
    assert fake_binding.calls == []


@pytest.mark.asyncio
async def test_all_native_batch_methods_bypass_sync_and_thread_dispatch(
    fake_binding: FakeBinding, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Runtime, "resolve_binding", _resolver(fake_binding))
    monkeypatch.setattr(time, "time", fake_clock)

    def fail(*args: object, **kwargs: object) -> object:
        _ = args, kwargs
        raise AssertionError

    monkeypatch.setattr("django.core.cache.backends.base.sync_to_async", fail)
    monkeypatch.setattr("asgiref.sync.sync_to_async", fail)
    monkeypatch.setattr("django_cloudflare_kv.backends._run_sync", fail)
    monkeypatch.setattr("django_cloudflare_kv.batches._run_sync", fail)
    cache = KVCache({"LOCATION": "DJANGO_CACHE"})

    assert await cache.aget_many([]) == {}
    assert await cache.aset_many({}) == []
    assert await cache.adelete_many([]) is None

    assert await cache.aset_many({"async": "value"}) == []
    assert await cache.aget_many(["async"]) == {"async": "value"}
    assert await cache.adelete_many(["async"]) is None


def test_sync_batch_methods_use_one_bridge_each(
    cache: KVCache,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge_calls = 0

    def bridge[T](coroutine: Coroutine[object, object, T]) -> T:
        nonlocal bridge_calls
        bridge_calls += 1
        return _complete(coroutine)

    monkeypatch.setattr("django_cloudflare_kv.batches._run_sync", bridge)

    assert cache.set_many({"one": "value"}) == []
    assert bridge_calls == 1
    assert cache.get_many(["one"]) == {"one": "value"}
    assert bridge_calls == 2
    assert cache.delete_many(["one"]) is None
    assert bridge_calls == 3


def test_empty_batch_does_not_resolve_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = KVCache({"LOCATION": "DJANGO_CACHE"})

    def fail(runtime: Runtime, name: str) -> KVNamespace:
        _ = runtime, name
        raise AssertionError

    monkeypatch.setattr(Runtime, "resolve_binding", fail)
    assert cache.get_many([]) == {}
    assert cache.set_many({}) == []
    assert cache.delete_many([]) is None

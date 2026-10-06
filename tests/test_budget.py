from __future__ import annotations

import time
from typing import TYPE_CHECKING, cast

import pytest

from django_cloudflare_kv.backends import KVCache
from django_cloudflare_kv.errors import CacheOperationLimitError
from django_cloudflare_kv.runtime import KVNamespace, Runtime

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from tests.fakes import FakeBinding, FakeClock


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
def make_cache(
    fake_binding: FakeBinding,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[int], KVCache]:
    monkeypatch.setattr(Runtime, "resolve_binding", _resolver(fake_binding))
    monkeypatch.setattr(time, "time", fake_clock)
    monkeypatch.setattr("django_cloudflare_kv.batches._run_sync", _complete)
    monkeypatch.setattr("django_cloudflare_kv.backends._run_sync", _complete)
    return lambda limit: KVCache(
        {"LOCATION": "DJANGO_CACHE", "OPTIONS": {"OPERATION_LIMIT": limit}}
    )


@pytest.mark.parametrize("count", [1, 2])
@pytest.mark.parametrize("operation", ["get", "set", "delete"])
def test_multi_key_operations_accept_budget_limit_minus_one_and_limit(
    make_cache: Callable[[int], KVCache],
    fake_binding: FakeBinding,
    operation: str,
    count: int,
) -> None:
    cache = make_cache(2)
    keys = [f"key-{index}" for index in range(count)]

    if operation == "get":
        assert cache.get_many(keys) == {}
    elif operation == "set":
        assert cache.set_many(dict.fromkeys(keys, "value")) == []
    else:
        assert cache.delete_many(keys) is None

    assert len(fake_binding.calls) == count


@pytest.mark.parametrize("operation", ["get", "set", "delete"])
def test_multi_key_operations_reject_limit_plus_one_before_io(
    make_cache: Callable[[int], KVCache],
    fake_binding: FakeBinding,
    operation: str,
) -> None:
    cache = make_cache(2)
    keys = ["first", "second", "third"]

    actions: dict[str, Callable[[], object]] = {
        "get": lambda: cache.get_many(keys),
        "set": lambda: cache.set_many(dict.fromkeys(keys, "value")),
        "delete": lambda: cache.delete_many(keys),
    }
    action = actions[operation]

    with pytest.raises(CacheOperationLimitError):
        _ = action()

    assert fake_binding.calls == []


@pytest.mark.asyncio
async def test_native_batch_budget_rejects_before_first_binding_call(
    fake_binding: FakeBinding,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Runtime, "resolve_binding", _resolver(fake_binding))
    monkeypatch.setattr(time, "time", fake_clock)
    cache = KVCache({"LOCATION": "DJANGO_CACHE", "OPTIONS": {"OPERATION_LIMIT": 1}})

    with pytest.raises(CacheOperationLimitError):
        _ = await cache.aset_many({"first": "value", "second": "value"})

    assert fake_binding.calls == []

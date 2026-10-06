from __future__ import annotations

import pickle
import time
from typing import TYPE_CHECKING, cast, final, override

import pytest

from django_cloudflare_kv.backends import KVCache
from django_cloudflare_kv.codec import encode_value
from django_cloudflare_kv.errors import (
    CacheOperationLimitError,
    CacheSerializationError,
)
from django_cloudflare_kv.keys import make_physical_key
from django_cloudflare_kv.runtime import KVNamespace, Runtime

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from .fakes import FakeBinding, FakeClock


@final
class AsyncFakeBinding:
    def __init__(self, binding: FakeBinding) -> None:
        self.binding: FakeBinding = binding

    async def get(self, key: str) -> str | None:
        return self.binding.get(key)

    async def put(
        self, key: str, value: str, *, expiration_ttl: int | None = None
    ) -> None:
        self.binding.put(key, value, expiration_ttl=expiration_ttl)

    async def list(self, *, prefix: str, cursor: str | None = None) -> object:
        return self.binding.list(prefix, cursor=cursor)

    async def delete(self, key: str) -> None:
        self.binding.delete(key)


def _complete[T](coroutine: Coroutine[object, object, T]) -> T:
    try:
        _ = coroutine.send(None)
    except StopIteration as result:
        return cast("T", result.value)
    raise AssertionError


def _resolver(binding: FakeBinding) -> Callable[[Runtime, str], KVNamespace]:
    def resolve(runtime: Runtime, name: str) -> KVNamespace:
        _ = runtime, name
        return AsyncFakeBinding(binding)

    return resolve


@pytest.fixture
def sync_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "django_cloudflare_kv.backends._run_sync", _complete, raising=False
    )


@pytest.fixture
def cache(
    fake_binding: FakeBinding,
    monkeypatch: pytest.MonkeyPatch,
    sync_bridge: None,
) -> KVCache:
    _ = sync_bridge
    monkeypatch.setattr(Runtime, "resolve_binding", _resolver(fake_binding))
    monkeypatch.setattr(time, "time", fake_binding.clock)
    return KVCache({"LOCATION": "DJANGO_CACHE", "TIMEOUT": 30})


@pytest.mark.parametrize("value", [None, False, 0, ""])
def test_get_distinguishes_falsy_hits_from_misses(
    cache: KVCache, fake_binding: FakeBinding, value: object
) -> None:
    key = make_physical_key(cache, "key")
    fake_binding.put(key, encode_value(value, expires_at=None))
    actual = cache.get("key", "fallback")
    assert actual == value


def test_set_zero_timeout_removes_previous_value_without_put(
    cache: KVCache, fake_binding: FakeBinding
) -> None:
    cache.set("key", "old")
    fake_binding.calls.clear()
    cache.set("key", "new", timeout=0)
    assert [call.operation for call in fake_binding.calls] == ["delete"]


def test_unserializable_set_does_not_write(
    cache: KVCache, fake_binding: FakeBinding
) -> None:
    class Unserializable:
        @override
        def __reduce__(self) -> str:
            raise pickle.PicklingError

    with pytest.raises(CacheSerializationError):
        _ = cache.set("key", Unserializable())
    assert not any(call.operation == "put" for call in fake_binding.calls)


def test_add_observes_existing_value_and_adds_on_miss(cache: KVCache) -> None:
    assert cache.add("key", "first") is True
    assert cache.add("key", "second") is False


def test_add_nonpositive_timeout_is_successful_without_put(
    cache: KVCache, fake_binding: FakeBinding
) -> None:
    assert cache.add("key", "value", timeout=0) is True
    assert not any(call.operation == "put" for call in fake_binding.calls)


def test_touch_preserves_payload_and_expiry_uses_controlled_clock(
    cache: KVCache, fake_clock: FakeClock
) -> None:
    cache.set("key", "payload")
    assert cache.touch("key", timeout=5) is True
    assert cache.get("key") == "payload"
    fake_clock.advance(5)
    assert cache.touch("key") is False


def test_touch_nonpositive_timeout_deletes_live_key(
    cache: KVCache, fake_binding: FakeBinding
) -> None:
    cache.set("key", "payload")
    assert cache.touch("key", timeout=0) is True
    assert fake_binding.calls[-1].operation == "delete"


def test_delete_returns_observed_presence_and_always_deletes(
    cache: KVCache, fake_binding: FakeBinding
) -> None:
    assert cache.delete("missing") is False
    assert fake_binding.calls[-1].operation == "delete"
    cache.set("present", None)
    assert cache.delete("present") is True


def test_has_key_and_contains_use_presence_not_truthiness(cache: KVCache) -> None:
    cache.set("empty", value=False)
    assert cache.has_key("empty") is True
    assert "empty" in cache


def test_corrupt_envelope_raises_serialization_error(
    cache: KVCache, fake_binding: FakeBinding
) -> None:
    fake_binding.put(make_physical_key(cache, "key"), "broken")
    with pytest.raises(CacheSerializationError):
        _ = cache.get("key", "fallback")


def test_binding_rejection_is_not_converted_to_miss(
    cache: KVCache, fake_binding: FakeBinding
) -> None:
    fake_binding.fail_next("get", RuntimeError("platform rejected read"))
    with pytest.raises(RuntimeError, match="platform rejected read"):
        _ = cache.get("key", "fallback")


def test_expired_read_is_a_miss_without_physical_delete(
    cache: KVCache, fake_binding: FakeBinding, fake_clock: FakeClock
) -> None:
    fake_binding.put(
        make_physical_key(cache, "key"), encode_value("old", expires_at=1001.0)
    )
    fake_clock.advance(1)
    value = cache.get("key", "fallback")
    assert value == "fallback"
    assert not any(call.operation == "delete" for call in fake_binding.calls)


def test_cached_miss_can_hide_a_newly_written_value(
    cache: KVCache,
    fake_binding: FakeBinding,
) -> None:
    fake_binding.visibility_delay = 5

    assert cache.get("key", "miss") == "miss"
    cache.set("key", "new")
    assert cache.get("key", "still-missed") == "still-missed"


def test_stale_overwrite_schedule_does_not_promise_read_after_write(
    cache: KVCache,
    fake_binding: FakeBinding,
    fake_clock: FakeClock,
) -> None:
    fake_binding.visibility_delay = 10
    physical_key = make_physical_key(cache, "key")
    fake_binding.put(physical_key, encode_value("old", expires_at=None))
    fake_clock.advance(10)
    assert cache.get("key") == "old"
    cache.set("key", "new")
    assert cache.get("key", "stale") == "stale"


@pytest.mark.usefixtures("sync_bridge")
@pytest.mark.usefixtures("cache")
def test_budget_stops_delete_before_second_binding_call(
    fake_binding: FakeBinding,
) -> None:
    constrained = KVCache(
        {"LOCATION": "DJANGO_CACHE", "OPTIONS": {"OPERATION_LIMIT": 1}}
    )
    with pytest.raises(CacheOperationLimitError):
        _ = constrained.delete("key")
    assert [call.operation for call in fake_binding.calls] == ["get"]


@pytest.mark.usefixtures("cache")
def test_budget_allows_one_call_add_with_nonpositive_timeout(
    fake_binding: FakeBinding,
) -> None:
    constrained = KVCache(
        {"LOCATION": "DJANGO_CACHE", "OPTIONS": {"OPERATION_LIMIT": 1}}
    )

    assert constrained.add("key", "value", timeout=0) is True
    assert [call.operation for call in fake_binding.calls] == ["get"]


@pytest.mark.asyncio
async def test_native_async_get_and_set(
    fake_binding: FakeBinding, monkeypatch: pytest.MonkeyPatch, fake_clock: FakeClock
) -> None:
    monkeypatch.setattr(Runtime, "resolve_binding", _resolver(fake_binding))
    monkeypatch.setattr(time, "time", fake_clock)
    cache = KVCache({"LOCATION": "DJANGO_CACHE"})
    await cache.aset("key", "value")
    assert await cache.aget("key", "fallback") == "value"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, False, 0, ""])
async def test_native_async_get_preserves_falsy_hits(
    fake_binding: FakeBinding,
    monkeypatch: pytest.MonkeyPatch,
    fake_clock: FakeClock,
    value: object,
) -> None:
    monkeypatch.setattr(Runtime, "resolve_binding", _resolver(fake_binding))
    monkeypatch.setattr(time, "time", fake_clock)
    cache = KVCache({"LOCATION": "DJANGO_CACHE"})
    await cache.aset("key", value, timeout=None)
    assert await cache.aget("key", "fallback") == value


@pytest.mark.asyncio
async def test_native_async_zero_timeout_replaces_old_entry_with_delete(
    fake_binding: FakeBinding, monkeypatch: pytest.MonkeyPatch, fake_clock: FakeClock
) -> None:
    monkeypatch.setattr(Runtime, "resolve_binding", _resolver(fake_binding))
    monkeypatch.setattr(time, "time", fake_clock)
    cache = KVCache({"LOCATION": "DJANGO_CACHE"})
    await cache.aset("key", "old", timeout=None)
    fake_binding.calls.clear()
    await cache.aset("key", "new", timeout=0)
    assert [call.operation for call in fake_binding.calls] == ["delete"]


@pytest.mark.asyncio
async def test_native_async_expiry_uses_fake_clock_without_deleting(
    fake_binding: FakeBinding,
    monkeypatch: pytest.MonkeyPatch,
    fake_clock: FakeClock,
) -> None:
    monkeypatch.setattr(Runtime, "resolve_binding", _resolver(fake_binding))
    monkeypatch.setattr(time, "time", fake_clock)
    cache = KVCache({"LOCATION": "DJANGO_CACHE"})
    await cache.aset("key", "short", timeout=1)
    fake_clock.advance(1)
    assert await cache.aget("key", "expired") == "expired"
    assert not any(call.operation == "delete" for call in fake_binding.calls)


@pytest.mark.asyncio
async def test_native_async_add_and_touch_nonpositive_timeouts(
    fake_binding: FakeBinding,
    monkeypatch: pytest.MonkeyPatch,
    fake_clock: FakeClock,
) -> None:
    monkeypatch.setattr(Runtime, "resolve_binding", _resolver(fake_binding))
    monkeypatch.setattr(time, "time", fake_clock)
    cache = KVCache({"LOCATION": "DJANGO_CACHE"})
    assert await cache.aadd("missing", "value", timeout=0) is True
    assert not any(call.operation == "put" for call in fake_binding.calls)
    await cache.aset("live", "payload", timeout=None)
    assert await cache.atouch("live", timeout=0) is True
    assert await cache.atouch("absent") is False


@pytest.mark.asyncio
async def test_native_async_touch_add_and_delete(
    fake_binding: FakeBinding, monkeypatch: pytest.MonkeyPatch, fake_clock: FakeClock
) -> None:
    monkeypatch.setattr(Runtime, "resolve_binding", _resolver(fake_binding))
    monkeypatch.setattr(time, "time", fake_clock)
    cache = KVCache({"LOCATION": "DJANGO_CACHE"})
    assert await cache.aadd("key", "one") is True
    assert await cache.aadd("key", "two") is False
    assert await cache.atouch("key") is True
    assert await cache.adelete("key") is True
    assert await cache.adelete("key") is False
    assert await cache.ahas_key("key") is False

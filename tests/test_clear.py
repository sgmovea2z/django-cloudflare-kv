from __future__ import annotations

import time
from typing import TYPE_CHECKING, cast

import pytest

from django_cloudflare_kv.backends import KVCache
from django_cloudflare_kv.errors import CacheOperationError, CacheOperationLimitError
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

        async def delete(self, key: str) -> None:
            binding.delete(key)

        async def list(
            self, prefix: str, *, cursor: str | None = None
        ) -> tuple[tuple[str, ...], str | None, bool]:
            return binding.list(prefix, cursor=cursor)

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
    return KVCache({"LOCATION": "DJANGO_CACHE", "OPTIONS": {"CACHE_NAMESPACE": "one"}})


def _operation_calls(binding: FakeBinding, operation: str) -> list[Call]:
    return [call for call in binding.calls if call.operation == operation]


def test_clear_handles_empty_middle_page_and_deduplicates_keys(
    cache: KVCache, fake_binding: FakeBinding, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given a paginated listing with duplicates, clear deletes each scoped key once."""
    first, second, third = (make_physical_key(cache, item) for item in ("a", "b", "c"))
    fake_binding.put(first, "a")
    fake_binding.put(second, "b")
    fake_binding.put(third, "c")
    fake_binding.calls.clear()

    class Pages:
        async def get(self, key: str) -> str | None:
            return fake_binding.get(key)

        async def put(
            self, key: str, value: str, *, expiration_ttl: int | None = None
        ) -> None:
            fake_binding.put(key, value, expiration_ttl=expiration_ttl)

        async def delete(self, key: str) -> None:
            fake_binding.delete(key)

        async def list(
            self, prefix: str, *, cursor: str | None = None
        ) -> tuple[tuple[str, ...], str | None, bool]:
            fake_binding.calls.append(Call("list", prefix))
            pages = {
                None: ((first,), "page-1", False),
                "page-1": ((), "page-2", False),
                "page-2": ((second, first), "page-3", False),
                "page-3": ((third,), "page-3", True),
            }
            return pages[cursor]

    def resolve_pages(_runtime: Runtime, _name: str) -> KVNamespace:
        return Pages()

    monkeypatch.setattr(Runtime, "resolve_binding", resolve_pages)
    assert cache.clear() is True

    deletes = _operation_calls(fake_binding, "delete")
    assert {call.key for call in deletes} == {first, second, third}
    assert len(deletes) == 3
    assert len(_operation_calls(fake_binding, "list")) == 4


def test_clear_removes_versions_and_key_prefixes_but_preserves_sibling_namespace(
    cache: KVCache, fake_binding: FakeBinding, fake_clock: FakeClock
) -> None:
    sibling = KVCache(
        {
            "LOCATION": "DJANGO_CACHE",
            "KEY_PREFIX": "else",
            "OPTIONS": {"CACHE_NAMESPACE": "two"},
        }
    )
    for version, key_prefix in ((1, ""), (4, "custom")):
        cache.set("own", version, version=version)
        cache.key_prefix = key_prefix
        cache.set("own", key_prefix)
    cache.key_prefix = ""
    sibling.set("must-survive", "sibling")
    sibling_key = make_physical_key(sibling, "must-survive")
    fake_binding.calls.clear()

    assert cache.clear() is True

    assert fake_binding.get(sibling_key) is not None
    assert _operation_calls(fake_binding, "delete")
    assert all(
        call.key is not None and call.key.startswith(cache.clear_prefix)
        for call in _operation_calls(fake_binding, "delete")
    )
    _ = fake_clock


@pytest.mark.parametrize(
    "case", ["list_error", "repeat", "foreign", "malformed", "budget"]
)
def test_clear_preflight_failures_never_delete(
    cache: KVCache,
    fake_binding: FakeBinding,
    case: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = make_physical_key(cache, "existing")
    fake_binding.put(key, "value")
    fake_binding.calls.clear()
    if case == "budget":
        cache.operation_limit = 1
    if case == "list_error":
        fake_binding.fail_next("list", RuntimeError("list failed"))
    if case in {"repeat", "foreign", "malformed"}:
        _install_faulty_listing(key, case, monkeypatch)

    with pytest.raises((CacheOperationError, CacheOperationLimitError, RuntimeError)):
        _ = cache.clear()

    assert _operation_calls(fake_binding, "delete") == []


def _install_faulty_listing(
    key: str,
    case: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses: dict[str, object] = {
        "repeat": ((key,), "same", False),
        "foreign": (("outside-prefix",), "done", True),
        "malformed": ("not-a-page",),
    }

    def list_fault(
        _fake: FakeBinding, prefix: str, *, cursor: str | None = None
    ) -> object:
        options = () if cursor is None else (("cursor", cursor),)
        _fake.calls.append(Call("list", prefix, options))
        return responses[case]

    monkeypatch.setattr(FakeBinding, "list", list_fault)


def test_clear_propagates_delete_failure_after_partial_deletion(
    cache: KVCache, fake_binding: FakeBinding, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys = [make_physical_key(cache, item) for item in ("a", "b")]
    for key in keys:
        fake_binding.put(key, "value")
    fake_binding.calls.clear()
    failing_key = max(keys)
    original_delete = FakeBinding.delete

    def fail_second(binding: FakeBinding, key: str) -> None:
        if key == failing_key:
            binding.calls.append(Call("delete", key))
            raise RuntimeError
        original_delete(binding, key)

    monkeypatch.setattr(FakeBinding, "delete", fail_second)
    with pytest.raises(RuntimeError):
        _ = cache.clear()

    completed_deletes = _operation_calls(fake_binding, "delete")
    assert completed_deletes[0].key is not None
    assert fake_binding.get(completed_deletes[0].key) is None
    assert fake_binding.get(failing_key) is not None


@pytest.mark.asyncio
async def test_aclear_returns_pass_completion_without_rescanning_late_keys(
    cache: KVCache, fake_binding: FakeBinding, monkeypatch: pytest.MonkeyPatch
) -> None:
    """True reports this pass completed, not immediate global namespace emptiness."""
    key = make_physical_key(cache, "first")
    fake_binding.put(key, "value")
    late_key = make_physical_key(cache, "late")
    fake_binding.calls.clear()

    class LateKeyBinding:
        async def get(self, key: str) -> str | None:
            return fake_binding.get(key)

        async def put(
            self, key: str, value: str, *, expiration_ttl: int | None = None
        ) -> None:
            fake_binding.put(key, value, expiration_ttl=expiration_ttl)

        async def delete(self, key: str) -> None:
            fake_binding.delete(key)

        async def list(self, prefix: str, *, cursor: str | None = None) -> object:
            _ = cursor
            fake_binding.calls.append(Call("list", prefix))
            fake_binding.put(late_key, "late")
            return {"keys": [{"name": key}], "cursor": "", "list_complete": True}

    def resolve_late(_runtime: Runtime, _name: str) -> KVNamespace:
        return LateKeyBinding()

    monkeypatch.setattr(Runtime, "resolve_binding", resolve_late)
    assert await cache.aclear() is True
    assert fake_binding.get(late_key) == "late"
    assert len(_operation_calls(fake_binding, "list")) == 1


@pytest.mark.asyncio
async def test_aclear_bypasses_thread_and_sync_dispatch(
    cache: KVCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: object, **kwargs: object) -> object:
        _ = args, kwargs
        raise AssertionError

    monkeypatch.setattr("django.core.cache.backends.base.sync_to_async", fail)
    monkeypatch.setattr("asgiref.sync.sync_to_async", fail)
    monkeypatch.setattr("django_cloudflare_kv.backends._run_sync", fail)
    assert await cache.aclear() is True

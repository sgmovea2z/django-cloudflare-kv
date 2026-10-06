from __future__ import annotations

import gc
import warnings
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from django_cloudflare_kv.backends import KVCache
from django_cloudflare_kv.errors import (
    CacheOperationError,
    CacheOperationLimitError,
    CacheRuntimeError,
    CacheSerializationError,
)
from django_cloudflare_kv.runtime import KVNamespace, Runtime, run_sync

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from .fakes import FakeBinding


def _complete[T](coroutine: Coroutine[object, object, T]) -> T:
    try:
        _ = coroutine.send(None)
    except StopIteration as result:
        return cast("T", result.value)
    raise AssertionError


def test_run_sync_outside_workers_raises_actionable_error_without_warning() -> None:
    async def operation() -> int:
        return 1

    coroutine = operation()
    with warnings.catch_warnings(record=True) as captured_warnings:
        warnings.simplefilter("always", RuntimeWarning)
        with pytest.raises(CacheRuntimeError) as error:
            _ = run_sync(coroutine)
        _ = gc.collect()

    assert error.value.__cause__ is not None
    assert error.value.__cause__ is not error.value
    assert "Workers runtime unavailable" in str(error.value)
    assert "inside a Worker" in str(error.value)
    assert coroutine.cr_frame is None
    assert not [
        warning
        for warning in captured_warnings
        if "was never awaited" in str(warning.message)
    ]


@pytest.mark.parametrize(
    "error_type",
    [
        CacheOperationLimitError,
        CacheSerializationError,
        CacheOperationError,
        CacheRuntimeError,
        NotImplementedError,
    ],
)
def test_run_sync_preserves_public_errors_from_bridged_operation(
    error_type: type[Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    cause = ValueError("original operation cause")
    operation_error = error_type("operation failed")

    async def operation() -> int:
        raise operation_error from cause

    def import_bridge(name: str) -> SimpleNamespace:
        assert name == "pyodide.ffi"
        return SimpleNamespace(run_sync=_complete)

    monkeypatch.setattr(
        "django_cloudflare_kv.runtime.importlib.import_module", import_bridge
    )
    coroutine = operation()

    with pytest.raises(error_type) as error:
        _ = run_sync(coroutine)

    assert error.value is operation_error
    assert error.value.__cause__ is cause
    assert coroutine.cr_frame is None


@pytest.mark.asyncio
async def test_native_async_methods_bypass_sync_and_thread_dispatch(
    fake_binding: FakeBinding, monkeypatch: pytest.MonkeyPatch
) -> None:
    class AsyncBinding:
        async def get(self, key: str) -> str | None:
            return fake_binding.get(key)

        async def put(
            self, key: str, value: str, *, expiration_ttl: int | None = None
        ) -> None:
            fake_binding.put(key, value, expiration_ttl=expiration_ttl)

        async def list(self, *, prefix: str, cursor: str | None = None) -> object:
            return fake_binding.list(prefix, cursor=cursor)

        async def delete(self, key: str) -> None:
            fake_binding.delete(key)

    def resolve(runtime: Runtime, name: str) -> KVNamespace:
        _ = runtime, name
        return AsyncBinding()

    monkeypatch.setattr(Runtime, "resolve_binding", resolve)

    def fail(*args: object, **kwargs: object) -> object:
        _ = args, kwargs
        raise AssertionError

    monkeypatch.setattr("django.core.cache.backends.base.sync_to_async", fail)
    monkeypatch.setattr("asgiref.sync.sync_to_async", fail)
    monkeypatch.setattr("django_cloudflare_kv.backends._run_sync", fail, raising=False)
    cache = KVCache({"LOCATION": "DJANGO_CACHE"})

    await cache.aset("key", "value")
    assert await cache.aget("key") == "value"
    assert await cache.aadd("key", "other") is False
    assert await cache.atouch("key") is True
    assert await cache.ahas_key("key") is True
    assert await cache.adelete("key") is True
    assert await cache.adelete("key") is False
    await cache.aclose()


@pytest.mark.asyncio
async def test_close_methods_are_runtime_free(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = KVCache({"LOCATION": "DJANGO_CACHE"})

    def fail_import(name: str) -> object:
        raise AssertionError(name)

    monkeypatch.setattr("importlib.import_module", fail_import)
    cache.close()
    await cache.aclose()


def test_sync_multicall_operations_bridge_one_coroutine_each(
    fake_binding: FakeBinding, monkeypatch: pytest.MonkeyPatch
) -> None:
    class AsyncBinding:
        async def get(self, key: str) -> str | None:
            return fake_binding.get(key)

        async def put(
            self, key: str, value: str, *, expiration_ttl: int | None = None
        ) -> None:
            fake_binding.put(key, value, expiration_ttl=expiration_ttl)

        async def list(self, *, prefix: str, cursor: str | None = None) -> object:
            return fake_binding.list(prefix, cursor=cursor)

        async def delete(self, key: str) -> None:
            fake_binding.delete(key)

    def resolve(runtime: Runtime, name: str) -> KVNamespace:
        _ = runtime, name
        return AsyncBinding()

    bridge_calls = 0

    def bridge[T](coroutine: Coroutine[object, object, T]) -> T:
        nonlocal bridge_calls
        bridge_calls += 1
        return _complete(coroutine)

    monkeypatch.setattr(Runtime, "resolve_binding", resolve)
    monkeypatch.setattr("django_cloudflare_kv.backends._run_sync", bridge)
    cache = KVCache({"LOCATION": "DJANGO_CACHE"})

    assert cache.add("key", "payload") is True
    assert bridge_calls == 1
    assert cache.touch("key", timeout=20) is True
    assert bridge_calls == 2
    assert cache.delete("key") is True
    assert bridge_calls == 3
    assert [call.operation for call in fake_binding.calls] == [
        "get",
        "put",
        "get",
        "put",
        "get",
        "delete",
    ]

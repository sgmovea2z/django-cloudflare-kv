"""Typed, lazy access to native Workers KV bindings."""

from __future__ import annotations

import importlib
import inspect
from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

from .errors import CacheRuntimeError


def run_sync[T](coroutine: Coroutine[object, object, T]) -> T:
    """Bridge one canonical coroutine into Workers' synchronous WSGI path."""
    try:
        module = importlib.import_module("pyodide.ffi")
        bridge_name = "run_sync"
        bridge = cast("Callable[[Awaitable[T]], T]", getattr(module, bridge_name))
    except ImportError as exc:
        coroutine.close()
        message = "Workers runtime unavailable; run this operation inside a Worker."
        raise CacheRuntimeError(message) from exc
    except AttributeError as exc:
        coroutine.close()
        message = "Workers runtime unavailable; run this operation inside a Worker."
        raise CacheRuntimeError(message) from exc

    try:
        return bridge(coroutine)
    except NotImplementedError as exc:
        if inspect.getcoroutinestate(coroutine) != inspect.CORO_CREATED:
            raise
        # Preserve operation errors; only the unstarted Pyodide stub means no bridge.
        coroutine.close()
        message = "Workers runtime unavailable; run this operation inside a Worker."
        raise CacheRuntimeError(message) from exc


class KVNamespace(Protocol):
    """Narrow asynchronous KV surface exposed by the Workers SDK."""

    def get(self, key: str) -> Awaitable[str | None]:
        """Return a stored string or ``None`` when the key is absent."""
        ...

    def put(
        self, key: str, value: str, *, expiration_ttl: int | None = None
    ) -> Awaitable[None]:
        """Store a string value under a key."""
        ...

    def delete(self, key: str) -> Awaitable[None]:
        """Delete the named key."""
        ...

    def list(self, *, prefix: str, cursor: str | None = None) -> Awaitable[object]:
        """List a prefix-scoped page; SDK result conversion stays at boundary."""
        ...


class Runtime:
    """Resolve runtime bindings on demand without retaining request state."""

    def resolve_binding(self, name: str) -> KVNamespace:
        """Resolve a named KV binding, chaining failures with actionable context."""
        try:
            workers_module = importlib.import_module("workers")
            environment = cast("object", workers_module.env)
            binding = cast("object", getattr(environment, name))
        except ImportError as exc:
            message = "Workers runtime unavailable; run this operation inside a Worker."
            raise CacheRuntimeError(message) from exc
        except AttributeError as exc:
            message = f"KV binding {name!r} unavailable; check Wrangler configuration."
            raise CacheRuntimeError(message) from exc
        return cast("KVNamespace", binding)

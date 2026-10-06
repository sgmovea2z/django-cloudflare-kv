# Django Cloudflare KV cache backend

A Django cache backend for the native classic Cloudflare Workers KV binding. It uses Django's cache API in Workers Python applications; it does not provide transactional storage or stronger consistency than KV.

## Installation

The distribution name is `django-cloudflare-kv` and the project is licensed under the MIT License. Install the supplied wheel in the consumer's Python environment.

```sh
python -m pip install ./django_cloudflare_kv-0.1.0-py3-none-any.whl
```

Python 3.13 is the tested tooling baseline. CI tests Django 5.2, 6.0, and 6.1 with Python 3.13. The configured Workers runtime currently selects Python 3.14.2; that is separate from the host/tooling version.

## Django configuration

```python
CACHES = {
    "default": {
        "BACKEND": "django_cloudflare_kv.backends.KVCache",
        "LOCATION": "DJANGO_CACHE",
        "TIMEOUT": 300,
        "KEY_PREFIX": "myapp",
        "VERSION": 1,
        "KEY_FUNCTION": "django.core.cache.utils.make_key",
        "OPTIONS": {
            "CACHE_NAMESPACE": "myapp-default",
            "OPERATION_LIMIT": 900,
        },
    },
}
```

`BACKEND` selects this class. `LOCATION` names the runtime KV binding and must be a non-empty string. Django applies `TIMEOUT`, `KEY_PREFIX`, `VERSION`, and `KEY_FUNCTION` using its normal backend contract. `KEY_FUNCTION` defaults to Django's standard key function. `OPTIONS.CACHE_NAMESPACE` defaults to `default`, must match `[A-Za-z0-9_-]{1,64}`, and is this library's prefix inside the binding—not a Cloudflare resource ID. `OPTIONS.OPERATION_LIMIT` defaults to 900; it must be an integer from 1 through 900 (booleans are rejected) and bounds binding calls for one top-level cache operation.

## Bind a KV namespace

Declare a classic KV namespace binding named exactly as `LOCATION` in the Worker's Wrangler configuration. The example below uses a placeholder ID for local configuration; replace it with the intended namespace ID in the consumer's own environment.

```jsonc
{
  "compatibility_date": "2026-10-02",
  "compatibility_flags": ["python_workers"],
  "kv_namespaces": [{ "binding": "DJANGO_CACHE", "id": "replace-with-namespace-id" }],
  "build": { "command": "true", "watch_dir": "watch" }
}
```

`watch_dir` keeps Wrangler focused on the intended build inputs and avoids watching generated Worker trees. Bindings are available only within the Workers runtime. Importing or constructing the backend in CPython does not access Workers; storage calls outside Workers raise an actionable `CacheRuntimeError`.

## WSGI entrypoint

For synchronous Django request handling, expose the standard WSGI application through the Workers Python entrypoint:

```python
from demo.dispatch import dispatch
from workers import Request, Response, WorkerEntrypoint

class Default(WorkerEntrypoint):
    async def fetch(self, request: Request) -> Response:
        return await dispatch(self, request)
```

Use the Workers Python package's WSGI adapter and entrypoint conventions for the specific application. This backend bridges each sync cache method through one `pyodide.ffi.run_sync` coroutine. It does not support arbitrary synchronous use outside supported Workers request dispatch.

## Native async cache use

In an async Django view, use Django's native async cache methods; do not wrap sync cache methods in a thread executor:

```python
from django.core.cache import cache

async def view(request):
    await cache.aset("profile:42", {"name": "Ada"}, timeout=60)
    profile = await cache.aget("profile:42")
    return profile
```

## Operational semantics

KV is eventually consistent and may serve cached misses. A successful write is not a promise that a following read, particularly at another location, observes that write immediately. Mutations are best effort, not atomic: `incr`/`decr` can lose concurrent updates; version moves can leave duplicate versions after a delete failure; `add` and `get_or_set` can race. KV throttles writes to roughly one write per key per second. This backend has no retry loop to conceal that throttle.

Logical expiration is enforced by the serialized envelope. KV's physical TTL is at least 60 seconds, so KV can retain a logically expired record longer; reads return a miss at/after the logical deadline and do not delete expired records. `clear()` lists and deletes only this backend namespace within its per-operation budget. `clear() is True` means this pass's listed deletions completed; it does not prove global or immediate emptiness. `set_many()` returns original keys whose individual writes failed; `delete_many()` can partially delete before the first failure propagates.

Aliases sharing a binding and `CACHE_NAMESPACE` share physical keys and `clear()` scope. Choose distinct namespace values for isolated cache scopes. Cached values use Python pickle protocol 4. Treat every value in the bound KV namespace as trusted: a poisoned binding value can execute code when unpickled.

## Documentation

- [Design and architecture](docs/design.md)
- [Configuration reference](docs/configuration.md)
- [Method semantics](docs/semantics.md)
- [Testing and evidence](docs/testing.md)

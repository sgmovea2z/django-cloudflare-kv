# Configuration reference

```python
CACHES = {
    "default": {
        "BACKEND": "django_cloudflare_kv.backends.KVCache",
        "LOCATION": "DJANGO_CACHE",
        "TIMEOUT": 300,
        "KEY_PREFIX": "myapp",
        "VERSION": 1,
        "KEY_FUNCTION": "django.core.cache.utils.make_key",
        "OPTIONS": {"CACHE_NAMESPACE": "myapp-default", "OPERATION_LIMIT": 900},
    }
}
```

| Setting | Meaning | Validation/default |
|---|---|---|
| `BACKEND` | Backend class | `django_cloudflare_kv.backends.KVCache` |
| `LOCATION` | Name of native KV binding | Required, non-empty string; fails during setup with `ImproperlyConfigured` if invalid |
| `TIMEOUT` | Django default cache timeout | Django semantics; `None` means no logical expiry |
| `KEY_PREFIX` | Django logical key prefix | Django semantics |
| `VERSION` | Django key version | Django semantics |
| `KEY_FUNCTION` | Django key canonicalization callable | Django default when omitted |
| `OPTIONS.CACHE_NAMESPACE` | Library-owned key prefix inside the bound KV namespace | Defaults to `default`; `[A-Za-z0-9_-]{1,64}`; not a Cloudflare namespace/resource ID |
| `OPTIONS.OPERATION_LIMIT` | Maximum binding calls for one top-level operation | Defaults to 900; integer 1–900; `bool` rejected |

Declare the KV binding in Wrangler with `binding` equal to `LOCATION`. Configure `compatibility_date` as `2026-10-02` and `compatibility_flags` with `python_workers` for the tested runtime target. Include `build.watch_dir` for the Worker build. Worker runtime Python 3.14.2 is distinct from the Python 3.13 host/tooling matrix.

Aliases with the same binding and `CACHE_NAMESPACE` share keys and clear scope. `clear()` consumes a budget for listing and deleting and can raise `CacheOperationLimitError` before it mutates data. It reports completion of one observed scan; eventual consistency means `True` does not guarantee global emptiness.

Outside Workers, backend construction and no-op `close()` need no binding. Attempting a storage operation raises an actionable `CacheRuntimeError` telling the caller to run in a configured Workers Python runtime with the `LOCATION` binding.

# Design

The backend adapts Django's `BaseCache` contract to the classic Workers KV binding. Native async methods perform binding operations; sync peers bridge exactly one coroutine with `pyodide.ffi.run_sync`. `run_sync(js.Promise.all([...]))` fails in the runtime with a borrowed-proxy `JsException`, so composite operations execute sequentially inside one bridged coroutine. Runtime imports and binding resolution are lazy.

## Key and value layout

Django canonicalizes the logical key once through the configured key function, prefix, and version. The backend hashes its UTF-8 representation with SHA-256 and stores it at `dckv:v1:{CACHE_NAMESPACE}:{sha256hex}`. The digest is 64 lowercase hexadecimal characters.

Each value is JSON with exactly `format=1`, `expires_at` (finite Unix timestamp or null), and `payload` (ASCII base64 of pickle protocol 4 bytes). The complete UTF-8 JSON envelope is limited to 25 MiB. Header/base64/size validation precedes unpickling. Cached `None` is distinct from a miss. Pickle is executable serialization: only use a trusted KV binding and trusted writers.

## Expiration

The backend obtains Django's absolute deadline through `get_backend_timeout`. A read at or after the deadline is a miss and never deletes the physical record. For finite deadlines, writes use physical `expirationTtl=max(60, ceil(expires_at-now))`; no physical TTL is sent for a non-expiring value. Thus logical TTLs below one minute remain effective even though KV retains the record longer.

## Runtime boundary and failures

`CacheBackendError` is the base for `CacheRuntimeError`, `CacheOperationError`, and `CacheSerializationError`; `CacheOperationLimitError` derives from `CacheOperationError`. Django configuration errors use `ImproperlyConfigured`. Missing increments/version changes raise `ValueError`; arithmetic errors retain their Python type. Original causes are chained. No error is converted into an ordinary miss and no retry masks KV throttling.

Each top-level operation counts its binding calls against `OPERATION_LIMIT` and checks known costs before mutation. `clear` lists only the exact physical prefix, follows pages including empty non-final pages, rejects repeated cursors, deduplicates keys, and checks list-plus-delete cost before deleting. It cannot account for unrelated Worker activity.

The backend uses only the configured native classic KV binding. It does not use REST, KV Instant, Durable Objects, thread pools, or an in-memory fallback.

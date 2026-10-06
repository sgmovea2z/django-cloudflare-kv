# Cache method semantics

Cloudflare KV is eventually consistent. Reads can reflect older values or cached misses after a successful write. The backend exposes observed outcomes; it cannot make KV strongly consistent.

| Method | Observed-value and error contract |
|---|---|
| `get` / `aget` | Return the default for a miss or logically expired value. Stored `None` is a hit. Corrupt values raise serialization errors. |
| `set` / `aset` | Return `None` after an accepted write. Timeout `<=0` deletes the physical key. Serialization happens before put. A write may be throttled; no retry loop hides it. |
| `add` / `aadd` | Existing observed live value returns `False`; observed miss attempts a write and returns `True`. Not conditional CAS; races can have multiple apparent winners. |
| `touch` / `atouch` | Missing/expired returns `False`; live entry is rewritten with new logical deadline. Non-positive timeout deletes and returns `True`. True reports accepted action, not global visibility. |
| `delete` / `adelete` | Returns observed live presence, then issues delete regardless. Read and delete are not transactional. |
| `has_key` / `ahas_key`, `__contains__` | Determine presence with a miss sentinel, not truthiness; falsey values and `None` can be present. |
| `get_or_set` / `aget_or_set` | On observed miss, evaluates a synchronous factory once, attempts `add`, then rereads with the computed value as fallback. Races and delayed visibility remain possible; async factories are not accepted. |
| `incr` / `aincr`, `decr` / `adecr` | Read, perform Python arithmetic, and rewrite preserving the original logical expiry. Missing key raises `ValueError`; invalid arithmetic keeps its Python exception. Not atomic; concurrent updates can be lost and writes can be throttled. |
| `incr_version` / `aincr_version`, `decr_version` / `adecr_version` | Read old version, write new version preserving deadline, then delete old. Missing source raises `ValueError`. A failed destination write leaves source; failed source deletion can leave duplicates. No transaction. |
| `get_many` / `aget_many` | Map original requested keys to observed live values; omit misses, preserve `None`, and deduplicate identical logical keys in request order. Preflight operation budget. |
| `set_many` / `aset_many` | Validate and serialize all inputs before mutation. Success returns `[]`; individual platform write failures continue and return original failed keys in input order. Not atomic. |
| `delete_many` / `adelete_many` | Return `None` on completion. First platform failure propagates; earlier individual deletes may already have completed. |
| `clear` / `aclear` | Delete listed keys under exact physical prefix after complete listing and budget preflight. Return `True` when this pass's deletes completed; this does not prove global emptiness or remove concurrent/unobserved writes. Partial delete failure propagates. |
| `close` / `aclose` | No-op; no binding or runtime cleanup. |

Each operation has a binding-call budget. `OPERATION_LIMIT` applies per top-level call, not across all cache calls in a Worker request. `clear` may fail before deleting if the required scan/delete budget exceeds the limit.

## Expiration and write limits

The serialized `expires_at` governs logical expiration. At or after that timestamp, reads return a miss and do not delete the stale physical record. KV physical expiration is at least 60 seconds; KV can retain expired envelopes longer. Writes are limited to roughly one per key per second. The backend performs no backoff/retry loop, so callers can receive an operation error during throttling.

`CACHE_NAMESPACE` is a library-owned prefix inside `LOCATION`, not a Cloudflare resource identifier. Configured aliases sharing both values share storage and clear scope. Values are pickled; a poisoned binding can execute code during unpickling, so binding contents and writers must be trusted.

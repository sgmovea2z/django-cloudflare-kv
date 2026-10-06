from __future__ import annotations

# pyright: basic
# pyright: reportMissingImports=false
# pyright: reportArgumentType=false, reportReturnType=false
# pyright: reportAttributeAccessIssue=false
from urllib.parse import unquote

from workers import Response

from django_cloudflare_kv.backends import KVCache
from django_cloudflare_kv.errors import CacheBackendError, CacheSerializationError
from django_cloudflare_kv.keys import make_physical_key
from django_cloudflare_kv.runtime import Runtime


def _response(data: object, status: int = 200) -> Response:
    return Response.json(data, status=status)


async def dispatch_f3_route(path: str) -> Response | None:
    segments = path.strip("/").split("/")
    if len(segments) == 4 and segments[0] == "f3-corrupt":
        return await _corrupt_value(
            segments[1], unquote(segments[2]), unquote(segments[3])
        )
    if len(segments) == 4 and segments[0] == "f3-clear":
        return await _clear_namespace(
            segments[1], unquote(segments[2]), unquote(segments[3])
        )
    return None


async def _corrupt_value(mode: str, physical_key: str, logical_key: str) -> Response:
    payloads = {
        "invalid-json": "{deliberately-invalid-json",
        "invalid-base64": '{"format":1,"expires_at":null,"payload":"%%%"}',
    }
    payload = payloads.get(mode)
    if payload is None:
        return _response({"error": "unknown corrupt mode"}, status=400)

    binding = Runtime().resolve_binding("DJANGO_CACHE")
    await binding.put(physical_key, payload)
    cache = KVCache(
        {"LOCATION": "DJANGO_CACHE", "OPTIONS": {"CACHE_NAMESPACE": "f3-corrupt"}}
    )
    try:
        value = await cache.aget(logical_key, "CALLER_DEFAULT")
    except CacheSerializationError as error:
        cause = error.__cause__
        return _response(
            {
                "payload": payload,
                "physical_key": physical_key,
                "error_type": type(error).__name__,
                "is_backend_error": isinstance(error, CacheBackendError),
                "error_message": str(error),
                "cause_type": type(cause).__name__ if cause is not None else None,
                "cause_message": str(cause) if cause is not None else None,
                "returned_default": False,
            }
        )
    return _response(
        {
            "payload": payload,
            "physical_key": physical_key,
            "error_type": None,
            "is_backend_error": False,
            "error_message": "",
            "cause_type": None,
            "cause_message": None,
            "returned_default": value == "CALLER_DEFAULT",
            "value": value,
        },
        status=500,
    )


async def _clear_namespace(
    suffix: str, cleared_logical: str, sibling_logical: str
) -> Response:
    cleared = KVCache(
        {
            "LOCATION": "DJANGO_CACHE",
            "OPTIONS": {"CACHE_NAMESPACE": "f3-cleared"},
        }
    )
    sibling = KVCache(
        {
            "LOCATION": "DJANGO_CACHE",
            "OPTIONS": {"CACHE_NAMESPACE": "f3-sibling"},
        }
    )
    cleared_key = make_physical_key(cleared, cleared_logical)
    sibling_key = make_physical_key(sibling, sibling_logical)
    await cleared.aset(cleared_logical, f"cleared-{suffix}")
    await sibling.aset(sibling_logical, f"sibling-{suffix}")
    binding = Runtime().resolve_binding("DJANGO_CACHE")
    cleared_envelope_before = await binding.get(cleared_key)
    written_sibling_envelope = await binding.get(sibling_key)
    clear_result = await cleared.aclear()
    return _response(
        {
            "clear": clear_result,
            "cleared_physical_key": cleared_key,
            "sibling_physical_key": sibling_key,
            "cleared_envelope_before": cleared_envelope_before,
            "written_sibling_envelope": written_sibling_envelope,
            "suffix": suffix,
        }
    )

from __future__ import annotations

# pyright: basic
# pyright: reportMissingImports=false
# pyright: reportArgumentType=false, reportReturnType=false
# pyright: reportAttributeAccessIssue=false
from urllib.parse import unquote

from workers import Response


def _response(data: object) -> Response:
    return Response.json(data)


async def dispatch_test_route(path: str) -> Response | None:
    segments = path.strip("/").split("/")
    if segments[0] in {"f3-corrupt", "f3-clear"}:
        from f3_routes import dispatch_f3_route

        return await dispatch_f3_route(path)
    if len(segments) == 2 and segments[0] == "native-seed":
        return await native_seed(unquote(segments[1]))
    if len(segments) == 2 and segments[0] == "native-suite":
        return await native_suite(unquote(segments[1]))
    if len(segments) == 2 and segments[0] in {"ttl-set", "ttl-read"}:
        return await ttl_probe(unquote(segments[1]), segments[0] == "ttl-read")
    if path == "/django-features":
        return await django_features()
    return None


def sync_seed(suffix: str) -> None:
    from django_cloudflare_kv.backends import KVCache

    cache = KVCache(
        {
            "LOCATION": "DJANGO_CACHE",
            "TIMEOUT": None,
            "OPTIONS": {"CACHE_NAMESPACE": "sync-suite", "OPERATION_LIMIT": 900},
        }
    )
    cache.set(f"bytes-{suffix}", b"bytes")
    cache.set(f"none-{suffix}", None, timeout=None)
    cache.set(f"structured-{suffix}", {"items": [1, "two"]})
    cache.set(f"touch-{suffix}", "touch")
    cache.set(f"delete-{suffix}", "delete")
    cache.set(f"incr-{suffix}", 1)
    cache.set(f"decr-{suffix}", 1)
    cache.set(f"version-{suffix}", "version")
    cache.set(f"version-down-{suffix}", "version")
    sibling = KVCache(
        {"LOCATION": "DJANGO_CACHE", "OPTIONS": {"CACHE_NAMESPACE": "sync-sibling"}}
    )
    sibling.set(f"survives-{suffix}", "sibling-value")


def sync_suite(suffix: str) -> dict[str, object]:
    from django_cloudflare_kv.backends import KVCache

    cache = KVCache(
        {
            "LOCATION": "DJANGO_CACHE",
            "TIMEOUT": None,
            "OPTIONS": {"CACHE_NAMESPACE": "sync-suite", "OPERATION_LIMIT": 900},
        }
    )
    sibling = KVCache(
        {"LOCATION": "DJANGO_CACHE", "OPTIONS": {"CACHE_NAMESPACE": "sync-sibling"}}
    )
    many_values = cache.get_many([f"bytes-{suffix}"])
    result = {
        "bytes": str(cache.get(f"bytes-{suffix}")),
        "none": cache.get(f"none-{suffix}", "missing"),
        "structured": cache.get(f"structured-{suffix}"),
        "add": cache.add(f"add-{suffix}", "value"),
        "touch": cache.touch(f"touch-{suffix}", timeout=None),
        "delete": cache.delete(f"delete-{suffix}"),
        "get_many": {key: str(value) for key, value in many_values.items()},
        "get_or_set": cache.get_or_set(f"get-or-set-{suffix}", "value"),
        "has_key": cache.has_key(f"structured-{suffix}"),
        "incr": cache.incr(f"incr-{suffix}"),
        "decr": cache.decr(f"decr-{suffix}"),
        "set_many": cache.set_many({f"bulk-{suffix}": "value"}),
        "delete_many": cache.delete_many([f"bulk-delete-{suffix}"]),
        "incr_version": cache.incr_version(f"version-{suffix}"),
        "decr_version": cache.decr_version(f"version-down-{suffix}"),
    }
    result["clear"] = cache.clear()
    result["sibling"] = sibling.get(f"survives-{suffix}")
    cache.close()
    return {
        "methods": [
            "add",
            "get",
            "set",
            "touch",
            "delete",
            "get_many",
            "get_or_set",
            "has_key",
            "incr",
            "decr",
            "set_many",
            "delete_many",
            "clear",
            "incr_version",
            "decr_version",
            "close",
        ],
        "result": result,
    }


async def native_seed(suffix: str) -> Response:
    from django_cloudflare_kv.backends import KVCache

    cache = KVCache(
        {
            "LOCATION": "DJANGO_CACHE",
            "TIMEOUT": None,
            "OPTIONS": {"CACHE_NAMESPACE": "native-suite", "OPERATION_LIMIT": 900},
        }
    )
    await cache.aset(f"bytes-{suffix}", b"bytes")
    await cache.aset(f"none-{suffix}", None, timeout=None)
    await cache.aset(f"structured-{suffix}", {"items": [1, "two"]})
    await cache.aset(f"touch-{suffix}", "touch")
    await cache.aset(f"delete-{suffix}", "delete")
    await cache.aset(f"incr-{suffix}", 1)
    await cache.aset(f"decr-{suffix}", 1)
    await cache.aset(f"version-{suffix}", "version")
    await cache.aset(f"version-down-{suffix}", "version")
    sibling = KVCache(
        {
            "LOCATION": "DJANGO_CACHE",
            "OPTIONS": {"CACHE_NAMESPACE": "native-sibling"},
        }
    )
    await sibling.aset(f"survives-{suffix}", "sibling-value")

    return _response({"seeded": True})


async def native_suite(suffix: str) -> Response:
    from django_cloudflare_kv.backends import KVCache

    cache = KVCache(
        {
            "LOCATION": "DJANGO_CACHE",
            "TIMEOUT": None,
            "OPTIONS": {"CACHE_NAMESPACE": "native-suite", "OPERATION_LIMIT": 900},
        }
    )
    sibling = KVCache(
        {
            "LOCATION": "DJANGO_CACHE",
            "OPTIONS": {"CACHE_NAMESPACE": "native-sibling"},
        }
    )
    many_values = await cache.aget_many([f"bytes-{suffix}"])
    result = {
        "bytes": str(await cache.aget(f"bytes-{suffix}")),
        "none": await cache.aget(f"none-{suffix}", "missing"),
        "structured": await cache.aget(f"structured-{suffix}"),
        "add": await cache.aadd(f"add-{suffix}", "value"),
        "touch": await cache.atouch(f"touch-{suffix}", timeout=None),
        "delete": await cache.adelete(f"delete-{suffix}"),
        "get_many": {key: str(value) for key, value in many_values.items()},
        "get_or_set": await cache.aget_or_set(f"get-or-set-{suffix}", "value"),
        "has_key": await cache.ahas_key(f"structured-{suffix}"),
        "incr": await cache.aincr(f"incr-{suffix}"),
        "decr": await cache.adecr(f"decr-{suffix}"),
        "set_many": await cache.aset_many({f"bulk-{suffix}": "value"}),
        "delete_many": await cache.adelete_many([f"bulk-delete-{suffix}"]),
        "incr_version": await cache.aincr_version(f"version-{suffix}"),
        "decr_version": await cache.adecr_version(f"version-down-{suffix}"),
    }
    result["clear"] = await cache.aclear()
    result["sibling"] = await sibling.aget(f"survives-{suffix}")
    await cache.aclose()
    return _response(
        {
            "methods": [
                "aadd",
                "aget",
                "aset",
                "atouch",
                "adelete",
                "aget_many",
                "aget_or_set",
                "ahas_key",
                "aincr",
                "adecr",
                "aset_many",
                "adelete_many",
                "aclear",
                "aincr_version",
                "adecr_version",
                "aclose",
            ],
            "result": result,
        }
    )


async def ttl_probe(key: str, check: bool = False) -> Response:
    from django_cloudflare_kv.backends import KVCache
    from django_cloudflare_kv.keys import make_physical_key
    from django_cloudflare_kv.runtime import Runtime

    cache = KVCache({"LOCATION": "DJANGO_CACHE", "OPTIONS": {"CACHE_NAMESPACE": "ttl"}})
    physical_key = make_physical_key(cache, key)
    if check:
        logical_value = await cache.aget(key, "expired")
        physical_value = (
            await Runtime().resolve_binding("DJANGO_CACHE").get(physical_key)
        )
        return _response(
            {
                "logical": logical_value,
                "physical_present": physical_value is not None,
                "envelope": physical_value,
            }
        )
    await cache.aset(key, "short-lived", timeout=1)
    return _response({"physical_key": physical_key})


async def django_features() -> Response:
    from django.conf import settings
    from django.http import HttpRequest, HttpResponse
    from django.template import Context, Engine
    from django.test import RequestFactory
    from django.views.decorators.cache import cache_page

    if not settings.configured:
        settings.configure(
            SECRET_KEY="integration",
            ALLOWED_HOSTS=["testserver"],
            USE_I18N=False,
            USE_TZ=False,
            CACHES={
                "default": {
                    "BACKEND": "django_cloudflare_kv.backends.KVCache",
                    "LOCATION": "DJANGO_CACHE",
                    "TIMEOUT": 60,
                    "OPTIONS": {"CACHE_NAMESPACE": "django-features"},
                }
            },
        )
    template = Engine(builtins=["django.templatetags.cache"]).from_string(
        "{% cache 60 integration-fragment %}fragment-hit{% endcache %}"
    )
    rendered = template.render(Context())
    page_count = {"value": 0}

    def page_view(_request: HttpRequest) -> HttpResponse:
        page_count["value"] += 1
        return HttpResponse("page-hit")

    cached_view = cache_page(60)(page_view)
    factory = RequestFactory()
    _ = cached_view(factory.get("/page"))
    response = cached_view(factory.get("/page"))
    return _response(
        {
            "fragment": rendered,
            "page": response.content.decode(),
            "page_view_count": page_count["value"],
        }
    )

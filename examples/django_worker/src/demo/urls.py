"""Public routes for the demo consumer."""

from django.core.cache import caches
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.template import engines
from django.urls import path
from django.views.decorators.cache import cache_page

from django_cloudflare_kv.backends import KVCache


def home(request: HttpRequest) -> HttpResponse:
    """Render a cached template fragment for the consumer home route."""
    template = engines["django"].from_string(
        "{% load cache %}{% cache 60 example-fragment %}fragment-hit{% endcache %}"
    )
    return HttpResponse(template.render({}, request))


@cache_page(60)
def cached(_request: HttpRequest) -> HttpResponse:
    """Return a response cached by Django's cache_page decorator."""
    return HttpResponse("page-hit")


async def asgi_cache_round_trip(
    _request: HttpRequest, key: str, value: str
) -> JsonResponse:
    """Write and read one value via Django's native async cache API."""
    cache = caches["default"]
    if not isinstance(cache, KVCache):
        message = "The default cache must use the Workers KV backend."
        raise ImproperlyConfigured(message)
    await cache.aset(key, value)
    cached_value = await cache.aget(key)
    return JsonResponse({"value": cached_value})


urlpatterns = [
    path("", home),
    path("cached", cached),
    path("asgi/cache/<str:key>/<str:value>", asgi_cache_round_trip),
]

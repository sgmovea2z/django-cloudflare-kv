"""Minimal database-free Django configuration for the example Worker."""

SECRET_KEY = "local-example-only"  # noqa: S105 — local, non-secret demo value
DEBUG = False
ROOT_URLCONF = "demo.urls"
ALLOWED_HOSTS = ["*"]
USE_I18N = False
USE_TZ = False
TEMPLATES = [
    {"BACKEND": "django.template.backends.django.DjangoTemplates", "APP_DIRS": True}
]
CACHES = {
    "default": {
        "BACKEND": "django_cloudflare_kv.backends.KVCache",
        "LOCATION": "DJANGO_CACHE",
        "TIMEOUT": 300,
        "KEY_PREFIX": "django-example",
        "VERSION": 1,
        "OPTIONS": {"CACHE_NAMESPACE": "django-example", "OPERATION_LIMIT": 900},
    }
}
MIDDLEWARE = [
    "django.middleware.cache.UpdateCacheMiddleware",
    "django.middleware.cache.FetchFromCacheMiddleware",
]
CACHE_MIDDLEWARE_SECONDS = 60
CACHE_MIDDLEWARE_KEY_PREFIX = "django-example"

"""ASGI application for Django's native async consumers."""

import os

_ = os.environ.setdefault("DJANGO_SETTINGS_MODULE", "demo.settings")

from django.core.asgi import get_asgi_application  # noqa: E402

application = get_asgi_application()

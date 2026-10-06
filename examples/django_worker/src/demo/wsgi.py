"""WSGI application served by the Workers Python bridge."""

import os

_ = os.environ.setdefault("DJANGO_SETTINGS_MODULE", "demo.settings")

from django.core.wsgi import get_wsgi_application  # noqa: E402

application = get_wsgi_application()

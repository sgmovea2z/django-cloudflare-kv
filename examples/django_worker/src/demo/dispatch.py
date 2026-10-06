# pyright: basic
# pyright: reportMissingImports=false
# pyright: reportArgumentType=false, reportReturnType=false
# pyright: reportAttributeAccessIssue=false

"""Dispatch the consumer's HTTP requests to Django's WSGI or ASGI adapter."""

from urllib.parse import urlsplit

from demo.asgi import application as asgi_application
from demo.wsgi import application as wsgi_application
from workers import Request, Response, WorkerEntrypoint
from workers.asgi import fetch as asgi_fetch
from workers.wsgi import fetch as wsgi_fetch


async def dispatch(worker: WorkerEntrypoint, request: Request) -> Response:
    """Use ASGI for the `/asgi/` URL prefix and WSGI for all other requests."""
    path = urlsplit(str(request.url)).path
    if path.startswith("/asgi/"):
        return await asgi_fetch(asgi_application, request, worker.env, worker.ctx)
    return await wsgi_fetch(wsgi_application, request, worker.env)

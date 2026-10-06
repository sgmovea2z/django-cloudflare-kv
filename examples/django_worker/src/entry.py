# pyright: basic
# pyright: reportMissingImports=false
# pyright: reportArgumentType=false, reportReturnType=false
# pyright: reportAttributeAccessIssue=false

"""Workers entrypoint for the documented Django consumer."""

from demo.dispatch import dispatch
from workers import Request, Response, WorkerEntrypoint


class Default(WorkerEntrypoint):
    """Dispatch Worker requests to Django."""

    async def fetch(self, request: Request) -> Response:
        """Dispatch ASGI-eligible paths and retain WSGI as the default."""
        return await dispatch(self, request)

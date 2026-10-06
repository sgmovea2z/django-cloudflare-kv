"""Public errors raised at the Django cache runtime boundary."""


class CacheBackendError(RuntimeError):
    """Base class for errors raised by the Django Cloudflare KV backend."""


class CacheRuntimeError(CacheBackendError):
    """The active Workers runtime or configured cache binding is unavailable."""


class CacheOperationError(CacheBackendError):
    """A native KV operation failed."""


class CacheSerializationError(CacheBackendError):
    """A cache value could not be serialized or deserialized."""


class CacheOperationLimitError(CacheOperationError):
    """An operation exceeded a Cloudflare KV operation limit."""

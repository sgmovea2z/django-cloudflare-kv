"""Minimal test-owned Worker for verifying native KV and WSGI bridges."""

# pyright: basic
# pyright: reportMissingImports=false
# pyright: reportArgumentType=false, reportReturnType=false
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from urllib.parse import unquote, urlsplit

import js
from pyodide.ffi import run_sync
from workers import Response, WorkerEntrypoint
from workers.wsgi import fetch as wsgi_fetch

from django_cloudflare_kv.backends import KVCache
from django_cloudflare_kv.errors import CacheRuntimeError
from django_cloudflare_kv.runtime import Runtime


def _json_response(body: object, status: int = 200) -> Response:
    return Response.json(body, status=status)


def _wsgi_application(  # noqa: C901, PLR0912, PLR0915 -- one runtime probe reports all SDK behaviors
    environ: dict[str, object],
    start_response: Callable[[str, list[tuple[str, str]]], None],
) -> list[bytes]:
    """Exercise the SDK's genuine WSGI path and sync KV bridge."""
    path = str(environ["PATH_INFO"])
    if path.startswith("/sync-seed/"):
        from consumer_routes import sync_seed

        sync_seed(path.removeprefix("/sync-seed/"))
        start_response("200 OK", [("Content-Type", "application/json")])
        return [b'{"seeded": true}']
    if path.startswith("/sync-suite/"):
        from consumer_routes import sync_suite

        payload = json.dumps(sync_suite(path.removeprefix("/sync-suite/"))).encode()
        start_response("200 OK", [("Content-Type", "application/json")])
        return [payload]
    if path.startswith("/cache/"):
        _, _, cache_key, cache_value = path.split("/", 3)
        cache = KVCache({"LOCATION": "DJANGO_CACHE", "TIMEOUT": None})
        cache.set(unquote(cache_key), unquote(cache_value))
        cached_value = cache.get(unquote(cache_key))
        payload = json.dumps({"value": cached_value}).encode()
        start_response("200 OK", [("Content-Type", "application/json")])
        return [payload]
    key = unquote(path.removeprefix("/wsgi/"))
    environment = environ["workers.env"]
    namespace = getattr(environment, "DJANGO_CACHE")
    value = run_sync(namespace.get(key))

    async def coroutine_probe() -> str | None:
        await namespace.put(f"{key}:coroutine", "coroutine-write")
        return await namespace.get(f"{key}:coroutine")

    probe_result: dict[str, object] = {}
    coroutine = coroutine_probe()
    try:
        probe_result["coroutine_probe"] = {
            "outcome": "resolved",
            "value": run_sync(coroutine),
        }
    except Exception as error:
        coroutine.close()
        probe_result["coroutine_probe"] = {
            "outcome": "error",
            "type": type(error).__name__,
            "message": str(error),
        }

    try:
        probe_result["promise_probe"] = {
            "outcome": "resolved",
            "value": run_sync(namespace.get(key)),
        }
    except Exception as error:
        probe_result["promise_probe"] = {
            "outcome": "error",
            "type": type(error).__name__,
            "message": str(error),
        }

    composition: dict[str, object] = {}
    try:
        combined = js.Promise.all(
            [namespace.get(key), namespace.get(f"{key}:coroutine")]
        )
        composition["construction"] = {
            "outcome": "resolved",
            "type": type(combined).__name__,
            "shape": (
                "js.Promise.all([namespace.get(key), "
                "namespace.get(key + ':coroutine')])"
            ),
        }
    except Exception as error:
        composition["construction"] = {
            "outcome": "error",
            "type": type(error).__name__,
            "message": str(error),
        }
        combined = None

    if combined is not None:
        try:
            composed_result = run_sync(combined)
            composition["run_sync"] = {
                "outcome": "resolved",
                "type": type(composed_result).__name__,
                "shape": "run_sync(combined); capture type only",
            }
        except Exception as error:
            composition["run_sync"] = {
                "outcome": "error",
                "type": type(error).__name__,
                "message": str(error),
                "shape": "run_sync(combined); exception captured before conversion",
            }
            composed_result = None

        if composed_result is None:
            try:
                retry_result = run_sync(
                    js.Promise.all(
                        [namespace.get(key), namespace.get(f"{key}:coroutine")]
                    )
                )
                composition["retry_run_sync"] = {
                    "outcome": "resolved",
                    "type": type(retry_result).__name__,
                }
                composed_result = retry_result
            except Exception as error:
                composition["retry_run_sync"] = {
                    "outcome": "error",
                    "type": type(error).__name__,
                    "message": str(error),
                }

        try:
            failed_consume_result = run_sync(
                js.Promise.all([namespace.get(key), namespace.get(f"{key}:coroutine")])
            )
        except Exception as error:
            composition["to_py"] = {
                "outcome": "not_run",
                "reason": "run_sync failed before to_py",
            }
            composition["index"] = {
                "outcome": "not_run",
                "reason": "run_sync failed before indexing",
            }
            composition["iteration"] = {
                "outcome": "not_run",
                "reason": "run_sync failed before iteration",
            }
            composition["failed_consume_run_sync"] = {
                "outcome": "error",
                "type": type(error).__name__,
                "message": str(error),
            }
        else:
            composition["failed_consume_run_sync"] = {
                "outcome": "resolved",
                "type": type(failed_consume_result).__name__,
            }
            for conversion_name, conversion in (
                ("to_py", failed_consume_result.to_py),
                ("index", lambda: failed_consume_result[0]),
                ("iteration", lambda: list(failed_consume_result)),
            ):
                try:
                    converted_value = conversion()
                    composition[conversion_name] = {
                        "outcome": "resolved",
                        "type": type(converted_value).__name__,
                        "value": converted_value,
                    }
                except Exception as error:
                    composition[conversion_name] = {
                        "outcome": "error",
                        "type": type(error).__name__,
                        "message": str(error),
                    }

        try:
            baseline = run_sync(namespace.get(key))
            probe_result["single_awaitable_baseline"] = {
                "outcome": "resolved",
                "value": baseline,
                "type": type(baseline).__name__,
                "shape": "run_sync(namespace.get(key))",
            }
        except Exception as error:
            probe_result["single_awaitable_baseline"] = {
                "outcome": "error",
                "type": type(error).__name__,
                "message": str(error),
                "shape": "run_sync(namespace.get(key))",
            }

        try:
            all_settled = js.Promise.allSettled(
                [namespace.get(key), namespace.get(f"{key}:coroutine")]
            )
            settled_result = run_sync(all_settled)
            composition["all_settled"] = {
                "outcome": "resolved",
                "type": type(settled_result).__name__,
            }
            try:
                composition["all_settled_to_py"] = {
                    "outcome": "resolved",
                    "type": type(settled_result.to_py()).__name__,
                }
            except Exception as error:
                composition["all_settled_to_py"] = {
                    "outcome": "error",
                    "type": type(error).__name__,
                    "message": str(error),
                }
        except Exception as error:
            composition["all_settled"] = {
                "outcome": "error",
                "type": type(error).__name__,
                "message": str(error),
            }

        try:
            fresh_result = run_sync(
                js.Promise.all([namespace.get(key), namespace.get(f"{key}:coroutine")])
            )
        except Exception as error:
            fresh_result = None
            composition["alternate_run_sync"] = {
                "outcome": "error",
                "type": type(error).__name__,
                "message": str(error),
            }

        if fresh_result is not None:
            composition["alternate_run_sync"] = {
                "outcome": "resolved",
                "type": type(fresh_result).__name__,
            }
            try:
                converted = fresh_result.to_py()
                composition["to_py"] = {
                    "outcome": "resolved",
                    "type": type(converted).__name__,
                    "value": converted,
                }
            except Exception as error:
                composition["to_py"] = {
                    "outcome": "error",
                    "type": type(error).__name__,
                    "message": str(error),
                }
                converted = None

            try:
                indexed = fresh_result[0]
                composition["index"] = {
                    "outcome": "resolved",
                    "type": type(indexed).__name__,
                    "value": str(indexed),
                }
            except Exception as error:
                composition["index"] = {
                    "outcome": "error",
                    "type": type(error).__name__,
                    "message": str(error),
                }

            try:
                composition["iteration"] = {
                    "outcome": "resolved",
                    "value": list(fresh_result),
                }
            except Exception as error:
                composition["iteration"] = {
                    "outcome": "error",
                    "type": type(error).__name__,
                    "message": str(error),
                }

            try:
                shallow = fresh_result.to_py(depth=1)
                composition["to_py_depth_1"] = {
                    "outcome": "resolved",
                    "type": type(shallow).__name__,
                }
            except Exception as error:
                composition["to_py_depth_1"] = {
                    "outcome": "error",
                    "type": type(error).__name__,
                    "message": str(error),
                }

            try:
                shallow_items = [str(fresh_result[index]) for index in range(2)]
                composition["direct_indexing"] = {
                    "outcome": "resolved",
                    "value": shallow_items,
                }
            except Exception as error:
                composition["direct_indexing"] = {
                    "outcome": "error",
                    "type": type(error).__name__,
                    "message": str(error),
                }

            try:
                composition["dict"] = {
                    "outcome": "resolved",
                    "value": dict(fresh_result),
                }
            except Exception as error:
                composition["dict"] = {
                    "outcome": "error",
                    "type": type(error).__name__,
                    "message": str(error),
                }
    else:
        for step in (
            "run_sync",
            "to_py",
            "index",
            "iteration",
            "all_settled",
            "to_py_depth_1",
            "direct_indexing",
            "dict",
        ):
            composition[step] = {
                "outcome": "not_run",
                "reason": "composition construction failed",
            }
    if "to_py" not in composition:
        composition["to_py"] = {
            "outcome": "not_run",
            "reason": "composed run_sync did not produce a result",
        }
    for step in (
        "index",
        "iteration",
        "all_settled",
        "to_py_depth_1",
        "direct_indexing",
        "dict",
    ):
        if step not in composition:
            composition[step] = {
                "outcome": "not_run",
                "reason": "composed result unavailable",
            }
    if "alternate_run_sync" not in composition:
        composition["alternate_run_sync"] = {
            "outcome": "not_run",
            "reason": "composition construction failed",
        }
    probe_result["composition_probe"] = composition

    payload = json.dumps({"value": value, **probe_result}).encode()
    start_response("200 OK", [("Content-Type", "application/json")])
    return [payload]


class Default(WorkerEntrypoint):
    """Serve direct async probes and pass WSGI probes to the SDK bridge."""

    async def fetch(self, request: object) -> Response:
        parsed = urlsplit(str(request.url))
        segments = parsed.path.strip("/").split("/")
        if parsed.path == "/health":
            return Response("ready")
        if parsed.path == "/versions":
            import pyodide

            return _json_response(
                {
                    "python": sys.version,
                    "pyodide": pyodide.__version__,
                    "workerd": str(js.navigator.userAgent),
                }
            )
        if parsed.path.startswith("/asgi/"):
            from demo.dispatch import dispatch

            return await dispatch(self, request)
        from consumer_routes import dispatch_test_route

        test_response = await dispatch_test_route(parsed.path)
        if test_response is not None:
            return test_response
        if len(segments) == 3 and segments[0] == "native":
            key, value = map(unquote, segments[1:])
            namespace = Runtime().resolve_binding("DJANGO_CACHE")
            await namespace.put(key, value)
            result = await namespace.get(key)
            return _json_response({"put": True, "get": result})
        if len(segments) == 2 and segments[0] == "diagnostic":
            namespace = Runtime().resolve_binding("DJANGO_CACHE")
            result = await namespace.get(unquote(segments[1]))
            return _json_response({"value": result})
        if segments[0] == "missing-binding":
            try:
                Runtime().resolve_binding("MISSING_BINDING")
            except CacheRuntimeError as error:
                assert isinstance(error.__cause__, AttributeError)
                assert "MISSING_BINDING" in str(error)
                return _json_response(
                    {
                        "type": type(error).__name__,
                        "message": str(error),
                        "cause_type": (
                            type(error.__cause__).__name__
                            if error.__cause__ is not None
                            else None
                        ),
                        "cause_message": (
                            str(error.__cause__)
                            if error.__cause__ is not None
                            else None
                        ),
                    },
                    status=500,
                )
            return _json_response({"error": "missing binding resolved"}, 500)
        return await wsgi_fetch(_wsgi_application, request, self.env)

"""Real local Worker tests for the Python runtime boundary."""

from __future__ import annotations

import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import quote

import pytest

from django_cloudflare_kv.backends import KVCache
from django_cloudflare_kv.codec import decode_value
from django_cloudflare_kv.keys import make_physical_key

from .runtime_gate_support import (
    WorkerProcess,
    decode_diagnostic_result,
    decode_missing_binding_result,
    decode_native_result,
    decode_versions_result,
    decode_wsgi_result,
    request,
)

ROOT = Path(__file__).resolve().parents[2]


def test_package_import_does_not_load_worker_runtime() -> None:
    """Given CPython, package import leaves Workers and Pyodide unloaded."""
    script = f"""
import sys
sys.path.insert(0, {str(ROOT / "src")!r})
import django_cloudflare_kv
assert not any(name == 'workers' or name.startswith('workers.') for name in sys.modules)
assert not any(name == 'pyodide' or name.startswith('pyodide.') for name in sys.modules)
print('clean-import')
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.stdout.strip() == "clean-import"


def test_runtime_construction_does_not_resolve_worker_bindings() -> None:
    """Given CPython, constructing Runtime does not import or resolve bindings."""
    script = f"""
import sys
sys.path.insert(0, {str(ROOT / "src")!r})
from django_cloudflare_kv.runtime import Runtime
runtime = Runtime()
assert not any(name == 'workers' or name.startswith('workers.') for name in sys.modules)
assert not any(name == 'pyodide' or name.startswith('pyodide.') for name in sys.modules)
print(type(runtime).__name__)
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.stdout.strip() == "Runtime"


def test_runtime_boundary_fails_with_public_error_outside_workers() -> None:
    """Given CPython, binding resolution chains its unavailable-runtime cause."""
    script = f"""
import sys
sys.path.insert(0, {str(ROOT / "src")!r})
from django_cloudflare_kv.errors import CacheRuntimeError
from django_cloudflare_kv.runtime import Runtime
try:
    Runtime().resolve_binding('DJANGO_CACHE')
except CacheRuntimeError as error:
    assert isinstance(error.__cause__, ModuleNotFoundError)
    assert 'Workers runtime' in str(error)
else:
    raise AssertionError('binding resolution unexpectedly succeeded in CPython')
print('chained-unavailable-runtime')
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.stdout.strip() == "chained-unavailable-runtime"


def test_public_error_hierarchy_is_complete() -> None:
    script = f"""
import sys
sys.path.insert(0, {str(ROOT / "src")!r})
from django_cloudflare_kv.errors import (
    CacheBackendError,
    CacheOperationError,
    CacheOperationLimitError,
    CacheRuntimeError,
    CacheSerializationError,
)
assert issubclass(CacheRuntimeError, CacheBackendError)
assert issubclass(CacheOperationError, CacheBackendError)
assert issubclass(CacheSerializationError, CacheBackendError)
assert issubclass(CacheOperationLimitError, CacheOperationError)
print('complete-error-hierarchy')
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.stdout.strip() == "complete-error-hierarchy"


@pytest.mark.integration
@pytest.mark.runtime_gate
def test_real_worker_resolves_binding_and_awaits_native_kv_operations(
    worker_process: WorkerProcess,
) -> None:
    """Given local KV, Worker async code writes and reads native binding data."""
    key = f"runtime-gate-{uuid.uuid4().hex}"
    value = f"binary-value-{uuid.uuid4().hex}"
    status, body = request(worker_process["port"], f"/native/{key}/{value}")

    assert status == 200, body
    result = decode_native_result(body)
    assert result == {"put": True, "get": value}

    diagnostic_status, diagnostic_body = request(
        worker_process["port"], f"/diagnostic/{key}"
    )
    assert diagnostic_status == 200
    result = decode_diagnostic_result(diagnostic_body)
    assert result == {"value": value}


@pytest.mark.integration
@pytest.mark.runtime_gate
def test_wsgi_sync_bridge_and_run_sync_stop_gate_shapes(
    worker_process: WorkerProcess,
) -> None:
    """Given active JSPI fetch, capture coroutine and JS-promise bridge results."""
    key = f"runtime-gate-wsgi-{uuid.uuid4().hex}"
    value = f"wsgi-value-{uuid.uuid4().hex}"
    put_status, put_body = request(worker_process["port"], f"/native/{key}/{value}")
    assert put_status == 200
    assert decode_native_result(put_body)["get"] == value

    wsgi_status, wsgi_body = request(worker_process["port"], f"/wsgi/{key}")
    assert wsgi_status == 200
    result = decode_wsgi_result(wsgi_body)
    assert result["value"] == value
    assert result["coroutine_probe"] == {
        "outcome": "resolved",
        "value": "coroutine-write",
    }
    assert result["promise_probe"] == {"outcome": "resolved", "value": value}
    assert result["single_awaitable_baseline"]["outcome"] == "resolved"
    assert result["single_awaitable_baseline"]["value"] == value
    assert result["single_awaitable_baseline"]["type"] == "str"
    assert (
        result["single_awaitable_baseline"]["shape"] == "run_sync(namespace.get(key))"
    )
    composition_probe = result["composition_probe"]
    assert composition_probe["construction"]["outcome"] == "resolved"
    assert "js.Promise.all" in str(composition_probe["construction"]["shape"])
    assert composition_probe["run_sync"]["outcome"] == "error"
    assert composition_probe["run_sync"]["type"] == "JsException"
    assert "borrowed proxy" in str(composition_probe["run_sync"]["message"])
    assert "before conversion" in str(composition_probe["run_sync"]["shape"])
    assert composition_probe["retry_run_sync"]["outcome"] == "error"
    assert composition_probe["failed_consume_run_sync"]["outcome"] == "error"
    assert composition_probe["to_py"]["outcome"] == "not_run"
    assert composition_probe["iteration"]["outcome"] == "not_run"
    assert composition_probe["index"]["outcome"] == "not_run"
    assert composition_probe["all_settled"]["outcome"] == "resolved"
    assert composition_probe["all_settled_to_py"]["outcome"] == "resolved"
    assert composition_probe["alternate_run_sync"]["outcome"] == "error"
    assert composition_probe["direct_indexing"]["outcome"] == "not_run"


@pytest.mark.integration
@pytest.mark.runtime_gate
def test_wsgi_cache_round_trip_is_verified_through_diagnostic_binding_read(
    worker_process: WorkerProcess,
) -> None:
    key = f"django-cache-{uuid.uuid4().hex}"
    value = f"value-{uuid.uuid4().hex}"
    status, body = request(
        worker_process["port"], f"/cache/{quote(key, safe='')}/{quote(value, safe='')}"
    )
    assert status == 200, body
    assert decode_wsgi_result(body)["value"] == value

    physical_key = make_physical_key(KVCache({"LOCATION": "DJANGO_CACHE"}), key)
    diagnostic_status, diagnostic_body = request(
        worker_process["port"], f"/diagnostic/{quote(physical_key, safe='')}"
    )
    assert diagnostic_status == 200
    envelope = decode_diagnostic_result(diagnostic_body)["value"]
    assert envelope is not None
    decoded = decode_value(envelope)
    assert decoded is not None
    assert decoded.value == value


@pytest.mark.integration
@pytest.mark.runtime_gate
def test_worker_missing_binding_raises_chained_public_error(
    worker_process: WorkerProcess,
) -> None:
    status, body = request(worker_process["port"], "/missing-binding")

    assert status == 500
    result = decode_missing_binding_result(body)
    assert result["type"] == "CacheRuntimeError"
    assert result["cause_type"] == "AttributeError"
    assert result["cause_message"] == "MISSING_BINDING"
    assert result["message"] == (
        "KV binding 'MISSING_BINDING' unavailable; check Wrangler configuration."
    )


@pytest.mark.integration
@pytest.mark.runtime_gate
def test_worker_reports_runtime_versions(
    worker_process: WorkerProcess,
) -> None:
    """Given a real Worker, report its runtime's observed version identifiers."""
    status, body = request(worker_process["port"], "/versions")

    assert status == 200
    versions = decode_versions_result(body)
    assert versions["python"]
    assert versions["pyodide"]
    assert versions["workerd"]

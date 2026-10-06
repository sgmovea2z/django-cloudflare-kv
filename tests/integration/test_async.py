from __future__ import annotations

import time
from urllib.parse import quote
from uuid import uuid4

import pytest

from django_cloudflare_kv.backends import KVCache
from django_cloudflare_kv.codec import decode_value
from django_cloudflare_kv.keys import make_physical_key

from .runtime_gate_support import (
    WorkerProcess,
    decode_asgi_cache_result,
    decode_diagnostic_result,
    decode_native_suite_result,
    decode_ttl_envelope,
    decode_ttl_result,
    request,
)


@pytest.mark.integration
def test_native_async_cache_api_coverage(worker_process: WorkerProcess) -> None:
    suffix = uuid4().hex
    seed_status, seed_body = request(worker_process["port"], f"/native-seed/{suffix}")
    assert seed_status == 200, seed_body
    time.sleep(1.1)
    status, body = request(worker_process["port"], f"/native-suite/{suffix}")

    assert status == 200, body
    response = decode_native_suite_result(body)
    methods = (
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
    )
    assert response["methods"] == list(methods)
    assert response["result"]["bytes"] == "b'bytes'"
    assert response["result"]["none"] is None
    assert response["result"]["structured"] == {"items": [1, "two"]}
    assert response["result"]["add"] is True
    assert response["result"]["touch"] is True
    assert response["result"]["delete"] is True
    assert len(response["result"]["get_many"]) == 1
    assert list(response["result"]["get_many"].values()) == ["b'bytes'"]
    assert response["result"]["get_or_set"] == "value"
    assert response["result"]["has_key"] is True
    assert response["result"]["incr"] == 2
    assert response["result"]["decr"] == 0
    assert response["result"]["set_many"] == []
    assert response["result"]["delete_many"] is None
    assert response["result"]["incr_version"] == 2
    assert response["result"]["decr_version"] == 0
    assert response["result"]["clear"] is True
    assert response["result"]["sibling"] == "sibling-value"


@pytest.mark.integration
def test_django_asgi_request_round_trips_native_cache_through_worker(
    worker_process: WorkerProcess,
) -> None:
    key = f"asgi-{uuid4().hex}"
    value = f"asgi-value-{uuid4().hex}"
    status, body = request(worker_process["port"], f"/asgi/cache/{key}/{value}")

    assert status == 200, body
    result = decode_asgi_cache_result(body)
    assert result["value"] == value

    cache = KVCache(
        {
            "LOCATION": "DJANGO_CACHE",
            "KEY_PREFIX": "django-example",
            "VERSION": 1,
            "OPTIONS": {"CACHE_NAMESPACE": "django-example"},
        }
    )
    physical_key = make_physical_key(cache, key)
    diagnostic_status, diagnostic_body = request(
        worker_process["port"], f"/diagnostic/{quote(physical_key, safe='')}"
    )
    assert diagnostic_status == 200, diagnostic_body
    envelope = decode_diagnostic_result(diagnostic_body)["value"]
    assert envelope is not None
    decoded = decode_value(envelope)
    assert decoded is not None
    assert decoded.value == value


@pytest.mark.integration
def test_ttl_one_expires_logically_before_local_kv_physical_cleanup(
    worker_process: WorkerProcess,
) -> None:
    key = f"ttl-{uuid4().hex}"
    status, body = request(worker_process["port"], f"/ttl-set/{key}")
    assert status == 200, body

    time.sleep(1.2)
    status, body = request(worker_process["port"], f"/ttl-read/{key}")

    assert status == 200, body
    result = decode_ttl_result(body)
    assert result["logical"] == "expired"
    assert result["physical_present"] is True
    assert result["envelope"] is not None
    envelope = decode_ttl_envelope(result["envelope"])
    assert envelope["format"] == 1
    assert envelope["expires_at"] is not None
    assert envelope["payload"]

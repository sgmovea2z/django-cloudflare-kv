from __future__ import annotations

import time
from uuid import uuid4

import pytest

from .runtime_gate_support import (
    NativeSuiteResult,
    WorkerProcess,
    decode_native_suite_result,
    request,
)


@pytest.mark.integration
def test_wsgi_django_request_round_trips_cache_value(
    worker_process: WorkerProcess,
) -> None:
    key = f"django-consumer-{uuid4().hex}"
    value = f"consumer-value-{uuid4().hex}"
    status, body = request(worker_process["port"], f"/cache/{key}/{value}")

    assert status == 200
    assert f'"value": "{value}"' in body


@pytest.mark.integration
def test_wsgi_sync_cache_methods_run_through_the_workers_bridge(
    worker_process: WorkerProcess,
) -> None:
    suffix = uuid4().hex
    seed_status, seed_body = request(worker_process["port"], f"/sync-seed/{suffix}")
    assert seed_status == 200, seed_body
    time.sleep(1.1)
    status, body = request(worker_process["port"], f"/sync-suite/{suffix}")

    assert status == 200, body
    result: NativeSuiteResult = decode_native_suite_result(body)
    assert result["methods"] == [
        "add",
        "get",
        "set",
        "touch",
        "delete",
        "get_many",
        "get_or_set",
        "has_key",
        "incr",
        "decr",
        "set_many",
        "delete_many",
        "clear",
        "incr_version",
        "decr_version",
        "close",
    ]
    assert result["result"]["bytes"] == "b'bytes'"
    assert result["result"]["none"] is None
    assert result["result"]["structured"] == {"items": [1, "two"]}
    assert result["result"]["add"] is True
    assert result["result"]["touch"] is True
    assert result["result"]["delete"] is True
    assert list(result["result"]["get_many"].values()) == ["b'bytes'"]
    assert result["result"]["get_or_set"] == "value"
    assert result["result"]["has_key"] is True
    assert result["result"]["incr"] == 2
    assert result["result"]["decr"] == 0
    assert result["result"]["set_many"] == []
    assert result["result"]["delete_many"] is None
    assert result["result"]["incr_version"] == 2
    assert result["result"]["decr_version"] == 0
    assert result["result"]["clear"] is True
    assert result["result"]["sibling"] == "sibling-value"

    async_suffix = uuid4().hex
    async_seed_status, async_seed_body = request(
        worker_process["port"], f"/native-seed/{async_suffix}"
    )
    assert async_seed_status == 200, async_seed_body
    time.sleep(1.1)
    async_status, async_body = request(
        worker_process["port"], f"/native-suite/{async_suffix}"
    )
    assert async_status == 200, async_body
    async_result = decode_native_suite_result(async_body)
    assert async_result["methods"] == [
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
    ]

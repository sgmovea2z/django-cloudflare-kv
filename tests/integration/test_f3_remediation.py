from __future__ import annotations

import json
import uuid
from urllib.parse import quote

import pytest

from django_cloudflare_kv.backends import KVCache
from django_cloudflare_kv.errors import CacheBackendError, CacheSerializationError
from django_cloudflare_kv.keys import make_physical_key

from .runtime_gate_support import (
    WorkerProcess,
    decode_f3_clear_result,
    decode_f3_corrupt_result,
    request,
)


@pytest.mark.integration
@pytest.mark.runtime_gate
@pytest.mark.parametrize(
    ("mode", "payload", "cause_type"),
    [
        ("invalid-json", "{deliberately-invalid-json", "JSONDecodeError"),
        (
            "invalid-base64",
            '{"format":1,"expires_at":null,"payload":"%%%"}',
            "Error",
        ),
    ],
)
def test_real_worker_corrupt_value_raises_chained_serialization_error(
    worker_process: WorkerProcess,
    mode: str,
    payload: str,
    cause_type: str,
) -> None:
    logical_key = f"f3-corrupt-{uuid.uuid4().hex}"
    cache = KVCache(
        {
            "LOCATION": "DJANGO_CACHE",
            "OPTIONS": {"CACHE_NAMESPACE": "f3-corrupt"},
        }
    )
    physical_key = make_physical_key(cache, logical_key)
    status, body = request(
        worker_process["port"],
        "/"
        + "/".join(
            [
                "f3-corrupt",
                mode,
                quote(physical_key, safe=""),
                quote(logical_key, safe=""),
            ]
        ),
    )

    assert status == 200, body
    result = decode_f3_corrupt_result(body)
    assert result["payload"] == payload
    assert result["physical_key"] == physical_key
    assert result["error_type"] == "CacheSerializationError"
    assert result["is_backend_error"] is True
    assert issubclass(CacheSerializationError, CacheBackendError)
    assert result["cause_type"] == cause_type
    assert result["cause_message"]
    assert result["returned_default"] is False
    assert logical_key not in result["error_message"]
    assert payload not in result["error_message"]
    assert "pickle" not in result["error_message"].lower()
    print(f"F3 corrupt observed: {result!r}")  # noqa: T201 — evidence capture


@pytest.mark.integration
@pytest.mark.runtime_gate
def test_real_worker_clear_preserves_sibling_physical_key_independently(
    worker_process: WorkerProcess,
) -> None:
    suffix = uuid.uuid4().hex
    cleared_logical = f"cleared-{suffix}"
    sibling_logical = f"sibling-{suffix}"
    cleared_cache = KVCache(
        {
            "LOCATION": "DJANGO_CACHE",
            "OPTIONS": {"CACHE_NAMESPACE": "f3-cleared"},
        }
    )
    sibling_cache = KVCache(
        {
            "LOCATION": "DJANGO_CACHE",
            "OPTIONS": {"CACHE_NAMESPACE": "f3-sibling"},
        }
    )
    cleared_key = make_physical_key(cleared_cache, cleared_logical)
    sibling_key = make_physical_key(sibling_cache, sibling_logical)
    status, body = request(
        worker_process["port"],
        "/"
        + "/".join(
            [
                "f3-clear",
                suffix,
                quote(cleared_logical, safe=""),
                quote(sibling_logical, safe=""),
            ]
        ),
    )

    assert status == 200, body
    result = decode_f3_clear_result(body)
    assert result["clear"] is True
    assert result["cleared_physical_key"] == cleared_key
    assert result["sibling_physical_key"] == sibling_key
    assert cleared_key.startswith("dckv:v1:f3-cleared:")
    assert sibling_key.startswith("dckv:v1:f3-sibling:")
    assert len(cleared_key.rsplit(":", maxsplit=1)[1]) == 64
    assert len(sibling_key.rsplit(":", maxsplit=1)[1]) == 64
    assert result["cleared_envelope_before"] is not None
    assert result["written_sibling_envelope"] is not None

    cleared_status, cleared_body = request(
        worker_process["port"], f"/diagnostic/{quote(cleared_key, safe='')}"
    )
    sibling_status, sibling_body = request(
        worker_process["port"], f"/diagnostic/{quote(sibling_key, safe='')}"
    )
    assert cleared_status == sibling_status == 200
    assert json.loads(cleared_body) == {"value": None}
    written_envelope = result["written_sibling_envelope"]
    assert written_envelope is not None
    assert json.loads(sibling_body) == {"value": written_envelope}
    print(  # noqa: T201 — evidence capture
        "".join(
            (
                f"F3 clear observed: before=({result['cleared_envelope_before']!r}, ",
                f"{written_envelope!r}); after=({cleared_body!r}, {sibling_body!r}); ",
                f"clear={result['clear']!r}",
            )
        )
    )

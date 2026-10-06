from __future__ import annotations

import pytest

from .runtime_gate_support import WorkerProcess, request


@pytest.mark.integration
def test_template_fragment_and_cache_page_hits(worker_process: WorkerProcess) -> None:
    status, body = request(worker_process["port"], "/django-features")

    assert status == 200, body
    assert '"fragment": "fragment-hit"' in body
    assert '"page": "page-hit"' in body
    assert '"page_view_count": 1' in body

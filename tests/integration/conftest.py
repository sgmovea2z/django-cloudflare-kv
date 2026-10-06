import subprocess
from pathlib import Path

import pytest

from .runtime_gate_support import ROOT, WORKER_ROOT, worker_process


@pytest.fixture(scope="session")
def built_wheel() -> Path:
    wheel_dir = ROOT / "dist"
    wheels = tuple(wheel_dir.glob("django_cloudflare_kv-*.whl"))
    assert len(wheels) == 1
    return wheels[0]


@pytest.fixture(scope="session", autouse=True)
def sync_worker_packages() -> None:
    command = [
        "uv",
        "run",
        "--project",
        str(ROOT),
        "pywrangler",
        "sync",
        "--force",
    ]
    result = subprocess.run(
        command,
        cwd=WORKER_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )
    _ = result


__all__ = ["worker_process"]

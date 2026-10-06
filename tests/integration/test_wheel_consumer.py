from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from .runtime_gate_support import ROOT


@pytest.mark.integration
def test_consumer_import_comes_from_installed_wheel(
    built_wheel: Path, tmp_path: Path
) -> None:
    consumer = tmp_path / "consumer"
    _ = shutil.copytree(ROOT / "examples/django_worker/src", consumer / "src")
    environment = consumer / ".venv"
    _ = subprocess.run(
        ["uv", "venv", str(environment)],
        cwd=consumer,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    _ = subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(environment / "bin/python"),
            str(built_wheel),
        ],
        cwd=consumer,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    code = """
import pathlib
import site
import sys
import sysconfig
import django_cloudflare_kv

module = pathlib.Path(django_cloudflare_kv.__file__).resolve()
site_packages = {pathlib.Path(path).resolve() for path in site.getsitepackages()}
site_packages.add(pathlib.Path(sysconfig.get_paths()['purelib']).resolve())
assert any(
    module.is_relative_to(path) for path in site_packages
), (module, site_packages)
source_root = pathlib.Path(sys.argv[1]).resolve()
assert not module.is_relative_to(source_root), (module, source_root)
print(module)
"""
    _ = subprocess.run(
        [str(environment / "bin/python"), "-I", "-c", code, str(ROOT / "src")],
        cwd=consumer,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
        env={"PATH": "/usr/bin:/bin"},
    )

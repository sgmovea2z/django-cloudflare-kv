"""Distribution contract tests for the installable package."""

from __future__ import annotations

import tarfile
import zipfile
from email.parser import Parser
from pathlib import Path

import pytest

from .fakes import FakeBinding, FakeClock


def test_wheel_contains_only_typed_library_and_declares_runtime_dependency() -> None:
    """Built wheel contains typed library, Django requirement, and no harness."""
    wheels = tuple(Path("dist").glob("django_cloudflare_kv-*.whl"))
    assert len(wheels) == 1, "expected exactly one built wheel in dist/"

    with zipfile.ZipFile(wheels[0]) as wheel:
        names = set(wheel.namelist())
        package_files = {
            name for name in names if name.startswith("django_cloudflare_kv/")
        }
        assert "django_cloudflare_kv/__init__.py" in package_files
        assert "django_cloudflare_kv/py.typed" in package_files
        assert not any(
            name.startswith(("tests/", "examples/", "skills/", ".agents/"))
            for name in names
        )

        metadata_path = next(
            name for name in names if name.endswith(".dist-info/METADATA")
        )
        metadata = Parser().parsestr(wheel.read(metadata_path).decode())
        assert metadata["Name"] == "django-cloudflare-kv"
        assert metadata["Version"] == "0.1.0"
        assert any(
            requirement.lower() == "django<6.2,>=5.2"
            for requirement in metadata.get_all("Requires-Dist", [])
        )


def test_sdist_contains_library_and_tests_without_workspace_artifacts() -> None:
    sdists = tuple(Path("dist").glob("django_cloudflare_kv-*.tar.gz"))
    assert len(sdists) == 1, "expected exactly one built sdist in dist/"

    with tarfile.open(sdists[0]) as sdist:
        names = {
            member.removeprefix("django_cloudflare_kv-0.1.0/")
            for member in sdist.getnames()
        }

    assert "pyproject.toml" in names
    assert "README.md" in names
    assert "uv.lock" in names
    assert "tests/test_package.py" in names
    assert any(name.startswith("tests/") for name in names)
    package_files = {
        f"src/{path.relative_to('src').as_posix()}"
        for path in Path("src/django_cloudflare_kv").rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    assert package_files <= names
    assert not any(
        part
        in {
            ".agents",
            ".codegraph",
            ".omo",
            "node_modules",
            ".venv",
            "dist",
            "build",
            ".hypothesis",
            ".pytest_cache",
            ".ruff_cache",
            ".serena",
            ".vscode",
            ".idea",
            "__pycache__",
            ".DS_Store",
            ".wrangler",
            ".venv-workers",
            "python_modules",
        }
        for name in names
        for part in name.split("/")
    )
    assert not any(
        name.startswith(
            (
                "tests/integration/worker/.venv-workers/",
                "tests/integration/worker/python_modules/",
                "tests/integration/worker/.wrangler/",
            )
        )
        for name in names
    )
    assert not any(name.startswith(("examples/", "docs/")) for name in names)


def test_fake_binding_honors_expiration_deadlines(fake_clock: FakeClock) -> None:
    """Given a finite TTL, get returns the value only before its deadline."""
    binding = FakeBinding(clock=fake_clock)
    binding.put("key", "value", expiration_ttl=3)

    assert binding.get("key") == "value"
    fake_clock.advance(3)
    assert binding.get("key") is None


def test_fake_binding_injects_failure_after_recording_call(
    fake_binding: FakeBinding,
) -> None:
    """Given a configured failure, the matching invocation is recorded first."""
    failure = RuntimeError("injected")
    fake_binding.fail_next("get", failure)

    with pytest.raises(RuntimeError, match="injected"):
        _ = fake_binding.get("key")

    assert fake_binding.calls[0].operation == "get"


def test_fake_binding_models_delayed_visibility_and_empty_pages(
    fake_clock: FakeClock,
) -> None:
    """Given delayed KV visibility, list can return empty non-final pages."""
    binding = FakeBinding(
        clock=fake_clock,
        visibility_delay=2,
        empty_pages_before_results=1,
        page_size=1,
    )
    binding.put("prefix/key", "value")

    assert binding.get("prefix/key") is None
    assert binding.list("prefix/") == ((), "0", False)
    fake_clock.advance(2)
    assert binding.get("prefix/key") == "value"
    assert binding.list("prefix/", cursor="0") == (("prefix/key",), "1", True)

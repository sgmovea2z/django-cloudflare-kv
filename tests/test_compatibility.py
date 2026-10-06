from __future__ import annotations

import os
import zipfile
from email.parser import Parser
from pathlib import Path

import django
import pytest
from django.core.exceptions import ImproperlyConfigured
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from django_cloudflare_kv.backends import KVCache

SUPPORTED_SERIES = ("5.2", "6.0", "6.1")


def assert_django_matches_constraint(version: Version, constraint: str) -> None:
    specifier = SpecifierSet(constraint)
    assert version in specifier, f"Django {version} does not satisfy {specifier}"


def test_installed_django_matches_explicit_matrix_row() -> None:
    version = Version(django.get_version())
    constraint = os.environ.get(
        "DJANGO_COMPATIBILITY_CONSTRAINT",
        f">={version.major}.{version.minor},<{version.major}.{version.minor + 1}",
    )
    assert_django_matches_constraint(version, constraint)


def test_declared_django_bound_covers_only_exercised_series() -> None:
    project = Path("pyproject.toml").read_text()
    requirement = next(
        line.removeprefix('dependencies = ["').removesuffix('"]')
        for line in project.splitlines()
        if line.startswith("dependencies = ")
    )
    parsed = Requirement(requirement)
    assert parsed.name.lower() == "django"
    assert parsed.specifier == SpecifierSet(">=5.2,<6.2")
    assert all(
        Version(f"{series}.0") in parsed.specifier for series in SUPPORTED_SERIES
    )
    wheels = tuple(Path("dist").glob("django_cloudflare_kv-*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as wheel:
        metadata_path = next(
            name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")
        )
        metadata = Parser().parsestr(wheel.read(metadata_path).decode())
    requirements = metadata.get_all("Requires-Dist")
    assert requirements is not None
    wheel_requirement = Requirement(requirements[0])
    assert wheel_requirement.name.lower() == "django"
    assert wheel_requirement.specifier == parsed.specifier


@pytest.mark.parametrize(
    ("constraint", "release", "matches"),
    [
        (">=5.2,<5.3", "5.2.11", True),
        (">=5.2,<5.3", "6.0.0", False),
        (">=6.0,<6.1", "6.1.1", False),
    ],
)
def test_constraint_rejects_deliberately_mismatched_version(
    constraint: str, release: str, matches: bool
) -> None:
    assert (Version(release) in SpecifierSet(constraint)) is matches


def test_matrix_assertion_fails_for_deliberately_wrong_installed_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DJANGO_COMPATIBILITY_CONSTRAINT", ">=5.2,<5.3")
    with pytest.raises(AssertionError, match="does not satisfy"):
        assert_django_matches_constraint(
            Version("6.1.1"), os.environ["DJANGO_COMPATIBILITY_CONSTRAINT"]
        )


def test_invalid_documented_namespace_is_rejected_during_backend_setup() -> None:
    with pytest.raises(ImproperlyConfigured, match="CACHE_NAMESPACE"):
        _ = KVCache(
            {
                "LOCATION": "DJANGO_CACHE",
                "OPTIONS": {"CACHE_NAMESPACE": "invalid namespace"},
            }
        )

"""Backend configuration validation without runtime binding access."""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys
from typing import TYPE_CHECKING, override

import pytest
from django.conf import settings
from django.core.cache import caches
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from django_cloudflare_kv.backends import KVCache

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import ModuleType


@pytest.mark.parametrize("length", [1, 64])
def test_namespace_length_boundaries_pass(length: int) -> None:
    backend = KVCache({"LOCATION": "KV", "OPTIONS": {"CACHE_NAMESPACE": "a" * length}})
    assert backend.cache_namespace == "a" * length


@pytest.mark.parametrize("length", [0, 65])
def test_namespace_length_boundaries_fail(length: int) -> None:
    with pytest.raises(ImproperlyConfigured):
        _ = KVCache({"LOCATION": "KV", "OPTIONS": {"CACHE_NAMESPACE": "a" * length}})


@pytest.mark.parametrize("namespace", ["bad space", "bad.dot", "é", "bad/slash"])
def test_invalid_namespace_characters_fail(namespace: str) -> None:
    with pytest.raises(ImproperlyConfigured):
        _ = KVCache({"LOCATION": "KV", "OPTIONS": {"CACHE_NAMESPACE": namespace}})


@pytest.mark.parametrize("budget", [0, 901, True, False, 1.5, "900"])
def test_invalid_budget_fails_before_any_binding_access(
    budget: float | bool | str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imported_workers: list[str] = []

    class WorkersImportTracker(importlib.abc.MetaPathFinder):
        @override
        def find_spec(
            self,
            fullname: str,
            path: Sequence[str] | None,
            target: ModuleType | None = None,
        ) -> importlib.machinery.ModuleSpec | None:
            if fullname == "workers" or fullname.startswith("workers."):
                imported_workers.append(fullname)
            return None

    monkeypatch.setattr(sys, "meta_path", [WorkersImportTracker(), *sys.meta_path])
    with pytest.raises(ImproperlyConfigured):
        _ = KVCache({"LOCATION": "KV", "OPTIONS": {"OPERATION_LIMIT": budget}})
    assert imported_workers == []


@pytest.mark.parametrize("budget", [1, 900])
def test_budget_boundaries_pass(budget: int) -> None:
    backend = KVCache({"LOCATION": "KV", "OPTIONS": {"OPERATION_LIMIT": budget}})
    assert backend.operation_limit == budget


@pytest.mark.parametrize("location", ["", " ", None, 1])
def test_invalid_location_fails(location: str | int | None) -> None:
    with pytest.raises(ImproperlyConfigured):
        _ = KVCache({"LOCATION": location})


def test_defaults_and_namespace_comment_contract() -> None:
    backend = KVCache({"LOCATION": "NATIVE_BINDING"})
    assert backend.cache_namespace == "default"
    assert backend.operation_limit == 900
    assert backend.location == "NATIVE_BINDING"


def test_django_cache_handler_constructs_backend_from_standard_settings() -> None:
    config = {
        "default": {
            "BACKEND": "django_cloudflare_kv.backends.KVCache",
            "LOCATION": "DJANGO_CACHE",
            "OPTIONS": {"CACHE_NAMESPACE": "handler"},
        }
    }
    if not settings.configured:
        settings.configure(CACHES=config)
    with override_settings(CACHES=config):
        caches.close_all()
        backend = caches["default"]
        assert isinstance(backend, KVCache)
        assert backend.location == "DJANGO_CACHE"
        caches.close_all()

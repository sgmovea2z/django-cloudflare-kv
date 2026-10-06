from __future__ import annotations

from typing import TYPE_CHECKING

from django_cloudflare_kv.backends import KVCache
from django_cloudflare_kv.keys import make_physical_key

if TYPE_CHECKING:
    from .fakes import FakeBinding


def test_stale_overwrite_schedule_keeps_last_observed_write(
    fake_binding: FakeBinding,
) -> None:
    cache = KVCache({"LOCATION": "KV"})
    key = make_physical_key(cache, "k")
    fake_binding.put(key, "initial")
    first_observation = fake_binding.get(key)
    second_observation = fake_binding.get(key)
    assert first_observation == second_observation

    fake_binding.put(key, "write from first actor")
    fake_binding.put(key, "stale overwrite from second actor")

    assert fake_binding.get(key) == "stale overwrite from second actor"


def test_cached_miss_schedule_can_miss_a_later_write(
    fake_binding: FakeBinding,
) -> None:
    cache = KVCache({"LOCATION": "KV"})
    key = make_physical_key(cache, "k")
    cached_observation = fake_binding.get(key)
    assert cached_observation is None

    fake_binding.put(key, "written after the miss")

    assert cached_observation is None
    assert fake_binding.get(key) == "written after the miss"


def test_two_observed_add_misses_can_both_report_success(
    fake_binding: FakeBinding,
) -> None:
    cache = KVCache({"LOCATION": "KV"})
    key = make_physical_key(cache, "k")
    first_observation = fake_binding.get(key)
    second_observation = fake_binding.get(key)
    assert first_observation is None
    assert second_observation is None

    fake_binding.put(key, "first add")
    first_result = True
    fake_binding.put(key, "second add")
    second_result = True

    assert first_result is True
    assert second_result is True
    assert fake_binding.get(key) == "second add"


def test_lost_increment_schedule_loses_one_update(
    fake_binding: FakeBinding,
) -> None:
    cache = KVCache({"LOCATION": "KV"})
    key = make_physical_key(cache, "counter")
    fake_binding.put(key, "10")
    first_read = int(fake_binding.get(key) or "0")
    second_read = int(fake_binding.get(key) or "0")

    fake_binding.put(key, str(first_read + 1))
    fake_binding.put(key, str(second_read + 1))

    assert fake_binding.get(key) == "11"

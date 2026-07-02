"""P8 section 3.3 - cache lookup (miss) overhead.

Isolates the cost of a TIP-style cache lookup when the cache is configured but
the key is a guaranteed **miss**: cache-key derivation plus the negative dict
lookup. Measured against a real ``VolatileCache`` pre-warmed with K entries
(K ∈ {0, 100, 10_000} as separate records, per the methodology), with the
lookup key always absent. Reported as the difference versus a no-op baseline.

Cache-hit response replay is intentionally out of scope (methodology section 3.3 note):
hits avoid the upstream call entirely and warrant their own measurement.

Opt-in: ``pytest -m p8_latency``.
"""

from __future__ import annotations

import pytest

from .p8_latency_harness import requires_p8_optin, run_difference_target

pytestmark = pytest.mark.p8_latency

_K_VALUES = (0, 100, 10_000)
_MISS_KEY = "p8-cache-miss-key-never-stored"


def test_cache_lookup_miss_overhead(request):
    requires_p8_optin(request)
    vc_mod = pytest.importorskip(
        "tokenpak.cache.volatile_cache", reason="volatile cache unavailable"
    )
    VolatileCache = vc_mod.VolatileCache

    records = []
    for k in _K_VALUES:
        cache = VolatileCache(ttl=3600.0, max_size=k + 16, name="p8")
        for i in range(k):
            cache.set(f"warm-{i}", {"v": i})

        def target(_cache=cache) -> None:
            # Key derivation (_make_key) + negative lookup; always a miss.
            _cache.get(_MISS_KEY)

        def baseline() -> None:
            pass

        record = run_difference_target(
            target="cache_lookup_miss",
            method=(
                f"real VolatileCache.get(miss) (key derivation + negative lookup), "
                f"K={k} pre-warmed entries, vs no-op (methodology section 3.3)"
            ),
            target_fn=target,
            baseline_fn=baseline,
        )
        assert record["target"] == "cache_lookup_miss"
        assert record["sample_size"] > 0
        assert record["status"] == "measured"
        records.append(record)

    assert len(records) == len(_K_VALUES)

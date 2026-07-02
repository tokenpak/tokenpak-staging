"""P8 latency benchmark harness — shared runner.

Evidence infrastructure for the P8 latency benchmark methodology. This module
provides the shared substrate used by the six ``p8_*`` target test files:

  * idle-loop **noise probe** (sets the run's noise bin: clean / noisy / dominated)
  * **host introspection** (class label, cpu model, ram, shared-CI flag, py version)
  * **host classification** + claim-eligibility (fleet-class / beefy / shared-ci / …)
  * a deterministic **percentile** helper (p50 / p95 / p99)
  * a warmup-discarding **measure** loop
  * structured **JSONL record** assembly + append

It deliberately does NOT make any live provider call, start a socket proxy, or
mutate host/runtime state. Each measurement is an in-process timing of a real
TokenPak code path (or a loopback-only transport), reported as the difference
between the target operation and a matched baseline so the absolute host latency
cancels out.

The harness is **opt-in**: the ``p8_latency`` pytest marker (registered in
``pyproject.toml``) gates execution. ``pytest -m p8_latency`` runs the targets;
the default / slim / dev / full suites collect them but ``skip`` (see
``requires_p8_optin``) so default-suite behaviour is unchanged.

Honesty contract (methodology section 5): every record self-reports its noise bin and a
``claim`` label. Only a clean run on a fleet-class / modest deployment host is
``claim``-eligible; beefy-only and shared-CI runs are ``non_claim`` / ``trend_only``
and can never become a canonical fleet-performance baseline.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

# ---------------------------------------------------------------------------
# Paths + env knobs
# ---------------------------------------------------------------------------

#: Committed baseline accumulator (one JSON record per line). Records are only
#: appended when ``TOKENPAK_P8_WRITE_BASELINE`` is truthy, so an ordinary
#: ``pytest -m p8_latency`` run measures without mutating the versioned file.
BASELINES_PATH = Path(__file__).with_name("p8_latency_baselines.jsonl")

#: Marker name; kept in one place so the tests and the gate agree.
P8_MARKER = "p8_latency"

# Default sample sizes. Methodology section 3 specifies N=1000 (+100 warmup) for the
# in-process targets and N=500 (+50 warmup) for the SSE targets. The in-test
# default is a fast smoke size so ``pytest -m p8_latency`` stays well under the
# 30s suite timeout; a real baseline capture sets TOKENPAK_P8_SAMPLES to the
# methodology N.
DEFAULT_SAMPLES = 300
DEFAULT_WARMUP = 50
DEFAULT_STREAM_SAMPLES = 200
DEFAULT_STREAM_WARMUP = 30

# Noise bin thresholds (methodology section 5), in milliseconds, on the idle-loop p99.
NOISE_CLEAN_MAX_MS = 2.0
NOISE_NOISY_MAX_MS = 15.0
NOISE_PROBE_READS = 1000


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        val = int(raw)
        return val if val > 0 else default
    except ValueError:
        return default


def samples_for(default: int = DEFAULT_SAMPLES) -> int:
    """Resolve the per-target sample size (TOKENPAK_P8_SAMPLES overrides)."""
    return _env_int("TOKENPAK_P8_SAMPLES", default)


def warmup_for(default: int = DEFAULT_WARMUP) -> int:
    """Resolve the warmup count to discard (TOKENPAK_P8_WARMUP overrides)."""
    return _env_int("TOKENPAK_P8_WARMUP", default)


def should_write_baseline() -> bool:
    """True when records should be appended to the committed JSONL accumulator."""
    return os.environ.get("TOKENPAK_P8_WRITE_BASELINE", "").lower() in ("1", "true", "yes")


def p8_opted_in(config) -> bool:
    """Return True when the p8_latency marker was explicitly selected.

    Checks the pytest ``-m`` expression for the marker name, or the
    ``TOKENPAK_P8_RUN`` env escape hatch. Used by ``requires_p8_optin`` so the
    targets stay collected-but-skipped under the default / slim / dev / full
    shapes (no addopts or conftest change required).
    """
    if os.environ.get("TOKENPAK_P8_RUN", "").lower() in ("1", "true", "yes"):
        return True
    markexpr = getattr(getattr(config, "option", None), "markexpr", "") or ""
    return P8_MARKER in markexpr


def requires_p8_optin(request) -> None:
    """Skip the calling test unless P8 was explicitly opted into.

    Call at the top of every ``p8_*`` test body. Keeps the opt-in semantics
    self-contained in the new files (a bare ``pytest`` run skips these instead
    of executing the benchmark)."""
    if not p8_opted_in(request.config):
        import pytest

        pytest.skip(
            "P8 latency targets are opt-in: run `pytest -m p8_latency` "
            "or set TOKENPAK_P8_RUN=1"
        )


# ---------------------------------------------------------------------------
# Percentiles + measurement
# ---------------------------------------------------------------------------

def percentile(samples: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile in the same convention as the existing
    benchmark suite (``test_load_100rps._percentile``)."""
    if not samples:
        return 0.0
    ordered = sorted(samples)
    idx = min(int(len(ordered) * pct / 100), len(ordered) - 1)
    return ordered[idx]


def measure(fn: Callable[[], None], n: int, warmup: int) -> List[float]:
    """Time ``fn`` ``n`` times (plus ``warmup`` discarded), returning per-call ms.

    A monotonic ``perf_counter`` brackets each call. Warmup iterations prime
    caches / JIT-free interpreter state and are not recorded."""
    for _ in range(max(0, warmup)):
        fn()
    out: List[float] = []
    perf = time.perf_counter
    for _ in range(max(1, n)):
        t0 = perf()
        fn()
        out.append((perf() - t0) * 1000.0)
    return out


def difference_distribution(
    target_fn: Callable[[], None],
    baseline_fn: Callable[[], None],
    n: int,
    warmup: int,
) -> List[float]:
    """Per-iteration (target - baseline) ms, clamped at 0.

    The methodology reports the *difference* distribution so the shared cost
    (interpreter dispatch, upstream latency in the transport case) cancels out
    and only the TokenPak-added overhead remains. Negative deltas (baseline
    momentarily slower than target due to scheduling jitter) clamp to 0 rather
    than fabricate a negative overhead."""
    for _ in range(max(0, warmup)):
        target_fn()
        baseline_fn()
    out: List[float] = []
    perf = time.perf_counter
    for _ in range(max(1, n)):
        b0 = perf()
        baseline_fn()
        b1 = perf()
        target_fn()
        t1 = perf()
        delta = (t1 - b1) - (b1 - b0)
        out.append(max(0.0, delta * 1000.0))
    return out


# ---------------------------------------------------------------------------
# Noise probe (methodology section 5)
# ---------------------------------------------------------------------------

@dataclass
class NoiseReading:
    state: str          # clean | noisy | dominated
    floor_ms: float     # idle-loop p99 in ms


def noise_probe(reads: int = NOISE_PROBE_READS) -> NoiseReading:
    """p99 of ``reads`` no-op ``perf_counter`` deltas sets the run's noise bin."""
    perf = time.perf_counter
    deltas: List[float] = []
    prev = perf()
    for _ in range(reads):
        now = perf()
        deltas.append((now - prev) * 1000.0)
        prev = now
    floor = percentile(deltas, 99)
    if floor < NOISE_CLEAN_MAX_MS:
        state = "clean"
    elif floor <= NOISE_NOISY_MAX_MS:
        state = "noisy"
    else:
        state = "dominated"
    return NoiseReading(state=state, floor_ms=round(floor, 4))


# ---------------------------------------------------------------------------
# Host introspection + classification
# ---------------------------------------------------------------------------

def _cpu_model() -> str:
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine() or "unknown"


def _ram_gb() -> float:
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return round(pages * page_size / 1e9, 2)
    except (ValueError, OSError, AttributeError):
        pass
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal"):
                    kb = float(line.split()[1])
                    return round(kb / 1e6, 2)
    except (OSError, IndexError, ValueError):
        pass
    return 0.0


def is_shared_ci() -> bool:
    for var in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "BUILDKITE", "CIRCLECI"):
        if os.environ.get(var, "").lower() in ("1", "true", "yes"):
            return True
    return False


def classify_host(cpu_count: int, ram_gb: float, shared_ci: bool) -> str:
    """Classify the executor host into the ratified P8 host taxonomy.

    Policy: the primary claim-bearing baseline is the
    **fleet-class / modest deployment host**; a
    clean lab host is an optional non-representative secondary; a **beefy-only**
    host can never be canonical. Thresholds are principled (a 4-vCPU / 4GB box
    is the repo's canonical modest deployment target), not host-specific."""
    if shared_ci:
        return "shared-ci"
    if cpu_count >= 32 or ram_gb >= 64.0:
        return "beefy"
    if 2 <= cpu_count <= 16 and 3.5 <= ram_gb <= 48.0:
        return "fleet-class-modest"
    if cpu_count < 2 or ram_gb < 3.5:
        return "constrained"
    return "unknown"


def host_info() -> Dict[str, object]:
    """Host descriptor for the JSONL record.

    ``name`` is a **class label**, not the machine hostname — P8 tracks host
    *classes* (per the ratified policy) and the committed accumulator must carry
    no machine identity. ``cpu`` is the generic hardware model string."""
    cpu_count = os.cpu_count() or 1
    ram = _ram_gb()
    shared = is_shared_ci()
    host_class = classify_host(cpu_count, ram, shared)
    return {
        "name": host_class,
        "host_class": host_class,
        "cpu": _cpu_model(),
        "cpu_count": cpu_count,
        "ram_gb": ram,
        "is_shared_ci": shared,
        "python_version": platform.python_version(),
    }


def claim_label(host_class: str, noise_state: str) -> str:
    """Resolve the evidence-claim tier for a run.

    ``claim``      — fleet-class/modest host, clean noise: comparable absolute numbers.
    ``trend_only`` — usable for shape/trend tracking only (noisy, dev host, or shared CI).
    ``non_claim``  — must not back any latency claim (beefy-only, dominated noise, unknown)."""
    if noise_state == "dominated":
        return "non_claim"
    if host_class == "fleet-class-modest":
        return "claim" if noise_state == "clean" else "trend_only"
    if host_class == "beefy":
        # A beefy-only run can never be canonical, even when perfectly clean.
        return "non_claim"
    if host_class in ("shared-ci", "constrained"):
        return "trend_only"
    return "non_claim"


# ---------------------------------------------------------------------------
# Git provenance
# ---------------------------------------------------------------------------

def _git(*args: str) -> str:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=str(Path(__file__).resolve().parent),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def git_commit() -> str:
    return os.environ.get("TOKENPAK_P8_COMMIT") or _git("rev-parse", "HEAD") or "unknown"


def git_ref() -> str:
    ref = os.environ.get("TOKENPAK_P8_REF")
    if ref:
        return ref
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    return branch or "unknown"


# ---------------------------------------------------------------------------
# Record assembly + persistence
# ---------------------------------------------------------------------------

@dataclass
class P8Record:
    target: str
    method: str
    samples: List[float] = field(default_factory=list)
    sample_size: int = 0
    warmup_discarded: int = 0
    noise: Optional[NoiseReading] = None
    host: Dict[str, object] = field(default_factory=dict)
    status: str = "measured"   # measured | blocker
    blocker_reason: str = ""

    def to_dict(self) -> Dict[str, object]:
        host = self.host or host_info()
        noise = self.noise or noise_probe()
        rec: Dict[str, object] = {
            "target": self.target,
            "method": self.method,
            "status": self.status,
            "p50_ms": round(percentile(self.samples, 50), 4),
            "p95_ms": round(percentile(self.samples, 95), 4),
            "p99_ms": round(percentile(self.samples, 99), 4),
            "sample_size": self.sample_size or len(self.samples),
            "warmup_discarded": self.warmup_discarded,
            "host": host,
            "noise_state": noise.state,
            "noise_floor_ms": noise.floor_ms,
            "claim": claim_label(str(host.get("host_class", "unknown")), noise.state),
            "commit": git_commit(),
            "ref": git_ref(),
            "ts": _utc_now_iso(),
        }
        if self.status == "blocker":
            rec["blocker_reason"] = self.blocker_reason
        return rec


def _utc_now_iso() -> str:
    # ISO 8601 UTC, second resolution. time.gmtime avoids a tz database dep.
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def validate_record(rec: Dict[str, object]) -> None:
    """Assert the full host/sample/noise schema is present (used by the tests)."""
    required_top = {
        "target", "method", "status", "p50_ms", "p95_ms", "p99_ms",
        "sample_size", "warmup_discarded", "host", "noise_state",
        "noise_floor_ms", "claim", "commit", "ref", "ts",
    }
    missing = required_top - set(rec)
    assert not missing, f"record missing top-level fields: {sorted(missing)}"
    required_host = {
        "name", "host_class", "cpu", "cpu_count", "ram_gb",
        "is_shared_ci", "python_version",
    }
    host = rec.get("host", {})
    assert isinstance(host, dict), "host must be an object"
    missing_host = required_host - set(host)
    assert not missing_host, f"host missing fields: {sorted(missing_host)}"
    assert rec["noise_state"] in ("clean", "noisy", "dominated")
    assert rec["claim"] in ("claim", "trend_only", "non_claim")


def write_baseline(rec: Dict[str, object]) -> None:
    """Append one record to the committed JSONL accumulator (gated)."""
    if not should_write_baseline():
        return
    with open(BASELINES_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# Convenience driver used by the six target tests
# ---------------------------------------------------------------------------

def run_difference_target(
    target: str,
    method: str,
    target_fn: Callable[[], None],
    baseline_fn: Callable[[], None],
    *,
    default_samples: int = DEFAULT_SAMPLES,
    default_warmup: int = DEFAULT_WARMUP,
) -> Dict[str, object]:
    """Measure a difference-distribution target end to end and return its record.

    Runs the noise probe, the warmup-discarding difference loop, assembles the
    structured record, validates the schema, and (when enabled) appends it to
    the committed accumulator. The caller is responsible only for supplying the
    real ``target_fn`` / ``baseline_fn`` closures."""
    n = samples_for(default_samples)
    warmup = warmup_for(default_warmup)
    noise = noise_probe()
    samples = difference_distribution(target_fn, baseline_fn, n, warmup)
    record = P8Record(
        target=target,
        method=method,
        samples=samples,
        sample_size=len(samples),
        warmup_discarded=warmup,
        noise=noise,
        host=host_info(),
    ).to_dict()
    validate_record(record)
    write_baseline(record)
    return record


def blocker_record(target: str, method: str, reason: str) -> Dict[str, object]:
    """Build (and persist, when enabled) an honest blocker receipt for a target.

    Used when a target's real code path cannot be exercised in the executor
    environment — recorded as ``status=blocker`` / ``claim=non_claim`` rather
    than emitting a fabricated latency number (packet stop-condition #1)."""
    record = P8Record(
        target=target,
        method=method,
        samples=[],
        sample_size=0,
        warmup_discarded=0,
        noise=noise_probe(),
        host=host_info(),
        status="blocker",
        blocker_reason=reason,
    ).to_dict()
    write_baseline(record)
    return record

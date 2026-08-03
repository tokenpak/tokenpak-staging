"""models.py — Per-model analytics and efficiency metrics.

Aggregates compression events by model, tracking:
- Request counts and token volumes
- Cache hit rates
- Compression efficiency
- Cost breakdown
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Optional

from tokenpak.models import get_model_costs, get_registry
from tokenpak.proxy.stats import CompressionStats, default_log_path

# Compatibility snapshot for callers that import the historical constant.
# Runtime lookups below resolve directly through the canonical registry.
MODEL_PRICING: Dict[str, Dict[str, float]] = {
    info.model_id: get_model_costs(info.model_id) for info in get_registry().all_models()
}


def get_model_pricing(model_name: str) -> Dict[str, float]:
    """Get current model pricing through the canonical model registry."""
    return get_model_costs(model_name)


@dataclass
class ModelStats:
    """Aggregated metrics for a single model."""

    model_name: str
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hits: int = 0
    cache_read_tokens: int = 0
    errors: int = 0
    total_latency_ms: int = 0

    def to_dict(self) -> dict:
        """Convert model stats to dictionary for serialization or reporting.

        Returns:
            dict: Keys include model name, request counts, token metrics, cache hit rate,
                  compression efficiency, and cost analysis.
        """
        return {
            "model": self.model_name,
            "requests": self.requests,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_hits": self.cache_hits,
            "cache_read_tokens": self.cache_read_tokens,
            "errors": self.errors,
            "avg_latency_ms": self._avg_latency(),
            "cache_hit_rate": self._cache_hit_rate(),
            "compression_efficiency": self._compression_efficiency(),
            "cost_metrics": self._cost_metrics(),
        }

    def _avg_latency(self) -> int:
        return self.total_latency_ms // self.requests if self.requests > 0 else 0

    def _cache_hit_rate(self) -> float:
        return round((self.cache_hits / self.requests * 100), 1) if self.requests > 0 else 0.0

    def _compression_efficiency(self) -> float:
        """Tokens saved as % of original input."""
        total_input = self.input_tokens + self.cache_read_tokens
        if total_input == 0:
            return 0.0
        saved = self.cache_read_tokens
        return round((saved / total_input * 100), 1)

    def _cost_metrics(self) -> Dict[str, float]:
        """Calculate cost sent + saved."""
        pricing = get_model_pricing(self.model_name)

        # Cost without cache
        cost_input = (self.input_tokens / 1_000_000) * pricing["input"]
        cost_output = (self.output_tokens / 1_000_000) * pricing["output"]
        cost_total = cost_input + cost_output

        # Savings from cache
        cost_cached = (self.cache_read_tokens / 1_000_000) * pricing["input"]
        cost_net = cost_total - cost_cached

        return {
            "sent": round(cost_total, 2),
            "saved": round(cost_cached, 2),
            "net": round(cost_net, 2),
        }


class ModelAnalyzer:
    """Aggregate compression events by model."""

    def __init__(self, log_path: Optional[str] = None):
        self.log_path = log_path or default_log_path()
        self.stats_by_model: Dict[str, ModelStats] = {}

    def load_from_file(self, limit: int = 1000) -> Dict[str, ModelStats]:
        """Load and aggregate events from the stats JSONL file."""
        cs = CompressionStats(log_path=self.log_path)
        events = cs.read_events(limit=limit)

        self.stats_by_model = defaultdict(lambda: ModelStats(""))

        for event in events:
            model = event.get("model", "unknown")

            if model not in self.stats_by_model:
                self.stats_by_model[model] = ModelStats(model)

            stats = self.stats_by_model[model]
            status = event.get("status", "ok")

            if status == "ok":
                stats.requests += 1
                stats.input_tokens += event.get("input_tokens", 0)
                stats.output_tokens += event.get("output_tokens", 0)
                stats.cache_hits += event.get("cache_hits", 0)
                stats.cache_read_tokens += event.get("cache_read_tokens", 0)
                stats.total_latency_ms += event.get("latency_ms", 0)
            else:
                stats.errors += 1

        return dict(self.stats_by_model)

    def get_summary(self) -> Dict:
        """Get aggregate summary across all models."""
        all_stats = list(self.stats_by_model.values())

        if not all_stats:
            return {
                "total_requests": 0,
                "total_models": 0,
                "total_tokens_sent": 0,
                "total_cache_hits": 0,
                "overall_cache_hit_rate": 0.0,
                "overall_compression_efficiency": 0.0,
                "total_cost_sent": 0.0,
                "total_cost_saved": 0.0,
                "total_cost_net": 0.0,
            }

        total_requests = sum(s.requests for s in all_stats)
        total_input = sum(s.input_tokens for s in all_stats)
        total_cache_reads = sum(s.cache_read_tokens for s in all_stats)
        total_cache_hits = sum(s.cache_hits for s in all_stats)

        # Aggregate costs
        total_cost_sent = 0.0
        total_cost_saved = 0.0

        for stats in all_stats:
            costs = stats._cost_metrics()
            total_cost_sent += costs["sent"]
            total_cost_saved += costs["saved"]

        # Overall metrics
        cache_hit_rate = (
            round((total_cache_hits / total_requests * 100), 1) if total_requests > 0 else 0.0
        )

        total_tokens = total_input + total_cache_reads
        compression_eff = (
            round((total_cache_reads / total_tokens * 100), 1) if total_tokens > 0 else 0.0
        )

        return {
            "total_requests": total_requests,
            "total_models": len(all_stats),
            "total_tokens_sent": total_input + sum(s.output_tokens for s in all_stats),
            "total_cache_hits": total_cache_hits,
            "overall_cache_hit_rate": cache_hit_rate,
            "overall_compression_efficiency": compression_eff,
            "total_cost_sent": round(total_cost_sent, 2),
            "total_cost_saved": round(total_cost_saved, 2),
            "total_cost_net": round(total_cost_sent - total_cost_saved, 2),
        }

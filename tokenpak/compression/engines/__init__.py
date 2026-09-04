"""Compaction engines module."""

from .base import CompactionEngine
from .heuristic import HeuristicEngine

# LLMLingua engines will be imported conditionally when available
try:
    from .llmlingua import LLMLinguaEngine

    LLMLINGUA_AVAILABLE = True
except ImportError:
    LLMLINGUA_AVAILABLE = False
    LLMLinguaEngine = None  # type: ignore[assignment, misc]  # fallback stub for the optional llmlingua dependency; None can't satisfy the imported class's inferred type

ENGINES = {
    "heuristic": HeuristicEngine,
    "fast": HeuristicEngine,
}

if LLMLINGUA_AVAILABLE:
    ENGINES["balanced"] = LLMLinguaEngine  # type: ignore[assignment]  # ENGINES infers HeuristicEngine's type from its first entries; LLMLinguaEngine only fits once the optional import succeeds
    ENGINES["llmlingua"] = LLMLinguaEngine  # type: ignore[assignment]  # ENGINES infers HeuristicEngine's type from its first entries; LLMLinguaEngine only fits once the optional import succeeds


def get_engine(name: str = "heuristic") -> CompactionEngine:
    """Get a compaction engine by name."""
    engine_class = ENGINES.get(name, HeuristicEngine)
    return engine_class()


__all__ = ["base", "heuristic", "llmlingua"]

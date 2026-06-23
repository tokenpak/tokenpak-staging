"""
tokenpak.proxy.token_cache — LRU token count cache + counters.

Extracted from runtime/proxy.py (L1143-1176, L6260-6270) as part of TPK-RESTRUCTURE-004.
"""
import hashlib
from typing import Dict

# ---------------------------------------------------------------------------
# Token count counters (module-level)
# ---------------------------------------------------------------------------

_TOKEN_CACHE_HITS: int = 0
_TOKEN_CACHE_MISSES: int = 0


def _inc_token_cache_hit() -> None:
    global _TOKEN_CACHE_HITS
    _TOKEN_CACHE_HITS += 1


def _inc_token_cache_miss() -> None:
    global _TOKEN_CACHE_MISSES
    _TOKEN_CACHE_MISSES += 1


# ---------------------------------------------------------------------------
# Digest-keyed FIFO token count cache
# ---------------------------------------------------------------------------

_TOKEN_COUNT_CACHE: Dict[str, int] = {}  # sha256(text) -> token_count
_TOKEN_COUNT_CACHE_MAX = 1024


def _token_count_cached(text: str, encoder) -> int:
    """Count tokens with a digest-keyed FIFO cache. Avoids re-encoding repeated text.

    Keyed on a SHA-256 digest of the text (not ``hash(text)``): the builtin hash
    is collision-prone and process-randomised, which could return another text's
    count and corrupt budget/truncation accounting.
    """
    key = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if key in _TOKEN_COUNT_CACHE:
        _inc_token_cache_hit()
        return _TOKEN_COUNT_CACHE[key]
    _inc_token_cache_miss()
    result = len(encoder.encode(text))
    if len(_TOKEN_COUNT_CACHE) >= _TOKEN_COUNT_CACHE_MAX:
        # Evict oldest key (dict insertion order preserved in Python 3.7+)
        _TOKEN_COUNT_CACHE.pop(next(iter(_TOKEN_COUNT_CACHE)))
    _TOKEN_COUNT_CACHE[key] = result
    return result


# ---------------------------------------------------------------------------
# Public count_tokens() — tiktoken if available, else cheap fallback
# ---------------------------------------------------------------------------

try:
    import tiktoken

    _ENC = tiktoken.get_encoding("cl100k_base")

    def count_tokens(text: str) -> int:
        return _token_count_cached(text, _ENC)

except ImportError:

    def count_tokens(text: str) -> int:  # type: ignore[misc]
        return len(text) // 4

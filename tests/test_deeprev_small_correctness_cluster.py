"""Focused regression tests for a small correctness cluster.

Each test targets one of the five independent fixes and is written to fail against
the pre-patch code and pass after the fix:

1. core/startup_validator.py - broken runtime import path.
2. orchestration/learning.py - memory_promoter bridge wrappers (ImportError when called).
3. _cli_core.py - ``claude --budget`` value parsing (uncaught float()/trailing flag).
4. proxy/cache.py - unconditional DEBUG print in CacheEntry.is_expired().
5. proxy/token_cache.py - token-count cache keyed on collision-prone hash(text).
"""
import builtins
import time

import pytest


# ---------------------------------------------------------------------------
# 1) startup_validator: validate_on_startup() must not raise ImportError.
# ---------------------------------------------------------------------------
def test_validate_on_startup_import_smoke():
    """Calling validate_on_startup exercises the (formerly broken) local import."""
    from tokenpak.core.startup_validator import validate_on_startup

    # Non-existent config + warn_only=True returns True without raising; the point
    # is that the in-function `from tokenpak.cli.cli_validate_config import ...`
    # resolves (pre-patch it pointed at the non-existent tokenpak.cli_validate_config).
    result = validate_on_startup("/nonexistent/tokenpak-test/config.yaml", warn_only=True)
    assert result is True


# ---------------------------------------------------------------------------
# 2) learning.py memory_promoter bridge wrappers must import + run.
# ---------------------------------------------------------------------------
def test_learning_wrappers_callable(tmp_path):
    """record_lesson / run_memory_promotion / get_durable_lessons no longer ImportError."""
    from tokenpak.orchestration.learning import (
        get_durable_lessons,
        record_lesson,
        run_memory_promotion,
    )

    store = str(tmp_path / "memory_promoter.json")

    lesson = record_lesson(
        "test_lesson",
        "Prefer cheaper model for trivial edits.",
        outcome=1.0,
        specificity_score=0.6,
        material_savings=0.2,
        memory_path=store,
    )
    assert lesson is not None
    assert getattr(lesson, "lesson_id", None) == "test_lesson"

    sweep = run_memory_promotion(memory_path=store)
    assert isinstance(sweep, dict)

    durable = get_durable_lessons(memory_path=store)
    assert isinstance(durable, list)


# ---------------------------------------------------------------------------
# 3) _cli_core: claude --budget parsing.
# ---------------------------------------------------------------------------
def test_extract_claude_budget_valid():
    from tokenpak._cli_core import _extract_claude_budget

    budget, passthrough = _extract_claude_budget(["--budget", "5.00", "-p", "hi"])
    assert budget == 5.0
    assert passthrough == ["-p", "hi"]


def test_extract_claude_budget_absent():
    from tokenpak._cli_core import _extract_claude_budget

    budget, passthrough = _extract_claude_budget(["-p", "hi", "--model", "opus"])
    assert budget is None
    assert passthrough == ["-p", "hi", "--model", "opus"]


def test_extract_claude_budget_non_numeric_clean_error(capsys):
    """`claude --budget abc` is a clean usage error (exit 2), not a traceback."""
    from tokenpak._cli_core import _extract_claude_budget

    with pytest.raises(SystemExit) as exc:
        _extract_claude_budget(["--budget", "abc"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--budget" in err and "number" in err


def test_extract_claude_budget_trailing_flag_clean_error(capsys):
    """A trailing `--budget` with no value errors cleanly instead of being forwarded."""
    from tokenpak._cli_core import _extract_claude_budget

    with pytest.raises(SystemExit) as exc:
        _extract_claude_budget(["-p", "hi", "--budget"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--budget" in err


# ---------------------------------------------------------------------------
# 4) cache.py: CacheEntry.is_expired() must not print a DEBUG line.
# ---------------------------------------------------------------------------
def test_cache_is_expired_no_debug_output(capsys):
    from tokenpak.proxy.cache import CacheEntry

    entry = CacheEntry(
        key="k",
        value="v",
        created_at=time.monotonic() - 10.0,
        last_accessed=time.monotonic(),
        ttl_seconds=5.0,
        size_bytes=1,
    )
    assert entry.is_expired() is True
    captured = capsys.readouterr()
    assert "DEBUG" not in captured.err
    assert "DEBUG" not in captured.out


# ---------------------------------------------------------------------------
# 5) token_cache: distinct text must yield distinct counts even under hash collision.
# ---------------------------------------------------------------------------
class _StubEncoder:
    """Encoder whose token count equals the character length of the text."""

    def encode(self, text):
        return list(text)


def test_token_cache_distinct_text_under_forced_collision(monkeypatch):
    """Force every hash(text) to collide; the digest-keyed cache must still distinguish."""
    from tokenpak.proxy import token_cache

    # Under the old hash(text) keying both texts would map to the same bucket and
    # the second lookup would wrongly return the first text's count.
    monkeypatch.setattr(builtins, "hash", lambda _x: 0)
    token_cache._TOKEN_COUNT_CACHE.clear()

    enc = _StubEncoder()
    count_a = token_cache._token_count_cached("aaa", enc)
    count_b = token_cache._token_count_cached("bbbbb", enc)

    assert count_a == 3
    assert count_b == 5


def test_token_cache_hit_returns_cached(monkeypatch):
    """A repeated text hits the cache and is not re-encoded."""
    from tokenpak.proxy import token_cache

    token_cache._TOKEN_COUNT_CACHE.clear()

    calls = {"n": 0}

    class _CountingEncoder:
        def encode(self, text):
            calls["n"] += 1
            return list(text)

    enc = _CountingEncoder()
    first = token_cache._token_count_cached("hello world", enc)
    second = token_cache._token_count_cached("hello world", enc)
    assert first == second == 11
    assert calls["n"] == 1

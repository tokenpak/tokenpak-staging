"""
tests/benchmarks/test_headline_claim.py — Headline corpus token-reduction benchmark.

Runs the deterministic headline corpus as a local benchmark receipt. Public
README/release claims must not cite a fixed percentage unless the current
release has a matching benchmark receipt.

Fixture: tests/fixtures/headline_corpus.txt
  A deterministic 9-message DevOps agent conversation (~8 kB) designed to
  exercise the alias compressor (repeated CamelCase service names, file paths,
  env vars) and the dedup/directives pipeline stages.

Reproducible locally: make benchmark-headline
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tokenpak.compression.pipeline import CompressionPipeline  # noqa: E402

FIXTURE = Path(__file__).parent.parent / "fixtures" / "headline_corpus.txt"

FORBIDDEN_PUBLIC_BANDS = ("30-50", "30–50")

_ROLES = {"system", "user", "assistant"}


def _load_messages() -> list[dict]:
    """Parse headline_corpus.txt into a messages list.

    Format: lines starting with [role] (role in system/user/assistant) open a
    new message; all lines until the next header are its content.
    """
    text = FIXTURE.read_text(encoding="utf-8")
    messages: list[dict] = []
    current_role: str | None = None
    current_lines: list[str] = []

    for line in text.splitlines():
        stripped = line.strip()
        if (
            stripped.startswith("[")
            and stripped.endswith("]")
            and stripped[1:-1] in _ROLES
        ):
            if current_role is not None:
                messages.append(
                    {"role": current_role, "content": "\n".join(current_lines).strip()}
                )
            current_role = stripped[1:-1]
            current_lines = []
        else:
            current_lines.append(line)

    if current_role is not None:
        messages.append(
            {"role": current_role, "content": "\n".join(current_lines).strip()}
        )

    return messages


def test_headline_corpus_benchmark_receipt(tmp_path: Path) -> None:
    """Compression on the headline corpus must produce a sane local receipt.

    Uses a per-invocation instruction table (tmp_path) so results are
    identical on every run regardless of prior test history.
    """
    messages = _load_messages()
    assert len(messages) >= 5, (
        f"Corpus parse error: expected >= 5 messages, got {len(messages)}. "
        f"Check tests/fixtures/headline_corpus.txt format."
    )

    pipeline = CompressionPipeline(
        instruction_table_path=str(tmp_path / "instruction_table.json"),
    )
    result = pipeline.run(messages)

    reduction_pct = result.savings_pct

    print(
        f"\nheadline benchmark: {reduction_pct:.1f}% reduction "
        f"({result.tokens_raw}→{result.tokens_after} tokens)"
    )

    assert 0.0 <= reduction_pct <= 100.0, (
        f"Headline corpus benchmark produced invalid reduction: {reduction_pct:.1f}%"
    )


def test_readme_does_not_publish_unreceipted_default_percentage_band() -> None:
    """README should avoid fixed default savings bands without current receipts."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    for band in FORBIDDEN_PUBLIC_BANDS:
        assert band not in readme

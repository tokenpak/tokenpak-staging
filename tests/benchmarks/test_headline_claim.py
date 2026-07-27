"""
tests/benchmarks/test_headline_claim.py — Headline token-reduction benchmark.

Regression guard on the compression pipeline: the fixture below must keep
reducing within a fixed band.

This pins *pipeline behavior against a deterministic corpus*, not a public
claim. The README makes no percentage promise, and the messaging standard
prohibits savings claims outright — so a red result here means the pipeline or
the fixture moved, never that public copy needs a number restored.

Standard 21 §9.8 — process-enforced blocking job.
Do NOT merge a PR to main if this test is red.

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

# Inclusive band for the fixture below — a measured property of this corpus, not
# a published figure. Move it only alongside a re-measured fixture.
REDUCTION_MIN = 30.0
REDUCTION_MAX = 50.0

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
        if stripped.startswith("[") and stripped.endswith("]") and stripped[1:-1] in _ROLES:
            if current_role is not None:
                messages.append({"role": current_role, "content": "\n".join(current_lines).strip()})
            current_role = stripped[1:-1]
            current_lines = []
        else:
            current_lines.append(line)

    if current_role is not None:
        messages.append({"role": current_role, "content": "\n".join(current_lines).strip()})

    return messages


def test_headline_claim(tmp_path: Path) -> None:
    """Compression on the headline corpus must land in [30, 50]%.

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

    assert REDUCTION_MIN <= reduction_pct <= REDUCTION_MAX, (
        f"Compression regression: {reduction_pct:.1f}% not in "
        f"[{REDUCTION_MIN}, {REDUCTION_MAX}] on the deterministic fixture. "
        f"This band is a measured property of that corpus, not a published "
        f"figure — do not 'fix' it by changing public copy. Investigate the "
        f"pipeline change, or re-measure and move the fixture and band together."
    )

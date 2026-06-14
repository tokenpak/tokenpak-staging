# OSS Targeted PAK Retrieval PRD

> **⚠️ SUPERSEDED (2026-06-14) — DO NOT IMPLEMENT FROM THIS DOCUMENT.**
> This conceptual draft is no longer the active design. After review it was
> found to cross the OSS/Pro boundary (it described a Pro cross-source recall
> resolver in OSS clothing — see Std 32 §1.3 / §5). It is replaced by the
> revised, boundary-correct proposal:
>
> - **Proposal:** `vault: 01_PROJECTS/tokenpak/proposals/2026-06-13-vault-retrieval-accuracy-and-recall-boundary-split.md`
> - **Vault branch / SHA:** `trix/vault-retrieval-accuracy-proposal-for-main-2026-06-13` @ `a12dd21f10`
>
> The proposal splits this into three tracks (OSS vault-retrieval accuracy /
> Pro cross-source recall — parked / schema-API fidelity). Retained here for
> history only; it is not a build source.

Status: Superseded
Superseded-by: vault 01_PROJECTS/tokenpak/proposals/2026-06-13-vault-retrieval-accuracy-and-recall-boundary-split.md (a12dd21f10)
Owner: TokenPak
Scope: OSS retrieval and context assembly
Last updated: 2026-06-14

## Summary

Build an OSS targeted PAK assembler that retrieves at most one ranked item from
each supported local source:

- Vault Index
- Journal Entries
- Decision Memory
- Session Capsules

The assembler must prioritize accuracy over speed while remaining deterministic,
local-first, explainable, and bounded. It should use the existing TokenPak
structure more completely than plain vault BM25 by combining source-specific
candidate retrieval, deterministic boosts, cross-source ranking, and explicit
selection reasons.

The OSS output is not a Pro MultiPak ranking system. It is a focused local
context package containing zero or one selected PAK from each source, plus
ranking metadata that explains why each source result was selected.

## Problem

Current OSS retrieval is accurate enough for simple lexical vault search but too
narrow for high-confidence targeted context. The active path relies primarily on
BM25 over vault blocks. Journals, decisions, capsules, and PAK metadata exist in
the codebase, but they are not first-class ranked contributors to default context
assembly.

This causes several accuracy failures:

- Exact file paths and packet identifiers can lose to nearby lexical matches.
- Recent or authoritative decisions are not reliably preferred over archived or
  superseded notes.
- Session and journal continuity is not represented in the target package.
- Decision recall is brittle when it depends on exact query hashes.
- Vault search can retrieve the right topic while missing the right level of
  authority, recency, or status.

## Goals

- Assemble a targeted OSS PAK with at most one item per source.
- Prefer accuracy over raw latency, within a predictable local runtime budget.
- Keep ranking deterministic for the same query, index state, config, and clock
  date bucket.
- Make ranking explainable through visible score components and reason codes.
- Strongly prefer exact path, exact title, block id, packet id, and session id
  matches when present.
- Penalize stale, archived, superseded, noisy, or weakly covered candidates.
- Use source-specific retrieval before cross-source selection.
- Provide an evaluation harness with golden queries and measurable top-k quality.

## Non-Goals

- Do not implement Pro autonomous PAKPlan injection.
- Do not assemble multiple PAKs from the same source in OSS.
- Do not require remote embeddings or network services.
- Do not make LLM judgment calls part of the deterministic ranking path.
- Do not replace existing `vault_search`; add a higher-level targeted assembler.

## User Story

As a local TokenPak user, when I ask for context about a task, decision, file, or
prior session, I want TokenPak to retrieve the most accurate local evidence from
the vault, journal, decisions, and capsules, then assemble a compact targeted PAK
with clear ranking reasons so I can trust the context being injected.

## Source Model

Each source adapter returns normalized candidates. A candidate is any local item
that can be represented as a PAK-like context unit.

```json
{
  "candidate_id": "vault:<block_id>",
  "source": "vault",
  "title": "string",
  "summary": "string",
  "content": "string",
  "path": "string|null",
  "created_at": "iso|null",
  "updated_at": "iso|null",
  "status": "current|active|accepted|draft|archived|superseded|unknown",
  "authority": "decision|source|journal|capsule|unknown",
  "base_score": 0.0,
  "score_components": {},
  "reasons": [],
  "risks": []
}
```

Supported adapters:

- `vault`: BM25 over vault blocks, with exact path and metadata boosts.
- `journal`: journal entries for current and recent sessions.
- `decision`: stored decisions, expanded beyond exact query hash.
- `capsule`: session capsules, especially decisions, artifacts, and action items.

## Retrieval Pipeline

1. Normalize query.
   - Lowercase for matching.
   - Preserve raw query for exact path/title matching.
   - Extract candidate identifiers: paths, block ids, dates, session ids, task
     packet ids, issue ids, and likely filenames.

2. Run exact lookup routes.
   - `block_id` exact match.
   - Normalized path exact match.
   - Path suffix match.
   - Title/frontmatter exact or near-exact match.
   - Session id exact match.
   - Decision id exact match.

3. Run source-specific candidate retrieval.
   - Retrieve more candidates than final output needs.
   - Recommended default: top 25 per source before reranking.
   - Use source-native indexes where available.

4. Score each candidate deterministically.
   - Compute lexical relevance.
   - Compute exactness boosts.
   - Compute metadata and authority boosts.
   - Compute recency and staleness modifiers.
   - Compute query coverage.
   - Apply penalties.

5. Select one winner per source.
   - Pick the highest final score for each source.
   - Require minimum confidence unless exact lookup matched.
   - Preserve deterministic tie breakers.

6. Assemble targeted PAK.
   - Sort selected source winners by final score.
   - Include source sections with reasons and risks.
   - Respect token budget.
   - Include omitted-source diagnostics.

## Scoring

Final score should be deterministic and explainable:

```text
final_score =
  lexical_score
  + exactness_boost
  + authority_boost
  + recency_boost
  + coverage_boost
  + source_specific_boost
  - stale_penalty
  - noise_penalty
```

Recommended default weights:

```text
lexical_score:          0.00 to 60.00
exact_path_boost:      +40.00
path_suffix_boost:     +25.00
exact_title_boost:     +30.00
packet_id_boost:       +30.00
block_id_boost:        +50.00
session_id_boost:      +35.00
authority_boost:        0.00 to 15.00
recency_boost:          0.00 to 10.00
coverage_boost:         0.00 to 12.00
source_specific_boost:  0.00 to 10.00
archived_penalty:      -10.00
superseded_penalty:    -25.00
low_coverage_penalty:  -15.00
large_block_penalty:    0.00 to -8.00
```

Tie breaker order:

1. Higher final score.
2. Higher exactness score.
3. Higher authority score.
4. More recent `updated_at`.
5. Shorter normalized path.
6. Lexicographic `candidate_id`.

## Source-Specific Ranking Rules

### Vault Index

Use BM25 as the recall layer, not the final authority layer.

Boost:

- Exact normalized path.
- Path suffix match.
- Filename stem match.
- Exact title or frontmatter title.
- Task packet id or dated packet id.
- Current/active status.

Penalize:

- Archived paths unless the query asks for archived material.
- Superseded packets.
- Very large whole-session transcript blocks when a smaller source also matches.
- Blocks with poor query coverage.

### Journal Entries

Use journal entries to recover recent work, milestones, and session-local
context. Prefer recent entries when relevance is comparable.

Boost:

- Current session entries.
- Milestone and user-authored entries.
- Entries with explicit file paths or task ids matching the query.
- Entries within recent date windows.

Penalize:

- Auto entries with weak query coverage.
- Entries without content anchors.

### Decision Memory

Decision retrieval must not depend only on exact query hashes. Add lexical
matching over query text, decision text, notes, and tags where available.

Boost:

- Accepted/high-confidence decisions.
- Exact decision id.
- Decision text covering multiple query terms.
- Decisions linked to matching files, tasks, or sessions.

Penalize:

- Low-confidence decisions.
- Decisions contradicted by newer decisions.

### Session Capsules

Capsules should expose compressed high-signal sections, not raw transcript bulk.

Boost:

- `decisions_made`.
- `artifacts_created`.
- `action_items`.
- Current or recently loaded sessions.
- Capsules linked to matching files or task ids.

Penalize:

- Raw transcript references with no extracted signal.
- Capsules with only generic summaries.

## Targeted PAK Output

The OSS assembler returns a structured object:

```json
{
  "query": "string",
  "mode": "oss_targeted",
  "generated_at": "iso",
  "selected": [
    {
      "source": "vault",
      "candidate_id": "vault:block-id",
      "score": 88.5,
      "confidence": "high",
      "reasons": ["exact_path", "high_bm25", "current_status"],
      "risks": [],
      "title": "string",
      "path": "string",
      "content": "string"
    }
  ],
  "omitted_sources": [
    {
      "source": "decision",
      "reason": "no_candidate_above_threshold"
    }
  ],
  "diagnostics": {
    "candidate_counts": {
      "vault": 25,
      "journal": 8,
      "decision": 4,
      "capsule": 3
    }
  }
}
```

Confidence bands:

```text
high:   exact match or final_score >= 70 with good coverage
medium: final_score >= 45 with acceptable coverage
low:    final_score >= 25 or weak coverage
omit:   final_score < 25 unless exact route matched
```

## Determinism Requirements

- Same query, same index, same config, and same date bucket must produce the
  same selected candidates and ordering.
- Recency scoring uses calendar-day buckets, not wall-clock seconds.
- No LLM-generated ranking decisions in the default OSS path.
- Optional semantic scores may be added only behind a config flag and must not
  replace deterministic tie breakers.
- Every selected candidate must include score components and reason codes.

## Configuration

Recommended defaults:

```text
TOKENPAK_TARGETED_PAK_ENABLED=1
TOKENPAK_TARGETED_PAK_MAX_PER_SOURCE=1
TOKENPAK_TARGETED_PAK_CANDIDATES_PER_SOURCE=25
TOKENPAK_TARGETED_PAK_MIN_SCORE=25
TOKENPAK_TARGETED_PAK_MIN_CONFIDENCE=low
TOKENPAK_TARGETED_PAK_MAX_TOKENS=6000
TOKENPAK_TARGETED_PAK_INCLUDE_DIAGNOSTICS=1
```

Source toggles:

```text
TOKENPAK_TARGETED_PAK_USE_VAULT=1
TOKENPAK_TARGETED_PAK_USE_JOURNAL=1
TOKENPAK_TARGETED_PAK_USE_DECISIONS=1
TOKENPAK_TARGETED_PAK_USE_CAPSULES=1
```

## API Surface

Add a new endpoint:

```text
GET /tpk/v1/targeted-pak?q=<query>&limit_per_source=1
```

Add MCP tool:

```text
targeted_pak(query: string, max_tokens?: number, diagnostics?: boolean)
```

Keep `vault_search` as the lower-level vault-only search primitive.

## Implementation Plan

Phase 1: Retrieval contracts

- Add normalized `RetrievalCandidate` and `TargetedPakResult` dataclasses.
- Add source adapter interface.
- Implement vault adapter using existing BM25 plus exact path/title boosts.
- Implement deterministic scorer and reason-code output.

Phase 2: Source adapters

- Add journal adapter over existing journal store.
- Add decision adapter with lexical search over decision text and notes.
- Add capsule adapter using high-signal capsule sections.
- Add one-winner-per-source selection.

Phase 3: API and MCP

- Add `/tpk/v1/targeted-pak`.
- Add MCP `targeted_pak`.
- Add diagnostics and omitted-source reporting.
- Keep output bounded by token budget.

Phase 4: Evaluation

- Add golden query fixtures.
- Track exact-match top-1, MRR, Recall@1 per source, and selected-source
  accuracy.
- Add regression tests for exact path, archived/superseded penalties, recent
  journal preference, decision paraphrase, and capsule section preference.

## Acceptance Criteria

- Exact block id and exact normalized path rank first for vault.
- Exact session id ranks first for capsules or journals when present.
- A paraphrased decision query can retrieve a relevant decision without exact
  query hash.
- Archived/superseded candidates lose to current candidates when relevance is
  comparable.
- The assembler returns no more than one selected candidate per source.
- Every selected candidate includes score components, reasons, risks, and source.
- Results are stable across repeated runs with unchanged input data.
- Golden query evaluation passes configured quality thresholds.

## Open Questions

- Should OSS include a lightweight SQLite FTS table for decisions and journals,
  or start with in-memory lexical scoring?
- Should recency be source-dependent, for example stronger for journals than
  vault documents?
- Should low-confidence source winners be included with warnings or omitted by
  default?
- Should targeted PAK injection replace current vault injection or remain an
  explicit opt-in mode during beta?

---
---

Vault retrieval: scope results to a declared project instead of blending several.

When one vault holds several projects, lexical ranking cannot separate them.
Projects of the same kind share nearly all vocabulary, and identifiers collide —
several projects can sit at pull request 100 simultaneously. A query like
"audit the PR 100 for the project" reduces to the terms `pr` and `100` after
stopword removal, and previously returned a blend drawn from several projects
and presented as one coherent result set. That failure is worse than an empty
answer: it looks authoritative and nothing downstream can distinguish it.

Ranking cannot fix this — a relevance boost still loses to dense term overlap
from the wrong project — so scope is resolved before ranking and applied as a
filter.

Adds an optional `projects:` block to `vault.yaml` (additive; schema v1
unchanged, so existing configs load untouched):

- Project identity is **declared, not derived from paths**. One project spans
  many roots — workbench, staging, archive, notes — and unrelated projects share
  path shapes, so membership is an explicit many-roots-to-one-project relation.
- Membership is stored as a join table, because a shared resource directory
  genuinely belongs to several projects; a `shared: true` root is visible from
  every scope.
- Roots resolve by longest prefix, so nested and overlapping declarations
  resolve deterministically regardless of declaration order.
- Declaring the same path under two different projects is a load-time error
  rather than a silent tiebreak.
- Roots carry a role; archived copies are excluded from results by default so
  stale duplicates cannot outrank the live tree.

Scope is resolved from, in order: an explicit `project` argument,
`$TOKENPAK_PROJECT`, the working directory, or a project named uniquely in the
query. When none of these resolve and results span more than one project,
retrieval fails closed — `GET /tpk/v1/vault/search` returns `409
ambiguous_project_scope` with the candidate list, and automatic context
injection contributes nothing rather than a blend.

Because the guarantee is a safety property, degradation paths fail closed too: a
`project` request that the active retrieval backend cannot honor returns `501
scoping_unsupported` instead of silently returning unscoped results, and a
project registry that is present but unreadable causes scoped queries to be
refused while preserving the last known-good membership.

The default `json_blocks`, optional SQLite, and editor-plugin retrieval indexes
all enforce the same filter and ambiguity contract. A discovery-driven
conformance matrix runs the shared regressions against every shipping index and
a minimal third-party implementation; an unverified backend is refused rather
than silently serving an unscoped answer.

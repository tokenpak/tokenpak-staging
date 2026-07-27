# Project scoping

When one vault holds several projects, retrieval has to know which one you mean
before it ranks anything.

## The problem

Retrieval ranks lexically. Two projects of the same kind share almost all of
their vocabulary, and identifiers collide freely — several projects can sit at
pull request 100 at the same time. A query like:

> audit the PR 100 for the project

reduces to the terms `pr` and `100` once stopwords are removed. `pr` appears
throughout any development vault, and `100` is ambiguous precisely because the
projects are progressing together. Nothing in the query identifies a project.

Without scoping the result is not an empty answer — it is a *blend*, drawn from
two or three projects and presented as one coherent set of results. That is the
failure worth preventing: it looks authoritative, and nothing downstream can
tell it apart from a correct answer.

Ranking cannot fix this. A relevance boost for the "right" project still loses
to dense term overlap from a wrong one. Scope has to be a filter, applied before
scoring.

## Declaring projects

Project identity is **declared, not derived from paths**. A directory is not a
project: one project routinely spans a workbench, a staging checkout, an
archived copy and a notes tree, while unrelated projects share path shapes.

Add a `projects:` block to `~/.tokenpak/vault.yaml`:

```yaml
version: 1

paths:
  - path: ~/vault

projects:
  - id: acme-storefront
    aliases: [acme, storefront]
    roots:
      - path: ~/workspace/acme-storefront
        role: workbench
      - path: ~/staging/acme-storefront
        role: staging
      - path: ~/archive/2025/acme-store-legacy
        role: archive
      - path: ~/vault/01_PROJECTS/acme
        role: notes

  - id: bluefin-portal
    roots:
      - path: ~/workspace/bluefin-portal
        role: workbench

  - id: design-system
    roots:
      - path: ~/workspace/design-system
        role: library
        shared: true          # visible from every project's scope
```

`paths:` says *what to index*. `projects:` says *what belongs to whom*. They are
independent — a path can be indexed without belonging to any project.

The block is optional and additive. A `vault.yaml` without it keeps working
exactly as before, and retrieval stays unscoped.

### Fields

| Field | Meaning |
|---|---|
| `id` | Canonical project identifier. Lowercase alphanumeric, `.`, `_`, `-`. |
| `aliases` | Other names that identify the project in query text. |
| `roots[].path` | A directory belonging to the project. |
| `roots[].role` | What kind of copy this is — `workbench`, `staging`, `archive`, `notes`, or anything you choose. |
| `roots[].shared` | `true` makes the root visible from every project's scope. |
| `roots[].projects` | Explicit membership list, for a root genuinely shared by named projects. |

### Overlap and multiple directories

Two rules keep overlapping declarations safe:

**Longest prefix wins.** Roots are matched by path specificity, so a nested root
resolves ahead of a broader one regardless of declaration order. Given roots
`~/vault` (project `notes-archive`) and `~/vault/01_PROJECTS/acme` (project
`acme-storefront`), a file under the latter resolves to `acme-storefront`.

**Ambiguity is an error, not a tiebreak.** Declaring the same path under two
different projects fails at config load with a message naming both. Genuine
sharing is expressed *within one root* instead:

```yaml
  - id: acme-storefront
    roots:
      - path: ~/workspace/shared-checkout
        projects: [acme-storefront, bluefin-portal]
```

### Archived copies

Roles with `archive` are excluded from results by default. An archived copy is
still part of its project and stays addressable, but a stale duplicate should
not compete with the live tree — archived files often repeat the same terms and
would otherwise rank well.

## How scope is resolved

First confident signal wins:

1. An explicit `project` argument.
2. `$TOKENPAK_PROJECT`, for pinning a shell or session.
3. The working directory, matched against declared roots.
4. A project id or alias named literally in the query text.

Step 4 requires a *unique* mention. A query naming two projects is ambiguous,
and guessing between them is the failure being prevented.

## When scope cannot be resolved

If none of the signals identify a project **and** the results span more than one,
retrieval fails closed:

- `GET /tpk/v1/vault/search` returns `409 ambiguous_project_scope` with the
  candidate list, so the caller can ask a more specific question.
- Automatic context injection contributes nothing at all.

A single-project result set is never ambiguous, so ordinary queries against a
one-project vault are unaffected.

## Practical guidance

- Name the project, or work from inside one of its roots. Deictic phrasing —
  "the project", "this repo" — carries no signal.
- Pin a scope for a working session with `export TOKENPAK_PROJECT=acme-storefront`.
- Declare every directory that belongs to a project, including staging and
  archived copies. A root you leave out is unreachable from that project's scope.
- Editing `projects:` re-resolves membership on the next reload — no reindex
  needed, since only path membership changed.

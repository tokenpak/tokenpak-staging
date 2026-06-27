---
---

Add the Prompt Packing wrapper surface to `tokenpak.compression`.

Recovers the lost `PromptPacker` / `PromptPackingService` / `PromptPackingResult`
wrapper (originally introduced on the unmerged `feat/terminology-prompt-packer`
branch, commit `fdd5142e`; absent on both staging and public main). The module
orchestrates the existing compression pipeline and context-pack compiler,
producing a TIP-conformant `Pak` with full compression metadata. No existing
compression classes are renamed or relocated — the surface is purely additive.

New module: `tokenpak/compression/prompt_packing.py`.
Canonical import: `from tokenpak.compression import PromptPacker`.

Release-gate: the public-API snapshot (`tokenpak/_snapshots/public-api.json`) is
regenerated to record the new surface. Net: **+10 symbols** (4659 → 4669), all
additions, no removals — the 5 public classes
(`CompressionMetadata`, `PackingPolicy`, `PromptPacker`, `PromptPackingResult`,
`PromptPackingService`) recorded under both `tokenpak.compression` and
`tokenpak.compression.prompt_packing` for release review.

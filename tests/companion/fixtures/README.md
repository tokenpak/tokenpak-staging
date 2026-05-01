# Companion transcript fixture corpus

This directory is the small offline corpus used to exercise session capsule generation without live provider/API calls.

| Fixture | Shape | Purpose | Runtime proof mapping |
|---|---|---|---|
| `basic_session.jsonl` | Normal Claude Code-style transcript with user, assistant, attachment, last-prompt, and ai-title records | Proves a parsed transcript can normalize into capsule input and produce a deterministic capsule with source metadata | TCM-05/TCM-06: validates the capsule producer/consumer boundary that joins session capsules with TIP cache context |
| `empty.jsonl` | Empty transcript file | Proves no-message sessions gracefully return no capsule instead of failing or fabricating context | TCM-06: downstream integration can treat `None` as vault-only fallback |
| `malformed.jsonl` | Mixed valid and invalid JSONL lines | Proves malformed lines are counted/skipped while valid messages still feed capsule generation | TCM-06/TCM-09: keeps status and attribution paths from depending on perfect transcript hygiene |
| `multiblock_assistant.jsonl` | Assistant/user content with thinking, text, tool_use, and tool_result blocks | Proves richer transcript block shapes flatten into useful capsule text and artifact/action hints | TCM-06: validates local transcript normalization before cache-context injection |

Provider/model/platform stance: these fixtures describe transcript shapes, not provider routing. They use only local parser and capsule-builder code, so they are safe for offline proof and do not mutate OpenClaw or provider configuration.

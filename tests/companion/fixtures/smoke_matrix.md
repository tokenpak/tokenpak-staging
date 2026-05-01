# Capsule offline smoke matrix

This matrix records local-only capsule generation over the companion transcript fixture corpus. It does not call provider APIs, mutate OpenClaw configuration, or route through model/provider adapters.

Smoke mode uses `CapsuleBuilder(min_message_count=1)` so every non-empty fixture shape can be exercised. Production default mode uses `CapsuleBuilder()` and therefore preserves the five-message minimum for runtime capsule usefulness.

| Fixture | Messages | Parse errors | Smoke capsule | Production-default capsule | Output summary | TCM mapping |
|---|---:|---:|---|---|---|---|
| `basic_session.jsonl` | 6 | 0 | yes | yes | Context: `Please help me refactor the authentication module`; artifact: `/home/trix/project/auth.py`; sha256: `0796f1acfe8ba5565e4c031fcbea29a56733cc412b2d3f447fb0711c7a1e79b7` | TCM-04/TCM-06: normal transcript can produce deterministic session context for TIP cache enrichment |
| `empty.jsonl` | 0 | 0 | no | no | Empty transcript returns no capsule; no fabricated context | TCM-06: downstream enrichment can fall back to vault-only/no-capsule behavior safely |
| `malformed.jsonl` | 3 | 2 | yes | no | Bad JSONL lines are counted and skipped; valid messages still build smoke capsule; sha256: `00cd2c618a856a73718af0dc4b50ad2ef4bdcd51c69377ff15013df7b5f7bd68` | TCM-06/TCM-09: malformed local transcript hygiene is observable and does not require provider calls |
| `multiblock_assistant.jsonl` | 4 | 0 | yes | no | Multi-block assistant content flattens tool context; artifact: `tests/test_auth.py`; action includes `python3 -m pytest tests/ -v`; sha256: `42a1292a5e8078e65dfc0c9cd4cd25a721022a9e237b98b2b989f40dd059885b` | TCM-04/TCM-06: local transcript normalization preserves tool/action hints before cache-context injection |

## Provider/model/platform-agnostic proof

- Inputs are local JSONL fixtures under `tests/companion/fixtures/`.
- Execution uses `parse_transcript()` and `CapsuleBuilder` only.
- No provider name, model name, platform adapter list, credential surface, OpenClaw config, or network endpoint is enumerated or modified.
- Output checks validate transcript shape handling and deterministic capsule metadata only.

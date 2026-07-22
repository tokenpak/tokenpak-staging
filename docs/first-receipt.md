# First Measured Savings Receipt

This is TokenPak's supported reference path from a clean install to a measured
receipt from your own real provider request. Its reference target is three shell
commands and no more than five minutes.

## Before you start

Have these available before starting the three-command path:

- Python 3.10 or newer.
- `ANTHROPIC_API_KEY` exported in the shell that will send the request.
- `ANTHROPIC_MODEL` exported to a model ID available to that account.
- An existing `README.md` in the current project directory containing at least
  8,000 UTF-8 characters of real project context. The reference request uses
  that document; it does not generate filler text.
- Outbound access to the Anthropic API. The third command is a real provider
  request and may incur provider charges.

Credential acquisition and provider login are prerequisites, not TokenPak
onboarding commands. Environment-specific package-download or provider latency
can extend wall-clock time beyond the reference target. TokenPak forwards the
API-key header for this request but does not persist it.

## The three commands

1. Install TokenPak:

   ```bash
   python -m pip install tokenpak
   ```

2. In terminal 1, start one proxy session with an eligible compression profile
   and the local per-request receipt enabled:

   ```bash
   tokenpak serve --profile aggressive --stats-footer
   ```

3. Leave terminal 1 running. From the project directory containing the eligible
   `README.md`, use terminal 2 to ask for a real release-readiness review:

   ```bash
   python -c 'import json, os, pathlib, sys, urllib.request as u; p=pathlib.Path("README.md"); context=p.read_text(encoding="utf-8"); len(context) >= 8000 or sys.exit("README.md must contain at least 8,000 UTF-8 characters of real project context"); data=json.dumps({"model": os.environ["ANTHROPIC_MODEL"], "max_tokens": 256, "messages": [{"role": "user", "content": f"Project document README.md:\n\n{context}"}, {"role": "assistant", "content": "Project context received."}, {"role": "user", "content": "Review this project context and identify five concrete release-readiness risks, citing the README.md section for each."}]}).encode(); req=u.Request("http://127.0.0.1:8766/v1/messages", data=data, headers={"content-type": "application/json", "anthropic-version": "2023-06-01", "x-api-key": os.environ["ANTHROPIC_API_KEY"]}, method="POST"); print(u.urlopen(req, timeout=120).read().decode())'
   ```

After the provider response completes, terminal 1 prints a receipt shaped like:

```text
⚡ TokenPak: -12,345 tokens (61%) | $0.037 saved
```

The token counts come from that request's before/after proxy measurements. The
dollar value is an estimate based on TokenPak's model-pricing table. Your values
will differ by model and payload.

## What does not qualify

- `tokenpak demo` is an offline fixture, not proof from your request.
- Short, already concise, code-heavy, or protected prompts may correctly save
  zero tokens.
- Byte-preserved client routes are intentionally not rewritten and may report
  zero TokenPak compression savings. Use the direct eligible API path above for
  the reference receipt.
- `--stats-footer` is opt-in and applies only to this proxy process. It prints
  the receipt in the proxy terminal; it does not modify the provider response.
- `safe` and `transparent` profiles intentionally do not provide a positive
  compression-savings proof.

Stop the foreground proxy with `Ctrl-C` when finished. For normal application
integration after the reference proof, run `tokenpak integrate` and review the
client-specific instructions before applying changes.

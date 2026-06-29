# First Measured Receipt

This is the supported local-first path from install to a measurable TokenPak
receipt. It requires a provider API key because the receipt comes from one real
eligible provider request through the local proxy, not from `tokenpak demo`.

## Command path

```bash
pip install tokenpak
tokenpak start
curl -sS http://localhost:8766/v1/messages \
  -H "x-api-key: $ANTHROPIC_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model":"claude-3-5-sonnet-20241022","max_tokens":64,"messages":[{"role":"user","content":"Summarize this recurring project context and keep the answer short."}]}'
```

Command count: 3, including install.

Expected local artifact: `~/.tpk/monitor.db`, table `requests`.

What is measured: request timestamp, model, input tokens, output tokens,
estimated request cost, compressed tokens when applicable, cache read/create
tokens, and cache-origin attribution (`proxy`, `client`, or `unknown`).

Inspect the rollup:

```bash
tokenpak status --json
```

## Evidence Boundary

The receipt is created only after a request passes through the local proxy.
`tokenpak demo` is useful as an offline illustration, but it is not the first
receipt proof.

Savings are not fabricated. If TokenPak cannot attribute compression or cache
savings for a request, the receipt records the request and reports zero savings.

No TokenPak cloud service, paid daemon, multi-instance runtime, dashboard write,
or search feature is required for this path.

Regression fixture:

```bash
python3 -m pytest tests/proxy/test_first_receipt_path.py
```

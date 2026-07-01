# Using TokenPak in CI

Use this page when a CI job needs to prove that TokenPak is installed and can
emit machine-readable status without a TTY, provider credentials, or a running
local proxy.

## Minimal Verify-Install Step

The smallest deterministic install check is:

```bash
python -m tokenpak --version
```

Expected result: exit code `0` and a line beginning with `tokenpak`.

## Non-Interactive Status JSON

For a CI-safe status artifact, isolate TokenPak state and request JSON output:

```bash
export TOKENPAK_HOME="${RUNNER_TEMP:-/tmp}/tokenpak-ci"
python -m tokenpak status --json --no-meme > tokenpak-status.json
python - <<'PY'
import json
from pathlib import Path

payload = json.loads(Path("tokenpak-status.json").read_text())
assert payload["proxy"]["reachable"] in (True, False)
assert "tip_cache" in payload
print("tokenpak status json ok")
PY
```

Expected result: exit code `0`. A fresh CI runner normally reports
`"reachable": false` until the proxy is started; that is still a valid install
and JSON-shape check.

## Generic CI Job

```yaml
name: tokenpak-smoke

on:
  pull_request:
  push:

jobs:
  tokenpak:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: python -m pip install -U pip
      - run: python -m pip install tokenpak
      - run: python -m tokenpak --version
      - run: |
          export TOKENPAK_HOME="${RUNNER_TEMP}/tokenpak-ci"
          python -m tokenpak status --json --no-meme > tokenpak-status.json
          python - <<'PY'
          import json
          from pathlib import Path

          payload = json.loads(Path("tokenpak-status.json").read_text())
          assert "proxy" in payload
          assert "tip_cache" in payload
          print("tokenpak ci smoke ok")
          PY
```

This job does not call a live provider and does not require API keys.

## Client Snippet Checks

When a job only needs to verify that TokenPak can print setup instructions for a
client, use a print-only integration target:

```bash
python -m tokenpak integrate openai-sdk
python -m tokenpak integrate anthropic-sdk
python -m tokenpak integrate litellm
```

Those commands print snippets for the selected client. They do not validate
provider credentials.

## Optional Proxy Smoke

If your CI environment intentionally starts the local proxy, run a separate
smoke after the proxy is listening:

```bash
python -m tokenpak start --port 8766 &
sleep 2
python -m tokenpak status --json --no-meme > tokenpak-status.json
```

Only add provider calls in jobs that already manage provider credentials and
cost controls.

## No Dedicated `tokenpak ci` Command Yet

There is no `tokenpak ci` command in the current public CLI. The supported CI
pattern is to compose existing non-interactive commands:

- `python -m tokenpak --version` for install proof.
- `python -m tokenpak status --json --no-meme` for machine-readable local
  status.
- `python -m tokenpak integrate <client>` for print-only client snippets.

A dedicated snippet generator can be reconsidered after these documented
commands prove insufficient across tester jobs.

---
---

Companion Codex doctor now smoke-tests the local Codex Linux sandbox with a
bounded `codex sandbox true` check. Bubblewrap, AppArmor, user-namespace, and
timeout failures are reported as TokenPak-controlled WARN rows with
troubleshooting guidance instead of surfacing as raw Codex startup warnings.

No host remediation is performed automatically: TokenPak does not install
packages, run apt/dpkg, or mutate `/etc`.

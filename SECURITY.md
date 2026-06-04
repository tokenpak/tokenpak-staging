# Security Policy

## Reporting a Security Issue

If you discover a suspected security vulnerability in TokenPak, please report it privately.
**Please do not open a public issue.**

- **Preferred:** [GitHub private vulnerability reporting](https://github.com/tokenpak/tokenpak/security/advisories/new)
- **Email:** security@tokenpak.ai

### What to include

- TokenPak version (`tokenpak --version`)
- Affected component (proxy, cache, compression, routing, CLI, …)
- Steps to reproduce
- Impact — what could an attacker do?
- (Optional) a suggested fix

## Scope

**In scope:** vulnerabilities in TokenPak's own code and published artifacts (this repository and
the official packages it produces).

**Out of scope:** third-party AI providers and their APIs; your own host, OS, or network
configuration; social engineering; and vulnerabilities in dependencies (please report those
upstream — we track them via automated dependency scanning).

## Our Response (targets, not guarantees)

The following are good-faith targets to set expectations — **not contractual SLAs or guarantees:**

- **Acknowledgement:** within **3 calendar days** of your report.
- **Triage & severity:** we assess impact and assign a severity (CVSS v3.1).
- **Remediation targets by severity:**
  - **Critical** — mitigation, advisory, or fix targeted within **~7 calendar days**.
  - **High** — targeted within **~30 calendar days**.
  - **Medium / Low** — addressed in a future scheduled release.
- **Coordinated disclosure:** we will agree a disclosure date with you; our default embargo is
  **90 days**, or until a fix/mitigation ships — whichever comes first.
- When a fix ships we publish a **GitHub Security Advisory** and credit you unless you prefer otherwise.

## Supported Versions

TokenPak is currently in **beta**. During beta, security fixes target the **latest published minor
only**, unless a specific advisory extends coverage.

| Version | Status |
|---------|--------|
| 1.7.x (latest) | ✅ Supported |
| < 1.7 | ⚠️ Best-effort for critical issues only |

## Safe Harbor

We support good-faith security research and will not pursue legal action against researchers who:

- test only against their own installation;
- do not run denial-of-service, mass-targeting, or data-destruction tests;
- do not access, modify, or exfiltrate data that is not theirs;
- report promptly and allow reasonable time to remediate before any public disclosure.

Thank you for helping keep TokenPak secure.

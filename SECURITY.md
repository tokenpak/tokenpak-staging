# Security Policy

## Reporting a Vulnerability

Please report vulnerabilities privately — never through a public issue, discussion, or pull request.

- **Preferred:** [GitHub private vulnerability reporting](https://github.com/tokenpak/tokenpak/security/advisories/new)
- **Email fallback:** **security@tokenpak.ai**

Include the TokenPak version (`tokenpak --version`), reproduction steps, and your assessment of the impact. We aim to acknowledge reports within **3 calendar days**.

## Scope

**In scope:** the TokenPak code and published artifacts — the `tokenpak` package on PyPI, the local proxy, CLI, and companion components in this repository.

**Out of scope:** third-party model providers and their APIs, your own host and network configuration, and social engineering.

## Supported Versions

| Version | Supported |
| ------- | --------- |
| 1.11.x (latest minor) | ✅ Security fixes |
| < 1.11 | ❌ Unsupported — please upgrade |

TokenPak is in beta: security fixes target the **latest public minor release line** unless a security advisory explicitly extends support to an earlier line. This table is checked at each release.

## Coordinated Disclosure

The timelines below are **targets we work to, not absolute guarantees**:

1. **Acknowledge** within 3 calendar days of receipt.
2. **Triage** — we assign a CVSS v3.1 severity (Critical / High / Medium / Low).
3. **Remediation targets by severity:**
   - **Critical:** mitigation, advisory, or patched release targeted within **7 calendar days**.
   - **High:** remediation targeted within **30 calendar days**.
   - **Medium / Low:** next scheduled release.
4. **Disclosure** — we coordinate a disclosure date with the reporter; the default embargo is **90 days**, or until a fix or mitigation is available, whichever is agreed.

Fixes for Medium+ issues are published as GitHub Security Advisories with a CVE requested, and reporters are credited unless they decline. Security fixes are never patched silently.

## Researcher Safe Harbor

We welcome good-faith security research and will not pursue legal action against researchers who:

- test only against their own TokenPak installation;
- do not run denial-of-service, mass-targeting, or data-destruction tests;
- do not access, modify, or exfiltrate data that isn't theirs;
- report promptly through the channels above and allow reasonable remediation time before public disclosure.

## Known Advisories in Optional Extras

TokenPak's core install carries none of the packages below. They arrive only if you opt into an
extra, and we disclose them rather than leave you to discover them.

### `tokenpak[crewai]` — chromadb

Installing this extra pulls `crewai`, which requires `chromadb~=1.1.0`. All published chromadb 1.x
releases are covered by CVE-2026-45829 (pre-authentication code injection), and **no fixed version
exists upstream** as of this writing — the advisory's range covers every release up to and including
the latest.

What this does and does not mean:

- **The published `tokenpak` package never imports or runs chromadb.** No shipped module reaches
  it, and the crewai integration is a set of context and handoff wrappers with no vector-store code
  path. For completeness: this repository also contains an unpublished, unshipped Chroma adapter
  under `packages/tokenpak-vectordb/`, which is not part of any release and not installed by any
  extra. We mention it so that a reader who greps the repository is not left thinking this
  disclosure is inaccurate.
- The exposure is inherent to running chromadb yourself, typically as a server. If your crewai
  configuration does not run one, the advisory has no attack surface in your deployment.
- We cannot pin around it: the constraint is crewai's, not ours.

If you need this extra and the advisory matters to your threat model, evaluate chromadb's exposure
in your own deployment, or run crewai without the vector-store features it enables.

**Review condition.** This note is revisited when a fixed chromadb is published *and* crewai's
constraint permits it. Note the advisory range is bounded at `<= 1.5.9`, so a future 1.6.0 would fall
outside the stated range without necessarily being patched — a version outside the range is not by
itself evidence of a fix.

## Best Practices

### For Users
- Keep TokenPak updated
- Treat prompts as sensitive data
- Avoid logging raw prompts or compressed blocks
- Use separate keys for dev/prod

### For Contributors
- Never commit secrets or API keys
- Validate all user inputs
- Use parameterized database access
- Keep dependencies current

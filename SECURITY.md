# Security Policy

This repository is an offline data-preparation pipeline and runs no online services. Security reports mainly concern code defects, data-build defects, and supply-chain issues.

**English** | [简体中文](SECURITY.zh-CN.md)

## Supported versions

| Version | Supported |
| --- | --- |
| 0.1.x | Security reports and fixes accepted |

Older versions receive no security fixes; before reporting, confirm that the issue exists in a supported version.

## Reporting

Please report **privately** — do not open a public issue, discussion, or PR that discloses details:

1. **Preferred**: GitHub Security Advisories — on the repository's Security tab, choose "Report a vulnerability" (GitHub private vulnerability reporting).
2. Alternative: send a private GitHub message to the maintainer.

Include in your report: a description of the issue and its impact, reproduction steps or a minimal example, the affected version/commit, and optionally a suggested fix.

## Response commitment

- Acknowledge receipt **within 7 days**.
- After confirming, assess the impact and give a fix or mitigation plan; progress is shared through the same private channel.
- Agree on public disclosure timing only after a fix is released.

## Scope

- **Code defects**: security or correctness problems in download, financial extraction, sample building, and similar logic.
- **Data-build defects**: leakage and lookahead violations (for example labels crossing splits, or as-of rules being bypassed).
- **Supply chain**: risks of tampering or poisoning in dependencies, builds, or release processes.

Known data-source realities (missing bars, non-calendar fiscal years, FRED revised values, etc. — see [docs/developer/known-quirks.md](docs/developer/known-quirks.md)) are not security vulnerabilities.

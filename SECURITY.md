# Security

XENIO can act on your computer, so security problems matter more here than in most projects.

## Reporting a vulnerability

Please do **not** open a public issue. Report it privately through GitHub's
[security advisories](https://github.com/tanishqnarula12/Xenio/security/advisories/new) for this repository.

Include what you found, how to reproduce it, and what an attacker could do with it. You will get an answer within a
week. Please give us a reasonable time to fix it before you disclose it.

## What is in scope

- A way around XENIO's permission policy, or an action that runs without the approval it should need.
- Code an agent wrote, or a skill or package it installed, reaching outside the sandbox: your files, your secrets,
  the network.
- Prompt injection that makes XENIO act against your interest (from a web page, a file, an email, a tool result).
- A secret (a key, a token, a password) reaching a log, a model, the interface or the repository.
- Anything on the network reaching XENIO's interface, which listens on this Mac only.

## How XENIO is built to be safe

- [Security model](docs/SECURITY_MODEL.md): the permission policy and approvals.
- [The sandbox](docs/SANDBOX.md): every place XENIO starts a program, how each is contained, and the known limits.
- [War Room security](docs/FOUNDRY_SECURITY.md) and [Engineering security](docs/ENGINEERING_SECURITY.md).
- [Prompt-injection defenses](docs/PROMPT_INJECTION_SECURITY.md).
- [The vault](docs/VAULT.md): passwords agents can use but never read.

## Supported versions

Security fixes go into the latest release on `main`.

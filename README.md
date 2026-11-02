---
title: Sailboat
emoji: ⛵
colorFrom: blue
colorTo: gray
sdk: docker
app_port: 7860
license: mit
hf_oauth: true
hf_oauth_scopes:
  - openid
  - profile
---

# Sailboat

An owner-controlled trading controller, dashboard, and agent connection.
Give your AI agent this repository URL and ask it to follow [instructions.txt](instructions.txt)
to install **your own independent instance**. Every start is DISARMED.

This repository contains the working Python controller, browser dashboard, contribution
assembly, plugin source, skills, and setup templates. Install directly from source.
GitHub is the source distribution; Hugging Face Spaces is an optional hosting destination.
No account, brokerage consent, running server, paid service, or trading permission comes
with the source.

## Start here

1. Clone the repository into a new directory and read [instructions.txt](instructions.txt).
2. Install Python 3.11+ dependencies in a fresh virtual environment.
3. Run `python -m sailboat.instance_setup init --directory .local/my-instance --public-url https://YOUR-OWN-HOST`.
4. Review your generated private configuration, configure your own HTTPS host and owner
   sign-in, and follow [deployment](docs/deployment.md).
5. Authorize your own service connections. Verify DISARMED and disabled broker writes.

The setup command creates new random credentials and empty state. It does not connect
to a broker, start analysis, provision cloud resources, or enable trading. No creator
endpoint or account is configured. Local dashboard HTML is available before sign-in;
account views and owner controls require configured identity verification.

## Included behavior

- Durable decisions, receipts, journal verification, reconciliation, and recovery.
- Existing option premium research, candidate selection, position management, and order guards.
- Owner dashboard with dark/light mode, account view, limits, schedules, manual-control choices, and ARM/DISARM.
- Separate owner credentials and agent credentials; an agent cannot arm through its connection.
- Existing optional scheduled analyst adapters and official Robinhood MCP integration.
- Existing core and assembly source, installed together without private package dependencies.

The strategy is experimental. Tests establish software behavior, not profitability or
permission to trade. Owner-enabled scheduled closing can permit exits at a loss;
it defaults off. Consult [behavior and authority](docs/behavior.md).

## Documentation

- [AI installation instructions](instructions.txt)
- [Configuration reference](docs/configuration.md)
- [Self-hosting and optional Hugging Face Spaces](docs/deployment.md)
- [Brokerage and analyst connections](docs/connections.md)
- [Plugin source and host setup](plugins/sailboat/README.md)
- [Security and isolation](SECURITY.md)
- [Publication review](docs/publication-review.md)
- [Source scope and third-party notices](NOTICE.md)

Legacy language-model experiment modules remain in the source for provenance and reuse;
their historical datasets, model locks, benchmark outputs and experiment launchers are
not distributed or part of this installation workflow. No backtest is needed to install.

## License

Sailboat source is provided under the [MIT License](LICENSE). Service access and dependency
licenses remain separate. Review [NOTICE.md](NOTICE.md) before publishing a derivative.

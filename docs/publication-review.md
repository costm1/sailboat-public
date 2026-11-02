# Publication review

This is a public source distribution. No download bundle, installer, executable, or
hosted account is the deliverable. The following checks also apply to future releases.

Review the repository's complete file list and history. Preserve the MIT License and
applicable third-party attribution. Controller policy files should remain
identical to the selected working source except the documented instance/configuration
boundaries. No private state, recorded assessments, deployment reports, account identifiers,
creator endpoints or previous Git objects should be present.

The source adds explicit per-owner identity/URLs, a fresh-instance setup CLI, an optional
local durable snapshot backend, optional storage/consent setup utilities, configuration
templates, and synthetic isolation checks. Core trading and dashboard presentation are reused.

Before pushing, inspect `git status`, `git log --all`, `git remote -v`, and scanner results.
Add a new owner-approved GitHub destination only after review; push this clean repository's
`main` branch. Never change a private operational repository's visibility to publish it.
Do not attach generated installations or private reports to a GitHub release.

The local verification report is maintained outside the public source tree. It should
state the exact tested commit/tree, passing test count, source installation result,
secret-scan findings and their disposition, and remaining external checks. Synthetic
tests are not backtests or brokerage test orders. Docker/hosted OAuth/provider consent
must not be described as verified unless actually exercised in a separately authorized
new-owner environment.

# Security and private state

The repository has no shared service, creator account, or imported runtime state.
Fresh instances have distinct owner/agent credentials and encryption keys. Keep local
private files restricted to the owner and hosting process, using appropriate Windows
ACLs or Unix permissions. Never publish them, including encrypted tokens or token hashes
from an actual installation. `.gitignore` and `.dockerignore` are safeguards, not scanners.

Owner access uses a separate credential or authenticated owner session. Agent tokens
cannot call owner controls. Missing owner identity prevents owner login. Wrong hosts
and cross-origin requests are rejected. Missing configuration/state fails closed.

Run these checks before publication from the repository root:

```text
python -m pytest -q
python tools/scan_publication.py
detect-secrets scan --all-files --no-verify
```

Run the general scanner on a clean source checkout, with no installed environment or
private setup files inside it. Its output is a finding inventory, not a pass/fail exit
code. Review every candidate; never use a broad test-directory exclusion to hide one.
`--no-verify` prevents token validation network calls. Do not send suspected credentials
to a service to test whether they work.

`tools/scan_publication.py` checks tracked files and **all** local Git objects, including
unreachable objects, for explicit secret patterns. A private optional `--denylist PATH`
accepts a JSON list of creator-specific strings for local prepublication review; keep that
list outside this repository. Use a mature general scanner against each historical blob
as well, not just HEAD. Git history, commit metadata, tags and remotes require inspection.

For the initial public release, create a fresh Git repository from the reviewed source
allowlist. Do not copy an existing `.git`, merge private history, or push from a private
operational checkout. If a secret was ever committed, deleting its current file is not
sufficient; revoke exposed credentials and rebuild/review clean history before publication.

If a security defect is found, share a minimal reproduction with the maintainer through
a private channel the maintainer has provided. Never post credentials, account data or
private checkpoint files in a public issue. No private contact address is embedded here.

These scans reduce accidental disclosure risk. They do not prove absence of every
possible secret, establish provider compatibility, or establish trading safety/profitability.

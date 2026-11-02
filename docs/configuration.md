# Configuration

Initialize with `sailboat-instance init`; edit only the generated private `instance.json`.
The launcher replaces inherited service environment variables with that configuration.
Direct `python -m sailboat.cloud_server` consumes your explicitly configured environment.
Neither path supplies a creator account or endpoint. A bare clone fails closed until setup.

| Setting | Purpose / initial behavior |
| --- | --- |
| `SAILBOAT_PUBLIC_URL` | Required owner-selected HTTPS origin, no path or embedded credentials |
| `SAILBOAT_ALLOWED_HOSTS` | Exact comma-separated hostnames, including ports where needed |
| `SAILBOAT_BIND_HOST`, `PORT` | Bind address (loopback by default), port (7860) |
| `SAILBOAT_KEYS_JSON` | SHA256 hashes mapped to agent principals; generated uniquely |
| `SAILBOAT_OWNER_KEY_SHA256` | Separate owner-control credential hash; never an agent key |
| `SAILBOAT_AGENT_KEY` | Independent Fernet key used for agent credential storage and chat OAuth |
| `SAILBOAT_OWNER_SUB` | Verified HF OAuth `sub` for this owner; empty disables owner login |
| `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET` | This deployment's HF OAuth app; configure together with owner subject |
| `SAILBOAT_STORAGE_BACKEND` | `local` (default), `bucket`, or legacy `hub` |
| `SAILBOAT_STATE_DIRECTORY` | Explicit isolated durable directory for local storage |
| `SAILBOAT_STATE_NAMESPACE` | Required owner HF namespace for Bucket storage; no fallback |
| `SAILBOAT_STATE_BUCKET`, `SAILBOAT_STATE_KEY` | Owner's private bucket name and unique checkpoint key |
| `SAILBOAT_STATE_S3_ACCESS_KEY`, `SAILBOAT_STATE_S3_SECRET_KEY` | Scoped credentials for the owner's private bucket |
| `SAILBOAT_STORAGE_MODE` | `delta-v1` by default; existing `snapshot-v1` also supported |
| `SAILBOAT_STATE_REPO`, `HF_TOKEN` | Legacy private Hub model-repository checkpoint backend only |
| `RH_OAUTH_BOOTSTRAP`, `RH_ENCRYPTION_KEY` | Owner-authorized broker OAuth configuration and independent encryption key |
| `SAILBOAT_BROKER_ACCOUNT` | Owner's verified broker account, entered only after consent |
| `SAILBOAT_BROKER_WRITES_DISABLED` | **1** during installation; changing it does not arm |
| `SAILBOAT_CANDIDATE_ENTRY_LIVE` | **0** during installation; candidate execution remains disabled |
| `SAILBOAT_AGENT_PROVIDER` | Unset by default; existing `https_agent` or `claude_subscription` adapters |
| `SAILBOAT_AGENT_URL`, `SAILBOAT_AGENT_TOKEN` | Owner-authorized HTTPS analyst endpoint and optional bearer token |
| `SAILBOAT_AGENT_MODEL` | Existing Claude adapter model selector, default `sonnet` |
| `SAILBOAT_CLAUDE_INITIAL` | Optional encrypted one-time import of this owner's freshly authorized profile; remove after import |
| `SAILBOAT_AGENT_INTERVAL` | Initial generated value 120 seconds for flat and held analysis |
| `SAILBOAT_MARKET_HOURS` | Initial generated value `NYSE_REGULAR` |
| `SAILBOAT_AGENT_SYMBOLS` | Existing default `QQQ,SPY,SMH` |
| `SAILBOAT_AGENT_MAX_CYCLES`, `SAILBOAT_AGENT_NOT_BEFORE`, `SAILBOAT_AGENT_STOP_AT` | Optional existing scheduler bounds |
| `SAILBOAT_AGENT_EVENT_WAKEUPS` | Existing scheduler event behavior (default 1) |
| `SAILBOAT_DAILY_LOSS_LIMIT` | Optional owner-authorized limit; do not set by inference |

Secrets are never arguments to the owner CLI. Read-only status can use a dedicated
owner token file with `sailboat-owner status --url https://YOUR-OWN-HOST --key-file PATH`.
The generated owner credential is stored in JSON; create a separate private plain-text
token file if using `--key-file`, without displaying the token in chat or command logs.

All runtime state is private: encrypted service tokens, account evidence, journals and
receipts. The storage provider must be private even when source code or a dashboard
shell is public. Back up configuration, encryption keys and state separately under owner
control. A missing or corrupt checkpoint is an error; the server never seeds over it.

The legacy Hub snapshot adapter retains its existing automatic history compaction.
Use the Bucket or local backend for new deployments. Do not point the legacy adapter
at source/model repositories or any repository containing unrelated data.

See [the blank environment template](../config/environment.example) for deployment keys.
It is a reference, not a working credential file or a shell script.

# Connect services owned by the installer

No provider account is included. Configure all services only with the current owner's
explicit authorization, keep the host write gate disabled, and never place a test order.

## Owner dashboard

The existing dashboard uses HF OAuth. Register this instance's callback and configure
the exact owner `sub`, client ID and client secret. A username is not the OAuth subject.
Use the owner's own verified HF identity response; do not infer it from a display name.
Owner authentication, chat connection approval and trading authorization are separate.

## Robinhood

The retained adapter targets the official Robinhood trading MCP endpoint. Public
protocol endpoints are service locations, not private creator infrastructure. Access
depends on the provider accepting this owner's application and consent; availability
is not promised by this repository.

After the owner authorizes connecting their account, and their own HTTPS server exists:

```text
python tools/connect_broker.py --config PATH/instance.json
```

This owner-invoked tool registers a new client for this instance's callback and saves
PKCE/state values and a consent link privately. It performs a service registration,
so do not run it during an offline installation check. If registration is denied, stop
and report it; do not extract tokens from a browser or another application.
Restart the controller with the updated configuration (or update the new Space's
secret `RH_OAUTH_BOOTSTRAP`), still DISARMED, then have the owner open the saved consent
link. The existing callback exchanges consent and stores encrypted tokens in this
instance's private durable state. Do not share the consent link publicly.

After successful consent, use authorized read-only account discovery to identify the
owner's account and set `SAILBOAT_BROKER_ACCOUNT`. Restart DISARMED. Keep writes disabled.
Readiness and account display are not trading authorization. The callback's legacy
"simulation only" status wording describes the unarmed initial connection, not proof
that a fully configured armed controller can never execute real orders.

OAuth bootstrap expires after 30 minutes. An interrupted setup requires deliberate
owner recovery; preserve the private config and avoid creating a new identity to bypass
a denial. The setup tool refuses to replace an existing bootstrap automatically.

## Analyst

Leave `SAILBOAT_AGENT_PROVIDER` unset to install without scheduled inference.
The existing `https_agent` adapter posts the controller's request object to an
owner-authorized HTTPS URL and accepts a JSON response. Optional bearer auth uses
`SAILBOAT_AGENT_TOKEN`. Review `sailboat/agent_provider.py`, `sailboat/market_analyst.py`,
and the included analyst prompts for the actual response contract; an ordinary web
chat URL is not a compatible endpoint. Account/market context is sent to this service,
so the owner must approve that data sharing and any service costs.

The existing `claude_subscription` adapter is also retained. Install `.[claude]` when
using it (the default Dockerfile intentionally installs only the base runtime).
It requires an owner-authorized isolated official CLI profile, encrypted under this
instance's `SAILBOAT_AGENT_KEY`, in the private `claude_subscription` state slot.
The expected profile contains `credentials` from that isolated CLI and optional
`config`. Use `tools/import_agent_profile.py` with the server stopped and local storage
before transferring a fresh private instance to its host. Never use another app's
profile or another owner's session. For a hosted instance, use the same tool with
`--output-secret PATH` instead of `--server-stopped`; securely set the resulting encrypted
value as `SAILBOAT_CLAUDE_INITIAL` on that instance. The existing one-time startup import
stores it only if the private slot is empty. Remove that temporary environment secret
after successful durable import. Do not upload it to source or reuse it on another instance.
This is an optional provider setup step, not part
of the default install. Provider sign-in must be performed by the owner; it has not
been verified for a new owner by the source installation tests.

Neither adapter changes owner controls. Selection, candidate eligibility and analyst
advice are subject to existing host gates, owner authorization, limits and broker checks.

## Chat agent

The plugin connects only to your own `/mcp`. Hosts supporting Sailboat's existing chat
OAuth can use its discovery flow, followed by owner approval of that connection.
Otherwise supply the instance's agent bearer token using the host's secret storage.
Never supply `owner_token` to an agent connection. Agent keys cannot call owner controls.

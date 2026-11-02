# Deploy an independent instance

GitHub is the source home. Hosting is an owner choice. Clone and install as described
in instructions.txt; no hosted service is connected by default.

## Self-hosting

Initialize an unused instance directory on the target host. Give it owner-only access,
keep its `state/` on durable local storage, and configure a TLS reverse proxy to port
7860. Supply your HTTPS origin and exact allowed hosts. Preserve the same durable
directory across restarts. Run exactly one server process/replica; do not run multiple
copies against a shared checkpoint. The snapshot writer uses locked compare-and-swap
and fences stale processes. It cannot make a nondurable volume durable.

Register an HF OAuth app for your own origin with redirect URI
`https://YOUR-OWN-HOST/owner/callback`. Configure its client ID/secret and the owner's
verified OAuth subject. The existing owner sign-in uses HF; self-hosted controller and
storage do not require Spaces, but this dashboard login integration still uses HF.
Do not accept the first person who signs in as owner. Verify the provider subject using
the owner's own authorized OAuth identity before allowing dashboard login.
See [HF OAuth](https://huggingface.co/docs/hub/oauth).

Start with `python -m sailboat.instance_setup serve --config PATH/instance.json`.
For a container, build the included Dockerfile from source, mount this instance's
initialized state directory at a private durable container path, set
`SAILBOAT_STATE_DIRECTORY` to that path, and inject the explicit configuration as runtime
environment variables. The container runs as UID 1000; grant that user access to the
volume. Never copy private configuration or state into the build context/image.
Forward only the chosen HTTPS origin; keep `/owner/view` and `/owner/action` authenticated.

## Optional Hugging Face Space

The root README metadata selects Docker and port 7860. The Dockerfile builds from the
same source. Variables and secrets are supplied in the new Space's Settings, at runtime.
See [Docker Spaces](https://huggingface.co/docs/hub/spaces-sdks-docker).

With the owner's authorization, use the installed `hf` CLI skill and a fresh Space ID:

```text
hf repos create OWNER/NEW-SAILBOAT --type space --sdk docker --private
```

Upload only a clean source checkout. Do not upload a working installation directory.
Inspect the CLI upload dry-run/allowlist capabilities for the installed version before
uploading. Keep the Space private for account use unless the owner chooses otherwise.
Public source does not require a public account dashboard or public checkpoint store.

Enable HF sign-in via `hf_oauth: true` (included). Configure the exact
`SAILBOAT_PUBLIC_URL`, allowed hostname, and verified `SAILBOAT_OWNER_SUB`. HF supplies
the OAuth app environment; confirm the registered redirect supports `/owner/callback`.
See [Sign-In with HF](https://huggingface.co/docs/hub/spaces-oauth).

For durable storage, choose a new private Bucket in this owner's namespace. Create it
with the `hf` skill, then provision scoped S3 access credentials. Initialize empty local
state using `sailboat-instance init`. Seed the chosen new key once with:

```text
python tools/seed_bucket.py --config PATH/instance.json
```

First change that private config to `SAILBOAT_STORAGE_BACKEND=bucket` and fill the
namespace, bucket, key, and scoped S3 credentials. The seed tool uses an exclusive
conditional write: it refuses an existing key and does not replace state. It also
verifies that its locally generated seed contains no positions, orders or private
credentials. This is a storage write, to be performed only under the owner's setup
authorization. The existing delta backend accepts this initial snapshot on recovery.
See [Bucket S3 access](https://huggingface.co/docs/hub/storage-buckets-s3).

Copy the config's keys into Space Variables/Secrets as appropriate. Credential values,
hash maps, account identifiers and encryption keys belong in Secrets. Do not pass
secrets through command-line arguments. Leave `SAILBOAT_BROKER_WRITES_DISABLED=1` and
`SAILBOAT_CANDIDATE_ENTRY_LIVE=0`, and leave broker and analyst configuration unset until
separately authorized. The Space binds `0.0.0.0`; local commands bind loopback by default.

For a public demonstration, keep all brokerage and analyst connections absent. The
unconnected dashboard shell displays sign-in and setup state; it is not seeded with
the creator's portfolio or historical results. Do not represent it as a funded account.

## Recovery and limitations

On every restart the existing controller disarms. Restore only this instance's verified
checkpoint and matching keys. Preserve uncertain receipts and journal evidence; never
retry an unknown order with a fresh identity. Do not delete state to clear a failure.

Spaces may sleep and hosts may fail. This deployment recipe does not guarantee timely
orders or closing. Docker build, hosted identity consent, broker registration, and
cloud recovery must be verified in the new owner's environment. Local offline tests
do not establish those external integrations or authorize real orders.

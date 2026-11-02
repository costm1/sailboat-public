# Sailboat plugin source

This directory reuses the plugin manifest, skill and artwork. It has no creator app
binding and no active remote server configuration. `mcp.json.example` uses a reserved,
nonfunctional hostname. Never install it as a live endpoint.

After configuring your own controller, generate the real connection locally:

```text
python -m sailboat.instance_setup plugin --config PATH/instance.json --output plugins/sailboat/mcp.json
```

`mcp.json` is ignored by Git. The command refuses to overwrite an existing file.
It writes your endpoint only, never credentials. Register this source directory in
your host if it supports local Agent Plugins 1.0, or configure its remote MCP connection
to the generated URL and install the included skill using the host's supported flow.
For OAuth hosts, use server discovery and owner connection approval. For bearer hosts,
store only your agent token in the host's credential store. Do not use the owner token.

The portable plugin and a host-specific cloud App registration are different objects.
An App-only host may require its own new registration; never reuse the creator's app ID.
No public plugin-directory submission or host-specific connection has been performed
by preparing this Git repository. Tool discovery should be verified in the owner's
chosen host without submitting an analysis or an order.

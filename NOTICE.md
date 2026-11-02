# Source and dependencies

This distribution reuses the Sailboat controller, dashboard, core contribution
validator, assembly ledger, plugin artwork, and skill source. Its publication changes
are limited to independent configuration, fresh-instance setup, storage/deployment
support, documentation, and isolation tests. The trading policy has not been redesigned.
Private operational records and previous Git history are excluded.

MIT is the proposed publication license for project-owned source. The publishing
owner must confirm they have rights to release that source and artwork under LICENSE.
No claim of ownership is made over external services, trademarks, model weights,
protocol specifications, or third-party packages. Python dependencies are installed
separately and retain their own licenses. The optional Claude runtime is not vendored.
The broker contract JSON describes interface schemas; it contains no account responses.
Service access is subject to the provider's own authorization and terms.

The source's grammar adapter identifies the external lm-format-enforcer integration;
that optional research dependency is not vendored or required by the deployment path.
No model weights, datasets, proprietary service credentials or recorded assessments
are included. A repository license does not grant brokerage or subscription access.

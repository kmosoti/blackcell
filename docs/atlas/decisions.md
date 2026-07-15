---
node: atlas/decisions
kind: decision-log
edges:
  records:
    - targets/containers
    - adr/0004-evolutionary-runtime-architecture
    - adr/0005-durable-run-and-execution-protocol
    - adr/0006-versioned-run-feedback-protocol
    - adr/0007-runtime-security-boundary
---

# Decisions

- Keep the Python package/import name as `blackcell`.
- Treat runtime integrations as adapters, not the product identity.
- Retire source-owned generated OpenCode agent packs; developer-tool configuration is separate
  from Blackcell runtime and gateway configuration.
- Keep credentials and provider auth out of repo files and container images.
- Use Cyclopts for the CLI surface.
- Use a modular monolith with inward dependencies, vertical feature slices, and one event-driven
  kernel before considering distributed services.
- Route model capabilities through a gateway; models propose while Blackcell authorizes and acts.
- Treat durable multi-agent DAG orchestration as a ledger-backed workflow consumer, not a second
  runtime or authority boundary.
- Record runs artifact-first in one causal kernel stream, and durably prepare an affordance before
  calling its adapter. Recover abandoned preparations explicitly through reconciliation.
- Preserve the version-one run grammar and add developer-owned evaluation criteria, pre/post state
  snapshots, gateway evidence, independent outcome evidence, evaluation, and observed transitions
  through a version-two workflow contract.
- Require an explicit owner-only service data root, one opaque environment-or-file API credential,
  strict Bearer and scope checks, zero proxy trust, and pre-storage secret redaction before HTTP
  exposure.

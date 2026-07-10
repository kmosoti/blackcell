---
node: targets/containers
kind: target
edges:
  supports:
    - targets/opencode
    - concepts/runtime-adapters
---

# Containers

> The container now supports the Blackcell Python runtime only. Node, NVM, and OpenCode were
> removed because model execution is an optional host adapter rather than part of the core
> image.

BlackCell includes a git-tracked development container built from
`ghcr.io/astral-sh/uv:python3.14-trixie-slim`.

The image installs Python tooling through `uv` plus the small set of system utilities needed
for repository observation and development.

Provider authentication and coding-agent binaries remain host-local and are not mounted into
the core development container by default.

Container files:

- `Containerfile`
- `compose.yaml`
- `.dockerignore`
- `.devcontainer/devcontainer.json`

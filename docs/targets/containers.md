---
node: targets/containers
kind: target
edges:
  supports:
    - targets/opencode
    - concepts/runtime-adapters
---

# Containers

BlackCell includes a git-tracked development container built from
`ghcr.io/astral-sh/uv:python3.14-trixie-slim`.

The image installs Python tooling through `uv`, system build dependencies, `nvm`,
Node, `npm`, and optionally OpenCode through `npm install -g opencode-ai`.

Auth is not part of the image. Run provider login from your own persisted local
config, for example:

```bash
opencode providers login --provider openai
```

Container files:

- `Containerfile`
- `compose.yaml`
- `.dockerignore`
- `.devcontainer/devcontainer.json`
- `.nvmrc`

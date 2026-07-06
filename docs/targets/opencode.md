---
node: targets/opencode
kind: target
edges:
  renders:
    - concepts/custom-agents
  runs-through:
    - concepts/runtime-adapters
  containerized-by:
    - targets/containers
---

# OpenCode Target

OpenCode is the preferred local target for the BlackCell agent pack.

## Scopes

- `project`: `.opencode/agents/*.md` and `.opencode/commands/*.md`
- `global`: `~/.config/opencode/agents/*.md` and `~/.config/opencode/commands/*.md`

Project scope is intended for repo-local, git-tracked configuration. Global
scope is explicit and user-local.

## Commands

```bash
uv run blackcell agents render --target opencode --scope project
uv run blackcell agents install --target opencode --scope project --apply
uv run blackcell agents doctor --target opencode --scope project
```

OpenCode provider auth remains local to the user. Do not commit credentials and
do not bake them into the container image.

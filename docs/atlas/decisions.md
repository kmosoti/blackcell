---
node: atlas/decisions
kind: decision-log
edges:
  records:
    - targets/opencode
    - targets/containers
    - concepts/custom-agents
---

# Decisions

- Keep the Python package/import name as `blackcell`.
- Treat runtime integrations as adapters, not the product identity.
- Prefer OpenCode for generated agent packs while keeping Codex CLI as optional legacy/adapter context.
- Use `project` scope for git-tracked `.opencode` artifacts by default.
- Keep `global` scope explicit and user-local under `~/.config/opencode`.
- Keep credentials and provider auth out of repo files and container images.
- Use Cyclopts for the CLI surface.

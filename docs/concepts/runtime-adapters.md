---
node: concepts/runtime-adapters
kind: concept
edges:
  used-by:
    - concepts/harness
  targets:
    - targets/opencode
    - targets/codex-cli
    - targets/containers
---

# Runtime Adapters

BlackCell treats runtimes as adapters behind a stable harness interface.

## Principles

- The runtime is not the product.
- The harness should survive runtime churn.
- Adapters should advertise availability and capability clearly.
- Traces should normalize runtime output into a common event shape.

## Current Adapters

- `dry-run`: always available, no external dispatch
- `opencode`: preferred local OpenCode adapter when installed
- `codex`: optional local Codex CLI adapter when installed

## Future Adapters

- local shell task runners
- MCP-backed execution surfaces
- remote agent orchestration backends
- evaluation-only replay runtimes

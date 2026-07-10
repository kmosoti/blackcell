---
node: concepts/custom-agents
kind: concept
edges:
  rendered-to:
    - targets/opencode
  planned-by:
    - concepts/harness
  refined-by:
    - concepts/agent-operating-model
---

# Custom Agents

> **Optional legacy adapter:** the OpenCode agent pack is not part of the Phase 1 kernel or
> research intervention. The model/execution boundary is defined in
> `../adr/0003-model-execution-boundary.md`.

BlackCell ships a small cosmic/organic agent pack that can be rendered into
runtime-specific configuration.

## First Pack

| Agent | Role |
| --- | --- |
| `blackcell-astrophage` | Primary planner that turns world state into constrained work packets. |
| `blackcell-mycelium` | Documentation graph curator. |
| `blackcell-spore` | Read-only repository observer and typed fact extractor. |
| `blackcell-lumen` | NeSy and contract constraint reviewer. |
| `blackcell-umbra` | Quality and security reviewer. |
| `blackcell-chimera` | Guarded write-capable executor for explicit implementation tasks. |

Generated artifacts are managed with BlackCell digest markers. Installs default
to dry-run; writes require `--apply`.

The generated prompts follow the shared
[`agent-operating-model`](agent-operating-model.md): role-specific workflows,
evidence rules, constraint rules, handoff protocols, output formats, stop
conditions, and failure handling.

```bash
uv run blackcell agents list
uv run blackcell agents install --target opencode --scope project
uv run blackcell agents install --target opencode --scope project --apply
uv run blackcell agents check-drift --target opencode --scope project
```

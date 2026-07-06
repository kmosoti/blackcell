---
node: atlas/graph
kind: atlas
edges:
  maps:
    - concepts/world-model
    - concepts/nesy
    - concepts/harness
    - concepts/runtime-adapters
    - concepts/custom-agents
    - targets/opencode
    - targets/codex-cli
    - targets/containers
---

# Documentation Graph

```mermaid
graph TD
    Repo[Repository] --> World[World Model]
    World --> Facts[Typed Facts]
    Facts --> NeSy[NeSy Rules]
    NeSy --> Harness[Harness]
    Harness --> Runtime[Runtime Adapters]
    Runtime --> Trace[Run Traces]
    Trace --> World
    Harness --> Agents[Custom Agents]
    Agents --> OpenCode[OpenCode Target]
    Agents --> Codex[Codex CLI Target]
    Runtime --> Container[Container Runtime]
```

## Node Families

- `atlas`: maps and vocabulary for navigating the graph
- `concepts`: stable project ideas and internal seams
- `targets`: generated or runtime-specific integration surfaces
- `research`: reference notes that inform future design
- `legacy`: preserved surfaces from the earlier GitHub control-plane direction

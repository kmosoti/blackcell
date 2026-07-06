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
    - concepts/agent-operating-model
    - guides/latent-harness-quickstart
    - targets/opencode
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
    Trace --> Latent[Latent Prediction]
    World --> Latent
    NeSy --> Latent
    Latent --> Surprise[Surprise / Revision]
    Surprise --> World
    Harness --> LatentGuide[Latent Harness Quickstart]
    LatentGuide --> Latent
    Harness --> Agents[Custom Agents]
    Agents --> AgentModel[Agent Operating Model]
    Agents --> OpenCode[OpenCode Target]
    Runtime --> Container[Container Runtime]
```

## Node Families

- `atlas`: maps and vocabulary for navigating the graph
- `concepts`: stable project ideas and internal seams
- `guides`: user-facing workflows over stable concepts and specs
- `targets`: generated or runtime-specific integration surfaces
- `research`: reference notes that inform future design
- `spec`: durable implementation plans and BCP-sized roadmap nodes

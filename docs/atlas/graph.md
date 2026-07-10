---
node: atlas/graph
kind: atlas
edges:
  maps:
    - charter
    - architecture
    - scientific-basis
    - evaluation-methodology
    - spec/index
---

# Documentation Graph

```mermaid
flowchart TD
    Charter[Charter] --> Architecture[Runtime Architecture]
    Science[Scientific Basis] --> Architecture
    Architecture --> Specs[BCP-0028 through BCP-0033]
    Architecture --> Evaluation[Evaluation Methodology]
    Specs --> Operator[Repository Operator]
    Evaluation --> Bench[OperatorBench]
    Operator --> Bench
```

## Node Families

- `charter`: product identity, scope, claim gates, and acceptance criteria
- `architecture`: stable event, state, context, policy, execution, and replay boundaries
- `research-contract`: terminology discipline and promotion rules
- `evaluation-contract`: scenarios, baselines, metrics, and trial protocol
- `adr`: accepted trade-offs and their consequences
- `spec`: bounded implementation slices
- `concepts`, `guides`, `targets`, and `research`: retained prototype history

---
node: atlas/glossary
kind: atlas
edges:
  defines:
    - charter
    - architecture
    - scientific-basis
---

# Glossary

- `observation event`: immutable evidence occurrence received from a source
- `claim`: typed assertion with provenance, epistemic status, time, freshness, and conflicts
- `operational state estimate`: rebuildable domain projection of claims at an event sequence
- `signal packet`: correlated, time-windowed derivative of observations; not another state store
- `ContextFrame`: immutable task projection with selections, omissions, constraints, and affordances
- `action proposal`: typed model suggestion with expected effects; it has no execution authority
- `policy decision`: allow, deny, or require-approval result with structured violations
- `affordance`: declared, bounded action implemented by Blackcell
- `execution lineage`: correlated history of context, proposal, policy, action, and outcome
- `historical replay`: deterministic recomputation using recorded model and tool results
- `counterfactual rerun`: a new experiment applying current components to historical input
- `transition model`: domain-scoped predictor of action-conditioned outcomes

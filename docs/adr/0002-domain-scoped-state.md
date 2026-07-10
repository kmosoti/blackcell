---
node: adr/0002-domain-scoped-state
kind: adr
edges:
  decides:
    - architecture
---

# ADR 0002: Scope State and Transition Models by Domain

Status: accepted

## Decision

Keep a common event envelope and artifact store, but define state projectors, action spaces,
transition models, horizons, and objectives within bounded domains.

## Rationale

Personal planning, repository workflow, and service telemetry do not share a coherent hidden
state or dynamics model. A universal `BeliefState` would hide incompatible semantics and make
scientific claims unfalsifiable.

## Consequences

ContextFrames may compose claims across domains, while prediction and policy remain explicit
about which domain contract and version they use.

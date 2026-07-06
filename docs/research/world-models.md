---
node: research/world-models
kind: research
edges:
  informs:
    - concepts/world-model
    - concepts/nesy
---

# Research Notes

## World Models

The design direction here is influenced by the recent world-model framing around
predictive latent state rather than surface reconstruction.

- I-JEPA argues for predicting semantic representations from context rather than
  reconstructing pixels.
- V-JEPA extends feature prediction into video and is useful as a mental model
  for "predict useful latent state, not raw output".

The software analogy is:

- observations are not the world model
- prompts are not memory
- traces are not enough unless they update belief state

## Contemporary NeSy References

- Scallop: differentiable Datalog with provenance semirings
- DeepProbLog: neural predicates inside probabilistic logic programs
- Logic Tensor Networks: differentiable first-order constraints
- NeurASP: symbolic constraints over neural distributions with ASP

## Why The First Slice Is Lightweight

The first branch focuses on representation and interfaces:

- typed facts
- explicit rules
- planner seams
- runtime adapters
- run traces

That keeps BlackCell flexible while the research direction solidifies.

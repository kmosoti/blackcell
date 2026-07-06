---
node: concepts/nesy
kind: concept
edges:
  consumes:
    - concepts/world-model
  constrains:
    - concepts/harness
  researched-by:
    - research/nesy-implementations
---

# NeSy Layer

The NeSy layer is a lightweight constraint scaffold in the first overhaul slice.

It does not yet attempt differentiable reasoning or training. Instead it gives
BlackCell a place to hold explicit symbolic rules that can be validated and used
for planning.

## Current Contract

- `RuleAtom`: `subject`, `predicate`, `object`
- `Rule`: keyed implication with a head, body, and rationale
- `RuleSet`: collection of rules with validation

## Why This Shape

This keeps the repo open to several future directions:

- Scallop-style differentiable Datalog
- DeepProbLog-style neural predicates
- Logic Tensor Network style differentiable first-order constraints
- NeurASP style symbolic constraints over probabilistic neural outputs

The project is not choosing one of these yet. It is creating the seam where one
or more can later be attached.

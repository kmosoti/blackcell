---
node: concepts/agent-operating-model
kind: concept
edges:
  refines:
    - concepts/custom-agents
  consumes:
    - concepts/world-model
    - concepts/nesy
    - concepts/harness
  informed-by:
    - research/world-models
---

# Agent Operating Model

BlackCell agents use a shared evidence-first protocol so generated runtime files
are more than persona blurbs.

## Shared Loop

1. Observe repository evidence and typed world facts.
2. Predict expected structure, constraints, or behavior from project state.
3. Compare expectation to evidence and report surprises.
4. Apply explicit NeSy constraints before planning or reviewing.
5. Hand off the smallest useful context to the next specialist agent.

## Required Prompt Sections

Every generated agent prompt carries these sections:

- Role
- Operating Model
- Inputs
- Workflow
- Evidence Rules
- Constraint Rules
- Handoff Protocol
- Output Format
- Stop Conditions
- Failure Handling

Commands carry matching workflow, evidence, output, verification, risk, and stop
condition sections.

## Research Mapping

- JEPA-style feature prediction informs the expectation/surprise loop.
- Neural-symbolic AI informs the separation between observations, beliefs, and
  explicit rules.
- DeepProbLog-style provenance and confidence inform typed fact reporting.
- awesome-copilot agent examples inform phase workflows, DAG planning, quality
  gates, and clear agent boundaries.

These are design inspirations, not claims that BlackCell implements those full
research systems today.

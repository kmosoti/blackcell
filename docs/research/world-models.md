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
- V-JEPA 2 strengthens the analogy with encoder/predictor structure,
  action-conditioned prediction, and planning over predicted embeddings.
- AMI/world-model framing treats future prediction in abstract representation
  space as the substrate for planning through possible action sequences.
- Graph-JEPA, time-series JEPA, and related systems show that latent prediction
  ideas are not limited to images or video. They remain research inspiration for
  BlackCell rather than V0 dependencies.

The software analogy is:

- observations are not the world model
- prompts are not memory
- traces are not enough unless they update belief state

## Contemporary NeSy References

- Scallop: differentiable Datalog with provenance semirings
- DeepProbLog: neural predicates inside probabilistic logic programs
- Logic Tensor Networks: differentiable first-order constraints
- NeurASP: symbolic constraints over neural distributions with ASP

## Agent Customization References

- awesome-copilot `gem-orchestrator`: phase-based orchestration, complexity
  classification, delegation, and failure handling.
- awesome-copilot `gem-planner`: DAG plans, wave scheduling, context envelopes,
  and anti-overplanning rules.
- awesome-copilot `quality-playbook`: phase-separated review, checkpoints, and
  iteration strategies.
- awesome-copilot `custom-agent-foundry`: role, boundaries, tool posture,
  handoffs, output formats, and examples as first-class agent design elements.

## Why The First Slice Is Lightweight

The first branch focuses on representation and interfaces:

- typed facts
- explicit rules
- planner seams
- runtime adapters
- run traces
- deterministic latent capsules
- transition-memory prediction
- self-supervision samples for later learned baselines

That keeps BlackCell flexible while the research direction solidifies.

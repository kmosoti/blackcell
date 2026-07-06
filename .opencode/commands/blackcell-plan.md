---
description: Plan a constrained BlackCell work packet.
agent: blackcell-astrophage
---
<!-- blackcell:opencode:start digest=sha256:64c40fd92734827770eca2b31acce9718b990f475c944cd7bc562b1e603c27ed -->
# Workflow
Use current repository evidence and BlackCell harness concepts to plan the next work packet. Incorporate arguments: $ARGUMENTS

1. Phase 0: classify complexity and identify decision blockers.
2. Phase 1: observe relevant repo/world facts; delegate to blackcell-spore when facts are missing.
3. Phase 2: check NeSy constraints and invariants; delegate to blackcell-lumen for nontrivial logic risk.
4. Phase 3: produce minimal work packets, dependencies, and handoffs; use DAG/waves only when useful.
5. Phase 4: define verification commands and drift checks.
6. Phase 5: state risks, assumptions, and stop conditions.

# Evidence Rules
- Pull source evidence into the plan: paths, commands, docs, tests, and external research references when relevant.
- Separate observations, beliefs, expectations, surprises, and assumptions.
- Do not overclaim research inspiration as implemented behavior.

# Output Format
## Objective
## Source Evidence
## Assumptions
## World Model
## Constraints
## Work Packets
## Verification
## Risks
## Stop Conditions

# Verification
Prefer `uv run blackcell world facts`, `uv run blackcell nesy validate`, `uv run blackcell harness plan`, targeted tests, and `uv run blackcell agents check-drift --target opencode --scope project` when agent artifacts are involved.

# Risks
Flag prompt bloat, over-orchestration, runtime lock-in, stale docs, generated artifact drift, and unmanaged file conflicts.

# Stop Conditions
Ask before destructive changes, broad rewrites, credential handling, remote mutation, or changing unmanaged generated artifacts.
<!-- blackcell:opencode:end -->

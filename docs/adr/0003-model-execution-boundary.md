---
node: adr/0003-model-execution-boundary
kind: adr
edges:
  decides:
    - architecture
---

# ADR 0003: Models Propose; Blackcell Governs and Executes

Status: accepted

## Decision

The model receives an immutable ContextFrame and returns a typed `ActionProposal`. It does not
receive direct tool authority. Blackcell evaluates policy, requests approval when necessary,
executes a declared affordance, observes the result, and records the outcome.

## Rationale

Prompt instructions cannot provide deterministic authorization. Separating proposal from
execution also permits recorded-model replay, model comparison, symbolic ablation, and safe
local subscription-backed adapters.

## Consequences

Affordance schemas and policy decisions become versioned runtime artifacts. Live coding-agent
or multi-agent orchestration remains an optional adapter, not the kernel.

## References

- [Codex non-interactive mode](https://developers.openai.com/codex/noninteractive)
- [Codex CLI reference](https://developers.openai.com/codex/cli/reference)

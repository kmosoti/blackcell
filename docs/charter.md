---
node: charter
kind: charter
edges:
  informs:
    - architecture
    - scientific-basis
    - evaluation-methodology
---

# BlackCell charter

## Canonical definition

BlackCell is a CLI-first, project-scoped software execution framework with durable review and
verification.

It turns project intent and repository evidence into a typed dependency plan, bounded execution,
independent review, verified outcomes, and live-free replay. One daemon owns authoritative state and
effects. Models are replaceable proposal mechanisms; they are not the state store, policy engine,
executor, reviewer of record, verifier, or source of truth.

## Product thesis

For a fixed model and context budget, explicit intent, evidence provenance, dependency-safe plans,
host-owned policy, and separate assurance stages should improve correctness and inspectability over
an unconstrained model loop. A closed epistemic matrix should make unsupported certainty,
counterevidence omission, causal overreach, and acceptance gaps visible before human acceptance.

Those are testable hypotheses, not assumed properties of the architecture.

## Runtime responsibilities

BlackCell owns:

- immutable event occurrence and content-addressed artifact history;
- explicit project, intent, plan, run, and execution identity;
- provenance, conflict, correction, unknown, and omission preservation;
- typed provider admission, policy, effects, paths, budgets, and acceptance commands;
- fenced work claims, isolated worktrees, bounded processes, cleanup, and reconciliation;
- independent review findings tied to admitted evidence;
- deterministic verification matrices and live-free replay;
- local service security, quotas, telemetry redaction, backup, restore, and recovery evidence.

## Authority invariants

1. A provider can propose content but cannot expand its own effects or acceptance criteria.
2. A client can request work but cannot schedule it or write runtime storage directly.
3. An execution worker cannot approve its own output.
4. A reviewer cannot execute tools or alter the accepted plan.
5. A verifier cannot repair its own findings or convert missing evidence into success.
6. Replay cannot invoke a live provider, rerun a check, or mutate a repository.
7. Human acceptance cannot be inferred from model confidence or a passing automated row.

## Epistemic policy

Review must cover acceptance coverage, evidence grounding, counterevidence, causal overreach, scope
challenge, and uncertainty. Every finding cites admitted evidence. Verification creates an explicit
row for every acceptance criterion and epistemic dimension:

- `pass` requires sufficient deterministic evidence;
- `fail` records contradictory evidence or an admitted concern;
- `unknown` remains unknown and makes the outcome inconclusive;
- `not-applicable` requires a bounded reason.

The matrix guards against known evaluator weaknesses; it does not make the evaluator infallible.
Tests and host-authored criteria can also be wrong, so counterevidence and unresolved questions stay
visible to the human decision maker.

## Naming and evolution

Executable architecture is named by capability: project, intent, plan, run, execution, review,
verification, and replay. Internal paths and symbols do not encode maturity or speculative product
generations. Revisions remain explicit only where a public protocol, persisted schema, package,
dependency, or isolated third-party adapter requires exact discrimination.

Iteration history belongs in version control, decisions, experiments, discussions, and release
metadata. It must not create parallel internal architectures or compatibility entry points.

## Acceptance

A change is accepted only when:

- its authority and failure boundaries are explicit;
- affected deterministic tests pass;
- architecture fitness still passes;
- the full maintained suite and coverage threshold pass for cross-cutting work;
- current operational documentation matches the executable surface;
- no test, contract, or expected result was weakened to manufacture a pass;
- any uncertainty or environmental limitation is reported rather than silently inferred away.

The active machine-readable capability and gate map is [`../blackcell.plan.yaml`](../blackcell.plan.yaml).

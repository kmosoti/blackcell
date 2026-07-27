---
node: scope
kind: scope
edges:
  governed-by:
    - adr/0009-project-runtime-scope
  constrains:
    - charter
    - architecture
    - evaluation-methodology
---

# BlackCell scope

## Product definition

BlackCell is a CLI-first, project-scoped software execution framework. It turns one bounded request
into explicit intent, a dependency-safe plan, isolated execution, independent review, deterministic
verification, and replayable records.

One foreground daemon owns project state, scheduling, persistence, policy, provider dispatch,
recovery, and the ordered event stream. The JSON-first CLI is the complete automation surface. The
terminal and browser interfaces are projections over the same service, not additional runtimes.

[`../blackcell.plan.yaml`](../blackcell.plan.yaml) is the active capability and verification map.

## Capability contract

The runtime has explicit authority-bearing stages:

1. **Project** binds a canonical repository root and project-configuration identity.
2. **Intent** records the outcome, constraints, assumptions, and unresolved questions.
3. **Plan** defines an acyclic graph, budgets, effects, allowed paths, and acceptance commands.
4. **Run** durably queues work and owns status and cancellation.
5. **Execution** operates only inside the admitted worktree, provider, and command boundaries.
6. **Review** searches for correctness, regression, security, policy, and epistemic defects.
7. **Verification** maps every declared criterion and epistemic dimension to deterministic evidence.
8. **Replay** reconstructs accepted state without invoking a provider or repeating an effect.

Models propose and synthesize inside this graph. They never become the state store, policy engine,
executor, approver, verifier, or source of truth.

## Service boundary

The public HTTP contract exposes project registration, intent and plan acceptance, asynchronous run
submission and query, cancellation, status, ordered events, replay, and browser support beneath
`/api/v1`. The revision token belongs to that public protocol boundary. Internal packages,
processes, symbols, tests, and workflows use semantic capability names.

Every accepted plan binds one base commit, explicit budgets and effects, repository-relative path
authority, and host-owned direct-argv acceptance commands. Repository writers must be ordered by
dependencies. Unknown fields, cycles, path escape, undeclared effects, and ambiguous writers fail
before work is queued.

Execution, review, and verification workers are separately configured and separately identified.
With no configuration the daemon is API-only. It never guesses a provider or worker authority from
the environment.

## Persistence boundary

The SQLite event ledger and content-addressed artifact store are the durable authority. Event
occurrence, stream sequence, idempotency, correlation, causation, recorded time, effective time,
actor, source, payload, and payload digest remain distinct.

Replay verifies every event and artifact binding and performs no live call. If an existing database
does not match the kernel's persisted schema, startup fails before opening a write-capable
connection. Migration, deletion, and recovery are explicit operator decisions.

## Project configuration boundary

Kernform remains behind a pinned argv-only JSON adapter. BlackCell validates the installed package
and wire contract, bounds process duration and output, and never imports a sibling checkout or
Kernform implementation internals. Package and wire revisions are explicit external boundaries,
not names for BlackCell's internal architecture.

## Assurance boundary

Review uses a closed matrix for acceptance coverage, evidence grounding, counterevidence, causal
overreach, scope challenge, and uncertainty. Findings cite admitted evidence. Verification maps
each row to deterministic evidence; a concern fails, missing evidence is inconclusive, and
not-applicable requires a reason. Human acceptance remains separate.

These mechanisms reduce known failure modes. They do not prove model independence, causal
understanding, hostile-code containment, correctness of the acceptance contract, or absence of
unknown failure modes.

## Non-goals

The current scope does not authorize:

- distributed scheduling or multi-host execution;
- online self-modification or self-publishing;
- hidden model tool authority or client-owned persistence;
- automatic schema migration or destructive state cleanup;
- a greenfield language rewrite;
- claims of calibrated world modeling or causal reasoning without held-out evidence;
- release, deployment, or publication as a side effect of execution.

## Promotion rule

A new capability enters the executable graph only with a typed contract, explicit authority,
failure and recovery behavior, durable evidence, focused tests, an applicable repository-wide gate,
and current operational documentation. Names describe what the capability does; iteration history
belongs in commits, decisions, discussions, and external protocol revisions.

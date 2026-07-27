---
node: guides/execution
kind: guide
edges:
  governed-by:
    - charter
    - scope
    - architecture
    - adr/0009-project-runtime-scope
  complements:
    - guides/runtime-quickstart
    - guides/execution-worker-configuration
    - guides/review-configuration
    - guides/verification-configuration
---

# Execution model

BlackCell turns an accepted project intent into a host-authoritative execution plan. A provider may
propose task text, dependencies, path scopes, and check identifiers from the admitted catalog. It
cannot replace host-owned acceptance commands, expand effects, widen repository paths, or approve
its own result.

## Capability graph

```text
project -> intent -> plan -> run
                           |
                           v
                       execution -> review -> verification
                           |
                           +------------------------> replay
```

The event ledger and content-addressed artifact store are the durable authority. The runtime
service owns project, intent, plan, and run admission. The execution worker owns only fenced node
claims and the worktree/provider/check ports supplied at composition. Review and verification use
separate streams, identities, and authority.

## Plan admission

An accepted plan must:

- bind one project, intent, base commit, and immutable node graph;
- be acyclic and have a deterministic topological order;
- declare budgets, allowed effects, allowed paths, and host-owned acceptance checks per node;
- serialize repository writers through dependency edges;
- reject unknown fields, undeclared effects, path escape, and ambiguous parallel writers;
- retain unresolved questions and unknown evidence instead of inventing certainty.

Generated planning remains proposal-only. The host validates the closed draft and records the
admitted plan before work can be selected.

## Execution lifecycle

The execution worker selects one dependency-ready node, records a fenced lease, prepares an
isolated Git worktree, records provider dispatch before making an external call, applies only
admitted text changes, and runs only the declared acceptance commands through the isolation port.
Every transition is append-only and idempotency checked.

If a process stops after provider dispatch and before a durable result, the run requires explicit
reconciliation. BlackCell cannot infer whether an external provider accepted the request. Cleanup
intent and cleanup evidence are also durable; a failed cleanup remains visible rather than being
silently retried.

## Review and epistemic guard

Independent review receives bounded execution evidence and a closed matrix with these dimensions:

- acceptance coverage;
- evidence grounding;
- counterevidence;
- causal overreach;
- scope challenge;
- uncertainty.

Every reported finding must cite admitted evidence. Verification maps acceptance criteria and
epistemic rows to deterministic evidence and preserves `unknown` as `inconclusive`; model confidence
or repeated prose never becomes proof. See [Epistemic evaluation](../epistemic-evaluation.md).

## Replay and persistence

Replay folds the exact event sequence and verifies every referenced artifact digest. It never calls
a model, executes an acceptance command, or mutates a repository. A database whose persisted schema
does not match the current kernel is rejected before a write-capable connection is opened. There is
no automatic migration, compatibility import, or destructive cleanup path.

## Verification commands

During development, run exact affected test nodes through the repository wrapper. The maintained
CI gate adds architecture fitness, Ruff, type checking, the full test suite, and coverage:

```bash
uv run python tools/run_pytest.py \
  tests/architecture/test_dependencies.py::test_executable_names_are_maturity_and_generation_agnostic \
  tests/architecture/test_dependencies.py::test_retired_runtime_modules_are_absent \
  -q --blackcell-require-all-pass
```

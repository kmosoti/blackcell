---
node: spec/bcp-0034-evolutionary-runtime
kind: bcp
edges:
  depends-on:
    - spec/bcp-0033-operator-bench
    - adr/0004-evolutionary-runtime-architecture
  governed-by:
    - implementation-baseline
    - migration-ledger
---

# BCP-0034: Evolutionary Agentic Runtime

Status: active — WP00-WP03 complete; WP04a state projection implemented

## Outcome

Evolve the working Repository Operator into a stateful, observable agentic runtime with one event
kernel, vertical feature slices, a capability-based model gateway, durable DAG orchestration,
advisory transition prediction, symbolic constraint solving, replayable evaluation, an operator
API, and a rootless Podman deployment.

## Target source tree

```text
src/blackcell/
├── kernel/                 # identity, envelopes, provenance, time, transactions
├── features/
│   ├── ingest_observation/
│   ├── project_operational_state/
│   ├── derive_signal_packet/
│   ├── retrieve_evidence/
│   ├── build_context/
│   ├── predict_transition/
│   ├── solve_constraints/
│   ├── authorize_action/
│   ├── execute_affordance/
│   ├── evaluate_outcome/
│   └── replay_run/
├── workflows/
│   └── daily_operator.py
├── gateway/                # model capabilities, routing, profiles, budgets, audit
├── orchestration/          # DAG contracts, scheduler, leases, fencing, roles
├── adapters/
│   ├── persistence/sqlite/
│   ├── retrieval/fts5/
│   ├── models/
│   │   ├── recorded/
│   │   ├── llama_cpp/
│   │   └── remote/
│   ├── reasoning/clingo/
│   ├── execution/local_process/
│   └── telemetry/otel/
├── interfaces/
│   ├── http/contracts/
│   ├── http/v1/
│   └── cli/
├── compatibility/          # temporary facades for old public paths
└── bootstrap/              # CLI, worker, HTTP composition roots
```

Each feature may start with `command.py`, `handler.py`, `events.py`, `projection.py`, and
`ports.py`, but it creates only the files its behavior needs. The tree is a boundary guide, not a
mandate for empty modules.

## Model gateway

All reasoning, coding, structured generation, and embedding requests pass through one gateway.
Agents request capabilities and constraints rather than importing provider clients or naming a
provider in domain code.

A gateway request contains:

- capability: `reason`, `code`, `review`, `verify`, or `embed`;
- typed input and required output schema;
- context and data-classification labels;
- latency, cost, token, and locality budgets;
- determinism and tool-use policy;
- correlation, causation, run, and node identifiers.

Routing profiles are configuration owned by Blackcell. A profile can choose a recorded model,
lightweight local model, llama.cpp server, subscription-backed command adapter, or remote API.
Model names—including any future `5.6 Terra` mapping—remain deployment configuration, not source
architecture. Requests and responses are immutable artifacts; the gateway emits correlated usage,
latency, routing, retry, and error events.

The gateway cannot grant affordances. Its typed output is a proposal consumed by policy and
constraint slices.

## Multi-agent DAG

The orchestration subsystem executes a typed directed acyclic graph whose nodes call workflows or
feature ports. The initial roles are:

| Role | Primary capability | Required independence |
| --- | --- | --- |
| planner | decompose goals and define acceptance evidence | cannot execute actions |
| executor | produce a bounded proposal or implementation artifact | cannot approve itself |
| reviewer | inspect correctness, design, and safety | receives evidence, not hidden executor state |
| verifier | run deterministic checks and compare acceptance criteria | deterministic checks precede model judgment |
| synthesizer | reconcile accepted outputs and unresolved uncertainty | cannot override a symbolic denial |

Nodes declare typed inputs, outputs, retry policy, timeout, budget, side-effect class, and required
approval. The durable scheduler records node readiness, attempts, leases, fencing tokens, results,
and terminal state. A worker must hold the current lease and fencing token before committing a node
result. At-least-once delivery is expected; handlers must be idempotent or reconcile uncertain
outcomes.

## Predictive and neural-symbolic realism

Blackcell does not claim a learned world model in the initial runtime. `predict_transition` starts
with deterministic and simulation adapters, then permits local model proposals behind the gateway.
Predictions carry horizon, confidence, assumptions, provenance, and model version. They are scored
against later observations and never become facts automatically.

`solve_constraints` starts with deterministic Python policy and can add Clingo through a port.
Neural interpretation may propose facts or plans, but symbolic checks consume typed facts and
return proof or violation artifacts. A denied constraint cannot be bypassed by model confidence.

Retrieval begins with SQLite FTS5 and provenance-preserving ranking. LightRAG or another graph/RAG
adapter may be evaluated only against the same retrieval port, scenarios, and context budget. It
does not own the operational belief state.

## API and deployment

Litestar owns HTTP transport and msgspec owns wire contracts. Granian serves the ASGI application.
Transport types do not enter feature packages. The initial API exposes health/readiness, observation
ingest, run submission and inspection, context inspection, approvals, events, replay, and evaluation.

The OCI image is Podman-compatible, runs as a non-root user, uses an explicit data volume, exposes
health checks, supports read-only root filesystems, and keeps provider credentials out of layers and
configuration committed to Git. The same image runs API and worker entry points.

## Work packages

| WP | Deliverable | Acceptance evidence |
| --- | --- | --- |
| 00 | measured baseline and migration ledger | baseline suite and remote branch |
| 01 | architecture ratification | ADR, target contracts, docs graph |
| 02 | dependency enforcement | AST/import tests and shrinking debt manifest |
| 03 | event kernel consolidation | transactional batch append, idempotency, replay tests |
| 04 | observation and state slices | characterized parity with repository projection |
| 05 | signal, retrieval, and context slices | provenance and context-budget tests |
| 06 | model gateway | capability routing, budgets, audit, recorded adapter |
| 07 | constraint and authorization slices | deterministic denial and proof artifacts |
| 08 | affordance execution slice | typed authority, approval, reconciliation tests |
| 09 | Daily Operator workflow and facades | old CLI behavior delegates to slices |
| 10 | transition prediction baseline | deterministic predictions scored against outcomes |
| 11 | local-model prediction adapter | offline/configurable adapter and matched evaluation |
| 12 | Clingo adapter | solver parity and explanation tests |
| 13 | durable DAG scheduler | leases, fencing, retries, recovery, idempotency |
| 14 | role profiles and gateway policies | planner/executor/reviewer/verifier separation |
| 15 | simulation and boundary review | failure matrix, token/latency/cost report |
| 16 | outcome evaluation slice | goal, evidence, policy, transition measures |
| 17 | replay and counterfactual separation | proof that replay has no live dependency path |
| 18 | Litestar/msgspec API | versioned contracts and API tests |
| 19 | Granian bootstrap | lifecycle, graceful shutdown, worker/API modes |
| 20 | Podman image and compose contract | rootless, health, volume, read-only tests |
| 21 | OpenTelemetry adapter | stable spans, redaction, trace correlation |
| 22 | recovery and security | backup/restore, secrets, quotas, threat model |
| 23 | comparative context/retrieval experiments | matched budgets, ablations, limitations |
| 24 | prediction/NeSy experiments | calibration and hybrid-vs-neural baseline |
| 25 | performance and reliability benchmark | profiling before optimization |
| 26 | legacy retirement | no dual stores or obsolete coordination paths |
| 27 | release evidence | docs, examples, SBOM, reproducible verification |

Every work package is a bounded commit, runs all relevant checks, updates this status and the
migration ledger, and is published to `evolutionary-runtime` before the next package starts.

## Global acceptance

- one command executes the full Daily Operator loop;
- every material claim has inspectable provenance;
- the ContextFrame is independently inspectable;
- replay performs no live model call or side effect;
- one symbolic constraint demonstrably rejects a neural proposal;
- model selection is gateway policy rather than agent-owned configuration;
- a multi-agent DAG survives worker restart without duplicate committed effects;
- prediction quality is measured and described without a learned-world-model claim;
- API and CLI share the same application use cases;
- the OCI image runs rootless under Podman with durable local state;
- evaluation reports quality, uncertainty, latency, tokens, and cost by run and node.

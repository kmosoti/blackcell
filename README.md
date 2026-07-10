# Blackcell

Blackcell is a local-first, event-sourced control runtime for evidence-grounded LLM
agents.

It turns immutable observations into domain-scoped operational state estimates and
telemetry-derived SignalPackets, builds inspectable ContextFrames, accepts typed action
proposals from a model, evaluates symbolic policies, executes approved affordances, and
records observed outcomes for replay and evaluation.

## Why

Most agent frameworks focus on model loops and tool access. Blackcell focuses on the state
and control boundary around the model:

- which evidence supports the current state;
- which claims conflict, are stale, or remain unknown;
- why particular context was selected or omitted;
- which actions are permitted and require approval;
- what the model predicted before action;
- what actually changed afterward;
- whether a historical run can be reconstructed without repeating side effects.

The LLM is a replaceable proposal mechanism. Blackcell remains the state store, policy gate,
executor, and evaluator.

## Phase 1

The first vertical slice is the Repository Operator:

```text
observe -> append -> project state -> build context -> propose
        -> evaluate policy -> execute one bounded action
        -> re-observe -> evaluate -> append
```

The deterministic `RecordedModel` makes the complete loop usable in tests and CI without
credentials. An optional `CodexExecModel` can use a local Codex CLI login while keeping tool
authority inside Blackcell.

```bash
uv sync --all-groups
uv run blackcell operator run --model recorded --repo .
uv run blackcell operator state
uv run blackcell operator context
uv run blackcell operator replay
uv run blackcell events list
uv run blackcell bench list
uv run blackcell bench run --condition structured --trials 1
```

Blackcell emits JSON for successful commands by default. Use `--jsonl` for streaming records
or `--rich` for operator tables. The current OperatorBench command is a deterministic
fixture-contract pilot; it validates context visibility and grading contracts but does not
estimate a model-dependent context effect.

## Scientific boundary

Phase 1 implements an operational state estimator and neural proposal with symbolic
validation. It does not claim a POMDP belief state, learned world model, JEPA architecture,
neuro-symbolic reasoning contribution, or causal understanding.

The runtime records real tuples of state, action, expected effects, observed outcome, and
residual. Learned transition models become eligible only after those records support
held-out comparison with persistence, symbolic, empirical, and LLM-only baselines.

## Documentation

- [`docs/charter.md`](docs/charter.md)
- [`docs/architecture.md`](docs/architecture.md)
- [`docs/scientific-basis.md`](docs/scientific-basis.md)
- [`docs/evaluation-methodology.md`](docs/evaluation-methodology.md)
- [`docs/adr/`](docs/adr/)
- [`docs/spec/`](docs/spec/)

## Development

Blackcell targets Python 3.14 and uses uv, Ruff, ty, pytest, Hypothesis, coverage, and mutation
testing. SQLite is the Phase 1 persistence substrate. Distributed queues, graph/vector
databases, Kubernetes, multi-agent orchestration, custom neural training, and Rust components
remain deferred until measurement justifies them.

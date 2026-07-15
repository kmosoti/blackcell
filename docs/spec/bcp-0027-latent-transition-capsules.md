---
node: spec/bcp-0027-latent-transition-capsules
kind: bcp
edges:
  depends-on:
    - spec/bcp-0026-telemetry-ledger
  implements:
    - spec/jepa-latent-prediction
---

# BCP-0027: Latent Transition Capsules

Status: retired by runtime-v1 WP26 after supersession by BCP-0028 through BCP-0033

This page records the historical prototype contract. It is not an executable guide: WP26 removed
the latent package, its independent SQLite store, and the latent and harness CLI surfaces. The
canonical runtime is the Repository Operator and Daily Operator v2 path described by BCP-0032 and
BCP-0034. Historical protocol decoding remains read-only where replay requires it.

Goal: make BlackCell capable of encoding state, predicting next state, measuring
error, and revising from surprise.

## Scope

- latent domain models;
- deterministic/frozen encoder pipeline;
- transition-memory predictor baseline;
- latent prediction error metrics;
- local SQLite self-supervision sample ledger;
- CLI commands for encode, predict, error inspection, recording, ledger summary,
  and stats.

## Non-Goals

- trainable neural predictor;
- V-JEPA checkpoint integration;
- GPU-bound predictor training;
- opaque single-vector-only state.

## Historical Acceptance

The retired prototype encoded simulated transitions, reported prediction error and confidence,
and could store non-parametric samples in a dedicated local database. Its harness integration
selected whether to summarize, record, or aggregate those samples. These statements describe the
former acceptance boundary; none of the removed commands or database paths is supported after
WP26.

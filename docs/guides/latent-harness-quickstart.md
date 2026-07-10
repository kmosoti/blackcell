---
node: guides/latent-harness-quickstart
kind: guide
edges:
  depends-on:
    - concepts/harness
    - spec/bcp-0026-telemetry-ledger
    - spec/bcp-0027-latent-transition-capsules
  informs:
    - concepts/traces
---

# Latent Harness Quickstart

> **Archived experiment:** this deterministic feature sketch is kept as a reproducible
> baseline. It is not a semantic encoder, learned transition model, or JEPA implementation.
> The canonical workflow is the Repository Operator described in `../charter.md`.

BlackCell's dry-run harness can emit a JEPA-inspired latent prediction summary,
record local transition samples, and fold action-level stats into the same JSON
trace. This is a non-training V0 workflow: deterministic latent capsules,
non-parametric memory, and a local SQLite ledger.

## Pick a Harness Mode

| Mode | Command | Behavior |
| --- | --- | --- |
| off | `uv run blackcell harness run --runtime dry-run --latent off` | Emit the dry-run trace without latent summary or stats. |
| summary | `uv run blackcell harness run --runtime dry-run --latent summary` | Emit a compact latent prediction summary without recording. This is the default mode. |
| record | `uv run blackcell harness run --runtime dry-run --latent record --latent-db .blackcell/latent.sqlite3` | Record a deterministic local transition and use prior matching state/action memory. |
| stats | `uv run blackcell harness run --runtime dry-run --latent stats --latent-db .blackcell/latent.sqlite3` | Record a transition and fold ledger-backed action stats into the run trace. |

## Compatibility Shortcuts

- `--latent-db <path>` with the default mode behaves like `--latent record --latent-db <path>`.
- `--show-stats` with `--latent-db <path>` behaves like `--latent stats --latent-db <path>`.
- Recording and stats require a ledger path. `.blackcell/latent.sqlite3` is ignored by git and intended for local state.

## Inspect the Latent Ledger Directly

```bash
uv run blackcell latent encode
uv run blackcell latent predict
uv run blackcell latent errors
uv run blackcell latent record --db .blackcell/latent.sqlite3
uv run blackcell latent ledger --db .blackcell/latent.sqlite3
uv run blackcell latent predict --db .blackcell/latent.sqlite3
uv run blackcell latent stats --db .blackcell/latent.sqlite3
```

The SQLite ledger stores deterministic, idempotent records keyed by stable IDs.
It is not a remote telemetry export and it is not a neural training loop.

When both ledgers are enabled, latent transition records cite the generic
run/event evidence that produced them:

```bash
uv run blackcell harness run \
  --runtime dry-run \
  --latent record \
  --latent-db .blackcell/latent.sqlite3 \
  --ledger-db .blackcell/ledger.sqlite3
```

## Inspect the Generic Run/Event Ledger

BCP-0026 also exposes a generic local ledger for run/event provenance:

```bash
uv run blackcell ledger init --db .blackcell/ledger.sqlite3
uv run blackcell harness run --runtime dry-run --ledger-db .blackcell/ledger.sqlite3
uv run blackcell ledger runs --db .blackcell/ledger.sqlite3
uv run blackcell ledger events --db .blackcell/ledger.sqlite3
```

This ledger is local-first and deterministic. The initial command slice creates
and reads SQLite state; `--ledger-db` records dry-run harness events into the
same run/event schema.

## Output Contract

BlackCell CLI output is JSON-first by default.

- `latent`: compact prediction/transition summary from the harness loop.
- `latent_stats`: action-level ledger stats when stats mode is enabled.
- `confidence_label`: `cold`, `warming`, or `grounded`; prediction labels only
  rise when the current state/action has matching local transition evidence.

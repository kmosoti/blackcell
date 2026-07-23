---
node: guides/alpha-verify-configuration
kind: guide
edges:
  governed-by:
    - charter
    - scope
    - architecture
    - adr/0009-project-runtime-scope
  complements:
    - guides/alpha-operator-quickstart
    - guides/alpha-worker-configuration
    - guides/alpha-review-configuration
---

# Alpha Verification Configuration

Verification is disabled unless `BLACKCELL_ALPHA_VERIFY_CONFIG_FILE` points to one valid
`blackcell.alpha-verify-config/v1` document. The verification worker is deterministic: it reads the
same durable execution and review evidence, constructs a closed acceptance matrix, writes a report,
and records `pass`, `fail`, or `inconclusive`. It has no model provider, executor, acceptance runner,
worktree, shell, or network-effect port.

## Prepare the closed configuration

Copy the checked example outside the managed project and make it owner-only:

```bash
install -d -m 700 "$HOME/.config/blackcell"
cp examples/alpha/alpha-verify.json "$HOME/.config/blackcell/alpha-verify.json"
chmod 600 "$HOME/.config/blackcell/alpha-verify.json"
```

The complete document is:

```json
{
  "schema_version": "blackcell.alpha-verify-config/v1",
  "worker": {
    "lease_seconds": 300,
    "poll_milliseconds": 250,
    "supervisor_id": "alpha-verify-supervisor.local-1",
    "worker_id": "alpha-verifier.local-1"
  }
}
```

The path must be absolute, canonical, outside `BLACKCELL_REPOSITORY_ROOT`, owned by the service user,
and mode `0600`. Unknown or duplicate JSON fields, symlinks, wrong ownership, wrong mode, files over
16 KiB, invalid identifiers, equal worker/supervisor identities, and out-of-range timing values fail
closed with one content-free error.

The verifier worker and supervisor identities must differ from each other and from the execution
worker, review worker, and review supervisor. This separation is checked when the daemon composes
all enabled children.

## Enable and diagnose

Export the absolute path in the daemon environment:

```text
BLACKCELL_ALPHA_VERIFY_CONFIG_FILE=/home/USER/.config/blackcell/alpha-verify.json
```

Run one reconciliation and candidate-selection cycle without starting the API supervisor:

```bash
uv run blackcell-runtime alpha-verify-worker --once
```

Exit `1` with `alpha-verify-worker-not-configured` means the variable is absent. Exit `3` means the
configuration and shared stores opened successfully but no reviewed successful run was ready, or
storage headroom was unavailable. Exit `0` means one verification candidate reached a worker-cycle
outcome. No exit code by itself proves a `pass`; inspect the run replay and verification lifecycle.

For normal operation, start `uv run blackcell daemon foreground` or the installed user service. The
daemon validates this configuration before spawning any child and adds `alpha-verify-worker` only
when the file is configured. The verifier may drain existing reviewed evidence even when execution
and review workers are not concurrently enabled.

## Evidence and restart behavior

Only a durable successful execution followed by a durable successful review is eligible. The source
service reconstructs and verifies their immutable events and artifacts without a provider, executor,
acceptance check, or worktree call. A model review is input evidence, not the verification verdict.

The worker claims a fenced verification lease, writes its canonical report artifact first, then
records completion with the exact report and matrix digests. Deterministic verification may finish
after the lease's wall-clock expiry; that terminal write is accepted only while the same lease
digest, worker, and claimed lifecycle state remain active. A supervisor requeue or newer fencing
token makes the old worker stale and rejects its terminal write. On restart, an incomplete claim may
be requeued under a higher fence. A stored report without a durable completion remains
`verifier-error`; it is never promoted to `pass` from file presence or metadata inference.

Inspect the resulting lifecycle through the shared client:

```bash
uv run blackcell alpha run status RUN_ID
uv run blackcell alpha run replay RUN_ID
uv run blackcell alpha events list --after CURSOR --limit 100
```

Replay is live-free. Missing, changed, malformed, noncanonical, metadata-mismatched, or source-unbound
verification evidence produces bounded findings or an inconclusive state rather than a fabricated
verdict.

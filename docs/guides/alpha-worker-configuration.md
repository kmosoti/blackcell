---
node: guides/alpha-worker-configuration
kind: guide
edges:
  governed-by:
    - charter
    - scope
    - architecture
    - adr/0009-project-runtime-scope
---

# Alpha Worker Configuration

The daemon is API-only unless `BLACKCELL_ALPHA_WORKER_CONFIG_FILE` points to one valid
`blackcell.alpha-worker-config/v1` document. This is deliberate: an omitted model, executable, or
isolation choice leaves work queued instead of selecting ambient authority or the historical V2
worker.

## Prepare paths

Resolve every executable to its canonical non-symlink path. Create the isolation directory as the
service user with mode `0700`. The JSON file must be outside the managed project, owned by the
service user, and mode `0600`.

The following is the complete shape; replace every uppercase placeholder with a canonical absolute
path or an operator-selected model identifier:

```json
{
  "schema_version": "blackcell.alpha-worker-config/v1",
  "provider": {
    "profile_id": "alpha-code",
    "model_id": "YOUR_CODE_MODEL_ID",
    "codex_executable": "/CANONICAL/PATH/TO/codex",
    "git_executable": "/CANONICAL/PATH/TO/git",
    "classification": "private",
    "locality": "remote-allowed",
    "max_input_tokens": 32000,
    "max_output_tokens": 4096,
    "max_cost_microusd": 0,
    "timeout_ceiling_seconds": 120,
    "environment_variables": ["CODEX_HOME", "HOME", "OPENAI_API_KEY"]
  },
  "isolation": {
    "root": "/OWNER/ONLY/BLACKCELL_DATA/alpha-worktrees",
    "executables": {
      "python": "/CANONICAL/PATH/TO/python3"
    },
    "runtime_roots": [],
    "bubblewrap_executable": "/CANONICAL/PATH/TO/bwrap",
    "prlimit_executable": "/CANONICAL/PATH/TO/prlimit",
    "probe_executable": "/CANONICAL/PATH/TO/true",
    "limits": {
      "address_space_bytes": 1073741824,
      "cpu_seconds": 60,
      "processes": 128,
      "open_files": 128,
      "file_size_bytes": 16777216,
      "tmpfs_bytes": 67108864
    }
  },
  "worker": {
    "worker_id": "alpha-worker.local-1",
    "stdout_limit_bytes": 1048576,
    "stderr_limit_bytes": 1048576,
    "lease_grace_seconds": 30,
    "max_retained_successful_worktrees": 2
  }
}
```

The file contains no credential values. `environment_variables` is a required name-only allowlist;
every listed name must exist in the daemon environment. BlackCell rejects its own `BLACKCELL_*`
variables and dynamic-loader, Python, Git, or shell control variables. Include only the provider
authentication and platform values the pinned Codex executable actually needs. If Codex uses an
owner-only auth store rather than an API-key variable, omit `OPENAI_API_KEY` and allow the required
home variable instead.

`classification` may be `public`, `internal`, or `private`. The only alpha provider in this version
is the non-local Codex CLI adapter, so `locality` must explicitly be `remote-allowed`; `secret`
classification and `local-only` fail closed. Each acceptance command's first argv token must match
one key in `isolation.executables`. Add a canonical runtime directory to `runtime_roots` only when
that executable requires files outside the fixed read-only system roots; those directories become
read-only sandbox mounts and cannot overlap the repository, runtime data, or worktree roots.

`max_retained_successful_worktrees` is a required global checkout-count policy from `0` through
`1024`. Zero removes every eligible successful checkout after recording success; larger values keep
the newest checkouts by durable success-event order. Cleanup never deletes the deterministic local
branch or its successful commit. Failed, canceled, dirty, reconciliation-required, and
cleanup-failed worktrees are not automatically removed.

## Enable and diagnose

Add the absolute config path and any allowlisted provider variables to the owner-only daemon
environment file:

```text
BLACKCELL_ALPHA_WORKER_CONFIG_FILE=/home/USER/.config/blackcell/alpha-worker.json
```

Then run one startup/reconciliation/dispatch cycle without starting the API supervisor:

```bash
uv run blackcell-runtime alpha-worker --once
```

Exit `3` means the configuration and shared stores opened successfully but no node was ready,
storage headroom was exhausted, or the retained-checkout count could not be brought within policy.
Inspect ordered events for `alpha.node.worktree-cleanup-failed` in the last case. Exit `0` means one
node reached a worker-cycle outcome. For normal
operation, start `uv run blackcell daemon foreground` or the installed user service. The daemon
validates this same contract before spawning either child; a bad alpha configuration cannot leave
an API process running with a silently missing worker.

## Provider crash boundary

For every repository-writing node, BlackCell stores the canonical bounded context artifact and then
appends `alpha.node.provider-dispatch-started` before invoking the configured Codex process. The
event binds the exact lease, worker, deterministic request ID, and identical context and artifact
digests. The provider call uses that event as its causation identity.

If the daemon restarts after this marker but before a terminal node event, BlackCell does not call
the provider again. The run becomes `reconciliation-required` with failure code
`alpha-provider-dispatch-ambiguous`, even when the worktree is missing or unchanged. This is a
fail-closed duplicate-prevention boundary, not proof of provider completion or external
exactly-once behavior; the prior Codex process may have accepted, completed, or still be executing
the request. Inspect the retained event and artifact evidence and resolve the attempt explicitly.
Cancellation already requested before restart keeps precedence.

## Successful checkout retention

Cleanup is itself replayable work. BlackCell appends
`alpha.node.worktree-cleanup-requested` before asking Git to remove a clean policy-compliant
checkout. It then appends `alpha.node.worktree-cleaned` with removal evidence, or
`alpha.node.worktree-cleanup-failed` with a stable content-free code. A restart completes a pending
request whether it stopped before removal or after removal but before the completion event; the
surviving branch must still resolve to the successful head. Recorded failures are not retried
automatically. Increase the configured retained count to unblock dispatch while preserving that
evidence, then restart the worker. Preserve or correct the checkout itself for a future explicit
operator-retry workflow; this alpha does not silently retry a recorded cleanup failure.

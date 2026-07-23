---
node: guides/alpha-review-configuration
kind: guide
edges:
  governed-by:
    - charter
    - scope
    - architecture
    - adr/0009-project-runtime-scope
  complements:
    - guides/alpha-worker-configuration
---

# Alpha Review Configuration

Review is disabled unless `BLACKCELL_ALPHA_REVIEW_CONFIG_FILE` points to one valid
`blackcell.alpha-review-config/v1` document. The review worker is a separate foreground process: it
reads successful execution evidence, owns the fenced review stream, writes review artifacts, and
calls one REVIEW-only model route. It receives no executor, acceptance runner, worktree, shell, or
network-effect port.

The JSON file must be an absolute canonical path outside the managed repository, owned by the
service user, and mode `0600`. Resolve the Codex and Git executables to canonical non-symlink paths.
The complete configuration is:

```json
{
  "schema_version": "blackcell.alpha-review-config/v1",
  "provider": {
    "profile_id": "alpha-review",
    "model_id": "YOUR_REVIEW_MODEL_ID",
    "codex_executable": "/CANONICAL/PATH/TO/codex",
    "git_executable": "/CANONICAL/PATH/TO/git",
    "classification": "private",
    "locality": "remote-allowed",
    "max_input_tokens": 64000,
    "max_output_tokens": 8192,
    "max_cost_microusd": 0,
    "timeout_ceiling_seconds": 180,
    "environment_variables": ["CODEX_HOME", "HOME", "OPENAI_API_KEY"]
  },
  "worker": {
    "worker_id": "alpha-reviewer.local-1",
    "supervisor_id": "alpha-review-supervisor.local-1",
    "lease_seconds": 300,
    "poll_milliseconds": 250
  }
}
```

`environment_variables` contains names, never credential values. Every listed variable must exist
in the daemon environment. BlackCell rejects its own `BLACKCELL_*` variables and dynamic-loader,
Python, Git, and shell control variables. The configured Codex child receives only the selected
values. Classification may be `public`, `internal`, or `private`; `secret` is rejected. The current
Codex adapter is non-local, so locality must explicitly be `remote-allowed`.

The reviewer and supervisor identities must differ. When alpha execution is also configured, the
reviewer identity, supervisor identity, and REVIEW profile must differ from the execution worker and
CODE profile. The model identifier may be the same when the operator deliberately wants that model,
but routing and durable authority remain separate.

`lease_seconds` must be strictly greater than `provider.timeout_ceiling_seconds`. BlackCell reserves
their difference for post-provider artifact admission and terminal persistence. Time spent preparing
the verified review context reduces the provider call's remaining latency budget so that the selected
reserve is preserved; an exhausted provider window fails before durable provider dispatch.

Add the path and any allowlisted provider variables to the owner-only daemon environment:

```text
BLACKCELL_ALPHA_REVIEW_CONFIG_FILE=/home/USER/.config/blackcell/alpha-review.json
```

Run one reconciliation and review-selection cycle without starting the API supervisor:

```bash
uv run blackcell-runtime alpha-review-worker --once
```

Exit `3` means startup succeeded but no unreviewed successful run was ready, or storage headroom was
unavailable. Exit `0` means one review reached a worker-cycle outcome. A provider is never called for
an absent, unsuccessful, artifact-incomplete, tampered, already-reviewed, or
reconciliation-required execution.

With normal `blackcell daemon foreground` operation, the daemon validates the REVIEW route before
spawning any child and adds `alpha-review-worker` only when this file is configured. Review may run
without a concurrently configured execution worker so an existing durable ledger can be drained;
neither setting starts the historical V2 worker.

On startup, `supervisor_id` reconciles existing review leases. A claim that stopped before provider
dispatch may be requeued under a new fence. Once `alpha.review.provider-dispatch-started` exists, the
request is never automatically repeated: restart records `alpha-review-dispatch-ambiguous` and
requires explicit reconciliation. The provider receives only the already-stored, live-free,
artifact-verified context and cannot approve its own proposal or alter acceptance criteria.

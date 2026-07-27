---
node: guides/review-configuration
kind: guide
edges:
  governed-by:
    - charter
    - scope
    - architecture
    - adr/0009-project-runtime-scope
  complements:
    - guides/execution-worker-configuration
    - guides/verification-configuration
    - epistemic-evaluation
---

# Review configuration

Review is disabled unless `BLACKCELL_REVIEW_CONFIG_FILE` points to one valid
`blackcell.review-config/v1` document. The review worker is a separate foreground process. It reads
successful execution evidence, owns a fenced review stream, writes review artifacts, and calls one
review-only model route. It receives no executor, acceptance runner, worktree, shell, or
network-effect port.

The file must be an absolute canonical path outside the managed repository, owned by the service
user, and mode `0600`. Resolve the Codex and Git executables to canonical non-symlink paths.

```json
{
  "schema_version": "blackcell.review-config/v1",
  "provider": {
    "profile_id": "review",
    "model_id": "REPLACE_WITH_REVIEW_MODEL_ID",
    "codex_executable": "/ABSOLUTE/PATH/TO/codex",
    "git_executable": "/ABSOLUTE/PATH/TO/git",
    "classification": "private",
    "locality": "remote-allowed",
    "max_input_tokens": 64000,
    "max_output_tokens": 8192,
    "max_cost_microusd": 0,
    "timeout_ceiling_seconds": 180,
    "environment_variables": []
  },
  "worker": {
    "worker_id": "reviewer.local-1",
    "supervisor_id": "review-supervisor.local-1",
    "lease_seconds": 300,
    "poll_milliseconds": 125
  }
}
```

The reviewer and supervisor identities must differ. The review profile and worker identities must
also differ from execution authority. The lease must outlive the provider timeout ceiling.

```bash
export BLACKCELL_REVIEW_CONFIG_FILE=/home/USER/.config/blackcell/review.json
uv run blackcell-runtime review-worker --once
```

The worker admits only a closed proposal whose findings cite evidence in the supplied context and
whose epistemic matrix contains every required dimension. Admission proves structural and evidence
binding only; verification still adjudicates the result. A restart never repeats a provider call
whose dispatch was recorded without a durable result—it records a reconciliation requirement.

See [Epistemic evaluation](../epistemic-evaluation.md) for the review and verification matrix.

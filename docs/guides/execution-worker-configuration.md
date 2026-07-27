---
node: guides/execution-worker-configuration
kind: guide
edges:
  governed-by:
    - charter
    - scope
    - architecture
    - adr/0009-project-runtime-scope
  complements:
    - guides/runtime-quickstart
    - guides/execution
---

# Execution worker configuration

Execution is disabled unless `BLACKCELL_EXECUTION_CONFIG_FILE` points to one valid
`blackcell.execution-worker-config/v3` document. An omitted provider, executable, or isolation
choice leaves work queued. The runtime never selects ambient authority and has no generic worker
fallback.

The file must be an absolute canonical path outside the managed repository, owned by the service
user, and mode `0600`. The isolation root must be owner-only mode `0700`. Every executable path must
be absolute, canonical, executable, and not a symlink.

```json
{
  "schema_version": "blackcell.execution-worker-config/v3",
  "provider": {
    "adapter": "codex-cli",
    "profile_id": "execution",
    "model_id": "REPLACE_WITH_MODEL_ID",
    "executable": "/ABSOLUTE/PATH/TO/codex",
    "git_executable": "/ABSOLUTE/PATH/TO/git",
    "classification": "private",
    "locality": "remote-allowed",
    "max_input_tokens": 32000,
    "max_output_tokens": 4096,
    "max_cost_microusd": 0,
    "timeout_ceiling_seconds": 120,
    "environment_variables": []
  },
  "isolation": {
    "root": "/ABSOLUTE/OWNER/ONLY/PATH/execution-worktrees",
    "executables": {
      "python": "/ABSOLUTE/PATH/TO/python"
    },
    "runtime_roots": [],
    "bubblewrap_executable": "/ABSOLUTE/PATH/TO/bwrap",
    "prlimit_executable": "/ABSOLUTE/PATH/TO/prlimit",
    "probe_executable": "/ABSOLUTE/PATH/TO/true",
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
    "worker_id": "execution-worker.local-1",
    "stdout_limit_bytes": 65536,
    "stderr_limit_bytes": 32768,
    "lease_grace_seconds": 15,
    "max_retained_successful_worktrees": 2
  }
}
```

The provider adapter may be `codex-cli` or `agy-cli`; each has a closed set of fields. Environment
variables are an allowlist of names, never credential values. Execution profile and worker
identities must differ from any configured review or verification authority.

Set the configuration path and run one bounded diagnostic claim:

```bash
export BLACKCELL_EXECUTION_CONFIG_FILE=/home/USER/.config/blackcell/execution.json
uv run blackcell-runtime execution-worker --once
```

Exit `0` means one node reached a terminal transition, exit `3` means no node was ready, and other
nonzero exits are content-free configuration or runtime failures. The daemon validates the same
configuration before starting any child.

Provider dispatch is recorded before the external process starts. If durable completion is absent
after a restart, the worker records a reconciliation requirement and does not repeat the provider
call automatically. Worktree cleanup follows the same record-intent-then-observe-result rule.

---
node: guides/verification-configuration
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
    - epistemic-evaluation
---

# Verification configuration

Verification is disabled unless `BLACKCELL_VERIFICATION_CONFIG_FILE` points to one valid
`blackcell.verification-config/v1` document. The verification worker is deterministic: it reads the
durable execution and review evidence, constructs a closed acceptance matrix, writes a report, and
records `pass`, `fail`, or `inconclusive`. It has no model provider, executor, acceptance runner,
worktree, shell, or network-effect port.

The complete checked example is:

```json
{
  "schema_version": "blackcell.verification-config/v1",
  "worker": {
    "lease_seconds": 300,
    "poll_milliseconds": 250,
    "supervisor_id": "verification-supervisor.local-1",
    "worker_id": "verifier.local-1"
  }
}
```

Copy it outside the managed repository and make it owner-only:

```bash
install -d -m 700 "$HOME/.config/blackcell"
cp examples/runtime/verification.json "$HOME/.config/blackcell/verification.json"
chmod 600 "$HOME/.config/blackcell/verification.json"
export BLACKCELL_VERIFICATION_CONFIG_FILE="$HOME/.config/blackcell/verification.json"
uv run blackcell-runtime verification-worker --once
```

The worker and supervisor identities must differ from one another and from configured execution and
review identities. Exit `0` means one candidate was processed, exit `3` means no candidate was
ready, and other nonzero exits are content-free configuration or runtime failures.

Every acceptance criterion must have a deterministic evidence row. The epistemic policy adds rows
for acceptance coverage, evidence grounding, counterevidence, causal overreach, scope challenge,
and uncertainty. A concern fails its row; missing evidence remains `unknown` and makes the outcome
`inconclusive`; a dimension may be `not-applicable` only with an explicit reason. See
[Epistemic evaluation](../epistemic-evaluation.md).

Inspect the durable result through the normal clients:

```bash
uv run blackcell run status RUN_ID
uv run blackcell run replay RUN_ID
uv run blackcell events list --after CURSOR --limit 100
```

---
node: spec/bcp-0029-event-kernel
kind: bcp
edges:
  depends-on:
    - spec/bcp-0028-charter-reset
  precedes:
    - spec/bcp-0030-repository-state
---

# BCP-0029: Unified Event and Artifact Kernel

Status: implemented

Provide an immutable versioned event envelope, unique occurrence identities, separate
idempotency semantics, optimistic stream concurrency, content-addressed artifacts, projection
checkpoints, and deterministic historical replay on local SQLite.

Acceptance:

- repeated equivalent occurrences remain distinct unless the same idempotency key is reused;
- divergent idempotency reuse and stream-version conflicts are rejected;
- artifact reads verify content hashes;
- projections rebuild deterministically after process restart.

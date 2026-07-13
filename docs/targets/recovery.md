---
node: targets/recovery
kind: target
edges:
  governed-by:
    - adr/0005-durable-run-and-execution-protocol
    - adr/0007-runtime-security-boundary
  deployed-by:
    - targets/containers
  implements:
    - spec/bcp-0034-evolutionary-runtime
---

# Runtime Recovery and Quotas

Runtime-v1 has one local recovery format for the canonical SQLite database and immutable artifact
blobs. A bundle is a mode-`0700` directory containing a consistent SQLite online snapshot, the
exact artifact inventory visible in that snapshot, and a canonical mode-`0600` manifest written
last. The manifest records the database hash, schema version, event high-water position, and every
artifact path, size, and digest.

Verification is mandatory before retention or restore. It rejects symlinks, unsafe uid or modes,
unknown or extra files, non-canonical manifests, database integrity or foreign-key failures, and
missing, extra, or corrupted artifact bytes. Public failures contain only stable codes.

## JSON-first commands

Backup and list use the explicit active `BLACKCELL_DATA_DIR` but do not load the service token,
repository, provider credentials, or model runtime. Retention defaults to seven verified bundles:

```bash
export BLACKCELL_DATA_DIR=/absolute/runtime-data
export BLACKCELL_BACKUP_RETENTION_COUNT=7
blackcell-runtime recovery backup
blackcell-runtime recovery list
```

Verification and restore take absolute paths and do not require active-runtime configuration:

```bash
blackcell-runtime recovery verify /absolute/off-volume/backup-ID
blackcell-runtime recovery restore \
  /absolute/off-volume/backup-ID \
  /absolute/new-runtime-data
```

Every successful command writes one JSON object to stdout. Failures write one content-free JSON
error to stderr and return nonzero. Copy a verified bundle away from the active data volume before
relying on it for disaster recovery; the canonical backup directory is intentionally on the same
runtime volume and is only the staging and local-retention location.

## Restore and cutover procedure

1. Create a backup, verify it, copy the complete bundle off the active volume, and verify the copy.
2. Stop the API and worker before recovery cutover. A backup may run online; restore and cutover may
   not overlap active writers.
3. Restore into an absent absolute target. Restore stages and fsyncs the complete canonical layout,
   then renames it into place. It never overwrites or deletes an existing target.
4. Start API and worker with `BLACKCELL_DATA_DIR` set to the restored target. Confirm readiness,
   inspect the event high-water position, and replay a known run with live dependencies disabled.
5. Keep the prior data root for rollback until application acceptance completes; remove it only as
   a separate, explicitly authorized operation.

If the original root was lost, restore the external copy to the original now-absent path or select
a new path. If the original root still exists, always select a different target. Retention deletes
only the oldest verified bundle directories after a new bundle verifies; it never removes kernel
events, artifact metadata, or active artifact blobs. Invalid or unrelated backup-directory entries
are left untouched for operator inspection.

For rootless Compose, an online backup can run as the service uid:

```bash
podman compose exec blackcell-api blackcell-runtime recovery backup
```

Copy and verify the resulting bundle outside the named volume. Disaster restore uses a stopped
deployment and a one-shot runtime container with the external bundle mounted read-only and the
named state volume mounted writable. Do not use `podman compose down --volumes` until an external
bundle has been verified and a restore drill has passed.

## Admission quotas

The production API and worker use these bounded defaults:

| Environment variable | Default | Contract |
| --- | ---: | --- |
| `BLACKCELL_REQUESTS_PER_MINUTE` | `600` | one process-local sliding window shared by all protected routes |
| `BLACKCELL_ACTIVE_STORAGE_MAX_BYTES` | `10737418240` | active database, WAL/SHM, and artifact tree ceiling for admission |
| `BLACKCELL_MUTATION_RESERVE_BYTES` | `16777216` | headroom required before API or worker mutation |
| `BLACKCELL_BACKUP_RETENTION_COUNT` | `7` | maximum verified bundles retained locally |

Request admission occurs before authentication, so failed credential attempts consume the same
budget. Liveness and readiness are exempt. The initial service intentionally trusts no proxy or
client address, so the quota is global rather than identity- or IP-specific. Exhaustion returns
HTTP `429` without echoing request content.

Active-storage admission excludes backup bundles, marks readiness not ready, returns HTTP `507`
for API mutations, and prevents the worker from recovering or acquiring work until reserve is
available. Artifact metadata transactions enforce an additional exact aggregate byte ceiling
across API and worker `ArtifactStore` instances; duplicate content remains idempotent.

These controls are application admission, not filesystem, cgroup, tenant, or distributed hard
quotas. A concurrent SQLite commit can consume some reserve, backup creation can still exhaust the
underlying volume, and host-level capacity monitoring remains required.

## Acceptance evidence

The focused recovery tests cover online backup during concurrent appends, canonical inventory and
mode checks, tamper rejection, verified-only retention, existing-target refusal, JSON-first CLI
behavior, active-storage and request boundaries, cross-instance artifact serialization, and
worker/API exhaustion behavior. The disaster test copies a bundle outside the active root, deletes
the source state, restores a new root, and verifies live-free replay and artifact integrity after
the observed repository is taken offline.

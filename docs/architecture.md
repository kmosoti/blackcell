# Architecture

BlackCell is SDK-first. The CLI translates arguments and renders
`ResultEnvelope`; services own behavior; adapters own provider-specific
transport and mapping.

```text
CLI
 └─ BlackcellClient
     ├─ PlanService
     │   └─ ProjectIntegration
     ├─ MaterializationService
     ├─ VerificationService
     └─ SyncService
         ├─ LinearGraphQLAdapter  (planner-side mutation)
         ├─ GitHubRestAdapter     (read-only)
         ├─ PlanStore             (canonical local directive copy)
         └─ Chronicle             (append-only SQLite audit/recovery events)
```

## Authority

Linear is authoritative for approval and work planning. GitHub is authoritative
for repository code, review, and merge. BlackCell does not repair ambiguous
divergence: it records an anomaly and stops.

## Linear Project integration

The Project integration is layered:

1. `ProjectPresentationConfig` owns non-secret brand, color, optional provider
   icon identifier, and repository-link label.
2. `LinearGraphQLAdapter` owns typed Project and external-link queries and
   mutations.
3. `ProjectIntegration` renders expected state, classifies identity versus
   presentation drift, verifies immutable states, and reconciles Proposal-only
   presentation fields.
4. `PlanService` packages directive create, inspect, reconcile, and verify
   workflows.
5. `BlackcellClient` and the `operation` CLI expose those workflows without
   duplicating provider policy.

`operation inspect` is read-only and reports drift. `operation reconcile` may
repair content, color, an explicitly configured icon identifier, and the
repository link only while the Project is a Proposal. `operation verify` is
strict and never repairs drift.

`pulse linear` verifies that the workspace-level GitHub integration is active.
Linear's public GraphQL read model does not expose `GitHubSettingsInput`
repository mappings, so the configured `BLCELL` to `kmosoti/blackcell`
bidirectional issue-sync mapping remains an explicit authenticated-UI check.
BlackCell reports that limitation instead of inferring sync readiness.

## Idempotency

Each Project and issue description contains a visible deterministic marker.
Before any create mutation BlackCell enumerates the applicable Linear scope and
locates the marker. Zero matches allows creation, one exact match is reused,
multiple matches or a digest mismatch is an anomaly.

Create mutations are never blindly retried. If a response is lost, reconciliation
searches by marker before attempting another create.

## Secrets

`blackcell.toml` is non-secret. Credentials are loaded into `SecretStr` values
from the process environment. Transport errors are mapped to stable BlackCell
errors without headers or response bodies. Chronicle payloads reject known
credential field names and any value containing a credential currently present
in the environment.

## Recovery

Materialization is serialized by a per-plan file lock. Chronicle inserts use
`BEGIN IMMEDIATE`; database triggers reject updates and deletes. Pending GitHub
echoes return the exact recovery command:

```bash
blackcell directive reconcile BCP-0001
```

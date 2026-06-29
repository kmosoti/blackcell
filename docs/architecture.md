# Architecture

BlackCell is SDK-first. The CLI translates arguments and renders
`ResultEnvelope`; services own behavior; adapters own provider-specific
transport and mapping.

```text
CLI
 └─ BlackcellClient
     ├─ OperationExecutor
     │   ├─ StructuredEventAspect
     │   ├─ CredentialAspect
     │   └─ AnomalyAspect
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

## Facade contracts

`sdk/operations.py` is the single operation catalog. Each public SDK workflow
has one immutable `OperationSpec` classifying its facade, authority, effect,
credentials, and invariant aspects. CLI packages delegate to those SDK
operations; they do not maintain a parallel policy table.

Invariant aspects are deliberately orthogonal:

- input and identity establish who and what may act;
- authority and state establish where and when an action is valid;
- immutability and idempotency constrain repeat execution;
- output and observability provide stable machine-readable behavior;
- publication identity constrains commit, push, and pull-request boundaries.

`OperationExecutor` is an explicit aspect pipeline, not decorator magic.
Credential preparation, correlation context, structured events, pending
outcomes, and conflict journaling execute consistently around every cataloged
SDK operation. `BLACKCELL_EVENTS=jsonl` enables redacted JSONL events on stderr,
while result envelopes remain on stdout.

## Provider capability protocols

The Linear adapter is the GraphQL schema boundary. Services depend on narrow
capability protocols such as identity reading, Project status reading, Project
writing, assignment reading, and relation writing. The materialization service
therefore cannot accidentally acquire unrelated Project presentation or
integration-management behavior merely because the concrete Linear adapter
implements it.

This is intentional schema containment: BlackCell packages the subset of the
published Linear schema required by its workflows, validates provider payloads
at the adapter, and keeps GraphQL objects out of policy and SDK contracts.

The provider contract is reviewed against Linear's published `current` schema:

```text
https://studio.apollographql.com/public/Linear-API/variant/current/schema/reference
https://api.linear.app/graphql
```

Relevant behavior is packaged by workflow rather than mirroring the entire
provider schema:

| BlackCell workflow | Provider behavior | Capability boundary |
| --- | --- | --- |
| profile and pulse | viewer, team, statuses, states, integrations | identity/workflow/integration readers |
| directive proposal | Project locate/create and Proposal presentation repair | Project locator/writer |
| operation inspection | Project marker, digest, repository link, presentation drift | Project locator |
| assignment materialization | labels, issues, parents, blocking relations | materialization backend |
| echo verification | read-only GitHub Issue projection | repository reader |
| publication preflight | local Git and GitHub CLI identity metadata | publication backend |

Adding another Linear field starts in the adapter response model and the
narrowest applicable capability protocol. It only reaches a service or facade
when a BlackCell invariant or packaged workflow consumes it.

`scripts/linear_schema_jsonl.py` captures the live Linear GraphQL schema through
introspection as streaming JSONL and records the public Apollo Studio reference
for the same current variant:

```text
https://studio.apollographql.com/public/Linear-API/variant/current/schema/reference
```

```bash
set -a
source ~/.config/blackcell/env
set +a
uv run python scripts/linear_schema_jsonl.py > /tmp/linear-schema.jsonl
```

The stream emits a `schema_source` record, schema, directive, and per-type
records, then a final `schema_digest` record. This keeps provider schema review
incremental and agent-friendly without persisting credentials or requiring a
monolithic pasted schema.

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

## Publication safety

Publication remains outside BlackCell's mutation authority. The
`publication preflight` facade only reads local Git and GitHub CLI metadata.
Its stage model progressively verifies:

1. `commit`: non-default branch, branch prefix, and local Git identity;
2. `push`: HEAD author and exact SSH push target;
3. `pull_request`: active GitHub login plus PR author, draft state, base, and
   head branch when a PR exists.

The expected executor is derived from `identity.executor_github_login`; it is
not duplicated in publication configuration. A mismatch fails as policy before
the operator runs the corresponding publication command.

---
node: adr/0008-architecture-consolidation
kind: adr
edges:
  decides:
    - architecture
    - implementation-baseline
  depends-on:
    - adr/0001-event-sourced-kernel
    - adr/0003-model-execution-boundary
    - adr/0004-evolutionary-runtime-architecture
    - adr/0005-durable-run-and-execution-protocol
    - adr/0006-versioned-run-feedback-protocol
    - adr/0007-runtime-security-boundary
---

# ADR 0008: Consolidate Architecture by Evidence, Not Object Count

Status: accepted

Amended: 2026-07-16 — AC07 source-bound candidate issuance retired; architecture fitness remains
enforced through ordinary protected-branch CI.

## Context

Runtime-v1 has accepted authority, provenance, replay, durability, recovery, security, and static
dependency boundaries. Its implementation also contains concrete composition outside the documented
bootstrap boundary, duplicated contract vocabulary, broad field-copying structural Protocols,
stateless service objects, physically large protocol coordinators, and SQLite-specific behavior
described as though it were freely replaceable.

These concerns are not evidence that object orientation or module count is intrinsically harmful.
Removing a class, port, package, or DTO can obscure an authority, temporal, persistence, failure,
or substitution boundary just as easily as it can simplify the code. Architecture consolidation
therefore requires source evidence and an explicit preserved invariant for every decision.

The source-bound AC00 inventory and classifications are recorded in
`../decisions/architecture-consolidation/ac00-baseline.json`.

## Decision

Blackcell remains a modular monolith. Consolidation removes false boundaries while retaining real
trust, time, persistence, replay, authority, recovery, and failure boundaries.

### Boundary-earning criteria

A separately named class, Protocol, package, DTO family, adapter seam, or application service is
presumed justified when at least two of these conditions hold:

1. it crosses an authority or trust boundary;
2. it owns independent failure or recovery semantics;
3. it has multiple demonstrated implementations;
4. it owns an externally persisted or versioned contract;
5. it has an independent deployment, scaling, latency, or resource profile;
6. it changes under a materially different ownership or release cadence;
7. it is independently invoked as a product use case;
8. it enforces security or policy.

A boundary satisfying fewer criteria may remain only when its decision evidence records the
specific invariant it protects. The criterion count is a review heuristic, not a quality score or
automated deletion rule.

### Decision vocabulary

Every consolidation candidate uses exactly one decision:

- `retain`: the boundary has independent semantics that remain necessary;
- `consolidate`: the boundary's implementation or ownership moves into a more cohesive capability;
- `defer`: evidence is insufficient or a prerequisite issue owns the remaining decision;
- `reject`: the proposed simplification would weaken a real boundary or optimize only syntax or
  object count.

Where a separate boundary is not justified, prefer a pure operation, internal module, private
value object, or strategy callable. No repository-wide class-to-function conversion is authorized.

### Ratified classifications

- **H1, composition ownership: confirmed.** Bootstrap must own concrete runtime assembly.
  RepositoryOperator's public use cases remain, but API and worker reach-through to its stores does
  not.
- **H2, state-to-context false boundaries: confirmed.** Identity-bearing `SignalPacket`,
  `EvidenceSelection`, and persisted `ContextFrame` contracts remain. Field-copying implementation
  and unjustified structural Protocols may consolidate behind those contracts. Deterministic and
  FTS5 matching remain separate strategies.
- **H3, overlapping contract vocabulary: confirmed.** Identical gateway/decision budget fields and
  closely related routing primitives require an ownership decision. Durable decision records and
  control authority records remain distinct. `legacy-canonical` is not an acceptable permanent
  classification.
- **H4, object-shaped ceremony: confirmed for named candidates only.** `SignalPacketProjector`,
  `ContextFrameBuilder`, and `ActionAuthorizer` are approved AC04 candidates. `OutcomeEvaluator`
  retains injected time behavior, and retrievers retain matcher strategy.
- **H5, protocol integration hotspots: confirmed.** One public `FeedbackRunRecorder` and one public
  transition-binding operation remain. Their physical internals may split by protocol phase; no
  generic command bus, saga framework, or polymorphic workflow hierarchy is introduced.
- **H6, SQLite boundary ambiguity: confirmed.** SQLite schema, WAL, filesystem mode, transaction,
  append, and recovery behavior are runtime-v1 kernel commitments. Alternate storage abstraction
  requires a demonstrated second implementation or deployment requirement and a separate ADR.
- **H7, incomplete architecture fitness: confirmed.** Existing dependency and replay checks remain.
  Binary composition, compatibility, and reach-through rules may fail CI; similarity, breadth,
  size, fan-in, and co-change remain advisory.

### Preserved invariants

No consolidation may weaken:

- the single authoritative immutable event ledger or content-addressed artifact integrity;
- exact occurrence, stream, correlation, causation, cutoff, provenance, and replay semantics;
- model-free and side-effect-free historical replay;
- the absence of ambient model execution authority;
- symbolic policy and authorization before execution;
- prepared execution, uncertain-effect reconciliation, fencing, leases, retries, approvals, and
  recovery;
- fail-closed handling of unknown, conflicting, malformed, or stale evidence;
- explicit versioning and identity of persisted event and artifact formats;
- existing CLI and HTTP behavior unless a separately approved issue changes it.

An issue stops and splits when it discovers a required persisted schema, public behavior,
authority, recovery, security, or dependency change.

### Architecture fitness

CI may fail only on deterministic binary rules, including import direction, live-free replay,
composition ownership, compatibility isolation, and facade non-reach-through. Record similarity,
Protocol breadth, module size, import breadth, constructor fan-in, and package co-change are
review evidence. They have no pass threshold and cannot justify a refactor without a source-level
semantic finding.

### Evidence policy

The runtime-v1 evidence bundle is historical and read-only. Every Git-tracked regular file below
`docs/decisions/runtime-v1/` and `release/runtime-v1/` is part of that frozen inventory.
Architecture-consolidation work does not regenerate its candidate ID, verification manifest,
decisions, release notes, release configuration, or SBOM. The source SHA ratified by AC00 anchors
the exact path and byte inventory, and current tests compare the frozen files with that Git tree so
adding, removing, or changing a historical evidence file fails closed. Runtime-v1 release-tool
behavior is exercised against a synthetic current fixture instead of falsely reproducing the
frozen candidate from the refactored working tree.

AC00 remains the source-bound historical baseline for the decisions that started this program. Its
candidate scheme is superseded by this amendment. The first AC07 implementation demonstrated that
binding all repository materials to a consolidation candidate made every later source or workflow
change require a new source commit, full verification run, manifest, decision, and evidence-only
commit. That lifecycle was release ceremony without an independent publication, deployment,
persisted-contract, or recovery boundary.

AC07 therefore retains only the parts that earn ongoing enforcement:

- deterministic binary rules remain in `tests/architecture/test_dependencies.py`;
- threshold-free advisory conclusions remain in the static
  `../decisions/architecture-consolidation/ac07-architecture-fitness.json` decision;
- protected-branch CI selects every required architecture rule by its exact pytest node ID, rejects
  missing, skipped, or xfailed rules, and runs locked dependency setup, formatting, lint, types,
  and the complete no-ignore coverage suite;
- CI never generates project evidence and does not bind ordinary source changes to a candidate;
- repository-approved merge methods are acceptable after required status checks and any enabled
  review or conversation policy pass.

The architecture-consolidation manifest, generator, replay/freshness modes, conditional SBOM, and
merge-commit-only rule are retired. This does not supersede or regenerate runtime-v1 historical
evidence. A future release or dependency-closure decision must establish its own independently
approved evidence contract rather than inheriting AC07 ceremony.

## Consequences

- AC01, AC02, AC03, and AC06 may begin after AC00 acceptance, while integration remains serialized
  on `refactor/consolidation`.
- Identity-bearing and durable contracts can outlive implementation consolidation.
- Retaining a boundary is a valid outcome when evidence establishes its independent semantics.
- AC07 keeps binary architecture checks and threshold-free advisory conclusions without treating
  the repository as a release candidate.
- Architecture-consolidation changes use the same protected-branch checks and review policy as
  other maintained source changes.

## Rejected alternatives

- reducing class, Protocol, file, module, or import counts as ends in themselves;
- flattening feature boundaries into untyped dictionaries or a shared-model dumping ground;
- introducing dependency injection, service location, command-bus, workflow-framework, or
  speculative persistence hierarchies;
- merging model proposal, policy, authorization, execution, observation, evaluation, and accepted
  transition contracts because their fields look similar;
- silently inheriting runtime-v1 release evidence for architecture-consolidation source.
- reissuing project-wide source evidence after every ordinary change.

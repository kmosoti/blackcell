---
node: adr/0007-runtime-security-boundary
kind: adr
edges:
  decides:
    - architecture
    - migration-ledger
    - spec/bcp-0034-evolutionary-runtime
    - targets/containers
    - targets/recovery
  depends-on:
    - adr/0001-event-sourced-kernel
    - adr/0003-model-execution-boundary
    - adr/0005-durable-run-and-execution-protocol
---

# ADR 0007: Runtime Security and Data Boundary

Status: accepted

## Context

Runtime-v1 is about to expose the canonical workflow, replay, approvals, events, and scheduler
through an HTTP process. The existing local CLI stores data under a repository's Git directory and
assumes the invoking user is the trust boundary. Those defaults cannot be inherited by a service:
an implicit data location, credential in a tracked config or command line, permissive filesystem
mode, ambiguous proxy header, raw authorization failure, or telemetry export containing a secret
would create authority or disclosure outside the kernel's typed policy boundary.

The protected assets are the kernel database, artifact bytes, backups, service credential,
model/provider credentials, approval authority, execution authority, and recorded prompts or
outputs. The initial actors are one local service process, its worker process, an authenticated API
client, the host/container operator, and telemetry or backup adapters. Model output is
untrusted content and never becomes an identity or credential.

## Decision

### Data paths

Service startup requires an absolute `BLACKCELL_DATA_DIR`. There is no repository or current-working
directory fallback. The data, artifact, and reserved backup directories must be real directories,
owned by the service uid, mode `0700`, and not symlinks. An existing kernel database must be a real
owner-owned mode-`0600` file. Startup fails with a content-free code instead of repairing or
following an unsafe path.

### Credential source and authentication

Exactly one of `BLACKCELL_API_TOKEN` or `BLACKCELL_API_TOKEN_FILE` is required. A credential file
must be absolute, regular, non-symlinked, owned by the service uid, and exactly mode `0600`.
Credentials are bounded visible-ASCII opaque values with a minimum length and diversity check;
multiline, delimiter-bearing, placeholder, and ambiguous inputs fail closed. Secret objects redact
`str` and `repr` and verify candidates through a constant-time SHA-256 digest comparison.

The framework-neutral interface accepts exactly one `Authorization` value using the Bearer scheme.
Missing, duplicate, malformed, and invalid credentials have distinct bounded codes that never
include submitted content. Authentication yields one typed service principal with explicit
`read`, `run`, `approve`, and `admin` scopes. Authorization requires every declared route scope;
`admin` does not implicitly expand into other scopes. WP18 must preserve header multiplicity and
may exempt only explicitly documented liveness and readiness routes.

### Network and proxy defaults

The service defaults to `127.0.0.1:8080`. Bind hosts must be IP literals and ports must be bounded.
Authentication remains mandatory when binding loopback or a non-loopback address. Forwarded-client
trust is disabled (`BLACKCELL_TRUSTED_PROXY_HOPS=0`); arbitrary proxy chains are rejected until a
deployment-specific trusted-proxy contract exists. TLS termination is an external deployment
boundary and non-loopback plaintext exposure is not claimed safe.

### Redaction

Telemetry sanitization runs before in-memory recording and before exporter invocation. It redacts
sensitive-key variants, credential-shaped strings, private-key markers, URI user information, and
the exact configured service secret, including nested collections and exception messages. The
configured values are excluded from policy representation and equality. Content remains
metadata-only. OpenTelemetry export is disabled by default and requires an explicit,
credential-free OTLP/HTTP endpoint. The runtime supplies a fixed non-secret header map so ambient
OpenTelemetry endpoint and header configuration cannot redirect or decorate exports. Exporter
failure is content-free and cannot alter domain execution.

### Quota and recovery extension

WP22b extends this boundary with one process-local sliding request window consumed before
authentication on every protected route. Health routes are exempt. With proxy trust fixed at zero,
the service has no safe client-IP partition and therefore makes no per-client quota claim.

Active-storage admission measures SQLite, WAL/SHM, and artifacts, reserves mutation headroom, and
fails API/worker admission closed. Every production artifact store also enforces one exact aggregate
artifact-byte ceiling inside its SQLite write transaction. Backup bytes are excluded from active
admission and require host capacity monitoring.

Recovery bundles contain a consistent SQLite online snapshot, its exact immutable artifacts, and a
canonical manifest. Verification enforces owner-only non-symlink entries, exact inventory and hashes,
and SQLite integrity before retention or restore. Restore creates only an absent target and leaves
cutover to a stopped, explicit operator procedure.

## Threat and mitigation matrix

| Threat | Mitigation | Residual limit |
| --- | --- | --- |
| Repository or cwd redirects service state | required absolute owner-only data root | a compromised service uid can read its own data |
| Symlink or permissive-path substitution | `lstat`, no-follow credential open, uid and exact-mode checks | parent mount and host integrity belong to deployment |
| Credential appears in argv or tracked YAML | environment or owner-only credential file only | host root and process-environment readers remain trusted |
| Header smuggling or duplicate credentials | transport preserves multiplicity; exactly one strict Bearer value | WP18 must not comma-fold headers before authentication |
| Token timing oracle or brute-force pressure | constant-time digest comparison, content-free failures, and one pre-auth protected-route request window | the limit is process-local and global, not a distributed or per-client defense |
| Admin token gains undeclared route power | explicit scope subset checks; no implicit admin expansion | the initial token intentionally receives all declared scopes |
| Secret reaches telemetry or exception export | nested key/pattern/exact-value sanitization before storage/export | arbitrary unknown secrets need provider-specific policy additions |
| Spoofed forwarded client identity | trusted proxy hops fixed at zero | trusted reverse-proxy support is deferred |
| Container gains host or repository write authority | rootless engine, numeric non-root user, dropped capabilities, no-new-privileges, read-only root/repository, no engine socket | the invoking host uid and local container engine remain trusted |
| Container replacement or volume loss destroys state | persistent volume plus verified SQLite-and-artifact bundles and an external-copy restore drill | the operator must transport and protect a bundle off-volume |
| Corrupt or substituted backup enters service state | canonical manifest, exact hashes/inventory, mode and symlink checks, SQLite integrity, and verify-before-restore | bundles are not encrypted or authenticated against a compromised service uid |
| Storage exhaustion creates partial mutations | active-byte reserve, readiness/API/worker admission, and serialized exact artifact ceiling | admission is not a filesystem hard quota; backups need host capacity monitoring |
| Restore destroys a usable active root | absent-target-only staged restore and explicit stopped-process cutover | deleting old roots remains a separate operator-authorized action |
| Model output grants runtime authority | authentication is an interface concern; action policy remains deterministic | prompt injection still requires ongoing policy and evaluation tests |

## Consequences

- WP18-WP21 consume these contracts across HTTP, process, telemetry, and container edges rather
  than defining alternate auth, path, proxy, or redaction defaults.
- Provider credentials remain separate from the Blackcell service token and are never placed in
  the data directory, image, tracked configuration, or telemetry.
- WP22b adds bounded request/storage admission and tested local backup, retention, restore, and
  disaster-recovery evidence. It does not add TLS, external identity federation, token rotation,
  multi-tenant RBAC, offsite transport, bundle encryption, or filesystem hard quotas.
- The repository-local CLI remains a user-invoked compatibility surface. Service startup uses the
  explicit runtime configuration and does not inherit the CLI's Git-directory default.

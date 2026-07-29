---
node: adr/0010-semantic-presentation
kind: decision
edges:
  governs:
    - architecture
    - guides/runtime-quickstart
  refines:
    - adr/0009-project-runtime-scope
---

# ADR 0010: Project Runtime Meaning Through One Semantic Presentation Contract

- Status: Accepted
- Date: 2026-07-28

## Context

The first browser and terminal clients each rebuilt runtime meaning from low-level responses. That
duplicated projection rules, encouraged raw JSON as the default interface, and made request-field
omissions possible. It also gave client implementation details—such as a terminal cursor store—an
accidental persistence role. A visually richer client would have amplified the drift unless the
meaning boundary moved back to the daemon.

The interface must serve two readers at once. A human needs compact status, dependency, finding,
evidence, and artifact views. An agent needs stable identities, explicit field bindings,
provenance, and a closed action vocabulary. Decoration may evolve, but it cannot decide what a
run means or which request fields exist.

## Decision

### Project canonical state once

The Python daemon owns a strict Pydantic presentation surface derived from canonical runtime and
tooling contracts. It has stable component IDs, revision and event cursors, source identities and
digests, a closed discriminated component union, and an explicit disposition for every canonical
request field. Unknown content, dangling sections, duplicate actions, invalid graph references,
and unbounded values fail closed.

The presentation contract does not replace canonical msgspec request validation or event-sourced
state. It translates those authorities for clients. Conditional ETags identify deterministic
surface revisions. The ordered WebSocket feed only tells a client when to refetch.

### Render semantics, not agent-authored code

The browser uses native ES modules, custom elements, semantic HTML, and SVG. The Rust terminal uses
Ratatui. Both consume the same component and action vocabulary and hold credentials only in memory.
Neither client owns a scheduler, projection database, or durable event cursor. Raw JSON is loaded
only through explicit source disclosure. Run artifacts are returned only after membership, digest,
size, media type, and response-header checks.

Visual graphs supplement rather than replace structured meaning. Every plan graph has a semantic
table alternative. Findings retain P1/P2/P3 severity, and epistemic evidence retains its
disposition and source digest. Codex and Agy tooling inspection projects their shared facets,
material differences, and every tool-specific Pydantic leaf without normalizing away differences.

The token file follows the stable Design Tokens Community Group JSON format. A pure
A2UI-compatible exporter is kept as an interoperability seam because A2UI offers a declarative,
non-executable cross-client direction. The exporter has no authority to admit arbitrary components
or actions, and the evolving external protocol does not name executable BlackCell modules.

### Verify meaning across surfaces

One synthetic review-workflow scenario declares presentation surfaces, semantic manifests, action
fidelity, accessibility expectations, provenance, and failure codes. Python validates the entire
scenario. Browser unit tests and the Rust parser compare their manifests against it. Playwright
runs the human flow in Chromium, Firefox, and WebKit and applies axe checks. Ratatui's in-memory
test backend produces deterministic buffers at standard and narrow terminal sizes; property tests
exercise scrolling. A clean wheel test installs and invokes both Python commands and the native
terminal while checking packaged browser assets and the absence of the retired Python TUI.

Screenshots and traces are failure diagnostics with short retention, not source-bound proof. No
real run trace, generated browser distribution, or per-change evidence bundle is committed.

## Consequences

- New UI components begin as a host-owned contract and must gain browser, terminal, and semantic
  parity coverage before use.
- Decorative iteration stays cheap because tokens and renderers can change without changing
  runtime meaning.
- The Rust binary makes wheels platform-specific; the initial claimed target is Linux x86-64.
- The package uses `setuptools-rust`: Maturin binary mode cannot coexist with the package's Python
  console-script entry points, while `setuptools-rust` supports a Rust executable in the same
  wheel.
- Browser clients remain useful with JavaScript and CSS alone; no production framework or asset
  compilation pipeline is introduced.
- An accessibility tool can detect many structural defects but cannot replace keyboard, semantic,
  contrast, and human review. The scenario matrix therefore combines multiple independent checks.

## Rejected alternatives

- let each client project raw runtime responses independently;
- make JSON the primary human interface;
- execute HTML, JavaScript, or components authored by an agent;
- add a browser database, CRDT, service-worker cache, or terminal cursor store;
- adopt a production JavaScript framework before interaction complexity requires it;
- render a graph without an equivalent semantic table;
- commit screenshots or regenerated evidence bundles after ordinary source changes;
- treat model confidence or visual polish as verification evidence.

## Primary references

- [Design Tokens Format Module 2025.10](https://www.w3.org/community/reports/design-tokens/CG-FINAL-format-20251028/)
- [A2UI protocol](https://a2ui.org/)
- [W3C accessible SVG guidance](https://www.w3.org/TR/SVG-access/)
- [W3C accessible tables tutorial](https://www.w3.org/WAI/tutorials/tables/)
- [Ratatui `TestBackend`](https://docs.rs/ratatui/latest/ratatui/backend/struct.TestBackend.html)
- [Playwright WebSocket routing](https://playwright.dev/docs/api/class-page#page-route-web-socket)

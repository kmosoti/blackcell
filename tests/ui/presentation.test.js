import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  buildRequest,
  semanticManifest,
  validateSurface,
} from "../../src/blackcell/interfaces/http/assets/ui/runtime-client.js";
import { graphLayout } from "../../src/blackcell/interfaces/http/assets/ui/surface-elements.js";

const digest = `sha256:${"a".repeat(64)}`;
const reviewScenario = JSON.parse(
  await readFile(new URL("review-workflow.json", import.meta.url), "utf8"),
);

function surface() {
  return {
    schema_version: "presentation-surface/v1",
    surface_id: "workspace",
    title: "BlackCell project runtime",
    revision: { number: 4, event_cursor: 4, source_digest: digest },
    components: [
      {
        component_id: "plan-form",
        kind: "form",
        label: "Accept plan",
        source: { kind: "contract", identity: "plan", digest, json_pointer: null },
        description: "Typed plan",
        action: {
          action_id: "accept-plan",
          operation: "accept-plan",
          label: "Accept plan",
          request_schema: "plan-request/v1",
          confirmation: null,
          fields: [
            {
              field_id: "planning-mode",
              json_pointer: "/planning_mode",
              label: "Planning mode",
              help: "",
              control: "select",
              required: true,
              sensitive: false,
              default: "declared",
              options: [
                { value: "declared", label: "Declared" },
                { value: "generated", label: "Generated" },
              ],
            },
          ],
        },
      },
      {
        component_id: "runs",
        kind: "table",
        label: "Recent runs",
        source: null,
        columns: [{ key: "run", label: "Run" }],
        rows: [],
        empty_message: "No runs.",
      },
    ],
    field_dispositions: [],
  };
}

test("generic surface validation and semantic manifest preserve host meaning", () => {
  const value = surface();

  assert.equal(validateSurface(value), value);
  assert.deepEqual(semanticManifest(value), [
    {
      id: "plan-form",
      kind: "form",
      label: "Accept plan",
      sourceDigest: digest,
      action: "accept-plan",
    },
    {
      id: "runs",
      kind: "table",
      label: "Recent runs",
      sourceDigest: null,
      action: null,
    },
  ]);
});

test("plan request construction retains planning_mode without a client schema allowlist", () => {
  const action = surface().components[0].action;

  assert.deepEqual(buildRequest(action, { planning_mode: "generated" }), {
    schema_version: "plan-request/v1",
    planning_mode: "generated",
  });
});

test("surface validation rejects unknown components and dangling sections", () => {
  const unknown = surface();
  unknown.components[0].kind = "agent-script";
  assert.throws(() => validateSurface(unknown), /invalid-presentation-component/);

  const dangling = surface();
  dangling.components.push({
    component_id: "section",
    kind: "section",
    label: "Section",
    source: null,
    description: "",
    children: ["missing"],
  });
  assert.throws(() => validateSurface(dangling), /invalid-section-binding/);
});

test("plan layout is deterministic and dependency ordered", () => {
  const nodes = [
    { node_id: "inspect" },
    { node_id: "implement" },
    { node_id: "verify" },
  ];
  const edges = [
    { source_id: "inspect", target_id: "implement" },
    { source_id: "implement", target_id: "verify" },
  ];

  assert.deepEqual(graphLayout(nodes, edges), [
    { nodeId: "inspect", x: 24, y: 24 },
    { nodeId: "implement", x: 284, y: 24 },
    { nodeId: "verify", x: 544, y: 24 },
  ]);
});

test("shared review scenario has exact browser semantic and action parity", () => {
  for (const expectation of reviewScenario.surface_expectations) {
    const selected = reviewScenario.surfaces.find(
      (surfaceValue) => surfaceValue.surface_id === expectation.surface_id,
    );
    assert.ok(selected);
    assert.deepEqual(
      semanticManifest(selected),
      expectation.manifest.map((item) => ({
        id: item.component_id,
        kind: item.kind,
        label: item.label,
        sourceDigest: item.source_digest,
        action: item.action,
      })),
    );
  }

  for (const expectation of reviewScenario.action_expectations) {
    const selected = reviewScenario.surfaces.find(
      (surfaceValue) => surfaceValue.surface_id === expectation.surface_id,
    );
    const component = selected.components.find(
      (item) => item.action?.action_id === expectation.action_id,
    );
    const request = buildRequest(component.action, expectation.submitted_values);
    assert.deepEqual(
      Object.fromEntries(
        Object.keys(expectation.expected_request_subset).map((key) => [key, request[key]]),
      ),
      expectation.expected_request_subset,
    );
    if (component.action.operation === "cancel-run") {
      assert.match(request.idempotency_key, /^web-cancel-[0-9a-f-]{36}$/);
    }
  }
});

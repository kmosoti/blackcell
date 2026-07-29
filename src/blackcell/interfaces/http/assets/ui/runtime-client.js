"use strict";

const MAX_RESPONSE_BYTES = 16_777_216;
const MAX_ARTIFACT_BYTES = 8_388_608;
const PRESENTATION_MEDIA_TYPE = "application/vnd.blackcell.presentation+json";
const IDENTIFIER = /^[A-Za-z0-9._-]{1,120}$/;
const DIGEST = /^sha256:[a-f0-9]{64}$/;
const SAFE_ERROR = /^[A-Za-z0-9._-]{1,100}$/;
const TICKET = /^[A-Za-z0-9_-]{32,128}$/;
const COMPONENT_KINDS = new Set([
  "section",
  "status",
  "metrics",
  "key-value",
  "table",
  "form",
  "plan-graph",
  "timeline",
  "findings",
  "evidence-matrix",
  "artifacts",
  "source",
]);
const OPERATION_CONTRACTS = Object.freeze({
  "register-project": ["POST", "/api/v1/projects", "project-request/v1"],
  "accept-intent": ["POST", "/api/v1/intents", "intent-request/v1"],
  "accept-plan": ["POST", "/api/v1/plans", "plan-request/v1"],
  "submit-run": ["POST", "/api/v1/runs", "run-request/v1"],
  "cancel-run": ["POST", null, "execution-cancel-run-request/v1"],
});
const ACTION_SCHEMAS = Object.freeze({
  ...Object.fromEntries(
    Object.entries(OPERATION_CONTRACTS).map(([operation, contract]) => [operation, contract[2]]),
  ),
  "inspect-run": "run-lookup/v1",
});

export class RuntimeApiClient {
  constructor(token) {
    if (
      typeof token !== "string" ||
      token.length < 32 ||
      token.length > 4096 ||
      token.includes(",") ||
      [...token].some((character) => character < "!" || character > "~")
    ) {
      throw new Error("invalid-credential");
    }
    this._token = token;
  }

  clear() {
    this._token = "";
  }

  async workspaceSurface() {
    return validateSurface(await this._json("/api/v1/ui/surfaces/workspace", {
      accept: PRESENTATION_MEDIA_TYPE,
    }));
  }

  async runSurface(runId) {
    const selected = validIdentifier(runId);
    return validateSurface(await this._json(
      `/api/v1/ui/surfaces/runs/${encodeURIComponent(selected)}`,
      { accept: PRESENTATION_MEDIA_TYPE },
    ));
  }

  async executeAction(action, values, subjectId = null) {
    if (!(action.operation in OPERATION_CONTRACTS)) {
      throw new Error("unsupported-action");
    }
    const [method, declaredPath] = OPERATION_CONTRACTS[action.operation];
    const request = buildRequest(action, values);
    const path = action.operation === "cancel-run"
      ? `/api/v1/runs/${encodeURIComponent(validIdentifier(subjectId))}/cancel`
      : declaredPath;
    return this._json(path, { method, body: JSON.stringify(request) });
  }

  async issueSocketTicket() {
    const value = await this._json("/api/v1/ui/socket-tickets", { method: "POST" });
    if (
      !isObject(value) ||
      value.schema_version !== "execution-web-socket-ticket/v1" ||
      typeof value.ticket !== "string" ||
      !TICKET.test(value.ticket) ||
      !Number.isInteger(value.expires_in_seconds) ||
      value.expires_in_seconds < 1 ||
      value.expires_in_seconds > 60 ||
      value.websocket_path !== "/api/v1/ui/events"
    ) {
      throw new Error("invalid-ticket-response");
    }
    return value;
  }

  socketUrl(ticket, cursor) {
    if (!Number.isSafeInteger(cursor) || cursor < 0) {
      throw new Error("invalid-event-cursor");
    }
    const url = new URL(ticket.websocket_path, window.location.origin);
    url.protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    url.searchParams.set("ticket", ticket.ticket);
    url.searchParams.set("after", String(cursor));
    return url;
  }

  async fetchSource(operation, subjectId) {
    const selected = validIdentifier(subjectId);
    const suffix = operation === "inspect-run" ? "status" : "replay";
    const value = await this._json(`/api/v1/runs/${encodeURIComponent(selected)}/${suffix}`);
    return JSON.stringify(value, null, 2);
  }

  async fetchArtifact(runId, artifact) {
    const selected = validIdentifier(runId);
    if (!isObject(artifact) || typeof artifact.digest !== "string" || !DIGEST.test(artifact.digest)) {
      throw new Error("invalid-artifact-binding");
    }
    const response = await this._request(
      `/api/v1/runs/${encodeURIComponent(selected)}/artifacts/${encodeURIComponent(artifact.digest)}`,
      { accept: "*/*" },
    );
    if (!response.ok) {
      throw new Error(await responseError(response));
    }
    const declared = boundedContentLength(response, MAX_ARTIFACT_BYTES);
    const bytes = await response.arrayBuffer();
    if (bytes.byteLength > MAX_ARTIFACT_BYTES || (declared !== null && declared !== bytes.byteLength)) {
      throw new Error("invalid-artifact-size");
    }
    const mediaType = response.headers.get("content-type")?.split(";", 1)[0].trim().toLowerCase();
    if (mediaType === "application/json" || mediaType === "text/plain" || mediaType === "text/markdown") {
      return {
        kind: "text",
        mediaType,
        text: new TextDecoder("utf-8", { fatal: true }).decode(bytes),
      };
    }
    return { kind: "binary", mediaType: mediaType || "application/octet-stream", bytes };
  }

  async _json(path, options = {}) {
    const response = await this._request(path, options);
    const mediaType = response.headers.get("content-type")?.split(";", 1)[0].trim().toLowerCase();
    const expected = response.ok ? options.accept || "application/json" : "application/json";
    if (mediaType !== expected) {
      throw new Error("invalid-response-media-type");
    }
    const declared = boundedContentLength(response, MAX_RESPONSE_BYTES);
    const bytes = await response.arrayBuffer();
    if (
      bytes.byteLength < 1 ||
      bytes.byteLength > MAX_RESPONSE_BYTES ||
      (declared !== null && declared !== bytes.byteLength)
    ) {
      throw new Error("invalid-response-size");
    }
    let value;
    try {
      value = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
    } catch {
      throw new Error("invalid-json-response");
    }
    if (!response.ok) {
      const code = isObject(value) && typeof value.error === "string" && SAFE_ERROR.test(value.error)
        ? value.error
        : `request-failed-${response.status}`;
      throw new Error(code);
    }
    return value;
  }

  _request(path, options = {}) {
    if (typeof path !== "string" || !path.startsWith("/api/v1/") || path.includes("..")) {
      throw new Error("invalid-client-path");
    }
    const headers = new Headers({
      Accept: options.accept || "application/json",
      Authorization: `Bearer ${this._token}`,
    });
    if (options.body !== undefined) {
      headers.set("Content-Type", "application/json");
    }
    return fetch(new URL(path, window.location.origin), {
      method: options.method || "GET",
      body: options.body,
      headers,
      cache: "no-store",
      credentials: "omit",
      redirect: "error",
    });
  }
}

export function buildRequest(action, values) {
  if (!isObject(action) || !(action.operation in OPERATION_CONTRACTS) || !isObject(values)) {
    throw new Error("invalid-action-binding");
  }
  const expectedSchema = OPERATION_CONTRACTS[action.operation][2];
  if (action.request_schema !== expectedSchema || !Array.isArray(action.fields)) {
    throw new Error("invalid-action-binding");
  }
  if (action.operation === "cancel-run") {
    return {
      schema_version: expectedSchema,
      idempotency_key: `web-cancel-${crypto.randomUUID()}`,
    };
  }
  const request = { schema_version: expectedSchema };
  for (const field of action.fields) {
    if (
      !isObject(field) ||
      typeof field.json_pointer !== "string" ||
      !/^\/[A-Za-z0-9_]+$/.test(field.json_pointer)
    ) {
      throw new Error("invalid-field-binding");
    }
    const key = field.json_pointer.slice(1);
    if (["__proto__", "constructor", "prototype"].includes(key)) {
      throw new Error("invalid-field-binding");
    }
    if (!(key in values)) {
      if (field.required) {
        throw new Error("missing-action-field");
      }
      continue;
    }
    request[key] = values[key];
  }
  return request;
}

export function validateSurface(value) {
  if (
    !isObject(value) ||
    !hasExactKeys(value, ["schema_version", "surface_id", "title", "revision", "components", "field_dispositions"]) ||
    value.schema_version !== "presentation-surface/v1" ||
    typeof value.surface_id !== "string" ||
    !presentationSurfaceId(value.surface_id) ||
    typeof value.title !== "string" ||
    value.title.length < 1 ||
    value.title.length > 240 ||
    !isObject(value.revision) ||
    !hasExactKeys(value.revision, ["number", "event_cursor", "source_digest"]) ||
    !Number.isSafeInteger(value.revision.number) ||
    value.revision.number < 0 ||
    !Number.isSafeInteger(value.revision.event_cursor) ||
    value.revision.event_cursor < 0 ||
    typeof value.revision.source_digest !== "string" ||
    !DIGEST.test(value.revision.source_digest) ||
    !Array.isArray(value.components) ||
    value.components.length > 512 ||
    !Array.isArray(value.field_dispositions) ||
    value.field_dispositions.length > 512
  ) {
    throw new Error("invalid-presentation-surface");
  }
  const identifiers = new Set();
  const actionIds = new Set();
  for (const component of value.components) {
    if (
      !isObject(component) ||
      typeof component.kind !== "string" ||
      !COMPONENT_KINDS.has(component.kind) ||
      typeof component.component_id !== "string" ||
      !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$/.test(component.component_id) ||
      identifiers.has(component.component_id) ||
      typeof component.label !== "string" ||
      component.label.length < 1 ||
      component.label.length > 240
    ) {
      throw new Error("invalid-presentation-component");
    }
    validateComponent(component);
    if (component.kind === "form") {
      if (actionIds.has(component.action.action_id)) {
        throw new Error("invalid-action-binding");
      }
      actionIds.add(component.action.action_id);
    }
    identifiers.add(component.component_id);
  }
  for (const component of value.components) {
    if (
      component.kind === "section" &&
      (!Array.isArray(component.children) || component.children.some((child) => !identifiers.has(child)))
    ) {
      throw new Error("invalid-section-binding");
    }
  }
  const dispositionKeys = new Set();
  for (const disposition of value.field_dispositions) {
    if (
      !isObject(disposition) ||
      !hasExactKeys(disposition, ["contract", "json_pointer", "disposition", "reason"]) ||
      !boundedText(disposition.contract, 1, 120) ||
      !jsonPointer(disposition.json_pointer) ||
      !["editable", "displayed", "derived", "hidden"].includes(disposition.disposition) ||
      !boundedText(disposition.reason, 1, 500)
    ) {
      throw new Error("invalid-field-disposition");
    }
    const key = `${disposition.contract}\0${disposition.json_pointer}`;
    if (dispositionKeys.has(key)) {
      throw new Error("invalid-field-disposition");
    }
    dispositionKeys.add(key);
  }
  const byId = new Map(value.components.map((component) => [component.component_id, component]));
  const visiting = new Set();
  const visited = new Set();
  function visit(componentId) {
    if (visiting.has(componentId)) {
      throw new Error("invalid-section-binding");
    }
    if (visited.has(componentId)) {
      return;
    }
    visiting.add(componentId);
    const component = byId.get(componentId);
    if (component.kind === "section") {
      for (const child of component.children) {
        visit(child);
      }
    }
    visiting.delete(componentId);
    visited.add(componentId);
  }
  for (const component of value.components) {
    visit(component.component_id);
  }
  return value;
}

function validateComponent(component) {
  const common = ["kind", "component_id", "label", "source"];
  validateSource(component.source);
  switch (component.kind) {
    case "section":
      requireExactComponent(component, [...common, "description", "children"]);
      if (
        !boundedText(component.description, 0, 2000) ||
        !boundedArray(component.children, 0, 128) ||
        component.children.some((item) => !presentationId(item)) ||
        new Set(component.children).size !== component.children.length ||
        component.children.includes(component.component_id)
      ) invalidComponent();
      return;
    case "status":
      requireExactComponent(component, [...common, "value", "tone", "detail"]);
      if (
        !boundedText(component.value, 1, 120) ||
        !["neutral", "info", "success", "warning", "danger"].includes(component.tone) ||
        !boundedText(component.detail, 0, 2000)
      ) invalidComponent();
      return;
    case "metrics":
      requireExactComponent(component, [...common, "metrics"]);
      if (
        !boundedArray(component.metrics, 1, 32) ||
        component.metrics.some((item) => !isObject(item) ||
          !hasExactKeys(item, ["label", "value", "unit"]) ||
          !boundedText(item.label, 1, 120) ||
          !presentationScalar(item.value) ||
          !boundedText(item.unit, 0, 40))
      ) invalidComponent();
      return;
    case "key-value":
      requireExactComponent(component, [...common, "items"]);
      if (
        !boundedArray(component.items, 0, 4096) ||
        component.items.some((item) => !isObject(item) ||
          !hasExactKeys(item, ["key", "value", "provenance"]) ||
          !boundedText(item.key, 1, 240) ||
          !presentationScalar(item.value) ||
          !boundedText(item.provenance, 0, 500))
      ) invalidComponent();
      return;
    case "table":
      validateTable(component, common);
      return;
    case "form":
      requireExactComponent(component, [...common, "description", "action"]);
      if (!boundedText(component.description, 0, 2000)) invalidComponent();
      validateAction(component.action);
      return;
    case "plan-graph":
      validateGraph(component, common);
      return;
    case "timeline":
      requireExactComponent(component, [...common, "items"]);
      if (
        !boundedArray(component.items, 0, 4096) ||
        component.items.some((item) => !isObject(item) ||
          !hasExactKeys(item, ["item_id", "title", "detail", "cursor", "status"]) ||
          !presentationId(item.item_id) ||
          !boundedText(item.title, 1, 240) ||
          !boundedText(item.detail, 0, 2000) ||
          !(item.cursor === null || (Number.isSafeInteger(item.cursor) && item.cursor >= 0)) ||
          !boundedText(item.status, 0, 120))
      ) invalidComponent();
      return;
    case "findings":
      requireExactComponent(component, [...common, "findings"]);
      if (
        !boundedArray(component.findings, 0, 4096) ||
        component.findings.some((item) => !isObject(item) ||
          !hasExactKeys(item, ["finding_id", "severity", "summary", "evidence"]) ||
          !presentationId(item.finding_id) ||
          !["P1", "P2", "P3", "info"].includes(item.severity) ||
          !boundedText(item.summary, 1, 2000) ||
          !boundedText(item.evidence, 0, 2000))
      ) invalidComponent();
      return;
    case "evidence-matrix":
      requireExactComponent(component, [...common, "rows"]);
      if (
        !boundedArray(component.rows, 0, 256) ||
        component.rows.some((item) => !isObject(item) ||
          !hasExactKeys(item, ["row_id", "dimension", "disposition", "evidence", "source_digest"]) ||
          !presentationId(item.row_id) ||
          !boundedText(item.dimension, 1, 240) ||
          !["supported", "concern", "unknown", "not-applicable"].includes(item.disposition) ||
          !boundedText(item.evidence, 0, 2000) ||
          typeof item.source_digest !== "string" || !DIGEST.test(item.source_digest))
      ) invalidComponent();
      return;
    case "artifacts":
      requireExactComponent(component, [...common, "run_id", "items"]);
      if (
        !boundedText(component.run_id, 1, 120) ||
        !boundedArray(component.items, 0, 4096) ||
        component.items.some((item) => !isObject(item) ||
          !hasExactKeys(item, ["digest", "role", "node_id", "media_type", "size_bytes", "verified"]) ||
          typeof item.digest !== "string" || !DIGEST.test(item.digest) ||
          !boundedText(item.role, 1, 120) ||
          !boundedText(item.node_id, 1, 120) ||
          !boundedText(item.media_type, 1, 240) ||
          !Number.isSafeInteger(item.size_bytes) || item.size_bytes < 0 ||
          typeof item.verified !== "boolean")
      ) invalidComponent();
      return;
    case "source":
      requireExactComponent(component, [...common, "operation", "subject_id", "summary"]);
      if (
        !["inspect-run", "replay-run"].includes(component.operation) ||
        !boundedText(component.subject_id, 1, 120) ||
        !boundedText(component.summary, 0, 32768)
      ) invalidComponent();
      return;
    default:
      invalidComponent();
  }
}

function validateTable(component, common) {
  requireExactComponent(component, [...common, "columns", "rows", "empty_message"]);
  if (
    !boundedArray(component.columns, 1, 64) ||
    !boundedArray(component.rows, 0, 4096) ||
    !boundedText(component.empty_message, 1, 500)
  ) invalidComponent();
  const keys = component.columns.map((column) => {
    if (
      !isObject(column) ||
      !hasExactKeys(column, ["key", "label"]) ||
      !presentationId(column.key) ||
      !boundedText(column.label, 1, 240)
    ) invalidComponent();
    return column.key;
  });
  if (new Set(keys).size !== keys.length) invalidComponent();
  const rowIds = new Set();
  for (const row of component.rows) {
    if (
      !isObject(row) ||
      !hasExactKeys(row, ["row_id", "cells"]) ||
      !presentationId(row.row_id) ||
      rowIds.has(row.row_id) ||
      !isObject(row.cells) ||
      Object.keys(row.cells).sort().join("\0") !== [...keys].sort().join("\0") ||
      Object.values(row.cells).some((value) => !presentationScalar(value))
    ) invalidComponent();
    rowIds.add(row.row_id);
  }
}

function validateGraph(component, common) {
  requireExactComponent(component, [...common, "nodes", "edges"]);
  if (!boundedArray(component.nodes, 0, 64) || !boundedArray(component.edges, 0, 4096)) {
    invalidComponent();
  }
  const nodes = new Set();
  for (const node of component.nodes) {
    if (
      !isObject(node) ||
      !hasExactKeys(node, ["node_id", "label", "status", "detail"]) ||
      !presentationId(node.node_id) ||
      nodes.has(node.node_id) ||
      !boundedText(node.label, 1, 240) ||
      !boundedText(node.status, 0, 120) ||
      !boundedText(node.detail, 0, 2000)
    ) invalidComponent();
    nodes.add(node.node_id);
  }
  if (component.edges.some((edge) => !isObject(edge) ||
    !hasExactKeys(edge, ["source_id", "target_id", "label"]) ||
    !nodes.has(edge.source_id) || !nodes.has(edge.target_id) ||
    !boundedText(edge.label, 0, 120))) invalidComponent();
}

export function semanticManifest(surface) {
  validateSurface(surface);
  return surface.components.map((component) => ({
    id: component.component_id,
    kind: component.kind,
    label: component.label,
    sourceDigest: component.source?.digest || null,
    action: component.kind === "form" ? component.action.operation : null,
  }));
}

function validateAction(action) {
  if (
    !isObject(action) ||
    !hasExactKeys(action, ["action_id", "operation", "label", "request_schema", "fields", "confirmation"]) ||
    typeof action.operation !== "string" ||
    !(action.operation in ACTION_SCHEMAS) ||
    !presentationId(action.action_id) ||
    !boundedText(action.label, 1, 120) ||
    !boundedText(action.request_schema, 1, 120) ||
    !Array.isArray(action.fields) ||
    action.fields.length > 128 ||
    !(action.confirmation === null || boundedText(action.confirmation, 0, 500))
  ) {
    throw new Error("invalid-action-binding");
  }
  if (
    action.request_schema !== ACTION_SCHEMAS[action.operation]
  ) {
    throw new Error("invalid-action-binding");
  }
  const fieldIds = new Set();
  const pointers = new Set();
  for (const field of action.fields) {
    if (
      !isObject(field) ||
      !hasExactKeys(field, ["field_id", "json_pointer", "label", "help", "control", "required", "sensitive", "default", "options"]) ||
      !presentationId(field.field_id) ||
      !jsonPointer(field.json_pointer) ||
      fieldIds.has(field.field_id) ||
      pointers.has(field.json_pointer) ||
      !boundedText(field.label, 1, 240) ||
      !boundedText(field.help, 0, 1000) ||
      !["text", "textarea", "number", "checkbox", "select", "string-list", "structured-list"].includes(field.control) ||
      typeof field.required !== "boolean" ||
      typeof field.sensitive !== "boolean" ||
      !boundedArray(field.options, 0, 64) ||
      field.options.some((option) => !isObject(option) ||
        !hasExactKeys(option, ["value", "label"]) ||
        !boundedText(option.value, 0, 2048) ||
        !boundedText(option.label, 1, 240)) ||
      ((field.control === "select") !== (field.options.length > 0))
    ) {
      throw new Error("invalid-action-binding");
    }
    fieldIds.add(field.field_id);
    pointers.add(field.json_pointer);
  }
}

function validateSource(source) {
  if (source === null) return;
  if (
    !isObject(source) ||
    !hasExactKeys(source, ["kind", "identity", "digest", "json_pointer"]) ||
    !["contract", "event", "run", "artifact", "tooling"].includes(source.kind) ||
    !boundedText(source.identity, 1, 240) ||
    typeof source.digest !== "string" ||
    !DIGEST.test(source.digest) ||
    !(source.json_pointer === null || boundedText(source.json_pointer, 0, 1024))
  ) invalidComponent();
}

function requireExactComponent(component, keys) {
  if (!hasExactKeys(component, keys)) invalidComponent();
}

function invalidComponent() {
  throw new Error("invalid-presentation-component");
}

function boundedArray(value, minimum, maximum) {
  return Array.isArray(value) && value.length >= minimum && value.length <= maximum;
}

function boundedText(value, minimum, maximum) {
  if (typeof value !== "string") return false;
  let length = 0;
  for (const _character of value) {
    length += 1;
    if (length > maximum) return false;
  }
  return length >= minimum;
}

function presentationId(value) {
  return typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$/.test(value);
}

function presentationSurfaceId(value) {
  return presentationId(value) || (
    typeof value === "string" && value.startsWith("run:") && presentationId(value.slice(4))
  );
}

function jsonPointer(value) {
  return boundedText(value, 1, 1024) && value.startsWith("/");
}

function presentationScalar(value) {
  return value === null || typeof value === "string" || typeof value === "boolean" ||
    (typeof value === "number" && Number.isFinite(value));
}

function hasExactKeys(value, expected) {
  const actual = Object.keys(value).sort();
  const orderedExpected = [...expected].sort();
  return actual.length === expected.length &&
    actual.every((key, index) => key === orderedExpected[index]);
}

function boundedContentLength(response, maximum) {
  const value = response.headers.get("content-length");
  if (value === null) {
    return null;
  }
  if (!/^[0-9]+$/.test(value)) {
    throw new Error("invalid-response-size");
  }
  const size = Number(value);
  if (!Number.isSafeInteger(size) || size > maximum) {
    throw new Error("response-too-large");
  }
  return size;
}

async function responseError(response) {
  try {
    const value = await response.json();
    return isObject(value) && typeof value.error === "string" && SAFE_ERROR.test(value.error)
      ? value.error
      : `request-failed-${response.status}`;
  } catch {
    return `request-failed-${response.status}`;
  }
}

function validIdentifier(value) {
  if (typeof value !== "string" || !IDENTIFIER.test(value)) {
    throw new Error("invalid-run-id");
  }
  return value;
}

function isObject(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function safeMessage(error) {
  if (error instanceof Error && SAFE_ERROR.test(error.message)) {
    return error.message.replaceAll("-", " ");
  }
  return "operation failed";
}

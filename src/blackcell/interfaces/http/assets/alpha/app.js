"use strict";

const MAX_RESPONSE_BYTES = 1_100_000;
const MAX_REQUEST_BYTES = 1_048_576;
const MAX_RETAINED_EVENTS = 200;
const MAX_RUN_ID_CHARS = 120;
const MAX_COLLECTION_ITEMS = 64;
const MAX_PLAN_NODES = 64;
const MAX_CHECK_ARGV = 32;
const MAX_ACCEPTANCE_TIMEOUT_SECONDS = 600;
const EVENT_PAGE_LIMIT = 100;
const RUN_ID = /^[A-Za-z0-9._-]+$/;
const SAFE_ERROR = /^[A-Za-z0-9._-]{1,100}$/;
const TICKET = /^[A-Za-z0-9_-]{32,128}$/;
const IDENTIFIER = /^[A-Za-z0-9._-]{1,120}$/;
const EXECUTABLE_ALIAS = /^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$/;
const DIGEST = /^sha256:[a-f0-9]{64}$/;
const COMMIT = /^[a-f0-9]{40}$/;
const EFFECTS = new Set(["network", "process", "repository-read", "repository-write"]);
const RUN_STATUSES = new Set([
  "queued",
  "running",
  "canceling",
  "canceled",
  "succeeded",
  "failed",
  "reconciliation-required",
]);
const EVENT_TYPES = new Set([
  "alpha.project.registered",
  "alpha.intent.accepted",
  "alpha.plan.accepted",
  "alpha.run.queued",
  "alpha.node.claimed",
  "alpha.node.worktree-prepared",
  "alpha.node.provider-dispatch-started",
  "alpha.run.cancel-requested",
  "alpha.node.succeeded",
  "alpha.node.failed",
  "alpha.node.requeued",
  "alpha.node.canceled",
  "alpha.node.reconciliation-required",
  "alpha.node.worktree-cleanup-requested",
  "alpha.node.worktree-cleaned",
  "alpha.node.worktree-cleanup-failed",
  "alpha.run.succeeded",
  "alpha.run.failed",
  "alpha.run.canceled",
  "alpha.run.reconciliation-required",
  "alpha.review.claimed",
  "alpha.review.provider-dispatch-started",
  "alpha.review.succeeded",
  "alpha.review.failed",
  "alpha.review.requeued",
  "alpha.review.reconciliation-required",
  "alpha.verification.claimed",
  "alpha.verification.completed",
  "alpha.verification.failed",
  "alpha.verification.requeued",
]);
const EVENT_PAGE_KEYS = [
  "after_cursor",
  "events",
  "has_more",
  "limit",
  "next_cursor",
  "scanned_events",
  "schema_version",
];
const EVENT_KEYS = [
  "actor",
  "causation_id",
  "correlation_id",
  "cursor",
  "event_id",
  "event_schema_version",
  "event_type",
  "payload",
  "payload_digest",
  "recorded_at",
  "schema_version",
  "stream_id",
  "stream_sequence",
];
const PROJECT_REQUEST_KEYS = [
  "configuration_digest",
  "configuration_provider",
  "configuration_version",
  "idempotency_key",
  "project_id",
  "root",
  "schema_version",
];
const INTENT_REQUEST_KEYS = [
  "assumptions",
  "constraints",
  "idempotency_key",
  "intent_id",
  "objective",
  "project_id",
  "schema_version",
  "unresolved_questions",
];
const PLAN_REQUEST_KEYS = [
  "allowed_effects",
  "base_commit",
  "idempotency_key",
  "intent_id",
  "nodes",
  "plan_id",
  "project_id",
  "schema_version",
];
const RUN_REQUEST_KEYS = [
  "idempotency_key",
  "intent_id",
  "plan_id",
  "project_id",
  "run_id",
  "schema_version",
];
const PLAN_NODE_KEYS = [
  "allowed_paths",
  "budget",
  "checks",
  "depends_on",
  "effects",
  "node_id",
  "objective",
];
const PLAN_BUDGET_KEYS = [
  "max_changed_files",
  "max_cost_microusd",
  "max_input_tokens",
  "max_output_tokens",
  "timeout_seconds",
];
const ACCEPTANCE_CHECK_KEYS = ["argv", "check_id", "expected_exit_code"];
const PROJECT_RESPONSE_KEYS = [
  "configuration_digest",
  "configuration_provider",
  "configuration_version",
  "cursor",
  "event_digest",
  "event_id",
  "principal_id",
  "project_id",
  "root",
  "schema_version",
];
const INTENT_RESPONSE_KEYS = [
  "assumptions",
  "constraints",
  "cursor",
  "event_digest",
  "event_id",
  "intent_id",
  "objective",
  "principal_id",
  "project_id",
  "schema_version",
  "unresolved_questions",
];
const PLAN_RESPONSE_KEYS = [
  "allowed_effects",
  "base_commit",
  "cursor",
  "event_digest",
  "event_id",
  "intent_id",
  "nodes",
  "plan_id",
  "principal_id",
  "project_id",
  "schema_version",
  "topological_order",
];
const RUN_KEYS = [
  "active_node_id",
  "attempt",
  "cancellation_requested",
  "cursor",
  "event_digest",
  "event_id",
  "fencing_token",
  "intent_id",
  "plan_id",
  "principal_id",
  "project_id",
  "retained_worktree",
  "run_id",
  "schema_version",
  "status",
];
const REPLAY_KEYS = [
  "artifact_evidence_digest",
  "artifact_integrity",
  "artifacts",
  "findings",
  "intent",
  "plan",
  "processed_events",
  "project",
  "run",
  "run_id",
  "schema_version",
  "state_digest",
  "verification",
];
const TICKET_KEYS = ["expires_in_seconds", "schema_version", "ticket", "websocket_path"];

const elements = {
  connectionForm: required("connection-form"),
  token: required("api-token"),
  connect: required("connect-button"),
  disconnect: required("disconnect-button"),
  connectionStatus: required("connection-status"),
  connectionMessage: required("connection-message"),
  cursor: required("cursor-display"),
  eventRows: required("event-rows"),
  eventEmpty: required("event-empty"),
  eventCount: required("event-count"),
  runForm: required("run-form"),
  runId: required("run-id"),
  operationMessage: required("operation-message"),
  operationOutput: required("operation-output"),
  workflowForm: required("workflow-form"),
  workflowOperation: required("workflow-operation"),
  workflowFile: required("workflow-file"),
  workflowSubmit: required("workflow-submit"),
  workflowMessage: required("workflow-message"),
  workflowOutput: required("workflow-output"),
};

const state = {
  client: null,
  socket: null,
  cursor: 0,
  events: [],
  reconnectAttempt: 0,
  reconnectTimer: null,
  generation: 0,
  wanted: false,
  workflowAbort: null,
  workflowBusy: false,
};

class AlphaApiClient {
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

  async issueSocketTicket() {
    const value = await this._request("/api/alpha/v1/ui/socket-tickets", { method: "POST" });
    if (
      !isObject(value) ||
      !hasExactKeys(value, TICKET_KEYS) ||
      value.schema_version !== "alpha-web-socket-ticket/v1" ||
      typeof value.ticket !== "string" ||
      !TICKET.test(value.ticket) ||
      !Number.isInteger(value.expires_in_seconds) ||
      value.expires_in_seconds < 1 ||
      value.expires_in_seconds > 60 ||
      value.websocket_path !== "/api/alpha/v1/ui/events"
    ) {
      throw new Error("invalid-ticket-response");
    }
    return value;
  }

  async registerProject(request, signal) {
    validateProjectRequest(request);
    const value = await this._post("/api/alpha/v1/projects", request, signal);
    return validateProjectResponse(value, request);
  }

  async acceptIntent(request, signal) {
    validateIntentRequest(request);
    const value = await this._post("/api/alpha/v1/intents", request, signal);
    return validateIntentResponse(value, request);
  }

  async acceptPlan(request, signal) {
    validatePlanRequest(request);
    const value = await this._post("/api/alpha/v1/plans", request, signal);
    return validatePlanResponse(value, request);
  }

  async submitRun(request, signal) {
    validateRunRequest(request);
    const value = await this._post("/api/alpha/v1/runs", request, signal);
    const response = validateRunResponse(value, request.run_id);
    if (
      response.project_id !== request.project_id ||
      response.intent_id !== request.intent_id ||
      response.plan_id !== request.plan_id
    ) {
      throw new Error("invalid-run-response");
    }
    return response;
  }

  async inspectRun(runId) {
    const expected = validRunId(runId);
    const value = await this._request(
      `/api/alpha/v1/runs/${encodeURIComponent(expected)}/status`,
    );
    return validateRunResponse(value, expected);
  }

  async replayRun(runId) {
    const expected = validRunId(runId);
    const value = await this._request(
      `/api/alpha/v1/runs/${encodeURIComponent(expected)}/replay`,
    );
    return validateReplayResponse(value, expected);
  }

  async cancelRun(runId) {
    const expected = validRunId(runId);
    const body = JSON.stringify({
      schema_version: "alpha-cancel-run-request/v1",
      idempotency_key: `web-cancel-${randomIdentifier()}`,
    });
    const value = await this._request(
      `/api/alpha/v1/runs/${encodeURIComponent(expected)}/cancel`,
      { method: "POST", body },
    );
    return validateRunResponse(value, expected);
  }

  async _post(path, request, signal) {
    const body = JSON.stringify(request);
    if (new TextEncoder().encode(body).byteLength > MAX_REQUEST_BYTES) {
      throw new Error("request-too-large");
    }
    return this._request(path, { method: "POST", body, signal });
  }

  async _request(path, options = {}) {
    if (!path.startsWith("/api/alpha/v1/") || path.includes("..")) {
      throw new Error("invalid-client-path");
    }
    const headers = new Headers({
      Accept: "application/json",
      Authorization: `Bearer ${this._token}`,
    });
    if (options.body !== undefined) {
      headers.set("Content-Type", "application/json");
    }
    const response = await fetch(new URL(path, window.location.origin), {
      ...options,
      headers,
      cache: "no-store",
      credentials: "omit",
      redirect: "error",
    });
    const mediaType = response.headers.get("content-type")?.split(";", 1)[0].trim().toLowerCase();
    if (mediaType !== "application/json") {
      throw new Error("invalid-response-media-type");
    }
    const declaredText = response.headers.get("content-length");
    if (declaredText !== null && !/^[0-9]+$/.test(declaredText)) {
      throw new Error("invalid-response-size");
    }
    const declared = declaredText === null ? null : Number(declaredText);
    if (declared !== null && (!Number.isSafeInteger(declared) || declared > MAX_RESPONSE_BYTES)) {
      throw new Error("response-too-large");
    }
    const bytes = await response.arrayBuffer();
    if (bytes.byteLength < 1 || bytes.byteLength > MAX_RESPONSE_BYTES) {
      throw new Error("invalid-response-size");
    }
    const text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    let value;
    try {
      value = JSON.parse(text);
    } catch {
      throw new Error("invalid-json-response");
    }
    if (!response.ok) {
      const code = isObject(value) && typeof value.error === "string" && SAFE_ERROR.test(value.error)
        ? value.error
        : `request-failed-${response.status}`;
      throw new Error(code);
    }
    if (!isObject(value)) {
      throw new Error("invalid-response-contract");
    }
    return value;
  }
}

elements.connectionForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const token = elements.token.value;
  elements.token.value = "";
  if (!allowsCredentialTransport()) {
    setMessage(elements.connectionMessage, "Remote plaintext HTTP is not allowed.", true);
    return;
  }
  disconnect({ announce: false });
  try {
    state.client = new AlphaApiClient(token);
    state.wanted = true;
    setControls(false);
    setConnection("connecting", "Connecting");
    setMessage(elements.connectionMessage, "Requesting a one-use event ticket.");
    await openEventSocket();
  } catch (error) {
    disconnect({ announce: false });
    setMessage(elements.connectionMessage, safeMessage(error), true);
  }
});

elements.disconnect.addEventListener("click", () => disconnect({ announce: true }));
elements.workflowForm.addEventListener("submit", (event) => {
  event.preventDefault();
  submitWorkflowContract();
});
elements.runForm.addEventListener("submit", (event) => {
  event.preventDefault();
  runOperation("status");
});

for (const button of document.querySelectorAll("[data-operation]")) {
  if (button.dataset.operation !== "status") {
    button.addEventListener("click", () => runOperation(button.dataset.operation));
  }
}

window.addEventListener("pagehide", () => disconnect({ announce: false }));

async function openEventSocket() {
  if (!state.wanted || state.client === null) {
    return;
  }
  const generation = ++state.generation;
  const ticket = await state.client.issueSocketTicket();
  if (!state.wanted || generation !== state.generation) {
    return;
  }
  const url = new URL(ticket.websocket_path, window.location.origin);
  url.protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  url.searchParams.set("ticket", ticket.ticket);
  url.searchParams.set("after", String(state.cursor));
  const socket = new WebSocket(url);
  socket.binaryType = "arraybuffer";
  state.socket = socket;

  socket.addEventListener("open", () => {
    if (generation !== state.generation) {
      socket.close(1000, "superseded");
      return;
    }
    state.reconnectAttempt = 0;
    setConnection("connected", "Connected");
    setMessage(elements.connectionMessage, "Following the daemon event ledger.");
    setControls(true);
  });

  socket.addEventListener("message", (event) => {
    if (generation !== state.generation) {
      return;
    }
    try {
      const bytes = event.data instanceof ArrayBuffer ? event.data : null;
      if (bytes === null || bytes.byteLength > MAX_RESPONSE_BYTES) {
        throw new Error("invalid-event-frame");
      }
      const page = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
      applyEventPage(page);
    } catch (error) {
      disconnect({ announce: false });
      setMessage(elements.connectionMessage, safeMessage(error), true);
    }
  });

  socket.addEventListener("close", (event) => {
    if (generation !== state.generation) {
      return;
    }
    state.socket = null;
    setControls(false);
    if (!state.wanted || state.client === null) {
      setConnection("disconnected", "Disconnected");
      return;
    }
    if (event.code === 4400) {
      disconnect({ announce: false });
      setMessage(elements.connectionMessage, "The server rejected the event protocol.", true);
      return;
    }
    scheduleReconnect();
  });

  socket.addEventListener("error", () => {
    if (generation === state.generation) {
      setMessage(elements.connectionMessage, "Event connection failed; retrying.", true);
    }
  });
}

function scheduleReconnect() {
  clearReconnectTimer();
  const delay = Math.min(5000, 250 * 2 ** Math.min(state.reconnectAttempt, 5));
  state.reconnectAttempt += 1;
  setConnection("connecting", `Reconnecting in ${delay} ms`);
  state.reconnectTimer = window.setTimeout(() => {
    state.reconnectTimer = null;
    openEventSocket().catch((error) => {
      setMessage(elements.connectionMessage, safeMessage(error), true);
      if (state.wanted) {
        scheduleReconnect();
      }
    });
  }, delay);
}

function applyEventPage(page) {
  validateEventPage(page, state.cursor);
  state.events = [...state.events, ...page.events].slice(-MAX_RETAINED_EVENTS);
  state.cursor = page.next_cursor;
  elements.cursor.textContent = `Cursor ${state.cursor}`;
  renderEvents();
}

function validateEventPage(page, expectedAfter) {
  if (
    !isObject(page) ||
    !hasExactKeys(page, EVENT_PAGE_KEYS) ||
    page.schema_version !== "alpha-event-page/v1" ||
    page.after_cursor !== expectedAfter ||
    page.limit !== EVENT_PAGE_LIMIT ||
    !Number.isSafeInteger(page.scanned_events) ||
    page.scanned_events < 0 ||
    page.scanned_events > EVENT_PAGE_LIMIT ||
    !Array.isArray(page.events) ||
    page.events.length > page.scanned_events ||
    !Number.isSafeInteger(page.next_cursor) ||
    page.next_cursor < expectedAfter ||
    typeof page.has_more !== "boolean"
  ) {
    throw new Error("invalid-event-page");
  }
  let previous = expectedAfter;
  const identifiers = new Set();
  for (const event of page.events) {
    if (
      !isObject(event) ||
      !hasExactKeys(event, EVENT_KEYS) ||
      event.schema_version !== "alpha-event/v1" ||
      !Number.isSafeInteger(event.cursor) ||
      event.cursor <= previous ||
      event.cursor > page.next_cursor ||
      typeof event.event_id !== "string" ||
      !IDENTIFIER.test(event.event_id) ||
      identifiers.has(event.event_id) ||
      typeof event.event_type !== "string" ||
      !EVENT_TYPES.has(event.event_type) ||
      !boundedWireText(event.stream_id, 200) ||
      !Number.isSafeInteger(event.stream_sequence) ||
      event.stream_sequence < 1 ||
      event.event_schema_version !== 1 ||
      !boundedWireText(event.recorded_at, 64) ||
      Number.isNaN(Date.parse(event.recorded_at)) ||
      !boundedWireText(event.correlation_id, 200) ||
      !(event.causation_id === null || boundedWireText(event.causation_id, 200)) ||
      !boundedWireText(event.actor, 200) ||
      typeof event.payload_digest !== "string" ||
      !DIGEST.test(event.payload_digest) ||
      !isObject(event.payload)
    ) {
      throw new Error("invalid-event-page");
    }
    identifiers.add(event.event_id);
    previous = event.cursor;
  }
  if (
    (page.scanned_events === 0 && page.next_cursor !== expectedAfter) ||
    (page.scanned_events > 0 && page.next_cursor <= expectedAfter) ||
    (page.scanned_events < EVENT_PAGE_LIMIT && page.has_more) ||
    (page.has_more && page.next_cursor === expectedAfter)
  ) {
    throw new Error("invalid-event-page");
  }
}

function renderEvents() {
  const fragment = document.createDocumentFragment();
  for (const event of [...state.events].reverse()) {
    const row = document.createElement("tr");
    row.append(cell(String(event.cursor)));
    row.append(cell(event.event_type));
    row.append(cell(event.stream_id));
    row.append(cell(formatTimestamp(event.recorded_at)));
    fragment.append(row);
  }
  elements.eventRows.replaceChildren(fragment);
  elements.eventEmpty.hidden = state.events.length > 0;
  elements.eventCount.textContent = `${state.events.length} retained`;
}

async function submitWorkflowContract() {
  const client = state.client;
  if (client === null) {
    setMessage(elements.workflowMessage, "Connect before sending a contract.", true);
    return;
  }
  if (state.workflowBusy) {
    setMessage(elements.workflowMessage, "workflow-operation-busy", true);
    return;
  }
  const operation = elements.workflowOperation.value;
  const file = elements.workflowFile.files?.item(0) ?? null;
  elements.workflowFile.value = "";
  if (file === null) {
    setMessage(elements.workflowMessage, "workflow-file-required", true);
    return;
  }
  const abort = new AbortController();
  state.workflowAbort = abort;
  state.workflowBusy = true;
  setControls(isEventConnected());
  setMessage(elements.workflowMessage, `Validating ${operation} contract.`);
  try {
    const request = await readWorkflowRequest(operation, file);
    if (state.client !== client || abort.signal.aborted) {
      return;
    }
    const value = await dispatchWorkflowContract(client, operation, request, abort.signal);
    if (state.client !== client || abort.signal.aborted) {
      return;
    }
    elements.workflowOutput.textContent = JSON.stringify(value, null, 2);
    if (operation === "run") {
      elements.runId.value = value.run_id;
    }
    setMessage(elements.workflowMessage, `${operation} contract accepted.`);
  } catch (error) {
    if (state.client === client && state.workflowAbort === abort && !abort.signal.aborted) {
      elements.workflowOutput.textContent = "No contract result available.";
      setMessage(elements.workflowMessage, safeMessage(error), true);
    }
  } finally {
    if (state.workflowAbort === abort) {
      state.workflowAbort = null;
      state.workflowBusy = false;
      setControls(isEventConnected());
    }
  }
}

async function readWorkflowRequest(operation, file) {
  if (!Number.isSafeInteger(file.size) || file.size < 1 || file.size > MAX_REQUEST_BYTES) {
    throw new Error("invalid-workflow-file-size");
  }
  const bytes = await file.arrayBuffer();
  if (bytes.byteLength !== file.size || bytes.byteLength > MAX_REQUEST_BYTES) {
    throw new Error("invalid-workflow-file-size");
  }
  const text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  let value;
  try {
    value = JSON.parse(text);
  } catch {
    throw new Error("invalid-workflow-json");
  }
  if (operation === "project") {
    return validateProjectRequest(value);
  }
  if (operation === "intent") {
    return validateIntentRequest(value);
  }
  if (operation === "plan") {
    return validatePlanRequest(value);
  }
  if (operation === "run") {
    return validateRunRequest(value);
  }
  throw new Error("invalid-workflow-operation");
}

async function dispatchWorkflowContract(client, operation, request, signal) {
  if (operation === "project") {
    return client.registerProject(request, signal);
  }
  if (operation === "intent") {
    return client.acceptIntent(request, signal);
  }
  if (operation === "plan") {
    return client.acceptPlan(request, signal);
  }
  if (operation === "run") {
    return client.submitRun(request, signal);
  }
  throw new Error("invalid-workflow-operation");
}

async function runOperation(operation) {
  const client = state.client;
  if (client === null) {
    setMessage(elements.operationMessage, "Connect before issuing a run command.", true);
    return;
  }
  let runId;
  try {
    runId = validRunId(elements.runId.value);
  } catch (error) {
    setMessage(elements.operationMessage, safeMessage(error), true);
    return;
  }
  setOperationBusy(true);
  setMessage(elements.operationMessage, `Requesting ${operation}.`);
  try {
    let value;
    if (operation === "status") {
      value = await client.inspectRun(runId);
    } else if (operation === "replay") {
      value = await client.replayRun(runId);
    } else if (operation === "cancel") {
      value = await client.cancelRun(runId);
    } else {
      throw new Error("invalid-operation");
    }
    if (state.client !== client) {
      return;
    }
    elements.operationOutput.textContent = JSON.stringify(value, null, 2);
    setMessage(elements.operationMessage, `${operation} completed.`);
  } catch (error) {
    elements.operationOutput.textContent = "No result available.";
    setMessage(elements.operationMessage, safeMessage(error), true);
  } finally {
    setOperationBusy(false);
  }
}

function disconnect({ announce }) {
  state.wanted = false;
  state.generation += 1;
  clearReconnectTimer();
  if (state.workflowAbort !== null) {
    state.workflowAbort.abort();
    state.workflowAbort = null;
  }
  state.workflowBusy = false;
  if (state.socket !== null) {
    state.socket.close(1000, "operator-disconnect");
    state.socket = null;
  }
  if (state.client !== null) {
    state.client.clear();
    state.client = null;
  }
  state.cursor = 0;
  state.events = [];
  state.reconnectAttempt = 0;
  elements.cursor.textContent = "Cursor 0";
  elements.workflowOperation.value = "project";
  elements.workflowFile.value = "";
  elements.workflowOutput.textContent = "No contract submitted.";
  setMessage(elements.workflowMessage, "");
  elements.operationOutput.textContent = "No result yet.";
  setControls(false);
  setConnection("disconnected", "Disconnected");
  renderEvents();
  if (announce) {
    setMessage(elements.connectionMessage, "Credential and projection state cleared.");
  }
}

function setConnection(stateName, label) {
  elements.connectionStatus.dataset.state = stateName;
  elements.connectionStatus.textContent = label;
}

function setControls(connected) {
  elements.connect.disabled = connected || state.client !== null;
  elements.disconnect.disabled = !connected && state.client === null;
  elements.workflowOperation.disabled = !connected || state.workflowBusy;
  elements.workflowFile.disabled = !connected || state.workflowBusy;
  elements.workflowSubmit.disabled = !connected || state.workflowBusy;
  for (const button of document.querySelectorAll("[data-operation]")) {
    button.disabled = !connected;
  }
}

function setOperationBusy(busy) {
  const connected = isEventConnected();
  for (const button of document.querySelectorAll("[data-operation]")) {
    button.disabled = busy || !connected;
  }
}

function isEventConnected() {
  return (
    state.client !== null &&
    state.socket !== null &&
    state.socket.readyState === WebSocket.OPEN
  );
}

function setMessage(element, message, error = false) {
  element.textContent = message;
  element.dataset.level = error ? "error" : "info";
}

function clearReconnectTimer() {
  if (state.reconnectTimer !== null) {
    window.clearTimeout(state.reconnectTimer);
    state.reconnectTimer = null;
  }
}

function validateProjectRequest(value) {
  if (
    !isObject(value) ||
    !hasExactKeys(value, PROJECT_REQUEST_KEYS) ||
    value.schema_version !== "alpha-project-request/v1" ||
    !validIdentifier(value.project_id) ||
    !boundedNonblankText(value.root, 4096) ||
    value.configuration_provider !== "kernform" ||
    value.configuration_version !== "0.1.0" ||
    typeof value.configuration_digest !== "string" ||
    !DIGEST.test(value.configuration_digest) ||
    !validIdentifier(value.idempotency_key)
  ) {
    throw new Error("invalid-project-request");
  }
  return value;
}

function validateIntentRequest(value) {
  if (
    !isObject(value) ||
    !hasExactKeys(value, INTENT_REQUEST_KEYS) ||
    value.schema_version !== "alpha-intent-request/v1" ||
    !validIdentifier(value.intent_id) ||
    !validIdentifier(value.project_id) ||
    !boundedNonblankText(value.objective, 8000) ||
    !boundedTextCollection(value.constraints) ||
    !boundedTextCollection(value.assumptions) ||
    !boundedTextCollection(value.unresolved_questions) ||
    !validIdentifier(value.idempotency_key)
  ) {
    throw new Error("invalid-intent-request");
  }
  return value;
}

function validatePlanRequest(value) {
  if (
    !isObject(value) ||
    !hasExactKeys(value, PLAN_REQUEST_KEYS) ||
    value.schema_version !== "alpha-plan-request/v1" ||
    !validIdentifier(value.plan_id) ||
    !validIdentifier(value.project_id) ||
    !validIdentifier(value.intent_id) ||
    typeof value.base_commit !== "string" ||
    !COMMIT.test(value.base_commit) ||
    !validIdentifier(value.idempotency_key) ||
    !uniqueEffects(value.allowed_effects) ||
    !Array.isArray(value.nodes) ||
    value.nodes.length < 1 ||
    value.nodes.length > MAX_PLAN_NODES
  ) {
    throw new Error("invalid-plan-request");
  }
  const allowedEffects = new Set(value.allowed_effects);
  const nodeIds = value.nodes.map((node) => node?.node_id);
  if (
    nodeIds.some((nodeId) => !validIdentifier(nodeId)) ||
    new Set(nodeIds).size !== nodeIds.length ||
    value.nodes.some((node) => !validPlanNode(node, allowedEffects))
  ) {
    throw new Error("invalid-plan-request");
  }
  const knownNodes = new Set(nodeIds);
  if (
    value.nodes.some((node) => node.depends_on.some((dependency) => !knownNodes.has(dependency)))
  ) {
    throw new Error("invalid-plan-request");
  }
  const order = planTopologicalOrder(value.nodes);
  if (order === null || !writersAreOrdered(value.nodes, order)) {
    throw new Error("invalid-plan-request");
  }
  return value;
}

function validateRunRequest(value) {
  if (
    !isObject(value) ||
    !hasExactKeys(value, RUN_REQUEST_KEYS) ||
    value.schema_version !== "alpha-run-request/v1" ||
    !validIdentifier(value.run_id) ||
    !validIdentifier(value.project_id) ||
    !validIdentifier(value.intent_id) ||
    !validIdentifier(value.plan_id) ||
    !validIdentifier(value.idempotency_key)
  ) {
    throw new Error("invalid-run-request");
  }
  return value;
}

function validPlanNode(node, allowedEffects) {
  if (
    !isObject(node) ||
    !hasExactKeys(node, PLAN_NODE_KEYS) ||
    !validIdentifier(node.node_id) ||
    !boundedNonblankText(node.objective, 2000) ||
    !uniqueIdentifiers(node.depends_on, MAX_PLAN_NODES) ||
    node.depends_on.includes(node.node_id) ||
    !uniqueEffects(node.effects) ||
    !node.effects.includes("repository-read") ||
    !node.effects.includes("process") ||
    node.effects.some((effect) => !allowedEffects.has(effect)) ||
    !uniqueRepositoryPaths(node.allowed_paths) ||
    !validPlanBudget(node.budget) ||
    !Array.isArray(node.checks) ||
    node.checks.length < 1 ||
    node.checks.length > MAX_COLLECTION_ITEMS ||
    node.checks.some((check) => !validAcceptanceCheck(check))
  ) {
    return false;
  }
  const checkIds = node.checks.map((check) => check.check_id);
  if (new Set(checkIds).size !== checkIds.length) {
    return false;
  }
  const writesRepository = node.effects.includes("repository-write");
  return writesRepository
    ? node.allowed_paths.length > 0 && node.budget.max_changed_files >= 1
    : node.allowed_paths.length === 0 && node.budget.max_changed_files === 0;
}

function validPlanBudget(value) {
  return (
    isObject(value) &&
    hasExactKeys(value, PLAN_BUDGET_KEYS) &&
    boundedInteger(value.max_input_tokens, 0, 1_000_000) &&
    boundedInteger(value.max_output_tokens, 0, 1_000_000) &&
    boundedInteger(value.timeout_seconds, 1, MAX_ACCEPTANCE_TIMEOUT_SECONDS) &&
    boundedInteger(value.max_cost_microusd, 0, 10_000_000_000) &&
    boundedInteger(value.max_changed_files, 0, 10_000)
  );
}

function validAcceptanceCheck(value) {
  return (
    isObject(value) &&
    hasExactKeys(value, ACCEPTANCE_CHECK_KEYS) &&
    validIdentifier(value.check_id) &&
    Array.isArray(value.argv) &&
    value.argv.length >= 1 &&
    value.argv.length <= MAX_CHECK_ARGV &&
    EXECUTABLE_ALIAS.test(value.argv[0]) &&
    value.argv.every((argument) => boundedWireText(argument, 2048)) &&
    boundedInteger(value.expected_exit_code, 0, 255)
  );
}

function validateProjectResponse(value, request) {
  if (
    !isObject(value) ||
    !hasExactKeys(value, PROJECT_RESPONSE_KEYS) ||
    value.schema_version !== "alpha-project/v1" ||
    value.project_id !== request.project_id ||
    value.root !== request.root ||
    value.configuration_provider !== request.configuration_provider ||
    value.configuration_version !== request.configuration_version ||
    value.configuration_digest !== request.configuration_digest ||
    !validAcceptedRecord(value)
  ) {
    throw new Error("invalid-project-response");
  }
  return value;
}

function validateIntentResponse(value, request) {
  if (
    !isObject(value) ||
    !hasExactKeys(value, INTENT_RESPONSE_KEYS) ||
    value.schema_version !== "alpha-intent/v1" ||
    value.intent_id !== request.intent_id ||
    value.project_id !== request.project_id ||
    value.objective !== request.objective ||
    !sameJsonValue(value.constraints, request.constraints) ||
    !sameJsonValue(value.assumptions, request.assumptions) ||
    !sameJsonValue(value.unresolved_questions, request.unresolved_questions) ||
    !validAcceptedRecord(value)
  ) {
    throw new Error("invalid-intent-response");
  }
  return value;
}

function validatePlanResponse(value, request) {
  const order = planTopologicalOrder(request.nodes);
  if (
    !isObject(value) ||
    !hasExactKeys(value, PLAN_RESPONSE_KEYS) ||
    value.schema_version !== "alpha-plan/v1" ||
    value.plan_id !== request.plan_id ||
    value.project_id !== request.project_id ||
    value.intent_id !== request.intent_id ||
    value.base_commit !== request.base_commit ||
    !sameJsonValue(value.allowed_effects, request.allowed_effects) ||
    !sameJsonValue(value.nodes, request.nodes) ||
    order === null ||
    !sameJsonValue(value.topological_order, order) ||
    !validAcceptedRecord(value)
  ) {
    throw new Error("invalid-plan-response");
  }
  return value;
}

function validAcceptedRecord(value) {
  return (
    boundedWireText(value.principal_id, 200) &&
    validIdentifier(value.event_id) &&
    boundedInteger(value.cursor, 1, Number.MAX_SAFE_INTEGER) &&
    typeof value.event_digest === "string" &&
    DIGEST.test(value.event_digest)
  );
}

function validateRunResponse(value, expectedRunId) {
  if (
    !isObject(value) ||
    !hasExactKeys(value, RUN_KEYS) ||
    value.schema_version !== "alpha-run/v1" ||
    value.run_id !== expectedRunId ||
    !IDENTIFIER.test(value.run_id) ||
    !IDENTIFIER.test(value.project_id) ||
    !IDENTIFIER.test(value.intent_id) ||
    !IDENTIFIER.test(value.plan_id) ||
    !RUN_STATUSES.has(value.status) ||
    typeof value.cancellation_requested !== "boolean" ||
    !(value.active_node_id === null || IDENTIFIER.test(value.active_node_id)) ||
    !Number.isSafeInteger(value.attempt) ||
    value.attempt < 0 ||
    !Number.isSafeInteger(value.fencing_token) ||
    value.fencing_token < 0 ||
    typeof value.retained_worktree !== "boolean" ||
    !boundedWireText(value.principal_id, 200) ||
    !IDENTIFIER.test(value.event_id) ||
    !Number.isSafeInteger(value.cursor) ||
    value.cursor < 1 ||
    typeof value.event_digest !== "string" ||
    !DIGEST.test(value.event_digest)
  ) {
    throw new Error("invalid-run-response");
  }
  return value;
}

function validateReplayResponse(value, expectedRunId) {
  if (
    !isObject(value) ||
    !hasExactKeys(value, REPLAY_KEYS) ||
    value.schema_version !== "alpha-replay/v2" ||
    value.run_id !== expectedRunId ||
    !isObject(value.project) ||
    value.project.schema_version !== "alpha-project/v1" ||
    !isObject(value.intent) ||
    value.intent.schema_version !== "alpha-intent/v1" ||
    !isObject(value.plan) ||
    value.plan.schema_version !== "alpha-plan/v1" ||
    !isObject(value.run) ||
    !Number.isSafeInteger(value.processed_events) ||
    value.processed_events < 0 ||
    typeof value.state_digest !== "string" ||
    !DIGEST.test(value.state_digest) ||
    !new Set(["not-applicable", "verified", "inconclusive", "failed"]).has(
      value.artifact_integrity,
    ) ||
    !Array.isArray(value.artifacts) ||
    value.artifacts.length > 4096 ||
    value.artifacts.some((artifact) => !isObject(artifact)) ||
    !Array.isArray(value.findings) ||
    value.findings.length > 4096 ||
    value.findings.some((finding) => !isObject(finding)) ||
    typeof value.artifact_evidence_digest !== "string" ||
    !DIGEST.test(value.artifact_evidence_digest) ||
    !isObject(value.verification) ||
    value.verification.schema_version !== "alpha-verification-replay/v1"
  ) {
    throw new Error("invalid-replay-response");
  }
  const run = validateRunResponse(value.run, expectedRunId);
  if (
    value.project.project_id !== run.project_id ||
    value.intent.intent_id !== run.intent_id ||
    value.intent.project_id !== run.project_id ||
    value.plan.plan_id !== run.plan_id ||
    value.plan.project_id !== run.project_id ||
    value.plan.intent_id !== run.intent_id
  ) {
    throw new Error("invalid-replay-response");
  }
  return value;
}

function validRunId(value) {
  const normalized = typeof value === "string" ? value.trim() : "";
  if (
    normalized.length < 1 ||
    normalized.length > MAX_RUN_ID_CHARS ||
    !RUN_ID.test(normalized)
  ) {
    throw new Error("invalid-run-id");
  }
  return normalized;
}

function randomIdentifier() {
  if (typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return [...bytes].map((value) => value.toString(16).padStart(2, "0")).join("");
}

function allowsCredentialTransport() {
  return (
    window.location.protocol === "https:" ||
    window.location.hostname === "localhost" ||
    window.location.hostname === "127.0.0.1" ||
    window.location.hostname === "::1"
  );
}

function safeMessage(error) {
  const value = error instanceof Error ? error.message : "operation-failed";
  return SAFE_ERROR.test(value) ? value : "operation-failed";
}

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function hasExactKeys(value, expected) {
  const actual = Object.keys(value).sort();
  return (
    actual.length === expected.length &&
    actual.every((name, index) => name === expected[index])
  );
}

function boundedWireText(value, maximum) {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= maximum &&
    [...value].every((character) => {
      const code = character.codePointAt(0);
      return code !== undefined && code >= 0x20 && code !== 0x7f;
    })
  );
}

function boundedNonblankText(value, maximum) {
  return boundedWireText(value, maximum) && value.trim().length > 0;
}

function validIdentifier(value) {
  return typeof value === "string" && IDENTIFIER.test(value);
}

function boundedTextCollection(values) {
  return (
    Array.isArray(values) &&
    values.length <= MAX_COLLECTION_ITEMS &&
    values.every((value) => boundedNonblankText(value, 2000)) &&
    new Set(values).size === values.length
  );
}

function uniqueIdentifiers(values, maximum) {
  return (
    Array.isArray(values) &&
    values.length <= maximum &&
    values.every((value) => validIdentifier(value)) &&
    new Set(values).size === values.length
  );
}

function uniqueEffects(values) {
  return (
    Array.isArray(values) &&
    values.length <= EFFECTS.size &&
    values.every((value) => typeof value === "string" && EFFECTS.has(value)) &&
    new Set(values).size === values.length
  );
}

function uniqueRepositoryPaths(values) {
  return (
    Array.isArray(values) &&
    values.length <= MAX_COLLECTION_ITEMS &&
    values.every((value) => validRepositoryPath(value)) &&
    new Set(values).size === values.length
  );
}

function validRepositoryPath(value) {
  if (!boundedWireText(value, 4096) || value.includes("\\") || value.startsWith("/")) {
    return false;
  }
  if (value === ".") {
    return true;
  }
  const parts = value.split("/");
  return (
    parts.length > 0 &&
    parts.every((part) => part !== "" && part !== "." && part !== ".." && part !== ".git")
  );
}

function boundedInteger(value, minimum, maximum) {
  return Number.isSafeInteger(value) && value >= minimum && value <= maximum;
}

function planTopologicalOrder(nodes) {
  const dependents = new Map(nodes.map((node) => [node.node_id, []]));
  const remaining = new Map(nodes.map((node) => [node.node_id, node.depends_on.length]));
  for (const node of nodes) {
    for (const dependency of node.depends_on) {
      const children = dependents.get(dependency);
      if (children === undefined) {
        return null;
      }
      children.push(node.node_id);
    }
  }
  const ready = [...remaining]
    .filter(([, count]) => count === 0)
    .map(([nodeId]) => nodeId)
    .sort();
  const ordered = [];
  while (ready.length > 0) {
    const nodeId = ready.shift();
    ordered.push(nodeId);
    for (const dependent of dependents.get(nodeId).sort()) {
      const count = remaining.get(dependent) - 1;
      remaining.set(dependent, count);
      if (count === 0) {
        ready.push(dependent);
        ready.sort();
      }
    }
  }
  return ordered.length === nodes.length ? ordered : null;
}

function writersAreOrdered(nodes, order) {
  const byId = new Map(nodes.map((node) => [node.node_id, node]));
  const ancestors = new Map();
  for (const nodeId of order) {
    const inherited = new Set();
    for (const dependency of byId.get(nodeId).depends_on) {
      inherited.add(dependency);
      for (const ancestor of ancestors.get(dependency)) {
        inherited.add(ancestor);
      }
    }
    ancestors.set(nodeId, inherited);
  }
  const writers = order.filter((nodeId) =>
    byId.get(nodeId).effects.includes("repository-write"),
  );
  return writers.slice(1).every((nodeId, index) => ancestors.get(nodeId).has(writers[index]));
}

function sameJsonValue(left, right) {
  if (left === right) {
    return true;
  }
  if (Array.isArray(left) || Array.isArray(right)) {
    return (
      Array.isArray(left) &&
      Array.isArray(right) &&
      left.length === right.length &&
      left.every((value, index) => sameJsonValue(value, right[index]))
    );
  }
  if (!isObject(left) || !isObject(right)) {
    return false;
  }
  const keys = Object.keys(left).sort();
  return (
    hasExactKeys(right, keys) && keys.every((key) => sameJsonValue(left[key], right[key]))
  );
}

function required(id) {
  const element = document.getElementById(id);
  if (element === null) {
    throw new Error("missing-ui-element");
  }
  return element;
}

function cell(value) {
  const element = document.createElement("td");
  element.textContent = value;
  return element;
}

function formatTimestamp(value) {
  const instant = new Date(value);
  return Number.isNaN(instant.valueOf()) ? "invalid-time" : instant.toLocaleTimeString();
}

setControls(false);
renderEvents();

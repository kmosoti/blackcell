"use strict";

import { RuntimeApiClient, safeMessage } from "/ui/assets/runtime-client.js";
import "/ui/assets/surface-elements.js";

const elements = {
  connectionForm: required("connection-form"),
  token: required("api-token"),
  connect: required("connect-button"),
  disconnect: required("disconnect-button"),
  connectionStatus: required("connection-status"),
  connectionMessage: required("connection-message"),
  cursor: required("cursor-display"),
  workspace: required("workspace-button"),
  runNavigation: required("run-navigation"),
  runId: required("run-id"),
  surfaceTitle: required("surface-title"),
  surfaceRevision: required("surface-revision"),
  surfaceMessage: required("surface-message"),
  surface: required("surface"),
};

const state = {
  client: null,
  socket: null,
  cursor: 0,
  generation: 0,
  surfaceGeneration: 0,
  surfacePending: false,
  refreshPending: false,
  reconnectAttempt: 0,
  reconnectTimer: null,
  refreshTimer: null,
  wanted: false,
  surfaceKind: "workspace",
  runId: null,
};

await loadTokens();

elements.connectionForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const token = elements.token.value;
  elements.token.value = "";
  disconnect(false);
  try {
    state.client = new RuntimeApiClient(token);
    state.wanted = true;
    setConnection("connecting", "Connecting");
    await showWorkspace(true);
    await openEventSocket();
  } catch (error) {
    disconnect(false);
    setMessage(elements.connectionMessage, safeMessage(error), true);
  }
});

elements.disconnect.addEventListener("click", () => disconnect(true));
elements.workspace.addEventListener("click", () => showWorkspace());
elements.runNavigation.addEventListener("submit", (event) => {
  event.preventDefault();
  showRun(elements.runId.value);
});
window.addEventListener("pagehide", () => disconnect(false));

async function showWorkspace(propagate = false) {
  if (state.client === null) {
    return;
  }
  const generation = ++state.surfaceGeneration;
  state.surfacePending = true;
  setBusy(true, "Loading workspace.");
  try {
    const surface = await state.client.workspaceSurface();
    if (!currentSurfaceRequest(generation)) {
      return;
    }
    state.surfaceKind = "workspace";
    state.runId = null;
    render(surface);
  } catch (error) {
    if (!currentSurfaceRequest(generation)) {
      return;
    }
    if (propagate) {
      throw error;
    }
    setMessage(elements.surfaceMessage, safeMessage(error), true);
  } finally {
    finishSurfaceRequest(generation);
  }
}

async function showRun(runId, propagate = false) {
  if (state.client === null) {
    return;
  }
  const selected = String(runId).trim();
  const generation = ++state.surfaceGeneration;
  state.surfacePending = true;
  setBusy(true, `Loading run ${selected}.`);
  try {
    const surface = await state.client.runSurface(selected);
    if (!currentSurfaceRequest(generation)) {
      return;
    }
    state.surfaceKind = "run";
    state.runId = selected;
    elements.runId.value = selected;
    render(surface);
  } catch (error) {
    if (!currentSurfaceRequest(generation)) {
      return;
    }
    if (propagate) {
      throw error;
    }
    setMessage(elements.surfaceMessage, safeMessage(error), true);
  } finally {
    finishSurfaceRequest(generation);
  }
}

function currentSurfaceRequest(generation) {
  return state.client !== null && generation === state.surfaceGeneration;
}

function finishSurfaceRequest(generation) {
  if (!currentSurfaceRequest(generation)) {
    return;
  }
  state.surfacePending = false;
  setBusy(false);
  if (state.refreshPending) {
    state.refreshPending = false;
    scheduleSurfaceRefresh();
  }
}

function render(surface) {
  elements.surfaceTitle.textContent = surface.title;
  elements.surfaceRevision.textContent = `Revision ${surface.revision.number} · cursor ${surface.revision.event_cursor}`;
  state.cursor = Math.max(state.cursor, surface.revision.event_cursor);
  elements.cursor.textContent = `Cursor ${state.cursor}`;
  elements.surface.renderSurface(surface, {
    onAction: executeAction,
    onSource: (operation, subjectId) => state.client.fetchSource(operation, subjectId),
    onArtifact: (runId, artifact) => state.client.fetchArtifact(runId, artifact),
  });
  setMessage(elements.surfaceMessage, "Surface synchronized with the daemon.");
  setControls(true);
}

async function executeAction(action, values, surface) {
  if (state.client === null) {
    throw new Error("not-connected");
  }
  const subjectId = surface.surface_id.startsWith("run:")
    ? surface.surface_id.slice("run:".length)
    : null;
  if (action.operation === "inspect-run") {
    await showRun(values.run_id, true);
    return;
  }
  await state.client.executeAction(action, values, subjectId);
  if (action.operation === "submit-run") {
    await showRun(values.run_id, true);
  } else if (action.operation === "cancel-run" && subjectId !== null) {
    await showRun(subjectId, true);
  } else {
    await showWorkspace(true);
  }
}

async function openEventSocket() {
  if (!state.wanted || state.client === null) {
    return;
  }
  const generation = ++state.generation;
  const ticket = await state.client.issueSocketTicket();
  if (!state.wanted || generation !== state.generation) {
    return;
  }
  const socket = new WebSocket(state.client.socketUrl(ticket, state.cursor));
  socket.binaryType = "arraybuffer";
  state.socket = socket;
  socket.addEventListener("open", () => {
    if (generation !== state.generation) {
      socket.close(1000, "superseded");
      return;
    }
    state.reconnectAttempt = 0;
    setConnection("connected", "Connected");
    setMessage(elements.connectionMessage, "Following daemon invalidations.");
  });
  socket.addEventListener("message", (event) => {
    if (generation !== state.generation) {
      return;
    }
    try {
      const bytes = event.data instanceof ArrayBuffer ? event.data : null;
      if (bytes === null || bytes.byteLength > 8_388_608) {
        throw new Error("invalid-event-frame");
      }
      const page = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
      if (
        typeof page !== "object" ||
        page === null ||
        !Number.isSafeInteger(page.next_cursor) ||
        page.next_cursor < state.cursor
      ) {
        throw new Error("invalid-event-page");
      }
      state.cursor = page.next_cursor;
      elements.cursor.textContent = `Cursor ${state.cursor}`;
      scheduleSurfaceRefresh();
    } catch (error) {
      socket.close(1002, "invalid-event-page");
      setMessage(elements.connectionMessage, safeMessage(error), true);
    }
  });
  socket.addEventListener("close", () => {
    if (generation !== state.generation || !state.wanted) {
      return;
    }
    state.socket = null;
    scheduleReconnect();
  });
  socket.addEventListener("error", () => socket.close());
}

function scheduleSurfaceRefresh() {
  if (state.refreshTimer !== null) {
    return;
  }
  state.refreshTimer = window.setTimeout(async () => {
    state.refreshTimer = null;
    if (state.surfacePending) {
      state.refreshPending = true;
      return;
    }
    if (state.surfaceKind === "run" && state.runId !== null) {
      await showRun(state.runId);
    } else {
      await showWorkspace();
    }
  }, 80);
}

function scheduleReconnect() {
  state.reconnectAttempt += 1;
  const delay = Math.min(10_000, 250 * (2 ** Math.min(state.reconnectAttempt, 6)));
  setConnection("connecting", "Reconnecting");
  state.reconnectTimer = window.setTimeout(() => {
    state.reconnectTimer = null;
    openEventSocket().catch((error) => {
      setMessage(elements.connectionMessage, safeMessage(error), true);
      scheduleReconnect();
    });
  }, delay);
}

function disconnect(announce) {
  state.wanted = false;
  state.generation += 1;
  state.surfaceGeneration += 1;
  for (const timer of [state.reconnectTimer, state.refreshTimer]) {
    if (timer !== null) {
      window.clearTimeout(timer);
    }
  }
  state.reconnectTimer = null;
  state.refreshTimer = null;
  state.surfacePending = false;
  state.refreshPending = false;
  if (state.socket !== null) {
    state.socket.close(1000, "client-disconnect");
    state.socket = null;
  }
  if (state.client !== null) {
    state.client.clear();
    state.client = null;
  }
  state.cursor = 0;
  state.runId = null;
  state.surfaceKind = "workspace";
  elements.cursor.textContent = "Cursor 0";
  elements.surface.replaceChildren();
  setBusy(false);
  elements.surfaceTitle.textContent = "Runtime surface";
  elements.surfaceRevision.textContent = "";
  setConnection("disconnected", "Disconnected");
  setControls(false);
  if (announce) {
    setMessage(elements.connectionMessage, "Disconnected and cleared the in-memory credential.");
    setMessage(elements.surfaceMessage, "Connect to load the semantic workspace.");
  }
}

function setControls(enabled) {
  elements.connect.disabled = enabled;
  elements.disconnect.disabled = !enabled;
  elements.workspace.disabled = !enabled;
  for (const control of elements.runNavigation.elements) {
    control.disabled = !enabled;
  }
}

function setConnection(kind, text) {
  elements.connectionStatus.dataset.state = kind;
  elements.connectionStatus.textContent = text;
}

function setBusy(busy, message = "") {
  elements.surface.setAttribute("aria-busy", String(busy));
  if (message) {
    setMessage(elements.surfaceMessage, message);
  }
}

function setMessage(element, message, error = false) {
  element.textContent = message;
  element.dataset.kind = error ? "error" : "status";
}

async function loadTokens() {
  const response = await fetch("/ui/assets/tokens.json", {
    cache: "no-store",
    credentials: "omit",
    redirect: "error",
  });
  if (!response.ok) {
    throw new Error("ui-tokens-unavailable");
  }
  const tokens = await response.json();
  for (const [group, entries] of Object.entries(tokens)) {
    if (group.startsWith("$") || typeof entries !== "object" || entries === null) {
      continue;
    }
    for (const [name, token] of Object.entries(entries)) {
      if (typeof token !== "object" || token === null || !("$value" in token)) {
        continue;
      }
      const cssValue = tokenCssValue(token);
      if (cssValue !== null) {
        document.documentElement.style.setProperty(`--${group}-${name}`, cssValue);
      }
    }
  }
}

function tokenCssValue(token) {
  const value = token.$value;
  if (
    token.$type === "color" &&
    typeof value === "object" &&
    value !== null &&
    value.colorSpace === "srgb" &&
    Array.isArray(value.components) &&
    value.components.length === 3 &&
    value.components.every((component) =>
      typeof component === "number" && Number.isFinite(component) && component >= 0 && component <= 1
    ) &&
    typeof value.hex === "string" &&
    /^#[0-9a-f]{6}$/i.test(value.hex)
  ) {
    return value.hex;
  }
  if (
    token.$type === "dimension" &&
    typeof value === "object" &&
    value !== null &&
    typeof value.value === "number" &&
    Number.isFinite(value.value) &&
    ["px", "rem"].includes(value.unit)
  ) {
    return `${value.value}${value.unit}`;
  }
  return null;
}

function required(id) {
  const element = document.getElementById(id);
  if (element === null) {
    throw new Error("missing-ui-element");
  }
  return element;
}

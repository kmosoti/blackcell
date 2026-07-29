"use strict";

const SVG = "http://www.w3.org/2000/svg";
const ElementBase = globalThis.HTMLElement || class {};

export class BlackCellSurface extends ElementBase {
  renderSurface(surface, handlers) {
    this._surface = surface;
    this._handlers = handlers;
    this.replaceChildren();
    const byId = new Map(surface.components.map((component) => [component.component_id, component]));
    const referenced = new Set(
      surface.components
        .filter((component) => component.kind === "section")
        .flatMap((component) => component.children),
    );
    for (const component of surface.components) {
      if (!referenced.has(component.component_id)) {
        this.append(renderComponent(component, byId, handlers, surface));
      }
    }
    if (surface.components.length === 0) {
      this.append(paragraph("This surface has no components.", "empty-state"));
    }
  }
}

if (globalThis.customElements && !globalThis.customElements.get("blackcell-surface")) {
  globalThis.customElements.define("blackcell-surface", BlackCellSurface);
}

export function graphLayout(nodes, edges) {
  const levels = new Map(nodes.map((node) => [node.node_id, 0]));
  for (let pass = 0; pass < nodes.length; pass += 1) {
    let changed = false;
    for (const edge of edges) {
      const next = Math.max(levels.get(edge.target_id) || 0, (levels.get(edge.source_id) || 0) + 1);
      if (next !== levels.get(edge.target_id)) {
        levels.set(edge.target_id, next);
        changed = true;
      }
    }
    if (!changed) {
      break;
    }
  }
  const rows = new Map();
  return nodes.map((node) => {
    const level = Math.min(levels.get(node.node_id) || 0, nodes.length);
    const row = rows.get(level) || 0;
    rows.set(level, row + 1);
    return { nodeId: node.node_id, x: 24 + (level * 260), y: 24 + (row * 96) };
  });
}

function renderComponent(component, byId, handlers, surface) {
  switch (component.kind) {
    case "section":
      return renderSection(component, byId, handlers, surface);
    case "status":
      return renderStatus(component);
    case "metrics":
      return renderMetrics(component);
    case "key-value":
      return renderKeyValue(component);
    case "table":
      return renderTable(component);
    case "form":
      return renderForm(component, handlers, surface);
    case "plan-graph":
      return renderGraph(component);
    case "timeline":
      return renderTimeline(component);
    case "findings":
      return renderFindings(component);
    case "evidence-matrix":
      return renderEvidence(component);
    case "artifacts":
      return renderArtifacts(component, handlers);
    case "source":
      return renderSource(component, handlers);
    default:
      return paragraph(`Unsupported component ${component.kind}.`, "component-error");
  }
}

function renderSection(component, byId, handlers, surface) {
  const section = document.createElement("section");
  section.className = "panel semantic-section";
  section.dataset.componentId = component.component_id;
  const heading = document.createElement("h3");
  heading.textContent = component.label;
  section.append(heading);
  if (component.description) {
    section.append(paragraph(component.description, "section-description"));
  }
  const content = document.createElement("div");
  content.className = "component-stack";
  for (const childId of component.children) {
    content.append(renderComponent(byId.get(childId), byId, handlers, surface));
  }
  section.append(content);
  return section;
}

function renderStatus(component) {
  const card = componentCard(component);
  const value = document.createElement("p");
  value.className = `status-value tone-${component.tone}`;
  value.textContent = component.value;
  card.append(value);
  if (component.detail) {
    card.append(paragraph(component.detail, "quiet-text"));
  }
  return card;
}

function renderMetrics(component) {
  const card = componentCard(component);
  const list = document.createElement("dl");
  list.className = "metric-grid";
  for (const metric of component.metrics) {
    const wrapper = document.createElement("div");
    const term = document.createElement("dt");
    const value = document.createElement("dd");
    term.textContent = metric.label;
    value.textContent = `${formatScalar(metric.value)}${metric.unit ? ` ${metric.unit}` : ""}`;
    wrapper.append(term, value);
    list.append(wrapper);
  }
  card.append(list);
  return card;
}

function renderKeyValue(component) {
  const card = componentCard(component);
  const list = document.createElement("dl");
  list.className = "key-value-list";
  for (const item of component.items) {
    const term = document.createElement("dt");
    const value = document.createElement("dd");
    term.textContent = item.key;
    value.textContent = formatScalar(item.value);
    if (item.provenance) {
      value.title = item.provenance;
    }
    list.append(term, value);
  }
  card.append(list);
  return card;
}

function renderTable(component) {
  const card = componentCard(component);
  const frame = document.createElement("div");
  frame.className = "table-frame";
  frame.tabIndex = 0;
  const table = document.createElement("table");
  const caption = document.createElement("caption");
  caption.textContent = component.label;
  caption.className = "visually-hidden";
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  for (const column of component.columns) {
    const cell = document.createElement("th");
    cell.scope = "col";
    cell.textContent = column.label;
    headRow.append(cell);
  }
  head.append(headRow);
  const body = document.createElement("tbody");
  for (const row of component.rows) {
    const tableRow = document.createElement("tr");
    tableRow.dataset.rowId = row.row_id;
    for (const column of component.columns) {
      const cell = document.createElement("td");
      cell.textContent = formatScalar(row.cells[column.key]);
      tableRow.append(cell);
    }
    body.append(tableRow);
  }
  table.append(caption, head, body);
  frame.append(table);
  if (component.rows.length === 0) {
    frame.append(paragraph(component.empty_message, "empty-state"));
  }
  card.append(frame);
  return card;
}

function renderForm(component, handlers, surface) {
  const card = componentCard(component);
  card.classList.add("form-card");
  if (component.description) {
    card.append(paragraph(component.description, "quiet-text"));
  }
  const form = document.createElement("form");
  form.className = "semantic-form";
  form.setAttribute("aria-label", component.label);
  const controls = new Map();
  for (const field of component.action.fields) {
    const wrapper = document.createElement("div");
    wrapper.className = "field";
    const label = document.createElement("label");
    label.htmlFor = field.field_id;
    label.textContent = field.label;
    const control = fieldControl(field);
    controls.set(field.json_pointer.slice(1), { field, control });
    wrapper.append(label, control);
    if (field.help) {
      wrapper.append(paragraph(field.help, "field-note"));
    }
    form.append(wrapper);
  }
  const submit = document.createElement("button");
  submit.className = component.action.operation === "cancel-run" ? "button danger" : "button primary";
  submit.type = "submit";
  submit.textContent = component.action.label;
  const message = paragraph("", "message");
  message.setAttribute("role", "status");
  message.setAttribute("aria-live", "polite");
  form.append(submit, message);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (component.action.confirmation && !window.confirm(component.action.confirmation)) {
      return;
    }
    submit.disabled = true;
    message.textContent = "Submitting typed action.";
    try {
      const values = Object.fromEntries(
        [...controls.entries()].map(([name, binding]) => [name, readControl(binding.field, binding.control)]),
      );
      await handlers.onAction(component.action, values, surface);
      message.textContent = "Action accepted.";
    } catch (error) {
      message.textContent = error instanceof Error ? error.message.replaceAll("-", " ") : "Action failed.";
      message.dataset.kind = "error";
    } finally {
      submit.disabled = false;
    }
  });
  card.append(form);
  return card;
}

function fieldControl(field) {
  let control;
  if (field.control === "textarea" || field.control === "string-list" || field.control === "structured-list") {
    control = document.createElement("textarea");
    control.rows = field.control === "structured-list" ? 10 : 4;
  } else if (field.control === "select") {
    control = document.createElement("select");
    for (const option of field.options) {
      const element = document.createElement("option");
      element.value = option.value;
      element.textContent = option.label;
      control.append(element);
    }
  } else {
    control = document.createElement("input");
    control.type = field.control === "number" ? "number" : field.control === "checkbox" ? "checkbox" : "text";
  }
  control.id = field.field_id;
  control.name = field.field_id;
  control.required = field.required;
  if (field.default !== null) {
    if (field.control === "checkbox") {
      control.checked = Boolean(field.default);
    } else if (field.control === "string-list") {
      control.value = Array.isArray(field.default) ? field.default.join("\n") : String(field.default);
    } else if (field.control === "structured-list") {
      control.value = JSON.stringify(field.default, null, 2);
    } else {
      control.value = String(field.default);
    }
  }
  return control;
}

function readControl(field, control) {
  if (field.control === "checkbox") {
    return control.checked;
  }
  if (field.control === "number") {
    const number = Number(control.value);
    if (!Number.isFinite(number)) {
      throw new Error("invalid numeric field");
    }
    return number;
  }
  if (field.control === "string-list") {
    return control.value.split("\n").map((item) => item.trim()).filter(Boolean);
  }
  if (field.control === "structured-list") {
    let parsed;
    try {
      parsed = JSON.parse(control.value || "[]");
    } catch {
      throw new Error("invalid structured list");
    }
    if (!Array.isArray(parsed)) {
      throw new Error("structured field must be a list");
    }
    return parsed;
  }
  return control.value;
}

function renderGraph(component) {
  const card = componentCard(component);
  const figure = document.createElement("figure");
  const description = document.createElement("figcaption");
  description.id = `${component.component_id}-caption`;
  description.textContent = "Directed plan graph. The adjacent plan table contains the same nodes and dependencies.";
  figure.setAttribute("aria-label", component.label);
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", component.label);
  svg.classList.add("plan-graph");
  const positions = graphLayout(component.nodes, component.edges);
  const byId = new Map(positions.map((position) => [position.nodeId, position]));
  const width = Math.max(320, ...positions.map((position) => position.x + 220));
  const height = Math.max(120, ...positions.map((position) => position.y + 72));
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  for (const edge of component.edges) {
    const source = byId.get(edge.source_id);
    const target = byId.get(edge.target_id);
    const line = document.createElementNS(SVG, "line");
    line.setAttribute("x1", source.x + 190);
    line.setAttribute("y1", source.y + 28);
    line.setAttribute("x2", target.x);
    line.setAttribute("y2", target.y + 28);
    line.setAttribute("class", "graph-edge");
    svg.append(line);
  }
  for (const node of component.nodes) {
    const position = byId.get(node.node_id);
    const group = document.createElementNS(SVG, "g");
    const title = document.createElementNS(SVG, "title");
    title.textContent = `${node.node_id}: ${node.label}; ${node.status}`;
    const box = document.createElementNS(SVG, "rect");
    box.setAttribute("x", position.x);
    box.setAttribute("y", position.y);
    box.setAttribute("width", 190);
    box.setAttribute("height", 56);
    box.setAttribute("rx", 6);
    box.setAttribute("class", `graph-node status-${node.status}`);
    const label = document.createElementNS(SVG, "text");
    label.setAttribute("x", position.x + 10);
    label.setAttribute("y", position.y + 23);
    label.textContent = node.node_id;
    const status = document.createElementNS(SVG, "text");
    status.setAttribute("x", position.x + 10);
    status.setAttribute("y", position.y + 43);
    status.setAttribute("class", "graph-status");
    status.textContent = node.status;
    group.append(title, box, label, status);
    svg.append(group);
  }
  figure.append(svg, description);
  card.append(figure);
  return card;
}

function renderTimeline(component) {
  const card = componentCard(component);
  const list = document.createElement("ol");
  list.className = "timeline";
  for (const item of component.items) {
    const entry = document.createElement("li");
    const title = document.createElement("strong");
    title.textContent = `${item.title}${item.status ? ` · ${item.status}` : ""}`;
    entry.append(title);
    if (item.detail) {
      entry.append(paragraph(item.detail, "quiet-text"));
    }
    list.append(entry);
  }
  card.append(list);
  return card;
}

function renderFindings(component) {
  const card = componentCard(component);
  const list = document.createElement("ul");
  list.className = "finding-list";
  for (const finding of component.findings) {
    const item = document.createElement("li");
    item.dataset.severity = finding.severity;
    const title = document.createElement("strong");
    title.textContent = `${finding.severity} · ${finding.summary}`;
    item.append(title);
    if (finding.evidence) {
      item.append(paragraph(finding.evidence, "quiet-text"));
    }
    list.append(item);
  }
  if (component.findings.length === 0) {
    list.append(paragraph("No replay findings.", "empty-state"));
  }
  card.append(list);
  return card;
}

function renderEvidence(component) {
  return renderTable({
    ...component,
    kind: "table",
    columns: [
      { key: "dimension", label: "Dimension" },
      { key: "disposition", label: "Disposition" },
      { key: "evidence", label: "Evidence" },
      { key: "digest", label: "Source digest" },
    ],
    rows: component.rows.map((row) => ({
      row_id: row.row_id,
      cells: {
        dimension: row.dimension,
        disposition: row.disposition,
        evidence: row.evidence,
        digest: row.source_digest,
      },
    })),
    empty_message: "No evidence rows.",
  });
}

function renderArtifacts(component, handlers) {
  const card = componentCard(component);
  const list = document.createElement("ul");
  list.className = "artifact-list";
  for (const artifact of component.items) {
    const item = document.createElement("li");
    const identity = document.createElement("code");
    identity.textContent = `${artifact.node_id} · ${artifact.role} · ${artifact.digest}`;
    const metadata = paragraph(
      `${artifact.media_type} · ${artifact.size_bytes} bytes · verified ${artifact.verified}`,
      "quiet-text",
    );
    const button = document.createElement("button");
    button.type = "button";
    button.className = "button quiet";
    button.textContent = "Open verified artifact";
    button.disabled = !artifact.verified;
    const output = document.createElement("pre");
    output.hidden = true;
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const result = await handlers.onArtifact(component.run_id, artifact);
        output.textContent = result.kind === "text"
          ? result.text
          : `Binary artifact (${result.mediaType}, ${result.bytes.byteLength} bytes).`;
        output.hidden = false;
      } catch (error) {
        output.textContent = error instanceof Error ? error.message : "Artifact unavailable.";
        output.hidden = false;
      } finally {
        button.disabled = !artifact.verified;
      }
    });
    item.append(identity, metadata, button, output);
    list.append(item);
  }
  if (component.items.length === 0) {
    list.append(paragraph("No run artifacts.", "empty-state"));
  }
  card.append(list);
  return card;
}

function renderSource(component, handlers) {
  const details = document.createElement("details");
  details.className = "component-card source-disclosure";
  details.dataset.componentId = component.component_id;
  const summary = document.createElement("summary");
  summary.textContent = component.label;
  const description = paragraph(component.summary, "quiet-text");
  const button = document.createElement("button");
  button.type = "button";
  button.className = "button quiet";
  button.textContent = "Load canonical JSON";
  const output = document.createElement("pre");
  output.hidden = true;
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      output.textContent = await handlers.onSource(component.operation, component.subject_id);
    } catch (error) {
      output.textContent = error instanceof Error ? error.message : "Source unavailable.";
    } finally {
      output.hidden = false;
      button.disabled = false;
    }
  });
  details.append(summary, description, button, output);
  return details;
}

function componentCard(component) {
  const card = document.createElement("article");
  card.className = "component-card";
  card.dataset.componentId = component.component_id;
  const heading = document.createElement("h4");
  heading.textContent = component.label;
  card.append(heading);
  return card;
}

function paragraph(text, className) {
  const element = document.createElement("p");
  element.className = className;
  element.textContent = text;
  return element;
}

function formatScalar(value) {
  if (value === null || value === undefined) {
    return "—";
  }
  if (typeof value === "boolean") {
    return value ? "yes" : "no";
  }
  return String(value);
}

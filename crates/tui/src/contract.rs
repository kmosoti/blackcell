//! Strict serde mirror of the renderer-neutral presentation contract.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

use serde::{Deserialize, Serialize};
use serde_json::Value;
use thiserror::Error;

const SURFACE_SCHEMA: &str = "presentation-surface/v1";
const MAX_COMPONENTS: usize = 512;
const MAX_ITEMS: usize = 4_096;

#[derive(Debug, Error, Clone, Copy, PartialEq, Eq)]
pub enum ContractError {
    #[error("invalid-presentation-surface")]
    InvalidSurface,
    #[error("invalid-presentation-component")]
    InvalidComponent,
    #[error("invalid-section-binding")]
    InvalidSection,
    #[error("invalid-action-binding")]
    InvalidAction,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct PresentationSurface {
    pub schema_version: String,
    pub surface_id: String,
    pub title: String,
    pub revision: SurfaceRevision,
    pub components: Vec<Component>,
    pub field_dispositions: Vec<FieldDisposition>,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct SurfaceRevision {
    pub number: u64,
    pub event_cursor: u64,
    pub source_digest: String,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct SourceBinding {
    pub kind: String,
    pub identity: String,
    pub digest: String,
    pub json_pointer: Option<String>,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct FieldDisposition {
    pub contract: String,
    pub json_pointer: String,
    pub disposition: String,
    pub reason: String,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
#[serde(tag = "kind")]
pub enum Component {
    #[serde(rename = "section")]
    Section(SectionComponent),
    #[serde(rename = "status")]
    Status(StatusComponent),
    #[serde(rename = "metrics")]
    Metrics(MetricComponent),
    #[serde(rename = "key-value")]
    KeyValue(KeyValueComponent),
    #[serde(rename = "table")]
    Table(TableComponent),
    #[serde(rename = "form")]
    Form(FormComponent),
    #[serde(rename = "plan-graph")]
    PlanGraph(PlanGraphComponent),
    #[serde(rename = "timeline")]
    Timeline(TimelineComponent),
    #[serde(rename = "findings")]
    Findings(FindingComponent),
    #[serde(rename = "evidence-matrix")]
    EvidenceMatrix(EvidenceMatrixComponent),
    #[serde(rename = "artifacts")]
    Artifacts(ArtifactComponent),
    #[serde(rename = "source")]
    Source(SourceComponent),
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct SectionComponent {
    pub component_id: String,
    pub label: String,
    pub source: Option<SourceBinding>,
    pub description: String,
    pub children: Vec<String>,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct StatusComponent {
    pub component_id: String,
    pub label: String,
    pub source: Option<SourceBinding>,
    pub value: String,
    pub tone: String,
    pub detail: String,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct MetricComponent {
    pub component_id: String,
    pub label: String,
    pub source: Option<SourceBinding>,
    pub metrics: Vec<Metric>,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Metric {
    pub label: String,
    pub value: Value,
    pub unit: String,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct KeyValueComponent {
    pub component_id: String,
    pub label: String,
    pub source: Option<SourceBinding>,
    pub items: Vec<KeyValueItem>,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct KeyValueItem {
    pub key: String,
    pub value: Value,
    pub provenance: String,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct TableComponent {
    pub component_id: String,
    pub label: String,
    pub source: Option<SourceBinding>,
    pub columns: Vec<TableColumn>,
    pub rows: Vec<TableRow>,
    pub empty_message: String,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct TableColumn {
    pub key: String,
    pub label: String,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct TableRow {
    pub row_id: String,
    pub cells: BTreeMap<String, Value>,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct FormComponent {
    pub component_id: String,
    pub label: String,
    pub source: Option<SourceBinding>,
    pub description: String,
    pub action: ActionBinding,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ActionBinding {
    pub action_id: String,
    pub operation: String,
    pub label: String,
    pub request_schema: String,
    pub fields: Vec<FieldBinding>,
    pub confirmation: Option<String>,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct FieldBinding {
    pub field_id: String,
    pub json_pointer: String,
    pub label: String,
    pub help: String,
    pub control: String,
    pub required: bool,
    pub sensitive: bool,
    pub default: Value,
    pub options: Vec<FieldOption>,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct FieldOption {
    pub value: String,
    pub label: String,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct PlanGraphComponent {
    pub component_id: String,
    pub label: String,
    pub source: Option<SourceBinding>,
    pub nodes: Vec<GraphNode>,
    pub edges: Vec<GraphEdge>,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct GraphNode {
    pub node_id: String,
    pub label: String,
    pub status: String,
    pub detail: String,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct GraphEdge {
    pub source_id: String,
    pub target_id: String,
    pub label: String,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct TimelineComponent {
    pub component_id: String,
    pub label: String,
    pub source: Option<SourceBinding>,
    pub items: Vec<TimelineItem>,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct TimelineItem {
    pub item_id: String,
    pub title: String,
    pub detail: String,
    pub cursor: Option<u64>,
    pub status: String,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct FindingComponent {
    pub component_id: String,
    pub label: String,
    pub source: Option<SourceBinding>,
    pub findings: Vec<Finding>,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Finding {
    pub finding_id: String,
    pub severity: String,
    pub summary: String,
    pub evidence: String,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct EvidenceMatrixComponent {
    pub component_id: String,
    pub label: String,
    pub source: Option<SourceBinding>,
    pub rows: Vec<EvidenceRow>,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct EvidenceRow {
    pub row_id: String,
    pub dimension: String,
    pub disposition: String,
    pub evidence: String,
    pub source_digest: String,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ArtifactComponent {
    pub component_id: String,
    pub label: String,
    pub source: Option<SourceBinding>,
    pub run_id: String,
    pub items: Vec<ArtifactItem>,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ArtifactItem {
    pub digest: String,
    pub role: String,
    pub node_id: String,
    pub media_type: String,
    pub size_bytes: u64,
    pub verified: bool,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct SourceComponent {
    pub component_id: String,
    pub label: String,
    pub source: Option<SourceBinding>,
    pub operation: String,
    pub subject_id: String,
    pub summary: String,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct SemanticManifestItem<'a> {
    pub id: &'a str,
    pub kind: &'static str,
    pub label: &'a str,
    pub source_digest: Option<&'a str>,
    pub action: Option<&'a str>,
}

macro_rules! component_property {
    ($value:expr, $field:ident) => {
        match $value {
            Component::Section(value) => value.$field.as_str(),
            Component::Status(value) => value.$field.as_str(),
            Component::Metrics(value) => value.$field.as_str(),
            Component::KeyValue(value) => value.$field.as_str(),
            Component::Table(value) => value.$field.as_str(),
            Component::Form(value) => value.$field.as_str(),
            Component::PlanGraph(value) => value.$field.as_str(),
            Component::Timeline(value) => value.$field.as_str(),
            Component::Findings(value) => value.$field.as_str(),
            Component::EvidenceMatrix(value) => value.$field.as_str(),
            Component::Artifacts(value) => value.$field.as_str(),
            Component::Source(value) => value.$field.as_str(),
        }
    };
}

impl PresentationSurface {
    pub fn decode(content: &[u8]) -> Result<Self, ContractError> {
        let surface: Self =
            serde_json::from_slice(content).map_err(|_| ContractError::InvalidSurface)?;
        surface.validate()?;
        Ok(surface)
    }

    pub fn validate(&self) -> Result<(), ContractError> {
        if self.schema_version != SURFACE_SCHEMA
            || !valid_surface_id(&self.surface_id)
            || self.title.is_empty()
            || self.title.len() > 240
            || !valid_digest(&self.revision.source_digest)
            || self.components.len() > MAX_COMPONENTS
            || self.field_dispositions.len() > 512
        {
            return Err(ContractError::InvalidSurface);
        }
        let mut identifiers = HashSet::new();
        let mut action_ids = HashSet::new();
        for component in &self.components {
            if !valid_id(component.id())
                || component.label().is_empty()
                || component.label().len() > 240
                || !identifiers.insert(component.id())
                || component
                    .source()
                    .is_some_and(|source| !valid_source(source))
            {
                return Err(ContractError::InvalidComponent);
            }
            component.validate()?;
            if let Component::Form(form) = component
                && !action_ids.insert(form.action.action_id.as_str())
            {
                return Err(ContractError::InvalidAction);
            }
        }
        let mut dispositions = HashSet::new();
        for disposition in &self.field_dispositions {
            if !valid_text(&disposition.contract, 1, 120)
                || !valid_pointer(&disposition.json_pointer)
                || !["editable", "displayed", "derived", "hidden"]
                    .contains(&disposition.disposition.as_str())
                || !valid_text(&disposition.reason, 1, 500)
                || !dispositions.insert((
                    disposition.contract.as_str(),
                    disposition.json_pointer.as_str(),
                ))
            {
                return Err(ContractError::InvalidSurface);
            }
        }
        let sections: HashMap<&str, &[String]> = self
            .components
            .iter()
            .filter_map(|component| match component {
                Component::Section(section) => {
                    Some((section.component_id.as_str(), section.children.as_slice()))
                }
                _ => None,
            })
            .collect();
        for children in sections.values() {
            if children
                .iter()
                .any(|child| !identifiers.contains(child.as_str()))
            {
                return Err(ContractError::InvalidSection);
            }
        }
        let mut visiting = HashSet::new();
        let mut visited = HashSet::new();
        for identifier in &identifiers {
            visit_section(identifier, &sections, &mut visiting, &mut visited)?;
        }
        Ok(())
    }

    pub fn semantic_manifest(&self) -> Vec<SemanticManifestItem<'_>> {
        self.components
            .iter()
            .map(|component| SemanticManifestItem {
                id: component.id(),
                kind: component.kind(),
                label: component.label(),
                source_digest: component.source().map(|source| source.digest.as_str()),
                action: match component {
                    Component::Form(form) => Some(form.action.operation.as_str()),
                    _ => None,
                },
            })
            .collect()
    }
}

impl Component {
    pub fn id(&self) -> &str {
        component_property!(self, component_id)
    }

    pub fn label(&self) -> &str {
        component_property!(self, label)
    }

    pub fn source(&self) -> Option<&SourceBinding> {
        match self {
            Self::Section(value) => value.source.as_ref(),
            Self::Status(value) => value.source.as_ref(),
            Self::Metrics(value) => value.source.as_ref(),
            Self::KeyValue(value) => value.source.as_ref(),
            Self::Table(value) => value.source.as_ref(),
            Self::Form(value) => value.source.as_ref(),
            Self::PlanGraph(value) => value.source.as_ref(),
            Self::Timeline(value) => value.source.as_ref(),
            Self::Findings(value) => value.source.as_ref(),
            Self::EvidenceMatrix(value) => value.source.as_ref(),
            Self::Artifacts(value) => value.source.as_ref(),
            Self::Source(value) => value.source.as_ref(),
        }
    }

    pub const fn kind(&self) -> &'static str {
        match self {
            Self::Section(_) => "section",
            Self::Status(_) => "status",
            Self::Metrics(_) => "metrics",
            Self::KeyValue(_) => "key-value",
            Self::Table(_) => "table",
            Self::Form(_) => "form",
            Self::PlanGraph(_) => "plan-graph",
            Self::Timeline(_) => "timeline",
            Self::Findings(_) => "findings",
            Self::EvidenceMatrix(_) => "evidence-matrix",
            Self::Artifacts(_) => "artifacts",
            Self::Source(_) => "source",
        }
    }

    fn validate(&self) -> Result<(), ContractError> {
        match self {
            Self::Section(value) => validate_section(value),
            Self::Status(value)
                if !valid_text(&value.value, 1, 120)
                    || !["neutral", "info", "success", "warning", "danger"]
                        .contains(&value.tone.as_str())
                    || !valid_text(&value.detail, 0, 2_000) =>
            {
                Err(ContractError::InvalidComponent)
            }
            Self::Metrics(value) => validate_metrics(value),
            Self::KeyValue(value) => validate_key_values(value),
            Self::Table(value) => validate_table(value),
            Self::Form(value) if !valid_text(&value.description, 0, 2_000) => {
                Err(ContractError::InvalidComponent)
            }
            Self::Form(value) => validate_action(&value.action),
            Self::PlanGraph(value) => validate_graph(value),
            Self::Timeline(value) => validate_timeline(value),
            Self::Findings(value) => validate_findings(value),
            Self::EvidenceMatrix(value) => validate_evidence(value),
            Self::Artifacts(value) => validate_artifacts(value),
            Self::Source(value)
                if !["inspect-run", "replay-run"].contains(&value.operation.as_str())
                    || !valid_text(&value.subject_id, 1, 120)
                    || !valid_text(&value.summary, 0, 32_768) =>
            {
                Err(ContractError::InvalidComponent)
            }
            _ => Ok(()),
        }
    }
}

fn validate_action(action: &ActionBinding) -> Result<(), ContractError> {
    const OPERATIONS: [(&str, &str); 6] = [
        ("register-project", "project-request/v1"),
        ("accept-intent", "intent-request/v1"),
        ("accept-plan", "plan-request/v1"),
        ("submit-run", "run-request/v1"),
        ("inspect-run", "run-lookup/v1"),
        ("cancel-run", "execution-cancel-run-request/v1"),
    ];
    let fields: BTreeSet<_> = action
        .fields
        .iter()
        .map(|field| field.field_id.as_str())
        .collect();
    let pointers: BTreeSet<_> = action
        .fields
        .iter()
        .map(|field| field.json_pointer.as_str())
        .collect();
    if !valid_id(&action.action_id)
        || !OPERATIONS.contains(&(action.operation.as_str(), action.request_schema.as_str()))
        || !valid_text(&action.label, 1, 120)
        || !valid_text(&action.request_schema, 1, 120)
        || action.fields.len() > 128
        || fields.len() != action.fields.len()
        || pointers.len() != action.fields.len()
        || action
            .confirmation
            .as_ref()
            .is_some_and(|value| value.len() > 500)
        || action.fields.iter().any(|field| {
            !valid_id(&field.field_id)
                || !valid_pointer(&field.json_pointer)
                || !valid_text(&field.label, 1, 240)
                || !valid_text(&field.help, 0, 1_000)
                || ![
                    "text",
                    "textarea",
                    "number",
                    "checkbox",
                    "select",
                    "string-list",
                    "structured-list",
                ]
                .contains(&field.control.as_str())
                || field.options.len() > 64
                || (field.control == "select") == field.options.is_empty()
                || field.options.iter().any(|option| {
                    !valid_text(&option.value, 0, 2_048) || !valid_text(&option.label, 1, 240)
                })
        })
    {
        return Err(ContractError::InvalidAction);
    }
    Ok(())
}

fn validate_table(table: &TableComponent) -> Result<(), ContractError> {
    let columns: BTreeSet<_> = table
        .columns
        .iter()
        .map(|column| column.key.as_str())
        .collect();
    if columns.len() != table.columns.len()
        || table.columns.is_empty()
        || table.columns.len() > 64
        || table.rows.len() > MAX_ITEMS
        || !valid_text(&table.empty_message, 1, 500)
        || table
            .columns
            .iter()
            .any(|column| !valid_id(&column.key) || !valid_text(&column.label, 1, 240))
        || table
            .rows
            .iter()
            .map(|row| row.row_id.as_str())
            .collect::<HashSet<_>>()
            .len()
            != table.rows.len()
        || table.rows.iter().any(|row| {
            !valid_id(&row.row_id)
                || row.cells.values().any(|value| !presentation_scalar(value))
                || row
                    .cells
                    .keys()
                    .map(String::as_str)
                    .collect::<BTreeSet<_>>()
                    != columns
        })
    {
        return Err(ContractError::InvalidComponent);
    }
    Ok(())
}

fn validate_graph(graph: &PlanGraphComponent) -> Result<(), ContractError> {
    let nodes: HashSet<_> = graph
        .nodes
        .iter()
        .map(|node| node.node_id.as_str())
        .collect();
    if nodes.len() != graph.nodes.len()
        || graph.nodes.len() > 64
        || graph.edges.len() > MAX_ITEMS
        || graph.nodes.iter().any(|node| {
            !valid_id(&node.node_id)
                || !valid_text(&node.label, 1, 240)
                || !valid_text(&node.status, 0, 120)
                || !valid_text(&node.detail, 0, 2_000)
        })
        || graph.edges.iter().any(|edge| {
            !nodes.contains(edge.source_id.as_str())
                || !nodes.contains(edge.target_id.as_str())
                || !valid_text(&edge.label, 0, 120)
        })
    {
        return Err(ContractError::InvalidComponent);
    }
    Ok(())
}

fn validate_section(section: &SectionComponent) -> Result<(), ContractError> {
    let children: HashSet<_> = section.children.iter().map(String::as_str).collect();
    if section.children.len() > 128
        || children.len() != section.children.len()
        || !valid_text(&section.description, 0, 2_000)
        || section.children.iter().any(|child| !valid_id(child))
    {
        return Err(ContractError::InvalidSection);
    }
    Ok(())
}

fn validate_metrics(metrics: &MetricComponent) -> Result<(), ContractError> {
    if metrics.metrics.is_empty()
        || metrics.metrics.len() > 32
        || metrics.metrics.iter().any(|metric| {
            !valid_text(&metric.label, 1, 120)
                || !valid_text(&metric.unit, 0, 40)
                || !presentation_scalar(&metric.value)
        })
    {
        return Err(ContractError::InvalidComponent);
    }
    Ok(())
}

fn validate_key_values(component: &KeyValueComponent) -> Result<(), ContractError> {
    if component.items.len() > MAX_ITEMS
        || component.items.iter().any(|item| {
            !valid_text(&item.key, 1, 240)
                || !valid_text(&item.provenance, 0, 500)
                || !presentation_scalar(&item.value)
        })
    {
        return Err(ContractError::InvalidComponent);
    }
    Ok(())
}

fn validate_timeline(component: &TimelineComponent) -> Result<(), ContractError> {
    if component.items.len() > MAX_ITEMS
        || component.items.iter().any(|item| {
            !valid_id(&item.item_id)
                || !valid_text(&item.title, 1, 240)
                || !valid_text(&item.detail, 0, 2_000)
                || !valid_text(&item.status, 0, 120)
        })
    {
        return Err(ContractError::InvalidComponent);
    }
    Ok(())
}

fn validate_findings(component: &FindingComponent) -> Result<(), ContractError> {
    if component.findings.len() > MAX_ITEMS
        || component.findings.iter().any(|finding| {
            !valid_id(&finding.finding_id)
                || !["P1", "P2", "P3", "info"].contains(&finding.severity.as_str())
                || !valid_text(&finding.summary, 1, 2_000)
                || !valid_text(&finding.evidence, 0, 2_000)
        })
    {
        return Err(ContractError::InvalidComponent);
    }
    Ok(())
}

fn validate_evidence(component: &EvidenceMatrixComponent) -> Result<(), ContractError> {
    if component.rows.len() > 256
        || component.rows.iter().any(|row| {
            !valid_id(&row.row_id)
                || !valid_text(&row.dimension, 1, 240)
                || !["supported", "concern", "unknown", "not-applicable"]
                    .contains(&row.disposition.as_str())
                || !valid_text(&row.evidence, 0, 2_000)
                || !valid_digest(&row.source_digest)
        })
    {
        return Err(ContractError::InvalidComponent);
    }
    Ok(())
}

fn validate_artifacts(component: &ArtifactComponent) -> Result<(), ContractError> {
    if !valid_text(&component.run_id, 1, 120)
        || component.items.len() > MAX_ITEMS
        || component.items.iter().any(|item| {
            !valid_digest(&item.digest)
                || !valid_text(&item.role, 1, 120)
                || !valid_text(&item.node_id, 1, 120)
                || !valid_text(&item.media_type, 1, 240)
        })
    {
        return Err(ContractError::InvalidComponent);
    }
    Ok(())
}

fn visit_section<'a>(
    identifier: &'a str,
    sections: &HashMap<&'a str, &'a [String]>,
    visiting: &mut HashSet<&'a str>,
    visited: &mut HashSet<&'a str>,
) -> Result<(), ContractError> {
    if visited.contains(identifier) {
        return Ok(());
    }
    if !visiting.insert(identifier) {
        return Err(ContractError::InvalidSection);
    }
    if let Some(children) = sections.get(identifier) {
        for child in *children {
            visit_section(child, sections, visiting, visited)?;
        }
    }
    visiting.remove(identifier);
    visited.insert(identifier);
    Ok(())
}

fn valid_id(value: &str) -> bool {
    (1..=120).contains(&value.len())
        && value.as_bytes()[0].is_ascii_alphanumeric()
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'))
}

fn valid_digest(value: &str) -> bool {
    value.len() == 71
        && value.starts_with("sha256:")
        && value[7..]
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn valid_source(source: &SourceBinding) -> bool {
    ["contract", "event", "run", "artifact", "tooling"].contains(&source.kind.as_str())
        && valid_text(&source.identity, 1, 240)
        && valid_digest(&source.digest)
        && source
            .json_pointer
            .as_ref()
            .is_none_or(|value| value.len() <= 1_024)
}

fn valid_pointer(value: &str) -> bool {
    valid_text(value, 1, 1_024) && value.starts_with('/')
}

fn valid_surface_id(value: &str) -> bool {
    valid_id(value) || value.strip_prefix("run:").is_some_and(valid_id)
}

fn valid_text(value: &str, minimum: usize, maximum: usize) -> bool {
    (minimum..=maximum).contains(&value.chars().count())
}

fn presentation_scalar(value: &Value) -> bool {
    value.is_null() || value.is_boolean() || value.is_number() || value.is_string()
}

//! Pure terminal model and deterministic Ratatui rendering.

use std::collections::BTreeMap;
use std::sync::OnceLock;

use ratatui::Frame;
use ratatui::layout::{Constraint, Direction, Layout};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Paragraph, Wrap};
use serde_json::Value;
use thiserror::Error;

use crate::contract::{ActionBinding, Component, FieldBinding, PresentationSurface};

const TOKENS: &str = include_str!("../../../src/blackcell/interfaces/presentation/tokens.json");
const MAX_FORM_VALUE_BYTES: usize = 64 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InputMode {
    Normal,
    RunId,
    ActionSelect,
    ActionEdit,
    ActionConfirm,
}

#[derive(Debug, Error, Clone, Copy, PartialEq, Eq)]
pub enum FormInputError {
    #[error("missing-required-action-field")]
    MissingRequired,
    #[error("invalid-action-field")]
    InvalidField,
    #[error("action-field-too-large")]
    ValueTooLarge,
}

#[derive(Debug, Clone, PartialEq)]
pub struct ActionSubmission {
    pub action: ActionBinding,
    pub values: BTreeMap<String, Value>,
}

#[derive(Debug, Clone)]
pub struct FormEditor {
    pub action: ActionBinding,
    pub field_index: usize,
    values: Vec<String>,
}

#[derive(Debug, Clone)]
pub struct AppModel {
    pub surface: PresentationSurface,
    pub scroll: u16,
    pub input_mode: InputMode,
    pub run_input: String,
    pub action_index: usize,
    pub form_editor: Option<FormEditor>,
    pub message: String,
}

impl AppModel {
    pub fn new(surface: PresentationSurface) -> Self {
        Self {
            surface,
            scroll: 0,
            input_mode: InputMode::Normal,
            run_input: String::new(),
            action_index: 0,
            form_editor: None,
            message: "Surface synchronized with the daemon.".to_owned(),
        }
    }

    pub fn replace_surface(&mut self, surface: PresentationSurface) {
        self.surface = surface;
        self.scroll = 0;
        self.input_mode = InputMode::Normal;
        self.run_input.clear();
        self.action_index = 0;
        self.form_editor = None;
        self.message = "Surface synchronized with the daemon.".to_owned();
    }

    pub fn synchronize_surface(&mut self, surface: PresentationSurface) {
        self.surface = surface;
        if self.input_mode == InputMode::Normal {
            self.scroll = 0;
            self.action_index = 0;
            self.message = "Surface synchronized with the daemon.".to_owned();
        } else if self.input_mode == InputMode::ActionSelect {
            self.action_index = self.action_index.min(self.action_count().saturating_sub(1));
        }
    }

    pub fn current_run_id(&self) -> Option<&str> {
        self.surface.surface_id.strip_prefix("run:")
    }

    pub fn scroll_down(&mut self, amount: u16) {
        self.scroll = self.scroll.saturating_add(amount);
    }

    pub fn scroll_up(&mut self, amount: u16) {
        self.scroll = self.scroll.saturating_sub(amount);
    }

    pub fn begin_action_selection(&mut self) {
        if self.action_count() == 0 {
            self.message = "This surface exposes no actions.".to_owned();
            return;
        }
        self.action_index = self.action_index.min(self.action_count() - 1);
        self.form_editor = None;
        self.input_mode = InputMode::ActionSelect;
    }

    pub fn close_actions(&mut self) {
        self.form_editor = None;
        self.input_mode = InputMode::Normal;
    }

    pub fn select_next_action(&mut self) {
        let count = self.action_count();
        if count > 0 {
            self.action_index = (self.action_index + 1) % count;
        }
    }

    pub fn select_previous_action(&mut self) {
        let count = self.action_count();
        if count > 0 {
            self.action_index = (self.action_index + count - 1) % count;
        }
    }

    pub fn begin_action_edit(&mut self) {
        let Some(action) = self.selected_action().cloned() else {
            self.close_actions();
            return;
        };
        self.form_editor = Some(FormEditor::new(action));
        self.message = "Editing typed action.".to_owned();
        self.input_mode = InputMode::ActionEdit;
    }

    pub fn return_to_action_selection(&mut self) {
        self.form_editor = None;
        self.input_mode = InputMode::ActionSelect;
    }

    pub fn action_requires_confirmation(&self) -> bool {
        self.form_editor
            .as_ref()
            .and_then(|editor| editor.action.confirmation.as_ref())
            .is_some()
    }

    pub fn begin_action_confirmation(&mut self) {
        self.input_mode = InputMode::ActionConfirm;
    }

    pub fn return_to_action_edit(&mut self) {
        self.input_mode = InputMode::ActionEdit;
    }

    pub fn select_next_field(&mut self) {
        if let Some(editor) = &mut self.form_editor {
            editor.select_next_field();
        }
    }

    pub fn select_previous_field(&mut self) {
        if let Some(editor) = &mut self.form_editor {
            editor.select_previous_field();
        }
    }

    pub fn edit_action_character(&mut self, character: char) {
        if !character.is_control()
            && let Some(editor) = &mut self.form_editor
            && let Err(error) = editor.push(character)
        {
            self.message = error.to_string();
        }
    }

    pub fn edit_action_newline(&mut self) {
        let Some(editor) = &mut self.form_editor else {
            return;
        };
        if editor.current_field().is_some_and(|field| {
            matches!(
                field.control.as_str(),
                "textarea" | "string-list" | "structured-list"
            )
        }) {
            if let Err(error) = editor.push('\n') {
                self.message = error.to_string();
            }
        } else {
            editor.select_next_field();
        }
    }

    pub fn edit_action_backspace(&mut self) {
        if let Some(editor) = &mut self.form_editor {
            editor.backspace();
        }
    }

    pub fn edit_action_toggle(&mut self) {
        if let Some(editor) = &mut self.form_editor {
            editor.toggle();
        }
    }

    pub fn edit_action_space(&mut self) {
        let Some(editor) = &mut self.form_editor else {
            return;
        };
        if editor
            .current_field()
            .is_some_and(|field| field.control == "checkbox")
        {
            editor.toggle();
        } else if let Err(error) = editor.push(' ') {
            self.message = error.to_string();
        }
    }

    pub fn select_next_option(&mut self) {
        if let Some(editor) = &mut self.form_editor {
            editor.select_next_option();
        }
    }

    pub fn select_previous_option(&mut self) {
        if let Some(editor) = &mut self.form_editor {
            editor.select_previous_option();
        }
    }

    pub fn set_action_value(&mut self, value: &str) -> Result<(), FormInputError> {
        let Some(editor) = &mut self.form_editor else {
            return Err(FormInputError::InvalidField);
        };
        editor.set_value(value)
    }

    pub fn action_submission(&self) -> Result<ActionSubmission, FormInputError> {
        self.form_editor
            .as_ref()
            .ok_or(FormInputError::InvalidField)?
            .submission()
    }

    pub fn action_failed(&mut self, message: String) {
        if self.input_mode == InputMode::ActionConfirm {
            self.return_to_action_edit();
        }
        self.message = message;
    }

    fn action_count(&self) -> usize {
        self.surface
            .components
            .iter()
            .filter(|component| matches!(component, Component::Form(_)))
            .count()
    }

    fn selected_action(&self) -> Option<&ActionBinding> {
        self.surface
            .components
            .iter()
            .filter_map(|component| match component {
                Component::Form(form) => Some(&form.action),
                _ => None,
            })
            .nth(self.action_index)
    }
}

impl FormEditor {
    fn new(action: ActionBinding) -> Self {
        let values = action
            .fields
            .iter()
            .map(initial_field_value)
            .collect::<Vec<_>>();
        Self {
            action,
            field_index: 0,
            values,
        }
    }

    fn current_field(&self) -> Option<&FieldBinding> {
        self.action.fields.get(self.field_index)
    }

    fn current_value(&self) -> Option<&str> {
        self.values.get(self.field_index).map(String::as_str)
    }

    fn select_next_field(&mut self) {
        if !self.values.is_empty() {
            self.field_index = (self.field_index + 1) % self.values.len();
        }
    }

    fn select_previous_field(&mut self) {
        if !self.values.is_empty() {
            self.field_index = (self.field_index + self.values.len() - 1) % self.values.len();
        }
    }

    fn push(&mut self, character: char) -> Result<(), FormInputError> {
        let value = self
            .values
            .get_mut(self.field_index)
            .ok_or(FormInputError::InvalidField)?;
        if value.len().saturating_add(character.len_utf8()) > MAX_FORM_VALUE_BYTES {
            return Err(FormInputError::ValueTooLarge);
        }
        value.push(character);
        Ok(())
    }

    fn backspace(&mut self) {
        if let Some(value) = self.values.get_mut(self.field_index) {
            value.pop();
        }
    }

    fn toggle(&mut self) {
        if self
            .current_field()
            .is_none_or(|field| field.control != "checkbox")
        {
            return;
        }
        if let Some(value) = self.values.get_mut(self.field_index) {
            *value = if value == "true" { "false" } else { "true" }.to_owned();
        }
    }

    fn select_next_option(&mut self) {
        self.select_option(1);
    }

    fn select_previous_option(&mut self) {
        let count = self.current_field().map_or(0, |field| field.options.len());
        if count > 0 {
            self.select_option(count - 1);
        }
    }

    fn select_option(&mut self, increment: usize) {
        let Some(field) = self.current_field() else {
            return;
        };
        if field.control != "select" || field.options.is_empty() {
            return;
        }
        let options = field.options.clone();
        let current = self.current_value().unwrap_or_default();
        let index = options
            .iter()
            .position(|option| option.value == current)
            .unwrap_or(0);
        if let Some(value) = self.values.get_mut(self.field_index) {
            *value = options[(index + increment) % options.len()].value.clone();
        }
    }

    fn set_value(&mut self, value: &str) -> Result<(), FormInputError> {
        if value.len() > MAX_FORM_VALUE_BYTES {
            return Err(FormInputError::ValueTooLarge);
        }
        let selected = self
            .values
            .get_mut(self.field_index)
            .ok_or(FormInputError::InvalidField)?;
        *selected = value.to_owned();
        Ok(())
    }

    fn submission(&self) -> Result<ActionSubmission, FormInputError> {
        let mut values = BTreeMap::new();
        for (field, raw) in self.action.fields.iter().zip(&self.values) {
            let key = field
                .json_pointer
                .strip_prefix('/')
                .filter(|value| !value.is_empty() && !value.contains('/'))
                .ok_or(FormInputError::InvalidField)?;
            if field.required
                && raw.trim().is_empty()
                && !matches!(field.control.as_str(), "checkbox" | "string-list")
            {
                return Err(FormInputError::MissingRequired);
            }
            values.insert(key.to_owned(), parse_field_value(field, raw)?);
        }
        Ok(ActionSubmission {
            action: self.action.clone(),
            values,
        })
    }
}

fn initial_field_value(field: &FieldBinding) -> String {
    match (&field.control[..], &field.default) {
        (_, Value::Null) => String::new(),
        ("string-list", Value::Array(values)) => values
            .iter()
            .filter_map(Value::as_str)
            .collect::<Vec<_>>()
            .join("\n"),
        (_, Value::String(value)) => value.clone(),
        (_, value) => value.to_string(),
    }
}

fn parse_field_value(field: &FieldBinding, raw: &str) -> Result<Value, FormInputError> {
    match field.control.as_str() {
        "checkbox" => raw
            .parse::<bool>()
            .map(Value::Bool)
            .map_err(|_| FormInputError::InvalidField),
        "number" => serde_json::from_str(raw)
            .ok()
            .filter(Value::is_number)
            .ok_or(FormInputError::InvalidField),
        "select" => field
            .options
            .iter()
            .find(|option| option.value == raw)
            .map(|option| Value::String(option.value.clone()))
            .ok_or(FormInputError::InvalidField),
        "string-list" => Ok(Value::Array(
            raw.lines()
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .map(|value| Value::String(value.to_owned()))
                .collect(),
        )),
        "structured-list" => serde_json::from_str(raw)
            .ok()
            .filter(Value::is_array)
            .ok_or(FormInputError::InvalidField),
        "text" | "textarea" => Ok(Value::String(raw.to_owned())),
        _ => Err(FormInputError::InvalidField),
    }
}

pub fn view(frame: &mut Frame<'_>, model: &AppModel) {
    let palette = palette();
    let layout = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(3),
            Constraint::Min(5),
            Constraint::Length(3),
        ])
        .split(frame.area());
    let header = Paragraph::new(Line::from(vec![
        Span::styled(
            "BlackCell ",
            Style::default()
                .fg(palette.accent)
                .add_modifier(Modifier::BOLD),
        ),
        Span::raw(&model.surface.title),
        Span::styled(
            format!(
                "  revision {} · cursor {}",
                model.surface.revision.number, model.surface.revision.event_cursor
            ),
            Style::default().fg(palette.muted),
        ),
    ]))
    .block(
        Block::default()
            .borders(Borders::ALL)
            .border_style(Style::default().fg(palette.muted)),
    );
    frame.render_widget(header, layout[0]);

    let body = Paragraph::new(surface_lines(&model.surface))
        .block(
            Block::default()
                .borders(Borders::ALL)
                .title(" Semantic surface ")
                .border_style(Style::default().fg(palette.accent)),
        )
        .wrap(Wrap { trim: false })
        .scroll((model.scroll, 0));
    frame.render_widget(body, layout[1]);

    let footer_text = match model.input_mode {
        InputMode::Normal => format!(
            "{}  ·  q quit · a actions · w workspace · r inspect run · c cancel run · j/k scroll",
            model.message
        ),
        InputMode::RunId => format!("Run ID: {}_  ·  Enter open · Esc cancel", model.run_input),
        InputMode::ActionSelect => {
            let action = model
                .selected_action()
                .map_or("No action", |action| action.label.as_str());
            format!("Action: {action}  ·  ↑/↓ choose · Enter edit · Esc close")
        }
        InputMode::ActionEdit => action_footer(model),
        InputMode::ActionConfirm => {
            let confirmation = model
                .form_editor
                .as_ref()
                .and_then(|editor| editor.action.confirmation.as_deref())
                .unwrap_or("Submit this action?");
            format!("{confirmation}  ·  y confirm · n/Esc return")
        }
    };
    let footer = Paragraph::new(footer_text)
        .style(Style::default().fg(palette.muted))
        .block(Block::default().borders(Borders::ALL));
    frame.render_widget(footer, layout[2]);
}

fn action_footer(model: &AppModel) -> String {
    let Some(editor) = &model.form_editor else {
        return "Action unavailable · Esc close".to_owned();
    };
    let Some(field) = editor.current_field() else {
        return format!(
            "{} · {} · no fields · Ctrl+S submit · Esc actions",
            model.message, editor.action.label
        );
    };
    let value = if field.sensitive {
        "••••••".to_owned()
    } else {
        editor
            .current_value()
            .unwrap_or_default()
            .replace('\n', "↵")
            .chars()
            .take(120)
            .collect()
    };
    format!(
        "{} · {} · {}: {}_ · Tab/Shift+Tab field · Ctrl+S submit · Esc actions",
        model.message, editor.action.label, field.label, value
    )
}

pub fn surface_lines(surface: &PresentationSurface) -> Vec<Line<'static>> {
    let mut lines = vec![
        Line::styled(
            format!("Source: {}", surface.revision.source_digest),
            Style::default().fg(palette().muted),
        ),
        Line::default(),
    ];
    for component in &surface.components {
        lines.push(Line::styled(
            format!("{}  [{}]", component.label(), component.kind()),
            Style::default()
                .fg(palette().accent)
                .add_modifier(Modifier::BOLD),
        ));
        append_component(component, &mut lines);
        lines.push(Line::default());
    }
    if surface.components.is_empty() {
        lines.push(Line::raw("This surface has no components."));
    }
    lines
}

fn append_component(component: &Component, lines: &mut Vec<Line<'static>>) {
    match component {
        Component::Section(value) => {
            if !value.description.is_empty() {
                lines.push(Line::raw(value.description.clone()));
            }
            lines.push(Line::raw(format!(
                "Contains: {}",
                value.children.join(", ")
            )));
        }
        Component::Status(value) => {
            lines.push(Line::styled(
                format!("{} · {}", value.value, value.detail),
                Style::default().fg(tone(&value.tone)),
            ));
        }
        Component::Metrics(value) => {
            for metric in &value.metrics {
                lines.push(Line::raw(format!(
                    "  {}: {}{}",
                    metric.label,
                    scalar(&metric.value),
                    if metric.unit.is_empty() {
                        String::new()
                    } else {
                        format!(" {}", metric.unit)
                    }
                )));
            }
        }
        Component::KeyValue(value) => {
            for item in &value.items {
                lines.push(Line::raw(format!(
                    "  {}: {}",
                    item.key,
                    scalar(&item.value)
                )));
            }
        }
        Component::Table(value) => {
            lines.push(Line::styled(
                value
                    .columns
                    .iter()
                    .map(|column| column.label.as_str())
                    .collect::<Vec<_>>()
                    .join(" │ "),
                Style::default().add_modifier(Modifier::UNDERLINED),
            ));
            if value.rows.is_empty() {
                lines.push(Line::raw(value.empty_message.clone()));
            }
            for row in &value.rows {
                lines.push(Line::raw(
                    value
                        .columns
                        .iter()
                        .map(|column| scalar(row.cells.get(&column.key).unwrap_or(&Value::Null)))
                        .collect::<Vec<_>>()
                        .join(" │ "),
                ));
            }
        }
        Component::Form(value) => {
            lines.push(Line::raw(format!(
                "  Action: {} ({})",
                value.action.label, value.action.operation
            )));
            for field in &value.action.fields {
                lines.push(Line::raw(format!(
                    "    {} [{}]{}",
                    field.label,
                    field.control,
                    if field.required { " required" } else { "" }
                )));
            }
        }
        Component::PlanGraph(value) => {
            for node in &value.nodes {
                let dependencies = value
                    .edges
                    .iter()
                    .filter(|edge| edge.target_id == node.node_id)
                    .map(|edge| edge.source_id.as_str())
                    .collect::<Vec<_>>();
                lines.push(Line::raw(format!(
                    "  {} ← {} · {} · {}",
                    node.node_id,
                    if dependencies.is_empty() {
                        "root".to_owned()
                    } else {
                        dependencies.join(", ")
                    },
                    node.status,
                    node.label
                )));
            }
        }
        Component::Timeline(value) => {
            for item in &value.items {
                lines.push(Line::raw(format!(
                    "  {} · {} · {}",
                    item.title, item.status, item.detail
                )));
            }
        }
        Component::Findings(value) => {
            if value.findings.is_empty() {
                lines.push(Line::raw("  No replay findings."));
            }
            for finding in &value.findings {
                lines.push(Line::styled(
                    format!(
                        "  {} · {} · {}",
                        finding.severity, finding.summary, finding.evidence
                    ),
                    Style::default().fg(if finding.severity == "P1" || finding.severity == "P2" {
                        palette().danger
                    } else {
                        palette().muted
                    }),
                ));
            }
        }
        Component::EvidenceMatrix(value) => {
            for row in &value.rows {
                lines.push(Line::raw(format!(
                    "  {} │ {} │ {} │ {}",
                    row.dimension, row.disposition, row.evidence, row.source_digest
                )));
            }
        }
        Component::Artifacts(value) => {
            if value.items.is_empty() {
                lines.push(Line::raw("  No run artifacts."));
            }
            for item in &value.items {
                lines.push(Line::raw(format!(
                    "  {} · {} · {} bytes · verified={} · {}",
                    item.node_id, item.role, item.size_bytes, item.verified, item.digest
                )));
            }
        }
        Component::Source(value) => {
            lines.push(Line::raw(format!(
                "  {} {} · {}",
                value.operation, value.subject_id, value.summary
            )));
        }
    }
}

fn scalar(value: &Value) -> String {
    match value {
        Value::Null => "—".to_owned(),
        Value::Bool(value) => if *value { "yes" } else { "no" }.to_owned(),
        Value::String(value) => value.clone(),
        other => other.to_string(),
    }
}

fn tone(value: &str) -> Color {
    match value {
        "success" => palette().success,
        "warning" => palette().warning,
        "danger" => palette().danger,
        "info" => palette().accent,
        _ => palette().text,
    }
}

#[derive(Debug, Clone, Copy)]
struct Palette {
    text: Color,
    muted: Color,
    accent: Color,
    success: Color,
    warning: Color,
    danger: Color,
}

fn palette() -> &'static Palette {
    static PALETTE: OnceLock<Palette> = OnceLock::new();
    PALETTE.get_or_init(|| {
        let tokens: Value = serde_json::from_str(TOKENS).unwrap_or(Value::Null);
        Palette {
            text: token_color(&tokens, "text", Color::White),
            muted: token_color(&tokens, "muted", Color::Gray),
            accent: token_color(&tokens, "accent", Color::Cyan),
            success: token_color(&tokens, "success", Color::Green),
            warning: token_color(&tokens, "warning", Color::Yellow),
            danger: token_color(&tokens, "danger", Color::Red),
        }
    })
}

fn token_color(tokens: &Value, name: &str, fallback: Color) -> Color {
    let Some(value) = tokens
        .get("color")
        .and_then(|value| value.get(name))
        .and_then(|value| value.get("$value"))
        .and_then(|value| value.get("hex"))
        .and_then(Value::as_str)
    else {
        return fallback;
    };
    if value.len() != 7 || !value.starts_with('#') {
        return fallback;
    }
    let parsed = u32::from_str_radix(&value[1..], 16).ok();
    parsed.map_or(fallback, |rgb| {
        Color::Rgb((rgb >> 16) as u8, (rgb >> 8) as u8, rgb as u8)
    })
}

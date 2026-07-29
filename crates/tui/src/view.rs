//! Pure terminal model and deterministic Ratatui rendering.

use std::sync::OnceLock;

use ratatui::Frame;
use ratatui::layout::{Constraint, Direction, Layout};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Paragraph, Wrap};
use serde_json::Value;

use crate::contract::{Component, PresentationSurface};

const TOKENS: &str = include_str!("../../../src/blackcell/interfaces/presentation/tokens.json");

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum InputMode {
    Normal,
    RunId,
}

#[derive(Debug, Clone)]
pub struct AppModel {
    pub surface: PresentationSurface,
    pub scroll: u16,
    pub input_mode: InputMode,
    pub run_input: String,
    pub message: String,
}

impl AppModel {
    pub fn new(surface: PresentationSurface) -> Self {
        Self {
            surface,
            scroll: 0,
            input_mode: InputMode::Normal,
            run_input: String::new(),
            message: "Surface synchronized with the daemon.".to_owned(),
        }
    }

    pub fn replace_surface(&mut self, surface: PresentationSurface) {
        self.surface = surface;
        self.scroll = 0;
        self.message = "Surface synchronized with the daemon.".to_owned();
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
            "{}  ·  q quit · w workspace · r inspect run · c cancel run · j/k scroll",
            model.message
        ),
        InputMode::RunId => format!("Run ID: {}_  ·  Enter open · Esc cancel", model.run_input),
    };
    let footer = Paragraph::new(footer_text)
        .style(Style::default().fg(palette.muted))
        .block(Block::default().borders(Borders::ALL));
    frame.render_widget(footer, layout[2]);
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

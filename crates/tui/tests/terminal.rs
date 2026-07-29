use std::collections::HashMap;
use std::fs;
use std::os::unix::fs::{PermissionsExt, symlink};

use blackcell_terminal::config::{
    API_TOKEN_ENV, API_TOKEN_FILE_ENV, Config, ConfigError, ParseOutcome,
};
use blackcell_terminal::contract::{ContractError, PresentationSurface};
use blackcell_terminal::view::{AppModel, view};
use proptest::prelude::*;
use ratatui::Terminal;
use ratatui::backend::TestBackend;
use serde_json::{Value, json};

const TOKEN: &str = "Native-terminal-token.0123456789-ABCDEFG";
const SCENARIO: &str = include_str!("../../../tests/ui/review-workflow.json");

#[test]
fn closed_contract_preserves_semantic_manifest() {
    let surface = sample_surface();
    let manifest = serde_json::to_value(surface.semantic_manifest()).unwrap();
    let scenario: Value = serde_json::from_str(SCENARIO).unwrap();
    let expected = scenario["surface_expectations"][1]["manifest"]
        .as_array()
        .unwrap()
        .iter()
        .map(|item| {
            json!({
                "id": item["component_id"],
                "kind": item["kind"],
                "label": item["label"],
                "sourceDigest": item["source_digest"],
                "action": item["action"],
            })
        })
        .collect::<Vec<_>>();

    assert_eq!(manifest, Value::Array(expected));
}

#[test]
fn closed_contract_rejects_unknown_fields() {
    let mut value = sample_value();
    value["components"][1]["renderer_hint"] = json!("trust-me");

    assert_eq!(
        PresentationSurface::decode(&serde_json::to_vec(&value).unwrap()).unwrap_err(),
        ContractError::InvalidSurface
    );
}

#[test]
fn configuration_keeps_credentials_out_of_arguments() {
    let environment = HashMap::from([(API_TOKEN_ENV.to_owned(), TOKEN.to_owned())]);
    let outcome = Config::parse_from(
        [
            "--endpoint",
            "https://runtime.example",
            "--refresh-seconds",
            "none",
            "--frames-per-second",
            "30",
        ],
        &environment,
    )
    .unwrap();
    let ParseOutcome::Run(config) = outcome else {
        panic!("expected runnable configuration");
    };

    assert_eq!(config.endpoint.as_str(), "https://runtime.example/");
    assert!(config.refresh.is_none());
    assert_eq!(config.frames_per_second, 30);
    assert!(!format!("{:?}", config.token).contains(TOKEN));
}

#[test]
fn token_files_must_be_absolute_owner_only_regular_files() {
    let directory = tempfile::tempdir().unwrap();
    let token_path = directory.path().join("token");
    fs::write(&token_path, format!("{TOKEN}\n")).unwrap();
    fs::set_permissions(&token_path, fs::Permissions::from_mode(0o600)).unwrap();
    let environment = HashMap::from([(
        API_TOKEN_FILE_ENV.to_owned(),
        token_path.to_string_lossy().into_owned(),
    )]);

    let ParseOutcome::Run(config) =
        Config::parse_from(std::iter::empty::<&str>(), &environment).unwrap()
    else {
        panic!("expected runnable configuration");
    };
    assert!(!format!("{:?}", config.token).contains(TOKEN));

    fs::set_permissions(&token_path, fs::Permissions::from_mode(0o640)).unwrap();
    assert!(matches!(
        Config::parse_from(std::iter::empty::<&str>(), &environment),
        Err(ConfigError::UnsafeSecretFile)
    ));

    fs::set_permissions(&token_path, fs::Permissions::from_mode(0o600)).unwrap();
    let link_path = directory.path().join("token-link");
    symlink(&token_path, &link_path).unwrap();
    let link_environment = HashMap::from([(
        API_TOKEN_FILE_ENV.to_owned(),
        link_path.to_string_lossy().into_owned(),
    )]);
    assert!(matches!(
        Config::parse_from(std::iter::empty::<&str>(), &link_environment),
        Err(ConfigError::UnsafeSecretFile)
    ));
}

#[test]
fn terminal_buffer_is_deterministic_at_standard_size() {
    insta::assert_snapshot!(render_at(120, 48, 0));
}

#[test]
fn terminal_buffer_remains_readable_at_narrow_size() {
    let rendered = render_at(60, 32, 0);
    let scrolled = render_at(60, 32, 18);

    assert!(rendered.contains("BlackCell Run run-1"));
    assert!(rendered.contains("Run status  [status]"));
    assert!(scrolled.contains("P1 · Acceptance evidence"));
    assert!(scrolled.contains("q quit"));
}

proptest! {
    #[test]
    fn scrolling_is_saturating(down in any::<u16>(), up in any::<u16>()) {
        let mut model = AppModel::new(sample_surface());
        model.scroll_down(down);
        model.scroll_up(up);

        prop_assert_eq!(model.scroll, down.saturating_sub(up));
    }
}

fn render_at(width: u16, height: u16, scroll: u16) -> String {
    let backend = TestBackend::new(width, height);
    let mut terminal = Terminal::new(backend).unwrap();
    let mut model = AppModel::new(sample_surface());
    model.scroll = scroll;
    terminal.draw(|frame| view(frame, &model)).unwrap();
    let buffer = terminal.backend().buffer();
    (0..height)
        .map(|y| {
            let line = (0..width)
                .map(|x| buffer[(x, y)].symbol())
                .collect::<String>();
            line.trim_end().to_owned()
        })
        .collect::<Vec<_>>()
        .join("\n")
        .trim_end()
        .to_owned()
}

fn sample_surface() -> PresentationSurface {
    PresentationSurface::decode(&serde_json::to_vec(&sample_value()).unwrap()).unwrap()
}

fn sample_value() -> Value {
    serde_json::from_str::<Value>(SCENARIO).unwrap()["surfaces"][1].clone()
}

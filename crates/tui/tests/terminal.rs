use std::collections::HashMap;
use std::fs;
use std::os::unix::fs::{PermissionsExt, symlink};
use std::process::Command;

use blackcell_terminal::client::build_action_request;
use blackcell_terminal::config::{
    API_TOKEN_ENV, API_TOKEN_FILE_ENV, Config, ConfigError, ParseOutcome,
};
use blackcell_terminal::contract::{ActionBinding, Component, ContractError, PresentationSurface};
use blackcell_terminal::view::{AppModel, FormInputError, InputMode, view};
use proptest::prelude::*;
use ratatui::Terminal;
use ratatui::backend::TestBackend;
use serde_json::{Value, json};

const TOKEN: &str = "Native-terminal-token.0123456789-ABCDEFG";
const SCENARIO: &str = include_str!("../../../tests/ui/review-workflow.json");

#[test]
fn preterminal_failures_use_the_json_error_contract() {
    let output = Command::new(env!("CARGO_BIN_EXE_blackcell-tui"))
        .arg("--unsupported")
        .env_remove(API_TOKEN_ENV)
        .env_remove(API_TOKEN_FILE_ENV)
        .output()
        .unwrap();

    assert_eq!(output.status.code(), Some(2));
    assert!(output.stdout.is_empty());
    assert_eq!(
        serde_json::from_slice::<Value>(&output.stderr).unwrap(),
        json!({"error": {"message": "invalid-tui-arguments"}})
    );
}

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
fn closed_contract_reserves_run_surface_prefix_space() {
    let mut value = sample_value();
    value["surface_id"] = json!(format!("run:{}", "r".repeat(120)));

    PresentationSurface::decode(&serde_json::to_vec(&value).unwrap()).unwrap();

    value["surface_id"] = json!("r".repeat(121));
    assert_eq!(
        PresentationSurface::decode(&serde_json::to_vec(&value).unwrap()).unwrap_err(),
        ContractError::InvalidSurface
    );

    value["surface_id"] = json!(format!("run::{}", "r".repeat(119)));
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

#[test]
fn terminal_form_editor_builds_typed_values_from_surface_bindings() {
    let mut model = AppModel::new(workspace_surface());

    model.begin_action_selection();
    assert_eq!(model.input_mode, InputMode::ActionSelect);
    model.begin_action_edit();
    assert_eq!(model.input_mode, InputMode::ActionEdit);
    model.select_next_option();

    let submission = model.action_submission().unwrap();
    assert_eq!(submission.action.operation, "accept-plan");
    assert_eq!(submission.values["planning_mode"], json!("generated"));
    assert_eq!(
        build_action_request(&submission.action, &submission.values).unwrap(),
        json!({
            "schema_version": "plan-request/v1",
            "planning_mode": "generated",
        })
    );
}

#[test]
fn typed_action_builder_supports_every_mutating_workspace_operation() {
    let operations = [
        ("register-project", "project-request/v1"),
        ("accept-intent", "intent-request/v1"),
        ("accept-plan", "plan-request/v1"),
        ("submit-run", "run-request/v1"),
    ];
    for (operation, schema) in operations {
        let action = action_binding(operation, schema);
        let values = std::collections::BTreeMap::from([("value".to_owned(), json!("typed"))]);

        assert_eq!(
            build_action_request(&action, &values).unwrap(),
            json!({"schema_version": schema, "value": "typed"})
        );
    }
}

#[test]
fn background_synchronization_preserves_active_terminal_input() {
    let surface = workspace_surface();
    let mut action_model = AppModel::new(surface.clone());
    action_model.begin_action_selection();
    action_model.begin_action_edit();
    action_model.select_next_option();

    action_model.synchronize_surface(surface.clone());

    assert_eq!(action_model.input_mode, InputMode::ActionEdit);
    assert_eq!(
        action_model.action_submission().unwrap().values["planning_mode"],
        json!("generated")
    );

    let mut run_model = AppModel::new(surface.clone());
    run_model.input_mode = InputMode::RunId;
    run_model.run_input = "run-in-progress".to_owned();

    run_model.synchronize_surface(surface);

    assert_eq!(run_model.input_mode, InputMode::RunId);
    assert_eq!(run_model.run_input, "run-in-progress");
}

#[test]
fn terminal_form_limits_and_confirmed_failures_are_explicit() {
    let mut model = AppModel::new(workspace_surface());
    model.begin_action_selection();
    model.begin_action_edit();

    assert_eq!(
        model.set_action_value(&"x".repeat(64 * 1024 + 1)),
        Err(FormInputError::ValueTooLarge)
    );
    model.set_action_value(&"x".repeat(64 * 1024)).unwrap();
    model.edit_action_character('x');
    assert_eq!(model.message, "action-field-too-large");

    model.begin_action_confirmation();
    model.action_failed("runtime-request-rejected".to_owned());
    assert_eq!(model.input_mode, InputMode::ActionEdit);
    assert_eq!(model.message, "runtime-request-rejected");
}

#[test]
fn required_string_list_fields_accept_canonical_empty_arrays() {
    let mut surface = workspace_surface();
    let form = surface
        .components
        .iter_mut()
        .find_map(|component| match component {
            Component::Form(form) => Some(form),
            _ => None,
        })
        .unwrap();
    let field = &mut form.action.fields[0];
    field.control = "string-list".to_owned();
    field.options.clear();
    field.default = Value::Null;

    let mut model = AppModel::new(surface);
    model.begin_action_selection();
    model.begin_action_edit();

    assert_eq!(
        model.action_submission().unwrap().values["planning_mode"],
        json!([])
    );
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

fn workspace_surface() -> PresentationSurface {
    let value = serde_json::from_str::<Value>(SCENARIO).unwrap()["surfaces"][0].clone();
    PresentationSurface::decode(&serde_json::to_vec(&value).unwrap()).unwrap()
}

fn sample_value() -> Value {
    serde_json::from_str::<Value>(SCENARIO).unwrap()["surfaces"][1].clone()
}

fn action_binding(operation: &str, schema: &str) -> ActionBinding {
    serde_json::from_value(json!({
        "action_id": operation,
        "operation": operation,
        "label": operation,
        "request_schema": schema,
        "fields": [{
            "field_id": "value",
            "json_pointer": "/value",
            "label": "Value",
            "help": "",
            "control": "text",
            "required": true,
            "sensitive": false,
            "default": null,
            "options": [],
        }],
        "confirmation": null,
    }))
    .unwrap()
}

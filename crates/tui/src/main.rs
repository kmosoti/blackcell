use std::io::{self, Stdout};
use std::process::ExitCode;
use std::time::Duration;

use blackcell_terminal::client::{ClientError, RuntimeClient};
use blackcell_terminal::config::{Config, ConfigError, ParseOutcome, help};
use blackcell_terminal::contract::PresentationSurface;
use blackcell_terminal::view::{ActionSubmission, AppModel, InputMode, view};
use crossterm::event::{Event, EventStream, KeyCode, KeyEventKind, KeyModifiers};
use crossterm::execute;
use crossterm::terminal::{
    EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode,
};
use futures_util::StreamExt;
use ratatui::Terminal;
use ratatui::backend::CrosstermBackend;
use thiserror::Error;
use tokio::sync::mpsc;
use tokio::task::JoinSet;
use tokio::time::{MissedTickBehavior, interval};

#[derive(Debug, Error)]
enum AppError {
    #[error(transparent)]
    Config(#[from] ConfigError),
    #[error(transparent)]
    Client(#[from] ClientError),
    #[error("terminal-io-failed")]
    Terminal,
}

#[tokio::main]
async fn main() -> ExitCode {
    match entry().await {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!(
                "{}",
                serde_json::json!({"error": {"message": error.to_string()}})
            );
            ExitCode::from(if matches!(error, AppError::Config(_)) {
                2
            } else {
                1
            })
        }
    }
}

async fn entry() -> Result<(), AppError> {
    let config = match Config::parse()? {
        ParseOutcome::Help => {
            print!("{}", help());
            return Ok(());
        }
        ParseOutcome::Version => {
            println!("blackcell-tui {}", env!("CARGO_PKG_VERSION"));
            return Ok(());
        }
        ParseOutcome::Run(config) => config,
    };
    let client = RuntimeClient::new(config.endpoint.clone(), config.token.clone())?;
    let surface = client.workspace().await?;
    let mut model = AppModel::new(surface);
    let mut terminal = TerminalSession::start()?;
    run(&mut terminal.terminal, client, config, &mut model).await
}

async fn run(
    terminal: &mut Terminal<CrosstermBackend<Stdout>>,
    client: RuntimeClient,
    config: Config,
    model: &mut AppModel,
) -> Result<(), AppError> {
    let (sender, mut invalidations) = mpsc::channel(16);
    let follower = tokio::spawn(
        client
            .clone()
            .follow_invalidations(model.surface.revision.event_cursor, sender),
    );
    let mut events = EventStream::new();
    let mut render_tick = interval(Duration::from_secs_f64(
        1.0 / f64::from(config.frames_per_second),
    ));
    render_tick.set_missed_tick_behavior(MissedTickBehavior::Skip);
    let mut refresh_tick = interval(config.refresh.unwrap_or(Duration::from_secs(86_400)));
    refresh_tick.set_missed_tick_behavior(MissedTickBehavior::Skip);
    let mut refreshes = JoinSet::new();
    let mut refresh_pending = false;
    let result = loop {
        tokio::select! {
            _ = render_tick.tick() => {
                terminal.draw(|frame| view(frame, model)).map_err(|_| AppError::Terminal)?;
            }
            event = events.next() => {
                let Some(event) = event else { break Ok(()); };
                let event = event.map_err(|_| AppError::Terminal)?;
                if handle_event(event, model, &client).await? {
                    break Ok(());
                }
            }
            cursor = invalidations.recv() => {
                let Some(cursor) = cursor else { break Err(AppError::Client(ClientError::EventStreamFailed)); };
                if cursor > model.surface.revision.event_cursor {
                    queue_surface_refresh(
                        &mut refreshes,
                        &mut refresh_pending,
                        model,
                        &client,
                    );
                }
            }
            _ = refresh_tick.tick(), if config.refresh.is_some() => {
                queue_surface_refresh(
                    &mut refreshes,
                    &mut refresh_pending,
                    model,
                    &client,
                );
            }
            refresh = refreshes.join_next(), if !refreshes.is_empty() => {
                let Some(refresh) = refresh else { continue; };
                if apply_surface_refresh(refresh, model) {
                    refresh_pending = true;
                }
                if std::mem::take(&mut refresh_pending) {
                    queue_surface_refresh(
                        &mut refreshes,
                        &mut refresh_pending,
                        model,
                        &client,
                    );
                }
            }
        }
    };
    follower.abort();
    result
}

async fn handle_event(
    event: Event,
    model: &mut AppModel,
    client: &RuntimeClient,
) -> Result<bool, AppError> {
    let Event::Key(key) = event else {
        return Ok(false);
    };
    if key.kind != KeyEventKind::Press {
        return Ok(false);
    }
    match model.input_mode {
        InputMode::RunId => match key.code {
            KeyCode::Esc => {
                model.input_mode = InputMode::Normal;
                model.run_input.clear();
            }
            KeyCode::Enter => {
                let selected = model.run_input.trim().to_owned();
                match client.run(&selected).await {
                    Ok(surface) => {
                        model.replace_surface(surface);
                        model.input_mode = InputMode::Normal;
                        model.run_input.clear();
                    }
                    Err(error) => model.message = error.to_string(),
                }
            }
            KeyCode::Backspace => {
                model.run_input.pop();
            }
            KeyCode::Char(character)
                if (character.is_ascii_alphanumeric() || matches!(character, '.' | '_' | '-'))
                    && model.run_input.len() < 120 =>
            {
                model.run_input.push(character);
            }
            _ => {}
        },
        InputMode::ActionSelect => match key.code {
            KeyCode::Esc => model.close_actions(),
            KeyCode::Enter => model.begin_action_edit(),
            KeyCode::Char('j') | KeyCode::Down => model.select_next_action(),
            KeyCode::Char('k') | KeyCode::Up => model.select_previous_action(),
            _ => {}
        },
        InputMode::ActionEdit => match key.code {
            KeyCode::Esc => model.return_to_action_selection(),
            KeyCode::Char('s') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                if model.action_requires_confirmation() {
                    model.begin_action_confirmation();
                } else {
                    submit_action(model, client).await;
                }
            }
            KeyCode::Tab if key.modifiers.contains(KeyModifiers::SHIFT) => {
                model.select_previous_field();
            }
            KeyCode::BackTab => model.select_previous_field(),
            KeyCode::Tab => model.select_next_field(),
            KeyCode::Left | KeyCode::Up => model.select_previous_option(),
            KeyCode::Right | KeyCode::Down => model.select_next_option(),
            KeyCode::Enter => model.edit_action_newline(),
            KeyCode::Backspace => model.edit_action_backspace(),
            KeyCode::Char(' ') => model.edit_action_space(),
            KeyCode::Char(character)
                if !key
                    .modifiers
                    .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT) =>
            {
                model.edit_action_character(character);
            }
            _ => {}
        },
        InputMode::ActionConfirm => match key.code {
            KeyCode::Char('y') => submit_action(model, client).await,
            KeyCode::Char('n') | KeyCode::Esc => model.return_to_action_edit(),
            _ => {}
        },
        InputMode::Normal => match key.code {
            KeyCode::Char('q') => return Ok(true),
            KeyCode::Char('a') => model.begin_action_selection(),
            KeyCode::Char('w') => match client.workspace().await {
                Ok(surface) => model.replace_surface(surface),
                Err(error) => model.message = error.to_string(),
            },
            KeyCode::Char('r') => {
                model.input_mode = InputMode::RunId;
                model.run_input.clear();
            }
            KeyCode::Char('c') => {
                if let Some(run_id) = model.current_run_id().map(str::to_owned) {
                    match client.cancel_run(&run_id).await {
                        Ok(()) => match client.run(&run_id).await {
                            Ok(surface) => model.replace_surface(surface),
                            Err(error) => model.message = error.to_string(),
                        },
                        Err(error) => model.message = error.to_string(),
                    }
                } else {
                    model.message = "No run surface is active.".to_owned();
                }
            }
            KeyCode::Char('j') | KeyCode::Down => model.scroll_down(1),
            KeyCode::Char('k') | KeyCode::Up => model.scroll_up(1),
            KeyCode::PageDown => model.scroll_down(10),
            KeyCode::PageUp => model.scroll_up(10),
            KeyCode::Home => model.scroll = 0,
            _ => {}
        },
    }
    Ok(false)
}

async fn submit_action(model: &mut AppModel, client: &RuntimeClient) {
    let submission = match model.action_submission() {
        Ok(submission) => submission,
        Err(error) => {
            model.action_failed(error.to_string());
            return;
        }
    };
    let label = submission.action.label.clone();
    let current_run_id = model.current_run_id().map(str::to_owned);
    match execute_submission(client, current_run_id.as_deref(), &submission).await {
        Ok(surface) => {
            model.replace_surface(surface);
            model.message = format!("{label} accepted.");
        }
        Err(error) => model.action_failed(error.to_string()),
    }
}

async fn execute_submission(
    client: &RuntimeClient,
    current_run_id: Option<&str>,
    submission: &ActionSubmission,
) -> Result<PresentationSurface, ClientError> {
    match submission.action.operation.as_str() {
        "inspect-run" => client.run(action_identifier(submission, "run_id")?).await,
        "cancel-run" => {
            let run_id = current_run_id.ok_or(ClientError::InvalidAction)?;
            client.cancel_run(run_id).await?;
            client.run(run_id).await
        }
        "submit-run" => {
            let run_id = action_identifier(submission, "run_id")?.to_owned();
            client
                .execute_action(&submission.action, &submission.values)
                .await?;
            client.run(&run_id).await
        }
        _ => {
            client
                .execute_action(&submission.action, &submission.values)
                .await?;
            client.workspace().await
        }
    }
}

fn action_identifier<'a>(
    submission: &'a ActionSubmission,
    key: &str,
) -> Result<&'a str, ClientError> {
    submission
        .values
        .get(key)
        .and_then(serde_json::Value::as_str)
        .ok_or(ClientError::InvalidAction)
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct SurfaceRefreshRequest {
    run_id: Option<String>,
    surface_id: String,
    revision: blackcell_terminal::contract::SurfaceRevision,
}

impl SurfaceRefreshRequest {
    fn capture(model: &AppModel) -> Self {
        Self {
            run_id: model.current_run_id().map(str::to_owned),
            surface_id: model.surface.surface_id.clone(),
            revision: model.surface.revision.clone(),
        }
    }

    fn is_current(&self, model: &AppModel) -> bool {
        self.surface_id == model.surface.surface_id && self.revision == model.surface.revision
    }
}

type SurfaceRefreshResult = (
    SurfaceRefreshRequest,
    Result<PresentationSurface, ClientError>,
);

fn queue_surface_refresh(
    refreshes: &mut JoinSet<SurfaceRefreshResult>,
    pending: &mut bool,
    model: &AppModel,
    client: &RuntimeClient,
) {
    if !refreshes.is_empty() {
        *pending = true;
        return;
    }
    let request = SurfaceRefreshRequest::capture(model);
    let client = client.clone();
    refreshes.spawn(async move {
        let result = match &request.run_id {
            Some(run_id) => client.run(run_id).await,
            None => client.workspace().await,
        };
        (request, result)
    });
}

fn apply_surface_refresh(
    refresh: Result<SurfaceRefreshResult, tokio::task::JoinError>,
    model: &mut AppModel,
) -> bool {
    match refresh {
        Ok((request, _)) if !request.is_current(model) => true,
        Ok((_, Ok(surface))) => {
            model.synchronize_surface(surface);
            false
        }
        Ok((_, Err(error))) => {
            model.message = error.to_string();
            false
        }
        Err(_) => {
            model.message = "runtime-refresh-task-failed".to_owned();
            false
        }
    }
}

struct TerminalSession {
    terminal: Terminal<CrosstermBackend<Stdout>>,
}

impl TerminalSession {
    fn start() -> Result<Self, AppError> {
        enable_raw_mode().map_err(|_| AppError::Terminal)?;
        let mut stdout = io::stdout();
        if execute!(stdout, EnterAlternateScreen).is_err() {
            let _ = disable_raw_mode();
            return Err(AppError::Terminal);
        }
        let terminal = match Terminal::new(CrosstermBackend::new(stdout)) {
            Ok(terminal) => terminal,
            Err(_) => {
                let _ = execute!(io::stdout(), LeaveAlternateScreen);
                let _ = disable_raw_mode();
                return Err(AppError::Terminal);
            }
        };
        Ok(Self { terminal })
    }
}

impl Drop for TerminalSession {
    fn drop(&mut self) {
        let _ = disable_raw_mode();
        let _ = execute!(self.terminal.backend_mut(), LeaveAlternateScreen);
        let _ = self.terminal.show_cursor();
    }
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;

    use blackcell_terminal::config::{API_TOKEN_ENV, ENDPOINT_ENV};
    use serde_json::Value;

    use super::*;

    const SCENARIO: &str = include_str!("../../../tests/ui/review-workflow.json");
    const TOKEN: &str = "Native-terminal-token.0123456789-ABCDEFG";

    #[tokio::test]
    async fn refresh_requests_are_backgrounded_and_coalesced() {
        let model = workspace_model();
        let client = local_client();
        let mut refreshes = JoinSet::new();
        let mut pending = false;

        queue_surface_refresh(&mut refreshes, &mut pending, &model, &client);
        queue_surface_refresh(&mut refreshes, &mut pending, &model, &client);

        assert_eq!(refreshes.len(), 1);
        assert!(pending);
        refreshes.abort_all();
        while refreshes.join_next().await.is_some() {}
    }

    #[test]
    fn completed_refresh_is_rejected_after_the_visible_revision_changes() {
        let mut model = workspace_model();
        let request = SurfaceRefreshRequest::capture(&model);

        model.surface.revision.number += 1;

        assert!(!request.is_current(&model));
    }

    fn local_client() -> RuntimeClient {
        let environment = HashMap::from([
            (API_TOKEN_ENV.to_owned(), TOKEN.to_owned()),
            (ENDPOINT_ENV.to_owned(), "http://127.0.0.1:9".to_owned()),
        ]);
        let ParseOutcome::Run(config) = Config::parse_from(Vec::<String>::new(), &environment)
            .expect("test configuration must be valid")
        else {
            panic!("expected runnable configuration");
        };
        RuntimeClient::new(config.endpoint, config.token).expect("test client must be valid")
    }

    fn workspace_model() -> AppModel {
        let value =
            serde_json::from_str::<Value>(SCENARIO).expect("scenario must be valid")["surfaces"][0]
                .clone();
        let surface = PresentationSurface::decode(
            &serde_json::to_vec(&value).expect("surface must serialize"),
        )
        .expect("surface must satisfy the contract");
        AppModel::new(surface)
    }
}

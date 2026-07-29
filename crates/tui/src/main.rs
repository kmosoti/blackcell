use std::io::{self, Stdout};
use std::process::ExitCode;
use std::time::Duration;

use blackcell_terminal::client::{ClientError, RuntimeClient};
use blackcell_terminal::config::{Config, ConfigError, ParseOutcome, help};
use blackcell_terminal::view::{AppModel, InputMode, view};
use crossterm::event::{Event, EventStream, KeyCode, KeyEventKind};
use crossterm::execute;
use crossterm::terminal::{
    EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode,
};
use futures_util::StreamExt;
use ratatui::Terminal;
use ratatui::backend::CrosstermBackend;
use thiserror::Error;
use tokio::sync::mpsc;
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
            eprintln!("{error}");
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
                    refresh_surface(model, &client).await;
                }
            }
            _ = refresh_tick.tick(), if config.refresh.is_some() => {
                refresh_surface(model, &client).await;
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
        InputMode::Normal => match key.code {
            KeyCode::Char('q') => return Ok(true),
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

async fn refresh_surface(model: &mut AppModel, client: &RuntimeClient) {
    let result = if let Some(run_id) = model.current_run_id() {
        client.run(run_id).await
    } else {
        client.workspace().await
    };
    match result {
        Ok(surface) => model.replace_surface(surface),
        Err(error) => model.message = error.to_string(),
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

//! Strict process configuration with credentials sourced only from environment or owner-only files.

use std::env;
use std::fmt;
use std::fs::OpenOptions;
use std::io::Read;
use std::net::IpAddr;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::Path;
use std::time::Duration;

use thiserror::Error;
use url::Url;

pub const API_TOKEN_ENV: &str = "BLACKCELL_API_TOKEN";
pub const API_TOKEN_FILE_ENV: &str = "BLACKCELL_API_TOKEN_FILE";
pub const ENDPOINT_ENV: &str = "BLACKCELL_RUNTIME_ENDPOINT";
const DEFAULT_ENDPOINT: &str = "http://127.0.0.1:8080";

#[derive(Debug, Error, Clone, Copy, PartialEq, Eq)]
pub enum ConfigError {
    #[error("invalid-tui-arguments")]
    InvalidArguments,
    #[error("invalid-runtime-endpoint")]
    InvalidEndpoint,
    #[error("missing-secret")]
    MissingSecret,
    #[error("ambiguous-secret-source")]
    AmbiguousSecret,
    #[error("invalid-secret")]
    InvalidSecret,
    #[error("unsafe-secret-file")]
    UnsafeSecretFile,
}

#[derive(Clone)]
pub struct Secret(String);

impl fmt::Debug for Secret {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("Secret([REDACTED])")
    }
}

impl Secret {
    pub fn expose(&self) -> &str {
        &self.0
    }
}

#[derive(Debug, Clone)]
pub struct Config {
    pub endpoint: Url,
    pub token: Secret,
    pub refresh: Option<Duration>,
    pub frames_per_second: u16,
}

pub enum ParseOutcome {
    Run(Config),
    Help,
    Version,
}

impl Config {
    pub fn parse() -> Result<ParseOutcome, ConfigError> {
        Self::parse_from(env::args().skip(1), &env::vars().collect())
    }

    pub fn parse_from<I>(
        arguments: I,
        environment: &std::collections::HashMap<String, String>,
    ) -> Result<ParseOutcome, ConfigError>
    where
        I: IntoIterator,
        I::Item: Into<String>,
    {
        let mut endpoint = environment
            .get(ENDPOINT_ENV)
            .cloned()
            .unwrap_or_else(|| DEFAULT_ENDPOINT.to_owned());
        let mut refresh = Some(Duration::from_secs(1));
        let mut frames_per_second = 20_u16;
        let mut arguments = arguments.into_iter().map(Into::into);
        while let Some(argument) = arguments.next() {
            match argument.as_str() {
                "--help" | "-h" => return Ok(ParseOutcome::Help),
                "--version" | "-V" => return Ok(ParseOutcome::Version),
                "--endpoint" => endpoint = arguments.next().ok_or(ConfigError::InvalidArguments)?,
                "--refresh-seconds" => {
                    let value = arguments.next().ok_or(ConfigError::InvalidArguments)?;
                    refresh = if value == "none" {
                        None
                    } else {
                        let seconds: f64 =
                            value.parse().map_err(|_| ConfigError::InvalidArguments)?;
                        if !seconds.is_finite() || !(0.25..=60.0).contains(&seconds) {
                            return Err(ConfigError::InvalidArguments);
                        }
                        Some(Duration::from_secs_f64(seconds))
                    };
                }
                "--frames-per-second" => {
                    frames_per_second = arguments
                        .next()
                        .ok_or(ConfigError::InvalidArguments)?
                        .parse()
                        .map_err(|_| ConfigError::InvalidArguments)?;
                    if !(1..=60).contains(&frames_per_second) {
                        return Err(ConfigError::InvalidArguments);
                    }
                }
                _ => return Err(ConfigError::InvalidArguments),
            }
        }
        Ok(ParseOutcome::Run(Self {
            endpoint: normalize_endpoint(&endpoint)?,
            token: load_token(environment)?,
            refresh,
            frames_per_second,
        }))
    }
}

pub fn help() -> &'static str {
    "blackcell-tui\n\nUSAGE:\n  blackcell-tui [--endpoint URL] [--refresh-seconds SECONDS|none] [--frames-per-second 1..60]\n\nCredentials are read from BLACKCELL_API_TOKEN or BLACKCELL_API_TOKEN_FILE; they are never accepted as arguments.\n"
}

fn normalize_endpoint(value: &str) -> Result<Url, ConfigError> {
    if value.is_empty() || value.len() > 2_048 || value.chars().any(char::is_whitespace) {
        return Err(ConfigError::InvalidEndpoint);
    }
    let mut url = Url::parse(value).map_err(|_| ConfigError::InvalidEndpoint)?;
    if !matches!(url.scheme(), "http" | "https")
        || url.host_str().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
        || !matches!(url.path(), "" | "/")
    {
        return Err(ConfigError::InvalidEndpoint);
    }
    if url.scheme() == "http" && !loopback_host(url.host_str().unwrap_or_default()) {
        return Err(ConfigError::InvalidEndpoint);
    }
    url.set_path("");
    Ok(url)
}

fn loopback_host(host: &str) -> bool {
    host.eq_ignore_ascii_case("localhost")
        || host
            .parse::<IpAddr>()
            .is_ok_and(|address| address.is_loopback())
}

fn load_token(
    environment: &std::collections::HashMap<String, String>,
) -> Result<Secret, ConfigError> {
    match (
        environment.get(API_TOKEN_ENV),
        environment.get(API_TOKEN_FILE_ENV),
    ) {
        (Some(_), Some(_)) => Err(ConfigError::AmbiguousSecret),
        (None, None) => Err(ConfigError::MissingSecret),
        (Some(value), None) => validate_secret(value),
        (None, Some(path)) => read_secret_file(Path::new(path)),
    }
}

fn read_secret_file(path: &Path) -> Result<Secret, ConfigError> {
    if !path.is_absolute() {
        return Err(ConfigError::UnsafeSecretFile);
    }
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
        .open(path)
        .map_err(|_| ConfigError::UnsafeSecretFile)?;
    let metadata = file.metadata().map_err(|_| ConfigError::UnsafeSecretFile)?;
    if !metadata.is_file()
        || metadata.permissions().mode() & 0o777 != 0o600
        || metadata.uid() != rustix::process::getuid().as_raw()
        || metadata.len() > 4_097
    {
        return Err(ConfigError::UnsafeSecretFile);
    }
    let mut content = Vec::with_capacity(metadata.len() as usize);
    file.take(4_098)
        .read_to_end(&mut content)
        .map_err(|_| ConfigError::UnsafeSecretFile)?;
    if content.len() > 4_097 {
        return Err(ConfigError::UnsafeSecretFile);
    }
    let mut value = String::from_utf8(content).map_err(|_| ConfigError::InvalidSecret)?;
    if value.ends_with('\n') {
        value.pop();
    }
    if value.contains(['\n', '\r']) {
        return Err(ConfigError::InvalidSecret);
    }
    validate_secret(&value)
}

fn validate_secret(value: &str) -> Result<Secret, ConfigError> {
    if !(32..=4_096).contains(&value.len())
        || value.contains(',')
        || value.bytes().any(|byte| !(0x21..=0x7e).contains(&byte))
        || value
            .bytes()
            .collect::<std::collections::HashSet<_>>()
            .len()
            < 8
    {
        return Err(ConfigError::InvalidSecret);
    }
    Ok(Secret(value.to_owned()))
}

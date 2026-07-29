//! Bounded authenticated client for presentation snapshots and event invalidations.

use std::time::Duration;

use futures_util::StreamExt;
use reqwest::header::{ACCEPT, AUTHORIZATION, CONTENT_LENGTH, CONTENT_TYPE, HeaderValue};
use serde::{Deserialize, Serialize};
use thiserror::Error;
use tokio::sync::mpsc;
use tokio_tungstenite::tungstenite::Message;
use url::Url;
use uuid::Uuid;

use crate::config::Secret;
use crate::contract::PresentationSurface;

const PRESENTATION_MEDIA_TYPE: &str = "application/vnd.blackcell.presentation+json";
const MAX_SURFACE_BYTES: usize = 16 * 1024 * 1024;
const MAX_EVENT_BYTES: usize = 8 * 1024 * 1024;

#[derive(Debug, Error, Clone, Copy, PartialEq, Eq)]
pub enum ClientError {
    #[error("runtime-connection-failed")]
    ConnectionFailed,
    #[error("runtime-request-rejected")]
    RequestRejected,
    #[error("invalid-runtime-response")]
    InvalidResponse,
    #[error("runtime-response-too-large")]
    ResponseTooLarge,
    #[error("invalid-run-id")]
    InvalidRunId,
    #[error("event-stream-failed")]
    EventStreamFailed,
}

#[derive(Clone)]
pub struct RuntimeClient {
    endpoint: Url,
    token: Secret,
    http: reqwest::Client,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct SocketTicket {
    ticket: String,
    expires_in_seconds: u16,
    websocket_path: String,
    schema_version: String,
}

#[derive(Debug, Deserialize)]
struct EventPage {
    next_cursor: u64,
}

#[derive(Debug, Serialize)]
struct CancelRequest {
    schema_version: &'static str,
    idempotency_key: String,
}

impl RuntimeClient {
    pub fn new(endpoint: Url, token: Secret) -> Result<Self, ClientError> {
        let http = reqwest::Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(Duration::from_secs(30))
            .build()
            .map_err(|_| ClientError::ConnectionFailed)?;
        Ok(Self {
            endpoint,
            token,
            http,
        })
    }

    pub async fn workspace(&self) -> Result<PresentationSurface, ClientError> {
        self.surface("/api/v1/ui/surfaces/workspace").await
    }

    pub async fn run(&self, run_id: &str) -> Result<PresentationSurface, ClientError> {
        let run_id = valid_run_id(run_id)?;
        self.surface(&format!("/api/v1/ui/surfaces/runs/{run_id}"))
            .await
    }

    pub async fn cancel_run(&self, run_id: &str) -> Result<(), ClientError> {
        let run_id = valid_run_id(run_id)?;
        let response = self
            .authorized(
                self.http
                    .post(self.url(&format!("/api/v1/runs/{run_id}/cancel"))?),
            )?
            .json(&CancelRequest {
                schema_version: "execution-cancel-run-request/v1",
                idempotency_key: format!("tui-cancel-{}", Uuid::new_v4()),
            })
            .send()
            .await
            .map_err(|_| ClientError::ConnectionFailed)?;
        if !response.status().is_success() {
            return Err(ClientError::RequestRejected);
        }
        Ok(())
    }

    pub async fn follow_invalidations(self, after: u64, sender: mpsc::Sender<u64>) {
        let mut cursor = after;
        loop {
            match self.follow_once(cursor, &sender).await {
                Ok(next) => cursor = next,
                Err(_) => tokio::time::sleep(Duration::from_millis(500)).await,
            }
            if sender.is_closed() {
                return;
            }
        }
    }

    async fn surface(&self, path: &str) -> Result<PresentationSurface, ClientError> {
        let response = self
            .authorized(self.http.get(self.url(path)?))?
            .header(ACCEPT, PRESENTATION_MEDIA_TYPE)
            .send()
            .await
            .map_err(|_| ClientError::ConnectionFailed)?;
        if !response.status().is_success() {
            return Err(ClientError::RequestRejected);
        }
        if media_type(response.headers().get(CONTENT_TYPE)) != Some(PRESENTATION_MEDIA_TYPE) {
            return Err(ClientError::InvalidResponse);
        }
        let content = bounded_body(response, MAX_SURFACE_BYTES).await?;
        PresentationSurface::decode(&content).map_err(|_| ClientError::InvalidResponse)
    }

    async fn follow_once(
        &self,
        after: u64,
        sender: &mpsc::Sender<u64>,
    ) -> Result<u64, ClientError> {
        let ticket = self.socket_ticket().await?;
        let mut url = self
            .endpoint
            .join(&ticket.websocket_path)
            .map_err(|_| ClientError::InvalidResponse)?;
        let scheme = if url.scheme() == "https" { "wss" } else { "ws" };
        url.set_scheme(scheme)
            .map_err(|_| ClientError::InvalidResponse)?;
        url.query_pairs_mut()
            .append_pair("ticket", &ticket.ticket)
            .append_pair("after", &after.to_string());
        let (mut socket, _) = tokio_tungstenite::connect_async(url.as_str())
            .await
            .map_err(|_| ClientError::EventStreamFailed)?;
        let mut cursor = after;
        while let Some(message) = socket.next().await {
            let message = message.map_err(|_| ClientError::EventStreamFailed)?;
            let content: &[u8] = match &message {
                Message::Binary(value) => value.as_ref(),
                Message::Text(value) => value.as_bytes(),
                Message::Close(_) => return Ok(cursor),
                Message::Ping(_) | Message::Pong(_) | Message::Frame(_) => continue,
            };
            if content.len() > MAX_EVENT_BYTES {
                return Err(ClientError::ResponseTooLarge);
            }
            let page: EventPage =
                serde_json::from_slice(content).map_err(|_| ClientError::InvalidResponse)?;
            if page.next_cursor < cursor {
                return Err(ClientError::InvalidResponse);
            }
            cursor = page.next_cursor;
            if sender.send(cursor).await.is_err() {
                return Ok(cursor);
            }
        }
        Ok(cursor)
    }

    async fn socket_ticket(&self) -> Result<SocketTicket, ClientError> {
        let response = self
            .authorized(self.http.post(self.url("/api/v1/ui/socket-tickets")?))?
            .header(ACCEPT, "application/json")
            .send()
            .await
            .map_err(|_| ClientError::ConnectionFailed)?;
        if !response.status().is_success() {
            return Err(ClientError::RequestRejected);
        }
        let content = bounded_body(response, 8 * 1024).await?;
        let ticket: SocketTicket =
            serde_json::from_slice(&content).map_err(|_| ClientError::InvalidResponse)?;
        if ticket.schema_version != "execution-web-socket-ticket/v1"
            || ticket.websocket_path != "/api/v1/ui/events"
            || !(1..=60).contains(&ticket.expires_in_seconds)
            || !(32..=128).contains(&ticket.ticket.len())
            || !ticket
                .ticket
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
        {
            return Err(ClientError::InvalidResponse);
        }
        Ok(ticket)
    }

    fn authorized(
        &self,
        request: reqwest::RequestBuilder,
    ) -> Result<reqwest::RequestBuilder, ClientError> {
        let value = HeaderValue::from_str(&format!("Bearer {}", self.token.expose()))
            .map_err(|_| ClientError::InvalidResponse)?;
        Ok(request.header(AUTHORIZATION, value))
    }

    fn url(&self, path: &str) -> Result<Url, ClientError> {
        if !path.starts_with("/api/v1/") || path.contains("..") {
            return Err(ClientError::InvalidResponse);
        }
        self.endpoint
            .join(path)
            .map_err(|_| ClientError::InvalidResponse)
    }
}

async fn bounded_body(response: reqwest::Response, maximum: usize) -> Result<Vec<u8>, ClientError> {
    if response
        .headers()
        .get(CONTENT_LENGTH)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.parse::<usize>().ok())
        .is_some_and(|length| length > maximum)
    {
        return Err(ClientError::ResponseTooLarge);
    }
    let mut body = Vec::new();
    let mut stream = response.bytes_stream();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|_| ClientError::ConnectionFailed)?;
        if body.len().saturating_add(chunk.len()) > maximum {
            return Err(ClientError::ResponseTooLarge);
        }
        body.extend_from_slice(&chunk);
    }
    if body.is_empty() {
        return Err(ClientError::InvalidResponse);
    }
    Ok(body)
}

fn media_type(value: Option<&HeaderValue>) -> Option<&str> {
    value
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.split(';').next())
        .map(str::trim)
}

fn valid_run_id(value: &str) -> Result<&str, ClientError> {
    if !(1..=120).contains(&value.len())
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
    {
        return Err(ClientError::InvalidRunId);
    }
    Ok(value)
}

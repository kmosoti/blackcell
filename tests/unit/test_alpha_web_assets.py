from __future__ import annotations

import re
from pathlib import Path

from blackcell.interfaces.http import MAX_RESPONSE_BODY_BYTES, AlphaWebTicketAuthority
from blackcell.interfaces.http.alpha_web_assets import load_alpha_web_assets
from tests.unit.test_alpha_web import _client, _service


def test_alpha_web_assets_are_bounded_packaged_and_closed() -> None:
    assets = load_alpha_web_assets()

    assert 0 < len(assets.html) <= 64 * 1024
    assert 0 < len(assets.css) <= 128 * 1024
    assert 0 < len(assets.javascript) <= 256 * 1024
    assert repr(assets) == "AlphaWebAssets()"

    html = assets.html.decode("utf-8")
    for content in (assets.html, assets.css, assets.javascript):
        assert b"\x00" not in content
        content.decode("utf-8")

    assert '<link rel="stylesheet" href="/alpha/assets/app.css">' in html
    assert '<script type="module" src="/alpha/assets/app.js"></script>' in html
    assert html.count("<script") == 1
    assert "<style" not in html
    assert "http://" not in html
    assert "https://" not in html


def test_alpha_web_routes_emit_accessible_shell_and_restrictive_security_headers(
    tmp_path: Path,
) -> None:
    assets = load_alpha_web_assets()
    expected = {
        "/alpha": ("text/html", assets.html),
        "/alpha/": ("text/html", assets.html),
        "/alpha/assets/app.css": ("text/css", assets.css),
        "/alpha/assets/app.js": ("application/javascript", assets.javascript),
    }

    with _client(_service(tmp_path), authority=AlphaWebTicketAuthority()) as client:
        responses = {path: client.get(path) for path in expected}
        unknown = client.get("/alpha/assets/unknown.js")

    for path, response in responses.items():
        media_type, content = expected[path]
        assert response.status_code == 200
        assert response.content == content
        assert response.headers["content-type"].startswith(media_type)
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["cross-origin-opener-policy"] == "same-origin"
        assert response.headers["cross-origin-resource-policy"] == "same-origin"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["permissions-policy"] == (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        )
        assert response.headers["content-security-policy"] == (
            "default-src 'none'; base-uri 'none'; connect-src 'self'; form-action 'self'; "
            "frame-ancestors 'none'; script-src 'self'; style-src 'self'"
        )
        assert "set-cookie" not in response.headers

    html = responses["/alpha"].text
    assert '<html lang="en">' in html
    assert '<a class="skip-link" href="#workspace">' in html
    assert 'role="status" aria-live="polite"' in html
    assert '<label for="api-token">' in html
    assert '<label for="run-id">' in html
    assert '<caption class="visually-hidden">' in html
    assert 'aria-label="Run operation result"' in html
    token_control = html[
        html.index('id="api-token"') : html.index(">", html.index('id="api-token"'))
    ]
    assert "value=" not in token_control
    assert unknown.status_code == 404


def test_browser_client_keeps_credentials_in_memory_and_uses_only_alpha_contracts() -> None:
    assets = load_alpha_web_assets()
    javascript = assets.javascript.decode("utf-8")
    css = assets.css.decode("utf-8")

    for forbidden in (
        "localStorage",
        "sessionStorage",
        "document.cookie",
        "innerHTML",
        "eval(",
        "/api/v1/runs",
        "http://",
        "https://",
    ):
        assert forbidden not in javascript

    for required in (
        'elements.token.value = "";',
        "Authorization",
        'credentials: "omit"',
        'redirect: "error"',
        'response.headers.get("content-type")',
        "response.arrayBuffer()",
        "new URL(path, window.location.origin)",
        '"/api/alpha/v1/ui/socket-tickets"',
        "new WebSocket(url)",
        'url.searchParams.set("ticket", ticket.ticket)',
        'url.searchParams.set("after", String(state.cursor))',
        "MAX_RETAINED_EVENTS = 200",
        "validateEventPage(page, state.cursor)",
        "validateRunResponse",
        "validateReplayResponse",
        "hasExactKeys",
        "textContent",
        "replaceChildren",
        'window.addEventListener("pagehide"',
        'operation === "status"',
        'operation === "replay"',
        'operation === "cancel"',
    ):
        assert required in javascript

    assert ":focus-visible" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "@import" not in css
    assert "http://" not in css
    assert "https://" not in css


def test_browser_response_ceiling_matches_the_http_service_contract() -> None:
    javascript = load_alpha_web_assets().javascript.decode("utf-8")
    request_match = re.search(r"const MAX_REQUEST_BYTES = ([0-9_]+);", javascript)
    response_match = re.search(
        r"const MAX_RESPONSE_BYTES = ([0-9_]+) \* MAX_REQUEST_BYTES;",
        javascript,
    )

    assert request_match is not None
    assert response_match is not None
    request_bytes = int(request_match.group(1).replace("_", ""))
    response_multiplier = int(response_match.group(1).replace("_", ""))
    assert request_bytes * response_multiplier == MAX_RESPONSE_BODY_BYTES


def test_alpha_web_workflow_surface_is_bounded_closed_and_accessible() -> None:
    assets = load_alpha_web_assets()
    html = assets.html.decode("utf-8")
    javascript = assets.javascript.decode("utf-8")
    css = assets.css.decode("utf-8")

    for required in (
        'aria-labelledby="workflow-heading"',
        '<form id="workflow-form"',
        '<label for="workflow-operation">',
        '<label for="workflow-file">',
        'id="workflow-file"',
        'type="file"',
        'accept=".json,application/json"',
        'aria-describedby="workflow-note"',
        'id="workflow-message" class="message" role="status" aria-live="polite"',
        'aria-label="Workflow contract result"',
        "File contents and paths are not",
    ):
        assert required in html
    for operation in ("project", "intent", "plan", "run"):
        assert f'<option value="{operation}">' in html

    for required in (
        "MAX_REQUEST_BYTES = 1_048_576",
        "MAX_ACCEPTANCE_TIMEOUT_SECONDS = 600",
        "EXECUTABLE_ALIAS = /^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$/",
        "file.arrayBuffer()",
        'new TextDecoder("utf-8", { fatal: true })',
        'elements.workflowFile.value = ""',
        "new AbortController()",
        "state.workflowAbort.abort()",
        'this._post("/api/alpha/v1/projects"',
        'this._post("/api/alpha/v1/intents"',
        'this._post("/api/alpha/v1/plans"',
        'this._post("/api/alpha/v1/runs"',
        "validateProjectRequest",
        "validateIntentRequest",
        "validatePlanRequest",
        "validateRunRequest",
        "validateProjectResponse",
        "validateIntentResponse",
        "validatePlanResponse",
        "validateRunResponse",
        "planTopologicalOrder",
        "writersAreOrdered",
        "node.budget.max_input_tokens >= 1",
        "node.budget.max_output_tokens >= 1",
        "boundedInteger(value.timeout_seconds, 1, MAX_ACCEPTANCE_TIMEOUT_SECONDS)",
        "EXECUTABLE_ALIAS.test(value.argv[0])",
        'part !== ".git"',
        "sameJsonValue",
        "elements.workflowOutput.textContent",
        "elements.runId.value = value.run_id",
    ):
        assert required in javascript

    for forbidden in (
        "file.name",
        "file.path",
        "webkitRelativePath",
        "FileReader",
        "state.workflowRequest",
        "state.request",
        "/api/v1/runs",
        "86_400",
    ):
        assert forbidden not in javascript

    assert ".workflow-grid" in css
    assert ".workflow-output" in css
    assert "@media (max-width: 42rem)" in css

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from blackcell.interfaces.http import WebTicketAuthority
from blackcell.interfaces.http import web_assets as web_assets_module
from blackcell.interfaces.http.web_assets import WebAssetError, _asset, load_web_assets
from tests.unit.test_web import _client, _service


def test_web_assets_are_bounded_packaged_source_modules() -> None:
    assets = load_web_assets()
    content = (
        assets.html,
        assets.css,
        assets.javascript,
        assets.runtime_client_javascript,
        assets.surface_elements_javascript,
        assets.tokens,
    )

    assert 0 < len(assets.html) <= 64 * 1024
    assert 0 < len(assets.css) <= 128 * 1024
    assert all(0 < len(module) <= 256 * 1024 for module in content[2:5])
    assert 0 < len(assets.tokens) <= 32 * 1024
    assert sum(len(item) for item in content) <= 250 * 1024
    assert repr(assets) == "WebAssets()"
    for item in content:
        assert b"\x00" not in item
        item.decode("utf-8")

    html = assets.html.decode("utf-8")
    assert '<link rel="stylesheet" href="/ui/assets/app.css">' in html
    assert '<script type="module" src="/ui/assets/app.js"></script>' in html
    assert html.count("<script") == 1
    assert "<style" not in html
    assert "http://" not in html
    assert "https://" not in html


@pytest.mark.parametrize(
    "content",
    (
        cast("bytes", "not-bytes"),
        b"",
        b"abcd",
        b"nul\x00byte",
        b"\xff",
    ),
)
def test_web_asset_loader_rejects_invalid_or_unbounded_bytes(content: bytes) -> None:
    with pytest.raises(WebAssetError) as caught:
        _asset(content, 3)

    assert str(caught.value) == "execution-web-invalid-asset"


def test_web_asset_loader_normalizes_packaging_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    load_web_assets.cache_clear()
    monkeypatch.setattr(
        web_assets_module,
        "files",
        lambda package: (_ for _ in ()).throw(OSError(package)),
    )

    with pytest.raises(WebAssetError) as caught:
        load_web_assets()

    load_web_assets.cache_clear()
    assert str(caught.value) == "execution-web-invalid-asset"


def test_web_routes_emit_accessible_shell_modules_and_security_headers(
    tmp_path: Path,
) -> None:
    assets = load_web_assets()
    expected = {
        "/ui": ("text/html", assets.html),
        "/ui/assets/app.css": ("text/css", assets.css),
        "/ui/assets/app.js": ("application/javascript", assets.javascript),
        "/ui/assets/runtime-client.js": (
            "application/javascript",
            assets.runtime_client_javascript,
        ),
        "/ui/assets/surface-elements.js": (
            "application/javascript",
            assets.surface_elements_javascript,
        ),
        "/ui/assets/tokens.json": ("application/json", assets.tokens),
    }

    with _client(_service(tmp_path), authority=WebTicketAuthority()) as client:
        responses = {path: client.get(path) for path in expected}
        unknown = client.get("/ui/assets/unknown.js")

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

    html = responses["/ui"].text
    assert '<html lang="en">' in html
    assert '<a class="skip-link" href="#surface">' in html
    assert 'role="status" aria-live="polite"' in html
    assert '<label for="api-token">' in html
    assert '<label for="run-id">' in html
    assert '<blackcell-surface id="surface" aria-busy="false">' in html
    token_control = html[
        html.index('id="api-token"') : html.index(">", html.index('id="api-token"'))
    ]
    assert "value=" not in token_control
    assert unknown.status_code == 404


def test_browser_keeps_credentials_in_memory_and_uses_closed_semantic_actions() -> None:
    assets = load_web_assets()
    application = assets.javascript.decode("utf-8")
    client = assets.runtime_client_javascript.decode("utf-8")
    renderer = assets.surface_elements_javascript.decode("utf-8")
    combined = "\n".join((application, client, renderer))

    for forbidden in (
        "localStorage",
        "sessionStorage",
        "document.cookie",
        "innerHTML",
        "eval(",
        "http://runtime",
        "https://runtime",
    ):
        assert forbidden not in combined

    for required in (
        'elements.token.value = "";',
        "Authorization",
        'credentials: "omit"',
        'redirect: "error"',
        '"/api/v1/ui/surfaces/workspace"',
        '"/api/v1/ui/socket-tickets"',
        '"register-project"',
        '"accept-intent"',
        '"accept-plan"',
        '"submit-run"',
        '"cancel-run"',
        "validateSurface",
        "semanticManifest",
        "textContent",
        "replaceChildren",
        'window.addEventListener("pagehide"',
    ):
        assert required in combined

    assert "new WebSocket" in application
    assert 'url.searchParams.set("ticket", ticket.ticket)' in client
    assert 'url.searchParams.set("after", String(cursor))' in client
    assert 'document.createElementNS(SVG, "svg")' in renderer
    assert 'document.createElement("table")' in renderer


def test_browser_styles_and_tokens_preserve_accessible_functional_defaults() -> None:
    assets = load_web_assets()
    css = assets.css.decode("utf-8")
    tokens = json.loads(assets.tokens)

    assert ":focus-visible" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "@media (forced-colors: active)" in css
    assert "@media (max-width: 48rem)" in css
    assert ".visually-hidden" in css
    assert "@import" not in css
    assert "http://" not in css
    assert "https://" not in css
    assert "$schema" not in tokens
    assert set(tokens["color"]) >= {
        "background",
        "surface",
        "text",
        "muted",
        "accent",
        "success",
        "warning",
        "danger",
        "focus",
    }
    assert all(
        token["$type"] == "color"
        and token["$value"]["colorSpace"] == "srgb"
        and len(token["$value"]["components"]) == 3
        for token in tokens["color"].values()
    )


def test_raw_json_is_secondary_to_semantic_graph_table_and_evidence_components() -> None:
    renderer = load_web_assets().surface_elements_javascript.decode("utf-8")

    assert 'case "plan-graph"' in renderer
    assert 'case "table"' in renderer
    assert 'case "evidence-matrix"' in renderer
    assert 'case "artifacts"' in renderer
    assert 'case "source"' in renderer
    assert 'document.createElement("details")' in renderer
    assert 'button.textContent = "Load canonical JSON"' in renderer
    assert "The adjacent plan table contains the same nodes and dependencies." in renderer

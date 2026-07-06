from pathlib import Path


def test_containerfile_installs_python_node_npm_and_opencode() -> None:
    text = Path("Containerfile").read_text(encoding="utf-8")

    assert "ghcr.io/astral-sh/uv:python3.14-trixie-slim" in text
    assert "NVM_VERSION" in text
    assert "nvm install" in text
    assert "npm install -g npm@latest" in text
    assert "opencode-ai" in text
    assert "uv sync --locked --dev" in text


def test_devcontainer_and_compose_mount_user_local_opencode_config() -> None:
    devcontainer = Path(".devcontainer/devcontainer.json").read_text(encoding="utf-8")
    compose = Path("compose.yaml").read_text(encoding="utf-8")

    assert "blackcell-opencode" in devcontainer
    assert "/root/.config/opencode" in devcontainer
    assert "blackcell-opencode" in compose
    assert "/root/.config/opencode" in compose


def test_nvmrc_matches_container_node_major() -> None:
    assert Path(".nvmrc").read_text(encoding="utf-8").strip() == "22"

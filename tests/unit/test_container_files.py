from pathlib import Path


def test_containerfile_installs_reproducible_python_runtime_only() -> None:
    text = Path("Containerfile").read_text(encoding="utf-8")

    assert "ghcr.io/astral-sh/uv:python3.14-trixie-slim" in text
    assert "uv sync --locked --all-groups" in text
    assert "NVM_VERSION" not in text
    assert "curl" not in text
    assert "npm" not in text
    assert "opencode-ai" not in text


def test_devcontainer_and_compose_do_not_mount_model_credentials() -> None:
    devcontainer = Path(".devcontainer/devcontainer.json").read_text(encoding="utf-8")
    compose = Path("compose.yaml").read_text(encoding="utf-8")

    assert "blackcell-opencode" not in devcontainer
    assert "/root/.config/opencode" not in devcontainer
    assert "blackcell-opencode" not in compose
    assert "/root/.config/opencode" not in compose


def test_node_version_file_is_not_referenced_by_core_runtime() -> None:
    assert ".nvmrc" not in Path("Containerfile").read_text(encoding="utf-8")
